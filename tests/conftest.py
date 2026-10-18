"""Shared setup: a fresh local Postgres database and the tracker imported
against it, with Meta, Telegram and the hub's checks mocked per test.

Run from the repo root:  python -m pytest tests
"""
import os
import sys

import pytest
from flask.testing import FlaskClient

from pg import fresh_database

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.update({
    "DATABASE_URL": fresh_database("hub_test"), "PIXEL_ID": "1449380569544787",
    "ACCESS_TOKEN": "COMFI" + "a" * 60, "SHOPIFY_WEBHOOK_SECRET": "comfi-webhook-secret",
    "HUB_SECRET_KEY": "test-hub-secret-key-0123456789", "DASH_PASSWORD": "pw",
    "GOOGLE_TRACKING_ENABLED": "false", "HUB_PUBLIC_HOST": "hub.example.test",
})

import alerts  # noqa: E402

alerts.start = lambda *args: None  # tests run the checker by hand

import main  # noqa: E402
import hub  # noqa: E402
import stores  # noqa: E402


HUB_URL = "https://hub.example.test"  # https: the hub's session cookie is Secure


class HubClient(FlaskClient):
    def open(self, *args, **kwargs):
        kwargs.setdefault("base_url", HUB_URL)
        return super().open(*args, **kwargs)

    def session_transaction(self, *args, **kwargs):
        kwargs.setdefault("base_url", HUB_URL)
        return super().session_transaction(*args, **kwargs)


def hub_client():
    """A test client already logged in to the hub (CSRF token "t")."""
    main.app.test_client_class = HubClient
    try:
        client = main.app.test_client()
    finally:
        main.app.test_client_class = None
    with client.session_transaction() as s:
        s["hub"] = True
        s["csrf"] = "t"
    return client


@pytest.fixture
def app_client():
    return main.app.test_client()


@pytest.fixture
def logged_in():
    return hub_client()
