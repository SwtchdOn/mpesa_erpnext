import sys
import types

import pytest

# Use the canonical frappe stub registered by conftest.py
frappe_stub = sys.modules["frappe"]
StubValidationError = frappe_stub.StubValidationError

from swdpay import reconciliation
from swdpay.qr import format_qr_amount, mpesa_qr_payload
from swdpay.reconciliation import (
    get_phone_variants,
    get_existing_payment_entry_for_receipt,
    get_small_difference_write_off_amount,
    is_hashed_msisdn,
    normalize_payload,
    normalize_phone,
    plan_reference_allocation,
    reconcile_transaction,
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


def test_mpesa_qr_payload_matches_pesaqr_formats():
    assert mpesa_qr_payload(13000, paybill="888880", account="SINV-2026-00045") == "PB|888880|13000|SINV-2026-00045"
    assert mpesa_qr_payload(100, till="123456") == "BG|123456|100"
    assert mpesa_qr_payload(50, phone="0712345678") == "SM|0712345678|50"


def test_mpesa_qr_amount_rounds_up_to_whole_shillings():
    assert format_qr_amount(914.25) == "915"
    assert format_qr_amount(914) == "914"
    assert mpesa_qr_payload(914.25, paybill="888880", account="SINV-001") == "PB|888880|915|SINV-001"


def test_mpesa_qr_payload_requires_target_and_account():
    import pytest

    with pytest.raises(StubValidationError):
        mpesa_qr_payload(100)

    with pytest.raises(StubValidationError):
        mpesa_qr_payload(100, paybill="888880")


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


# --- Automatic Sales Order matching in reconcile_transaction ----------------
#
# The flow tests below stub only the frappe surface reconcile_transaction and the
# matching validators touch (frappe.db.exists, frappe.get_doc, frappe.get_single)
# and replace create_payment_entry so they assert which match path was taken,
# not ERPNext's Payment Entry mechanics.


class _Doc(types.SimpleNamespace):
    """Minimal frappe Document stand-in: .get(), .save() and .append()."""

    def get(self, key, default=None):
        return getattr(self, key, default)

    def save(self, ignore_permissions=False):
        pass

    def append(self, fieldname, row):
        getattr(self, fieldname).append(row)


def _make_settings(**overrides):
    settings = types.SimpleNamespace(
        company="Switched On",
        allow_partial_payments=True,
        allow_overpayments=False,
        allow_customer_phone_matching=False,
        enable_small_difference_write_off=False,
        max_write_off_difference=0,
        auto_submit_payment_entry=True,
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


def _make_order(**overrides):
    order = _Doc(
        name="SAL-ORD-2026-00007",
        docstatus=1,
        company="Switched On",
        customer="CUST-0001",
        per_billed=0,
        base_grand_total=10000,
        grand_total=10000,
        advance_paid=0,
    )
    for key, value in overrides.items():
        setattr(order, key, value)
    return order


def _make_transaction(**overrides):
    transaction = _Doc(
        name="MPTX-0001",
        status="New",
        mpesa_receipt_number="UEOCI5CWU6",
        bill_ref_number="SAL-ORD-2026-00007",
        amount=10000,
        phone_number=None,
        customer=None,
        sales_invoice=None,
        sales_order=None,
        payment_entry=None,
        error_message=None,
        transaction_date=None,
        paybill_number=None,
        reconciliation_attempt_count=0,
        last_reconciliation_attempt=None,
        reconciliation_attempts=[],
        flags=types.SimpleNamespace(),
    )
    for key, value in overrides.items():
        setattr(transaction, key, value)
    return transaction


def _install_reconcile_env(monkeypatch, transaction, settings, docs):
    """docs maps (doctype, name) to document stubs; returns the list of
    create_payment_entry calls made by reconcile_transaction."""
    created = []

    def fake_exists(doctype, name):
        if isinstance(name, dict):
            # get_existing_payment_entry_for_receipt lookup — no prior entry
            return None
        return name if (doctype, name) in docs else None

    def fake_get_doc(doctype, name):
        if doctype == "M-Pesa Transaction" and name == transaction.name:
            return transaction
        if (doctype, name) in docs:
            return docs[(doctype, name)]
        raise StubValidationError(f"{doctype} {name} not found")

    def fake_create_payment_entry(txn, txn_settings, mapping, invoice=None, order=None, customer=None):
        created.append({"invoice": invoice, "order": order, "customer": customer})
        txn.payment_entry = "ACC-PAY-2026-00001"
        return types.SimpleNamespace(name="ACC-PAY-2026-00001")

    monkeypatch.setattr(frappe_stub, "db", types.SimpleNamespace(exists=fake_exists))
    monkeypatch.setattr(frappe_stub, "get_doc", fake_get_doc)
    monkeypatch.setattr(frappe_stub, "get_single", lambda doctype: settings)
    monkeypatch.setattr(
        reconciliation,
        "get_payment_account_mapping",
        lambda txn, txn_settings: {"mode_of_payment": "M-Pesa", "paid_to_account": "M-Pesa - SO"},
    )
    monkeypatch.setattr(
        reconciliation,
        "get_sales_order_exchange_rate",
        lambda order, receivable_account=None: 1,
    )
    monkeypatch.setattr(reconciliation, "create_payment_entry", fake_create_payment_entry)
    return created


def test_reconcile_transaction_matches_sales_order_reference(monkeypatch):
    order = _make_order()
    transaction = _make_transaction()
    created = _install_reconcile_env(
        monkeypatch,
        transaction,
        _make_settings(),
        {("Sales Order", order.name): order},
    )

    result = reconcile_transaction("MPTX-0001")

    assert created == [{"invoice": None, "order": order, "customer": "CUST-0001"}]
    assert result.sales_order == "SAL-ORD-2026-00007"
    assert result.sales_invoice is None
    assert result.customer == "CUST-0001"
    assert result.status == "Reconciled"
    assert result.error_message is None


@pytest.mark.parametrize(
    "order_overrides",
    [
        {"docstatus": 2},  # cancelled
        {"company": "Other Company"},  # wrong company
        {"advance_paid": 10000},  # fully paid via prior advances
        {"per_billed": 100},  # fully billed
    ],
)
def test_reconcile_transaction_invalid_sales_order_falls_through_to_phone(monkeypatch, order_overrides):
    order = _make_order(**order_overrides)
    transaction = _make_transaction(phone_number="254712345678")
    created = _install_reconcile_env(
        monkeypatch,
        transaction,
        _make_settings(allow_customer_phone_matching=True),
        {("Sales Order", order.name): order},
    )
    monkeypatch.setattr(reconciliation, "_find_customers_by_phone", lambda phone: ["CUST-PHONE"])

    result = reconcile_transaction("MPTX-0001")

    assert created == [{"invoice": None, "order": None, "customer": "CUST-PHONE"}]
    assert result.sales_order is None
    assert result.sales_invoice is None
    assert result.customer == "CUST-PHONE"
    assert result.status == "Reconciled"


def test_reconcile_transaction_prefers_sales_invoice_over_sales_order(monkeypatch):
    invoice = _Doc(
        name="ACC-SINV-2026-00045",
        docstatus=1,
        company="Switched On",
        customer="CUST-INV",
        outstanding_amount=10000,
    )
    # a Sales Order under the same reference must lose to the invoice match
    order = _make_order(name=invoice.name)
    transaction = _make_transaction(bill_ref_number=invoice.name)
    created = _install_reconcile_env(
        monkeypatch,
        transaction,
        _make_settings(),
        {
            ("Sales Invoice", invoice.name): invoice,
            ("Sales Order", order.name): order,
        },
    )
    monkeypatch.setattr(
        reconciliation,
        "get_invoice_outstanding_in_company_currency",
        lambda inv: float(inv.outstanding_amount),
    )

    result = reconcile_transaction("MPTX-0001")

    assert created == [{"invoice": invoice, "order": None, "customer": "CUST-INV"}]
    assert result.sales_invoice == invoice.name
    assert result.sales_order is None
    assert result.status == "Reconciled"


def test_get_valid_sales_order_throws_when_asked(monkeypatch):
    order = _make_order(docstatus=2)
    transaction = _make_transaction()
    _install_reconcile_env(
        monkeypatch,
        transaction,
        _make_settings(),
        {("Sales Order", order.name): order},
    )

    with pytest.raises(StubValidationError):
        reconciliation._get_valid_sales_order(transaction, _make_settings(), throw=True)

    assert reconciliation._get_valid_sales_order(transaction, _make_settings(), throw=False) is None


def test_manual_sales_order_path_uses_shared_validator(monkeypatch):
    order = _make_order(advance_paid=10000)
    transaction = _make_transaction(bill_ref_number=None)
    _install_reconcile_env(
        monkeypatch,
        transaction,
        _make_settings(),
        {("Sales Order", order.name): order},
    )

    with pytest.raises(StubValidationError):
        reconciliation._get_manual_sales_order(transaction, _make_settings(), sales_order=order.name)

    valid_order = _make_order()
    monkeypatch.setattr(
        frappe_stub,
        "get_doc",
        lambda doctype, name: valid_order,
    )
    monkeypatch.setattr(
        frappe_stub,
        "db",
        types.SimpleNamespace(exists=lambda doctype, name: name if name == valid_order.name else None),
    )
    manual_order = reconciliation._get_manual_sales_order(
        transaction, _make_settings(), sales_order=valid_order.name
    )
    assert manual_order is valid_order
