import frappe

from swdpay.reconciliation import normalize_phone

ALLOWED_TRANSITIONS = {
    "Requested": {"Approved", "Rejected", "Cancelled"},
    "Approved": {"Sending", "Failed", "Cancelled"},
    "Sending": {"Paid", "Failed"},
    "Paid": set(),
    "Failed": set(),
    "Rejected": set(),
    "Cancelled": set(),
}


def assert_transition(current, new):
    if new not in ALLOWED_TRANSITIONS.get(current, set()):
        frappe.throw(f"Cannot move payment request from {current} to {new}")


def validate_payment_request(doc, max_amount=None):
    amount = doc.amount or 0
    if amount <= 0:
        frappe.throw("Amount must be greater than zero")
    if amount != int(amount):
        frappe.throw("M-Pesa payments must be whole shillings (no cents)")
    if max_amount and amount > max_amount:
        frappe.throw(f"Amount exceeds the maximum request amount of {max_amount}")
    if not (doc.payee_name or "").strip():
        frappe.throw("Payee name is required")

    if doc.recipient_type == "Buy Goods Till":
        if not (doc.till_number or "").strip():
            frappe.throw("Till number is required for Buy Goods payments")
    elif doc.recipient_type == "Paybill":
        if not (doc.paybill_number or "").strip():
            frappe.throw("Paybill number is required for Paybill payments")
        if not (doc.account_reference or "").strip():
            frappe.throw("Account reference is required for Paybill payments")
    elif doc.recipient_type == "Phone":
        normalized = normalize_phone(doc.phone_number)
        # Valid normalized phone should be 12 digits starting with 254
        if not normalized or not (len(str(normalized)) == 12 and str(normalized).startswith("254")):
            frappe.throw("Enter a valid Kenyan phone number")
        doc.phone_number = normalized
    else:
        frappe.throw("Recipient type is required")
