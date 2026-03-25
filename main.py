from flask import Flask, request, jsonify
import requests
import hashlib
import time
import os

app = Flask(__name__)


def hash_data(value):
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


@app.route('/webhook/order-created', methods=['POST'])
def order_created():
    order = request.json

    note_attributes = order.get("note_attributes", [])
    attrs = {a["name"]: a["value"] for a in note_attributes}
    fbc = attrs.get("_fbc", "")
    fbclid = attrs.get("_fbclid", "")

    email = order.get("email", "")
    phone = order.get("phone", "")
    total_price = order.get("total_price", "0")
    order_id = str(order.get("id", ""))

    hashed_email = hash_data(email) if email else ""
    hashed_phone = hash_data(phone) if phone else ""

    PIXEL_ID = os.environ.get("PIXEL_ID")
    ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")

    payload = {
        "data": [{
            "event_name": "Purchase",
            "event_time": int(time.time()),
            "event_id": f"purchase_{order_id}",
            "action_source": "website",
            "user_data": {
                "em": [hashed_email],
                "ph": [hashed_phone],
                "fbc": fbc,
                "fbp": ""
            },
            "custom_data": {
                "value": total_price,
                "currency": "GBP"
            }
        }],
        "test_event_code": "TEST36671",
        "access_token": ACCESS_TOKEN
    }

    requests.post(
        f"https://graph.facebook.com/v19.0/{PIXEL_ID}/events",
        json=payload
    )

    return jsonify({'ok': True}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
