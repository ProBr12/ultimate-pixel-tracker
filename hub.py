"""The tracking hub: a password-protected web app on the tracker for adding
stores, setting each one up in a few minutes, and watching its signal.

Pages (all under /hub):
  /hub                      every store and whether it is sending normally
  /hub/stores/new           add a store (the Meta token is checked on save)
  /hub/stores/<id>          Health, Funnel, Setup and Settings for one store
"""
import hmac
import html
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse

import requests
from flask import Blueprint, Response, abort, redirect, request, session

import stores

SNIPPET_VERSION = 6
bp = Blueprint("hub", __name__, url_prefix="/hub")
_send_capi_event = None


def register(app, send_capi_event):
    global _send_capi_event
    _send_capi_event = send_capi_event
    key = stores.session_secret()
    if key:
        app.secret_key = key
    app.config.update(
        SESSION_COOKIE_NAME="hub_session", SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=14))
    app.register_blueprint(bp)


# ============ HELPERS ============

def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def ago(ts):
    if not ts:
        return "never"
    seconds = (datetime.now(timezone.utc) - ts).total_seconds()
    if seconds < 90:
        return "just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return "just now"


def pct(part, whole):
    return round(100 * part / whole) if whole else 0


CURRENCY_SIGNS = {"GBP": "£", "USD": "$", "EUR": "€", "AUD": "A$", "CAD": "C$"}


def money(value, currency):
    sign = CURRENCY_SIGNS.get(currency)
    return f"{sign}{value:,.0f}" if sign else f"{value:,.0f} {esc(currency)}"


def public_host():
    host = os.environ.get("HUB_PUBLIC_HOST") or request.host
    return host if re.fullmatch(r"[A-Za-z0-9.-]+(:[0-9]+)?", host or "") else "track.comfishop.com"


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def csrf_field():
    return f'<input type="hidden" name="csrf" value="{esc(csrf_token())}">'


def check_csrf():
    sent = request.form.get("csrf", "")
    if not session.get("csrf") or not hmac.compare_digest(sent, session["csrf"]):
        abort(400, "This form expired. Go back, reload the page and try again.")


def hub_ready():
    return stores.secret_configured() and bool(os.environ.get("DASH_PASSWORD")) and bool(stores.DATABASE_URL)


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not hub_ready():
            return setup_needed_page()
        if not session.get("hub"):
            return redirect(f"/hub/login?next={request.path}")
        return view(*args, **kwargs)
    return wrapper


# ============ PAGE SHELL ============

CSS = """
:root{--paper:#F4F6F8;--surface:#FFFFFF;--ink:#17202B;--muted:#5A6573;--line:#DDE3E9;
--accent:#2747D4;--accent-ink:#FFFFFF;--ok:#0F7A5A;--warn:#B26B00;--bad:#C0352B;--soft:#EEF1F5}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--paper:#10151C;--surface:#171E27;
--ink:#E7ECF2;--muted:#9AA6B5;--line:#2A3441;--accent:#7D93FF;--accent-ink:#0E1420;--ok:#38C793;
--warn:#F0B44C;--bad:#FF7A6E;--soft:#1D2530}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 "Schibsted Grotesk",system-ui,sans-serif;
font-variant-numeric:tabular-nums}
a{color:var(--accent)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
.shell{display:grid;grid-template-columns:260px 1fr;min-height:100vh}
.shell>*{min-width:0}
.rack{border-right:1px solid var(--line);padding:22px 16px;display:flex;flex-direction:column;gap:18px;background:var(--surface)}
.brand{font-weight:700;font-size:17px;letter-spacing:-.01em;color:var(--ink);text-decoration:none;padding:0 8px}
.brand small{display:block;font-weight:400;font-size:13px;color:var(--muted)}
.rack nav{display:flex;flex-direction:column;gap:2px}
.rack nav a{display:flex;align-items:center;gap:10px;padding:9px 10px;border-radius:8px;color:var(--ink);text-decoration:none}
.rack nav a:hover{background:var(--soft)}
.rack nav a.on{background:var(--soft);font-weight:600}
.rack nav a span.dom{display:block;font-size:12.5px;color:var(--muted);font-weight:400}
.dot{width:9px;height:9px;border-radius:50%;flex:none;background:var(--muted)}
.dot.ok{background:var(--ok)}.dot.setup{background:var(--warn)}.dot.problem{background:var(--bad)}
.rack .add{margin:0 8px}
.rack form{margin-top:auto;padding:0 8px}
main{padding:30px clamp(16px,4vw,44px) 60px;max-width:1180px;width:100%}
h1{font-size:28px;line-height:1.15;letter-spacing:-.02em;margin:0 0 4px}
h2{font-size:18px;letter-spacing:-.01em;margin:34px 0 10px}
h3{font-size:15px;margin:0 0 4px}
p{margin:0 0 12px;max-width:70ch}
.sub{color:var(--muted);margin:0 0 20px}
.status{display:inline-flex;align-items:center;gap:8px;font-weight:600}
.status.ok{color:var(--ok)}.status.setup{color:var(--warn)}.status.problem{color:var(--bad)}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:22px 0 24px;overflow-x:auto}
.tabs a{padding:10px 14px;color:var(--muted);text-decoration:none;border-bottom:2px solid transparent;margin-bottom:-1px;white-space:nowrap}
.tabs a.on{color:var(--ink);border-color:var(--accent);font-weight:600}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;font:inherit;font-weight:600;
padding:9px 16px;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:var(--accent-ink);
cursor:pointer;text-decoration:none}
.btn.quiet{background:transparent;color:var(--accent)}
.btn.small{padding:6px 12px;font-size:14px}
.figures{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--line);
border:1px solid var(--line);border-radius:10px;overflow:hidden;margin:6px 0 8px}
.figures div{background:var(--surface);padding:14px 16px}
.figures b{display:block;font-size:24px;letter-spacing:-.02em}
.figures span{color:var(--muted);font-size:13.5px}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:18px 20px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--surface)}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:500;font-size:13px}
tr:last-child td{border-bottom:0}
td.num,th.num{text-align:right}
.grid td.cell{text-align:center;padding:0;min-width:64px}
.grid td.cell div{margin:3px;padding:7px 4px;border-radius:5px;font-size:13px;
background:color-mix(in srgb,var(--accent) var(--c),var(--surface));color:var(--cell-ink)}
.grid td.event{font-weight:600}
.legend{display:flex;align-items:center;gap:10px;color:var(--muted);font-size:13px;margin:8px 0 0}
.legend i{display:inline-block;width:120px;height:10px;border-radius:5px;
background:linear-gradient(90deg,var(--surface),var(--accent));border:1px solid var(--line)}
ol.steps{list-style:none;counter-reset:s;padding:0;margin:0;display:flex;flex-direction:column;gap:12px}
ol.steps>li{counter-increment:s;display:grid;grid-template-columns:34px 1fr;gap:14px;background:var(--surface);
border:1px solid var(--line);border-radius:10px;padding:16px 18px}
ol.steps>li::before{content:counter(s);width:30px;height:30px;border-radius:50%;display:grid;place-items:center;
font-weight:700;border:2px solid var(--line);color:var(--muted)}
ol.steps>li.done::before{content:"✓";border-color:var(--ok);color:var(--ok)}
ol.steps>li.wait::before{border-color:var(--warn);color:var(--warn)}
ol.steps>li.bad::before{border-color:var(--bad);color:var(--bad)}
.state{font-size:14px;margin:0 0 10px}
.state.done{color:var(--ok)}.state.wait{color:var(--warn)}.state.bad{color:var(--bad)}.state.neutral{color:var(--muted)}
.copy{display:flex;gap:8px;align-items:stretch;margin:8px 0}
.copy code{flex:1;background:var(--soft);border:1px solid var(--line);border-radius:8px;padding:8px 10px;
font:13.5px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;overflow-x:auto;white-space:nowrap}
textarea.code{width:100%;height:220px;font:12.5px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--soft);
color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:10px}
form.fields{display:grid;gap:16px;max-width:620px}
label{display:grid;gap:5px;font-weight:600;font-size:14px}
label span{font-weight:400;color:var(--muted);font-size:13.5px}
input[type=text],input[type=password],input[type=url],select{font:inherit;padding:9px 11px;border-radius:8px;
border:1px solid var(--line);background:var(--surface);color:var(--ink);width:100%}
.row{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.check{display:flex;gap:10px;align-items:center;font-weight:500}
.note{border-left:3px solid var(--warn);padding:10px 14px;background:var(--surface);border-radius:0 8px 8px 0;margin:0 0 18px}
.note.bad{border-color:var(--bad)}.note.good{border-color:var(--ok)}
.muted{color:var(--muted)}
.empty{padding:28px;text-align:center;color:var(--muted)}
.range a{margin-right:12px}
.login{max-width:380px;margin:12vh auto;padding:0 16px}
@media (max-width:820px){.shell{grid-template-columns:1fr}.rack{border-right:0;border-bottom:1px solid var(--line)}
.rack form{margin-top:0}.row{grid-template-columns:1fr}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

COPY_JS = """
document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-copy]");
  if (!b) return;
  var el = document.getElementById(b.getAttribute("data-copy"));
  var text = el.value !== undefined ? el.value : el.textContent;
  navigator.clipboard.writeText(text).then(function () {
    var old = b.textContent; b.textContent = "Copied"; setTimeout(function () { b.textContent = old; }, 1500);
  });
});
"""


def page(title, content, active=None, bare=False):
    head = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body>"""
    if bare:
        return head + content + "</body></html>"
    rack = ""
    try:
        for store in stores.list_stores():
            state, _ = store_status(store)
            on = " on" if store["id"] == active else ""
            rack += (f'<a class="{on.strip()}" href="/hub/stores/{esc(store["id"])}">'
                     f'<i class="dot {state}" aria-hidden="true"></i>'
                     f'<span>{esc(store["name"])}<span class="dom">{esc(store["shop_domain"])}</span></span></a>')
    except Exception:
        rack = '<p class="muted">Stores could not be loaded.</p>'
    return (head + f"""<div class="shell"><aside class="rack">
<a class="brand" href="/hub">Tracking hub<small>Meta signal for every store</small></a>
<nav aria-label="Stores">{rack}</nav>
<a class="btn quiet small add" href="/hub/stores/new">Add a store</a>
<form method="post" action="/hub/logout">{csrf_field()}<button class="btn quiet small">Log out</button></form>
</aside><main>{content}</main></div><script>{COPY_JS}</script></body></html>""")


def setup_needed_page():
    missing = []
    if not stores.secret_configured():
        missing.append("<li><b>HUB_SECRET_KEY</b>: a long random value (at least 16 characters). "
                       "It encrypts store credentials and signs your login. Changing it later means "
                       "re-entering every store's token and webhook key.</li>")
    if not os.environ.get("DASH_PASSWORD"):
        missing.append("<li><b>DASH_PASSWORD</b>: the password you will log in with.</li>")
    if not stores.DATABASE_URL:
        missing.append("<li><b>DATABASE_URL</b>: the hub stores everything in Postgres.</li>")
    return page("Finish setting up the hub", f"""<div class="login"><h1>One step left</h1>
<p>Add these Railway variables to the tracker service, then reload this page.</p><ul>{''.join(missing)}</ul></div>""",
                bare=True), 503


# ============ LOGIN ============

_failed = {}


def _client_ip():
    return request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote_addr or "?"


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not hub_ready():
        return setup_needed_page()
    error = ""
    ip = _client_ip()
    recent = [t for t in _failed.get(ip, []) if t > time.time() - 900]
    _failed[ip] = recent
    if request.method == "POST":
        check_csrf()
        if len(recent) >= 5:
            error = "Too many wrong passwords. Try again in 15 minutes."
        elif hmac.compare_digest(request.form.get("password", "").encode(),
                                 os.environ.get("DASH_PASSWORD", "").encode()):
            session.clear()
            session.permanent = True
            session["hub"] = True
            nxt = request.args.get("next", "/hub")
            return redirect(nxt if re.fullmatch(r"/hub[A-Za-z0-9/_-]*", nxt) else "/hub")
        else:
            _failed[ip].append(time.time())
            error = "That password is not right."
    return page("Log in", f"""<div class="login"><h1>Tracking hub</h1>
<p class="sub">Log in to manage your stores.</p>
{f'<p class="note bad">{esc(error)}</p>' if error else ''}
<form class="fields" method="post">{csrf_field()}
<label>Password<input type="password" name="password" autocomplete="current-password" required autofocus></label>
<button class="btn">Log in</button></form></div>""", bare=True)


@bp.route("/logout", methods=["POST"])
def logout():
    check_csrf()
    session.clear()
    return redirect("/hub/login")


# ============ DATA ============

KEY_COLUMNS = [("em", "Email"), ("ph", "Phone"), ("fn", "Name"), ("zp", "Postcode"),
               ("fbc", "Ad click"), ("fbp", "Browser ID"), ("external_id", "Visitor ID"),
               ("client_ip_address", "IP"), ("client_user_agent", "Device"), ("country", "Country")]
FUNNEL_ORDER = ["PageView", "ViewContent", "AddToCart", "InitiateCheckout", "AddPaymentInfo", "Purchase"]


def store_facts(shop):
    """Everything the status, health and setup views need about one store."""
    facts = {"events": [], "errors": [], "receipts": {}, "last_browser": None, "version": None,
             "total": 0, "accepted": 0, "purchases": 0, "apinfo_7d": 0, "restores_7d": 0, "identified": 0}
    if not shop:
        return facts
    key_sql = ", ".join("count(*) FILTER (WHERE %s = ANY(keys))" for _ in KEY_COLUMNS)
    with stores.db() as conn, conn.cursor() as cur:
        cur.execute(
            f"""SELECT event_name, count(*), count(*) FILTER (WHERE status BETWEEN 200 AND 299), {key_sql}
                FROM meta_deliveries
                WHERE shop = %s AND source <> 'test' AND ts > now() - interval '24 hours'
                GROUP BY event_name""",
            [k for k, _ in KEY_COLUMNS] + [shop])
        for row in cur.fetchall():
            name, count, ok, *keys = row
            facts["events"].append({"name": name, "count": count, "ok": ok, "keys": keys})
            facts["total"] += count
            facts["accepted"] += ok
            if name == "Purchase":
                facts["purchases"] = count
        facts["events"].sort(key=lambda e: (FUNNEL_ORDER.index(e["name"]) if e["name"] in FUNNEL_ORDER else 99, e["name"]))
        cur.execute("""SELECT max(ts) FROM meta_deliveries WHERE shop = %s AND source = 'browser'""", (shop,))
        facts["last_browser"] = cur.fetchone()[0]
        cur.execute("""SELECT client_version FROM meta_deliveries
                       WHERE shop = %s AND source = 'browser' AND ts > now() - interval '24 hours'
                       ORDER BY ts DESC LIMIT 1""", (shop,))
        row = cur.fetchone()
        facts["version"] = row[0] if row else None
        cur.execute("""SELECT count(*) FILTER (WHERE event_name = 'AddPaymentInfo' AND status BETWEEN 200 AND 299),
                              count(*) FILTER (WHERE restored IS NOT NULL)
                       FROM meta_deliveries WHERE shop = %s AND ts > now() - interval '7 days'""", (shop,))
        facts["apinfo_7d"], facts["restores_7d"] = cur.fetchone()
        cur.execute("""SELECT ts, event_name, source, status, error FROM meta_deliveries
                       WHERE shop = %s AND (status < 200 OR status > 299) AND ts > now() - interval '7 days'
                       ORDER BY ts DESC LIMIT 8""", (shop,))
        facts["errors"] = cur.fetchall()
        cur.execute("SELECT topic, last_at, total, last_result FROM webhook_receipts WHERE shop = %s", (shop,))
        facts["receipts"] = {t: {"last_at": a, "total": n, "result": r} for t, a, n, r in cur.fetchall()}
        try:
            cur.execute("SELECT count(*) FROM meta_identity WHERE shop = %s AND identified_at IS NOT NULL", (shop,))
            facts["identified"] = cur.fetchone()[0]
        except Exception:
            conn.rollback()
    return facts


def store_status(store, facts=None):
    """('ok' | 'setup' | 'problem', plain-language reason)."""
    if not store["enabled"]:
        return "setup", "Switched off: nothing is sent to Meta"
    if not store["pixel_id"] or not store["access_token"]:
        return "setup", "Add the Meta pixel ID and access token"
    if store["pixel_check_error"]:
        return "problem", "Meta rejected the pixel ID or access token"
    if not store["webhook_secret"]:
        return "setup", "Add the Shopify webhook signing key"
    try:
        facts = facts or store_facts(store["shop_domain"])
    except Exception:
        return "setup", "Status unavailable right now"
    if not facts["last_browser"]:
        return "setup", "Waiting for the tracking code on the theme"
    receipts = facts["receipts"]
    if not any(t.startswith("checkouts/") for t in receipts):
        return "setup", "Waiting for the checkout webhooks"
    if any(r["result"] == "bad_signature" for r in receipts.values()):
        return "problem", "Webhooks are arriving with the wrong signing key"
    if datetime.now(timezone.utc) - facts["last_browser"] > timedelta(hours=24):
        return "problem", "No events from the theme in the last 24 hours"
    if facts["total"] >= 20 and pct(facts["accepted"], facts["total"]) < 95:
        return "problem", "Meta is rejecting some events"
    return "ok", "Sending normally"


WEBHOOK_RESULTS = {
    "purchase_sent": "Purchase sent to Meta", "test_order_ignored": "Test order, not sent",
    "identity": "Shopper details saved", "sent": "Checkout identity sent to Meta",
    "already_sent": "Already sent for this checkout", "waiting": "Waiting for the shopper's browser",
    "send_failed": "Meta refused, will retry", "storage_down": "Storage unavailable",
    "bad_signature": "Wrong signing key", "invalid_body": "Unreadable message", "error": "Error, see server logs",
}


def readable_meta_error(text):
    """Meta's own explanation from an error response, not the raw JSON."""
    try:
        err = json.loads(text or "").get("error") or {}
        return err.get("error_user_msg") or err.get("message") or text
    except (ValueError, AttributeError):
        return text


def check_pixel(pixel_id, access_token):
    """(pixel name, None) when Meta accepts this token for this pixel, else (None, reason)."""
    try:
        r = requests.get(f"https://graph.facebook.com/v19.0/{pixel_id}",
                         params={"fields": "name", "access_token": access_token}, timeout=10)
        body = r.json()
    except (requests.RequestException, ValueError):
        return None, "Could not reach Meta to check. Try again in a minute."
    if r.status_code == 200 and body.get("name"):
        return body["name"], None
    message = (body.get("error") or {}).get("message") or f"Meta answered {r.status_code}"
    return None, message[:300]


# ============ STORE LIST ============

@bp.route("")
@bp.route("/")
@login_required
def home():
    rows = []
    for store in stores.list_stores():
        try:
            facts = store_facts(store["shop_domain"])
        except Exception:
            facts = {"total": 0, "purchases": 0, "last_browser": None}
        state, reason = store_status(store, facts if "receipts" in facts else None)
        rows.append(f"""<tr><td><a href="/hub/stores/{esc(store['id'])}"><b>{esc(store['name'])}</b></a><br>
<span class="muted">{esc(store['site_url'] or store['shop_domain'])}</span></td>
<td><span class="status {state}"><i class="dot {state}" aria-hidden="true"></i>{esc(reason)}</span></td>
<td class="num">{facts['total']:,}</td><td class="num">{facts['purchases']:,}</td>
<td>{ago(facts['last_browser'])}</td></tr>""")
    table = (f"""<div class="scroll"><table><thead><tr><th>Store</th><th>Status</th><th class="num">Events, 24h</th>
<th class="num">Purchases, 24h</th><th>Last theme event</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>"""
             if rows else '<div class="panel empty"><p>No stores yet.</p><a class="btn" href="/hub/stores/new">Add your first store</a></div>')
    return page("Tracking hub", f"""<h1>Stores</h1>
<p class="sub">Every store sends its events to its own Meta pixel. Green means events are flowing and Meta accepts them.</p>
{table}""")


# ============ ADD / EDIT ============

def read_store_form(existing=None):
    """Validated values from the store form, or (None, error)."""
    f = request.form
    values = {
        "name": f.get("name", "").strip(),
        "shop_domain": stores.shop_key(f.get("shop_domain", "")),
        "site_url": f.get("site_url", "").strip().rstrip("/"),
        "pixel_id": f.get("pixel_id", "").strip(),
        "access_token": f.get("access_token", "").strip(),
        "webhook_secret": f.get("webhook_secret", "").strip(),
        "default_country": f.get("default_country", "GB").strip().upper(),
        "currency": f.get("currency", "GBP").strip().upper(),
        "test_event_code": f.get("test_event_code", "").strip().upper() or None,
        "enabled": f.get("enabled") == "on" if existing else True,
    }
    if not 2 <= len(values["name"]) <= 60:
        return None, "Give the store a name (2 to 60 characters)."
    if not values["shop_domain"]:
        return None, "The Shopify address must look like yourstore.myshopify.com."
    site = urlparse(values["site_url"] if "://" in values["site_url"] else "https://" + values["site_url"])
    if not site.hostname or "." not in site.hostname:
        return None, "The website address must look like https://yourstore.com."
    values["site_url"] = f"https://{site.hostname}"
    if not stores.PIXEL_ID_RE.fullmatch(values["pixel_id"]):
        return None, "The pixel ID is the long number shown in Events Manager (digits only)."
    if values["access_token"] and not re.fullmatch(r"[A-Za-z0-9]{40,600}", values["access_token"]):
        return None, "That access token does not look right. Copy it again from Events Manager."
    if not existing and not values["access_token"]:
        return None, "Add the Conversions API access token."
    if values["webhook_secret"] and not re.fullmatch(r"[A-Za-z0-9_-]{16,200}", values["webhook_secret"]):
        return None, "That webhook signing key does not look right. Copy it again from Shopify."
    if not existing and not values["webhook_secret"]:
        return None, "Add the Shopify webhook signing key."
    if not stores.COUNTRY_RE.fullmatch(values["default_country"]):
        return None, "The country must be a two-letter code, like GB or US."
    if not stores.CURRENCY_RE.fullmatch(values["currency"]):
        return None, "The currency must be a three-letter code, like GBP or USD."
    if values["test_event_code"] and not stores.TEST_CODE_RE.fullmatch(values["test_event_code"]):
        return None, "The test event code looks like TEST12345 (from Events Manager, Test events)."
    return values, None


def store_form(store, error=""):
    s = store or {}
    new = store is None
    keep = "" if new else '<span>Leave empty to keep the one saved.</span>'
    country = esc(s.get("default_country") or "GB")
    currency = esc(s.get("currency") or "GBP")
    enabled = "" if new else f"""<label class="check"><input type="checkbox" name="enabled" {'checked' if s.get('enabled') else ''}>
Send this store's events to Meta</label>"""
    return f"""{f'<p class="note bad">{esc(error)}</p>' if error else ''}
<form class="fields" method="post" autocomplete="off">{csrf_field()}
<label>Store name<input type="text" name="name" value="{esc(s.get('name'))}" required maxlength="60"></label>
<div class="row">
<label>Shopify address<span>Admin, Settings, Domains: the one ending in .myshopify.com</span>
<input type="text" name="shop_domain" value="{esc(s.get('shop_domain'))}" placeholder="yourstore.myshopify.com" required></label>
<label>Website address<span>What shoppers see in the address bar</span>
<input type="text" name="site_url" value="{esc(s.get('site_url'))}" placeholder="https://yourstore.com" required></label>
</div>
<label>Meta pixel ID<span>Events Manager, your pixel (dataset), Settings</span>
<input type="text" name="pixel_id" value="{esc(s.get('pixel_id'))}" inputmode="numeric" required></label>
<label>Conversions API access token<span>Same Settings page, Conversions API, Generate access token. {keep}</span>
<input type="password" name="access_token" {'required' if new else ''}></label>
<label>Shopify webhook signing key<span>Shopify admin, Settings, Notifications, Webhooks: "Your webhooks will be signed with". {keep}</span>
<input type="password" name="webhook_secret" {'required' if new else ''}></label>
<div class="row">
<label>Main country<span>Two letters, used to read phone numbers</span>
<input type="text" name="default_country" value="{country}" maxlength="2" required></label>
<label>Currency<input type="text" name="currency" value="{currency}" maxlength="3" required></label>
</div>
<label>Test event code (optional)<span>Events Manager, Test events. Only the hub's test button uses it.</span>
<input type="text" name="test_event_code" value="{esc(s.get('test_event_code'))}" placeholder="TEST12345"></label>
{enabled}
<div><button class="btn">{'Add store' if new else 'Save changes'}</button></div>
</form>"""


def unique_store_id(name):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "store"
    taken = {s["id"] for s in stores.list_stores()}
    candidate, n = base, 2
    while candidate in taken or not stores.STORE_ID_RE.fullmatch(candidate):
        candidate, n = f"{base}-{n}"[:40], n + 1
    return candidate


@bp.route("/stores/new", methods=["GET", "POST"])
@login_required
def new_store():
    error = ""
    if request.method == "POST":
        check_csrf()
        values, error = read_store_form()
        if values and any(s["shop_domain"] == values["shop_domain"] for s in stores.list_stores()):
            values, error = None, "That Shopify store is already in the hub."
        if values:
            values["id"] = unique_store_id(values["name"])
            name, problem = check_pixel(values["pixel_id"], values["access_token"])
            try:
                stores.save_store(values)
            except stores.HubNotConfigured:
                return setup_needed_page()
            stores.record_pixel_check(values["id"], name, problem)
            return redirect(f"/hub/stores/{values['id']}?tab=setup")
    return page("Add a store", f"""<h1>Add a store</h1>
<p class="sub">About ten minutes: fill this in, paste the tracking code, add three webhooks. The Setup tab checks each step for you.</p>
{store_form(None, error)}""")


# ============ STORE PAGE ============

@bp.route("/stores/<store_id>", methods=["GET", "POST"])
@login_required
def store_page(store_id):
    store = stores.get_store(store_id)
    if not store:
        abort(404)
    notice = ""
    tab = request.args.get("tab")
    if request.method == "POST":  # Settings form
        check_csrf()
        values, error = read_store_form(existing=store)
        if values and values["shop_domain"] != store["shop_domain"] and any(
                s["shop_domain"] == values["shop_domain"] for s in stores.list_stores()):
            values, error = None, "That Shopify store is already in the hub."
        if not values:
            return page(store["name"], header(store, "settings") + store_form(store, error), active=store_id)
        try:
            stores.save_store(values, store_id)
        except stores.HubNotConfigured:
            return setup_needed_page()
        store = stores.get_store(store_id)
        name, problem = check_pixel(store["pixel_id"], store["access_token"])
        stores.record_pixel_check(store_id, name, problem)
        return redirect(f"/hub/stores/{store_id}?tab=settings&saved=1")
    try:
        facts = store_facts(store["shop_domain"])
    except Exception:
        facts = None
    state, _ = store_status(store, facts)
    tab = tab if tab in ("health", "funnel", "setup", "settings") else ("health" if state != "setup" else "setup")
    if request.args.get("saved"):
        notice = '<p class="note good">Saved.</p>'
    if tab == "settings":
        body = notice + store_form(store)
    elif tab == "setup":
        body = setup_view(store, facts)
    elif tab == "funnel":
        body = funnel_view(store)
    else:
        body = health_view(store, facts)
    return page(store["name"], header(store, tab, facts) + body, active=store_id)


def header(store, tab, facts=None):
    state, reason = store_status(store, facts)
    links = "".join(
        f'<a class="{"on" if tab == key else ""}" href="/hub/stores/{esc(store["id"])}?tab={key}">{label}</a>'
        for key, label in (("health", "Health"), ("funnel", "Funnel"), ("setup", "Setup"), ("settings", "Settings")))
    site = esc(store["site_url"] or "")
    return f"""<h1>{esc(store['name'])}</h1>
<p class="sub"><a href="{site}" rel="noopener" target="_blank">{site}</a>, pixel {esc(store['pixel_id'])}</p>
<span class="status {state}"><i class="dot {state}" aria-hidden="true"></i>{esc(reason)}</span>
<nav class="tabs" aria-label="Store sections">{links}</nav>"""


def health_view(store, facts):
    if facts is None:
        return '<p class="note bad">Health data could not be loaded right now. Reload in a minute.</p>'
    accepted = pct(facts["accepted"], facts["total"])
    figures = f"""<div class="figures">
<div><b>{facts['total']:,}</b><span>Events sent, 24h</span></div>
<div><b>{accepted if facts['total'] else '-'}{'%' if facts['total'] else ''}</b><span>Accepted by Meta</span></div>
<div><b>{facts['purchases']:,}</b><span>Purchases, 24h</span></div>
<div><b>{facts['apinfo_7d']:,}</b><span>Checkout identities sent, 7 days</span></div>
<div><b>{facts['restores_7d']:,}</b><span>Safari wipes undone, 7 days</span></div>
<div><b>{facts['identified']:,}</b><span>Visitors we can name</span></div></div>"""
    version = facts["version"]
    code_note = ""
    if facts["last_browser"] and version != SNIPPET_VERSION:
        code_note = (f'<p class="note">The theme is running {"an older version of" if version else "pre-hub"} tracking code. '
                     f'<a href="/hub/stores/{esc(store["id"])}?tab=setup">Copy the latest from Setup</a> to get every improvement.</p>')
    if facts["events"]:
        head = "".join(f"<th>{label}</th>" for _, label in KEY_COLUMNS)
        rows = ""
        for e in facts["events"]:
            cells = ""
            for (key, label), n in zip(KEY_COLUMNS, e["keys"]):
                share = pct(n, e["count"])
                ink = "var(--accent-ink)" if share >= 55 else "var(--ink)"
                cells += (f'<td class="cell"><div style="--c:{share}%;--cell-ink:{ink}" '
                          f'title="{esc(label)}: {share}% of {esc(e["name"])} events">{share}%</div></td>')
            rows += (f'<tr><td class="event">{esc(e["name"])}</td><td class="num">{e["count"]:,}</td>'
                     f'<td class="num">{pct(e["ok"], e["count"])}%</td>{cells}</tr>')
        grid = f"""<h2>What Meta can match on</h2>
<p class="muted">Each square is the share of that event carrying the key, over the last 24 hours. Darker is better.</p>
<div class="scroll"><table class="grid"><thead><tr><th>Event</th><th class="num">Sent</th><th class="num">Accepted</th>{head}</tr></thead>
<tbody>{rows}</tbody></table></div>
<p class="legend">0% <i aria-hidden="true"></i> 100%</p>"""
    else:
        grid = '<div class="panel empty">No events in the last 24 hours yet. Once the tracking code is on the theme they appear here within a minute.</div>'
    hooks = ""
    for topic, label in (("orders/create", "Orders"), ("checkouts/create", "Checkout started"),
                         ("checkouts/update", "Checkout details")):
        r = facts["receipts"].get(topic)
        hooks += (f"<tr><td>{label}</td><td>{ago(r['last_at']) if r else 'never'}</td>"
                  f"<td class='num'>{r['total'] if r else 0:,}</td>"
                  f"<td>{esc(WEBHOOK_RESULTS.get((r or {}).get('result'), (r or {}).get('result') or '-'))}</td></tr>")
    errors = ""
    if facts["errors"]:
        errors = ("<h2>Recent Meta errors</h2><div class='scroll'><table><thead><tr><th>When</th><th>Event</th>"
                  "<th>From</th><th>Status</th><th>Message</th></tr></thead><tbody>")
        for ts, name, source, status, message in facts["errors"]:
            errors += (f"<tr><td>{ago(ts)}</td><td>{esc(name)}</td><td>{esc(source)}</td><td>{esc(status)}</td>"
                       f"<td style='white-space:normal'>{esc(readable_meta_error(message)[:200])}</td></tr>")
        errors += "</tbody></table></div>"
    return f"""{code_note}{figures}{grid}
<h2>Shopify webhooks</h2><div class="scroll"><table><thead><tr><th>Webhook</th><th>Last received</th><th class="num">Total</th><th>Last result</th></tr></thead>
<tbody>{hooks}</tbody></table></div>{errors}"""


def setup_view(store, facts):
    facts = facts or {"receipts": {}, "last_browser": None, "version": None}
    host = public_host()
    sid = esc(store["id"])
    receipts = facts["receipts"]

    def step(done, title, state_text, body, waiting=True, optional=False):
        kind = "done" if done else ("neutral" if optional else ("wait" if waiting else "bad"))
        return (f'<li class="{kind}"><div><h3>{title}</h3><p class="state {kind}">{state_text}</p>'
                f"{body}</div></li>")

    pixel_ok = bool(store["pixel_name"]) and not store["pixel_check_error"]
    if pixel_ok:
        pixel_state = f"Connected to “{esc(store['pixel_name'])}”, checked {ago(store['pixel_checked_at'])}."
    elif store["pixel_check_error"]:
        pixel_state = f"Meta said: {esc(store['pixel_check_error'])}"
    else:
        pixel_state = "Not checked yet."
    pixel_step = step(pixel_ok, "Meta pixel and access token", pixel_state, f"""
<form method="post" action="/hub/stores/{sid}/check-pixel">{csrf_field()}<button class="btn quiet small">Check again</button></form>""",
                      waiting=not store["pixel_check_error"])

    code = render_snippet(store, f"https://{host}/track")
    seen = facts["last_browser"]
    current = facts.get("version") == SNIPPET_VERSION
    if seen and current:
        code_state = f"Running the latest code. Last event {ago(seen)}."
    elif seen:
        code_state = f"Events are arriving (last {ago(seen)}), but the theme has older code. Paste this version over it."
    else:
        code_state = "No events from the theme yet."
    code_step = step(bool(seen) and current, "Tracking code on the theme", code_state, f"""
<p>Shopify admin, Online Store, Themes, Edit code, <b>layout/theme.liquid</b>. Paste this right after the
<code>&lt;head&gt;</code> line and save. Remove any other Meta pixel code first, so the pixel does not load twice.</p>
<textarea class="code" id="snippet" readonly spellcheck="false">{esc(code)}</textarea>
<p><button class="btn small" type="button" data-copy="snippet">Copy code</button>
<a class="btn quiet small" href="/hub/stores/{sid}/snippet.liquid">Download</a></p>""")

    def hook_row(topic, label):
        r = receipts.get(topic)
        if r and r["result"] == "bad_signature":
            return f"<li>{label}: arriving with the wrong signing key. Check the key in Settings.</li>"
        return f"<li>{label}: {'received ' + ago(r['last_at']) if r else 'not received yet'}</li>"

    orders = receipts.get("orders/create")
    order_ok = bool(orders) and orders["result"] != "bad_signature"
    checkout_ok = any(t.startswith("checkouts/") and r["result"] != "bad_signature" for t, r in receipts.items())
    hooks_step = step(order_ok and checkout_ok, "Shopify webhooks",
                      "All three are arriving." if order_ok and checkout_ok else
                      "Waiting for Shopify. Use “Send test” on each webhook to check right away.", f"""
<p>Shopify admin, Settings, Notifications, Webhooks. Create three webhooks, format JSON, newest API version:</p>
<div class="copy"><code id="u1">https://{esc(host)}/webhook/order-created</code><button class="btn quiet small" type="button" data-copy="u1">Copy</button></div>
<p class="muted">Event: Order creation</p>
<div class="copy"><code id="u2">https://{esc(host)}/webhook/checkout</code><button class="btn quiet small" type="button" data-copy="u2">Copy</button></div>
<p class="muted">Events: Checkout creation, and again for Checkout update (same address)</p>
<ul class="muted">{hook_row('orders/create', 'Orders')}{hook_row('checkouts/create', 'Checkout creation')}{hook_row('checkouts/update', 'Checkout update')}</ul>
<p class="muted">Shopify's “Send test” and orders paid with the test gateway show here as received but are never sent to Meta as purchases, so no fake revenue reaches the pixel.</p>""")

    test_body = (f"""<p>Sends one PageView to Meta's Test events tab for code {esc(store['test_event_code'])}.
Open Events Manager, your pixel, Test events, then press the button.</p>
<form method="post" action="/hub/stores/{sid}/test-event">{csrf_field()}<button class="btn quiet small">Send test event</button></form>"""
                 if store["test_event_code"] else
                 f"""<p>Optional. Add the test event code from Events Manager (Test events) in
<a href="/hub/stores/{sid}?tab=settings">Settings</a> to send a test event from here.</p>""")
    result = request.args.get("test")
    if result:
        test_body = f'<p class="note {"good" if result == "ok" else "bad"}">{esc(request.args.get("msg", ""))}</p>' + test_body
    test_step = step(result == "ok", "Test event", "Sent." if result == "ok" else "Optional.", test_body,
                     optional=True)
    return f"""<ol class="steps">{pixel_step}{code_step}{hooks_step}{test_step}</ol>"""


SNIPPET_TEMPLATE = r'''{%- comment -%}
  Tracking hub snippet v__VERSION__ for __STORE_NAME__.
  Paste into layout/theme.liquid, right after <head>. Generated by the hub:
  re-paste from the hub's Setup tab when it says the code is out of date.
{%- endcomment -%}
{%- unless request.design_mode -%}
<!-- Identity restore: runs before Shopify's scripts and the Meta pixel.
     Safari deletes script-set storage after 7 days without a visit, which
     turns a returning shopper into a stranger. Shopify's cart cookie is set
     by Shopify's own server, so it outlives that, and the cart carries the
     visitor ID and Meta browser IDs (written by the tracking block below).
     Anything missing is put back from there, so the pixel keeps the same
     _fbp. If the pixel never creates one (ad blocker), the block below
     makes one, the way Meta allows for server-side setups. -->
<script>
(function () {
  try {
    var VID = /^v_[a-z0-9]{6,40}$/;
    var FBP = /^fb\.\d\.\d{10,16}\.\d{1,30}$/;
    var FBC = /^fb\.\d\.\d{10,16}\.[A-Za-z0-9_-]{10,600}$/;
    // Cart attributes can be set by anyone with a cart link, so they are
    // HTML-escaped here and only used if they match the exact formats.
    var saved = {
      vid: {{ cart.attributes['_comfi_vid'] | escape | json }},
      fbp: {{ cart.attributes['_fbp'] | escape | json }},
      fbc: {{ cart.attributes['_fbc'] | escape | json }}
    };
    window.comfiCartIds = saved;
    function valid(re, v) { return typeof v === "string" && re.test(v); }
    function recentClick(fbc) {
      var ms = Number(fbc.split(".")[2]);
      if (ms < 1e12) ms *= 1000;
      return Date.now() - ms < 90 * 864e5;
    }
    function getCookie(name) {
      var m = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
      return m ? decodeURIComponent(m[1]) : null;
    }
    // The registrable domain (yourstore.com), found the way Meta's and
    // Google's scripts do it: the shortest domain that accepts a cookie.
    var parts = location.hostname.split("."), domain = location.hostname;
    for (var i = parts.length - 2; i >= 0; i--) {
      var d = parts.slice(i).join(".");
      document.cookie = "comfi_d=1; path=/; domain=" + d;
      if (getCookie("comfi_d")) {
        document.cookie = "comfi_d=; max-age=0; path=/; domain=" + d;
        domain = d;
        break;
      }
    }
    function setCookie(name, value, days) {
      document.cookie = name + "=" + value + "; max-age=" + Math.round(days * 86400)
        + "; path=/; domain=" + domain + "; SameSite=Lax"
        + (location.protocol === "https:" ? "; Secure" : "");
    }
    window.comfiSetCookie = setCookie;
    var ls = window.localStorage, restored = [];

    if (!valid(VID, ls.getItem("comfi_vid")) && valid(VID, saved.vid)) {
      ls.setItem("comfi_vid", saved.vid);
      restored.push("vid");
    }

    if (!valid(FBP, getCookie("_fbp"))) {
      var fbp = valid(FBP, ls.getItem("comfi_fbp")) ? ls.getItem("comfi_fbp")
        : (valid(FBP, saved.fbp) ? saved.fbp : "");
      if (fbp) {
        setCookie("_fbp", fbp, 90);
        ls.setItem("comfi_fbp", fbp);
        restored.push("fbp");
      }
    }

    // A new ad click in the URL always wins; Meta's pixel records it itself.
    if (!new URLSearchParams(location.search).get("fbclid")) {
      var cookieFbc = getCookie("_fbc");
      if (cookieFbc && !valid(FBC, cookieFbc)) {
        // Junk like "fb.1.1779838569048.fbclid" from an old script: remove it.
        document.cookie = "_fbc=; max-age=0; path=/; domain=" + domain;
        document.cookie = "_fbc=; max-age=0; path=/";
        cookieFbc = null;
      }
      // Meta's own _fbc cookie lasts 90 days; a stored copy older than that
      // (or junk) is dropped rather than sent as a stale click.
      var storedFbc = ls.getItem("comfi_fbc");
      if (!valid(FBC, storedFbc) || !recentClick(storedFbc)) {
        ls.removeItem("comfi_fbc");
        ls.removeItem("comfi_fbclid");
      }
      if (!cookieFbc) {
        var fbc = valid(FBC, ls.getItem("comfi_fbc")) ? ls.getItem("comfi_fbc")
          : (valid(FBC, saved.fbc) ? saved.fbc : "");
        if (fbc && recentClick(fbc)) {
          setCookie("_fbc", fbc, 90);
          ls.setItem("comfi_fbc", fbc);
          ls.setItem("comfi_fbclid", fbc.split(".").slice(3).join("."));
          restored.push("fbc");
        }
      }
    }
    window.comfiRestored = restored.join(",");
  } catch (e) {}
})();
</script>

<!-- Meta Pixel Base Code -->
<script>
!function(f,b,e,v,n,t,s)
{if(f.fbq)return;n=f.fbq=function(){n.callMethod?
n.callMethod.apply(n,arguments):n.queue.push(arguments)};
if(!f._fbq)f._fbq=n;n.push=n;n.loaded=!0;n.version='2.0';
n.queue=[];t=b.createElement(e);t.async=!0;
t.src=v;s=b.getElementsByTagName(e)[0];
s.parentNode.insertBefore(t,s)}(window, document,'script',
'https://connect.facebook.net/en_US/fbevents.js');
// Advanced matching: only fields that actually exist. Guests used to get
// em: null / ph: null here, which Meta flags as broken advanced matching.
(function () {
  function clean(v) { return v == null ? "" : String(v).trim().toLowerCase(); }
  function phone(v, country) {
    var d = clean(v).replace(/[^\d]/g, "");
    if (!d) return "";
    if (country === "us" || country === "ca") return d.length === 10 ? "1" + d : d;
    if (d.indexOf("44") === 0) return d;
    if (d.indexOf("0") === 0) return "44" + d.slice(1);
    return d;
  }
  var raw = {% if customer %}{
    em: {{ customer.email | json }},
    ph: {{ customer.phone | json }},
    fn: {{ customer.first_name | json }},
    ln: {{ customer.last_name | json }},
    ct: {{ customer.default_address.city | json }},
    zp: {{ customer.default_address.zip | json }},
    st: {{ customer.default_address.province_code | json }},
    country: {{ customer.default_address.country_code | json }}
  }{% else %}{}{% endif %};
  var m = {};
  var country = clean(raw.country || {{ localization.country.iso_code | json }});
  if (country) m.country = country;
  if (clean(raw.em)) m.em = clean(raw.em);
  if (phone(raw.ph, country)) m.ph = phone(raw.ph, country);
  function name(v) { return clean(v).replace(/[^a-zà-ɏ]/g, ""); }
  if (name(raw.fn)) m.fn = name(raw.fn);
  if (name(raw.ln)) m.ln = name(raw.ln);
  if (clean(raw.ct).replace(/[^a-z]/g, "")) m.ct = clean(raw.ct).replace(/[^a-z]/g, "");
  if (clean(raw.st).replace(/[^a-z]/g, "")) m.st = clean(raw.st).replace(/[^a-z]/g, "");
  var zp = clean(raw.zp).replace(/[\s-]/g, "");
  if (country === "us") zp = zp.slice(0, 5);
  if (zp) m.zp = zp;
  window.comfiMatch = m;
  fbq('init', '__PIXEL_ID__', m);
})();
</script>
<noscript><img height="1" width="1" style="display:none"
src="https://www.facebook.com/tr?id=__PIXEL_ID__&ev=PageView&noscript=1"
/></noscript>
<!-- End Meta Pixel Base Code -->

<!-- Tracking: visitor ID, click and browser IDs, browser + server events -->
<script>
(function () {
  try {
    // ── Utility ──
    function getCookie(name) {
      var pattern = new RegExp("(?:^|; )" + name + "=([^;]*)");
      var match = document.cookie.match(pattern);
      return match ? decodeURIComponent(match[1]) : null;
    }

    // ── Logged-in customer data (Liquid-injected, only exists if customer is logged in) ──
    var comfiCustomer = {
      email: {{ customer.email | json | default: 'null' }},
      phone: {{ customer.phone | json | default: 'null' }}
    };

    // ── Active presentment currency (US FIX: follows Shopify Markets, no more hardcoded GBP) ──
    var comfiCurrency = {{ cart.currency.iso_code | json }};

    // ── Real values and product IDs (were hardcoded value 0, no product ID) ──
    // Prices are in minor units (pence/cents). Product IDs match Purchase.
    var comfiProduct = {% if template.name == 'product' and product %}{
      id: {{ product.id | json }},
      price: {{ product.selected_or_first_available_variant.price | json }}
    }{% else %}null{% endif %};
    var comfiCart = {
      value: {{ cart.total_price | json }},
      ids: {{ cart.items | map: 'product_id' | json }},
      count: {{ cart.item_count | json }}
    };
    function comfiMoney(minor) { return Math.round(Number(minor) || 0) / 100; }
    function comfiCartData() {
      var ids = (comfiCart.ids || []).map(String).filter(function (id, i, all) {
        return id && all.indexOf(id) === i;
      });
      var data = { value: comfiMoney(comfiCart.value), currency: comfiCurrency };
      if (ids.length) { data.content_ids = ids; data.content_type = "product"; }
      if (comfiCart.count) data.num_items = comfiCart.count;
      return data;
    }

    // ── 0. Visitor ID (our own, does not depend on Facebook cookies) ──
    var comfiVid = localStorage.getItem("comfi_vid");
    if (!comfiVid) {
      comfiVid = "v_" + Math.random().toString(36).substring(2) + Date.now().toString(36);
      localStorage.setItem("comfi_vid", comfiVid);
    }

    // ── 1. Capture fbc + fbclid + utm_source immediately ──
    // Only well-formed IDs: a junk _fbc ("fb.1.<time>.fbclid") was being
    // copied into storage and the cart and sent to Meta as a click.
    var FBC_OK = /^fb\.\d\.\d{10,16}\.([A-Za-z0-9_-]{10,600})$/;
    var FBP_OK = /^fb\.\d\.\d{10,16}\.\d{1,30}$/;
    var CLICK_OK = /^[A-Za-z0-9_-]{10,600}$/;
    var existingFbc = getCookie("_fbc");
    var fbcMatch = existingFbc && existingFbc.match(FBC_OK);
    var fbclid = new URLSearchParams(window.location.search).get("fbclid");
    if (fbclid && !CLICK_OK.test(fbclid)) fbclid = null;
    if (fbclid) {
      localStorage.setItem("comfi_fbclid", fbclid);
      localStorage.setItem("comfi_fbc", fbcMatch && fbcMatch[1] === fbclid
        ? existingFbc : "fb.1." + Date.now() + "." + fbclid);
    } else if (fbcMatch) {
      localStorage.setItem("comfi_fbc", existingFbc);
      localStorage.setItem("comfi_fbclid", fbcMatch[1]);
    }

    var utmSource = null;
    var _uparts = window.location.search.slice(1).split("&");
    for (var _ui = 0; _ui < _uparts.length; _ui++) {
      var _ueq = _uparts[_ui].indexOf("=");
      if (_ueq !== -1 && _uparts[_ui].slice(0, _ueq) === "utm_source") {
        utmSource = _uparts[_ui].slice(_ueq + 1);
        break;
      }
    }
    if (utmSource) {
      localStorage.setItem("comfi_utm_source", utmSource);
    }

    // ── 2. Capture fbp with retry polling ──
    function refreshFbp() {
      var fbp = getCookie("_fbp");
      if (fbp) localStorage.setItem("comfi_fbp", fbp);
      return fbp;
    }
    refreshFbp();
    var fbpAttempts = 0;
    var fbpMaxAttempts = 10;
    var fbpInterval = setInterval(function () {
      fbpAttempts++;
      var fbpVal = refreshFbp();
      if (fbpVal) {
        clearInterval(fbpInterval);
        writeCartAttributes();
      } else if (fbpAttempts >= fbpMaxAttempts) {
        clearInterval(fbpInterval);
        // Still no _fbp after 5s: Meta's pixel is blocked or failed. Make
        // one in Meta's documented format (fb.1.<time>.<random>) so this
        // browser's server events still link up; the pixel adopts it if it
        // loads later.
        var madeFbp = "fb.1." + Date.now() + "." + Math.floor(1e9 + Math.random() * 9e9);
        if (window.comfiSetCookie) window.comfiSetCookie("_fbp", madeFbp, 90);
        localStorage.setItem("comfi_fbp", madeFbp);
        writeCartAttributes();
      }
    }, 500);

    // ── 3. Write cart attributes ──
    window.comfiWriteCartAttributes = writeCartAttributes;
    var cartWrite = Promise.resolve();
    function writeCartAttributes() {
      // Serialize these small writes; capture current identifiers when each runs.
      cartWrite = cartWrite.then(function () {
        refreshFbp();
        var fbc = localStorage.getItem("comfi_fbc");
        var fbp = localStorage.getItem("comfi_fbp");
        var fbclid = localStorage.getItem("comfi_fbclid");
        var utm = localStorage.getItem("comfi_utm_source");

        var attributes = {};
        attributes["_comfi_vid"] = comfiVid;
        var cartIds = window.comfiCartIds || {};
        if (fbc && FBC_OK.test(fbc)) attributes["_fbc"] = fbc;
        else if (cartIds.fbc) attributes["_fbc"] = "";  // clears junk left on the cart
        if (fbp && FBP_OK.test(fbp)) attributes["_fbp"] = fbp;
        if (fbclid && CLICK_OK.test(fbclid)) attributes["_fbclid"] = fbclid;
        if (utm) attributes["_utm_source"] = utm;
        if (typeof window.comfiGoogleCartAttributes === "function") {
          try { Object.assign(attributes, window.comfiGoogleCartAttributes()); } catch (e) {}
        }

        var updateUrl = window.routes && window.routes.cart_update_url;
        if (!updateUrl) updateUrl = {{ routes.cart_update_url | json }};
        return fetch(updateUrl, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ attributes: attributes }),
          keepalive: true
        }).then(function (response) {
          if (!response.ok) throw new Error("Cart attribution update failed");
          return response.json();
        }).then(function (cart) {
          return Object.keys(attributes).every(function (key) {
            return cart.attributes && (cart.attributes[key] === attributes[key]
              || (attributes[key] === "" && !(key in cart.attributes)));
          });
        });
      }).catch(function () {
        return false;
      });
      return cartWrite;
    }

    // ── 4. Browser CAPI relay ──
    function sendCapiEvent(eventName, data) {
      refreshFbp();
      var eventId = Math.random().toString(36).substring(2) + Date.now().toString(36);
      var fbc = localStorage.getItem("comfi_fbc") || "";
      var fbp = localStorage.getItem("comfi_fbp") || "";
      var fbclid = localStorage.getItem("comfi_fbclid") || "";

      if (typeof fbq !== "undefined") {
        fbq("track", eventName, data || {}, { eventID: eventId });
      }

      var payload = {
        event_name: eventName,
        event_id: eventId,
        vid: comfiVid,
        fbc: fbc,
        fbp: fbp,
        fbclid: fbclid,
        source_url: window.location.href,
        referrer: document.referrer || "",
        value: data ? data.value : null,
        currency: comfiCurrency
      };
      if (data && data.content_ids) {
        payload.content_ids = data.content_ids;
        payload.content_type = data.content_type;
        if (data.contents) payload.contents = data.contents;
        if (data.num_items) payload.num_items = data.num_items;
      }
      if (window.comfiMatch && window.comfiMatch.country) payload.country = window.comfiMatch.country;
      // Which store this is (the hub sends the event to this store's pixel)
      // and which version of this code is installed.
      payload.shop = {{ shop.permanent_domain | json }};
      payload.v = __VERSION__;
      // Once per page: which IDs were brought back from the cart, so the
      // server logs show how often Safari's wipe was undone.
      if (eventName === "PageView" && window.comfiRestored) payload.restored = window.comfiRestored;

      // Attach logged-in customer data if available
      if (comfiCustomer.email) payload.email = comfiCustomer.email;
      if (comfiCustomer.phone) payload.phone = comfiCustomer.phone;

      // text/plain makes this a "simple" request: no CORS preflight, so it
      // is one request instead of two and still lands when the page is
      // navigating away (checkout clicks).
      fetch("__TRACK_URL__", {
        method: "POST",
        headers: { "Content-Type": "text/plain" },
        body: JSON.stringify(payload),
        keepalive: true
      }).catch(function () {});
    }
    window.comfiSendCapiEvent = sendCapiEvent;

    // ── 5. Fire standard events ──

    sendCapiEvent("PageView");

    if (comfiProduct) {
      sendCapiEvent("ViewContent", {
        value: comfiMoney(comfiProduct.price),
        currency: comfiCurrency,
        content_ids: [String(comfiProduct.id)],
        content_type: "product",
        contents: [{ id: String(comfiProduct.id), quantity: 1 }]
      });
    } else if (window.location.pathname.indexOf("/products/") !== -1) {
      sendCapiEvent("ViewContent", { value: 0, currency: comfiCurrency });
    }

    // Keep comfiCart current after adds, so InitiateCheckout sends the real total.
    function refreshCart() {
      var cartUrl = (window.routes && window.routes.cart_url) || {{ routes.cart_url | json }};
      return originalFetch(cartUrl.replace(/\/$/, "") + ".js", { credentials: "same-origin" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (cart) {
          if (!cart) return false;
          comfiCart = {
            value: cart.total_price,
            ids: (cart.items || []).map(function (item) { return item.product_id; }),
            count: cart.item_count
          };
          return true;
        }).catch(function () { return false; });
    }

    // AddToCart — fetch interception
    var originalFetch = window.fetch;
    var pendingCartAdds = [];
    window.fetch = function () {
      var url = arguments[0];
      var result = originalFetch.apply(this, arguments);
      if (typeof url === "string" && url.indexOf("/cart/add") !== -1) {
        var pending = result.then(function (response) {
          if (!response.ok) return;
          // Read the added line(s) from a copy, so the theme still gets the original.
          var added = response.clone().json().catch(function () { return null; });
          return added.then(function (body) {
            var items = body ? (body.items || [body]) : [];
            var value = 0, ids = [], contents = [], qty = 0;
            items.forEach(function (item) {
              if (!item || !item.product_id) return;
              var q = item.quantity || 1;
              value += item.final_line_price != null ? item.final_line_price : (item.price || 0) * q;
              ids.push(String(item.product_id));
              contents.push({ id: String(item.product_id), quantity: q });
              qty += q;
            });
            if (!ids.length && comfiProduct) {
              value = comfiProduct.price;
              ids = [String(comfiProduct.id)];
              contents = [{ id: ids[0], quantity: 1 }];
              qty = 1;
            }
            // Shopify returns the line's total quantity when the product was
            // already in the cart, so the exact amount added is the change in
            // the cart total. Fall back to the response if the cart can't be read.
            var before = comfiCart.value;
            return refreshCart().then(function (ok) {
              var delta = ok ? comfiCart.value - before : 0;
              if (delta > 0) value = delta;
              if (!ok) {
                // Cart unreadable: keep our running total from what was added.
                comfiCart.value = (Number(comfiCart.value) || 0) + value;
                comfiCart.ids = (comfiCart.ids || []).concat(ids);
                comfiCart.count = (comfiCart.count || 0) + qty;
              }
              var data = { value: comfiMoney(value), currency: comfiCurrency };
              if (ids.length) {
                data.content_ids = ids;
                data.content_type = "product";
                data.contents = contents;
                data.num_items = qty;
              }
              try { sendCapiEvent("AddToCart", data); } catch (e) {}
              return writeCartAttributes();
            });
          });
        }).catch(function () {});
        pendingCartAdds.push(pending);
        pending.then(function () {
          pendingCartAdds.splice(pendingCartAdds.indexOf(pending), 1);
        });
      }
      return result;
    };

    // Normal checkout only: retain native forms and leave wallet buttons alone.
    var checkoutPending = false;
    var resumedForm = null;
    var resumedEvent = null;
    function prepareCheckout(continueCheckout) {
      if (checkoutPending) return;
      checkoutPending = true;
      var continued = false;
      function finish() {
        if (continued) return;
        continued = true;
        clearTimeout(timeout);
        continueCheckout();
      }
      var timeout = setTimeout(finish, 1500);
      // Read the live cart first (catches drawer quantity changes and removals),
      // but never wait more than 600ms: send with the last known cart instead.
      var icSent = false;
      var icDone = new Promise(function (resolve) {
        function sendIC() {
          if (icSent) return;
          icSent = true;
          try { sendCapiEvent("InitiateCheckout", comfiCartData()); } catch (e) {}
          resolve();
        }
        var icTimer = setTimeout(sendIC, 600);
        Promise.all(pendingCartAdds.slice()).then(refreshCart).then(function () {
          clearTimeout(icTimer);
          sendIC();
        }, sendIC);
      });
      Promise.all(pendingCartAdds.slice().concat([icDone])).then(writeCartAttributes).then(finish, finish);
    }
    window.addEventListener("pageshow", function (e) { if (e.persisted) checkoutPending = false; });
    document.addEventListener("submit", function (e) {
      var form = e.target;
      if (form === resumedForm) {
        resumedEvent = e;
        return;
      }
      if (e.defaultPrevented || typeof form.requestSubmit !== "function") return;
      var submitter = e.submitter;
      if (!submitter && /^(cart|CartDrawer-Form|cart-notification-form)$/.test(form.id)) {
        submitter = form.elements.namedItem("checkout");
      }
      if (!submitter || submitter.name !== "checkout") return;
      var action = new URL(form.action, window.location.href);
      if (action.origin !== window.location.origin || !/\/(?:cart|checkout)\/?$/.test(action.pathname)) return;
      e.preventDefault();
      prepareCheckout(function () {
        // Cart section rendering may have replaced the form while we waited.
        var currentForm = form.isConnected ? form : document.getElementById(form.id);
        var currentSubmitter = submitter.isConnected ? submitter
          : currentForm && currentForm.elements.namedItem("checkout");
        if (!currentForm || !currentSubmitter) { checkoutPending = false; return; }
        resumedForm = currentForm;
        resumedEvent = null;
        try {
          currentForm.requestSubmit(currentSubmitter);
        } catch (e) {
          checkoutPending = false;
        } finally {
          resumedForm = null;
          if (!resumedEvent || resumedEvent.defaultPrevented) checkoutPending = false;
        }
      });
    });
    document.addEventListener("click", function(e) {
      var target = e.target.closest && e.target.closest('a[href]');
      if (!target || e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
      if (target.hasAttribute("download") || (target.target && target.target !== "_self")) return;
      var destination = new URL(target.href, window.location.href);
      if (destination.origin !== window.location.origin || !/\/(?:checkout|checkouts)(?:\/|$)/.test(destination.pathname)) return;
      e.preventDefault();
      prepareCheckout(function () { window.location.assign(destination.href); });
    });

  } catch (e) {
    console.error("Tracking error:", e);
  }
})();
</script>
<!-- End tracking -->
{%- endunless -%}
'''


def render_snippet(store, track_url):
    name = re.sub(r"[^A-Za-z0-9 &'.-]", "", store["name"] or "")[:60]
    return (SNIPPET_TEMPLATE.replace("__PIXEL_ID__", store["pixel_id"] or "")
            .replace("__TRACK_URL__", track_url)
            .replace("__VERSION__", str(SNIPPET_VERSION))
            .replace("__STORE_NAME__", name))


@bp.route("/stores/<store_id>/snippet.liquid")
@login_required
def snippet_download(store_id):
    store = stores.get_store(store_id) or abort(404)
    return Response(render_snippet(store, f"https://{public_host()}/track"), mimetype="text/plain",
                    headers={"Content-Disposition": f'attachment; filename="tracking-{store["id"]}.liquid"'})


@bp.route("/stores/<store_id>/check-pixel", methods=["POST"])
@login_required
def recheck_pixel(store_id):
    check_csrf()
    store = stores.get_store(store_id) or abort(404)
    name, problem = (check_pixel(store["pixel_id"], store["access_token"]) if store["access_token"]
                     else (None, "No access token saved."))
    stores.record_pixel_check(store_id, name, problem)
    return redirect(f"/hub/stores/{store_id}?tab=setup")


@bp.route("/stores/<store_id>/test-event", methods=["POST"])
@login_required
def test_event(store_id):
    check_csrf()
    store = stores.get_store(store_id) or abort(404)
    if not store["test_event_code"]:
        return redirect(f"/hub/stores/{store_id}?tab=setup&test=bad&msg=Add+a+test+event+code+first.")
    try:
        response = _send_capi_event(
            event_name="PageView", event_id=f"hubtest_{int(time.time())}",
            source_url=store["site_url"], client_ip=_client_ip(),
            user_agent=request.headers.get("User-Agent", "")[:400], external_id="hub-test",
            country=store["default_country"], pixel_id=store["pixel_id"],
            access_token=store["access_token"], test_event_code=store["test_event_code"],
            log={"shop": store["shop_domain"], "source": "test"})
        ok = 200 <= response.status_code < 300
        try:
            body = response.json()
        except ValueError:
            body = {}
        msg = ("Meta received it. It shows in Test events within a few seconds." if ok else
               f"Meta refused it: {((body.get('error') or {}).get('message') or response.status_code)}")
    except Exception as e:
        ok, msg = False, f"Could not reach Meta ({type(e).__name__}). Try again."
    from urllib.parse import quote
    return redirect(f"/hub/stores/{store_id}?tab=setup&test={'ok' if ok else 'bad'}&msg={quote(str(msg)[:250])}")


# ============ FUNNEL ============

SESSION_SQL = """
WITH ordered AS (
    SELECT vid, ts, event_name, path, ad_id, value,
           CASE WHEN lag(ts) OVER (PARTITION BY vid ORDER BY ts) IS NULL
                  OR ts - lag(ts) OVER (PARTITION BY vid ORDER BY ts) > interval '30 minutes'
                THEN 1 ELSE 0 END AS is_new
    FROM events
    WHERE ts >= now() - (%(days)s || ' days')::interval
      AND vid IS NOT NULL AND shop = %(shop)s
),
numbered AS (SELECT *, sum(is_new) OVER (PARTITION BY vid ORDER BY ts) AS sess_no FROM ordered),
sessions AS (
    SELECT vid, sess_no, min(ts) AS started,
           (array_agg(ad_id ORDER BY ts) FILTER (WHERE ad_id IS NOT NULL))[1] AS ad_id,
           (array_agg(path ORDER BY ts))[1] AS landing,
           bool_or(event_name = 'ViewContent' OR path LIKE '/products/%%') AS pdp,
           bool_or(event_name = 'AddToCart') AS atc,
           bool_or(event_name = 'InitiateCheckout') AS ic,
           bool_or(event_name = 'Purchase') AS purchase,
           coalesce(max(value) FILTER (WHERE event_name = 'Purchase'), 0) AS revenue
    FROM numbered GROUP BY vid, sess_no
)
"""


def funnel_view(store):
    try:
        days = max(1, min(int(request.args.get("days", 7)), 90))
    except ValueError:
        days = 7
    params = {"days": str(days), "shop": store["shop_domain"]}
    cur_sign = store["currency"]
    try:
        with stores.db() as conn, conn.cursor() as cur:
            cur.execute(SESSION_SQL + """SELECT count(*), count(*) FILTER (WHERE pdp), count(*) FILTER (WHERE atc),
                count(*) FILTER (WHERE ic), count(*) FILTER (WHERE purchase), coalesce(sum(revenue), 0) FROM sessions""", params)
            ses, pdp, atc, ic, pur, rev = cur.fetchone()
            cur.execute(SESSION_SQL + """SELECT coalesce(ad_id, 'No ad ID'), count(*), count(*) FILTER (WHERE pdp),
                count(*) FILTER (WHERE atc), count(*) FILTER (WHERE ic), count(*) FILTER (WHERE purchase),
                coalesce(sum(revenue), 0) FROM sessions GROUP BY 1 ORDER BY 2 DESC LIMIT 25""", params)
            per_ad = cur.fetchall()
            cur.execute(SESSION_SQL + """SELECT to_char(date_trunc('day', started), 'Dy DD Mon'), count(*),
                count(*) FILTER (WHERE pdp), count(*) FILTER (WHERE atc), count(*) FILTER (WHERE purchase),
                coalesce(sum(revenue), 0) FROM sessions GROUP BY date_trunc('day', started)
                ORDER BY date_trunc('day', started) DESC""", params)
            daily = cur.fetchall()
    except Exception:
        return '<p class="note bad">The funnel could not be loaded right now. Reload in a minute.</p>'
    sid = esc(store["id"])
    ranges = "".join(
        f'<a href="/hub/stores/{sid}?tab=funnel&days={d}"{" aria-current=page" if d == days else ""}>'
        f'{"<b>" + label + "</b>" if d == days else label}</a>'
        for d, label in ((1, "Today"), (7, "7 days"), (30, "30 days"), (90, "90 days")))
    if not ses:
        return f'<p class="range">{ranges}</p><div class="panel empty">No sessions in this period yet.</div>'
    figures = f"""<div class="figures">
<div><b>{ses:,}</b><span>Sessions</span></div>
<div><b>{pdp:,}</b><span>Saw a product, {pct(pdp, ses)}%</span></div>
<div><b>{atc:,}</b><span>Added to cart, {pct(atc, pdp)}% of those</span></div>
<div><b>{ic:,}</b><span>Started checkout, {pct(ic, atc)}% of those</span></div>
<div><b>{pur:,}</b><span>Bought, {pct(pur, ses) if ses else 0}% of sessions</span></div>
<div><b>{money(float(rev), cur_sign)}</b><span>Revenue</span></div></div>"""
    ad_rows = "".join(
        f"<tr><td>{esc(a)}</td><td class='num'>{s:,}</td><td class='num'>{p:,} ({pct(p, s)}%)</td><td class='num'>{c:,}</td>"
        f"<td class='num'>{i:,}</td><td class='num'>{u:,}</td><td class='num'>{money(float(r), cur_sign)}</td></tr>"
        for a, s, p, c, i, u, r in per_ad)
    day_rows = "".join(
        f"<tr><td>{esc(d)}</td><td class='num'>{s:,}</td><td class='num'>{p:,}</td><td class='num'>{c:,}</td>"
        f"<td class='num'>{u:,}</td><td class='num'>{money(float(r), cur_sign)}</td></tr>"
        for d, s, p, c, u, r in daily)
    return f"""<p class="range">{ranges}</p>
<p class="muted">From the tracker's own records: a session ends after 30 minutes without activity. Purchases are counted from Shopify's orders.</p>
{figures}
<h2>By ad</h2><div class="scroll"><table><thead><tr><th>Ad ID</th><th class="num">Sessions</th><th class="num">Product page</th>
<th class="num">Cart</th><th class="num">Checkout</th><th class="num">Bought</th><th class="num">Revenue</th></tr></thead><tbody>{ad_rows}</tbody></table></div>
<h2>By day</h2><div class="scroll"><table><thead><tr><th>Day</th><th class="num">Sessions</th><th class="num">Product page</th>
<th class="num">Cart</th><th class="num">Bought</th><th class="num">Revenue</th></tr></thead><tbody>{day_rows}</tbody></table></div>"""
