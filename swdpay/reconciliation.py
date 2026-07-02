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

        if transaction.payment_entry:
            _complete_existing_payment_entry(transaction, settings)
            transaction.status = STATUS_RECONCILED if _is_submitted_or_draft_allowed(transaction, settings) else STATUS_MATCHED
            transaction.save(ignore_permissions=True)
            _safe_record_reconciliation_attempt(transaction, source, "Success", "Existing Payment Entry completed")
            return transaction

        existing_payment_entry = get_existing_payment_entry_for_receipt(transaction.mpesa_receipt_number)
        if existing_payment_entry:
            _link_existing_payment_entry(transaction, settings, existing_payment_entry)
            _safe_record_reconciliation_attempt(
                transaction,
                source,
                "Success",
                f"Existing Payment Entry {existing_payment_entry} linked",
            )
            return transaction

        payment_mapping = get_payment_account_mapping(transaction, settings)
        invoice = _get_valid_sales_invoice(transaction, settings, throw=False)
        customer = invoice.customer if invoice else _get_valid_customer_by_phone(transaction, settings)

        transaction.sales_invoice = invoice.name if invoice else None
        transaction.customer = customer
        transaction.status = STATUS_MATCHED
        transaction.error_message = None
        transaction.save(ignore_permissions=True)

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
        transaction.flags.skip_payment_entry_completion = True
        transaction.save(ignore_permissions=True)
        _safe_record_reconciliation_attempt(transaction, source, "Exception", str(exc))
        frappe.log_error(frappe.get_traceback(), f"M-Pesa reconciliation failed: {transaction.name}")
        if manual:
            raise
        return transaction


def manually_create_payment_entry(
    transaction_name,
    sales_invoice=None,
    sales_order=None,
    customer=None,
    payment_entry=None,
):
    transaction = frappe.get_doc("M-Pesa Transaction", transaction_name)
    settings = frappe.get_single("M-Pesa Settings")
    source = "Manual Payment Entry"

    try:
        _safe_record_reconciliation_attempt(transaction, source, "Started", "Manual Payment Entry creation started")

        if transaction.payment_entry:
            _complete_existing_payment_entry(transaction, settings)
            transaction.status = STATUS_RECONCILED if _is_submitted_or_draft_allowed(transaction, settings) else STATUS_MATCHED
            transaction.save(ignore_permissions=True)
            _safe_record_reconciliation_attempt(transaction, source, "Success", "Existing Payment Entry completed")
            return transaction

        if payment_entry:
            _link_existing_payment_entry(transaction, settings, payment_entry)
            _safe_record_reconciliation_attempt(
                transaction,
                source,
                "Success",
                f"Existing Payment Entry {transaction.payment_entry} linked",
            )
            return transaction

        existing_payment_entry = get_existing_payment_entry_for_receipt(transaction.mpesa_receipt_number)
        if existing_payment_entry:
            _link_existing_payment_entry(transaction, settings, existing_payment_entry)
            _safe_record_reconciliation_attempt(
                transaction,
                source,
                "Success",
                f"Existing Payment Entry {existing_payment_entry} linked",
            )
            return transaction

        payment_mapping = get_payment_account_mapping(transaction, settings)
        invoice = _get_manual_sales_invoice(transaction, settings, sales_invoice)
        order = None if invoice else _get_manual_sales_order(transaction, settings, sales_order)
        resolved_customer = (
            invoice.customer
            if invoice
            else order.customer
            if order
            else _get_manual_customer(customer or transaction.customer)
        )

        transaction.sales_invoice = invoice.name if invoice else None
        transaction.sales_order = order.name if order else None
        transaction.customer = resolved_customer

        payment_entry = create_payment_entry(
            transaction,
            settings,
            payment_mapping,
            invoice=invoice,
            order=order,
            customer=resolved_customer,
        )
        transaction.status = STATUS_RECONCILED
        transaction.error_message = None
        transaction.save(ignore_permissions=True)
        _safe_record_reconciliation_attempt(transaction, source, "Success", f"Payment Entry {payment_entry.name} created")
        return transaction
    except Exception as exc:
        transaction.status = STATUS_EXCEPTION
        transaction.error_message = str(exc)
        transaction.flags.skip_payment_entry_completion = True
        transaction.save(ignore_permissions=True)
        _safe_record_reconciliation_attempt(transaction, source, "Exception", str(exc))
        frappe.log_error(frappe.get_traceback(), f"Manual M-Pesa Payment Entry creation failed: {transaction.name}")
        raise


def _link_existing_payment_entry(transaction, settings, payment_entry_name):
    payment_entry = apply_selected_payment_entry(transaction, payment_entry_name=payment_entry_name)
    transaction.save(ignore_permissions=True)
    _complete_existing_payment_entry(transaction, settings)
    transaction.status = STATUS_RECONCILED if _is_submitted_or_draft_allowed(transaction, settings) else STATUS_MATCHED
    transaction.save(ignore_permissions=True)
    return payment_entry


def apply_selected_payment_entry(transaction, payment_entry_name=None):
    payment_entry_name = payment_entry_name or transaction.payment_entry
    if not payment_entry_name:
        return None

    receipt_payment_entry = get_existing_payment_entry_for_receipt(transaction.mpesa_receipt_number)
    if receipt_payment_entry and receipt_payment_entry != payment_entry_name:
        frappe.throw(
            f"Payment Entry {receipt_payment_entry} already uses M-Pesa receipt number {transaction.mpesa_receipt_number}"
        )

    linked_transaction = frappe.db.exists(
        "M-Pesa Transaction",
        {
            "name": ["!=", transaction.name],
            "payment_entry": payment_entry_name,
        },
    )
    if linked_transaction:
        frappe.throw(f"Payment Entry {payment_entry_name} is already linked to M-Pesa Transaction {linked_transaction}")

    payment_entry = frappe.get_doc("Payment Entry", payment_entry_name)
    if payment_entry.docstatus == 2:
        frappe.throw(f"Payment Entry {payment_entry.name} is cancelled")

    if payment_entry.payment_type != "Receive":
        frappe.throw(f"Payment Entry {payment_entry.name} must be a Receive payment")

    if payment_entry.party_type != "Customer":
        frappe.throw(f"Payment Entry {payment_entry.name} must be for a Customer")

    if flt(payment_entry.paid_amount) != flt(transaction.amount) and flt(payment_entry.received_amount) != flt(transaction.amount):
        frappe.throw(f"Payment Entry {payment_entry.name} amount does not match the M-Pesa transaction amount")

    if payment_entry.reference_no and payment_entry.reference_no != transaction.mpesa_receipt_number:
        frappe.throw(
            f"Payment Entry {payment_entry.name} Reference No must match M-Pesa receipt {transaction.mpesa_receipt_number}"
        )

    transaction.payment_entry = payment_entry.name
    transaction.customer = payment_entry.party
    _set_transaction_references_from_payment_entry(transaction, payment_entry)
    transaction.status = STATUS_RECONCILED if payment_entry.docstatus == 1 else STATUS_MATCHED
    transaction.error_message = None
    return payment_entry


def complete_selected_payment_entry(transaction):
    settings = frappe.get_single("M-Pesa Settings")
    _complete_existing_payment_entry(transaction, settings)
    status = STATUS_RECONCILED if _is_submitted_or_draft_allowed(transaction, settings) else STATUS_MATCHED

    if transaction.status != status:
        frappe.db.set_value(
            "M-Pesa Transaction",
            transaction.name,
            "status",
            status,
            update_modified=False,
        )
        transaction.status = status


def _set_transaction_references_from_payment_entry(transaction, payment_entry):
    transaction.sales_invoice = None
    transaction.sales_order = None

    for reference in payment_entry.references:
        if reference.reference_doctype == "Sales Invoice" and not transaction.sales_invoice:
            transaction.sales_invoice = reference.reference_name
        elif reference.reference_doctype == "Sales Order" and not transaction.sales_order:
            transaction.sales_order = reference.reference_name


def create_payment_entry(transaction, settings, payment_mapping, invoice=None, order=None, customer=None):
    payment_entry = (
        _get_invoice_payment_entry(transaction, settings, payment_mapping, invoice)
        if invoice
        else _get_order_payment_entry(transaction, settings, payment_mapping, order)
        if order
        else _get_customer_payment_entry(transaction, settings, payment_mapping, customer)
    )

    _validate_payment_entry_accounts(payment_entry)
    payment_entry.flags.ignore_permissions = True
    payment_entry.insert(ignore_permissions=True)
    transaction.payment_entry = payment_entry.name
    _set_transaction_write_off_fields(transaction, settings, invoice)
    transaction.flags.skip_payment_entry_completion = True
    transaction.save(ignore_permissions=True)

    if settings.auto_submit_payment_entry:
        payment_entry.submit()

    return payment_entry


def _validate_payment_entry_accounts(payment_entry):
    paid_from_type = (
        frappe.get_cached_value("Account", payment_entry.paid_from, "account_type") if payment_entry.paid_from else None
    )
    if paid_from_type != "Receivable":
        frappe.throw(
            f"Paid From account {payment_entry.paid_from} must be an account of type Receivable. "
            "Check the Default Receivable Account and Payment Account Mappings in M-Pesa Settings."
        )

    paid_to_type = (
        frappe.get_cached_value("Account", payment_entry.paid_to, "account_type") if payment_entry.paid_to else None
    )
    if paid_to_type not in ("Bank", "Cash"):
        frappe.throw(
            f"Paid To account {payment_entry.paid_to} must be an account of type Bank or Cash. "
            "Check the Paid To Account in M-Pesa Settings."
        )


def get_existing_payment_entry_for_receipt(receipt_number):
    if not receipt_number:
        return None

    return frappe.db.exists(
        "Payment Entry",
        {
            "reference_no": receipt_number,
            "docstatus": ["<", 2],
        },
    )


def _get_invoice_payment_entry(transaction, settings, payment_mapping, invoice):
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
    write_off_amount = get_small_difference_write_off_amount(transaction.amount, outstanding, settings)
    allocated_amount = outstanding if write_off_amount else min(flt(transaction.amount), outstanding)
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

    if write_off_amount:
        _apply_small_difference_write_off(payment_entry, settings, write_off_amount)

    return payment_entry


def _get_order_payment_entry(transaction, settings, payment_mapping, order):
    try:
        from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

        payment_entry = get_payment_entry(
            "Sales Order",
            order.name,
            party_amount=flt(transaction.amount),
            bank_amount=flt(transaction.amount),
            payment_type="Receive",
        )
    except Exception:
        payment_entry = _new_payment_entry()

    _apply_common_payment_fields(payment_entry, transaction, payment_mapping, order.company, order.customer)
    payment_entry.paid_from = _get_sales_order_receivable_account(order)

    payment_entry.set("references", [])
    outstanding = _get_sales_order_outstanding_amount(order)
    allocated_amount = min(flt(transaction.amount), outstanding)
    payment_entry.append(
        "references",
        {
            "reference_doctype": "Sales Order",
            "reference_name": order.name,
            "total_amount": _get_sales_order_total_amount(order),
            "outstanding_amount": outstanding,
            "allocated_amount": allocated_amount,
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

    if flt(transaction.amount) < flt(invoice.outstanding_amount) and not _is_small_difference_write_off_allowed(
        transaction.amount,
        invoice.outstanding_amount,
        settings,
    ) and not settings.allow_partial_payments:
        frappe.throw("Partial payments are disabled")

    if flt(transaction.amount) > flt(invoice.outstanding_amount) and not settings.allow_overpayments:
        frappe.throw("Payment amount exceeds outstanding amount")

    return invoice


def _get_manual_sales_invoice(transaction, settings, sales_invoice=None):
    sales_invoice = sales_invoice or transaction.sales_invoice
    if not sales_invoice:
        return None

    invoice_name = frappe.db.exists("Sales Invoice", sales_invoice)
    if not invoice_name:
        frappe.throw(f"Sales Invoice {sales_invoice} was not found")

    invoice = frappe.get_doc("Sales Invoice", invoice_name)

    if invoice.docstatus != 1:
        frappe.throw(f"Sales Invoice {invoice.name} is not submitted")

    if invoice.company != settings.company:
        frappe.throw(f"Sales Invoice {invoice.name} belongs to company {invoice.company}, not {settings.company}")

    if flt(invoice.outstanding_amount) <= 0:
        frappe.throw(f"Sales Invoice {invoice.name} is already fully paid")

    if flt(transaction.amount) < flt(invoice.outstanding_amount) and not _is_small_difference_write_off_allowed(
        transaction.amount,
        invoice.outstanding_amount,
        settings,
    ) and not settings.allow_partial_payments:
        frappe.throw("Partial payments are disabled")

    if flt(transaction.amount) > flt(invoice.outstanding_amount) and not settings.allow_overpayments:
        frappe.throw("Payment amount exceeds outstanding amount")

    return invoice


def _get_manual_sales_order(transaction, settings, sales_order=None):
    sales_order = sales_order or transaction.get("sales_order")
    if not sales_order:
        return None

    order_name = frappe.db.exists("Sales Order", sales_order)
    if not order_name:
        frappe.throw(f"Sales Order {sales_order} was not found")

    order = frappe.get_doc("Sales Order", order_name)

    if order.docstatus != 1:
        frappe.throw(f"Sales Order {order.name} is not submitted")

    if order.company != settings.company:
        frappe.throw(f"Sales Order {order.name} belongs to company {order.company}, not {settings.company}")

    if flt(order.per_billed) >= 100:
        frappe.throw(f"Sales Order {order.name} is already fully billed")

    if flt(transaction.amount) <= 0:
        frappe.throw("Amount is zero")

    outstanding = _get_sales_order_outstanding_amount(order)
    if outstanding <= 0:
        frappe.throw(f"Sales Order {order.name} is already fully paid")

    if flt(transaction.amount) > outstanding and not settings.allow_overpayments:
        frappe.throw("Payment amount exceeds outstanding amount")

    return order


def _get_sales_order_total_amount(order):
    return flt(order.get("base_rounded_total") or order.get("base_grand_total") or order.get("rounded_total") or order.get("grand_total"))


def _get_sales_order_outstanding_amount(order):
    return flt(order.get("outstanding_amount") or (_get_sales_order_total_amount(order) - flt(order.get("advance_paid"))))


def _get_manual_customer(customer):
    if not customer:
        frappe.throw("Select a Sales Invoice, Sales Order, or Customer before creating a Payment Entry")

    if not frappe.db.exists("Customer", customer):
        frappe.throw(f"Customer {customer} was not found")

    return customer


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


def _get_sales_order_receivable_account(order):
    try:
        from erpnext.accounts.party import get_party_account

        return get_party_account("Customer", order.customer, order.company)
    except Exception:
        frappe.throw("Customer Receivable Account is required for Sales Order payments")


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
        payment_entry.flags.ignore_permissions = True
        payment_entry.submit()


def _is_submitted_or_draft_allowed(transaction, settings):
    payment_entry = frappe.get_doc("Payment Entry", transaction.payment_entry)
    return payment_entry.docstatus == 1 or not settings.auto_submit_payment_entry


def get_small_difference_write_off_amount(payment_amount, outstanding_amount, settings):
    difference = flt(outstanding_amount) - flt(payment_amount)
    if difference <= 0 or not settings.enable_small_difference_write_off:
        return 0

    if difference > flt(settings.max_write_off_difference):
        return 0

    return difference


def _is_small_difference_write_off_allowed(payment_amount, outstanding_amount, settings):
    return bool(get_small_difference_write_off_amount(payment_amount, outstanding_amount, settings))


def get_write_off_account(settings):
    return settings.write_off_account or frappe.get_cached_value("Company", settings.company, "write_off_account")


def get_write_off_cost_center(settings):
    return settings.write_off_cost_center or frappe.get_cached_value("Company", settings.company, "cost_center")


def _apply_small_difference_write_off(payment_entry, settings, write_off_amount):
    write_off_account = get_write_off_account(settings)
    write_off_cost_center = get_write_off_cost_center(settings)

    if not write_off_account:
        frappe.throw(
            "Write Off Account is required for small difference write off. "
            "Set it in M-Pesa Settings or as the Company default Write Off Account."
        )

    if not write_off_cost_center:
        frappe.throw(
            "Write Off Cost Center is required for small difference write off. "
            "Set it in M-Pesa Settings or as the Company default Cost Center."
        )

    payment_entry.append(
        "deductions",
        {
            "account": write_off_account,
            "cost_center": write_off_cost_center,
            "amount": write_off_amount,
        },
    )


def _set_transaction_write_off_fields(transaction, settings, invoice=None):
    transaction.write_off_amount = 0
    transaction.write_off_account = None
    transaction.write_off_cost_center = None

    if not invoice:
        return

    write_off_amount = get_small_difference_write_off_amount(transaction.amount, invoice.outstanding_amount, settings)
    if not write_off_amount:
        return

    transaction.write_off_amount = write_off_amount
    transaction.write_off_account = get_write_off_account(settings)
    transaction.write_off_cost_center = get_write_off_cost_center(settings)


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
