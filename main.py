from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import hashlib
import time
import os
import logging

# Setup logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)


def hash_data(value):
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


def send_capi_event(event_name, event_id, fbc=None, fbclid=None,
                    email=None, phone=None, first_name=None, last_name=None,
                    city=None, postcode=None, region=None, country=None,
                    value=None, currency="GBP", source_url=None):

    PIXEL_ID = os.environ.get("PIXEL_ID")
    ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")

    user_data = {}
    if email:
        user_data["em"] = [hash_data(email)]
    if phone:
        user_data["ph"] = [hash_data(phone)]
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
    if fbc:
        user_data["fbc"] = fbc
    if fbclid:
        user_data["fbc"] = f"fb.1.{int(time.time() * 1000)}.{fbclid}" if not fbc else fbc

    event = {
        "event_name": event_name,
        "event_time": int(time.time()),
        "event_id": event_id,
        "action_source": "website",
        "event_source_url": source_url or "https://comfishop.com",
        "user_data": user_data,
    }

    if value is not None:
        event["custom_data"] = {
            "value": value,
            "currency": currency
        }

    payload = {
        "data": [event],
        "access_token": ACCESS_TOKEN
    }

    logger.info(
        f"Sending CAPI event: {event_name} | event_id: {event_id} | fbc: {fbc} | fbclid: {fbclid} | email: {email} | phone: {phone}")

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
    fbclid = attrs.get("_fbclid", "")

    logger.info(f"Cookies from order: fbc={fbc} | fbclid={fbclid}")

    email = order.get("email", "")
    phone = order.get("phone", "")
    order_id = str(order.get("id", ""))
    total_price = order.get("total_price", "0")

    billing = order.get("billing_address") or order.get(
        "shipping_address") or {}
    first_name = billing.get("first_name", "")
    last_name = billing.get("last_name", "")
    city = billing.get("city", "")
    postcode = billing.get("zip", "")
    region = billing.get("province", "")
    country = billing.get("country_code", "")

    logger.info(
        f"Customer: {email} | {phone} | {city} | {postcode} | {country}")

    send_capi_event(
        event_name="Purchase",
        event_id=f"purchase_{order_id}",
        fbc=fbc,
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
        source_url="https://comfishop.com"
    )

    return jsonify({'ok': True}), 200


# Browser event relay
@app.route('/track', methods=['POST'])
def track_event():
    body = request.json
    logger.info(
        f"Browser event received: {body.get('event_name')} | fbc: {body.get('fbc')} | fbclid: {body.get('fbclid')}")

    send_capi_event(
        event_name=body.get("event_name"),
        event_id=body.get("event_id", f"evt_{int(time.time())}"),
        fbc=body.get("fbc"),
        fbclid=body.get("fbclid"),
        email=body.get("email"),
        phone=body.get("phone"),
        value=body.get("value"),
        currency=body.get("currency", "GBP"),
        source_url=body.get("source_url")
    )

    return jsonify({'ok': True}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
