"""Telegram alerts when a store goes red, and again when it recovers.

Every 10 minutes each gunicorn worker runs a check, but a Postgres advisory
lock lets only one of them work at a time, and every store's last status is
kept in store_alert_state, so a change is only ever announced once:
  - into "problem": a red alert (at most one per store every 6 hours)
  - from an announced problem back to "ok": a recovered message
Stores still being set up ("setup") never alert. Until alerts are switched on
in the hub, nothing is checked or recorded, so a store that is already red
gets its alert as soon as they are.

The bot token and chat ID are kept encrypted in hub_settings, edited on the
hub's Alerts page; no Railway variable is involved.
"""
import logging
import os
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

import stores

logger = logging.getLogger(__name__)

SETTING = "alerts"
CHECK_SECONDS = 600
COOLDOWN = timedelta(hours=6)
LOCK_ID = 724505
BOT_TOKEN_RE = re.compile(r"[0-9]{5,15}:[A-Za-z0-9_-]{30,60}")
CHAT_ID_RE = re.compile(r"-?[0-9]{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31}")
TELEGRAM = "https://api.telegram.org"


def load_config():
    """{"bot_token", "chat_id", "enabled"}, or {} when not set up."""
    try:
        config = stores.get_setting(SETTING)
    except Exception as e:
        logger.error(f"alert settings read failed: {type(e).__name__}")
        return {}
    return config if BOT_TOKEN_RE.fullmatch(config.get("bot_token") or "") else {}


def ready(config):
    return bool(config.get("enabled") and config.get("bot_token") and config.get("chat_id"))


def telegram_send(config, text):
    """(True, None) when Telegram delivered the message, else (False, reason).
    The bot token is part of Telegram's URL, so it never goes in a log line."""
    try:
        r = requests.post(f"{TELEGRAM}/bot{config['bot_token']}/sendMessage",
                          json={"chat_id": config["chat_id"], "text": text,
                                "disable_web_page_preview": True}, timeout=10)
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        return False, f"Could not reach Telegram ({type(e).__name__})"
    if r.status_code == 200 and body.get("ok"):
        return True, None
    return False, str(body.get("description") or f"Telegram answered {r.status_code}")[:200]


def find_chat_id(bot_token):
    """(chat_id, chat name) from the newest message sent to the bot, or (None, reason)."""
    try:
        r = requests.get(f"{TELEGRAM}/bot{bot_token}/getUpdates", timeout=10)
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        return None, f"Could not reach Telegram ({type(e).__name__})"
    if r.status_code != 200 or not body.get("ok"):
        return None, str(body.get("description") or f"Telegram answered {r.status_code}")[:200]
    for update in reversed(body.get("result") or []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if isinstance(chat.get("id"), int):
            name = chat.get("title") or " ".join(
                p for p in (chat.get("first_name"), chat.get("last_name")) if p) or chat.get("username") or ""
            return str(chat["id"]), name
    return None, "No message found. Open your bot in Telegram, press Start (or send it any message), then try again."


def store_link(store):
    host = (os.environ.get("HUB_PUBLIC_HOST") or "").strip().lower()
    return f"\nhttps://{host}/hub/stores/{store['id']}" if host else ""


def problem_text(store, reason):
    return f"🔴 {store['name']}: {reason}.{store_link(store)}"


def recovered_text(store):
    return f"✅ {store['name']} is sending normally again.{store_link(store)}"


def check_once(store_facts, store_status, now=None):
    """One pass over every store. Returns the messages sent, or None when
    alerts are off or another worker holds the lock."""
    config = load_config()
    if not ready(config):
        return None
    now = now or datetime.now(timezone.utc)
    sent = []
    with stores.db() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (LOCK_ID,))
        if not cur.fetchone()[0]:
            return None
        cur.execute("SELECT store_id, state, problem_alerted, last_alert_at FROM store_alert_state")
        known = {sid: (state, alerted, last) for sid, state, alerted, last in cur.fetchall()}
        for store in stores.list_stores():
            try:
                facts = store_facts(store["shop_domain"])
            except Exception as e:
                # Storage trouble says nothing about the store: check again next time.
                logger.error(f"alert check skipped {store['id']}: {type(e).__name__}")
                continue
            state, reason = store_status(store, facts)
            previous, alerted, last_alert = known.get(store["id"], (None, False, None))
            last_alert_at = last_alert
            if state == "problem" and previous != "problem":
                if last_alert and now - last_alert < COOLDOWN:
                    pass  # flapping: stay quiet; "recovered" follows only an announced problem
                else:
                    ok, error = telegram_send(config, problem_text(store, reason))
                    if not ok:
                        # Leave the old status in place so the next check tries again.
                        logger.error(f"alert for {store['id']} not delivered: {error}")
                        continue
                    alerted, last_alert_at = True, now
                    sent.append((store["id"], "problem"))
            elif state == "ok" and alerted:
                ok, error = telegram_send(config, recovered_text(store))
                if not ok:
                    logger.error(f"recovery alert for {store['id']} not delivered: {error}")
                    continue
                alerted = False
                sent.append((store["id"], "recovered"))
            cur.execute(
                """INSERT INTO store_alert_state (store_id, state, reason, problem_alerted, last_alert_at, changed_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (store_id) DO UPDATE SET
                       changed_at = CASE WHEN store_alert_state.state = EXCLUDED.state
                                         THEN store_alert_state.changed_at ELSE EXCLUDED.changed_at END,
                       state = EXCLUDED.state, reason = EXCLUDED.reason,
                       problem_alerted = EXCLUDED.problem_alerted, last_alert_at = EXCLUDED.last_alert_at""",
                (store["id"], state, (reason or "")[:200], alerted, last_alert_at, now))
    for store_id, kind in sent:
        logger.info(f"Alert sent: {store_id} {kind}")
    return sent


def _loop(store_facts, store_status):
    time.sleep(60 + random.random() * 60)  # let the worker finish starting
    while True:
        try:
            check_once(store_facts, store_status)
        except Exception as e:
            logger.error(f"alert check failed: {type(e).__name__}: {e}")
        time.sleep(CHECK_SECONDS)


def start(store_facts, store_status):
    if stores.DATABASE_URL:
        threading.Thread(target=_loop, args=(store_facts, store_status), name="store-alerts", daemon=True).start()
