import sys
import types


frappe_stub = types.ModuleType("frappe")
frappe_utils_stub = types.ModuleType("frappe.utils")
frappe_utils_stub.flt = lambda value: float(value or 0)
frappe_utils_stub.getdate = lambda value=None: value
frappe_utils_stub.now_datetime = lambda: "2026-05-24 00:00:00"
frappe_stub.utils = frappe_utils_stub
sys.modules.setdefault("frappe", frappe_stub)
sys.modules.setdefault("frappe.utils", frappe_utils_stub)

from swdpay.reconciliation import (
    get_phone_variants,
    get_existing_payment_entry_for_receipt,
    get_small_difference_write_off_amount,
    is_hashed_msisdn,
    normalize_payload,
    normalize_phone,
)


def test_normalize_payload_builds_transaction_fields():
    payload = {
        "TransID": "RHF123ABC",
        "TransAmount": "10000",
        "BillRefNumber": "SINV-2026-00045",
        "MSISDN": "254712345678",
        "FirstName": "John",
        "MiddleName": "",
        "LastName": "Kamau",
        "TransTime": "20260519124500",
    }

    transaction = normalize_payload(payload)

    assert transaction["mpesa_receipt_number"] == "RHF123ABC"
    assert transaction["amount"] == 10000
    assert transaction["paybill_number"] is None
    assert transaction["bill_ref_number"] == "SINV-2026-00045"
    assert transaction["payer_name"] == "John Kamau"


def test_normalize_phone_supports_common_kenyan_formats():
    assert normalize_phone("254712345678") == "254712345678"
    assert normalize_phone("0712345678") == "254712345678"
    assert normalize_phone("712345678") == "254712345678"
    assert normalize_phone("+254 712 345 678") == "254712345678"


def test_get_phone_variants_returns_common_lookup_formats():
    variants = get_phone_variants("0712345678")

    assert "254712345678" in variants
    assert "0712345678" in variants
    assert "712345678" in variants
    assert "+254712345678" in variants


def test_hashed_msisdn_is_not_treated_as_phone_number():
    hashed_msisdn = "e8b2183ea2c5bfbede283ab72c0e78e64aab0bf146ec418db891d88a0e469786"
    payload = {
        "TransID": "UEOCI5CWU6",
        "TransAmount": "914.00",
        "BillRefNumber": "SINV2600057",
        "MSISDN": hashed_msisdn,
        "FirstName": "WILLIAM",
        "TransTime": "20260524091632",
    }

    transaction = normalize_payload(payload)

    assert is_hashed_msisdn(hashed_msisdn)
    assert transaction["phone_number"] is None
    assert transaction["phone_hash"] == hashed_msisdn
    assert normalize_phone(hashed_msisdn) is None


def test_small_difference_write_off_requires_enabled_tolerance():
    disabled_settings = types.SimpleNamespace(
        enable_small_difference_write_off=False,
        max_write_off_difference=1,
    )
    enabled_settings = types.SimpleNamespace(
        enable_small_difference_write_off=True,
        max_write_off_difference=1,
    )

    assert get_small_difference_write_off_amount(914, 914.25, disabled_settings) == 0
    assert get_small_difference_write_off_amount(914, 914.25, enabled_settings) == 0.25
    assert get_small_difference_write_off_amount(913, 914.25, enabled_settings) == 0
    assert get_small_difference_write_off_amount(915, 914.25, enabled_settings) == 0


def test_existing_payment_entry_lookup_ignores_blank_receipt(monkeypatch):
    def fail_exists(*args, **kwargs):
        raise AssertionError("frappe.db.exists should not be called")

    frappe_stub.db = types.SimpleNamespace(exists=fail_exists)

    assert get_existing_payment_entry_for_receipt(None) is None
    assert get_existing_payment_entry_for_receipt("") is None


def test_existing_payment_entry_lookup_uses_reference_no(monkeypatch):
    calls = []

    def fake_exists(doctype, filters):
        calls.append((doctype, filters))
        return "ACC-PAY-2026-00001"

    frappe_stub.db = types.SimpleNamespace(exists=fake_exists)

    assert get_existing_payment_entry_for_receipt("UEOCI5CWU6") == "ACC-PAY-2026-00001"
    assert calls == [
        (
            "Payment Entry",
            {
                "reference_no": "UEOCI5CWU6",
                "docstatus": ["<", 2],
            },
        )
    ]
