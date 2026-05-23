import sys
import types


frappe_stub = types.ModuleType("frappe")
frappe_utils_stub = types.ModuleType("frappe.utils")
frappe_utils_stub.flt = lambda value: float(value or 0)
frappe_utils_stub.getdate = lambda value=None: value
frappe_stub.utils = frappe_utils_stub
sys.modules.setdefault("frappe", frappe_stub)
sys.modules.setdefault("frappe.utils", frappe_utils_stub)

from swdpay.reconciliation import get_phone_variants, normalize_payload, normalize_phone


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
