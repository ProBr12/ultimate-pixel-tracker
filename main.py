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

import stores

# Setup logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)


# ============ DATABASE LAYER ============
import psycopg2
import phonenumbers
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
            -- Which store each event belongs to (added with the hub; instant).
            ALTER TABLE events ADD COLUMN IF NOT EXISTS shop TEXT;
        """)
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Database ready.")
    except Exception as e:
        logger.error(f"DB init failed: {e}")


def save_event_db(event_name, vid=None, url=None, referrer=None, value=None, order_id=None, shop=None):
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
            """INSERT INTO events (event_name, vid, url, path, referrer, ad_id, adset_id, campaign_id, utm_source, value, order_id, shop)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (event_name, vid, url, parsed.path or None, referrer,
             q.get("ad_id", [None])[0],
             q.get("utm_term", [None])[0],
             q.get("campaign_id", [None])[0],
             q.get("utm_source", [None])[0],
             value, order_id, shop))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"DB write failed: {e}")


init_db()
stores.init_store_db()


def backfill_event_shops():
    """Label events stored before the hub with their store (by the site
    address in the URL; path-only Purchases came from the first store's order
    webhook). Runs once, in id ranges, in the background."""
    try:
        with stores.db() as conn, conn.cursor() as cur:
            # Built here, not at startup, so a large table never delays boot.
            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_shop_ts ON events (shop, ts)")
        with stores.db() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_xact_lock(724503)")
            if not cur.fetchone()[0]:
                return  # the other worker is on it
            cur.execute("SELECT 1 FROM hub_migrations WHERE name = 'events_shop_backfill'")
            if cur.fetchone():
                return
            cur.execute("SELECT shop_domain, site_url FROM stores WHERE shop_domain IS NOT NULL")
            rules, params = [], []
            for shop, site in cur.fetchall():
                host = urlparse(site or "").hostname
                if host:
                    rules.append("WHEN url LIKE %s OR url LIKE %s OR url = %s THEN %s")
                    params += [f"https://{host}/%", f"https://www.{host}/%", f"https://{host}", shop]
            rules.append("WHEN event_name = 'Purchase' AND (url IS NULL OR url LIKE '/%%') THEN %s")
            params.append(stores.FIRST_STORE["shop_domain"])
            cur.execute("SELECT coalesce(min(id), 0), coalesce(max(id), 0) FROM events")
            low, high = cur.fetchone()
        step = 20000
        for start in range(low - 1, high + 1, step):
            with stores.db() as conn, conn.cursor() as cur:
                cur.execute(
                    f"UPDATE events SET shop = CASE {' '.join(rules)} END "
                    f"WHERE id > %s AND id <= %s AND shop IS NULL AND (CASE {' '.join(rules)} END) IS NOT NULL",
                    params + [start, start + step] + params)
        with stores.db() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO hub_migrations (name) VALUES ('events_shop_backfill') ON CONFLICT DO NOTHING")
        logger.info("Backfilled the store on pre-hub events")
    except Exception as e:
        logger.error(f"event backfill failed: {type(e).__name__}: {e}")



def hash_data(value):
    return hashlib.sha256(value.strip().lower().encode()).hexdigest()


# Real Meta click IDs are long base64url strings. The old check let through
# junk like "fb.1.1779838569048.fbclid" (found on a live cart), which Meta
# reads as a broken click ID.
FBC_RE = re.compile(r"fb\.[0-9]\.[0-9]{10,16}\.[A-Za-z0-9_-]{10,600}")
FBP_RE = re.compile(r"fb\.[0-9]\.[0-9]{10,16}\.[0-9]{1,30}")
FBCLID_RE = re.compile(r"[A-Za-z0-9_-]{10,600}")


def valid_fbc(value):
    return isinstance(value, str) and bool(FBC_RE.fullmatch(value))


def valid_fbp(value):
    return isinstance(value, str) and bool(FBP_RE.fullmatch(value))


def valid_fbclid(value):
    return isinstance(value, str) and bool(FBCLID_RE.fullmatch(value))


def fbc_recent(value, days=90):
    """A stored click ID is reused only within Meta's own 90-day _fbc lifetime."""
    if not valid_fbc(value):
        return False
    created_ms = int(value.split(".")[2])
    if created_ms < 10 ** 12:  # creation time in seconds, not milliseconds
        created_ms *= 1000
    return time.time() * 1000 - created_ms < days * 86400 * 1000


PHONE_REGIONS = phonenumbers.SUPPORTED_REGIONS


def normalize_phone(phone, country="GB"):
    """The phone number in Meta's format: E.164 digits without the "+"
    ("07767 304185" in GB is 447767304185), read with Google's
    libphonenumber for every country. country is the region a number without
    its own country code belongs to (the address's country, else the store's).
    A number that cannot be a real phone number returns "" rather than a
    wrong hash."""
    text = phone.strip() if isinstance(phone, str) else ""
    if not re.search(r"\d", text) or len(text) > 40:
        return ""
    region = (country or "").upper()
    try:
        number = phonenumbers.parse(text, region if region in PHONE_REGIONS else None)
    except phonenumbers.NumberParseException:
        return ""  # no country code and no known country: never guess one
    # Possible as a whole number: not a local-only fragment missing its area
    # code. (Not "valid", which would drop numbers in newly issued ranges.)
    if phonenumbers.is_possible_number_with_reason(number) != phonenumbers.ValidationResult.IS_POSSIBLE:
        return ""
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)[1:]


MATCH_KEYS = ("em", "ph", "fn", "ln", "ct", "st", "zp", "country", "external_id")
HASH_RE = re.compile(r"[0-9a-f]{64}")


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def hashed_user_data(email=None, phone=None, first_name=None, last_name=None,
                     city=None, postcode=None, region=None, country=None,
                     external_id=None, default_country="GB"):
    """Meta's customer matching keys, normalised and SHA-256 hashed. The one
    place this happens, so the browser relay, checkout and order webhooks and
    the identity store all produce identical hashes for the same person."""
    country_code = _text(country).upper()
    data = {}
    email = _text(email)
    if "@" in email:
        data["em"] = [hash_data(email)]
    # phone may be one number or a list (billing, shipping, customer record).
    phones = phone if isinstance(phone, (list, tuple)) else [phone]
    phone_hashes = []
    for p in phones:
        # Read in the buyer's country, else the store's (see normalize_phone).
        digits = normalize_phone(_text(p), country_code or default_country)
        if digits:
            h = hash_data(digits)
            if h not in phone_hashes:
                phone_hashes.append(h)
    if phone_hashes:
        data["ph"] = phone_hashes
    # Names: lowercase letters only, the same rule the theme's pixel uses.
    for key, raw in (("fn", first_name), ("ln", last_name)):
        name = re.sub(r"[^a-zà-ɏ]", "", _text(raw).lower())
        if name:
            data[key] = [hash_data(name)]
    # Meta's ct and st format: lowercase letters only, no spaces.
    for key, raw in (("ct", city), ("st", region)):
        letters = re.sub(r"[^a-z]", "", _text(raw).lower())
        if letters:
            data[key] = [hash_data(letters)]
    zp = _text(postcode).lower().replace(" ", "").replace("-", "")
    if country_code == "US":
        zp = zp[:5]  # US ZIPs are matched on the first 5 digits
    if zp:
        data["zp"] = [hash_data(zp)]
    if re.fullmatch(r"[A-Z]{2}", country_code):
        data["country"] = [hash_data(country_code.lower())]
    # external_id may be one ID or a list (visitor ID and Shopify customer ID).
    ext_ids = external_id if isinstance(external_id, (list, tuple)) else [external_id]
    ext_hashes = []
    for e in ext_ids:
        if e not in (None, ""):
            h = hash_data(str(e))
            if h not in ext_hashes:
                ext_hashes.append(h)
    if ext_hashes:
        data["external_id"] = ext_hashes
    return data


def send_capi_event(event_name, event_id, fbc=None, fbp=None, fbclid=None,
                    email=None, phone=None, first_name=None, last_name=None,
                    city=None, postcode=None, region=None, country=None,
                    value=None, currency="GBP", source_url=None,
                    client_ip=None, user_agent=None,
                    external_id=None, content_ids=None, content_type=None,
                    contents=None, num_items=None, order_id=None,
                    pixel_id=None, access_token=None, matched=None,
                    test_event_code=None, log=None, default_country="GB"):

    PIXEL_ID = pixel_id if pixel_id is not None else os.environ.get("PIXEL_ID")
    ACCESS_TOKEN = access_token if access_token is not None else os.environ.get("ACCESS_TOKEN")

    user_data = hashed_user_data(
        email=email, phone=phone, first_name=first_name, last_name=last_name,
        city=city, postcode=postcode, region=region, country=country,
        external_id=external_id, default_country=default_country)

    # Hashed keys remembered for this visitor (identity store). Only for events
    # that carry no identity of their own, so one event never mixes two people.
    if matched and "em" not in user_data and "ph" not in user_data:
        for key in MATCH_KEYS:
            stored = [h for h in (matched.get(key) or []) if isinstance(h, str) and HASH_RE.fullmatch(h)]
            if not stored:
                continue
            if key == "external_id":
                current = user_data.get(key, [])
                user_data[key] = (current + [h for h in stored if h not in current])[:5]
            elif key not in user_data:
                user_data[key] = stored[:3]

    if valid_fbc(fbc):
        user_data["fbc"] = fbc
    elif valid_fbclid(fbclid):
        user_data["fbc"] = f"fb.1.{int(time.time() * 1000)}.{fbclid}"
    if valid_fbp(fbp):
        user_data["fbp"] = fbp
    if client_ip:
        user_data["client_ip_address"] = client_ip
    if user_agent:
        user_data["client_user_agent"] = user_agent

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

    if test_event_code:
        payload["test_event_code"] = test_event_code

    try:
        response = requests.post(
            f"https://graph.facebook.com/v19.0/{PIXEL_ID}/events",
            json=payload,
            timeout=10
        )
    except requests.RequestException as e:
        if log:
            stores.log_delivery(log.get("shop"), event_name, log.get("source"), user_data.keys(), 0,
                                f"{type(e).__name__}", log.get("version"), log.get("restored"))
        raise

    logger.info(f"Meta CAPI response: {response.status_code} {response.text}")
    if log:
        ok = 200 <= response.status_code < 300
        stores.log_delivery(log.get("shop"), event_name, log.get("source"), user_data.keys(),
                            response.status_code, None if ok else response.text,
                            log.get("version"), log.get("restored"))
    return response


def verify_shopify_webhook(raw_body, hmac_header, secret=None):
    """Check that this webhook genuinely came from Shopify: each store signs
    its webhooks with its own key (Settings > Notifications > Webhooks).
    Without a store's key (secret=None, used only by the Google route) the
    SHOPIFY_WEBHOOK_SECRET Railway variable is used as before."""
    if secret is None:
        secret = os.environ.get("SHOPIFY_WEBHOOK_SECRET", "")
        if not secret:
            logger.warning(
                "SHOPIFY_WEBHOOK_SECRET not set - webhook accepted WITHOUT verification. Set it in Railway ASAP.")
            return True
    if not secret or not hmac_header:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode()
    return hmac.compare_digest(computed, hmac_header)


# ============ STORES ============
# Every store-specific value (pixel, access token, webhook key, country,
# currency) comes from the hub's store registry (stores.py), keyed by the
# store's permanent *.myshopify.com domain. Shopify sends it on every webhook
# and the theme snippet sends it on every event. A store that is not in the
# registry, or is switched off, sends nothing to Meta.

shop_key = stores.shop_key
store_config = stores.store_config


def verified_store(raw_body, topic=None):
    """(shop, store) for a webhook genuinely signed by a store in the
    registry, otherwise (shop, None)."""
    shop = shop_key(request.headers.get("X-Shopify-Shop-Domain"))
    store = store_config(shop)
    if not store or not store.get("webhook_secret"):
        logger.warning(f"Webhook from a store not set up in the hub: {shop or '?'}")
        return shop, None
    if not verify_shopify_webhook(raw_body, request.headers.get("X-Shopify-Hmac-Sha256", ""),
                                  store["webhook_secret"]):
        logger.warning(
            f"Webhook REJECTED - invalid signature for {shop}. Source IP: {request.headers.get('X-Forwarded-For', request.remote_addr)}")
        stores.note_webhook(shop, topic or request.headers.get("X-Shopify-Topic"), "bad_signature")
        return shop, None
    return shop, store


def site_origin(url):
    """https://host of a Shopify URL (order status or checkout recovery link).
    Only the origin: the rest of those URLs carries access keys."""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return None
    if parsed.scheme == "https" and parsed.hostname:
        return f"https://{parsed.hostname}"
    return None


def note_attributes(record):
    """Cart attributes from an order or checkout payload, as a dict."""
    attrs = {}
    for item in record.get("note_attributes") or []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            attrs[item["name"]] = item.get("value") if isinstance(item.get("value"), str) else ""
    return attrs


def buyer_details(record):
    """Who is buying, from a Shopify order or checkout. Billing is the buyer
    (often an adult child paying for a parent); shipping fills gaps. Shopify
    scatters the phone, so every place it keeps one is collected."""
    billing = record.get("billing_address") or {}
    shipping = record.get("shipping_address") or {}
    customer = record.get("customer") or {}
    address = billing or shipping
    phones = []
    for p in (record.get("phone"), billing.get("phone"), customer.get("phone"),
              record.get("sms_marketing_phone")):
        if p and p not in phones:
            phones.append(p)
    # Shipping phone only as a fallback: the recipient may not be the buyer.
    if not phones and shipping.get("phone"):
        phones.append(shipping.get("phone"))
    return {
        "email": record.get("email") or customer.get("email") or "",
        "phone": phones,
        "first_name": address.get("first_name") or "",
        "last_name": address.get("last_name") or "",
        "city": address.get("city") or "",
        "postcode": address.get("zip") or "",
        # province_code gives "CA" not "California": the format Meta matches on
        "region": address.get("province_code") or address.get("province") or "",
        "country": address.get("country_code") or shipping.get("country_code") or "",
        "customer_id": customer.get("id"),
    }


def buyer_match(details, default_country="GB"):
    return hashed_user_data(
        email=details["email"], phone=details["phone"],
        first_name=details["first_name"], last_name=details["last_name"],
        city=details["city"], postcode=details["postcode"], region=details["region"],
        country=details["country"], external_id=details["customer_id"],
        default_country=default_country)


# ============ IDENTITY STORE ============
# One row per store and visitor ID, holding only the hashed matching keys Meta
# already receives (never raw details) plus the browser's last fbc/fbp, user
# agent and IP. Fed by checkout and order webhooks and by identified browser
# events; read back to give that visitor's later anonymous events the email
# and phone they typed at checkout. Joins are exact visitor-ID matches only.

VID_RE = re.compile(r"v_[A-Za-z0-9]{6,40}")
BROWSER_FRESH = timedelta(days=3)


def valid_vid(value):
    return isinstance(value, str) and bool(VID_RE.fullmatch(value))


def init_identity_db():
    if not DATABASE_URL:
        return
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        with conn, conn.cursor() as cur:
            # Both gunicorn workers start at once; the lock stops them racing
            # each other through CREATE TABLE.
            cur.execute("SELECT pg_advisory_xact_lock(724501)")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS meta_identity (
                    shop TEXT NOT NULL,
                    vid TEXT NOT NULL,
                    match JSONB NOT NULL DEFAULT '{}'::jsonb,
                    identified_at TIMESTAMPTZ,
                    fbc TEXT, fbp TEXT, user_agent TEXT, client_ip TEXT,
                    browser_seen_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (shop, vid)
                );
                CREATE INDEX IF NOT EXISTS meta_identity_updated ON meta_identity (updated_at);
                -- device_match: orders with no visitor ID, found by IP and user agent
                CREATE INDEX IF NOT EXISTS meta_identity_device ON meta_identity (shop, client_ip);
                CREATE TABLE IF NOT EXISTS meta_checkout_signal (
                    shop TEXT NOT NULL,
                    checkout_token TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (shop, checkout_token, event_name)
                );
            """)
        conn.close()
    except Exception as e:
        logger.error(f"identity init failed: {type(e).__name__}: {e}")


init_identity_db()

IDENTITY_UPSERT = """
INSERT INTO meta_identity AS t
    (shop, vid, match, identified_at, fbc, fbp, user_agent, client_ip, browser_seen_at, updated_at)
VALUES (%(shop)s, %(vid)s, %(match)s::jsonb,
        CASE WHEN %(match)s::jsonb = '{}'::jsonb THEN NULL ELSE now() END,
        %(fbc)s, %(fbp)s, %(ua)s, %(ip)s,
        CASE WHEN %(ua)s::text IS NULL THEN NULL ELSE now() END, now())
ON CONFLICT (shop, vid) DO UPDATE SET
    -- A different email means a different person on this device: start over
    -- rather than mix two people's details. Otherwise add to what we know.
    match = CASE
        WHEN EXCLUDED.match ? 'em' AND t.match ? 'em' AND t.match->'em' <> EXCLUDED.match->'em'
        THEN EXCLUDED.match
        ELSE t.match || EXCLUDED.match END,
    identified_at = COALESCE(EXCLUDED.identified_at, t.identified_at),
    fbc = COALESCE(EXCLUDED.fbc, t.fbc),
    fbp = COALESCE(EXCLUDED.fbp, t.fbp),
    user_agent = COALESCE(EXCLUDED.user_agent, t.user_agent),
    client_ip = COALESCE(EXCLUDED.client_ip, t.client_ip),
    browser_seen_at = COALESCE(EXCLUDED.browser_seen_at, t.browser_seen_at),
    updated_at = now()
RETURNING match, fbc, fbp, user_agent, client_ip, browser_seen_at
"""

IDENTITY_SELECT = """
SELECT match, fbc, fbp, user_agent, client_ip, browser_seen_at
FROM meta_identity WHERE shop = %(shop)s AND vid = %(vid)s
"""


def identity_record(shop, vid, match=None, fbc=None, fbp=None,
                    user_agent=None, client_ip=None, write=True):
    """Store what we learned about this visitor (write=True) or just read it.
    Returns the stored row as a dict, or {} on any problem: tracking must
    never fail because of it."""
    if not DATABASE_URL or not shop or not valid_vid(vid):
        return {}
    match = {k: v for k, v in (match or {}).items() if k in MATCH_KEYS and v}
    params = {
        "shop": shop, "vid": vid, "match": json.dumps(match),
        "fbc": fbc if valid_fbc(fbc) else None,
        "fbp": fbp if valid_fbp(fbp) else None,
        "ua": user_agent[:512] if isinstance(user_agent, str) and user_agent else None,
        "ip": client_ip[:64] if isinstance(client_ip, str) and client_ip else None,
    }
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=3)
        with conn, conn.cursor() as cur:
            cur.execute(IDENTITY_UPSERT if write else IDENTITY_SELECT, params)
            row = cur.fetchone()
        conn.close()
    except Exception as e:
        logger.error(f"identity {'write' if write else 'read'} failed: {type(e).__name__}")
        return {}
    if not row:
        return {}
    stored, fbc, fbp, ua, ip, seen = row
    record = {"match": stored or {}, "fbc": fbc, "fbp": fbp}
    # The browser details only count while recent: they stand in for the
    # shopper's own device on server events.
    if seen and datetime.now(timezone.utc) - seen < BROWSER_FRESH:
        record["user_agent"] = ua
        record["client_ip"] = ip
    return record


DEVICE_MATCH_WINDOW = timedelta(hours=3)

DEVICE_MATCH_SELECT = """
SELECT vid, match, fbc, fbp FROM meta_identity
WHERE shop = %(shop)s AND client_ip = %(ip)s AND user_agent = %(ua)s
  AND browser_seen_at > now() - %(window)s
ORDER BY browser_seen_at DESC LIMIT 2
"""


def device_match(shop, client_ip, user_agent, email_hash=None):
    """The visitor behind an order that arrived with no visitor ID (Shop Pay
    and other express wallets skip the cart, and no pixel runs on their
    checkout). Only a visitor seen on this store in the last few hours with
    exactly the same IP and user agent counts, and only when they are the
    only one: two candidates (a shared mobile or office IP) means no match.
    A visitor already known under a different email is someone else.
    Returns {vid, fbc, fbp} or {}."""
    if not (DATABASE_URL and shop and isinstance(client_ip, str) and client_ip
            and isinstance(user_agent, str) and user_agent):
        return {}
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=3)
        with conn, conn.cursor() as cur:
            cur.execute(DEVICE_MATCH_SELECT, {"shop": shop, "ip": client_ip[:64], "ua": user_agent[:512],
                                              "window": DEVICE_MATCH_WINDOW})
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        logger.error(f"device match failed: {type(e).__name__}")
        return {}
    if len(rows) != 1:
        return {}
    vid, stored, fbc, fbp = rows[0]
    stored_email = (stored or {}).get("em")
    if email_hash and stored_email and stored_email != email_hash:
        return {}
    return {"vid": vid, "fbc": fbc, "fbp": fbp}


def checkout_signal_claim(shop, token, event_name):
    """True only for the first caller per checkout and event, so webhook
    retries and repeated checkout updates never send the event twice.
    None means storage is down: then nothing is sent."""
    if not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=3)
        with conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO meta_checkout_signal (shop, checkout_token, event_name)
                   VALUES (%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1""",
                (shop, token, event_name))
            claimed = cur.fetchone() is not None
        conn.close()
        return claimed
    except Exception as e:
        logger.error(f"checkout signal claim failed: {type(e).__name__}")
        return None


def checkout_signal_release(shop, token, event_name):
    """Undo a claim when Meta did not accept the event, so the next checkout
    update can try again."""
    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=3)
        with conn, conn.cursor() as cur:
            cur.execute(
                """DELETE FROM meta_checkout_signal
                   WHERE shop = %s AND checkout_token = %s AND event_name = %s""",
                (shop, token, event_name))
        conn.close()
    except Exception as e:
        logger.error(f"checkout signal release failed: {type(e).__name__}")


def identity_cleanup():
    """Anonymous visitors are kept 30 days, identified ones 400 days."""
    while True:
        try:
            conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
            with conn, conn.cursor() as cur:
                cur.execute("""
                    DELETE FROM meta_identity
                    WHERE (identified_at IS NULL AND updated_at < now() - interval '30 days')
                       OR updated_at < now() - interval '400 days';
                    DELETE FROM meta_checkout_signal WHERE sent_at < now() - interval '60 days';
                    DELETE FROM meta_context WHERE received_at < now() - interval '30 days';
                """)
            conn.close()
            stores.cleanup_logs()
        except Exception as e:
            logger.error(f"identity cleanup failed: {type(e).__name__}")
        time.sleep(6 * 3600)


# Shopify order webhook — fires Purchase event
@app.route('/webhook/order-created', methods=['POST'])
def order_created():
    raw_body = request.get_data()
    shop, config = verified_store(raw_body, "orders/create")
    if not config:
        return jsonify({'error': 'unauthorized'}), 401

    order = request.get_json(force=True, silent=True)
    if not isinstance(order, dict):
        stores.note_webhook(shop, "orders/create", "invalid_body")
        return jsonify({'ok': True, 'ignored': 'invalid_body'}), 200
    logger.info(f"Webhook received for order: {order.get('id')} | shop: {shop}")
    if order.get("test") is True:
        # Shopify's "Send test" and test-gateway orders: never fake revenue in Meta.
        stores.note_webhook(shop, "orders/create", "test_order_ignored")
        logger.info(f"Test order {order.get('id')} not sent to Meta")
        return jsonify({'ok': True, 'ignored': 'test_order'}), 200

    attrs = note_attributes(order)
    fbc = attrs.get("_fbc", "")
    fbp = attrs.get("_fbp", "")
    fbclid = attrs.get("_fbclid", "")
    utm_source = attrs.get("_utm_source", "")
    vid = attrs.get("_comfi_vid", "")
    order_id = str(order.get("id", ""))

    # Express checkout / Buy it now skip the cart, so the cart attributes are
    # missing. Fill the gaps from what the checkout pixel sent (/meta/context).
    # A malformed value counts as missing, as in the checkout webhook.
    context = {}
    if not (valid_vid(vid) and valid_fbp(fbp) and valid_fbc(fbc)):
        context = meta_context_lookup(order.get("checkout_token"))
        vid = vid if valid_vid(vid) else (context.get("vid") or "")
        fbp = fbp if valid_fbp(fbp) else (context.get("fbp") or "")
        fbc = fbc if valid_fbc(fbc) else (context.get("fbc") or "")

    buyer = buyer_details(order)
    match = buyer_match(buyer, config["default_country"])

    # Still no visitor: Shop Pay and other wallets skip the cart and no pixel
    # runs on their checkout. Find the visitor by this order's own device
    # (same IP and user agent, recently, and only one such visitor).
    client_details = order.get("client_details") or {}
    device = {}
    if not valid_vid(vid):
        device = device_match(
            shop, order.get("browser_ip") or client_details.get("browser_ip"),
            client_details.get("user_agent") or context.get("user_agent"), match.get("em"))
        vid = device.get("vid") or vid

    # Remember this buyer against their visitor ID, so their later visits
    # carry the email and phone they bought with. The stored row also holds
    # the click and browser IDs from this visitor's earlier events, and their
    # user agent and IP for when the order has none.
    known = identity_record(shop, vid, match=match, fbc=fbc, fbp=fbp)

    if not valid_fbc(fbc) and not valid_fbclid(fbclid):
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

    # Last resort, the IDs this visitor's own browser sent us earlier (their
    # AddToCart, InitiateCheckout, a past order), as /track and the checkout
    # webhook already do: the cart and the checkout pixel can both miss them.
    # This order's own click (fbclid above) wins over an older stored one.
    from_identity = []
    if not valid_fbc(fbc) and not valid_fbclid(fbclid) and fbc_recent(known.get("fbc")):
        fbc = known["fbc"]
        from_identity.append("fbc")
    if not valid_fbp(fbp) and valid_fbp(known.get("fbp")):
        fbp = known["fbp"]
        from_identity.append("fbp")

    logger.info(
        f"Identifiers for order {order_id}: vid={bool(vid)} fbc={bool(fbc)} fbp={bool(fbp)} "
        f"fbclid={bool(fbclid)} from_context={bool(context)} "
        f"from_identity={','.join(from_identity) or '-'} utm_source={utm_source}"
        + (" from_device=True" if device else ""))

    total_price = order.get("total_price", "0")
    content_ids, contents, num_items = line_item_contents(order)

    # IP and user agent from Shopify's client_details (read above)
    browser_ip = (order.get("browser_ip") or client_details.get("browser_ip")
                  or known.get("client_ip") or "")
    browser_ua = (client_details.get("user_agent") or context.get("user_agent")
                  or known.get("user_agent") or "")

    save_event_db(
        event_name="Purchase",
        vid=vid,
        url=order.get("landing_site"),
        value=float(total_price),
        order_id=order_id,
        shop=shop)

    send_capi_event(
        event_name="Purchase",
        event_id=f"purchase_{order_id}",
        fbc=fbc,
        fbp=fbp,
        fbclid=fbclid,
        email=buyer["email"],
        phone=buyer["phone"],
        first_name=buyer["first_name"],
        last_name=buyer["last_name"],
        city=buyer["city"],
        postcode=buyer["postcode"],
        region=buyer["region"],
        country=buyer["country"],
        value=float(total_price),
        # total_price is always shop currency; pair it with the order's own currency field
        currency=order.get("currency", "GBP"),
        source_url=site_origin(order.get("order_status_url")) or config.get("site_url"),
        client_ip=browser_ip,
        user_agent=browser_ua,
        # Both IDs: the browser sends the visitor ID, Meta can also join on the
        # customer ID across repeat orders.
        external_id=[vid, buyer["customer_id"]],
        content_ids=content_ids,
        content_type="product",
        contents=contents,
        num_items=num_items,
        order_id=order_id,
        pixel_id=config["pixel_id"],
        access_token=config["access_token"],
        default_country=config["default_country"],
        log={"shop": shop, "source": "webhook"}
    )
    stores.note_webhook(shop, "orders/create", "purchase_sent")

    return jsonify({'ok': True}), 200


def line_item_contents(record):
    """content_ids, contents and item count from an order or checkout."""
    content_ids, contents, num_items = [], [], 0
    for item in record.get("line_items") or []:
        if not isinstance(item, dict):
            continue
        quantity = item.get("quantity") or 1
        num_items += quantity
        if not item.get("product_id"):
            continue
        product_id = str(item["product_id"])
        content_ids.append(product_id)
        try:
            price = float(item.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        contents.append({"id": product_id, "quantity": quantity, "item_price": price})
    return content_ids, contents, num_items


# ============ CHECKOUT WEBHOOKS ============
# Shopify's "Checkout creation" and "Checkout update" webhooks fire from
# Shopify's own servers as soon as a shopper types their details at checkout,
# so no ad blocker, browser or consent banner can stop them. They carry the
# email, phone, address and the cart attributes (visitor ID, fbc, fbp).
#
# Two things happen with them:
# 1. The identity store learns who this visitor is (see above).
# 2. Once per checkout, an AddPaymentInfo event goes to Meta with the full
#    identity, when the shopper has given contact and delivery details and is
#    at the payment step. No browser sends AddPaymentInfo, so nothing double
#    counts. It lets Meta match abandoners nearly as well as buyers, which
#    feeds retargeting and the delivery algorithm.

CHECKOUT_EVENT = "AddPaymentInfo"


@app.route('/webhook/checkout', methods=['POST'])
def checkout_webhook():
    raw_body = request.get_data()
    topic = request.headers.get("X-Shopify-Topic") or "checkouts/update"
    shop, config = verified_store(raw_body, topic)
    if not config:
        return jsonify({'error': 'unauthorized'}), 401
    try:
        checkout = json.loads(raw_body)
    except (ValueError, TypeError):
        checkout = None
    if not isinstance(checkout, dict):
        stores.note_webhook(shop, topic, "invalid_body")
        return jsonify({'ok': True, 'ignored': 'invalid_body'}), 200
    try:
        result = handle_checkout(shop, config, checkout)
    except Exception as e:
        # Always 200: Shopify disables webhooks that keep failing, and a lost
        # checkout signal must never cost us the order webhooks.
        logger.error(f"Checkout webhook failed: {type(e).__name__}: {e}")
        result = "error"
    stores.note_webhook(shop, topic, result)
    return jsonify({'ok': True, 'result': result}), 200


def handle_checkout(shop, config, checkout):
    token = checkout.get("token")
    token = token if isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", token) else ""
    attrs = note_attributes(checkout)
    vid = attrs.get("_comfi_vid", "")
    fbc = attrs.get("_fbc", "")
    fbp = attrs.get("_fbp", "")
    fbclid = attrs.get("_fbclid", "")

    # Express checkout skips the cart: fall back to what the checkout pixel sent.
    context = {}
    if token and not (valid_vid(vid) and valid_fbp(fbp) and valid_fbc(fbc)):
        context = meta_context_lookup(token)
        vid = vid if valid_vid(vid) else (context.get("vid") or "")
        fbp = fbp if valid_fbp(fbp) else (context.get("fbp") or "")
        fbc = fbc if valid_fbc(fbc) else (context.get("fbc") or "")

    buyer = buyer_details(checkout)
    match = buyer_match(buyer, config["default_country"])
    known = identity_record(shop, vid, match=match, fbc=fbc, fbp=fbp)
    if not valid_fbc(fbc) and fbc_recent(known.get("fbc")):
        fbc = known.get("fbc")
    if not valid_fbp(fbp):
        fbp = known.get("fbp")

    shipping = checkout.get("shipping_address") or {}
    billing = checkout.get("billing_address") or {}
    has_contact = "em" in match or "ph" in match
    has_delivery = any(a.get(f) for a in (shipping, billing) for f in ("address1", "zip"))
    summary = (f"Checkout {token[:8] or '?'}: shop={shop} vid={valid_vid(vid)} "
               f"keys={','.join(sorted(match))} fbc={valid_fbc(fbc)} fbp={valid_fbp(fbp)}")

    if not (token and has_contact and has_delivery):
        logger.info(f"{summary} -> identity only (not at payment step yet)")
        return "identity"

    # Meta requires the shopper's browser user agent on website events. The
    # checkout webhook has none, so use the one from this visitor's own
    # InitiateCheckout or AddToCart (or the checkout pixel). Without it, wait
    # for a later checkout update rather than send a weak event.
    user_agent = context.get("user_agent") or known.get("user_agent")
    if not user_agent:
        logger.info(f"{summary} -> {CHECKOUT_EVENT} waiting: no browser user agent yet")
        return "waiting"

    claimed = checkout_signal_claim(shop, token, CHECKOUT_EVENT)
    if not claimed:
        logger.info(f"{summary} -> {CHECKOUT_EVENT} {'already sent' if claimed is False else 'skipped: storage down'}")
        return "already_sent" if claimed is False else "storage_down"

    content_ids, contents, num_items = line_item_contents(checkout)
    try:
        value = float(checkout.get("total_price") or 0)
    except (TypeError, ValueError):
        value = None
    # Checkout amounts are in the shopper's currency, not the shop's.
    currency = checkout.get("presentment_currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        currency = checkout.get("currency") if isinstance(checkout.get("currency"), str) else config["currency"]

    try:
        response = send_capi_event(
            event_name=CHECKOUT_EVENT,
            event_id=f"addpaymentinfo_{token}",
            fbc=fbc, fbp=fbp, fbclid=fbclid,
            email=buyer["email"], phone=buyer["phone"],
            first_name=buyer["first_name"], last_name=buyer["last_name"],
            city=buyer["city"], postcode=buyer["postcode"], region=buyer["region"],
            country=buyer["country"],
            value=value, currency=currency,
            source_url=site_origin(checkout.get("abandoned_checkout_url")) or config.get("site_url"),
            client_ip=known.get("client_ip"), user_agent=user_agent,
            external_id=[vid, buyer["customer_id"]],
            content_ids=content_ids, content_type="product",
            contents=contents, num_items=num_items,
            pixel_id=config["pixel_id"], access_token=config["access_token"],
            default_country=config["default_country"],
            log={"shop": shop, "source": "webhook"})
        delivered = 200 <= response.status_code < 300
    except Exception as e:
        logger.error(f"{CHECKOUT_EVENT} send failed: {type(e).__name__}")
        delivered = False
    if not delivered:
        checkout_signal_release(shop, token, CHECKOUT_EVENT)
        return "send_failed"
    logger.info(f"{summary} -> {CHECKOUT_EVENT} sent")
    return "sent"


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
            cur.execute("SELECT pg_advisory_xact_lock(724504)")  # both workers start at once
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
                -- Which store's checkout pixel sent it and which version, so
                -- the hub's Setup tab can tick. Lookups stay by checkout token.
                ALTER TABLE meta_context ADD COLUMN IF NOT EXISTS shop TEXT;
                ALTER TABLE meta_context ADD COLUMN IF NOT EXISTS client_version INT;
                CREATE INDEX IF NOT EXISTS meta_context_shop
                    ON meta_context (shop, received_at DESC);
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
    # The store is only recorded when it is in the hub (this endpoint is
    # public); older pixels send none. It never affects which checkout the
    # row belongs to.
    shop = shop_key(body.get("shop"))
    shop = shop if shop and store_config(shop) else None
    version = body.get("v")
    version = version if isinstance(version, int) and not isinstance(version, bool) and 0 < version < 1000 else None

    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        with conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO meta_context (checkout_token, order_id, vid, fbp, fbc, user_agent, shop, client_version)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (clean.get("checkout_token"), clean.get("order_id"), clean.get("vid"),
                 clean.get("fbp"), clean.get("fbc"), clean.get("user_agent"), shop, version))
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

    save_event_db("Purchase", vid=vid, url=source_url, value=value, order_id=order_id, shop="woo")
    return jsonify({'ok': True}), 200


# ============ BOT FILTER ============
# On hearcomfi.com 84% of relayed events were bots (mostly Facebook's link
# preview crawler fetching /meta.json), all sent to Meta as real PageViews.
# Purchase is never filtered: a real sale must never be dropped.

BOT_UA = re.compile(
    # "(?<!cu)" keeps real CUBOT-brand phones from matching "bot".
    # "meta-external" is Meta's own ad-review and AI crawlers (meta-externalads,
    # meta-externalagent, meta-externalfetcher).
    r"(?i)((?<!cu)bot(?:[/;)\-\s]|$)|crawl|spider|slurp|externalhit|facebookcatalog|meta-external|"
    r"headless|lighthouse|pagespeed|pingdom|uptime|monitor|python-|curl/|wget/|"
    r"postman|go-http-client|okhttp|java/|axios/|node-fetch|scrapy|ahrefs|"
    r"semrush|petalbot|bytespider|gptbot|claudebot|ccbot|perplexity|"
    r"vaultpress|detect|scanner|preview)")

NON_PAGE_PATH = re.compile(
    r"(?i)(\.(json|txt|xml|js|css|png|jpe?g|gif|svg|ico|webp|map|php)$"
    r"|^/(wp-json|wp-admin|wp-content|wp-includes|wp-cron|xmlrpc|api|feed|cdn-cgi)(/|$)"
    r"|^/sitemap)")


def bot_reason(event_name, user_agent, source_url):
    """Why this event looks automated, or None if it looks like a person."""
    if event_name == "Purchase":
        return None
    if not user_agent:
        return "no_user_agent"
    match = BOT_UA.search(user_agent)
    if match:
        return f"ua:{match.group(0).strip(' /;)').lower()}"
    path = urlparse(source_url or "").path or "/"
    if NON_PAGE_PATH.search(path):
        return f"path:{path[:60]}"
    return None


# Lets the hub confirm a store's tracking address reaches this service.
@app.route('/ping')
def ping():
    return jsonify({'ok': True, 'service': 'tracking-hub'}), 200


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

    # force: the theme posts text/plain, which skips the CORS preflight so the
    # event survives the page navigating away (checkout clicks).
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'invalid_body'}), 400

    # Get IP from request (Railway uses X-Forwarded-For behind proxy)
    client_ip = request.headers.get(
        "X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr
    user_agent = request.headers.get("User-Agent", "")
    event_name = body.get("event_name")

    if woo_options:
        client_ip = body.get("client_ip") or client_ip
        user_agent = body.get("user_agent") or user_agent
        woo_options.update({key: body.get(key) for key in (
            "first_name", "last_name", "city", "postcode", "region", "country",
            "content_ids", "content_type", "contents", "num_items")})
        extras = woo_options
        shop = "woo"
        config = {}
    else:
        extras = browser_extras(body)
        shop = shop_key(body.get("shop"))
        config = store_config(shop)
        if not config:
            # Not a store in the hub (or switched off): its events must never
            # land in another store's pixel.
            logger.info(f"Skipped event from a store not set up in the hub: {body.get('event_name')} | "
                        f"shop: {shop or '-'} | url: {str(body.get('source_url'))[:80]}")
            return jsonify({'ok': True, 'skipped': 'unknown_store'}), 200

    vid = body.get("vid")

    reason = bot_reason(event_name, user_agent, body.get("source_url"))
    if reason:
        # 200 so the sender (theme or WordPress relay) treats it as handled.
        logger.info(f"Skipped bot event: {event_name} | {reason}")
        return jsonify({'ok': True, 'skipped': 'bot'}), 200

    # Identity store: record who this visitor is when the event says so
    # (logged-in customer, WooCommerce order), and refresh their browser details
    # on the steps before checkout, which the checkout webhook relies on.
    # Everything else only reads.
    own = hashed_user_data(
        email=body.get("email"), phone=body.get("phone"),
        first_name=extras.get("first_name"), last_name=extras.get("last_name"),
        city=extras.get("city"), postcode=extras.get("postcode"),
        region=extras.get("region"), country=extras.get("country"),
        default_country=config.get("default_country") or "GB")
    identified = "em" in own or "ph" in own
    # Ad visitors are written from their first page too: someone who lands
    # from an ad and pays with Shop Pay never adds to cart, and the order then
    # arrives with nothing but their IP and user agent (see device_match).
    write = (identified or event_name in ("AddToCart", "InitiateCheckout")
             or valid_fbc(body.get("fbc")))
    known = identity_record(
        shop, vid, match=own if identified else None,
        fbc=body.get("fbc"), fbp=body.get("fbp"),
        user_agent=user_agent, client_ip=client_ip, write=write)

    restored = body.get("restored")
    restored = restored if isinstance(restored, str) and re.fullmatch(r"[a-z_,]{1,40}", restored) else ""
    logger.info(
        f"Browser event received: {event_name} | url: {body.get('source_url')} | ref: {body.get('referrer')} | vid: {vid} | fbc: {body.get('fbc')} | fbp: {body.get('fbp')} | fbclid: {body.get('fbclid')}"
        f" | shop: {shop or '-'} | known: {','.join(sorted(known.get('match') or {})) or '-'}"
        + (f" | restored: {restored}" if restored else ""))

    value = body.get("value")
    currency = body.get("currency", "GBP")
    if not woo_options:  # the signed WooCommerce relay is trusted as it was
        try:
            value = float(value) if isinstance(value, (int, float, str)) and not isinstance(value, bool) else None
        except ValueError:
            value = None
        if value is not None and not 0 <= value < 10_000_000:
            value = None
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            currency = config["currency"]
    version = body.get("v")
    version = version if isinstance(version, int) and not isinstance(version, bool) and 0 < version < 1000 else None
    # Fall back to the click and browser IDs stored for this visitor when the
    # browser lost its own (Safari's 7-day wipe, ad blockers).
    fbc = body.get("fbc") if valid_fbc(body.get("fbc")) else (
        known.get("fbc") if fbc_recent(known.get("fbc")) else None)
    fbp = body.get("fbp") if valid_fbp(body.get("fbp")) else known.get("fbp")

    save_event_db(
        event_name=event_name,
        vid=vid,
        url=body.get("source_url"),
        referrer=body.get("referrer"),
        value=value,
        shop=shop)

    response = send_capi_event(
        event_name=event_name,
        event_id=body.get("event_id", f"evt_{int(time.time())}"),
        fbc=fbc,
        fbp=fbp,
        fbclid=body.get("fbclid"),
        email=body.get("email"),
        phone=body.get("phone"),
        value=value,
        currency=currency,
        source_url=body.get("source_url"),
        client_ip=client_ip,
        user_agent=user_agent,
        external_id=vid,
        matched=known.get("match"),
        default_country=config.get("default_country") or "GB",
        **{k: v for k, v in config.items() if k in ("pixel_id", "access_token")},
        **extras,
        log={"shop": shop, "source": "woo-relay" if woo_options else "browser",
             "version": version, "restored": restored}
    )

    if woo_options and not 200 <= response.status_code < 300:
        return jsonify({'error': 'Meta delivery failed'}), 502

    return jsonify({'ok': True}), 200


def browser_extras(body):
    """Country and product fields from the storefront theme, checked: this
    endpoint is public, so only well-formed values reach Meta."""
    out = {}
    country = body.get("country")
    if isinstance(country, str) and re.fullmatch(r"[A-Za-z]{2}", country):
        out["country"] = country.upper()
    product_id = re.compile(r"[A-Za-z0-9_-]{1,64}")

    def count(value, maximum=9999):
        return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= maximum

    ids = body.get("content_ids")
    if isinstance(ids, list):
        ids = [str(i) for i in ids[:50]
               if isinstance(i, (str, int)) and not isinstance(i, bool) and product_id.fullmatch(str(i))]
        if ids:
            out["content_ids"] = ids
    if body.get("content_type") in ("product", "product_group"):
        out["content_type"] = body["content_type"]
    contents = []
    for item in body.get("contents") if isinstance(body.get("contents"), list) else []:
        if not isinstance(item, dict) or not product_id.fullmatch(str(item.get("id", ""))):
            continue
        clean = {"id": str(item["id"])}
        if count(item.get("quantity")):
            clean["quantity"] = item["quantity"]
        price = item.get("item_price")
        if isinstance(price, (int, float)) and not isinstance(price, bool) and 0 <= price < 10_000_000:
            clean["item_price"] = price
        contents.append(clean)
        if len(contents) == 50:
            break
    if contents:
        out["contents"] = contents
    if count(body.get("num_items")):
        out["num_items"] = body["num_items"]
    return out




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

if DATABASE_URL:
    threading.Thread(target=identity_cleanup, name="identity-cleanup", daemon=True).start()
    threading.Thread(target=backfill_event_shops, name="event-backfill", daemon=True).start()
    stores.start_background()


# ============ HUB (store setup, health, funnel) ============
import hub  # noqa: E402  (needs the functions above)
hub.register(app, send_capi_event=send_capi_event)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
