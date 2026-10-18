"""The browser code the hub generates: the checkout pixel passes Shopify's
editor rules and JSHint, and browser and server read phone numbers and names
identically. The JavaScript runs in Node (browser_harness.js)."""
import hashlib
import json
import os
import re
import shutil
import subprocess

import pytest

import hub
import main

HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "browser_harness.js")
COMFI = {"id": "comfi-uk", "name": "Comfi UK", "pixel_id": "1449380569544787",
         "shop_domain": "v8284i-f1.myshopify.com", "default_country": "GB"}


def node(mode, code, data, tmp_path):
    code_path, data_path = tmp_path / "code.js", tmp_path / "data.json"
    code_path.write_text(code, encoding="utf-8")
    data_path.write_text(json.dumps(data), encoding="utf-8")
    result = subprocess.run(["node", HARNESS, mode, str(code_path), str(data_path)],
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


# ============ PHONE PARITY ============

PHONE_CASES = [
    # GB: the existing behaviour, plus the formats shoppers type
    ("07767 304185", "GB", "447767304185"), ("+44 7767 304185", "GB", "447767304185"),
    ("0044 7767 304185", "GB", "447767304185"), ("447767304185", "GB", "447767304185"),
    ("7767304185", "GB", "447767304185"), ("020 7946 0018", "GB", "442079460018"),
    ("12345", "GB", ""), ("", "GB", ""),
    # US
    ("(555) 625-1199", "US", "15556251199"), ("555-625-1199", "US", "15556251199"),
    ("1 555 625 1199", "US", "15556251199"), ("+1 555 625 1199", "US", "15556251199"),
    ("011 44 7767 304185", "US", "447767304185"), ("625-1199", "US", ""),
    # IE
    ("087 123 4567", "IE", "353871234567"), ("+353 87 123 4567", "IE", "353871234567"),
    ("01 234 5678", "IE", "35312345678"), ("00353 1 234 5678", "IE", "35312345678"),
    # DE
    ("0151 23456789", "DE", "4915123456789"), ("+49 151 23456789", "DE", "4915123456789"),
    ("030 1234567", "DE", "49301234567"), ("0049 30 1234567", "DE", "49301234567"),
    # AU
    ("0412 345 678", "AU", "61412345678"), ("+61 412 345 678", "AU", "61412345678"),
    ("(02) 9876 5432", "AU", "61298765432"), ("0011 44 7767 304185", "AU", "447767304185"),
    # AE
    ("050 123 4567", "AE", "971501234567"), ("+971 50 123 4567", "AE", "971501234567"),
    ("04 123 4567", "AE", "97141234567"), ("00971 4 123 4567", "AE", "97141234567"),
    # A number with its own country code wins over the address's country
    ("+44 7767 304185", "US", "447767304185"), ("+353 87 123 4567", "GB", "353871234567"),
    # No country code and no known country: never guessed
    ("0123 456789", "", ""), ("0123 456789", "XX", ""), ("+44 7767 304185", "", "447767304185"),
]


def test_server_phone_rules():
    assert [main.normalize_phone(p, c) for p, c, _ in PHONE_CASES] == [want for _, _, want in PHONE_CASES]


def test_browser_matches_server(tmp_path):
    got = node("phone", hub.PHONE_JS, [[p, c] for p, c, _ in PHONE_CASES], tmp_path)
    assert got == [want for _, _, want in PHONE_CASES]


def test_old_comfi_phone_outputs_unchanged():
    """The previous rules for UK and US/CA, on the numbers they handled correctly."""
    for phone, country, want in [("07767 304185", "GB", "447767304185"), ("447767304185", "GB", "447767304185"),
                                 ("+44 7767 304185", "GB", "447767304185"), ("(555) 625-1199", "US", "15556251199"),
                                 ("15556251199", "US", "15556251199"), ("416 555 0123", "CA", "14165550123")]:
        assert main.normalize_phone(phone, country) == want


def test_wrong_numbers_dropped_not_guessed():
    """The old code turned an Irish number on a UK store into a fake UK one."""
    assert main.normalize_phone("087 123 4567", "IE") == "353871234567"
    assert main.normalize_phone("0000", "GB") == ""
    data = main.hashed_user_data(phone="12345", country="GB")
    assert "ph" not in data


def test_phone_table_covers_parity_countries():
    rules = json.loads(hub.PHONE_RULES_JSON)
    assert {"GB", "US", "CA", "IE", "DE", "AU", "AE"} <= set(rules)
    assert rules["GB"][:2] == ["44", "0"] and rules["US"][:2] == ["1", "1"]


# ============ CHECKOUT PIXEL ============

def comfi_pixel():
    return hub.render_checkout_pixel(COMFI, "https://track.comfishop.com/meta/context")


def test_pixel_passes_shopify_editor_rules():
    code = comfi_pixel()
    assert "<" not in code
    assert not [line for line in code.splitlines() if line.lstrip().startswith("?")]
    assert "__" not in code.replace("_fbq", "").replace("__proto__", "")  # every placeholder filled
    assert 'const PIXEL_ID = "1449380569544787";' in code
    assert 'const CONTEXT_URL = "https://track.comfishop.com/meta/context";' in code
    assert 'const SHOP = "v8284i-f1.myshopify.com";' in code
    assert f"const VERSION = {hub.CHECKOUT_PIXEL_VERSION};" in code


def test_pixel_store_name_cannot_break_out():
    code = hub.render_checkout_pixel(dict(COMFI, name="Bad</script>\n?x"), "https://h.example/meta/context")
    assert "<" not in code and "\n?" not in code


@pytest.mark.skipif(not shutil.which("npx"), reason="needs Node's npx")
def test_pixel_passes_jshint(tmp_path):
    (tmp_path / "pixel.js").write_text(comfi_pixel(), encoding="utf-8")
    (tmp_path / ".jshintrc").write_text('{"esversion": 11}', encoding="utf-8")
    result = subprocess.run([shutil.which("npx"), "--yes", "jshint@2.13.6", "--config", str(tmp_path / ".jshintrc"),
                             str(tmp_path / "pixel.js")], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr


CHECKOUT = {
    "token": "chk123abcdef", "email": " Buyer@Example.com", "phone": None, "smsMarketingPhone": None,
    "totalPrice": {"amount": "199.00", "currencyCode": "GBP"}, "currencyCode": "GBP",
    "order": {"id": "gid://shopify/Order/987654", "customer": {"phone": None}},
    "billingAddress": {"firstName": "Zoë", "lastName": "O'Brien-Smith", "city": "St Albans", "provinceCode": "ENG",
                       "zip": "AL1 3XY", "countryCode": "GB", "phone": "07767 304185"},
    "shippingAddress": {},
    "lineItems": [{"quantity": 2, "variant": {"price": {"amount": "99.50"},
                                              "product": {"id": "gid://shopify/Product/9001"}}}],
}
STORAGE = {"comfi_vid": "v_abc123def456"}
COOKIES = {"_fbp": "fb.1.1759990000000.1234567890", "_fbc": "fb.1.1759990000000.IwAR3xYzAbCdEfGhIjKlMnOp"}


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def test_pixel_purchase_and_context(tmp_path):
    got = node("pixel", comfi_pixel(), {"shop": "v8284i-f1.myshopify.com", "checkout": CHECKOUT,
                                        "storage": STORAGE, "cookies": COOKIES}, tmp_path)
    init, track = got["fbq"]
    assert init[:2] == ["init", "1449380569544787"]
    user = init[2]
    assert user == {"em": "buyer@example.com", "ph": "447767304185", "fn": "zoë", "ln": "obriensmith",
                    "ct": "stalbans", "st": "eng", "zp": "al13xy", "country": "gb",
                    "external_id": sha("v_abc123def456")}
    assert track[:2] == ["track", "Purchase"]
    assert track[3] == {"eventID": "purchase_987654"}
    assert track[2] == {"value": 199, "currency": "GBP", "order_id": "987654",
                        "contents": [{"id": "9001", "quantity": 2, "item_price": 99.5}],
                        "content_ids": ["9001"], "content_type": "product", "num_items": 2}
    context = got["fetches"][0]
    assert context["url"] == "https://track.comfishop.com/meta/context"
    assert context["body"] == {"checkout_token": "chk123abcdef", "order_id": "987654", "vid": "v_abc123def456",
                               "fbp": COOKIES["_fbp"], "fbc": COOKIES["_fbc"], "user_agent": "TestAgent/1.0",
                               "shop": "v8284i-f1.myshopify.com", "v": hub.CHECKOUT_PIXEL_VERSION}

    # The server hashes the same buyer to the same values (browser and server agree).
    server = main.hashed_user_data(
        email=" Buyer@Example.com", phone=["07767 304185"], first_name="Zoë", last_name="O'Brien-Smith",
        city="St Albans", postcode="AL1 3XY", region="ENG", country="GB", external_id="v_abc123def456")
    for key in ("em", "ph", "fn", "ln", "ct", "st", "zp", "country"):
        assert server[key] == [sha(user[key])], key
    assert server["external_id"] == [user["external_id"]]


def test_pixel_in_the_wrong_store_does_nothing(tmp_path):
    got = node("pixel", comfi_pixel(), {"shop": "someone-else.myshopify.com", "checkout": CHECKOUT}, tmp_path)
    assert got == {"subscribed": [], "fetches": [], "fbq": []}


def test_pixel_us_buyer_phone(tmp_path):
    us = dict(CHECKOUT, billingAddress=dict(CHECKOUT["billingAddress"], countryCode="US", phone="(555) 625-1199",
                                            zip="94105-1234", provinceCode="CA"))
    got = node("pixel", comfi_pixel(), {"shop": "v8284i-f1.myshopify.com", "checkout": us}, tmp_path)
    user = got["fbq"][0][2]
    assert user["ph"] == "15556251199" and user["zp"] == "94105" and user["country"] == "us"


# ============ THEME SNIPPET ============

def liquid(code, values, logged_in):
    """Just enough Liquid to run the snippet's scripts outside Shopify."""
    code = re.sub(r"\{%-? comment -?%\}.*?\{%-? endcomment -?%\}", "", code, flags=re.S)
    code = re.sub(r"\{% if customer %\}(.*?)\{% else %\}(.*?)\{% endif %\}",
                  lambda m: m.group(1) if logged_in else m.group(2), code, flags=re.S)
    code = re.sub(r"\{% if template\.name == 'product' and product %\}.*?\{% else %\}(.*?)\{% endif %\}",
                  lambda m: m.group(1), code, flags=re.S)
    code = re.sub(r"\{%-? (unless|endunless)[^%]*-?%\}", "", code)

    def value(m):
        expr = m.group(1)
        assert expr in values, f"no test value for {expr}"
        return json.dumps(values[expr])
    code = re.sub(r"\{\{\s*([A-Za-z_.\[\]'0-9]+)[^}]*\}\}", value, code)
    assert "{{" not in code and "{%" not in code
    return re.findall(r"<script>(.*?)</script>", code, flags=re.S)


def theme_values(**customer):
    values = {"cart.attributes['_comfi_vid']": None, "cart.attributes['_fbp']": None,
              "cart.attributes['_fbc']": None, "localization.country.iso_code": "GB",
              "cart.currency.iso_code": "GBP", "cart.total_price": 0, "cart.items": [], "cart.item_count": 0,
              "routes.cart_update_url": "/cart/update", "routes.cart_url": "/cart",
              "shop.permanent_domain": "v8284i-f1.myshopify.com"}
    for key in ("email", "phone", "first_name", "last_name"):
        values[f"customer.{key}"] = customer.get(key)
    for key in ("city", "zip", "province_code", "country_code"):
        values[f"customer.default_address.{key}"] = customer.get(key)
    return values


def run_theme(tmp_path, store, logged_in, **customer):
    values = theme_values(**customer)
    if "localization" in customer:
        values["localization.country.iso_code"] = customer["localization"]
    scripts = liquid(hub.render_snippet(store, "https://track.comfishop.com/track"), values, logged_in)
    assert len(scripts) == 3
    return node("theme", json.dumps(scripts), {"url": "https://comfishop.com/"}, tmp_path)


def test_theme_gb_customer(tmp_path):
    got = run_theme(tmp_path, COMFI, True, email="Member@Example.com", phone="07767 304185", first_name="Zoë",
                    last_name="O'Brien", city="St Albans", zip="AL1 3XY", province_code="ENG", country_code="GB")
    init = next(c for c in got["fbq"] if c[0] == "init")
    assert init[1] == "1449380569544787"
    assert init[2] == {"country": "gb", "em": "member@example.com", "ph": "447767304185", "fn": "zoë",
                       "ln": "obrien", "ct": "stalbans", "st": "eng", "zp": "al13xy"}
    page_view = got["track"][0]
    assert page_view["event_name"] == "PageView"
    assert page_view["shop"] == "v8284i-f1.myshopify.com" and page_view["v"] == hub.SNIPPET_VERSION
    assert page_view["email"] == "Member@Example.com" and page_view["phone"] == "07767 304185"
    assert page_view["vid"].startswith("v_")


def test_theme_us_customer(tmp_path):
    got = run_theme(tmp_path, COMFI, True, phone="(555) 625-1199", country_code="US", zip="94105-1234")
    init = next(c for c in got["fbq"] if c[0] == "init")
    assert init[2]["ph"] == "15556251199" and init[2]["zp"] == "94105"


def test_theme_falls_back_to_store_country(tmp_path):
    irish = dict(COMFI, default_country="IE")
    got = run_theme(tmp_path, irish, True, phone="087 123 4567", localization=None)
    init = next(c for c in got["fbq"] if c[0] == "init")
    assert init[2] == {"ph": "353871234567"}


def test_theme_guest(tmp_path):
    got = run_theme(tmp_path, COMFI, False)
    init = next(c for c in got["fbq"] if c[0] == "init")
    assert init[2] == {"country": "gb"}
    assert [c[1] for c in got["fbq"] if c[0] == "track"] == ["PageView"]
