from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import hashlib
import hmac
import base64
import time
import os
import logging
import re
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

# Setup logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)


# ============ DATABASE LAYER ============
import psycopg2
from urllib.parse import urlparse, parse_qs

DATABASE_URL = os.environ.get("DATABASE_URL")


def init_db():
    if not DATABASE_URL:
        logger.warning("DATABASE_URL not set - events will NOT be saved to the database.")
        return
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                ts TIMESTAMPTZ DEFAULT now(),
                event_name TEXT,
                vid TEXT,
                url TEXT,
                path TEXT,
                referrer TEXT,
                ad_id TEXT,
                adset_id TEXT,
                campaign_id TEXT,
                utm_source TEXT,
                value NUMERIC,
                order_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);
            CREATE INDEX IF NOT EXISTS idx_events_vid ON events (vid);
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Database ready.")
    except Exception as e:
        logger.error(f"DB init failed: {e}")


def save_event_db(event_name, vid=None, url=None, referrer=None, value=None, order_id=None):
    """Save every event to our own database. Never allowed to break tracking:
    if the DB is down, we log the error and Meta still gets the event."""
    if not DATABASE_URL:
        return
    try:
        parsed = urlparse(url or "")
        q = parse_qs(parsed.query)
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO events (event_name, vid, url, path, referrer, ad_id, adset_id, campaign_id, utm_source, value, order_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (event_name, vid, url, parsed.path or None, referrer,
             q.get("ad_id", [None])[0],
             q.get("utm_term", [None])[0],
             q.get("campaign_id", [None])[0],
             q.get("utm_source", [None])[0],
             value, order_id))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"DB write failed: {e}")


init_db()



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


def normalize_phone(phone, country="GB"):
    """US FIX: country-aware phone normalization.
    Meta hashes phones WITH country code. US/CA 10-digit numbers get a leading 1.
    UK behaviour unchanged."""
    digits = re.sub(r'[^\d]', '', phone or "")
    if not digits:
        return digits
    country = (country or "GB").upper()
    if country in ("US", "CA"):
        if len(digits) == 10:
            return "1" + digits
        return digits
    # UK default
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
                    contents=None, num_items=None, order_id=None,
                    pixel_id=None, access_token=None):

    PIXEL_ID = pixel_id if pixel_id is not None else os.environ.get("PIXEL_ID")
    ACCESS_TOKEN = access_token if access_token is not None else os.environ.get("ACCESS_TOKEN")

    country_code = (country or "").upper()

    user_data = {}
    if email:
        user_data["em"] = [hash_data(email)]
    # phone may be one number or a list (billing, shipping, customer record).
    phones = phone if isinstance(phone, (list, tuple)) else [phone]
    phone_hashes = []
    for p in phones:
        # US FIX: pass country so normalization matches Meta's expected format
        digits = normalize_phone(p, country_code or "GB")
        if len(digits) >= 10:
            h = hash_data(digits)
            if h not in phone_hashes:
                phone_hashes.append(h)
    if phone_hashes:
        user_data["ph"] = phone_hashes
    if first_name:
        user_data["fn"] = [hash_data(first_name)]
    if last_name:
        user_data["ln"] = [hash_data(last_name)]
    if city:
        # US FIX (helps UK too): Meta's ct format is lowercase letters only, no spaces
        user_data["ct"] = [hash_data(re.sub(r'[^a-z]', '', city.lower()))]
    if postcode:
        # US FIX: strip spaces/dashes; US ZIPs hashed as first 5 digits only
        zp = postcode.lower().replace(" ", "").replace("-", "")
        if country_code == "US":
            zp = zp[:5]
        user_data["zp"] = [hash_data(zp)]
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
    # external_id may be one ID or a list (visitor ID and Shopify customer ID).
    ext_ids = external_id if isinstance(external_id, (list, tuple)) else [external_id]
    ext_hashes = []
    for e in ext_ids:
        if e:
            h = hash_data(str(e))
            if h not in ext_hashes:
                ext_hashes.append(h)
    if ext_hashes:
        user_data["external_id"] = ext_hashes

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

    # Log which keys were matched, never the customer's actual details.
    logger.info(
        f"Sending CAPI event: {event_name} | event_id: {event_id} | url: {source_url} | "
        f"keys: {','.join(sorted(user_data.keys()))}")

    response = requests.post(
        f"https://graph.facebook.com/v19.0/{PIXEL_ID}/events",
        json=payload,
        timeout=10
    )

    logger.info(f"Meta CAPI response: {response.status_code} {response.text}")
    return response


def verify_shopify_webhook(raw_body, hmac_header):
    """Check that this webhook genuinely came from Shopify.
    Shopify signs every webhook with a secret only you and Shopify know.
    If SHOPIFY_WEBHOOK_SECRET is not set yet, we allow the request through
    (so nothing breaks before you configure it) but log a loud warning."""
    secret = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")
    if not secret:
        logger.warning(
            "SHOPIFY_WEBHOOK_SECRET not set - webhook accepted WITHOUT verification. Set it in Railway ASAP.")
        return True
    if not hmac_header:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode()
    return hmac.compare_digest(computed, hmac_header)


# Shopify order webhook — fires Purchase event
@app.route('/webhook/order-created', methods=['POST'])
def order_created():
    raw_body = request.get_data()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    if not verify_shopify_webhook(raw_body, hmac_header):
        logger.warning(
            f"Webhook REJECTED - invalid signature. Source IP: {request.headers.get('X-Forwarded-For', request.remote_addr)}")
        return jsonify({'error': 'unauthorized'}), 401

    order = request.json
    logger.info(f"Webhook received for order: {order.get('id')}")

    note_attributes = order.get("note_attributes", [])
    attrs = {a["name"]: a["value"] for a in note_attributes}
    fbc = attrs.get("_fbc", "")
    fbp = attrs.get("_fbp", "")
    fbclid = attrs.get("_fbclid", "")
    utm_source = attrs.get("_utm_source", "")
    vid = attrs.get("_comfi_vid", "")
    order_id = str(order.get("id", ""))

    # Express checkout / Buy it now skip the cart, so the cart attributes are
    # missing. Fill the gaps from what the checkout pixel sent (/meta/context).
    context = {}
    if not (vid and fbp and fbc):
        context = meta_context_lookup(order.get("checkout_token"))
        vid = vid or context.get("vid") or ""
        fbp = fbp or context.get("fbp") or ""
        fbc = fbc or context.get("fbc") or ""

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
        f"Identifiers for order {order_id}: vid={bool(vid)} fbc={bool(fbc)} fbp={bool(fbp)} "
        f"fbclid={bool(fbclid)} from_context={bool(context)} utm_source={utm_source}")

    email = order.get("email", "")
    total_price = order.get("total_price", "0")

    customer = order.get("customer") or {}
    customer_id = customer.get("id")
    shipping = order.get("shipping_address") or {}
    billing_raw = order.get("billing_address") or {}

    # Shopify almost never fills order.phone (0 of the last 50 orders); the
    # number sits on the billing address, shipping address or customer record.
    # Billing first: that is the buyer, the shipping phone may be a relative's.
    phones = []
    for p in (order.get("phone"), billing_raw.get("phone"), customer.get("phone")):
        if p and p not in phones:
            phones.append(p)
    # Shipping phone only as a fallback: the recipient may not be the buyer.
    if not phones and shipping.get("phone"):
        phones.append(shipping.get("phone"))

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
    # US FIX: province_code gives "CA" not "California" — the format Meta actually matches on
    region = billing.get("province_code") or billing.get("province", "")
    country = billing.get("country_code", "")

    # Extract IP and User Agent from Shopify's client_details
    client_details = order.get("client_details") or {}
    browser_ip = order.get(
        "browser_ip") or client_details.get("browser_ip", "")
    browser_ua = client_details.get("user_agent") or context.get("user_agent", "")

    save_event_db(
        event_name="Purchase",
        vid=vid,
        url=order.get("landing_site"),
        value=float(total_price),
        order_id=order_id)

    send_capi_event(
        event_name="Purchase",
        event_id=f"purchase_{order_id}",
        fbc=fbc,
        fbp=fbp,
        fbclid=fbclid,
        email=email,
        phone=phones,
        first_name=first_name,
        last_name=last_name,
        city=city,
        postcode=postcode,
        region=region,
        country=country,
        value=float(total_price),
        # US FIX: total_price is always shop currency — pair it with the order's own currency field
        currency=order.get("currency", "GBP"),
        source_url="https://comfishop.com",
        client_ip=browser_ip,
        user_agent=browser_ua,
        # Both IDs: the browser sends the visitor ID, Meta can also join on the
        # customer ID across repeat orders.
        external_id=[vid, customer_id],
        content_ids=content_ids,
        content_type="product",
        contents=contents,
        num_items=num_items,
        order_id=order_id
    )

    return jsonify({'ok': True}), 200


# ============ META CHECKOUT CONTEXT ============
# The checkout pixel posts the shopper's real _fbp/_fbc cookies, visitor ID and
# user agent here, keyed by checkout token. order_created uses them when the
# order has no cart attributes (express checkout skips the cart).

META_CONTEXT_PATTERNS = {
    "checkout_token": r"[A-Za-z0-9_-]{8,128}",
    "order_id": r"[0-9]{1,20}",
    "vid": r"v_[a-z0-9]{6,40}",
    "fbp": r"fb\.[0-9]\.[0-9]{10,16}\.[0-9]{1,30}",
    "fbc": r"fb\.[0-9]\.[0-9]{10,16}\.[A-Za-z0-9_-]{10,600}",
}


def init_meta_context_db():
    if not DATABASE_URL:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        with conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS meta_context (
                    id BIGSERIAL PRIMARY KEY,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    checkout_token TEXT, order_id TEXT, vid TEXT,
                    fbp TEXT, fbc TEXT, user_agent TEXT
                );
                CREATE INDEX IF NOT EXISTS meta_context_checkout
                    ON meta_context (checkout_token, received_at DESC);
                CREATE INDEX IF NOT EXISTS meta_context_order
                    ON meta_context (order_id, received_at DESC);
            """)
        conn.close()
    except Exception as e:
        logger.error(f"meta_context init failed: {e}")


init_meta_context_db()


@app.route('/meta/context', methods=['POST'])
def meta_context_received():
    if not DATABASE_URL:
        return jsonify(error="storage_unavailable"), 503
    raw = request.get_data()
    if len(raw) > 4096:
        return jsonify(error="too_large"), 413
    try:
        body = json.loads(raw)  # sent as text/plain to avoid a CORS preflight
    except (ValueError, TypeError):
        return jsonify(error="invalid_json"), 400
    if not isinstance(body, dict):
        return jsonify(error="invalid_body"), 400

    clean = {}
    for key, pattern in META_CONTEXT_PATTERNS.items():
        value = body.get(key)
        if isinstance(value, str) and re.fullmatch(pattern, value):
            clean[key] = value
    if not clean.get("checkout_token"):
        return jsonify(error="missing_checkout_token"), 400
    ua = body.get("user_agent")
    clean["user_agent"] = ua[:512] if isinstance(ua, str) else None

    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        with conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO meta_context (checkout_token, order_id, vid, fbp, fbc, user_agent)
                   VALUES (%s,%s,%s,%s,%s,%s)""",
                (clean.get("checkout_token"), clean.get("order_id"), clean.get("vid"),
                 clean.get("fbp"), clean.get("fbc"), clean.get("user_agent")))
        conn.close()
    except Exception as e:
        logger.error(f"meta_context write failed: {type(e).__name__}")
        return jsonify(error="storage_unavailable"), 503
    return jsonify(ok=True), 200


def meta_context_lookup(checkout_token):
    """Newest non-empty value of each identifier for this checkout, from the
    last 7 days. Matches on the checkout token only: it is long and random,
    unlike order IDs, so nobody can attach data to someone else's order.
    Returns {} on any problem: tracking must never fail on it."""
    if not DATABASE_URL or not checkout_token:
        return {}
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        with conn, conn.cursor() as cur:
            cur.execute(
                """SELECT vid, fbp, fbc, user_agent FROM meta_context
                   WHERE checkout_token = %s
                     AND received_at > now() - interval '7 days'
                   ORDER BY received_at DESC LIMIT 50""",
                (checkout_token,))
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"meta_context lookup failed: {type(e).__name__}")
        return {}
    result = {}
    for row in rows:
        for key, value in zip(("vid", "fbp", "fbc", "user_agent"), row):
            if value and key not in result:
                result[key] = value
    return result


# WooCommerce requests use base64-encoded HMAC-SHA256, matching the plugin.
def verify_comfi_signature(raw_body, signature, secret):
    if not secret or not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest), signature.encode("utf-8"))


@app.route('/webhook/woo-order', methods=['POST'])
def woo_order_created():
    if not verify_comfi_signature(
            request.get_data(), request.headers.get("X-Comfi-Webhook-Signature", ""),
            os.environ.get("WOO_WEBHOOK_SECRET", "")):
        return jsonify({'error': 'unauthorized'}), 401

    pixel_id = os.environ.get("WOO_PIXEL_ID")
    access_token = os.environ.get("WOO_ACCESS_TOKEN")
    if not pixel_id or not access_token:
        return jsonify({'error': 'WooCommerce credentials not configured'}), 503

    order = request.get_json()
    if not isinstance(order, dict) or not order.get("event_id"):
        return jsonify({'error': 'event_id required'}), 400
    billing = order.get("billing") or {}
    attrs = order.get("attribution") or {}
    items = order.get("line_items") or []
    order_id = str(order.get("id", ""))
    vid = attrs.get("vid")
    value = float(order.get("total", 0))
    source_url = order.get("landing_site") or "https://comfistore.com"

    response = send_capi_event(
        event_name="Purchase",
        event_id=order["event_id"],
        fbc=attrs.get("fbc"), fbp=attrs.get("fbp"), fbclid=attrs.get("fbclid"),
        email=billing.get("email"), phone=billing.get("phone"),
        first_name=billing.get("first_name"), last_name=billing.get("last_name"),
        city=billing.get("city"), postcode=billing.get("postcode"),
        region=billing.get("state"), country=billing.get("country"),
        value=value, currency=order.get("currency", "GBP"),
        source_url=source_url,
        client_ip=order.get("customer_ip_address"),
        user_agent=order.get("customer_user_agent"),
        external_id=vid or order.get("customer_id"),
        content_ids=[str(item["product_id"]) for item in items if item.get("product_id")],
        content_type="product",
        contents=[{"id": str(item["product_id"]), "quantity": item.get("quantity", 1),
                   "item_price": float(item.get("price", 0))}
                  for item in items if item.get("product_id")],
        num_items=sum(item.get("quantity", 1) for item in items),
        order_id=order_id, pixel_id=pixel_id, access_token=access_token)
    if not 200 <= response.status_code < 300:
        return jsonify({'error': 'Meta delivery failed'}), 502

    save_event_db("Purchase", vid=vid, url=source_url, value=value, order_id=order_id)
    return jsonify({'ok': True}), 200


# Browser event relay
@app.route('/track', methods=['POST'])
def track_event():
    woo_options = {}
    if "X-Comfi-Signature" in request.headers:
        if not verify_comfi_signature(
                request.get_data(), request.headers.get("X-Comfi-Signature", ""),
                os.environ.get("TRACK_SHARED_SECRET", "")):
            return jsonify({'error': 'unauthorized'}), 401
        pixel_id = os.environ.get("WOO_PIXEL_ID")
        access_token = os.environ.get("WOO_ACCESS_TOKEN")
        if not pixel_id or not access_token:
            return jsonify({'error': 'WooCommerce credentials not configured'}), 503
        woo_options = {"pixel_id": pixel_id, "access_token": access_token}

    body = request.json

    # Get IP from request (Railway uses X-Forwarded-For behind proxy)
    client_ip = request.headers.get(
        "X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr
    user_agent = request.headers.get("User-Agent", "")

    if woo_options:
        client_ip = body.get("client_ip") or client_ip
        user_agent = body.get("user_agent") or user_agent
        woo_options.update({key: body.get(key) for key in (
            "first_name", "last_name", "city", "postcode", "region", "country",
            "content_ids", "content_type", "contents", "num_items")})

    vid = body.get("vid")

    logger.info(
        f"Browser event received: {body.get('event_name')} | url: {body.get('source_url')} | ref: {body.get('referrer')} | vid: {vid} | fbc: {body.get('fbc')} | fbp: {body.get('fbp')} | fbclid: {body.get('fbclid')}")

    save_event_db(
        event_name=body.get("event_name"),
        vid=vid,
        url=body.get("source_url"),
        referrer=body.get("referrer"),
        value=body.get("value"))

    response = send_capi_event(
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
        user_agent=user_agent,
        external_id=vid,
        **woo_options
    )

    if woo_options and not 200 <= response.status_code < 300:
        return jsonify({'error': 'Meta delivery failed'}), 502

    return jsonify({'ok': True}), 200




# ============ DASHBOARD ============

SESSION_SQL = """
WITH ordered AS (
    SELECT vid, ts, event_name, path, ad_id, value,
           CASE WHEN lag(ts) OVER (PARTITION BY vid ORDER BY ts) IS NULL
                  OR ts - lag(ts) OVER (PARTITION BY vid ORDER BY ts) > interval '30 minutes'
                THEN 1 ELSE 0 END AS is_new
    FROM events
    WHERE ts >= now() - (%s || ' days')::interval
      AND vid IS NOT NULL
),
numbered AS (
    SELECT *, sum(is_new) OVER (PARTITION BY vid ORDER BY ts) AS sess_no
    FROM ordered
),
sessions AS (
    SELECT vid, sess_no,
           min(ts) AS started,
           (array_agg(ad_id ORDER BY ts) FILTER (WHERE ad_id IS NOT NULL))[1] AS ad_id,
           (array_agg(path ORDER BY ts))[1] AS landing,
           bool_or(event_name = 'ViewContent' OR path LIKE '/products/%%') AS pdp,
           bool_or(event_name = 'AddToCart') AS atc,
           bool_or(event_name = 'InitiateCheckout') AS ic,
           bool_or(event_name = 'Purchase') AS purchase,
           coalesce(max(value) FILTER (WHERE event_name = 'Purchase'), 0) AS revenue
    FROM numbered
    GROUP BY vid, sess_no
)
"""


def q(cur, sql, params):
    cur.execute(SESSION_SQL + sql, params)
    return cur.fetchall()


def pct(a, b):
    return f"{a / b * 100:.0f}%" if b else "-"


def render_rows(rows, headers):
    th = "".join(f"<th>{h}</th>" for h in headers)
    trs = ""
    for r in rows:
        tds = "".join(f"<td>{c}</td>" for c in r)
        trs += f"<tr>{tds}</tr>"
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


@app.route('/dashboard')
def dashboard():
    if request.args.get("key") != os.environ.get("DASH_PASSWORD"):
        return "Unauthorized", 401
    if not DATABASE_URL:
        return "DATABASE_URL not configured", 500

    days = max(1, min(int(request.args.get("days", 7)), 90))
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    total = q(cur, """
        SELECT count(*),
               count(*) FILTER (WHERE pdp),
               count(*) FILTER (WHERE atc),
               count(*) FILTER (WHERE ic),
               count(*) FILTER (WHERE purchase),
               coalesce(sum(revenue), 0)
        FROM sessions""", (str(days),))[0]
    ses, pdp, atc, ic, pur, rev = total

    per_ad = q(cur, """
        SELECT coalesce(ad_id, '(no ad id)'),
               count(*),
               count(*) FILTER (WHERE pdp),
               count(*) FILTER (WHERE atc),
               count(*) FILTER (WHERE ic),
               count(*) FILTER (WHERE purchase),
               coalesce(sum(revenue), 0)
        FROM sessions GROUP BY 1 ORDER BY 2 DESC LIMIT 25""", (str(days),))

    per_landing = q(cur, """
        SELECT coalesce(landing, '?'),
               count(*),
               count(*) FILTER (WHERE pdp),
               count(*) FILTER (WHERE purchase)
        FROM sessions GROUP BY 1 ORDER BY 2 DESC LIMIT 15""", (str(days),))

    daily = q(cur, """
        SELECT to_char(date_trunc('day', started), 'DD Mon'),
               count(*),
               count(*) FILTER (WHERE pdp),
               count(*) FILTER (WHERE atc),
               count(*) FILTER (WHERE purchase),
               coalesce(sum(revenue), 0)
        FROM sessions GROUP BY date_trunc('day', started)
        ORDER BY date_trunc('day', started) DESC""", (str(days),))

    cur.close()
    conn.close()

    funnel_html = render_rows([[
        ses, f"{pdp} ({pct(pdp, ses)})", f"{atc} ({pct(atc, pdp)} of PDP)",
        f"{ic} ({pct(ic, atc)} of ATC)", pur, f"&pound;{rev:.0f}",
        pct(pur, ses)
    ]], ["Sessions", "Product page", "Add to cart", "Checkout", "Purchases", "Revenue", "CVR"])

    ad_rows = [[a, s, f"{p} ({pct(p, s)})", c, i, pu, f"&pound;{r:.0f}"]
               for a, s, p, c, i, pu, r in per_ad]
    landing_rows = [[l, s, f"{p} ({pct(p, s)})", pu] for l, s, p, pu in per_landing]
    daily_rows = [[d, s, p, c, pu, f"&pound;{r:.0f}"] for d, s, p, c, pu, r in daily]

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Comfi Dashboard</title>
<style>
body {{ font-family: -apple-system, system-ui, sans-serif; margin: 2rem auto; max-width: 960px; padding: 0 1rem; color: #1a1a1a; }}
h1 {{ font-size: 22px; }} h2 {{ font-size: 16px; margin-top: 2.2rem; color: #444; }}
table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #e5e5e5; }}
th {{ color: #777; font-weight: 500; font-size: 12px; text-transform: uppercase; }}
.range a {{ margin-right: 10px; color: #2a78d6; text-decoration: none; }}
</style></head><body>
<h1>Comfi - true funnel (last {days} days)</h1>
<p class="range">Range:
<a href="?key={request.args.get('key')}&days=1">today</a>
<a href="?key={request.args.get('key')}&days=7">7d</a>
<a href="?key={request.args.get('key')}&days=30">30d</a>
<a href="?key={request.args.get('key')}&days=90">90d</a></p>
<h2>Funnel</h2>{funnel_html}
<h2>Per ad</h2>{render_rows(ad_rows, ["Ad ID", "Sessions", "Product page", "ATC", "Checkout", "Purchases", "Revenue"])}
<h2>Per landing page</h2>{render_rows(landing_rows, ["Landing", "Sessions", "Product page", "Purchases"])}
<h2>Daily</h2>{render_rows(daily_rows, ["Day", "Sessions", "PDP", "ATC", "Purchases", "Revenue"])}
</body></html>"""


# ============ ISOLATED GOOGLE PURCHASE IMPORT ============
# GOOGLE_TRACKING_ENABLED enables storage/worker; GOOGLE_UPLOADS_ENABLED permits Google requests.
# Neither changes the Meta or WooCommerce routes above. Credentials never enter browser code.
GOOGLE_CLICK_KEYS = ("gclid", "gbraid", "wbraid")
GOOGLE_WINDOW_MS = 30 * 86400 * 1000


def google_log(event, **fields):
    logger.info(json.dumps({"channel": "google", "event": event, **fields}, default=str))


@contextmanager
def google_db():
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_google_db():
    with google_db() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS google_context (
                id BIGSERIAL PRIMARY KEY, received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                vid TEXT, client_id TEXT, checkout_token TEXT, order_id TEXT,
                context JSONB NOT NULL
            );
            CREATE INDEX IF NOT EXISTS google_context_vid ON google_context (vid, received_at DESC);
            CREATE INDEX IF NOT EXISTS google_context_client ON google_context (client_id, received_at DESC);
            CREATE INDEX IF NOT EXISTS google_context_checkout ON google_context (checkout_token, received_at DESC);
            CREATE TABLE IF NOT EXISTS google_purchase_delivery (
                id BIGSERIAL PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                shop TEXT NOT NULL, order_id TEXT NOT NULL, action_id TEXT NOT NULL,
                snapshot JSONB NOT NULL, payload JSONB, request_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt TIMESTAMPTZ NOT NULL DEFAULT now(), lease_until TIMESTAMPTZ,
                diagnostics JSONB NOT NULL DEFAULT '{}',
                UNIQUE (shop, order_id, action_id)
            );
        """)


def google_order_id(value):
    match = re.fullmatch(r"(?:gid://shopify/Order/)?([0-9]+)", str(value or ""))
    if not match:
        raise ValueError("invalid_order_id")
    return match[1]


def google_text(value, maximum=256):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError("invalid_context_field")
    return value or None


def google_click(state, at_ms):
    """Return a whole eligible click record, never mix identifiers from different visits."""
    if not isinstance(state, dict):
        return {}
    try:
        ts = int(state.get("captured_at", 0))
    except (ValueError, TypeError):
        return {}
    if not 0 <= at_ms - ts <= GOOGLE_WINDOW_MS:
        return {}
    click = {key: state.get(key) or "" for key in GOOGLE_CLICK_KEYS}
    if not any(click.values()) or any(not isinstance(v, str) or
            (v and not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", v)) for v in click.values()):
        return {}
    return {**click, "captured_at": ts}


def google_context_body(body):
    if not isinstance(body, dict) or body.get("source") not in ("theme", "pixel"):
        raise ValueError("invalid_context")
    privacy = body.get("consent") or {}
    if not isinstance(privacy, dict) or any(type(privacy.get(key)) is not bool for key in ("marketingAllowed", "saleOfDataAllowed")):
        raise ValueError("missing_consent_state")
    result = {key: google_text(body.get(key)) for key in ("vid", "client_id", "checkout_token")}
    if not result["vid"] and not result["client_id"]:
        raise ValueError("missing_visitor")
    result["order_id"] = google_order_id(body["order_id"]) if body.get("order_id") else None
    result["source"] = body["source"]
    result["consent"] = {key: privacy[key] for key in ("marketingAllowed", "saleOfDataAllowed")}
    result["consent_ts"] = int(time.time() * 1000)  # Server receipt, not a client-controlled ordering key.
    state = body.get("attribution") or {}
    click = google_click(state, result["consent_ts"])
    result["attribution"] = {}
    if all(result["consent"].values()) and click:
        for key in ("landing_url", "referrer"):
            url = urlparse(google_text(state.get(key), 2048) or "")
            click[key] = f"{url.scheme}://{url.netloc}{url.path}" if url.scheme in ("http", "https") else ""
        click["utm_source"] = google_text(state.get("utm_source"), 100) or ""
        result["attribution"] = click
    return result


@app.route('/google/context', methods=['POST'])
def google_context_received():
    if os.environ.get("GOOGLE_TRACKING_ENABLED") != "true" or not DATABASE_URL:
        return jsonify(error="google_tracking_disabled"), 503
    if len(request.get_data()) > 8192:
        return jsonify(error="context_too_large"), 413
    try:
        context = google_context_body(request.get_json(silent=True))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    try:
        with google_db() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO google_context (vid, client_id, checkout_token, order_id, context)
                           VALUES (%s,%s,%s,%s,%s::jsonb)""",
                        (context["vid"], context["client_id"], context["checkout_token"],
                         context["order_id"], json.dumps(context)))
        google_log("context_saved", source=context["source"], vid=context["vid"],
                   order_id=context["order_id"], click_types=[k for k in GOOGLE_CLICK_KEYS
                   if context["attribution"].get(k)], consent=context["consent"])
        return jsonify(ok=True), 200
    except Exception as exc:
        google_log("context_storage_failed", error_type=type(exc).__name__)
        return jsonify(error="storage_unavailable"), 503


def google_user_data(order):
    identifiers = []
    email = re.sub(r"\s+", "", str(order.get("email") or "")).lower()
    if re.fullmatch(r"[^@]+@[^@]+\.[^@]+", email):
        local, domain = email.rsplit("@", 1)
        if domain in ("gmail.com", "googlemail.com"):
            local = local.split("+", 1)[0].replace(".", "")
        identifiers.append({"emailAddress": hash_data(local + "@" + domain)})
    billing = order.get("billing_address") or order.get("shipping_address") or {}
    phone = re.sub(r"[\s().-]", "", str(order.get("phone") or billing.get("phone") or ""))
    # Only hash an unambiguous E.164 number; do not reuse Meta's phone normalization.
    if re.fullmatch(r"\+[1-9][0-9]{7,14}", phone):
        identifiers.append({"phoneNumber": hash_data(phone)})
    return {"userIdentifiers": identifiers} if identifiers else {}


def google_paid_snapshot(order, triggered_at):
    order_id = google_order_id(order.get("id"))
    price_set = order.get("total_price_set") or {}
    money = price_set.get("presentment_money") or price_set.get("shop_money") or {
        "amount": order.get("total_price"), "currency_code": order.get("currency")}
    try:
        amount = Decimal(str(money["amount"]))
        if not amount.is_finite() or amount <= 0:
            raise ValueError("invalid_value")
    except (InvalidOperation, KeyError, TypeError):
        raise ValueError("invalid_value")
    currency = money.get("currency_code", "")
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("invalid_currency")
    paid_at = datetime.fromisoformat(triggered_at.replace("Z", "+00:00")) if triggered_at else datetime.now(timezone.utc)
    if paid_at.tzinfo is None or paid_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise ValueError("invalid_paid_time")
    attrs = {a["name"]: a.get("value", "") for a in order.get("note_attributes", []) if "name" in a}
    return {"order_id": order_id, "event_time": paid_at.isoformat(), "value": str(amount), "currency": currency,
            "vid": attrs.get("_comfi_vid") or None, "checkout_token": order.get("checkout_token"),
            "utm_source": attrs.get("_utm_source", ""),
            "click": {**{k: attrs.get("_" + k, "") for k in GOOGLE_CLICK_KEYS},
                      "captured_at": attrs.get("_google_click_ts", "")},
            "user_data": google_user_data(order)}


@app.route('/webhook/google-order-paid', methods=['POST'])
def google_order_paid():
    secret = os.environ.get("SHOPIFY_WEBHOOK_SECRET")
    shop = os.environ.get("GOOGLE_SHOP_DOMAIN", "")
    customer = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "")
    action = os.environ.get("GOOGLE_ADS_CONVERSION_ACTION_ID", "")
    login = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or customer
    if (os.environ.get("GOOGLE_TRACKING_ENABLED") != "true" or not DATABASE_URL or not secret or not shop
            or not re.fullmatch(r"[0-9]{10}", login)
            or not re.fullmatch(r"[0-9]{10}", customer) or not re.fullmatch(r"[0-9]+", action)):
        return jsonify(error="google_tracking_not_configured"), 503
    if (not verify_shopify_webhook(request.get_data(), request.headers.get("X-Shopify-Hmac-Sha256", ""))
            or request.headers.get("X-Shopify-Shop-Domain") != shop
            or request.headers.get("X-Shopify-Topic") != "orders/paid"):
        return jsonify(error="unauthorized"), 401
    order = request.get_json(silent=True)
    if not isinstance(order, dict):
        return jsonify(error="invalid_order"), 400
    if order.get("test") or order.get("cancelled_at") or order.get("financial_status") != "paid":
        google_log("order_skipped", reason="not_eligible_paid_order")
        return jsonify(ok=True, skipped=True), 200
    try:
        snapshot = google_paid_snapshot(order, request.headers.get("X-Shopify-Triggered-At", ""))
        snapshot["customer_id"] = customer
        snapshot["login_id"] = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or customer
        with google_db() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO google_purchase_delivery (shop, order_id, action_id, snapshot, next_attempt)
                           VALUES (%s,%s,%s,%s::jsonb,now() + interval '15 seconds')
                           ON CONFLICT (shop, order_id, action_id) DO NOTHING""",
                        (shop, snapshot["order_id"], action, json.dumps(snapshot)))
            inserted = bool(cur.rowcount)
        google_log("paid_order_saved", order_id=snapshot["order_id"], inserted=inserted,
                   value=snapshot["value"], currency=snapshot["currency"])
        return jsonify(ok=True), 200
    except (ValueError, TypeError) as exc:
        google_log("invalid_paid_order", error_type=type(exc).__name__)
        return jsonify(error="invalid_order_fields"), 400
    except Exception as exc:
        google_log("paid_order_storage_failed", error_type=type(exc).__name__)
        return jsonify(error="storage_unavailable"), 503


def google_order_context(conn, snapshot):
    """Require checkout-token agreement for browser order links; an order ID alone is not proof."""
    with conn.cursor() as cur:
        cur.execute("""SELECT context FROM google_context WHERE checkout_token=%s
                       AND (order_id IS NULL OR order_id=%s) ORDER BY id DESC LIMIT 1""",
                    (snapshot.get("checkout_token"), snapshot["order_id"]))
        row = cur.fetchone()
        bridge = row[0] if row else {}
        vid = snapshot.get("vid") or bridge.get("vid")
        client = bridge.get("client_id")
        if snapshot.get("vid") and bridge.get("vid") and snapshot["vid"] != bridge["vid"]:
            return None, "conflicting_visitor"
        cur.execute("""SELECT context FROM google_context
                       WHERE (vid=%s OR client_id=%s OR (checkout_token=%s AND (order_id IS NULL OR order_id=%s)))
                       AND received_at > now() - interval '30 days' ORDER BY id DESC LIMIT 100""",
                    (vid, client, snapshot.get("checkout_token"), snapshot["order_id"]))
        contexts = [r[0] for r in cur.fetchall()]
    if not contexts:
        return None, "missing_context"
    vid = vid or next((c.get("vid") for c in contexts if c.get("vid")), None)
    if not all(contexts[0].get("consent", {}).get(k) is True for k in ("marketingAllowed", "saleOfDataAllowed")):
        return None, "consent_not_granted"
    at_ms = int(datetime.fromisoformat(snapshot["event_time"]).timestamp() * 1000)
    revoked_at = max((c.get("consent_ts", 0) for c in contexts
                      if not all(c.get("consent", {}).values())), default=0)
    if revoked_at >= at_ms:
        return None, "consent_revoked_after_purchase"
    click = google_click(snapshot.get("click"), at_ms)
    if click and click["captured_at"] <= revoked_at:
        click = {}
    source = "cart" if click else "none"
    for context in contexts:
        candidate = google_click(context.get("attribution"), at_ms)
        if candidate and candidate["captured_at"] > revoked_at and (
                not click or candidate["captured_at"] > click["captured_at"]):
            click = candidate
            source = "checkout_bridge" if bridge else "visitor"
    return {"click": click, "consent": contexts[0]["consent"], "vid": vid, "source": source}, None


def google_purchase_payload(row, context):
    snapshot = row["snapshot"]
    privacy = context["consent"]
    if not all(privacy.get(k) is True for k in ("marketingAllowed", "saleOfDataAllowed")):
        raise ValueError("consent_not_granted")
    event = {"transactionId": f"{row['shop']}:{row['order_id']}", "eventTimestamp": snapshot["event_time"],
             "eventSource": "WEB", "conversionValue": float(Decimal(snapshot["value"])),
             "currency": snapshot["currency"],
             "consent": {"adUserData": "CONSENT_GRANTED" if privacy["saleOfDataAllowed"] else "CONSENT_DENIED",
                         "adPersonalization": "CONSENT_GRANTED" if privacy["marketingAllowed"] else "CONSENT_DENIED"}}
    click = context["click"]
    for key in ("gclid", "wbraid", "gbraid"):
        if click.get(key):
            event["adIdentifiers"] = {key: click[key]}
            break
    if snapshot["user_data"]:
        event["userData"] = snapshot["user_data"]
    if not event.get("adIdentifiers") and not event.get("userData"):
        raise ValueError("missing_identifiers")
    destination = {"operatingAccount": {"accountType": "GOOGLE_ADS", "accountId": snapshot["customer_id"]},
                   "loginAccount": {"accountType": "GOOGLE_ADS", "accountId": snapshot["login_id"]},
                   "productDestinationId": row["action_id"]}
    return {"destinations": [destination], "events": [event], "encoding": "HEX"}


def google_processing_result(body, action_id, customer_id=None):
    results = body.get("requestStatusPerDestination", [])
    result = next((r for r in results if r.get("destination", {}).get("productDestinationId") == action_id
                   and (customer_id is None or r.get("destination", {}).get("operatingAccount", {}).get("accountId") == customer_id)), None)
    if not result:
        return "accepted", {"reason": "awaiting_destination_status"}
    status = result.get("requestStatus")
    diagnostics = {key: result[key] for key in ("errorInfo", "warningInfo", "eventsIngestionStatus") if key in result}
    if status == "SUCCESS":
        return "processed", diagnostics  # Processing success is not proof of Ads attribution/bidding.
    if status in ("FAILED", "PARTIAL_SUCCESS"):
        reasons = {item.get("reason") for item in result.get("errorInfo", {}).get("errorCounts", [])}
        transient = {"PROCESSING_ERROR_REASON_INTERNAL_ERROR", "PROCESSING_ERROR_REASON_TOO_RECENT_CLICK"}
        if status == "FAILED" and reasons and reasons <= transient:
            return "retry", diagnostics
        return "failed", diagnostics
    return "accepted", diagnostics


def google_delivery_update(row_id, status, diagnostics, delay=60, request_id=None, clear_request=False):
    with google_db() as conn, conn.cursor() as cur:
        cur.execute("""UPDATE google_purchase_delivery SET status=%s, diagnostics=%s::jsonb,
                       next_attempt=now()+(%s * interval '1 second'), lease_until=NULL,
                       request_id=CASE WHEN %s THEN NULL ELSE COALESCE(%s,request_id) END WHERE id=%s""",
                    (status, json.dumps(diagnostics), delay, clear_request, request_id, row_id))
    google_log("delivery_status", delivery_id=row_id, status=status, request_id=request_id, diagnostics=diagnostics)


def google_deliver_one(session):
    from psycopg2.extras import RealDictCursor
    with google_db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""SELECT * FROM google_purchase_delivery
                       WHERE status IN ('pending','waiting_context','retry','accepted') AND next_attempt<=now()
                       AND (lease_until IS NULL OR lease_until<now())
                       AND (%s='' OR order_id=%s) ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1""",
                    (os.environ.get("GOOGLE_TEST_ORDER_ID", ""), os.environ.get("GOOGLE_TEST_ORDER_ID", "")))
        row = cur.fetchone()
        if not row:
            return False
        cur.execute("""UPDATE google_purchase_delivery SET lease_until=now()+interval '120 seconds',
                       attempts=attempts+1 WHERE id=%s""", (row["id"],))
    try:
        if datetime.now(timezone.utc) - row["created_at"] > timedelta(days=2):
            google_delivery_update(row["id"], "needs_review", {"reason": "delivery_deadline"})
            return True
        if row["request_id"]:
            response = session.get("https://datamanager.googleapis.com/v1/requestStatus:retrieve",
                                   params={"requestId": row["request_id"]}, timeout=20)
        else:
            with google_db() as conn:
                context, reason = google_order_context(conn, row["snapshot"])
                if not context:
                    google_delivery_update(row["id"], "waiting_context", {"reason": reason})
                    return True
                if not row["payload"]:
                    row["payload"] = google_purchase_payload(row, context)
                    with conn.cursor() as cur:
                        cur.execute("UPDATE google_purchase_delivery SET payload=%s::jsonb WHERE id=%s",
                                    (json.dumps(row["payload"]), row["id"]))
            google_log("purchase_upload", order_id=row["order_id"], vid=context["vid"],
                       attribution_source=context["source"],
                       click_types=list(row["payload"]["events"][0].get("adIdentifiers", {})))
            response = session.post("https://datamanager.googleapis.com/v1/events:ingest",
                                    json=row["payload"], timeout=20)
        if not response.ok:
            status = "retry" if response.status_code == 429 or response.status_code >= 500 else "failed"
            try:
                error = response.json().get("error", {})
                details = {"http_status": response.status_code, "api_status": error.get("status"),
                           "reasons": [d.get("reason") for d in error.get("details", []) if d.get("reason")]}
            except ValueError:
                details = {"http_status": response.status_code}
            google_delivery_update(row["id"], status, details,
                                   min(3600, 30 * 2 ** min(row["attempts"], 7)))
            return True
        body = response.json()
        if row["request_id"]:
            status, diagnostics = google_processing_result(body, row["action_id"], row["snapshot"]["customer_id"])
            if row["diagnostics"].get("fieldWarnings"):
                diagnostics["fieldWarnings"] = row["diagnostics"]["fieldWarnings"]
            if status == "retry":
                diagnostics["previous_request_id"] = row["request_id"]
            google_delivery_update(row["id"], status, diagnostics, delay=300 if status == "retry" else 60,
                                   request_id=row["request_id"], clear_request=status == "retry")
        elif body.get("requestId"):
            google_delivery_update(row["id"], "accepted", {"fieldWarnings": body.get("fieldWarnings", [])},
                                   request_id=body["requestId"])
        else:
            google_delivery_update(row["id"], "needs_review", {"reason": "missing_request_id"})
    except ValueError as exc:
        google_delivery_update(row["id"], "needs_review", {"reason": str(exc)[:100]})
    except Exception as exc:
        google_delivery_update(row["id"], "retry", {"error_type": type(exc).__name__},
                               min(3600, 30 * 2 ** min(row["attempts"], 7)))
    return True


def google_worker():
    session = None
    ready = False
    while True:
        try:
            if not ready:
                init_google_db()
                ready = True
            if os.environ.get("GOOGLE_UPLOADS_ENABLED") == "true":
                if session is None:
                    from google.oauth2.service_account import Credentials
                    from google.auth.transport.requests import AuthorizedSession
                    credentials = Credentials.from_service_account_info(
                        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
                        scopes=["https://www.googleapis.com/auth/datamanager"])
                    session = AuthorizedSession(credentials)
                google_deliver_one(session)
        except Exception as exc:
            google_log("worker_error", error_type=type(exc).__name__)
        time.sleep(5)


if os.environ.get("GOOGLE_TRACKING_ENABLED") == "true" and DATABASE_URL:
    threading.Thread(target=google_worker, name="google-delivery", daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
