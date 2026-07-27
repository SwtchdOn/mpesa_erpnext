import sys
import types

frappe_stub = sys.modules.get("frappe") or types.ModuleType("frappe")
frappe_utils_stub = sys.modules.get("frappe.utils") or types.ModuleType("frappe.utils")
frappe_utils_stub.flt = getattr(
    frappe_utils_stub, "flt",
    lambda value, precision=None: (
        round(float(value or 0), precision) if precision is not None else float(value or 0)
    )
)
frappe_utils_stub.getdate = getattr(frappe_utils_stub, "getdate", lambda value=None: value)
frappe_utils_stub.now_datetime = getattr(frappe_utils_stub, "now_datetime", lambda: "2026-07-27 00:00:00")
frappe_stub.utils = frappe_utils_stub


class StubValidationError(Exception):
    pass


def _stub_throw(message, *args, **kwargs):
    raise StubValidationError(message)


frappe_stub.throw = _stub_throw
sys.modules.setdefault("frappe", frappe_stub)
sys.modules.setdefault("frappe.utils", frappe_utils_stub)

import pytest

from swdpay.outbound import ALLOWED_TRANSITIONS, assert_transition, validate_payment_request


def _request(**overrides):
    base = dict(
        recipient_type="Buy Goods Till", till_number="832909", paybill_number=None,
        account_reference=None, phone_number=None, payee_name="Kamau Electricals",
        amount=1500, status="Requested",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_validate_accepts_whole_shilling_till_request():
    validate_payment_request(_request())  # no raise


def test_validate_rejects_decimal_amounts():
    with pytest.raises(Exception, match="whole"):
        validate_payment_request(_request(amount=1500.50))


def test_validate_rejects_missing_conditional_fields():
    with pytest.raises(Exception):
        validate_payment_request(_request(till_number=None))
    with pytest.raises(Exception):
        validate_payment_request(_request(recipient_type="Paybill", till_number=None))
    with pytest.raises(Exception):
        validate_payment_request(_request(recipient_type="Phone", till_number=None))


def test_validate_normalizes_phone_in_place():
    req = _request(recipient_type="Phone", till_number=None, phone_number="0712345678")
    validate_payment_request(req)
    assert req.phone_number == "254712345678"


def test_validate_rejects_unnormalizable_phone():
    with pytest.raises(Exception, match="phone"):
        validate_payment_request(_request(recipient_type="Phone", till_number=None, phone_number="12"))


def test_validate_enforces_max_amount_cap():
    with pytest.raises(Exception, match="maximum"):
        validate_payment_request(_request(amount=5000), max_amount=1000)


def test_transitions_follow_the_lifecycle():
    assert_transition("Requested", "Approved")
    assert_transition("Requested", "Rejected")
    assert_transition("Requested", "Cancelled")
    assert_transition("Approved", "Sending")
    assert_transition("Approved", "Failed")
    assert_transition("Sending", "Paid")
    assert_transition("Sending", "Failed")
    for bad in [("Requested", "Paid"), ("Paid", "Failed"), ("Rejected", "Approved"), ("Sending", "Approved")]:
        with pytest.raises(Exception):
            assert_transition(*bad)
    assert "Requested" in ALLOWED_TRANSITIONS
