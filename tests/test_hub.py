"""End to end against the local Postgres: stores stay isolated, unknown stores
get nothing, the checkout pixel ticks on Setup, and alerts fire on changes.
Meta, Telegram and the hub's checks are mocked."""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import alerts
import hub
from conftest import hub_client
import main
import stores

COMFI = "v8284i-f1.myshopify.com"
SECOND = "asbfh1-ay.myshopify.com"
SECOND_TOKEN = "SECOND" + "b" * 60
SECOND_KEY = "second-store-webhook-key"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Version/17.5 Safari/605.1.15"
BOT = "123456789:AAH" + "c" * 32


class Wire:
    """Records requests.post and requests.get; answers like Meta and Telegram."""

    def __init__(self):
        self.posts, self.gets = [], []
        self.telegram_ok = True
        self.updates = []

    def response(self, status, body):
        r = MagicMock(status_code=status, text=json.dumps(body))
        r.json.return_value = body
        return r

    def post(self, url, json=None, timeout=None, **kwargs):
        self.posts.append({"url": url, "body": json})
        if "api.telegram.org" in url:
            return (self.response(200, {"ok": True}) if self.telegram_ok
                    else self.response(400, {"ok": False, "description": "Bad Request: chat not found"}))
        return self.response(200, {"events_received": 1})

    def get(self, url, params=None, timeout=None, **kwargs):
        self.gets.append(url)
        if url.endswith("/getUpdates"):
            return self.response(200, {"ok": True, "result": self.updates})
        if url.endswith("/ping"):
            return self.response(200, {"ok": True, "service": "tracking-hub"})
        return self.response(200, {"name": "Test pixel"})

    def meta(self):
        return [p for p in self.posts if "graph.facebook.com" in p["url"]]

    def telegram(self):
        return [p for p in self.posts if "api.telegram.org" in p["url"]]


@pytest.fixture
def wire():
    w = Wire()
    with patch("requests.post", side_effect=w.post), patch("requests.get", side_effect=w.get):
        yield w


@pytest.fixture(scope="module", autouse=True)
def second_store():
    """Wisehearshop-style second store, added through the hub's own form."""
    w = Wire()
    client = hub_client()
    with patch("requests.get", side_effect=w.get):
        r = client.post("/hub/stores/new", data={
            "csrf": "t", "name": "Second Store", "shop_domain": SECOND, "site_url": "https://second.example",
            "pixel_id": "222333444555", "access_token": SECOND_TOKEN, "webhook_secret": SECOND_KEY,
            "default_country": "US", "currency": "USD"})
    assert r.status_code == 302, r.data
    stores.forget_cached()
    yield


def signed(body, key):
    raw = json.dumps(body).encode()
    return raw, base64.b64encode(hmac.new(key.encode(), raw, hashlib.sha256).digest()).decode()


def order(order_id, phone="555-625-1199", country=None):
    return {"id": order_id, "email": "a@example.com", "total_price": "50.00", "currency": "USD",
            "billing_address": {"phone": phone, **({"country_code": country} if country else {})},
            "client_details": {"user_agent": UA}, "line_items": []}


def post_order(client, shop, key, body):
    raw, sig = signed(body, key)
    return client.post("/webhook/order-created", data=raw, headers={
        "X-Shopify-Shop-Domain": shop, "X-Shopify-Hmac-Sha256": sig, "X-Shopify-Topic": "orders/create"})


def track(client, shop, **extra):
    body = {"event_name": "PageView", "event_id": "e1", "vid": "v_track000001",
            "source_url": "https://x.example/", "shop": shop, **extra}
    return client.post("/track", data=json.dumps(body), content_type="text/plain", headers={"User-Agent": UA})


# ============ ISOLATION ============

def test_each_store_uses_its_own_pixel_and_token(app_client, wire):
    assert track(app_client, COMFI).status_code == 200
    assert track(app_client, SECOND).status_code == 200
    comfi, second = wire.meta()
    assert comfi["url"].endswith("/1449380569544787/events") and comfi["body"]["access_token"].startswith("COMFI")
    assert second["url"].endswith("/222333444555/events") and second["body"]["access_token"] == SECOND_TOKEN


def test_webhooks_need_that_stores_own_key(app_client, wire):
    assert post_order(app_client, SECOND, SECOND_KEY, order(1)).status_code == 200
    assert post_order(app_client, SECOND, "comfi-webhook-secret", order(2)).status_code == 401
    assert post_order(app_client, COMFI, SECOND_KEY, order(3)).status_code == 401
    assert post_order(app_client, COMFI, "comfi-webhook-secret", order(4, "07767 304185", "GB")).status_code == 200
    sent = wire.meta()
    assert [s["url"].split("/")[-2] for s in sent] == ["222333444555", "1449380569544787"]
    assert [s["body"]["data"][0]["event_id"] for s in sent] == ["purchase_1", "purchase_4"]


def test_unknown_store_gets_nothing(app_client, wire):
    r = track(app_client, "stranger.myshopify.com")
    assert r.get_json() == {"ok": True, "skipped": "unknown_store"}
    assert track(app_client, None).get_json()["skipped"] == "unknown_store"
    assert post_order(app_client, "stranger.myshopify.com", "anything", order(9)).status_code == 401
    assert wire.meta() == []


def test_phone_read_in_the_stores_country(app_client, wire):
    """No country on the address: the US store reads the number as American."""
    post_order(app_client, SECOND, SECOND_KEY, order(11, "555-625-1199"))
    ph = wire.meta()[0]["body"]["data"][0]["user_data"]["ph"]
    assert ph == [hashlib.sha256(b"15556251199").hexdigest()]


def test_junk_phone_never_sent(app_client, wire):
    post_order(app_client, SECOND, SECOND_KEY, order(12, "12345"))
    assert "ph" not in wire.meta()[0]["body"]["data"][0]["user_data"]


# ============ CHECKOUT PIXEL ON SETUP ============

def context(client, **body):
    base = {"checkout_token": "tok" + "x" * 12, "vid": "v_ctx000001", "user_agent": UA}
    return client.post("/meta/context", data=json.dumps({**base, **body}), content_type="text/plain")


def context_rows(token):
    with stores.db() as conn, conn.cursor() as cur:
        cur.execute("SELECT shop, client_version FROM meta_context WHERE checkout_token = %s ORDER BY id", (token,))
        return cur.fetchall()


def test_context_records_registered_shop_only(app_client):
    assert context(app_client, checkout_token="tokoldpixel01").status_code == 200  # today's v2 pixel
    assert context(app_client, checkout_token="tokunknown001", shop="stranger.myshopify.com", v=3).status_code == 200
    assert context(app_client, checkout_token="tokjunkshop01", shop="<script>", v="3").status_code == 200
    assert context(app_client, checkout_token="tokcomfi00001", shop=COMFI.upper(), v=3).status_code == 200
    assert context_rows("tokoldpixel01") == [(None, None)]
    assert context_rows("tokunknown001") == [(None, 3)]
    assert context_rows("tokjunkshop01") == [(None, None)]
    assert context_rows("tokcomfi00001") == [(COMFI, 3)]
    # Lookups stay keyed by checkout token, whatever the shop column says.
    assert main.meta_context_lookup("tokunknown001")["vid"] == "v_ctx000001"


def setup_page(client, store_id):
    return client.get(f"/hub/stores/{store_id}?tab=setup").get_data(as_text=True)


def test_setup_step_ticks_when_pixel_seen(logged_in, app_client, wire):
    second = next(s for s in stores.list_stores() if s["shop_domain"] == SECOND)
    page = setup_page(logged_in, second["id"])
    assert "Checkout pixel" in page and "Not seen yet." in page
    assert 'const PIXEL_ID = &quot;222333444555&quot;;' in page
    assert f"https://hub.example.test/meta/context" in page  # no tracking address: the hub's own

    context(app_client, checkout_token="toksecondold1", shop=SECOND, v=2)
    assert "but an older version" in setup_page(logged_in, second["id"])
    context(app_client, checkout_token="toksecondnew1", shop=SECOND, v=hub.CHECKOUT_PIXEL_VERSION)
    assert "Running the latest checkout pixel" in setup_page(logged_in, second["id"])

    comfi_page = setup_page(logged_in, "comfi-uk")
    assert "https://track.comfishop.com/meta/context" in comfi_page

    r = logged_in.get(f"/hub/stores/{second['id']}/checkout-pixel.js")
    assert r.status_code == 200 and "<" not in r.get_data(as_text=True)
    assert "attachment" in r.headers["Content-Disposition"]


def test_snippet_download_is_current_version(logged_in):
    text = logged_in.get("/hub/stores/comfi-uk/snippet.liquid").get_data(as_text=True)
    assert f"payload.v = {hub.SNIPPET_VERSION};" in text and "metaPhone(raw.ph" in text
    assert "__" not in text.replace("_fbq", "")


# ============ ALERTS: SETTINGS PAGE ============

def test_alert_settings_saved_encrypted(logged_in, wire):
    r = logged_in.post("/hub/alerts", data={"csrf": "t", "action": "save", "bot_token": BOT,
                                            "chat_id": "424242", "enabled": "on"})
    assert r.status_code == 302 and "done=saved" in r.headers["Location"]
    with stores.db() as conn, conn.cursor() as cur:
        cur.execute("SELECT value_enc FROM hub_settings WHERE name = 'alerts'")
        raw = cur.fetchone()[0]
    assert BOT not in raw and "424242" not in raw
    assert alerts.load_config() == {"bot_token": BOT, "chat_id": "424242", "enabled": True}
    page = logged_in.get("/hub/alerts").get_data(as_text=True)
    assert "On: alerts go to your Telegram" in page and BOT not in page

    # Blank token keeps the saved one.
    logged_in.post("/hub/alerts", data={"csrf": "t", "action": "save", "chat_id": "424242", "enabled": "on"})
    assert alerts.load_config()["bot_token"] == BOT


def test_alert_settings_reject_bad_input(logged_in, wire):
    page = logged_in.post("/hub/alerts", data={"csrf": "t", "action": "save", "bot_token": "nope"}).get_data(as_text=True)
    assert "does not look right" in page
    r = logged_in.post("/hub/alerts", data={"csrf": "x", "action": "save", "bot_token": BOT})
    assert r.status_code == 400  # CSRF


def test_find_chat_id_and_test_alert(logged_in, wire):
    wire.updates = [{"update_id": 1, "message": {"chat": {"id": 987654321, "first_name": "Taif"}}}]
    r = logged_in.post("/hub/alerts", data={"csrf": "t", "action": "find", "bot_token": BOT})
    assert "done=found" in r.headers["Location"]
    assert alerts.load_config()["chat_id"] == "987654321" and alerts.load_config()["enabled"] is True

    r = logged_in.post("/hub/alerts", data={"csrf": "t", "action": "test", "chat_id": "987654321", "enabled": "on"})
    assert "done=test_ok" in r.headers["Location"]
    sent = wire.telegram()[-1]
    assert sent["url"] == f"https://api.telegram.org/bot{BOT}/sendMessage"
    assert sent["body"]["chat_id"] == "987654321" and "Test alert" in sent["body"]["text"]

    wire.telegram_ok = False
    page = logged_in.post("/hub/alerts", data={"csrf": "t", "action": "test", "chat_id": "987654321",
                                               "enabled": "on"}).get_data(as_text=True)
    assert "chat not found" in page


# ============ ALERTS: TRANSITIONS ============

@pytest.fixture
def checker(wire):
    """Runs alerts.check_once with each store's status set by the test."""
    stores.save_setting(alerts.SETTING, {"bot_token": BOT, "chat_id": "111", "enabled": True})
    with stores.db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM store_alert_state")
    status = {}
    clock = {"now": datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)}

    def run(minutes=10, **states):
        status.update(states)
        clock["now"] += timedelta(minutes=minutes)
        before = len(wire.telegram())
        result = alerts.check_once(lambda shop: {},
                                   lambda store, facts: status.get(store["id"], ("setup", "Waiting")),
                                   now=clock["now"])
        return result, [p["body"]["text"] for p in wire.telegram()[before:]]
    return run


def ok():
    return ("ok", "Sending normally")


def problem(reason="No events from the theme in the last 24 hours"):
    return ("problem", reason)


def test_alerts_only_on_transitions(checker):
    assert checker(**{"comfi-uk": ok()}) == ([], [])
    result, texts = checker(**{"comfi-uk": problem()})
    assert result == [("comfi-uk", "problem")]
    assert texts == ["🔴 Comfi UK: No events from the theme in the last 24 hours.\n"
                     "https://hub.example.test/hub/stores/comfi-uk"]
    assert checker(**{"comfi-uk": problem("Meta is rejecting some events")}) == ([], [])  # still red
    result, texts = checker(**{"comfi-uk": ok()})
    assert result == [("comfi-uk", "recovered")]
    assert texts[0].startswith("✅ Comfi UK is sending normally again.")
    assert checker(**{"comfi-uk": ok()}) == ([], [])


def test_setup_never_alerts(checker):
    assert checker(**{"comfi-uk": ("setup", "Waiting for the checkout webhooks")}) == ([], [])
    assert checker(**{"comfi-uk": ok()}) == ([], [])  # setup to ok is not a recovery


def test_already_red_store_alerts_on_first_check(checker):
    assert checker(**{"comfi-uk": problem()})[0] == [("comfi-uk", "problem")]


def test_cooldown_stops_flapping(checker):
    assert checker(**{"comfi-uk": problem()})[0] == [("comfi-uk", "problem")]
    assert checker(**{"comfi-uk": ok()})[0] == [("comfi-uk", "recovered")]
    assert checker(**{"comfi-uk": problem()})[0] == []  # 20 minutes after the first alert
    assert checker(**{"comfi-uk": ok()})[0] == []  # nothing announced, so no "recovered"
    assert checker(minutes=6 * 60, **{"comfi-uk": problem()})[0] == [("comfi-uk", "problem")]


def test_red_then_setup_then_red_still_recovers(checker):
    assert checker(**{"comfi-uk": problem()})[0] == [("comfi-uk", "problem")]
    assert checker(**{"comfi-uk": ("setup", "Switched off")})[0] == []
    assert checker(**{"comfi-uk": problem()})[0] == []  # within cooldown
    assert checker(**{"comfi-uk": ok()})[0] == [("comfi-uk", "recovered")]


def test_stores_are_tracked_separately(checker):
    second = next(s for s in stores.list_stores() if s["shop_domain"] == SECOND)["id"]
    result, texts = checker(**{"comfi-uk": problem(), second: problem("Meta is rejecting some events")})
    assert sorted(result) == sorted([("comfi-uk", "problem"), (second, "problem")])
    assert checker(**{"comfi-uk": ok()})[0] == [("comfi-uk", "recovered")]


def test_failed_delivery_is_retried(checker, wire):
    wire.telegram_ok = False
    assert checker(**{"comfi-uk": problem()})[0] == []
    wire.telegram_ok = True
    assert checker(**{"comfi-uk": problem()})[0] == [("comfi-uk", "problem")]


def test_only_one_worker_checks_at_a_time(checker):
    with stores.db() as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (alerts.LOCK_ID,))  # "the other worker"
        assert checker(**{"comfi-uk": problem()})[0] is None
    assert checker(**{"comfi-uk": problem()})[0] == [("comfi-uk", "problem")]


def test_nothing_checked_while_alerts_off(checker):
    stores.save_setting(alerts.SETTING, {"bot_token": BOT, "chat_id": "111", "enabled": False})
    assert checker(**{"comfi-uk": problem()})[0] is None
    with stores.db() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM store_alert_state")
        assert cur.fetchone()[0] == 0


def test_storage_trouble_skips_store(wire, checker):
    stores.save_setting(alerts.SETTING, {"bot_token": BOT, "chat_id": "111", "enabled": True})

    def broken(shop):
        raise RuntimeError("db down")
    assert alerts.check_once(broken, lambda s, f: problem()) == []
    assert wire.telegram() == []
