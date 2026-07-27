import sys
import types

# Use the canonical frappe stub registered by conftest.py
frappe_stub = sys.modules["frappe"]
StubValidationError = frappe_stub.StubValidationError

import pytest

from swdpay.outbound import ALLOWED_TRANSITIONS, assert_transition, validate_payment_request, build_payout_request


def _request(**overrides):
    base = dict(
        name="MPPR-2026-00001", description="Cable and breakers",
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


_KW = dict(
    initiator_name="apiop", security_credential="ENCRYPTED", shortcode="4165101",
    b2c_shortcode="4165101", callback_base="https://erp.example.com", callback_token="S3CRET",
)


def test_till_payment_builds_business_buy_goods():
    out = build_payout_request(_request(), **_KW)
    assert out["path"] == "/mpesa/b2b/v1/paymentrequest"
    b = out["body"]
    assert b["CommandID"] == "BusinessBuyGoods"
    assert b["PartyA"] == "4165101" and b["PartyB"] == "832909"
    assert b["SenderIdentifierType"] == "4" and b["RecieverIdentifierType"] == "4"
    assert b["Amount"] == 1500
    assert "AccountReference" not in b
    assert b["ResultURL"] == "https://erp.example.com/api/method/swdpay.api.callbacks.payout_result?token=S3CRET"
    assert b["QueueTimeOutURL"] == "https://erp.example.com/api/method/swdpay.api.callbacks.payout_timeout?token=S3CRET"


def test_paybill_payment_builds_business_paybill_with_account():
    req = _request(recipient_type="Paybill", till_number=None, paybill_number="888880", account_reference="INV-99")
    b = build_payout_request(req, **_KW)["body"]
    assert b["CommandID"] == "BusinessPayBill"
    assert b["PartyB"] == "888880" and b["AccountReference"] == "INV-99"


def test_phone_payment_builds_b2c():
    req = _request(recipient_type="Phone", till_number=None, phone_number="254712345678")
    req.name = "MPPR-2026-00007"
    out = build_payout_request(req, **_KW)
    assert out["path"] == "/mpesa/b2c/v3/paymentrequest"
    b = out["body"]
    assert b["CommandID"] == "BusinessPayment"
    assert b["OriginatorConversationID"] == "MPPR-2026-00007"
    assert b["InitiatorName"] == "apiop"
    assert b["PartyA"] == "4165101" and b["PartyB"] == "254712345678"


def test_b2b_bodies_carry_originator_conversation_id():
    req = _request()
    req.name = "MPPR-2026-00008"
    assert build_payout_request(req, **_KW)["body"]["OriginatorConversationID"] == "MPPR-2026-00008"
