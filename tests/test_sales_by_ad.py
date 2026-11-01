"""Sales by ad: each real order counted once, credited to the buyer's last
tagged ad click, so two ad accounts never both get the same sale."""
from datetime import datetime, timedelta, timezone

import pytest

import hub
import main
import stores

SHOP = "sales-test.myshopify.com"


def event(name, vid, url, minutes_ago, order_id=None, value=None):
    main.save_event_db(name, vid=vid, url=url, value=value, order_id=order_id, shop=SHOP)
    with stores.db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE events SET ts = %s WHERE id = (SELECT max(id) FROM events)",
                    (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),))


def ad_link(ad, account="main", adset="as1", campaign="c1"):
    return (f"https://shop.example/pages/adv?utm_source=facebook&utm_medium=paid&utm_campaign={campaign}"
            f"&utm_content={ad}&utm_term={adset}&utm_id={account}&fbclid=IwAR{'x' * 20}")


@pytest.fixture(scope="module", autouse=True)
def orders():
    with stores.db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM events WHERE shop = %s", (SHOP,))
        cur.execute("""INSERT INTO stores (id, name, shop_domain, site_url, pixel_id, currency)
                       VALUES ('sales-test', 'Sales Test', %s, 'https://shop.example', '123456789', 'GBP')
                       ON CONFLICT (id) DO NOTHING""", (SHOP,))
    # A: clicked a bm2 ad, then later a main ad, then came back direct: main's ad wins.
    event("PageView", "v_buyera001", ad_link("ad_bm2", account="bm2"), 200)
    event("PageView", "v_buyera001", ad_link("ad_main1"), 90)
    event("PageView", "v_buyera001", "https://shop.example/products/x", 30)
    event("Purchase", "v_buyera001", "/products/x", 20, order_id="1001", value=100)
    # Webhook retry: the same order saved twice is still one sale.
    event("Purchase", "v_buyera001", "/products/x", 19, order_id="1001", value=100)
    # B: only the bm2 ad.
    event("PageView", "v_buyerb002", ad_link("ad_bm2", account="bm2"), 60)
    event("Purchase", "v_buyerb002", "/", 10, order_id="1002", value=50)
    # C: Meta click with no ad tags.
    event("PageView", "v_buyerc003", "https://shop.example/?utm_source=facebook&utm_medium=paid", 60)
    event("Purchase", "v_buyerc003", "/", 9, order_id="1003", value=40)
    # D: no visitor ID; the order's own landing page carries the tags.
    event("Purchase", "", "/pages/adv?utm_source=facebook&utm_content=ad_main2&utm_id=main", 8,
          order_id="1004", value=60)
    # E: nothing at all.
    event("Purchase", "v_buyere005", "/", 7, order_id="1005", value=30)
    # F: a click 40 days old is too old to count.
    event("PageView", "v_buyerf006", ad_link("ad_old"), 40 * 24 * 60)
    event("Purchase", "v_buyerf006", "/", 6, order_id="1006", value=20)
    yield


def page(client, period="7"):
    return client.get(f"/hub/stores/sales-test?tab=sales&period={period}").get_data(as_text=True)


def test_each_order_once_last_ad_click_wins(logged_in):
    text = page(logged_in)
    assert "<b>6</b><span>Orders, last 7 days</span>" in text
    assert "<b>3</b><span>From a known Meta ad, 50%</span>" in text
    assert "<b>1</b><span>Meta, ad not tagged</span>" in text
    assert "<b>2</b><span>No ad click found</span>" in text
    assert "main / ad ad_main1" in text and "bm2 / ad ad_bm2" in text and "main / ad ad_main2" in text
    assert "ad_old" not in text


def test_accounts_never_share_a_sale(logged_in):
    text = page(logged_in)
    by_account = text.split("<h2>By ad account</h2>")[1].split("</table>")[0]
    assert "<td>main</td><td class='num'>2</td>" in by_account
    assert "<td>bm2</td><td class='num'>1</td>" in by_account


def test_untagged_meta_sales_show_how_to_tag(logged_in):
    assert "utm_content={{ad.id}}" in page(logged_in)


def test_other_tabs_unchanged_and_empty_period(logged_in):
    assert 'href="/hub/stores/sales-test?tab=sales"' in page(logged_in)
    assert "Sales by ad" in logged_in.get("/hub/stores/comfi-uk?tab=health").get_data(as_text=True)


def test_click_parsing():
    assert hub.ad_click("https://x/?gclid=abc")["channel"] == "google"
    assert hub.ad_click("https://x/?utm_source=tiktok&utm_campaign=z")["source"] == "tiktok"
    assert hub.ad_click("https://x/products/y") is None
    c = hub.ad_click("/p?fbclid=IwAR1&utm_content=123&utm_id=bm2&ad_name=Hook%20A")
    assert c["channel"] == "meta" and c["ad"] == "Hook A" and c["account"] == "bm2"
