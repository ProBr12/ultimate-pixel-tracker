"""Comfi's requests to Meta (Purchase above all) must stay byte-identical to
the previous commit. Each version runs in its own process and database."""
import json
import os
import subprocess
import sys

import pytest

from pg import fresh_database

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.environ.get("BASELINE_COMMIT", "1e7d55c")
CAPTURE = os.path.join(REPO, "tests", "purchase_capture.py")


def export_commit(commit, target):
    os.makedirs(target, exist_ok=True)
    for name in ("main.py", "stores.py", "hub.py"):
        source = subprocess.run(["git", "-C", REPO, "show", f"{commit}:{name}"],
                                check=True, capture_output=True).stdout
        with open(os.path.join(target, name), "wb") as f:
            f.write(source)


def capture(code_dir, db_name, out):
    result = subprocess.run([sys.executable, CAPTURE, code_dir, fresh_database(db_name), out],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    with open(out, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def both(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("regression")
    old_dir = str(tmp / "old")
    export_commit(BASELINE, old_dir)
    old = capture(old_dir, "hub_regress_old", str(tmp / "old.json"))
    new = capture(REPO, "hub_regress_new", str(tmp / "new.json"))
    return old, new


def events(captured, name):
    return [c for c in captured if json.loads(c["wire"])["data"][0]["event_name"] == name]


def test_purchase_byte_identical(both):
    old, new = both
    old_p, new_p = events(old, "Purchase"), events(new, "Purchase")
    assert len(old_p) == 8  # nine orders, the Shopify test order is never sent
    assert [c["wire"] for c in new_p] == [c["wire"] for c in old_p]
    assert [c["url"] for c in new_p] == [c["url"] for c in old_p]


def test_every_comfi_request_byte_identical(both):
    """AddToCart, InitiateCheckout, the logged-in PageView and AddPaymentInfo too."""
    old, new = both
    assert len(new) == len(old) == 12
    assert new == old


def test_phone_hashes_present(both):
    """The fixtures really exercise phones (GB national, +44, US, shipping only, customer only)."""
    _, new = both
    with_phone = [c for c in events(new, "Purchase") if "ph" in json.loads(c["wire"])["data"][0]["user_data"]]
    assert len(with_phone) == 7  # every order but the empty one (5008)
