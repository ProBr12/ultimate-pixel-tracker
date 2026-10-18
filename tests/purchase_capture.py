"""Replays fixed Comfi traffic through one version of the tracker and writes
every request it sends to Meta, as JSON, for the regression test.

  python purchase_capture.py <code_dir> <database_url> <out.json>

Run once with the code from the previous commit and once with the current
code; the test compares the two outputs byte for byte. Meta is mocked.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
from unittest.mock import MagicMock, patch

code_dir, database_url, out_path = sys.argv[1:4]
sys.path.insert(0, code_dir)
os.chdir(code_dir)
os.environ.update({
    "DATABASE_URL": database_url, "PIXEL_ID": "1449380569544787", "ACCESS_TOKEN": "COMFI" + "a" * 60,
    "SHOPIFY_WEBHOOK_SECRET": "comfi-webhook-secret", "HUB_SECRET_KEY": "test-hub-secret-key-0123456789",
    "DASH_PASSWORD": "pw", "GOOGLE_TRACKING_ENABLED": "false",
})
os.environ.pop("HUB_PUBLIC_HOST", None)

SHOP = "v8284i-f1.myshopify.com"
NOW = 1760000000.0
UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Version/17.5 Mobile Safari/604.1"
VID = "v_abc123def456"
FBC = "fb.1.1759990000000.IwAR3xYzAbCdEfGhIjKlMnOp"
FBP = "fb.1.1759990000000.1234567890"
sent = []


def fake_post(url, json=None, timeout=None, **kwargs):
    sent.append({"url": url, "body": json})
    response = MagicMock(status_code=200, text='{"events_received":1}')
    response.json.return_value = {"events_received": 1}
    return response


def signed(body):
    raw = json.dumps(body).encode()
    sig = base64.b64encode(hmac.new(b"comfi-webhook-secret", raw, hashlib.sha256).digest()).decode()
    return raw, sig


def address(**extra):
    base = {"first_name": "Zoë", "last_name": "O'Brien-Smith", "address1": "1 High St", "city": "St Albans",
            "province_code": "ENG", "zip": "AL1 3XY", "country_code": "GB", "phone": None}
    base.update(extra)
    return base


def order(order_id, **extra):
    body = {
        "id": order_id, "email": "Buyer@Example.com ", "phone": None, "total_price": "199.00", "currency": "GBP",
        "checkout_token": f"tok{order_id}abcdef", "order_status_url": f"https://comfishop.com/123/orders/x?key=secret",
        "landing_site": "/products/comfi-pro", "browser_ip": "81.2.69.160",
        "client_details": {"browser_ip": "81.2.69.160", "user_agent": UA},
        "note_attributes": [{"name": "_comfi_vid", "value": VID}, {"name": "_fbc", "value": FBC},
                            {"name": "_fbp", "value": FBP}],
        "billing_address": address(phone="07767 304185"), "shipping_address": address(),
        "customer": {"id": 7001, "email": "buyer@example.com", "phone": None},
        "line_items": [{"product_id": 9001, "quantity": 2, "price": "99.50"}, {"product_id": None, "quantity": 1}],
    }
    body.update(extra)
    return body


ORDERS = [
    order(5001),
    order(5002, phone="+447767304185", billing_address=address(phone=None)),
    order(5003, note_attributes=[], client_details={}, browser_ip=None),  # express: from the checkout pixel
    order(5004, note_attributes=[{"name": "_comfi_vid", "value": "v_zzz999yyy888"}],
          landing_site="/products/x?utm_source=fb&fbclid=IwAR0abcdefghijklmnop"),
    order(5005, billing_address=address(phone="(555) 625-1199", country_code="US", province_code="CA",
                                        zip="94105-1234", city="San Francisco")),
    order(5006, billing_address=address(phone=None), shipping_address=address(phone="07700 900123")),
    order(5007, billing_address=address(phone=None), customer={"id": 7007, "phone": "+44 7700 900456"}),
    order(5008, billing_address={}, shipping_address={}, customer={}, email="", line_items=[]),
    order(5009, test=True),
]

CHECKOUT = {
    "token": "chk5010abcdefgh", "email": "shopper@example.com", "phone": "07700 900789",
    "total_price": "149.00", "presentment_currency": "GBP",
    "abandoned_checkout_url": "https://comfishop.com/checkouts/x?key=secret",
    "note_attributes": [{"name": "_comfi_vid", "value": "v_chk999aaa111"}, {"name": "_fbp", "value": FBP}],
    "billing_address": address(phone="07700 900789"), "shipping_address": address(),
    "customer": {"id": 7010}, "line_items": [{"product_id": 9002, "quantity": 1, "price": "149.00"}],
}

with patch("requests.post", side_effect=fake_post), patch("time.time", return_value=NOW):
    import main
    client = main.app.test_client()
    # The checkout pixel's context for the express order (as the live v2 pixel sends it).
    r = client.post("/meta/context", data=json.dumps({
        "checkout_token": "tok5003abcdef", "order_id": "5003", "vid": "v_express00001", "fbp": FBP, "fbc": FBC,
        "user_agent": UA}), content_type="text/plain")
    assert r.status_code == 200, r.data
    # A browser AddToCart and InitiateCheckout from the theme (gives the checkout its user agent).
    for name in ("AddToCart", "InitiateCheckout"):
        r = client.post("/track", data=json.dumps({
            "event_name": name, "event_id": f"evt_{name}", "vid": "v_chk999aaa111", "fbp": FBP, "fbc": "",
            "source_url": "https://comfishop.com/cart", "value": 149, "currency": "GBP", "shop": SHOP,
            "country": "GB", "content_ids": ["9002"], "content_type": "product", "num_items": 1}),
            content_type="text/plain", headers={"User-Agent": UA, "X-Forwarded-For": "81.2.69.160"})
        assert r.status_code == 200, r.data
    # A logged-in shopper's PageView with their own email and phone.
    r = client.post("/track", data=json.dumps({
        "event_name": "PageView", "event_id": "evt_pv_logged_in", "vid": "v_loggedin0001", "fbp": FBP,
        "source_url": "https://comfishop.com/", "shop": SHOP, "country": "GB",
        "email": "member@example.com", "phone": "+447700900111"}),
        content_type="text/plain", headers={"User-Agent": UA, "X-Forwarded-For": "81.2.69.160"})
    assert r.status_code == 200, r.data
    raw, sig = signed(CHECKOUT)
    r = client.post("/webhook/checkout", data=raw, headers={
        "X-Shopify-Shop-Domain": SHOP, "X-Shopify-Hmac-Sha256": sig, "X-Shopify-Topic": "checkouts/update"})
    assert r.status_code == 200, r.data
    for body in ORDERS:
        raw, sig = signed(body)
        r = client.post("/webhook/order-created", data=raw, headers={
            "X-Shopify-Shop-Domain": SHOP, "X-Shopify-Hmac-Sha256": sig, "X-Shopify-Topic": "orders/create"})
        assert r.status_code == 200, r.data

# Serialised the way requests serialises json= (json.dumps defaults), so equal
# strings mean byte-identical requests to Meta.
with open(out_path, "w", encoding="utf-8") as f:
    json.dump([{"url": s["url"], "wire": json.dumps(s["body"])} for s in sent], f, indent=1)
print(f"captured {len(sent)} Meta requests")
