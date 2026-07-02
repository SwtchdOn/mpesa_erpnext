import sys
import types


frappe_stub = types.ModuleType("frappe")
frappe_utils_stub = types.ModuleType("frappe.utils")
frappe_utils_stub.flt = lambda value, precision=None: (
    round(float(value or 0), precision) if precision is not None else float(value or 0)
)
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
    plan_reference_allocation,
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
    assert get_small_difference_write_off_amount(914.25, 914.25, enabled_settings) == 0


def test_small_difference_write_off_handles_overpayments():
    disabled_settings = types.SimpleNamespace(
        enable_small_difference_write_off=False,
        max_write_off_difference=1,
    )
    enabled_settings = types.SimpleNamespace(
        enable_small_difference_write_off=True,
        max_write_off_difference=1,
    )

    assert get_small_difference_write_off_amount(915, 914.25, disabled_settings) == 0
    assert get_small_difference_write_off_amount(915, 914.25, enabled_settings) == -0.75
    assert get_small_difference_write_off_amount(916, 914.25, enabled_settings) == 0


def test_plan_reference_allocation_company_currency_matches_legacy_behaviour():
    exact = plan_reference_allocation(1000, 1000, 1)
    assert exact == {"paid_amount": 1000, "allocated_amount": 1000, "deduction_amount": 0}

    write_off = plan_reference_allocation(999.5, 1000, 1, write_off_amount=0.5)
    assert write_off == {"paid_amount": 999.5, "allocated_amount": 1000, "deduction_amount": 0.5}

    partial = plan_reference_allocation(400, 1000, 1)
    assert partial == {"paid_amount": 400, "allocated_amount": 400, "deduction_amount": 0}

    overpay_advance = plan_reference_allocation(1200, 1000, 1)
    assert overpay_advance == {"paid_amount": 1200, "allocated_amount": 1000, "deduction_amount": 0}


def test_plan_reference_allocation_converts_foreign_currency_references():
    # foreign-currency rows never carry a write off deduction: ERPNext books
    # the base paid vs base received difference to Exchange Gain/Loss itself

    # USD 100 invoice at 130 KES/USD, customer pays the exact KES equivalent
    exact = plan_reference_allocation(13000, 100, 130)
    assert exact == {"paid_amount": 100, "allocated_amount": 100, "deduction_amount": 0}

    # small KES shortfall within the write off tolerance settles the invoice
    # fully; paying the full outstanding leaves the 0.5 KES gap to ERPNext
    write_off = plan_reference_allocation(12999.5, 100, 130, write_off_amount=0.5)
    assert write_off == {"paid_amount": 100, "allocated_amount": 100, "deduction_amount": 0}

    # small KES overpayment within the tolerance settles the same way
    rounding_gain = plan_reference_allocation(13000.5, 100, 130, write_off_amount=-0.5)
    assert rounding_gain == {"paid_amount": 100, "allocated_amount": 100, "deduction_amount": 0}


def test_plan_reference_allocation_handles_foreign_partial_payments():
    # 6,500 KES against a USD 100 invoice at a ragged 133.7 rate:
    # 6,500 / 133.7 = 48.6163... rounds to 48.62 USD; the 0.49 KES conversion
    # residue is booked by ERPNext as exchange gain/loss
    plan = plan_reference_allocation(6500, 100, 133.7)

    assert plan["paid_amount"] == 48.62
    assert plan["allocated_amount"] == 48.62
    assert plan["deduction_amount"] == 0


def test_plan_reference_allocation_keeps_large_foreign_overpayment_as_credit():
    plan = plan_reference_allocation(13500, 100, 130)

    assert plan["allocated_amount"] == 100
    assert plan["paid_amount"] == 103.85
    assert plan["deduction_amount"] == 0


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
