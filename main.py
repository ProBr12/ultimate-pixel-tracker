from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import hashlib
import time
import os
import logging
import re

# Setup logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)


def hash_data(value):
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def valid_fbc(value):
    if not value:
        return False
    return value.startswith("fb.1.") and value.count(".") >= 3


def valid_fbp(value):
    if not value:
        return False
    return value.startswith("fb.1.") and value.count(".") >= 3


def normalize_phone(phone):
    digits = re.sub(r'[^\d]', '', phone)
    if digits.startswith('44'):
        return digits
    if digits.startswith('0'):
        return '44' + digits[1:]
    return digits


def send_capi_event(event_name, event_id, fbc=None, fbp=None, fbclid=None,
                    email=None, phone=None, first_name=None, last_name=None,
                    city=None, postcode=None, region=None, country=None,
                    value=None, currency="GBP", source_url=None,
                    client_ip=None, user_agent=None,
                    external_id=None, content_ids=None, content_type=None,
                    contents=None, num_items=None, order_id=None):

    PIXEL_ID = os.environ.get("PIXEL_ID")
    ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")

    user_data = {}
    if email:
        user_data["em"] = [hash_data(email)]
    if phone:
        user_data["ph"] = [hash_data(normalize_phone(phone))]
    if first_name:
        user_data["fn"] = [hash_data(first_name)]
    if last_name:
        user_data["ln"] = [hash_data(last_name)]
    if city:
        user_data["ct"] = [hash_data(city.lower())]
    if postcode:
        user_data["zp"] = [hash_data(postcode.lower().replace(" ", ""))]
    if region:
        user_data["st"] = [hash_data(region.lower())]
    if country:
        user_data["country"] = [hash_data(country.lower())]
    if valid_fbc(fbc):
        user_data["fbc"] = fbc
    if valid_fbp(fbp):
        user_data["fbp"] = fbp
    if fbclid and not fbc:
        user_data["fbc"] = f"fb.1.{int(time.time() * 1000)}.{fbclid}"
    if client_ip:
        user_data["client_ip_address"] = client_ip
    if user_agent:
        user_data["client_user_agent"] = user_agent
    if external_id:
        user_data["external_id"] = [hash_data(str(external_id))]

    event = {
        "event_name": event_name,
        "event_time": int(time.time()),
        "event_id": event_id,
        "action_source": "website",
        "event_source_url": source_url or "https://comfishop.com",
        "user_data": user_data,
    }

    custom_data = {}
    if value is not None:
        custom_data["value"] = value
        custom_data["currency"] = currency
    if content_ids:
        custom_data["content_ids"] = content_ids
    if content_type:
        custom_data["content_type"] = content_type
    if contents:
        custom_data["contents"] = contents
    if num_items is not None:
        custom_data["num_items"] = num_items
    if order_id:
        custom_data["order_id"] = order_id
    if custom_data:
        event["custom_data"] = custom_data

    payload = {
        "data": [event],
        "access_token": ACCESS_TOKEN
    }

    logger.info(
        f"Sending CAPI event: {event_name} | event_id: {event_id} | fbc: {fbc} | fbp: {fbp} | fbclid: {fbclid} | email: {email} | phone: {phone}")

    response = requests.post(
        f"https://graph.facebook.com/v19.0/{PIXEL_ID}/events",
        json=payload
    )

    logger.info(f"Meta CAPI response: {response.status_code} {response.text}")
    return response


# Shopify order webhook — fires Purchase event
@app.route('/webhook/order-created', methods=['POST'])
def order_created():
    order = request.json
    logger.info(f"Webhook received for order: {order.get('id')}")

    note_attributes = order.get("note_attributes", [])
    attrs = {a["name"]: a["value"] for a in note_attributes}
    fbc = attrs.get("_fbc", "")
    fbp = attrs.get("_fbp", "")
    fbclid = attrs.get("_fbclid", "")
    utm_source = attrs.get("_utm_source", "")

    if not fbc and not fbclid:
        landing_site = order.get("landing_site", "") or ""
        raw_query = landing_site.split("?", 1)[1] if "?" in landing_site else ""
        ls_fbclid = None
        for part in raw_query.split("&"):
            eq = part.find("=")
            if eq != -1 and part[:eq] == "fbclid":
                ls_fbclid = part[eq + 1:]
                break
        if ls_fbclid:
            fbclid = ls_fbclid
            logger.info(f"Recovered fbclid from landing_site: {fbclid}")

    logger.info(
        f"Cookies from order: fbc={fbc} | fbp={fbp} | fbclid={fbclid} | utm_source={utm_source}")

    email = order.get("email", "")
    phone = order.get("phone", "")
    order_id = str(order.get("id", ""))
    total_price = order.get("total_price", "0")

    customer = order.get("customer") or {}
    customer_id = customer.get("id")

    line_items = order.get("line_items", [])
    content_ids = [str(item.get("product_id", ""))
                   for item in line_items if item.get("product_id")]
    contents = [{"id": str(item.get("product_id", "")), "quantity": item.get("quantity", 1), "item_price": float(
        item.get("price", 0))} for item in line_items if item.get("product_id")]
    num_items = sum(item.get("quantity", 1) for item in line_items)

    billing = order.get("billing_address") or order.get(
        "shipping_address") or {}
    first_name = billing.get("first_name", "")
    last_name = billing.get("last_name", "")
    city = billing.get("city", "")
    postcode = billing.get("zip", "")
    region = billing.get("province", "")
    country = billing.get("country_code", "")

    # Extract IP and User Agent from Shopify's client_details
    client_details = order.get("client_details") or {}
    browser_ip = order.get(
        "browser_ip") or client_details.get("browser_ip", "")
    browser_ua = client_details.get("user_agent", "")

    logger.info(
        f"Customer: {email} | {phone} | {city} | {postcode} | {country} | IP: {browser_ip} | UA: {browser_ua[:80]}")

    send_capi_event(
        event_name="Purchase",
        event_id=f"purchase_{order_id}",
        fbc=fbc,
        fbp=fbp,
        fbclid=fbclid,
        email=email,
        phone=phone,
        first_name=first_name,
        last_name=last_name,
        city=city,
        postcode=postcode,
        region=region,
        country=country,
        value=float(total_price),
        currency="GBP",
        source_url="https://comfishop.com",
        client_ip=browser_ip,
        user_agent=browser_ua,
        external_id=customer_id,
        content_ids=content_ids,
        content_type="product",
        contents=contents,
        num_items=num_items,
        order_id=order_id
    )

    return jsonify({'ok': True}), 200


# Browser event relay
@app.route('/track', methods=['POST'])
def track_event():
    body = request.json

    # Get IP from request (Railway uses X-Forwarded-For behind proxy)
    client_ip = request.headers.get(
        "X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr
    user_agent = request.headers.get("User-Agent", "")

    logger.info(
        f"Browser event received: {body.get('event_name')} | fbc: {body.get('fbc')} | fbp: {body.get('fbp')} | fbclid: {body.get('fbclid')}")

    send_capi_event(
        event_name=body.get("event_name"),
        event_id=body.get("event_id", f"evt_{int(time.time())}"),
        fbc=body.get("fbc"),
        fbp=body.get("fbp"),
        fbclid=body.get("fbclid"),
        email=body.get("email"),
        phone=body.get("phone"),
        value=body.get("value"),
        currency=body.get("currency", "GBP"),
        source_url=body.get("source_url"),
        client_ip=client_ip,
        user_agent=user_agent
    )

    return jsonify({'ok': True}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
