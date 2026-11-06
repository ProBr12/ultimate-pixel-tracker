"""The US pixel gets only US events. UK orders (and every other non-US order)
never reach it, whether it is a store's extra pixel or its main one, and the
UK pixels keep getting every order exactly as before."""
import time

import pytest
from unittest.mock import patch

import main
import stores
from conftest import hub_client
from test_hub import UA, Wire, post_order, track, wire  # noqa: F401  (wire is a fixture)

US_PIXEL = main.US_PIXEL_ID
UK_PIXEL, UK_PIXEL_2 = "1449380569544787", "2183170248967407"
UK_TOKEN, UK_TOKEN_2, US_TOKEN = "UKMAIN" + "u" * 60, "UKTWO" + "k" * 60, "USPIX" + "s" * 60

US_EXTRA_SHOP, US_EXTRA_KEY = "us-extra-test.myshopify.com", "us-extra-webhook-key"
US_MAIN_SHOP, US_MAIN_KEY = "us-main-test.myshopify.com", "us-main-webhook-key"


def add_store(shop, key, name, pixel, token, extras):
    data = {"csrf": "t", "name": name, "shop_domain": shop, "site_url": "https://comfi.example",
            "pixel_id": pixel, "access_token": token, "webhook_secret": key,
            "default_country": "GB", "currency": "GBP", "enabled": "on"}
    for suffix, (extra_id, extra_token) in zip(("", "_2", "_3"), extras):
        data[f"extra_pixel_id{suffix}"] = extra_id
        data[f"extra_access_token{suffix}"] = extra_token
    w = Wire()
    with patch("requests.get", side_effect=w.get):
        r = hub_client().post("/hub/stores/new", data=data)
    assert r.status_code == 302, r.data


@pytest.fixture(scope="module", autouse=True)
def us_stores():
    # UK main pixel, US pixel as an extra (how it is set up today).
    add_store(US_EXTRA_SHOP, US_EXTRA_KEY, "US Extra Store", UK_PIXEL, UK_TOKEN,
              [(US_PIXEL, US_TOKEN), (UK_PIXEL_2, UK_TOKEN_2)])
    # US pixel as the main one, both UK pixels as extras.
    add_store(US_MAIN_SHOP, US_MAIN_KEY, "US Main Store", US_PIXEL, US_TOKEN,
              [(UK_PIXEL, UK_TOKEN), (UK_PIXEL_2, UK_TOKEN_2)])
    stores.forget_cached()
    yield


def sent_after(wire, count, seconds=5):
    """Meta posts once `count` have arrived, plus a moment for any stray extra."""
    end = time.time() + seconds
    while len(wire.meta()) < count and time.time() < end:
        time.sleep(0.02)
    time.sleep(0.2)
    return wire.meta()


def pixels(posts):
    return sorted(p["url"].split("/")[-2] for p in posts)


def token_for(posts, pixel):
    return next(p["body"]["access_token"] for p in posts if p["url"].split("/")[-2] == pixel)


def uk_order(order_id, **changes):
    return {"id": order_id, "email": "buyer@example.com", "total_price": "49.99", "currency": "GBP",
            "presentment_currency": "GBP",
            "total_price_set": {"presentment_money": {"amount": "49.99", "currency_code": "GBP"}},
            "shipping_address": {"country": "United Kingdom", "country_code": "GB"},
            "billing_address": {"phone": "07767 304185", "country_code": "GB"},
            "client_details": {"user_agent": UA}, "line_items": [], **changes}


# ============ PURCHASE WEBHOOK, US PIXEL AS AN EXTRA ============

def test_uk_order_never_reaches_the_us_pixel(app_client, wire):
    assert post_order(app_client, US_EXTRA_SHOP, US_EXTRA_KEY, uk_order(70001)).status_code == 200
    sent = sent_after(wire, 2)
    assert not any(f"/{US_PIXEL}/events" in p["url"] for p in sent)
    assert pixels(sent) == sorted([UK_PIXEL, UK_PIXEL_2])
    # UK pixels get it as before: own tokens, same event.
    assert token_for(sent, UK_PIXEL) == UK_TOKEN and token_for(sent, UK_PIXEL_2) == UK_TOKEN_2
    assert {p["body"]["data"][0]["event_id"] for p in sent} == {"purchase_70001"}


@pytest.mark.parametrize("order_id,changes", [
    (70002, {"presentment_currency": "USD",
             "total_price_set": {"presentment_money": {"amount": "59.99", "currency_code": "USD"}}}),
    (70003, {"total_price_set": {"presentment_money": {"amount": "59.99", "currency_code": "USD"}},
             "presentment_currency": None}),
    (70004, {"shipping_address": {"country": "United States", "country_code": "US"}}),
    (70005, {"shipping_address": {"country": "United States"}}),
])
def test_us_order_reaches_the_us_pixel_and_the_uk_ones(app_client, wire, order_id, changes):
    assert post_order(app_client, US_EXTRA_SHOP, US_EXTRA_KEY, uk_order(order_id, **changes)).status_code == 200
    sent = sent_after(wire, 3)
    assert pixels(sent) == sorted([UK_PIXEL, UK_PIXEL_2, US_PIXEL])
    assert token_for(sent, US_PIXEL) == US_TOKEN and token_for(sent, UK_PIXEL) == UK_TOKEN
    assert {p["body"]["data"][0]["event_id"] for p in sent} == {f"purchase_{order_id}"}


# ============ PURCHASE WEBHOOK, US PIXEL AS THE MAIN ONE ============

def test_uk_order_skips_a_us_main_pixel_but_reaches_the_uk_extras(app_client, wire):
    assert post_order(app_client, US_MAIN_SHOP, US_MAIN_KEY, uk_order(71001)).status_code == 200
    sent = sent_after(wire, 2)
    assert not any(f"/{US_PIXEL}/events" in p["url"] for p in sent)
    assert pixels(sent) == sorted([UK_PIXEL, UK_PIXEL_2])
    assert token_for(sent, UK_PIXEL) == UK_TOKEN and token_for(sent, UK_PIXEL_2) == UK_TOKEN_2
    assert sent[0]["body"]["data"] == sent[1]["body"]["data"]


def test_us_order_reaches_a_us_main_pixel_and_the_uk_extras(app_client, wire):
    order = uk_order(71002, presentment_currency="USD",
                     shipping_address={"country": "United States", "country_code": "US"})
    assert post_order(app_client, US_MAIN_SHOP, US_MAIN_KEY, order).status_code == 200
    sent = sent_after(wire, 3)
    assert pixels(sent) == sorted([UK_PIXEL, UK_PIXEL_2, US_PIXEL])
    assert token_for(sent, US_PIXEL) == US_TOKEN


def test_unsigned_order_still_refused(app_client, wire):
    assert post_order(app_client, US_EXTRA_SHOP, "wrong-key", uk_order(72001)).status_code == 401
    assert sent_after(wire, 1, seconds=0.3) == []


def test_store_without_the_us_pixel_is_unchanged(app_client, wire):
    """Comfi UK (main pixel only) still sends exactly one request per order."""
    assert post_order(app_client, "v8284i-f1.myshopify.com", "comfi-webhook-secret",
                      uk_order(72002)).status_code == 200
    sent = sent_after(wire, 1)
    assert pixels(sent) == [UK_PIXEL]


# ============ BROWSER /track ============

def test_uk_browser_event_never_reaches_the_us_pixel(app_client, wire):
    assert track(app_client, US_EXTRA_SHOP, event_id="ev_uk1",
                 source_url="https://comfishop.com/products/x").status_code == 200
    assert pixels(sent_after(wire, 2)) == sorted([UK_PIXEL, UK_PIXEL_2])


@pytest.mark.parametrize("extra", [
    {"source_url": "https://comfishop.com/en-us/products/x"},
    {"event_name": "AddToCart", "value": 39.99, "currency": "USD"},
])
def test_us_browser_event_reaches_the_us_pixel(app_client, wire, extra):
    assert track(app_client, US_EXTRA_SHOP, event_id="ev_us1", **extra).status_code == 200
    assert pixels(sent_after(wire, 3)) == sorted([UK_PIXEL, UK_PIXEL_2, US_PIXEL])


def test_uk_browser_event_skips_a_us_main_pixel(app_client, wire):
    assert track(app_client, US_MAIN_SHOP, event_id="ev_uk2").status_code == 200
    assert pixels(sent_after(wire, 2)) == sorted([UK_PIXEL, UK_PIXEL_2])


# ============ THE RULES ============

@pytest.mark.parametrize("order,us", [
    ({}, False),
    ({"presentment_currency": "GBP", "shipping_address": {"country_code": "GB"}}, False),
    ({"currency": "USD"}, False),  # shop currency alone says nothing about the buyer
    ({"presentment_currency": "usd"}, True),
    ({"total_price_set": {"presentment_money": {"currency_code": "USD"}}}, True),
    ({"shipping_address": {"country_code": "US"}}, True),
    ({"shipping_address": {"country": "US"}}, True),
    ({"shipping_address": {"country": "United States"}}, True),
    ({"shipping_address": None, "total_price_set": None}, False),
    ({"shipping_address": "US", "total_price_set": {"presentment_money": "USD"}}, False),
])
def test_is_us_order(order, us):
    assert main.is_us_order(order) is us


def test_route_keeps_everything_for_us_and_only_drops_the_us_pixel_otherwise():
    config = {"pixel_id": UK_PIXEL, "access_token": UK_TOKEN,
              "extra_pixels": [{"pixel_id": US_PIXEL, "access_token": US_TOKEN},
                               {"pixel_id": UK_PIXEL_2, "access_token": UK_TOKEN_2}]}
    assert main.us_pixel_route(config, True) == (config, True)
    route, primary = main.us_pixel_route(config, False)
    assert primary is True and route["pixel_id"] == UK_PIXEL and route["access_token"] == UK_TOKEN
    assert route["extra_pixels"] == [{"pixel_id": UK_PIXEL_2, "access_token": UK_TOKEN_2}]
    assert main.us_pixel_route({**config, "pixel_id": US_PIXEL}, False)[1] is False
