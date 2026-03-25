from flask import Flask, request, jsonify
import requests
app = Flask(__name__)


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

    return jsonify({'ok': True}), 200
