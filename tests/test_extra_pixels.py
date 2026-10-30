"""Extra pixels: a store can send every event to a second pixel (e.g. one in
another Business Manager) as well as its main one, same event IDs, each with
its own token, without touching what the main pixel gets."""
import json
import time

import pytest
from unittest.mock import patch

import hub
import main
import stores
from conftest import hub_client
from test_hub import Wire, post_order, order, track, wire  # noqa: F401  (wire is a fixture)

SHOP = "extra-test.myshopify.com"
KEY = "extra-store-webhook-key"
MAIN_PIXEL, MAIN_TOKEN = "777000111222", "MAIN" + "m" * 60
EXTRA_PIXEL, EXTRA_TOKEN = "888000111222", "EXTRA" + "x" * 60


def form(**changes):
    return {"csrf": "t", "name": "Extra Store", "shop_domain": SHOP, "site_url": "https://extra.example",
            "pixel_id": MAIN_PIXEL, "default_country": "GB", "currency": "GBP", "enabled": "on", **changes}


def row():
    stores.forget_cached()
    return next(s for s in stores.list_stores() if s["shop_domain"] == SHOP)


@pytest.fixture(scope="module", autouse=True)
def extra_store():
    w = Wire()
    with patch("requests.get", side_effect=w.get):
        r = hub_client().post("/hub/stores/new", data=form(
            access_token=MAIN_TOKEN, webhook_secret=KEY,
            extra_pixel_id=EXTRA_PIXEL, extra_access_token=EXTRA_TOKEN))
    assert r.status_code == 302, r.data
    stores.forget_cached()
    yield


def wait_for(wire, count, seconds=5):
    end = time.time() + seconds
    while len(wire.meta()) < count and time.time() < end:
        time.sleep(0.02)
    time.sleep(0.05)  # anything extra would show up now
    return wire.meta()


def by_pixel(posts):
    return {p["url"].split("/")[-2]: p for p in posts}


def test_token_is_stored_encrypted_and_read_back():
    store = row()
    assert store["extra_pixels"] == [{"pixel_id": EXTRA_PIXEL, "access_token": EXTRA_TOKEN}]
    assert EXTRA_TOKEN not in (store["extra_pixels_enc"] or "")


def test_browser_event_goes_to_both_pixels_with_their_own_tokens(app_client, wire):
    assert track(app_client, SHOP, event_id="ev_both1").status_code == 200
    sent = by_pixel(wait_for(wire, 2))
    assert set(sent) == {MAIN_PIXEL, EXTRA_PIXEL}
    assert sent[MAIN_PIXEL]["body"]["access_token"] == MAIN_TOKEN
    assert sent[EXTRA_PIXEL]["body"]["access_token"] == EXTRA_TOKEN
    # The same event, same ID, so each pixel dedupes against its own browser copy.
    assert sent[MAIN_PIXEL]["body"]["data"] == sent[EXTRA_PIXEL]["body"]["data"]
    assert sent[EXTRA_PIXEL]["body"]["data"][0]["event_id"] == "ev_both1"


def test_purchase_webhook_goes_to_both_pixels(app_client, wire):
    assert post_order(app_client, SHOP, KEY, order(91001)).status_code == 200
    sent = by_pixel(wait_for(wire, 2))
    assert set(sent) == {MAIN_PIXEL, EXTRA_PIXEL}
    assert {p["body"]["data"][0]["event_id"] for p in sent.values()} == {"purchase_91001"}


def test_extra_pixel_failure_never_touches_the_main_one(app_client, wire):
    original = wire.post

    def flaky(url, **kwargs):
        if f"/{EXTRA_PIXEL}/" in url:
            wire.posts.append({"url": url, "body": kwargs.get("json")})
            return wire.response(400, {"error": {"message": "Invalid OAuth access token"}})
        return original(url, **kwargs)

    with patch("requests.post", side_effect=flaky):
        assert track(app_client, SHOP, event_id="ev_flaky").status_code == 200
        sent = by_pixel(wait_for(wire, 2))
    assert set(sent) == {MAIN_PIXEL, EXTRA_PIXEL}


def test_main_pixel_only_when_no_extra(app_client, wire):
    """Other stores (Comfi above all) send exactly one request per event."""
    assert track(app_client, "v8284i-f1.myshopify.com", event_id="ev_comfi_only").status_code == 200
    sent = wait_for(wire, 2, seconds=0.3)
    assert len(sent) == 1 and sent[0]["url"].endswith("/1449380569544787/events")


def test_extra_copies_stay_out_of_the_store_counts(app_client, wire):
    track(app_client, SHOP, event_id="ev_count1")
    wait_for(wire, 2)
    time.sleep(2.5)  # the delivery log writes in batches every 2 seconds
    facts = hub.store_facts(SHOP)
    assert facts["extra_total"] >= 1
    main_rows = sum(e["count"] for e in facts["events"])
    assert main_rows >= 1 and facts["total"] == main_rows
    with stores.db() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM meta_deliveries WHERE shop = %s AND source = 'extra:browser'", (SHOP,))
        cur.execute("SELECT count(*) FROM meta_deliveries WHERE shop = %s AND source LIKE 'extra:%%'"
                    " AND ts > now() - interval '24 hours'", (SHOP,))
        assert cur.fetchone()[0] == facts["extra_total"]


def test_snippet_and_checkout_pixel_init_both_pixels(logged_in):
    store_id = row()["id"]
    snippet = logged_in.get(f"/hub/stores/{store_id}/snippet.liquid").get_data(as_text=True)
    assert f"fbq('init', '{MAIN_PIXEL}', m);" in snippet and f"fbq('init', '{EXTRA_PIXEL}', m);" in snippet
    assert f"tr?id={EXTRA_PIXEL}&ev=PageView" in snippet
    assert "__" not in snippet.replace("_fbq", "")
    pixel = logged_in.get(f"/hub/stores/{store_id}/checkout-pixel.js").get_data(as_text=True)
    assert f'const EXTRA_PIXEL_IDS = ["{EXTRA_PIXEL}"];' in pixel and "<" not in pixel


def test_other_stores_code_has_no_extra_pixel(logged_in):
    snippet = logged_in.get("/hub/stores/comfi-uk/snippet.liquid").get_data(as_text=True)
    assert snippet.count("fbq('init'") == 1
    pixel = logged_in.get("/hub/stores/comfi-uk/checkout-pixel.js").get_data(as_text=True)
    assert "const EXTRA_PIXEL_IDS = [];" in pixel


def test_blank_token_keeps_the_saved_one_and_blank_id_removes_it(logged_in):
    store_id = row()["id"]
    w = Wire()
    with patch("requests.get", side_effect=w.get):
        assert logged_in.post(f"/hub/stores/{store_id}", data=form(extra_pixel_id=EXTRA_PIXEL)).status_code == 302
        assert row()["extra_pixels"] == [{"pixel_id": EXTRA_PIXEL, "access_token": EXTRA_TOKEN}]

        r = logged_in.post(f"/hub/stores/{store_id}", data=form(extra_pixel_id="999000111222"))
        assert r.status_code == 200 and b"access token for the extra pixel" in r.data

        r = logged_in.post(f"/hub/stores/{store_id}", data=form(extra_pixel_id=MAIN_PIXEL, extra_access_token=EXTRA_TOKEN))
        assert r.status_code == 200 and b"different pixel" in r.data

        assert logged_in.post(f"/hub/stores/{store_id}", data=form(extra_pixel_id="")).status_code == 302
        assert row()["extra_pixels"] == [] and row()["extra_pixels_enc"] is None

        # Put it back for any test that runs after.
        assert logged_in.post(f"/hub/stores/{store_id}", data=form(
            extra_pixel_id=EXTRA_PIXEL, extra_access_token=EXTRA_TOKEN)).status_code == 302
