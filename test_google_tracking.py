"""Offline regression tests. No production database, Shopify, Meta or Google calls."""
import os
import json
import hmac
import base64
import hashlib
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

# Importing the legacy module normally initializes its database. Isolate it first.
with patch.dict(os.environ, {"DATABASE_URL": "", "GOOGLE_TRACKING_ENABLED": "false"}):
    import main as tracker


PRIVACY = {"marketingAllowed": True, "saleOfDataAllowed": True}
CONFIG = {"GOOGLE_TRACKING_ENABLED": "true", "SHOPIFY_WEBHOOK_SECRET": "test-only-secret",
          "GOOGLE_SHOP_DOMAIN": "test.myshopify.com", "GOOGLE_ADS_CUSTOMER_ID": "1234567890",
          "GOOGLE_ADS_CONVERSION_ACTION_ID": "123", "GOOGLE_ADS_LOGIN_CUSTOMER_ID": ""}


def snapshot():
    return {"order_id": "42", "event_time": datetime.now(timezone.utc).isoformat(), "value": "179.99",
            "currency": "GBP", "customer_id": "1234567890", "login_id": "1234567890", "click": {},
            "vid": "v_test", "checkout_token": "checkout-secret", "user_data": {"userIdentifiers": [{"emailAddress": "a" * 64}]}}


def context(**overrides):
    return {"vid": "v_test", "source": "cart", "consent": dict(PRIVACY),
            "consent_ts": 1, "click": {"gclid": "realistic_test_id"}, **overrides}


def delivery(**overrides):
    return {"id": 1, "shop": CONFIG["GOOGLE_SHOP_DOMAIN"], "order_id": "42", "action_id": "123",
            "snapshot": snapshot(), "payload": None, "request_id": None, "attempts": 0,
            "diagnostics": {}, "created_at": datetime.now(timezone.utc), **overrides}


@contextmanager
def fake_db(row=None):
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = row
    cur.rowcount = 1
    yield conn


class GoogleTrackingTests(unittest.TestCase):
    def setUp(self):
        self.now = int(datetime.now(timezone.utc).timestamp() * 1000)

    def test_click_types_and_expiry(self):
        for key in tracker.GOOGLE_CLICK_KEYS:
            state = {key: "Click_ID-1", "captured_at": self.now - 1000}
            self.assertEqual(tracker.google_click(state, self.now)[key], "Click_ID-1")
        for ts in [self.now + 1, self.now - tracker.GOOGLE_WINDOW_MS - 1, "bad", None]:
            self.assertFalse(tracker.google_click({"gclid": "x", "captured_at": ts}, self.now))
        self.assertFalse(tracker.google_click({"gclid": "bad id", "captured_at": self.now}, self.now))

    def test_context_requires_real_boolean_consent_and_discards_query_data(self):
        body = {"source": "theme", "vid": "v_test", "consent": PRIVACY,
                "attribution": {"gclid": "test", "captured_at": self.now - 1000,
                                "landing_url": "https://comfishop.com/products/test?email=private",
                                "referrer": "https://www.google.com/?q=private", "utm_source": "google"}}
        saved = tracker.google_context_body(body)
        self.assertEqual(saved["attribution"]["landing_url"], "https://comfishop.com/products/test")
        self.assertEqual(saved["attribution"]["referrer"], "https://www.google.com/")
        for privacy in [{}, [], {"marketingAllowed": "true", "saleOfDataAllowed": True}]:
            with self.assertRaises(ValueError):
                tracker.google_context_body({**body, "consent": privacy})
        denied = tracker.google_context_body({**body, "consent": {**PRIVACY, "marketingAllowed": False}})
        self.assertFalse(denied["attribution"])

    def test_order_id_and_money_are_authoritative_and_currency_paired(self):
        order = {"id": 42, "total_price": "179.99", "currency": "GBP",
                 "total_price_set": {"presentment_money": {"amount": "219.99", "currency_code": "USD"}}}
        result = tracker.google_paid_snapshot(order, "2026-09-27T10:00:00Z")
        self.assertEqual((result["value"], result["currency"]), ("219.99", "USD"))
        self.assertEqual(tracker.google_order_id("gid://shopify/Order/42"), "42")
        for invalid in ["NaN", "Infinity", "-1", "0", "bad"]:
            with self.assertRaises(ValueError):
                tracker.google_paid_snapshot({"id": 42, "total_price": invalid, "currency": "GBP"}, "")

    def test_google_normalization_does_not_reuse_meta_phone_format(self):
        data = tracker.google_user_data({"email": "A.B+offer@gmail.com", "phone": "+44 (7700) 900-123"})
        self.assertEqual(data["userIdentifiers"], [
            {"emailAddress": hashlib.sha256(b"ab@gmail.com").hexdigest()},
            {"phoneNumber": hashlib.sha256(b"+447700900123").hexdigest()}])
        self.assertFalse(tracker.google_user_data({"phone": "07700900123"}))
        other = tracker.google_user_data({"email": "A.B+offer@example.com"})
        self.assertEqual(other["userIdentifiers"][0]["emailAddress"], tracker.hash_data("a.b+offer@example.com"))

    def test_payload_uses_each_click_type_one_purchase_and_stable_transaction(self):
        row = delivery()
        for key in tracker.GOOGLE_CLICK_KEYS:
            body = tracker.google_purchase_payload(row, context(click={key: "test"}))
            self.assertEqual(len(body["events"]), 1)
            event = body["events"][0]
            self.assertEqual(event["adIdentifiers"], {key: "test"})
            self.assertEqual(event["transactionId"], "test.myshopify.com:42")
            self.assertEqual(event["conversionValue"], 179.99)
            self.assertEqual(event["eventTimestamp"], row["snapshot"]["event_time"])
        with self.assertRaises(ValueError):
            tracker.google_purchase_payload(row, context(consent={**PRIVACY, "saleOfDataAllowed": False}))
        self.assertNotIn("adIdentifiers", tracker.google_purchase_payload(row, context(click={}))["events"][0])
        row["snapshot"]["user_data"] = {}
        with self.assertRaises(ValueError):
            tracker.google_purchase_payload(row, context(click={}))

    def test_processing_never_treats_request_id_or_wrong_destination_as_success(self):
        self.assertEqual(tracker.google_processing_result({"requestId": "request"}, "123")[0], "accepted")
        for state, expected in [("PROCESSING", "accepted"), ("SUCCESS", "processed"),
                                ("PARTIAL_SUCCESS", "failed"), ("FAILED", "failed")]:
            body = {"requestStatusPerDestination": [{"destination": {"productDestinationId": "123",
                     "operatingAccount": {"accountId": "1234567890"}}, "requestStatus": state}]}
            self.assertEqual(tracker.google_processing_result(body, "123", "1234567890")[0], expected)
            self.assertEqual(tracker.google_processing_result(body, "123", "9999999999")[0], "accepted")

    def webhook(self, order, secret="test-only-secret", **headers):
        raw = json.dumps(order).encode()
        signature = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
        return tracker.app.test_client().post('/webhook/google-order-paid', data=raw, content_type='application/json',
            headers={"X-Shopify-Hmac-Sha256": signature, "X-Shopify-Shop-Domain": CONFIG["GOOGLE_SHOP_DOMAIN"],
                     "X-Shopify-Topic": "orders/paid", **headers})

    def test_paid_webhook_requires_hmac_shop_topic_and_configuration(self):
        with patch.dict(os.environ, CONFIG), patch.object(tracker, "DATABASE_URL", "unused"), patch.object(tracker, "google_db") as db:
            self.assertEqual(self.webhook({}, secret="wrong").status_code, 401)
            self.assertEqual(self.webhook({}, **{"X-Shopify-Shop-Domain": "wrong"}).status_code, 401)
            self.assertEqual(self.webhook({}, **{"X-Shopify-Topic": "orders/create"}).status_code, 401)
            with patch.dict(os.environ, {"SHOPIFY_WEBHOOK_SECRET": ""}):
                self.assertEqual(self.webhook({}).status_code, 503)
            db.assert_not_called()

    def test_paid_webhook_queues_without_google_or_meta_and_storage_failure_retries(self):
        order = {"id": 42, "financial_status": "paid", "total_price": "179.99", "currency": "GBP"}
        with patch.dict(os.environ, CONFIG), patch.object(tracker, "DATABASE_URL", "unused"), \
             patch.object(tracker, "send_capi_event") as meta, patch.object(tracker.requests, "post") as outbound:
            with patch.object(tracker, "google_db", side_effect=fake_db):
                self.assertEqual(self.webhook(order).status_code, 200)
            with patch.object(tracker, "google_db", side_effect=RuntimeError("db offline")):
                self.assertEqual(self.webhook(order).status_code, 503)
            for changes in [{"test": True}, {"financial_status": "pending"}, {"cancelled_at": "date"}]:
                self.assertTrue(self.webhook({**order, **changes}).json["skipped"])
            meta.assert_not_called()
            outbound.assert_not_called()

    def test_context_bridge_token_conflicts_consent_and_late_clicks(self):
        snap = snapshot()
        at_ms = int(datetime.fromisoformat(snap["event_time"]).timestamp() * 1000)
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = ({"vid": "v_other"},)
        self.assertEqual(tracker.google_order_context(conn, snap)[1], "conflicting_visitor")
        cur.fetchone.return_value = ({"client_id": "client"},)
        saved = context(attribution={"wbraid": "click", "captured_at": at_ms - 1000})
        cur.fetchall.return_value = [(saved,)]
        result, error = tracker.google_order_context(conn, snap)
        self.assertIsNone(error)
        self.assertEqual(result["click"]["wbraid"], "click")
        snap["click"] = {"gclid": "old_cart", "captured_at": at_ms - 2000}
        newer = tracker.google_order_context(conn, snap)[0]["click"]
        self.assertEqual(newer["wbraid"], "click")
        self.assertEqual(newer["gclid"], "")
        snap["click"] = {}
        saved["attribution"]["captured_at"] = at_ms + 1000
        self.assertFalse(tracker.google_order_context(conn, snap)[0]["click"])
        saved["consent"] = {**PRIVACY, "marketingAllowed": False}
        self.assertEqual(tracker.google_order_context(conn, snap)[1], "consent_not_granted")
        # A later grant must not revive pre-withdrawal click identity.
        cur.fetchall.return_value = [(context(attribution={}),), (context(consent={**PRIVACY, "marketingAllowed": False},
                                            consent_ts=at_ms - 500),), (context(attribution={"gclid": "old", "captured_at": at_ms - 1000}),)]
        self.assertFalse(tracker.google_order_context(conn, snap)[0]["click"])

    def run_delivery(self, row, session):
        with patch.object(tracker, "google_db", side_effect=lambda: fake_db(row)), \
             patch.object(tracker, "google_order_context", return_value=(context(), None)), \
             patch.object(tracker, "google_delivery_update") as update:
            tracker.google_deliver_one(session)
            return update.call_args

    def test_worker_acceptance_polling_and_retry(self):
        row = delivery()
        session = MagicMock()
        session.post.return_value.ok = True
        session.post.return_value.json.return_value = {"requestId": "accepted-id"}
        result = self.run_delivery(row, session)
        self.assertEqual(result.args[1], "accepted")
        self.assertEqual(result.kwargs["request_id"], "accepted-id")
        session.reset_mock()
        row["request_id"] = "accepted-id"
        session.get.return_value.ok = True
        session.get.return_value.json.return_value = {"requestStatusPerDestination": [{"destination": {
            "productDestinationId": "123", "operatingAccount": {"accountId": "1234567890"}}, "requestStatus": "SUCCESS"}]}
        self.assertEqual(self.run_delivery(row, session).args[1], "processed")
        session.post.assert_not_called()
        session.get.return_value.json.return_value["requestStatusPerDestination"][0].update(
            requestStatus="FAILED", errorInfo={"errorCounts": [{"reason": "PROCESSING_ERROR_REASON_TOO_RECENT_CLICK"}]})
        retry = self.run_delivery(row, session)
        self.assertEqual(retry.args[1], "retry")
        self.assertTrue(retry.kwargs["clear_request"])
        self.assertEqual(retry.args[2]["previous_request_id"], "accepted-id")
        row["request_id"] = None
        session.post.return_value.ok = False
        for code, expected in [(429, "retry"), (503, "retry"), (400, "failed")]:
            session.post.return_value.status_code = code
            session.post.return_value.json.return_value = {"error": {"status": "ERROR"}}
            self.assertEqual(self.run_delivery(row, session).args[1], expected)

    def test_worker_reuses_persisted_payload_after_ambiguous_network_failure(self):
        row = delivery()
        row["payload"] = tracker.google_purchase_payload(row, context(click={"wbraid": "original"}))
        session = MagicMock()
        session.post.side_effect = tracker.requests.Timeout()
        self.assertEqual(self.run_delivery(row, session).args[1], "retry")
        self.assertIs(session.post.call_args.kwargs["json"], row["payload"])

    def test_context_endpoint_cannot_create_a_purchase(self):
        body = {"source": "pixel", "vid": "v_test", "client_id": "client", "checkout_token": "checkout",
                "order_id": "gid://shopify/Order/42", "consent": PRIVACY}
        conn = MagicMock()
        with patch.dict(os.environ, CONFIG), patch.object(tracker, "DATABASE_URL", "unused"), \
             patch.object(tracker, "google_db") as db, patch.object(tracker, "send_capi_event") as meta:
            db.return_value.__enter__.return_value = conn
            response = tracker.app.test_client().post('/google/context', json=body)
            self.assertEqual(response.status_code, 200)
            sql = conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0]
            self.assertIn("INSERT INTO google_context", sql)
            self.assertNotIn("google_purchase_delivery", sql)
            meta.assert_not_called()


if __name__ == '__main__':
    unittest.main()
