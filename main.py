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
                    contents=None, num_items=None, order_id=None):

    PIXEL_ID = os.environ.get("PIXEL_ID")
    ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN")

    country_code = (country or "").upper()

    user_data = {}
    if email:
        user_data["em"] = [hash_data(email)]
    if phone:
        # US FIX: pass country so normalization matches Meta's expected format
        user_data["ph"] = [hash_data(normalize_phone(phone, country_code or "GB"))]
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
        f"Sending CAPI event: {event_name} | event_id: {event_id} | url: {source_url} | vid: {external_id} | fbc: {fbc} | fbp: {fbp} | fbclid: {fbclid} | email: {email} | phone: {phone}")

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
        f"Cookies from order: vid={vid} | fbc={fbc} | fbp={fbp} | fbclid={fbclid} | utm_source={utm_source}")

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
    # US FIX: province_code gives "CA" not "California" — the format Meta actually matches on
    region = billing.get("province_code") or billing.get("province", "")
    country = billing.get("country_code", "")

    # Extract IP and User Agent from Shopify's client_details
    client_details = order.get("client_details") or {}
    browser_ip = order.get(
        "browser_ip") or client_details.get("browser_ip", "")
    browser_ua = client_details.get("user_agent", "")

    logger.info(
        f"Customer: {email} | {phone} | {city} | {postcode} | {country} | IP: {browser_ip} | UA: {browser_ua[:80]}")

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
        phone=phone,
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
        external_id=vid or customer_id,
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

    vid = body.get("vid")

    logger.info(
        f"Browser event received: {body.get('event_name')} | url: {body.get('source_url')} | ref: {body.get('referrer')} | vid: {vid} | fbc: {body.get('fbc')} | fbp: {body.get('fbp')} | fbclid: {body.get('fbclid')}")

    save_event_db(
        event_name=body.get("event_name"),
        vid=vid,
        url=body.get("source_url"),
        referrer=body.get("referrer"),
        value=body.get("value"))

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
        user_agent=user_agent,
        external_id=vid
    )

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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))