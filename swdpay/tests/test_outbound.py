import sys
import types

# Use the canonical frappe stub registered by conftest.py
frappe_stub = sys.modules["frappe"]
StubValidationError = frappe_stub.StubValidationError

import pytest

from swdpay.outbound import ALLOWED_TRANSITIONS, assert_transition, validate_payment_request, build_payout_request, parse_payout_result, parse_etims_invoice_number


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


def _result_payload(code=0, params=None):
    return {"Result": {
        "ResultType": 0, "ResultCode": code, "ResultDesc": "The service request is processed successfully.",
        "OriginatorConversationID": "MPPR-2026-00001", "ConversationID": "AG_20260727_abc123",
        "TransactionID": "TGH7SK61SU",
        "ResultParameters": {"ResultParameter": params or [
            {"Key": "TransactionAmount", "Value": 1500},
            {"Key": "TransactionReceipt", "Value": "TGH7SK61SU"},
            {"Key": "ReceiverPartyPublicName", "Value": "254712345678 - John Kamau"},
        ]},
    }}


def test_parse_payout_result_flattens_success():
    parsed = parse_payout_result(_result_payload())
    assert parsed["originator_conversation_id"] == "MPPR-2026-00001"
    assert parsed["conversation_id"] == "AG_20260727_abc123"
    assert parsed["result_code"] == 0
    assert parsed["transaction_id"] == "TGH7SK61SU"
    assert parsed["receiver_public_name"] == "254712345678 - John Kamau"


def test_parse_payout_result_reads_b2b_credit_party_name():
    payload = _result_payload(params=[{"Key": "CreditPartyPublicName", "Value": "832909 - Kamau Electricals"}])
    assert parse_payout_result(payload)["receiver_public_name"] == "832909 - Kamau Electricals"


def test_parse_payout_result_handles_failure_without_parameters():
    payload = {"Result": {"ResultCode": 2001, "ResultDesc": "The initiator information is invalid.",
                          "OriginatorConversationID": "MPPR-2026-00002", "ConversationID": "AG_x"}}
    parsed = parse_payout_result(payload)
    assert parsed["result_code"] == 2001 and parsed["transaction_id"] is None


def test_parse_etims_invoice_number_from_kra_url():
    url = "https://itax.kra.go.ke/KRA-Portal/invoiceChk.htm?actionCode=loadPage&invoiceNo=0010023456789"
    assert parse_etims_invoice_number(url) == "0010023456789"
    assert parse_etims_invoice_number("https://example.com/no-invoice") is None
    assert parse_etims_invoice_number(None) is None


def test_daraja_base_url_by_environment():
    from swdpay.daraja import daraja_base_url
    assert daraja_base_url("Sandbox") == "https://sandbox.safaricom.co.ke"
    assert daraja_base_url("Production") == "https://api.safaricom.co.ke"


def test_encrypt_security_credential_round_trips_with_test_key():
    from base64 import b64decode
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.x509.oid import NameOID
    from datetime import datetime, timedelta, timezone

    from swdpay.daraja import encrypt_security_credential

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()

    encrypted = encrypt_security_credential("s3cret-pass", pem)
    decrypted = key.decrypt(b64decode(encrypted), padding.PKCS1v15())
    assert decrypted == b"s3cret-pass"
