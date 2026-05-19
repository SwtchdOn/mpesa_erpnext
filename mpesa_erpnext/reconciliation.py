import json
from datetime import datetime

import frappe
from frappe.utils import flt, getdate


STATUS_NEW = "New"
STATUS_MATCHED = "Matched"
STATUS_RECONCILED = "Reconciled"
STATUS_DUPLICATE = "Duplicate"
STATUS_EXCEPTION = "Exception"


def normalize_payload(payload):
    if isinstance(payload, str):
        payload = json.loads(payload)

    return {
        "mpesa_receipt_number": payload.get("TransID"),
        "amount": flt(payload.get("TransAmount")),
        "paybill_number": payload.get("BusinessShortCode") or payload.get("ShortCode") or payload.get("PayBillNumber"),
        "bill_ref_number": payload.get("BillRefNumber"),
        "phone_number": payload.get("MSISDN"),
        "payer_name": " ".join(
            filter(
                None,
                [
                    payload.get("FirstName"),
                    payload.get("MiddleName"),
                    payload.get("LastName"),
                ],
            )
        ),
        "transaction_date": parse_mpesa_datetime(payload.get("TransTime")),
        "raw_payload": json.dumps(payload, indent=2, sort_keys=True),
    }


def parse_mpesa_datetime(value):
    if not value:
        return None

    value = str(value)
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S")
    except ValueError:
        return frappe.utils.get_datetime(value)


def save_transaction_from_payload(payload):
    data = normalize_payload(payload)
    receipt = data.get("mpesa_receipt_number")

    if not receipt:
        data["mpesa_receipt_number"] = f"MISSING-{frappe.generate_hash(length=10)}"
        return _save_exception_transaction(data, "Missing M-Pesa receipt number")

    existing = frappe.db.exists("M-Pesa Transaction", {"mpesa_receipt_number": receipt})
    if existing:
        return frappe.get_doc("M-Pesa Transaction", existing), True

    transaction = frappe.get_doc(
        {
            "doctype": "M-Pesa Transaction",
            "status": STATUS_NEW,
            **data,
        }
    )
    transaction.insert(ignore_permissions=True)
    return transaction, False


def process_payload(payload, auto_commit=False):
    transaction, duplicate = save_transaction_from_payload(payload)
    if duplicate:
        return transaction

    try:
        reconcile_transaction(transaction.name)
    except Exception:
        frappe.log_error(frappe.get_traceback(), "M-Pesa reconciliation failed")

    if auto_commit:
        frappe.db.commit()

    return frappe.get_doc("M-Pesa Transaction", transaction.name)


def reconcile_transaction(transaction_name, manual=False):
    transaction = frappe.get_doc("M-Pesa Transaction", transaction_name)
    settings = frappe.get_single("M-Pesa Settings")

    if transaction.status == STATUS_RECONCILED and transaction.payment_entry:
        return transaction

    try:
        payment_mapping = get_payment_account_mapping(transaction, settings)
        invoice = _get_valid_sales_invoice(transaction, settings, throw=False)
        customer = invoice.customer if invoice else _get_valid_customer_by_phone(transaction, settings)

        transaction.sales_invoice = invoice.name if invoice else None
        transaction.customer = customer
        transaction.status = STATUS_MATCHED
        transaction.error_message = None
        transaction.save(ignore_permissions=True)

        if transaction.payment_entry:
            transaction.status = STATUS_RECONCILED
            transaction.save(ignore_permissions=True)
            return transaction

        payment_entry = create_payment_entry(
            transaction,
            settings,
            payment_mapping,
            invoice=invoice,
            customer=customer,
        )
        transaction.payment_entry = payment_entry.name
        transaction.status = STATUS_RECONCILED
        transaction.error_message = None
        transaction.save(ignore_permissions=True)
        return transaction
    except Exception as exc:
        transaction.status = STATUS_EXCEPTION
        transaction.error_message = str(exc)
        transaction.save(ignore_permissions=True)
        if manual:
            raise
        return transaction


def create_payment_entry(transaction, settings, payment_mapping, invoice=None, customer=None):
    amount = flt(transaction.amount)

    payment_entry = frappe.new_doc("Payment Entry")
    payment_entry.payment_type = "Receive"
    payment_entry.company = invoice.company if invoice else settings.company
    payment_entry.posting_date = getdate(transaction.transaction_date) if transaction.transaction_date else getdate()
    payment_entry.mode_of_payment = payment_mapping["mode_of_payment"]
    payment_entry.party_type = "Customer"
    payment_entry.party = customer or invoice.customer
    payment_entry.paid_from = (
        invoice.debit_to
        if invoice
        else _get_customer_receivable_account(customer, settings, payment_mapping)
    )
    payment_entry.paid_to = payment_mapping["paid_to_account"]
    payment_entry.paid_amount = amount
    payment_entry.received_amount = amount
    payment_entry.reference_no = transaction.mpesa_receipt_number
    payment_entry.reference_date = payment_entry.posting_date

    if invoice:
        outstanding = flt(invoice.outstanding_amount)
        allocated_amount = min(amount, outstanding)
        payment_entry.append(
            "references",
            {
                "reference_doctype": "Sales Invoice",
                "reference_name": invoice.name,
                "total_amount": invoice.grand_total,
                "outstanding_amount": outstanding,
                "allocated_amount": allocated_amount,
            },
        )

    payment_entry.insert(ignore_permissions=True)

    if settings.auto_submit_payment_entry:
        payment_entry.submit()

    return payment_entry


def get_payment_account_mapping(transaction, settings):
    paybill_number = transaction.paybill_number or settings.paybill_number
    default_mapping = None

    for row in settings.payment_account_mappings:
        mapping = {
            "mode_of_payment": row.mode_of_payment,
            "paid_to_account": row.paid_to_account,
            "receivable_account": row.receivable_account,
        }

        if row.paybill_number and paybill_number and row.paybill_number == paybill_number:
            return mapping

        if row.is_default:
            default_mapping = mapping

    mapping = default_mapping or {
        "mode_of_payment": settings.default_mode_of_payment,
        "paid_to_account": settings.paid_to_account,
        "receivable_account": settings.default_receivable_account,
    }

    if not mapping.get("mode_of_payment"):
        frappe.throw("Mode of Payment is missing in M-Pesa Settings")

    if not mapping.get("paid_to_account"):
        frappe.throw("Paid To Account is missing in M-Pesa Settings")

    return mapping


def _get_valid_sales_invoice(transaction, settings, throw=True):
    def fail(message):
        if throw:
            frappe.throw(message)
        return None

    if not transaction.bill_ref_number:
        return fail("Bill Ref Number is required")

    if flt(transaction.amount) <= 0:
        return fail("Amount is zero")

    invoice_name = frappe.db.exists("Sales Invoice", transaction.bill_ref_number)
    if not invoice_name:
        return fail(f"No Sales Invoice found for account number {transaction.bill_ref_number}")

    invoice = frappe.get_doc("Sales Invoice", invoice_name)

    if invoice.docstatus != 1:
        frappe.throw(f"Sales Invoice {invoice.name} is not submitted")

    if invoice.company != settings.company:
        frappe.throw(f"Sales Invoice {invoice.name} belongs to company {invoice.company}, not {settings.company}")

    if flt(invoice.outstanding_amount) <= 0:
        frappe.throw(f"Sales Invoice {invoice.name} is already fully paid")

    if flt(transaction.amount) < flt(invoice.outstanding_amount) and not settings.allow_partial_payments:
        frappe.throw("Partial payments are disabled")

    if flt(transaction.amount) > flt(invoice.outstanding_amount) and not settings.allow_overpayments:
        frappe.throw("Payment amount exceeds outstanding amount")

    return invoice


def _get_valid_customer_by_phone(transaction, settings):
    if not settings.allow_customer_phone_matching:
        frappe.throw(f"No Sales Invoice found for account number {transaction.bill_ref_number}")

    if flt(transaction.amount) <= 0:
        frappe.throw("Amount is zero")

    phone = normalize_phone(transaction.phone_number)
    if not phone:
        frappe.throw("Phone Number is required for customer phone matching")

    customers = _find_customers_by_phone(phone)
    if not customers:
        frappe.throw(f"No Customer found with phone number {transaction.phone_number}")

    if len(customers) > 1:
        frappe.throw(f"Multiple Customers found with phone number {transaction.phone_number}")

    return customers[0]


def _find_customers_by_phone(phone):
    matches = []
    for customer in frappe.get_all("Customer", fields=["name", "mobile_no", "phone"]):
        if normalize_phone(customer.mobile_no) == phone or normalize_phone(customer.phone) == phone:
            matches.append(customer.name)

    return matches


def _get_customer_receivable_account(customer, settings, payment_mapping):
    if payment_mapping.get("receivable_account"):
        return payment_mapping["receivable_account"]

    try:
        from erpnext.accounts.party import get_party_account

        return get_party_account("Customer", customer, settings.company)
    except Exception:
        frappe.throw("Default Receivable Account is required for customer phone matching")


def normalize_phone(value):
    if not value:
        return None

    digits = "".join(char for char in str(value) if char.isdigit())
    if digits.startswith("0") and len(digits) == 10:
        return f"254{digits[1:]}"
    if digits.startswith("7") and len(digits) == 9:
        return f"254{digits}"
    return digits


def _save_exception_transaction(data, error_message):
    transaction = frappe.get_doc(
        {
            "doctype": "M-Pesa Transaction",
            "status": STATUS_EXCEPTION,
            "error_message": error_message,
            **data,
        }
    )
    transaction.insert(ignore_permissions=True)
    return transaction, False
