import sys
import types

# Use the canonical frappe stub registered by conftest.py
frappe_stub = sys.modules["frappe"]
StubValidationError = frappe_stub.StubValidationError

import pytest

from swdpay.outbound import (
    ALLOWED_TRANSITIONS, assert_transition, validate_payment_request, build_payout_request,
    parse_payout_result, parse_etims_invoice_number, plan_approval, plan_party_resolution,
    payout_outcome, handle_payout_callback, reconcile_payout, find_request_by_originator,
    parse_status_result, handle_status_result, plan_receipt_attach,
    approve_and_send, reject_request, send_payment_request,
    parse_balance_result,
    # Aliased: pytest would otherwise collect this as a test itself (name starts with "test_").
    test_initiator_balance as call_test_initiator_balance,
)


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
    # Daraja v1 B2B does not document OriginatorConversationID as a request field —
    # it's minted by Daraja and captured from the response instead.
    assert "OriginatorConversationID" not in b
    assert b["ResultURL"] == "https://erp.example.com/api/method/swdpay.api.callbacks.payout_result?token=S3CRET"
    assert b["QueueTimeOutURL"] == "https://erp.example.com/api/method/swdpay.api.callbacks.payout_timeout?token=S3CRET"


def test_paybill_payment_builds_business_paybill_with_account():
    req = _request(recipient_type="Paybill", till_number=None, paybill_number="888880", account_reference="INV-99")
    b = build_payout_request(req, **_KW)["body"]
    assert b["CommandID"] == "BusinessPayBill"
    assert b["PartyB"] == "888880" and b["AccountReference"] == "INV-99"
    assert "OriginatorConversationID" not in b


def test_phone_payment_builds_b2c():
    req = _request(recipient_type="Phone", till_number=None, phone_number="254712345678")
    req.name = "MPPR-2026-00007"
    out = build_payout_request(req, **_KW)
    assert out["path"] == "/mpesa/b2c/v1/paymentrequest"
    b = out["body"]
    assert b["CommandID"] == "BusinessPayment"
    # Daraja v1 B2C does not document OriginatorConversationID as a request field
    # either — it's minted by Daraja and captured from the response instead.
    assert "OriginatorConversationID" not in b
    assert b["InitiatorName"] == "apiop"
    assert b["PartyA"] == "4165101" and b["PartyB"] == "254712345678"


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


def test_plan_approval_guards():
    assert plan_approval("Requested", enabled=True, has_role=True) is None  # OK → no error
    assert "disabled" in plan_approval("Requested", enabled=False, has_role=True)
    assert "permission" in plan_approval("Requested", enabled=True, has_role=False)
    assert "Cannot move" in plan_approval("Paid", enabled=True, has_role=True)


def test_till_resolves_to_existing_supplier_by_identifier():
    plan = plan_party_resolution(_request(), supplier_by_identifier="Kamau Electricals Ltd",
                                 employee_matches=[], supplier_matches=[])
    assert plan == {"party_type": "Supplier", "party": "Kamau Electricals Ltd", "create_supplier": False}


def test_till_with_no_match_creates_supplier_from_payee_name():
    plan = plan_party_resolution(_request(), supplier_by_identifier=None,
                                 employee_matches=[], supplier_matches=[])
    assert plan == {"party_type": "Supplier", "party": "Kamau Electricals", "create_supplier": True}


def test_phone_prefers_exactly_one_employee():
    req = _request(recipient_type="Phone", till_number=None, phone_number="254712345678")
    plan = plan_party_resolution(req, supplier_by_identifier=None,
                                 employee_matches=["HR-EMP-00003"], supplier_matches=["Some Shop"])
    assert plan == {"party_type": "Employee", "party": "HR-EMP-00003", "create_supplier": False}


def test_phone_with_multiple_employee_matches_falls_through_to_supplier():
    req = _request(recipient_type="Phone", till_number=None, phone_number="254712345678")
    plan = plan_party_resolution(req, supplier_by_identifier=None,
                                 employee_matches=["A", "B"], supplier_matches=["Some Shop"])
    assert plan == {"party_type": "Supplier", "party": "Some Shop", "create_supplier": False}


def test_phone_with_no_matches_creates_supplier():
    req = _request(recipient_type="Phone", till_number=None, phone_number="254712345678")
    plan = plan_party_resolution(req, supplier_by_identifier=None, employee_matches=[], supplier_matches=[])
    assert plan == {"party_type": "Supplier", "party": "Kamau Electricals", "create_supplier": True}


def test_payout_outcome_maps_result_codes():
    assert payout_outcome({"result_code": 0}) == "Paid"
    assert payout_outcome({"result_code": 2001}) == "Failed"
    assert payout_outcome({"result_code": None}) == "Failed"


def test_terminal_states_ignore_duplicate_callbacks():
    # Reconciliation must be a no-op once Paid/Failed — guarded by ALLOWED_TRANSITIONS.
    assert "Paid" not in ALLOWED_TRANSITIONS["Paid"]
    assert "Paid" not in ALLOWED_TRANSITIONS["Failed"]


# --- handle_payout_callback / reconcile_payout: the money path, exercised against
# the in-memory fakes registered in conftest.py (frappe_stub.db / get_doc / enqueue / log_error) ---

def test_result_callback_marks_paid_and_stores_receipt(payout_env, monkeypatch):
    payout_env.db.docs["MPPR-2026-00001"] = {
        "status": "Sending", "conversation_id": "AG_20260727_abc123", "payment_entry": None,
    }
    pe_calls = []
    monkeypatch.setattr(
        "swdpay.outbound.create_payout_payment_entry",
        lambda doc, settings: pe_calls.append(doc.name) or pe_calls.append("PE-0001"),
    )

    handle_payout_callback(_result_payload(code=0))

    # Raw payload persisted before reconciliation is even queued.
    assert payout_env.db.docs["MPPR-2026-00001"]["raw_result"]
    assert len(payout_env.enqueued) == 1
    job = payout_env.enqueued[0]
    assert job["job_id"] == "swdpay-payout-reconcile-MPPR-2026-00001-result"
    assert job["deduplicate"] is True
    assert job["enqueue_after_commit"] is True

    # Simulate the worker running the enqueued job with the exact kwargs recorded.
    reconcile_payout(job["request_name"], job["parsed"], timeout=job["timeout"])

    doc = payout_env.db.docs["MPPR-2026-00001"]
    assert doc["status"] == "Paid"
    assert doc["receipt_pending"] == 1
    assert doc["mpesa_transaction_id"] == "TGH7SK61SU"
    assert doc["receiver_public_name"] == "254712345678 - John Kamau"
    assert pe_calls == ["MPPR-2026-00001", "PE-0001"]


def test_reconcile_payout_on_paid_doc_is_a_noop(payout_env):
    payout_env.db.docs["MPPR-2026-00001"] = {
        "status": "Paid", "conversation_id": "AG_20260727_abc123",
        "payment_entry": "PE-0001", "mpesa_transaction_id": "TGH7SK61SU",
    }
    before = dict(payout_env.db.docs["MPPR-2026-00001"])

    reconcile_payout("MPPR-2026-00001", {"result_code": 0, "conversation_id": "AG_20260727_abc123"})

    assert payout_env.db.docs["MPPR-2026-00001"] == before


def test_timeout_callback_leaves_status_sending(payout_env):
    payout_env.db.docs["MPPR-2026-00001"] = {"status": "Sending", "conversation_id": "AG_20260727_abc123"}

    reconcile_payout("MPPR-2026-00001", {"conversation_id": "AG_20260727_abc123"}, timeout=True)

    doc = payout_env.db.docs["MPPR-2026-00001"]
    assert doc["status"] == "Sending"
    assert "queue timeout" in doc["error_message"].lower()


def test_unknown_originator_logs_and_enqueues_nothing(payout_env):
    handle_payout_callback(_result_payload(code=0))  # MPPR-2026-00001 was never seeded

    assert payout_env.enqueued == []
    assert len(payout_env.logged_errors) == 1
    assert "unknown request" in payout_env.logged_errors[0]["title"]
    assert payout_env.logged_errors[0]["message"]  # payload captured, not lost


# --- find_request_by_originator: v1's originator is Daraja-minted, so correlation
# must fall through to the stored originator_conversation_id field ---

def test_find_request_by_originator_resolves_doc_name_directly(payout_env):
    payout_env.db.docs["MPPR-2026-00001"] = {"status": "Sending"}
    assert find_request_by_originator("MPPR-2026-00001") == "MPPR-2026-00001"


def test_find_request_by_originator_resolves_daraja_generated_id(payout_env):
    payout_env.db.docs["MPPR-2026-00001"] = {
        "status": "Sending", "originator_conversation_id": "AG_xyz_123",
    }
    assert find_request_by_originator("AG_xyz_123") == "MPPR-2026-00001"


def test_find_request_by_originator_unknown_returns_none(payout_env):
    assert find_request_by_originator("AG_never_seen") is None
    assert find_request_by_originator(None) is None
    assert find_request_by_originator("") is None


def test_result_callback_resolves_via_stored_originator_conversation_id(payout_env, monkeypatch):
    # Daraja v1: the callback's OriginatorConversationID is Daraja's own id (captured
    # at dispatch), not the doc name — correlation must go through the stored field.
    payout_env.db.docs["MPPR-2026-00001"] = {
        "status": "Sending", "conversation_id": "AG_20260727_abc123",
        "originator_conversation_id": "AG_xyz_123", "payment_entry": None,
    }
    pe_calls = []
    monkeypatch.setattr(
        "swdpay.outbound.create_payout_payment_entry",
        lambda doc, settings: pe_calls.append(doc.name) or pe_calls.append("PE-0001"),
    )

    payload = _result_payload(code=0)
    payload["Result"]["OriginatorConversationID"] = "AG_xyz_123"

    handle_payout_callback(payload)

    assert len(payout_env.enqueued) == 1
    job = payout_env.enqueued[0]
    assert job["request_name"] == "MPPR-2026-00001"

    reconcile_payout(job["request_name"], job["parsed"], timeout=job["timeout"])

    doc = payout_env.db.docs["MPPR-2026-00001"]
    assert doc["status"] == "Paid"
    assert pe_calls == ["MPPR-2026-00001", "PE-0001"]


def test_conversation_id_mismatch_is_dropped_with_log(payout_env):
    payout_env.db.docs["MPPR-2026-00001"] = {"status": "Sending", "conversation_id": "AG_STORED"}

    reconcile_payout("MPPR-2026-00001", {"result_code": 0, "conversation_id": "AG_DIFFERENT"})

    doc = payout_env.db.docs["MPPR-2026-00001"]
    assert doc["status"] == "Sending"
    assert len(payout_env.logged_errors) == 1
    assert "ConversationID mismatch" in payout_env.logged_errors[0]["title"]


def test_reconcile_payout_marks_failed_with_result_desc(payout_env):
    payout_env.db.docs["MPPR-2026-00002"] = {"status": "Sending", "conversation_id": "AG_x"}

    reconcile_payout("MPPR-2026-00002", {
        "result_code": 2001, "result_desc": "The initiator information is invalid.",
        "conversation_id": "AG_x",
    })

    doc = payout_env.db.docs["MPPR-2026-00002"]
    assert doc["status"] == "Failed"
    assert doc["error_message"] == "The initiator information is invalid."
    assert doc["result_code"] == "2001"


def test_reconcile_payout_stores_none_result_code_as_none_not_string(payout_env):
    payout_env.db.docs["MPPR-2026-00003"] = {"status": "Sending", "conversation_id": None}

    reconcile_payout("MPPR-2026-00003", {"result_code": None, "conversation_id": None})

    doc = payout_env.db.docs["MPPR-2026-00003"]
    assert doc["status"] == "Failed"
    assert doc["result_code"] is None


# --- parse_status_result: tolerant mapping of a Daraja Transaction Status result ---

def _status_result_payload(status="Completed", params=None):
    base_params = [
        {"Key": "TransactionStatus", "Value": status},
        {"Key": "OriginatorConversationID", "Value": "MPPR-2026-00001"},
        {"Key": "ConversationID", "Value": "AG_query_999"},
        {"Key": "ReceiptNo", "Value": "TGH7SK61SU"},
    ]
    return {"Result": {"ResultParameters": {"ResultParameter": params if params is not None else base_params}}}


def test_parse_status_result_completed_maps_to_zero_with_originator_from_params():
    parsed = parse_status_result(_status_result_payload(status="Completed"))
    assert parsed["result_code"] == 0
    assert parsed["originator_conversation_id"] == "MPPR-2026-00001"
    assert parsed["conversation_id"] == "AG_query_999"
    assert parsed["transaction_id"] == "TGH7SK61SU"


def test_parse_status_result_failed_maps_to_one():
    parsed = parse_status_result(_status_result_payload(status="Failed"))
    assert parsed["result_code"] == 1


def test_parse_status_result_unknown_status_is_none():
    parsed = parse_status_result(_status_result_payload(status="Initiated"))
    assert parsed["result_code"] is None


def test_parse_status_result_originator_falls_back_to_occasion():
    # No OriginatorConversationID/OriginalConversationID in params — only Occasion.
    params = [
        {"Key": "TransactionStatus", "Value": "Completed"},
        {"Key": "Occasion", "Value": "MPPR-2026-00042"},
    ]
    parsed = parse_status_result(_status_result_payload(params=params))
    assert parsed["originator_conversation_id"] == "MPPR-2026-00042"


def test_parse_status_result_originator_falls_back_to_result_level_occasion():
    # Nothing at all in ResultParameters that identifies the request; only the
    # top-level Result.Occasion (as Daraja echoes the query's own Occasion field).
    payload = {"Result": {"Occasion": "MPPR-2026-00099", "ResultParameters": {"ResultParameter": [
        {"Key": "TransactionStatus", "Value": "Completed"},
    ]}}}
    parsed = parse_status_result(payload)
    assert parsed["originator_conversation_id"] == "MPPR-2026-00099"


# --- handle_status_result: routes a status-query result onto reconcile_payout ---

def test_handle_status_result_completed_enqueues_reconcile_without_conversation_check(payout_env):
    payout_env.db.docs["MPPR-2026-00001"] = {"status": "Sending", "conversation_id": "AG_20260727_abc123"}

    handle_status_result(_status_result_payload(status="Completed"))

    assert len(payout_env.enqueued) == 1
    job = payout_env.enqueued[0]
    assert job["method"] == "swdpay.outbound.reconcile_payout"
    assert job["job_id"] == "swdpay-payout-reconcile-MPPR-2026-00001-status"
    assert job["deduplicate"] is True
    assert job["enqueue_after_commit"] is True
    assert job["request_name"] == "MPPR-2026-00001"
    assert job["verify_conversation"] is False
    assert job["parsed"]["result_code"] == 0


def test_handle_status_result_inconclusive_logs_and_enqueues_nothing(payout_env):
    payout_env.db.docs["MPPR-2026-00001"] = {"status": "Sending"}

    handle_status_result(_status_result_payload(status="Pending"))

    assert payout_env.enqueued == []
    assert len(payout_env.logged_errors) == 1
    assert "inconclusive" in payout_env.logged_errors[0]["title"]


def test_handle_status_result_unknown_originator_logs_and_enqueues_nothing(payout_env):
    handle_status_result(_status_result_payload(status="Completed"))  # MPPR-2026-00001 never seeded

    assert payout_env.enqueued == []
    assert len(payout_env.logged_errors) == 1
    assert "unknown request" in payout_env.logged_errors[0]["title"]


# --- plan_receipt_attach: the attach_receipt guard (Paid-only, own-request-only) ---

def test_receipt_attach_only_for_paid_requests_by_requester():
    assert plan_receipt_attach("Paid", "tech@x.com", "tech@x.com") is None
    assert "Paid" in plan_receipt_attach("Sending", "tech@x.com", "tech@x.com")
    assert "own" in plan_receipt_attach("Paid", "tech@x.com", "other@x.com")


# --- reject_request / approve_and_send: must decide off a row-locked status read,
# not a possibly-stale in-memory doc.status (final-review Finding #1) ---

def test_reject_request_raises_when_locked_status_is_sending(payout_env, monkeypatch):
    # doc.status is cached as "Requested" (Requested -> Rejected is allowed) but a
    # concurrent worker moves the row to "Sending" between get_doc and the locked
    # read — assert_transition must see "Sending" (not allowed -> Rejected), not
    # the stale "Requested" the pre-fix code would have used.
    payout_env.db.docs["MPPR-2026-00001"] = {"status": "Requested"}
    monkeypatch.setattr(frappe_stub, "get_roles", lambda: ["M-Pesa Payment Approver"])

    original_get_doc = frappe_stub.get_doc

    def racy_get_doc(doctype, name=None):
        doc = original_get_doc(doctype, name)
        payout_env.db.docs[name]["status"] = "Sending"  # the race
        return doc

    monkeypatch.setattr(frappe_stub, "get_doc", racy_get_doc)

    with pytest.raises(Exception, match="Cannot move"):
        reject_request("MPPR-2026-00001", "wrong request")


def test_approve_and_send_raises_when_locked_status_is_paid(payout_env, monkeypatch):
    # doc.status is cached as "Requested" (Requested -> Approved is allowed) but a
    # concurrent worker has already moved the row all the way to "Paid" — plan_approval
    # must see the locked "Paid" (terminal), not the stale "Requested".
    payout_env.db.docs["MPPR-2026-00001"] = {
        "status": "Requested", "amount": 1500, "payee_name": "Kamau Electricals",
        "recipient_type": "Buy Goods Till", "till_number": "832909",
    }
    monkeypatch.setattr(frappe_stub, "get_roles", lambda: ["M-Pesa Payment Approver"])
    monkeypatch.setattr(
        frappe_stub, "get_single",
        lambda doctype: types.SimpleNamespace(enable_outbound_payments=True, max_request_amount=None),
    )

    original_get_doc = frappe_stub.get_doc

    def racy_get_doc(doctype, name=None):
        doc = original_get_doc(doctype, name)
        payout_env.db.docs[name]["status"] = "Paid"  # the race
        return doc

    monkeypatch.setattr(frappe_stub, "get_doc", racy_get_doc)

    with pytest.raises(Exception, match="Cannot move"):
        approve_and_send("MPPR-2026-00001")

    # And it must not have progressed the doc to Approved before raising.
    assert payout_env.db.docs["MPPR-2026-00001"]["status"] == "Paid"


# --- send_payment_request: Daraja's synchronous ResponseCode gate (Finding #2) ---

def _sending_settings(**overrides):
    base = dict(
        enable_outbound_payments=True, max_request_amount=None,
        initiator_name="apiop", security_certificate="PEM-CERT",
        paybill_number="4165101", b2c_shortcode=None,
        callback_base_url="https://erp.example.com",
    )
    base.update(overrides)
    settings = types.SimpleNamespace(**base)
    settings.get_password = lambda field: {"initiator_password": "s3cret", "callback_secret": "S3CRET"}[field]
    return settings


def _approved_till_doc():
    return {
        "status": "Approved", "amount": 1500, "payee_name": "Kamau Electricals",
        "recipient_type": "Buy Goods Till", "till_number": "832909",
        "paybill_number": None, "account_reference": None, "phone_number": None,
        "description": "Cable and breakers",
    }


def test_send_payment_request_nonzero_response_code_marks_failed(payout_env, monkeypatch):
    payout_env.db.docs["MPPR-2026-00001"] = _approved_till_doc()
    monkeypatch.setattr(frappe_stub, "get_single", lambda doctype: _sending_settings())
    monkeypatch.setattr("swdpay.daraja.encrypt_security_credential", lambda password, cert: "ENCRYPTED")
    monkeypatch.setattr(
        "swdpay.daraja.daraja_post",
        lambda settings, path, body: {"ResponseCode": "1", "ResponseDescription": "Initiator invalid"},
    )

    send_payment_request("MPPR-2026-00001")

    doc = payout_env.db.docs["MPPR-2026-00001"]
    assert doc["status"] == "Failed"
    assert "ResponseCode 1" in doc["error_message"]
    assert "Initiator invalid" in doc["error_message"]
    assert "conversation_id" not in doc


def test_send_payment_request_zero_response_code_stores_conversation_id(payout_env, monkeypatch):
    payout_env.db.docs["MPPR-2026-00001"] = _approved_till_doc()
    monkeypatch.setattr(frappe_stub, "get_single", lambda doctype: _sending_settings())
    monkeypatch.setattr("swdpay.daraja.encrypt_security_credential", lambda password, cert: "ENCRYPTED")
    monkeypatch.setattr(
        "swdpay.daraja.daraja_post",
        lambda settings, path, body: {
            "ResponseCode": "0",
            "ConversationID": "AG_20260727_abc123",
            "OriginatorConversationID": "AG_20260727_orig456",
        },
    )

    send_payment_request("MPPR-2026-00001")

    doc = payout_env.db.docs["MPPR-2026-00001"]
    assert doc["status"] == "Sending"
    # Both Daraja-issued ids from the sync response are captured — v1 doesn't accept
    # a caller-supplied OriginatorConversationID, so this is the only place it's known.
    assert doc["conversation_id"] == "AG_20260727_abc123"
    assert doc["originator_conversation_id"] == "AG_20260727_orig456"


def test_send_payment_request_missing_originator_id_falls_back_to_doc_name(payout_env, monkeypatch):
    # A v3-style/edge response that omits OriginatorConversationID must not clobber
    # correlation — fall back to the doc name (the pre-dispatch default).
    payout_env.db.docs["MPPR-2026-00001"] = _approved_till_doc()
    monkeypatch.setattr(frappe_stub, "get_single", lambda doctype: _sending_settings())
    monkeypatch.setattr("swdpay.daraja.encrypt_security_credential", lambda password, cert: "ENCRYPTED")
    monkeypatch.setattr(
        "swdpay.daraja.daraja_post",
        lambda settings, path, body: {"ResponseCode": "0", "ConversationID": "AG_20260727_abc123"},
    )

    send_payment_request("MPPR-2026-00001")

    doc = payout_env.db.docs["MPPR-2026-00001"]
    assert doc["originator_conversation_id"] == "MPPR-2026-00001"


# --- parse_balance_result: tolerant extraction of the AccountBalance parameter ---

def test_parse_balance_result_extracts_account_balance():
    payload = {"Result": {
        "ResultCode": 0, "ResultDesc": "The service request is processed successfully.",
        "ResultParameters": {"ResultParameter": [
            {"Key": "AccountBalance", "Value": "Working Account|KES|150000.00|150000.00|0.00|0.00"},
            {"Key": "BOCompletedTime", "Value": 20260727120000},
        ]},
    }}
    parsed = parse_balance_result(payload)
    assert parsed["result_code"] == 0
    assert parsed["result_desc"] == "The service request is processed successfully."
    assert parsed["account_balance"] == "Working Account|KES|150000.00|150000.00|0.00|0.00"


def test_parse_balance_result_tolerant_of_missing_parameter():
    payload = {"Result": {"ResultCode": 0, "ResultDesc": "OK", "ResultParameters": {"ResultParameter": []}}}
    parsed = parse_balance_result(payload)
    assert parsed["account_balance"] is None
    assert parsed["result_code"] == 0


def test_parse_balance_result_tolerant_of_empty_payload():
    assert parse_balance_result({}) == {"result_code": None, "result_desc": None, "account_balance": None}
    assert parse_balance_result(None) == {"result_code": None, "result_desc": None, "account_balance": None}


# --- test_initiator_balance: role/enabled guards + the AccountBalance request shape ---

def test_test_initiator_balance_requires_a_role(monkeypatch):
    monkeypatch.setattr(frappe_stub, "get_roles", lambda: [])
    with pytest.raises(Exception, match="permission"):
        call_test_initiator_balance()


def test_test_initiator_balance_requires_outbound_enabled(monkeypatch):
    monkeypatch.setattr(frappe_stub, "get_roles", lambda: ["M-Pesa Payment Approver"])
    monkeypatch.setattr(frappe_stub, "get_single", lambda doctype: _sending_settings(enable_outbound_payments=False))
    with pytest.raises(Exception, match="Enable outbound payments"):
        call_test_initiator_balance()


def test_test_initiator_balance_sends_account_balance_query(monkeypatch):
    monkeypatch.setattr(frappe_stub, "get_roles", lambda: ["M-Pesa Payment Approver"])
    monkeypatch.setattr(frappe_stub, "get_single", lambda doctype: _sending_settings())
    monkeypatch.setattr("swdpay.daraja.encrypt_security_credential", lambda password, cert: "ENCRYPTED")

    captured = {}

    def fake_daraja_post(settings, path, body):
        captured["path"] = path
        captured["body"] = body
        return {"ResponseCode": "0", "ResponseDescription": "Accept the service request successfully."}

    monkeypatch.setattr("swdpay.daraja.daraja_post", fake_daraja_post)

    response = call_test_initiator_balance()

    assert captured["path"] == "/mpesa/accountbalance/v1/query"
    body = captured["body"]
    assert body["CommandID"] == "AccountBalance"
    assert body["Initiator"] == "apiop"
    assert body["SecurityCredential"] == "ENCRYPTED"
    assert body["PartyA"] == "4165101"
    assert body["IdentifierType"] == "4"
    assert body["ResultURL"] == "https://erp.example.com/api/method/swdpay.api.callbacks.balance_result?token=S3CRET"
    assert body["QueueTimeOutURL"] == body["ResultURL"]
    assert response["ResponseCode"] == "0"
