"""End to end against the local Postgres: stores stay isolated, unknown stores
get nothing, the checkout pixel ticks on Setup, and alerts fire on changes.
Meta, Telegram and the hub's checks are mocked."""
import base64
import hashlib
import hmac
import json
import time
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
        self.pixel_read_error = None  # Meta's error for reading the pixel's name, if any
        self.send_error = None  # Meta's error for sending events, if any

    def response(self, status, body):
        r = MagicMock(status_code=status, text=json.dumps(body))
        r.json.return_value = body
        return r

    def post(self, url, json=None, timeout=None, **kwargs):
        self.posts.append({"url": url, "body": json})
        if "api.telegram.org" in url:
            return (self.response(200, {"ok": True}) if self.telegram_ok
                    else self.response(400, {"ok": False, "description": "Bad Request: chat not found"}))
        if self.send_error:
            return self.response(400, {"error": self.send_error})
        return self.response(200, {"events_received": 1})

    def get(self, url, params=None, timeout=None, **kwargs):
        self.gets.append(url)
        if url.endswith("/getUpdates"):
            return self.response(200, {"ok": True, "result": self.updates})
        if url.endswith("/ping"):
            return self.response(200, {"ok": True, "service": "tracking-hub"})
        if self.pixel_read_error:
            return self.response(400, {"error": self.pixel_read_error})
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


def test_red_kept_quiet_by_cooldown_is_announced_when_it_ends(checker):
    assert checker(**{"comfi-uk": problem()})[0] == [("comfi-uk", "problem")]
    assert checker(**{"comfi-uk": ok()})[0] == [("comfi-uk", "recovered")]
    assert checker(**{"comfi-uk": problem()})[0] == []  # red again inside the cooldown
    assert checker(minutes=2 * 60, **{"comfi-uk": problem()})[0] == []  # still inside it
    assert checker(minutes=4 * 60, **{"comfi-uk": problem()})[0] == [("comfi-uk", "problem")]  # still red after it
    assert checker(**{"comfi-uk": problem()})[0] == []  # announced once
    assert checker(**{"comfi-uk": ok()})[0] == [("comfi-uk", "recovered")]


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


# ============ PIXEL CHECK: TOKENS THAT SEND BUT CANNOT READ ============

READ_DENIED = {"message": "(#100) Missing Permission", "type": "OAuthException", "code": 100}
BAD_TOKEN = {"message": "Invalid OAuth access token - Cannot parse access token", "type": "OAuthException",
             "code": 190}


def second_store_row():
    return next(s for s in stores.list_stores() if s["shop_domain"] == SECOND)


def save_second_settings(client, **changes):
    data = {"csrf": "t", "name": "Second Store", "shop_domain": SECOND, "site_url": "https://second.example",
            "pixel_id": "222333444555", "default_country": "US", "currency": "USD",
            "test_event_code": "TEST39027", "enabled": "on", **changes}
    return client.post(f"/hub/stores/{second_store_row()['id']}", data=data)


@pytest.fixture
def restore_second_store():
    yield
    stores.record_pixel_check(second_store_row()["id"], "Test pixel", None, reset_verified=True)


def test_token_that_cannot_read_the_pixel_is_not_red(logged_in, wire, restore_second_store):
    """Live, 20 Oct: a WiseHear token made in Events Manager sent a test event
    that Meta processed, yet reading the pixel's name gave (#100) Missing Permission."""
    wire.pixel_read_error = READ_DENIED
    assert save_second_settings(logged_in).status_code == 302
    store = second_store_row()
    assert store["pixel_check_error"] is None and store["pixel_name"] is None
    assert hub.store_status(store)[0] != "problem"
    page = setup_page(logged_in, store["id"])
    assert "Token saved." in page and "Meta said" not in page

    r = logged_in.post(f"/hub/stores/{store['id']}/test-event", data={"csrf": "t"})
    assert "test=ok" in r.headers["Location"]
    store = second_store_row()
    assert store["pixel_verified_at"] is not None
    assert "Working: Meta accepted a test event" in setup_page(logged_in, store["id"])

    # Saving the settings again (perhaps a new token) needs fresh proof.
    save_second_settings(logged_in)
    assert second_store_row()["pixel_verified_at"] is None


def test_red_test_event_and_bad_token_stay_red(logged_in, wire, restore_second_store):
    wire.pixel_read_error = BAD_TOKEN
    save_second_settings(logged_in)
    store = second_store_row()
    assert store["pixel_check_error"].startswith("Invalid OAuth access token")
    assert hub.store_status(store) == ("problem", "Meta rejected the pixel ID or access token")

    wire.pixel_read_error = READ_DENIED
    save_second_settings(logged_in)
    wire.send_error = {"message": "(#100) Missing Permission", "code": 100}
    r = logged_in.post(f"/hub/stores/{second_store_row()['id']}/test-event", data={"csrf": "t"})
    assert "test=bad" in r.headers["Location"]
    store = second_store_row()
    assert store["pixel_check_error"] == "Meta refused a test event: (#100) Missing Permission"
    assert hub.store_status(store)[0] == "problem"


def test_unreadable_token_goes_green_once_events_are_accepted(logged_in, app_client, wire, restore_second_store):
    wire.pixel_read_error = READ_DENIED
    save_second_settings(logged_in)
    assert track(app_client, SECOND).status_code == 200
    time.sleep(2.5)  # the delivery log is written in batches
    assert "Working: Meta is accepting this store's events." in setup_page(logged_in, second_store_row()["id"])


def test_tracking_does_not_read_the_new_column(app_client, wire):
    """The per-event lookup must keep working even if the new column were missing."""
    assert "pixel_verified_at" not in stores.STORE_COLUMNS
    assert "pixel_verified_at" in stores.HUB_COLUMNS


# ============ PURCHASE FALLS BACK TO THE VISITOR'S STORED IDS ============

def click_id(days_ago, click="IwAR" + "q" * 30):
    return f"fb.1.{int((time.time() - days_ago * 86400) * 1000)}.{click}"


def purchase_user_data(wire, order_id):
    sent = [p["body"]["data"][0] for p in wire.meta()
            if p["body"]["data"][0]["event_id"] == f"purchase_{order_id}"]
    assert len(sent) == 1
    return sent[0]["user_data"]


def test_purchase_uses_stored_ids_when_the_order_has_only_the_visitor(app_client, wire):
    fbc, fbp = click_id(3), "fb.1.1760000000000.111222333"
    assert track(app_client, SECOND, event_name="AddToCart", vid="v_stored00001",
                 fbc=fbc, fbp=fbp).status_code == 200
    body = {**order(7101), "note_attributes": [{"name": "_comfi_vid", "value": "v_stored00001"}]}
    assert post_order(app_client, SECOND, SECOND_KEY, body).status_code == 200
    user = purchase_user_data(wire, 7101)
    assert user["fbc"] == fbc and user["fbp"] == fbp


def test_purchase_never_uses_a_stored_click_older_than_90_days(app_client, wire):
    assert track(app_client, SECOND, event_name="AddToCart", vid="v_stored00002",
                 fbc=click_id(91), fbp="fb.1.1760000000000.444555666").status_code == 200
    body = {**order(7102), "note_attributes": [{"name": "_comfi_vid", "value": "v_stored00002"}]}
    assert post_order(app_client, SECOND, SECOND_KEY, body).status_code == 200
    user = purchase_user_data(wire, 7102)
    assert "fbc" not in user and user["fbp"] == "fb.1.1760000000000.444555666"


def test_the_orders_own_click_beats_an_older_stored_one(app_client, wire):
    assert track(app_client, SECOND, event_name="AddToCart", vid="v_stored00003",
                 fbc=click_id(20, "IwARold" + "o" * 20)).status_code == 200
    body = {**order(7103), "landing_site": "/products/x?fbclid=IwARnew" + "n" * 20,
            "note_attributes": [{"name": "_comfi_vid", "value": "v_stored00003"}]}
    assert post_order(app_client, SECOND, SECOND_KEY, body).status_code == 200
    assert purchase_user_data(wire, 7103)["fbc"].endswith(".IwARnew" + "n" * 20)


def test_cart_ids_beat_stored_ones_and_junk_counts_as_missing(app_client, wire):
    stored = click_id(10, "IwARstored" + "s" * 20)
    assert track(app_client, SECOND, event_name="AddToCart", vid="v_stored00004", fbc=stored).status_code == 200
    cart = click_id(1, "IwARcart" + "c" * 20)
    body = {**order(7104), "note_attributes": [{"name": "_comfi_vid", "value": "v_stored00004"},
                                               {"name": "_fbc", "value": cart}]}
    assert post_order(app_client, SECOND, SECOND_KEY, body).status_code == 200
    assert purchase_user_data(wire, 7104)["fbc"] == cart
    junk = {**order(7105), "note_attributes": [{"name": "_comfi_vid", "value": "v_stored00004"},
                                               {"name": "_fbc", "value": "fb.1.1779838569048.fbclid"}]}
    assert post_order(app_client, SECOND, SECOND_KEY, junk).status_code == 200
    # The cart's newer click was stored by the first order, so that is what fills the gap.
    assert purchase_user_data(wire, 7105)["fbc"] == cart


# ============ ORDERS WITH NO VISITOR ID: MATCHED BY DEVICE ============
# Shop Pay and other wallets skip the cart, and no pixel runs on their
# checkout, so the order arrives with no visitor ID. The visitor is found by
# the order's own IP and user agent, only when exactly one recent visitor fits.

def landing(client, shop, vid, ip, fbc=None, event_name="PageView", ua=UA):
    body = {"event_name": event_name, "event_id": "e-" + vid, "vid": vid,
            "source_url": "https://x.example/en-gb/products/x", "shop": shop}
    if fbc:
        body["fbc"] = fbc
        body["fbp"] = "fb.1.1793042396399.476937934158071451"
    return client.post("/track", data=json.dumps(body), content_type="text/plain",
                       headers={"User-Agent": ua, "X-Forwarded-For": ip})


def wallet_order(order_id, ip, ua=UA, email="buyer@example.com"):
    return {**order(order_id), "email": email, "browser_ip": ip, "client_details": {"user_agent": ua}}


def test_shop_pay_order_finds_the_ad_visitor_by_device(app_client, wire):
    fbc = click_id(0, "IwARshoppay" + "s" * 20)
    assert landing(app_client, SECOND, "v_device00001", "92.40.215.157", fbc=fbc).status_code == 200
    assert post_order(app_client, SECOND, SECOND_KEY, wallet_order(7201, "92.40.215.157")).status_code == 200
    user = purchase_user_data(wire, 7201)
    assert user["fbc"] == fbc
    assert user["fbp"] == "fb.1.1793042396399.476937934158071451"
    assert main.hash_data("v_device00001") in user["external_id"]


def test_two_visitors_on_one_device_means_no_match(app_client, wire):
    for vid in ("v_device00002", "v_device00003"):
        assert landing(app_client, SECOND, vid, "10.9.8.7", fbc=click_id(0, "IwAR" + vid[-5:] * 4)).status_code == 200
    assert post_order(app_client, SECOND, SECOND_KEY, wallet_order(7202, "10.9.8.7")).status_code == 200
    user = purchase_user_data(wire, 7202)
    assert "fbc" not in user and "fbp" not in user


def test_device_match_needs_the_same_ip_and_user_agent(app_client, wire):
    assert landing(app_client, SECOND, "v_device00004", "10.1.1.1", fbc=click_id(0, "IwARiponly" + "i" * 20)).status_code == 200
    assert post_order(app_client, SECOND, SECOND_KEY, wallet_order(7203, "10.1.1.2")).status_code == 200
    assert "fbc" not in purchase_user_data(wire, 7203)
    assert post_order(app_client, SECOND, SECOND_KEY,
                      wallet_order(7204, "10.1.1.1", ua="Mozilla/5.0 (iPhone) Other/1.0")).status_code == 200
    assert "fbc" not in purchase_user_data(wire, 7204)


def test_device_match_stays_in_its_own_store(app_client, wire):
    assert landing(app_client, COMFI, "v_device00005", "10.2.2.2", fbc=click_id(0, "IwARcomfi" + "c" * 20)).status_code == 200
    assert post_order(app_client, SECOND, SECOND_KEY, wallet_order(7205, "10.2.2.2")).status_code == 200
    assert "fbc" not in purchase_user_data(wire, 7205)


def test_a_visitor_known_under_another_email_is_someone_else(app_client, wire):
    assert landing(app_client, SECOND, "v_device00006", "10.3.3.3", fbc=click_id(0, "IwARother" + "o" * 20)).status_code == 200
    first = {**wallet_order(7206, "10.3.3.3", email="first@example.com"),
             "note_attributes": [{"name": "_comfi_vid", "value": "v_device00006"}]}
    assert post_order(app_client, SECOND, SECOND_KEY, first).status_code == 200
    assert post_order(app_client, SECOND, SECOND_KEY,
                      wallet_order(7207, "10.3.3.3", email="second@example.com")).status_code == 200
    assert "fbc" not in purchase_user_data(wire, 7207)


def test_plain_page_views_are_not_stored(app_client, wire):
    """Only ad visitors (a click ID) are written from a page view."""
    assert landing(app_client, SECOND, "v_device00007", "10.4.4.4").status_code == 200
    assert main.identity_record(SECOND, "v_device00007", write=False) == {}


def test_the_cart_visitor_still_wins_over_a_device_match(app_client, wire):
    assert landing(app_client, SECOND, "v_device00008", "10.5.5.5", fbc=click_id(0, "IwARdevice" + "d" * 20)).status_code == 200
    body = {**wallet_order(7208, "10.5.5.5"), "note_attributes": [{"name": "_comfi_vid", "value": "v_cartvid0001"}]}
    assert post_order(app_client, SECOND, SECOND_KEY, body).status_code == 200
    user = purchase_user_data(wire, 7208)
    assert "fbc" not in user and main.hash_data("v_cartvid0001") in user["external_id"]


# ============ ORDERS WITH NO VISITOR ID: MATCHED BY A RECENT CHECKOUT START ============
# The hub only sees IPv4, while many UK buyers reach Shopify's checkout over
# IPv6, so the IP step cannot find them. Second step: the same user agent
# started checkout on this store in the last 20 minutes, and only one did.

def started_checkout(client, shop, vid, ua, ip="81.2.3.4", fbc=None):
    body = {"event_name": "InitiateCheckout", "event_id": "ic-" + vid, "vid": vid,
            "source_url": "https://x.example/products/x", "shop": shop,
            "fbc": fbc or click_id(0, "IwARtap" + vid[-6:] * 4), "fbp": "fb.1.1793042396399.123456789"}
    return client.post("/track", data=json.dumps(body), content_type="text/plain",
                       headers={"User-Agent": ua, "X-Forwarded-For": ip})


def ua(tag):
    return f"Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) Mobile/22H374 [FBAN/FBIOS;FBDV/{tag}]"


IPV6 = "2a00:23c8:6dfe:2101:c30b:d9de:36c:209c"


def test_ipv6_shop_pay_order_finds_the_visitor_who_started_checkout(app_client, wire):
    fbc = click_id(0, "IwARipv6buyer" + "b" * 20)
    assert started_checkout(app_client, SECOND, "v_tap0000001", ua("tap1"), fbc=fbc).status_code == 200
    assert post_order(app_client, SECOND, SECOND_KEY, wallet_order(7301, IPV6, ua=ua("tap1"))).status_code == 200
    user = purchase_user_data(wire, 7301)
    assert user["fbc"] == fbc and main.hash_data("v_tap0000001") in user["external_id"]


def test_checkout_started_too_long_ago_is_no_match(app_client, wire):
    assert started_checkout(app_client, SECOND, "v_tap0000002", ua("tap2")).status_code == 200
    import psycopg2
    conn = psycopg2.connect(main.DATABASE_URL)
    with conn, conn.cursor() as cur:
        cur.execute("UPDATE meta_identity SET checkout_at = now() - interval '25 minutes' WHERE vid = 'v_tap0000002'")
    conn.close()
    assert post_order(app_client, SECOND, SECOND_KEY, wallet_order(7302, IPV6, ua=ua("tap2"))).status_code == 200
    assert "fbc" not in purchase_user_data(wire, 7302)


def test_two_checkout_starters_on_the_same_browser_type_is_no_match(app_client, wire):
    for vid in ("v_tap0000003", "v_tap0000004"):
        assert started_checkout(app_client, SECOND, vid, ua("tap3")).status_code == 200
    assert post_order(app_client, SECOND, SECOND_KEY, wallet_order(7303, IPV6, ua=ua("tap3"))).status_code == 200
    assert "fbc" not in purchase_user_data(wire, 7303)


def test_a_different_browser_is_no_match(app_client, wire):
    assert started_checkout(app_client, SECOND, "v_tap0000005", ua("tap5")).status_code == 200
    assert post_order(app_client, SECOND, SECOND_KEY, wallet_order(7304, IPV6, ua=ua("tap5-other"))).status_code == 200
    assert "fbc" not in purchase_user_data(wire, 7304)


def test_page_views_alone_never_count_as_starting_checkout(app_client, wire):
    assert landing(app_client, SECOND, "v_tap0000006", "81.9.9.9",
                   fbc=click_id(0, "IwARbrowseonly" + "x" * 20), ua=ua("tap6")).status_code == 200
    assert post_order(app_client, SECOND, SECOND_KEY, wallet_order(7305, IPV6, ua=ua("tap6"))).status_code == 200
    assert "fbc" not in purchase_user_data(wire, 7305)


def test_checkout_starter_known_under_another_email_is_no_match(app_client, wire):
    assert started_checkout(app_client, SECOND, "v_tap0000007", ua("tap7")).status_code == 200
    first = {**wallet_order(7306, IPV6, ua=ua("tap7"), email="earlier@example.com"),
             "note_attributes": [{"name": "_comfi_vid", "value": "v_tap0000007"}]}
    assert post_order(app_client, SECOND, SECOND_KEY, first).status_code == 200
    assert post_order(app_client, SECOND, SECOND_KEY,
                      wallet_order(7307, IPV6, ua=ua("tap7"), email="someone.else@example.com")).status_code == 200
    assert "fbc" not in purchase_user_data(wire, 7307)


def test_checkout_match_stays_in_its_own_store(app_client, wire):
    assert started_checkout(app_client, COMFI, "v_tap0000008", ua("tap8")).status_code == 200
    assert post_order(app_client, SECOND, SECOND_KEY, wallet_order(7308, IPV6, ua=ua("tap8"))).status_code == 200
    assert "fbc" not in purchase_user_data(wire, 7308)


def test_orders_that_already_have_a_visitor_never_use_the_checkout_match(app_client, wire):
    assert started_checkout(app_client, SECOND, "v_tap0000009", ua("tap9")).status_code == 200
    body = {**wallet_order(7309, IPV6, ua=ua("tap9")), "note_attributes": [{"name": "_comfi_vid", "value": "v_ownvisitor1"}]}
    assert post_order(app_client, SECOND, SECOND_KEY, body).status_code == 200
    user = purchase_user_data(wire, 7309)
    assert "fbc" not in user and main.hash_data("v_ownvisitor1") in user["external_id"]
