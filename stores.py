"""Store registry for the tracking hub.

Which stores exist, each one's Meta pixel and credentials, and the small logs
the hub's health page reads. Every store-specific value the tracker uses comes
from store_config(shop), keyed by the store's permanent *.myshopify.com domain
(Shopify sends it on every webhook; the theme snippet sends it on every event).

Credentials (Meta access token, Shopify webhook signing key) are encrypted with
a key derived from the HUB_SECRET_KEY Railway variable. A store whose
credential is empty uses the matching Railway variable instead: that is how the
first store (Comfi UK, set up before the hub) keeps working untouched.
"""
import base64
import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
from contextlib import contextmanager

import psycopg2
import psycopg2.pool
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)
DATABASE_URL = os.environ.get("DATABASE_URL")

SHOP_DOMAIN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,60}\.myshopify\.com")
STORE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,40}")
PIXEL_ID_RE = re.compile(r"[0-9]{6,20}")
COUNTRY_RE = re.compile(r"[A-Z]{2}")
CURRENCY_RE = re.compile(r"[A-Z]{3}")
TEST_CODE_RE = re.compile(r"TEST[0-9]{3,10}")


def shop_key(value):
    value = value.strip().lower() if isinstance(value, str) else ""
    return value if SHOP_DOMAIN_RE.fullmatch(value) else None


# ============ DATABASE ============

_pool = None
_pool_lock = threading.Lock()


@contextmanager
def db():
    """A pooled connection inside a transaction (commit on success, rollback
    on error). Each gunicorn worker builds its own pool on first use."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(1, 8, DATABASE_URL, connect_timeout=5)
    conn = _pool.getconn()
    broken = False
    try:
        with conn:
            yield conn
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        broken = True
        raise
    finally:
        _pool.putconn(conn, close=broken or bool(conn.closed))


SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL DEFAULT 'owner',
    platform TEXT NOT NULL DEFAULT 'shopify',
    name TEXT NOT NULL,
    shop_domain TEXT UNIQUE,
    site_url TEXT,
    pixel_id TEXT,
    access_token_enc TEXT,
    webhook_secret_enc TEXT,
    test_event_code TEXT,
    default_country TEXT NOT NULL DEFAULT 'GB',
    currency TEXT NOT NULL DEFAULT 'GBP',
    enabled BOOLEAN NOT NULL DEFAULT true,
    uses_env_credentials BOOLEAN NOT NULL DEFAULT false,
    pixel_name TEXT,
    pixel_checked_at TIMESTAMPTZ,
    pixel_check_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS webhook_receipts (
    shop TEXT NOT NULL,
    topic TEXT NOT NULL,
    first_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    total BIGINT NOT NULL DEFAULT 1,
    last_result TEXT,
    PRIMARY KEY (shop, topic)
);
CREATE TABLE IF NOT EXISTS meta_deliveries (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    shop TEXT,
    event_name TEXT,
    source TEXT,
    keys TEXT[],
    status INT,
    error TEXT,
    client_version INT,
    restored TEXT
);
CREATE INDEX IF NOT EXISTS meta_deliveries_shop_ts ON meta_deliveries (shop, ts DESC);
-- Where the store's browser sends its events, e.g. track.yourstore.com (a
-- subdomain of the store's own site pointed at the hub). Empty: the hub's address.
ALTER TABLE stores ADD COLUMN IF NOT EXISTS tracking_host TEXT;
-- When Meta last accepted a test event sent with the store's token. Tokens made
-- in Events Manager can send events but not read the pixel's name, so for
-- those this, not the name lookup, is what confirms the token works.
ALTER TABLE stores ADD COLUMN IF NOT EXISTS pixel_verified_at TIMESTAMPTZ;
-- More pixels (e.g. one in a second Business Manager) that get every event too:
-- an encrypted JSON list of {"pixel_id", "access_token"}. Empty: main pixel only.
ALTER TABLE stores ADD COLUMN IF NOT EXISTS extra_pixels_enc TEXT;
CREATE TABLE IF NOT EXISTS hub_migrations (
    name TEXT PRIMARY KEY,
    done_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Hub-wide settings edited in the hub (alert channel), encrypted like store credentials.
CREATE TABLE IF NOT EXISTS hub_settings (
    name TEXT PRIMARY KEY,
    value_enc TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Each store's status at the last alert check, so alerts fire on changes only.
CREATE TABLE IF NOT EXISTS store_alert_state (
    store_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    reason TEXT,
    problem_alerted BOOLEAN NOT NULL DEFAULT false,
    last_alert_at TIMESTAMPTZ,
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# The store that existed before the hub. Its credentials stay in the Railway
# variables (PIXEL_ID, ACCESS_TOKEN, SHOPIFY_WEBHOOK_SECRET) until they are
# re-entered in the hub, so creating the registry changes nothing for it.
FIRST_STORE = {
    "id": "comfi-uk", "name": "Comfi UK", "shop_domain": "v8284i-f1.myshopify.com",
    "site_url": "https://comfishop.com", "default_country": "GB", "currency": "GBP",
    "tracking_host": "track.comfishop.com",
}


def init_store_db():
    if not DATABASE_URL:
        return
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(724502)")  # both workers start at once
            cur.execute(SCHEMA)
            cur.execute("SELECT count(*) FROM stores")
            if cur.fetchone()[0] == 0 and os.environ.get("PIXEL_ID"):
                cur.execute(
                    """INSERT INTO stores (id, name, shop_domain, site_url, pixel_id, default_country,
                                           currency, uses_env_credentials)
                       VALUES (%(id)s, %(name)s, %(shop_domain)s, %(site_url)s, %(pixel)s,
                               %(default_country)s, %(currency)s, true)
                       ON CONFLICT DO NOTHING""",
                    dict(FIRST_STORE, pixel=os.environ["PIXEL_ID"]))
                logger.info(f"Store registry: added {FIRST_STORE['name']} from the Railway variables")
            # Once: Comfi's theme already sends to its own subdomain; record it.
            cur.execute("""INSERT INTO hub_migrations (name) VALUES ('first_store_tracking_host')
                           ON CONFLICT DO NOTHING RETURNING 1""")
            if cur.fetchone():
                cur.execute("UPDATE stores SET tracking_host = %s WHERE id = %s AND tracking_host IS NULL",
                            (FIRST_STORE["tracking_host"], FIRST_STORE["id"]))
    except Exception as e:
        logger.error(f"store registry init failed: {type(e).__name__}: {e}")


# ============ ENCRYPTION ============

class HubNotConfigured(Exception):
    """HUB_SECRET_KEY is not set, so credentials cannot be stored."""


def _key(label):
    secret = os.environ.get("HUB_SECRET_KEY", "")
    if len(secret) < 16:
        return None
    return hashlib.sha256(f"comfi-hub:{label}:{secret}".encode()).digest()


def secret_configured():
    return _key("check") is not None


def session_secret():
    key = _key("session")
    return key.hex() if key else None


def encrypt(text):
    key = _key("credentials-v1")
    if key is None:
        raise HubNotConfigured()
    return Fernet(base64.urlsafe_b64encode(key)).encrypt(text.encode()).decode()


def decrypt(token):
    key = _key("credentials-v1")
    if not token or key is None:
        return None
    try:
        return Fernet(base64.urlsafe_b64encode(key)).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        logger.error("A stored credential could not be decrypted (HUB_SECRET_KEY changed?)")
        return None


# ============ STORE CONFIG ============

_cache = {}
_cache_lock = threading.Lock()
CACHE_SECONDS = 30
STORE_COLUMNS = ("id", "account_id", "platform", "name", "shop_domain", "site_url", "pixel_id",
                 "access_token_enc", "webhook_secret_enc", "test_event_code", "default_country",
                 "currency", "enabled", "uses_env_credentials", "pixel_name", "pixel_checked_at",
                 "pixel_check_error", "created_at", "updated_at", "tracking_host", "extra_pixels_enc")
# The hub's pages also read when the token was last confirmed by a test event.
# Kept out of store_config, the per-event lookup, so tracking never depends on it.
HUB_COLUMNS = STORE_COLUMNS + ("pixel_verified_at",)


def _row_to_store(row, columns=STORE_COLUMNS):
    store = dict(zip(columns, row))
    env = store["uses_env_credentials"]
    store["access_token"] = decrypt(store["access_token_enc"]) or (
        os.environ.get("ACCESS_TOKEN") if env else None)
    store["webhook_secret"] = decrypt(store["webhook_secret_enc"]) or (
        os.environ.get("SHOPIFY_WEBHOOK_SECRET") if env else None)
    if env and not store["pixel_id"]:
        store["pixel_id"] = os.environ.get("PIXEL_ID")
    store["extra_pixels"] = read_extra_pixels(store.get("extra_pixels_enc"), store["pixel_id"])
    return store


def read_extra_pixels(token, main_pixel=None):
    """The store's extra pixels as [{"pixel_id", "access_token"}], skipping
    anything malformed and the main pixel itself (never send an event twice)."""
    text = decrypt(token) if token else None
    try:
        items = json.loads(text) if text else []
    except ValueError:
        return []
    pixels, seen = [], {main_pixel}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        pid, tok = str(item.get("pixel_id") or ""), item.get("access_token")
        if PIXEL_ID_RE.fullmatch(pid) and isinstance(tok, str) and tok and pid not in seen:
            seen.add(pid)
            pixels.append({"pixel_id": pid, "access_token": tok})
    return pixels


def store_config(shop):
    """The store for this *.myshopify.com domain, or None when it is not in
    the registry or switched off. Cached for 30 seconds per worker."""
    shop = shop_key(shop)
    if not shop or not DATABASE_URL:
        return None
    now = time.time()
    with _cache_lock:
        hit = _cache.get(shop)
        if hit and hit[0] > now:
            return hit[1]
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT {', '.join(STORE_COLUMNS)} FROM stores WHERE shop_domain = %s", (shop,))
            row = cur.fetchone()
    except Exception as e:
        logger.error(f"store lookup failed: {type(e).__name__}")
        with _cache_lock:
            hit = _cache.get(shop)
        return hit[1] if hit else None  # keep serving the last known config
    store = _row_to_store(row) if row else None
    if store and not store["enabled"]:
        store = None
    with _cache_lock:
        _cache[shop] = (now + CACHE_SECONDS, store)
    return store


def forget_cached(shop=None):
    with _cache_lock:
        if shop:
            _cache.pop(shop, None)
        else:
            _cache.clear()


def list_stores():
    with db() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(HUB_COLUMNS)} FROM stores ORDER BY created_at")
        return [_row_to_store(r, HUB_COLUMNS) for r in cur.fetchall()]


def get_store(store_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(HUB_COLUMNS)} FROM stores WHERE id = %s", (store_id,))
        row = cur.fetchone()
    return _row_to_store(row, HUB_COLUMNS) if row else None


def save_store(values, store_id=None):
    """Insert or update a store from already-validated values. Credentials are
    only replaced when a new one is given (blank keeps the stored one)."""
    fields = {k: values[k] for k in ("name", "shop_domain", "site_url", "pixel_id", "test_event_code", "tracking_host",
                                     "default_country", "currency", "enabled") if k in values}
    if values.get("access_token"):
        fields["access_token_enc"] = encrypt(values["access_token"])
    if values.get("webhook_secret"):
        fields["webhook_secret_enc"] = encrypt(values["webhook_secret"])
    if "extra_pixels" in values:
        fields["extra_pixels_enc"] = encrypt(json.dumps(values["extra_pixels"])) if values["extra_pixels"] else None
    with db() as conn, conn.cursor() as cur:
        if store_id is None:
            fields["id"] = values["id"]
            cols = list(fields)
            cur.execute(
                f"INSERT INTO stores ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
                [fields[c] for c in cols])
        else:
            if fields.get("access_token_enc") and fields.get("webhook_secret_enc"):
                fields["uses_env_credentials"] = False  # both now live in the hub
            sets = ", ".join(f"{c} = %s" for c in fields)
            cur.execute(f"UPDATE stores SET {sets}, updated_at = now() WHERE id = %s",
                        [*fields.values(), store_id])
    forget_cached()


def get_setting(name):
    """A hub setting saved with save_setting (a dict), or {} when unset or unreadable."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT value_enc FROM hub_settings WHERE name = %s", (name,))
        row = cur.fetchone()
    text = decrypt(row[0]) if row else None
    try:
        value = json.loads(text) if text else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def save_setting(name, value):
    enc = encrypt(json.dumps(value))
    with db() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO hub_settings (name, value_enc) VALUES (%s, %s)
                       ON CONFLICT (name) DO UPDATE SET value_enc = EXCLUDED.value_enc, updated_at = now()""",
                    (name, enc))


def record_pixel_check(store_id, name, error, reset_verified=False):
    """Store the result of the hub's pixel check. reset_verified forgets an
    earlier accepted test event (the pixel ID or token may have changed)."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE stores SET pixel_name = %s, pixel_check_error = %s, pixel_checked_at = now(),
                              pixel_verified_at = CASE WHEN %s THEN NULL ELSE pixel_verified_at END
                       WHERE id = %s""", (name, error, reset_verified, store_id))


def mark_pixel_verified(store_id):
    """Meta accepted a test event sent with this store's token: it works."""
    with db() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE stores SET pixel_verified_at = now(), pixel_check_error = NULL
                       WHERE id = %s""", (store_id,))


# ============ LOGS FOR THE HEALTH PAGE ============

def note_webhook(shop, topic, result):
    """Remember that this store's webhook arrived, for the setup checklist."""
    if not DATABASE_URL or not shop:
        return
    topic = topic if isinstance(topic, str) and re.fullmatch(r"[a-z_]+/[a-z_]+", topic) else "unknown"
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO webhook_receipts (shop, topic, last_result) VALUES (%s, %s, %s)
                   ON CONFLICT (shop, topic) DO UPDATE
                   SET last_at = now(), total = webhook_receipts.total + 1, last_result = EXCLUDED.last_result""",
                (shop, topic, (result or "")[:40]))
    except Exception as e:
        logger.error(f"webhook receipt failed: {type(e).__name__}")


_deliveries = queue.Queue(maxsize=20000)


def log_delivery(shop, event_name, source, keys, status, error=None, client_version=None, restored=None):
    """Queue one Meta delivery for the health page. Written in batches by a
    background thread, so it never slows down an event."""
    if not DATABASE_URL:
        return
    try:
        _deliveries.put_nowait((shop, (event_name or "")[:40], source, sorted(keys or []), status,
                                (error or None) and error[:300], client_version, restored or None))
    except queue.Full:
        pass


def _delivery_writer():
    while True:
        batch = [_deliveries.get()]
        time.sleep(2)
        while len(batch) < 500:
            try:
                batch.append(_deliveries.get_nowait())
            except queue.Empty:
                break
        try:
            with db() as conn, conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO meta_deliveries
                       (shop, event_name, source, keys, status, error, client_version, restored)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", batch)
        except Exception as e:
            logger.error(f"delivery log write failed: {type(e).__name__}")


def cleanup_logs():
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM meta_deliveries WHERE ts < now() - interval '30 days'")


def start_background():
    if DATABASE_URL:
        threading.Thread(target=_delivery_writer, name="delivery-log", daemon=True).start()
