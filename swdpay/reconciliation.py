import json
from datetime import datetime

import frappe
from frappe.utils import flt, getdate, now_datetime


STATUS_NEW = "New"
STATUS_MATCHED = "Matched"
STATUS_RECONCILED = "Reconciled"
STATUS_DUPLICATE = "Duplicate"
STATUS_EXCEPTION = "Exception"


def normalize_payload(payload):
    if isinstance(payload, str):
        payload = json.loads(payload)

    msisdn = payload.get("MSISDN")
    phone_number = None if is_hashed_msisdn(msisdn) else msisdn
    phone_hash = msisdn if is_hashed_msisdn(msisdn) else None

    return {
        "mpesa_receipt_number": payload.get("TransID"),
        "amount": flt(payload.get("TransAmount")),
        "paybill_number": payload.get("BusinessShortCode") or payload.get("ShortCode") or payload.get("PayBillNumber"),
        "bill_ref_number": payload.get("BillRefNumber"),
        "phone_number": phone_number,
        "phone_hash": phone_hash,
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
    try:
        transaction.insert(ignore_permissions=True)
    except frappe.DuplicateEntryError:
        existing = frappe.db.exists("M-Pesa Transaction", {"mpesa_receipt_number": receipt})
        if existing:
            return frappe.get_doc("M-Pesa Transaction", existing), True
        raise

    return transaction, False


def enqueue_reconciliation(transaction_name):
    frappe.enqueue(
        "swdpay.reconciliation.reconcile_transaction",
        queue="short",
        transaction_name=transaction_name,
        source="Background Job",
        enqueue_after_commit=True,
    )


def process_payload(payload, auto_commit=False):
    transaction, duplicate = save_transaction_from_payload(payload)
    if duplicate or transaction.status == STATUS_EXCEPTION:
        if auto_commit:
            frappe.db.commit()
        return transaction

    try:
        reconcile_transaction(transaction.name, source="Manual Import")
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"M-Pesa reconciliation failed: {transaction.name}")

    if auto_commit:
        frappe.db.commit()

    return frappe.get_doc("M-Pesa Transaction", transaction.name)


def validate_c2b_payload(payload):
    data = normalize_payload(payload)
    settings = frappe.get_single("M-Pesa Settings")

    if flt(data.get("amount")) <= 0:
        return "C2B00013", "Invalid amount"

    bill_ref_number = data.get("bill_ref_number")
    if not bill_ref_number:
        return _validation_result(settings, False, "Account number is required")

    validation_error = _validate_invoice_reference(data, settings)
    if validation_error:
        return "C2B00011", validation_error

    if _reference_exists("Sales Invoice", bill_ref_number):
        return 0, "Accepted"

    if _reference_exists("Sales Order", bill_ref_number):
        return 0, "Accepted"

    if _reference_exists("Quotation", bill_ref_number):
        return 0, "Accepted"

    if settings.allow_customer_phone_matching and normalize_phone(data.get("phone_number")):
        return 0, "Accepted"

    return _validation_result(settings, False, f"No matching reference found for account number {bill_ref_number}")


def reconcile_transaction(transaction_name, manual=False, source=None):
    transaction = frappe.get_doc("M-Pesa Transaction", transaction_name)
    settings = frappe.get_single("M-Pesa Settings")
    source = source or ("Manual Retry" if manual else "Automatic")

    if transaction.status == STATUS_RECONCILED and transaction.payment_entry:
        _safe_record_reconciliation_attempt(transaction, source, "Skipped", "Transaction is already reconciled")
        return transaction

    try:
        _safe_record_reconciliation_attempt(transaction, source, "Started", "Reconciliation started", increment=True)
        payment_mapping = get_payment_account_mapping(transaction, settings)
        invoice = _get_valid_sales_invoice(transaction, settings, throw=False)
        customer = invoice.customer if invoice else _get_valid_customer_by_phone(transaction, settings)

        transaction.sales_invoice = invoice.name if invoice else None
        transaction.customer = customer
        transaction.status = STATUS_MATCHED
        transaction.error_message = None
        transaction.save(ignore_permissions=True)

        if transaction.payment_entry:
            _complete_existing_payment_entry(transaction, settings)
            transaction.status = STATUS_RECONCILED if _is_submitted_or_draft_allowed(transaction, settings) else STATUS_MATCHED
            transaction.save(ignore_permissions=True)
            _safe_record_reconciliation_attempt(transaction, source, "Success", "Existing Payment Entry completed")
            return transaction

        payment_entry = create_payment_entry(
            transaction,
            settings,
            payment_mapping,
            invoice=invoice,
            customer=customer,
        )
        transaction.status = STATUS_RECONCILED
        transaction.error_message = None
        transaction.save(ignore_permissions=True)
        _safe_record_reconciliation_attempt(transaction, source, "Success", f"Payment Entry {payment_entry.name} created")
        return transaction
    except Exception as exc:
        transaction.status = STATUS_EXCEPTION
        transaction.error_message = str(exc)
        transaction.save(ignore_permissions=True)
        _safe_record_reconciliation_attempt(transaction, source, "Exception", str(exc))
        frappe.log_error(frappe.get_traceback(), f"M-Pesa reconciliation failed: {transaction.name}")
        if manual:
            raise
        return transaction


def create_payment_entry(transaction, settings, payment_mapping, invoice=None, customer=None):
    amount = flt(transaction.amount)
    payment_entry = (
        _get_invoice_payment_entry(transaction, payment_mapping, invoice)
        if invoice
        else _get_customer_payment_entry(transaction, settings, payment_mapping, customer)
    )

    payment_entry.insert(ignore_permissions=True)
    transaction.payment_entry = payment_entry.name
    transaction.save(ignore_permissions=True)

    if settings.auto_submit_payment_entry:
        payment_entry.submit()

    return payment_entry


def _get_invoice_payment_entry(transaction, payment_mapping, invoice):
    try:
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

        payment_entry = get_payment_entry(
            "Sales Invoice",
            invoice.name,
            party_amount=flt(transaction.amount),
            bank_amount=flt(transaction.amount),
            payment_type="Receive",
        )
    except Exception:
        payment_entry = _new_payment_entry()

    _apply_common_payment_fields(payment_entry, transaction, payment_mapping, invoice.company, invoice.customer)
    payment_entry.paid_from = invoice.debit_to

    payment_entry.set("references", [])
    outstanding = flt(invoice.outstanding_amount)
    payment_entry.append(
        "references",
        {
            "reference_doctype": "Sales Invoice",
            "reference_name": invoice.name,
            "total_amount": invoice.grand_total,
            "outstanding_amount": outstanding,
            "allocated_amount": min(flt(transaction.amount), outstanding),
        },
    )
    return payment_entry


def _get_customer_payment_entry(transaction, settings, payment_mapping, customer):
    payment_entry = _new_payment_entry()
    _apply_common_payment_fields(payment_entry, transaction, payment_mapping, settings.company, customer)
    payment_entry.paid_from = _get_customer_receivable_account(customer, settings, payment_mapping)
    return payment_entry


def _new_payment_entry():
    payment_entry = frappe.new_doc("Payment Entry")
    payment_entry.payment_type = "Receive"
    payment_entry.party_type = "Customer"
    return payment_entry


def _apply_common_payment_fields(payment_entry, transaction, payment_mapping, company, customer):
    amount = flt(transaction.amount)

    payment_entry.company = company
    payment_entry.posting_date = getdate(transaction.transaction_date) if transaction.transaction_date else getdate()
    payment_entry.mode_of_payment = payment_mapping["mode_of_payment"]
    payment_entry.party_type = "Customer"
    payment_entry.party = customer
    payment_entry.paid_to = payment_mapping["paid_to_account"]
    payment_entry.paid_amount = amount
    payment_entry.received_amount = amount
    payment_entry.reference_no = transaction.mpesa_receipt_number
    payment_entry.reference_date = payment_entry.posting_date


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


def _validate_invoice_reference(data, settings):
    bill_ref_number = data.get("bill_ref_number")
    invoice_name = frappe.db.exists("Sales Invoice", bill_ref_number)
    if not invoice_name:
        return None

    transaction = frappe._dict(data)
    try:
        _get_valid_sales_invoice(transaction, settings, throw=True)
    except Exception as exc:
        return str(exc)

    return None


def _reference_exists(doctype, name):
    return bool(frappe.db.exists(doctype, name))


def _validation_result(settings, is_valid, message):
    if is_valid or not settings.enable_strict_c2b_validation:
        return 0, "Accepted"

    return "C2B00011", message


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
    matches = set()
    variants = get_phone_variants(phone)

    for customer in frappe.get_all(
        "Customer",
        filters={"mobile_no": ["in", variants]},
        pluck="name",
    ):
        matches.add(customer)

    for customer in frappe.get_all(
        "Customer",
        filters={"phone": ["in", variants]},
        pluck="name",
    ):
        matches.add(customer)

    for customer in _find_customers_by_contact_phone(variants):
        matches.add(customer)

    return list(matches)


def _find_customers_by_contact_phone(variants):
    if not variants:
        return []

    return frappe.db.sql(
        """
        select distinct dl.link_name
        from `tabContact` contact
        inner join `tabDynamic Link` dl on dl.parent = contact.name
        where dl.parenttype = 'Contact'
            and dl.link_doctype = 'Customer'
            and (contact.mobile_no in %(variants)s or contact.phone in %(variants)s)
        """,
        {"variants": tuple(variants)},
        pluck=True,
    )


def _get_customer_receivable_account(customer, settings, payment_mapping):
    if payment_mapping.get("receivable_account"):
        return payment_mapping["receivable_account"]

    try:
        from erpnext.accounts.party import get_party_account

        return get_party_account("Customer", customer, settings.company)
    except Exception:
        frappe.throw("Default Receivable Account is required for customer phone matching")


def normalize_phone(value):
    if not value or is_hashed_msisdn(value):
        return None

    digits = "".join(char for char in str(value) if char.isdigit())
    if digits.startswith("0") and len(digits) == 10:
        return f"254{digits[1:]}"
    if digits.startswith("7") and len(digits) == 9:
        return f"254{digits}"
    return digits


def is_hashed_msisdn(value):
    if not value:
        return False

    value = str(value).strip().lower()
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def get_phone_variants(value):
    phone = normalize_phone(value)
    if not phone:
        return []

    variants = {phone}
    if phone.startswith("254") and len(phone) == 12:
        local = f"0{phone[3:]}"
        short = phone[3:]
        variants.update({local, short, f"+{phone}", f"+254 {short[:3]} {short[3:6]} {short[6:]}"})

    return list(variants)


def _complete_existing_payment_entry(transaction, settings):
    payment_entry = frappe.get_doc("Payment Entry", transaction.payment_entry)
    if payment_entry.docstatus == 0 and settings.auto_submit_payment_entry:
        payment_entry.submit()


def _is_submitted_or_draft_allowed(transaction, settings):
    payment_entry = frappe.get_doc("Payment Entry", transaction.payment_entry)
    return payment_entry.docstatus == 1 or not settings.auto_submit_payment_entry


def _record_reconciliation_attempt(transaction, source, status, message, increment=False):
    transaction.last_reconciliation_attempt = now_datetime()
    if increment:
        transaction.reconciliation_attempt_count = (transaction.reconciliation_attempt_count or 0) + 1

    transaction.append(
        "reconciliation_attempts",
        {
            "attempted_on": transaction.last_reconciliation_attempt,
            "source": source,
            "status": status,
            "message": message,
        },
    )
    transaction.save(ignore_permissions=True)


def _safe_record_reconciliation_attempt(transaction, source, status, message, increment=False):
    try:
        _record_reconciliation_attempt(transaction, source, status, message, increment=increment)
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"M-Pesa reconciliation attempt log failed: {transaction.name}",
        )


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
