import frappe
from urllib.parse import parse_qs, urlparse

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


def _callback_url(callback_base, callback_token, method):
    return f"{callback_base}/api/method/swdpay.api.callbacks.{method}?token={callback_token}"


def build_payout_request(request, *, initiator_name, security_credential, shortcode,
                         b2c_shortcode, callback_base, callback_token):
    remarks = (request.description or "Field purchase")[:100]
    common = {
        "OriginatorConversationID": request.name,
        "SecurityCredential": security_credential,
        "Amount": int(request.amount),
        "Remarks": remarks,
        "ResultURL": _callback_url(callback_base, callback_token, "payout_result"),
        "QueueTimeOutURL": _callback_url(callback_base, callback_token, "payout_timeout"),
    }

    if request.recipient_type == "Phone":
        body = {
            **common,
            "InitiatorName": initiator_name,
            "CommandID": "BusinessPayment",
            "PartyA": b2c_shortcode,
            "PartyB": request.phone_number,
            "Occasion": (request.project or "")[:100] if getattr(request, "project", None) else "",
        }
        return {"path": "/mpesa/b2c/v3/paymentrequest", "body": body}

    body = {
        **common,
        "Initiator": initiator_name,
        "CommandID": "BusinessBuyGoods" if request.recipient_type == "Buy Goods Till" else "BusinessPayBill",
        "SenderIdentifierType": "4",
        "RecieverIdentifierType": "4",
        "PartyA": shortcode,
        "PartyB": request.till_number if request.recipient_type == "Buy Goods Till" else request.paybill_number,
    }
    if request.recipient_type == "Paybill":
        body["AccountReference"] = request.account_reference
    return {"path": "/mpesa/b2b/v1/paymentrequest", "body": body}


def parse_payout_result(payload):
    result = (payload or {}).get("Result") or {}
    params = {}
    for row in ((result.get("ResultParameters") or {}).get("ResultParameter") or []):
        if isinstance(row, dict) and row.get("Key"):
            params[row["Key"]] = row.get("Value")

    receiver = params.get("ReceiverPartyPublicName") or params.get("CreditPartyPublicName")
    code = result.get("ResultCode")
    return {
        "originator_conversation_id": result.get("OriginatorConversationID"),
        "conversation_id": result.get("ConversationID"),
        "result_code": int(code) if code is not None else None,
        "result_desc": result.get("ResultDesc"),
        "transaction_id": result.get("TransactionID") or params.get("TransactionReceipt"),
        "receiver_public_name": receiver,
    }


def parse_etims_invoice_number(url):
    if not url:
        return None
    try:
        query = parse_qs(urlparse(url).query)
    except ValueError:
        return None
    values = query.get("invoiceNo") or query.get("invoiceno") or []
    return values[0] if values else None


def plan_approval(status, *, enabled, has_role):
    if not enabled:
        return "Outbound payments are disabled in M-Pesa Settings"
    if not has_role:
        return "You do not have permission to approve payment requests"
    if "Approved" not in ALLOWED_TRANSITIONS.get(status, set()):
        return f"Cannot move payment request from {status} to Approved"
    return None


@frappe.whitelist()
def approve_and_send(name):
    settings = frappe.get_single("M-Pesa Settings")
    doc = frappe.get_doc("M-Pesa Payment Request", name)
    error = plan_approval(
        doc.status,
        enabled=bool(settings.enable_outbound_payments),
        has_role="M-Pesa Payment Approver" in frappe.get_roles(),
    )
    if error:
        frappe.throw(error)
    validate_payment_request(doc, max_amount=settings.max_request_amount)

    doc.db_set("status", "Approved")
    frappe.enqueue(
        "swdpay.outbound.send_payment_request",
        queue="short",
        job_id=f"swdpay-payout-{name}",  # dedup: a double-tap enqueues once
        deduplicate=True,  # job_id alone does not dedupe in frappe; this makes the RQ id enforce it
        enqueue_after_commit=True,  # don't let the worker start before the "Approved" commit lands
        request_name=name,
    )
    return {"status": "Approved"}


@frappe.whitelist()
def reject_request(name, reason):
    if "M-Pesa Payment Approver" not in frappe.get_roles():
        frappe.throw("You do not have permission to reject payment requests")
    if not (reason or "").strip():
        frappe.throw("A rejection reason is required")
    doc = frappe.get_doc("M-Pesa Payment Request", name)
    assert_transition(doc.status, "Rejected")
    doc.db_set({"status": "Rejected", "rejection_reason": reason.strip()})


def send_payment_request(request_name):
    from swdpay.daraja import daraja_post, encrypt_security_credential

    doc = frappe.get_doc("M-Pesa Payment Request", request_name)
    # Row-lock the claim: a concurrent worker blocks here, then sees "Sending" and bails.
    locked_status = frappe.db.get_value("M-Pesa Payment Request", request_name, "status", for_update=True)
    if locked_status != "Approved":
        return  # idempotent: stale/duplicate job

    settings = frappe.get_single("M-Pesa Settings")
    try:
        credential = encrypt_security_credential(
            settings.get_password("initiator_password"), settings.security_certificate
        )
        payout = build_payout_request(
            doc,
            initiator_name=settings.initiator_name,
            security_credential=credential,
            shortcode=settings.paybill_number,
            b2c_shortcode=settings.b2c_shortcode or settings.paybill_number,
            callback_base=(settings.callback_base_url or "").rstrip("/"),
            callback_token=settings.get_password("callback_secret"),
        )
    except Exception:
        # Pre-dispatch failure: nothing sent, safe to mark Failed for re-approval.
        doc.db_set({"status": "Failed", "error_message": "Could not prepare the payment request"})
        frappe.log_error(title=f"swdpay payout prepare failed: {request_name}")
        frappe.db.commit()
        raise

    doc.db_set("status", "Sending")
    frappe.db.commit()  # persist Sending BEFORE dispatch: after this point only callbacks/watchdog resolve
    try:
        response = daraja_post(settings, payout["path"], payout["body"])
    except Exception:
        # Ambiguous: the request may or may not have reached Safaricom. Leave in
        # Sending — the Task 12 watchdog resolves it via Transaction Status.
        frappe.log_error(title=f"swdpay payout dispatch error: {request_name}")
        raise
    doc.db_set("conversation_id", response.get("ConversationID"))


def recipient_identifier(request):
    if request.recipient_type == "Buy Goods Till":
        return request.till_number
    if request.recipient_type == "Paybill":
        return request.paybill_number
    return request.phone_number


def plan_party_resolution(request, *, supplier_by_identifier, employee_matches, supplier_matches):
    if supplier_by_identifier:
        return {"party_type": "Supplier", "party": supplier_by_identifier, "create_supplier": False}
    if request.recipient_type == "Phone" and len(employee_matches) == 1:
        return {"party_type": "Employee", "party": employee_matches[0], "create_supplier": False}
    if len(supplier_matches) == 1:
        return {"party_type": "Supplier", "party": supplier_matches[0], "create_supplier": False}
    return {"party_type": "Supplier", "party": request.payee_name, "create_supplier": True}


def resolve_or_create_party(doc):
    """Resolve doc → (party_type, party). Manual party set by ops wins outright."""
    from swdpay.reconciliation import get_phone_variants

    if doc.party_type and doc.party:
        return doc.party_type, doc.party

    identifier = recipient_identifier(doc)
    supplier_by_identifier = frappe.db.get_value("Supplier", {"mpesa_identifier": identifier}, "name")

    employee_matches, supplier_matches = [], []
    if doc.recipient_type == "Phone":
        variants = get_phone_variants(doc.phone_number)
        employee_matches = [
            e.name for e in frappe.get_all("Employee", filters={"cell_number": ["in", variants], "status": "Active"})
        ]
        supplier_matches = [s.name for s in frappe.get_all("Supplier", filters={"mobile_no": ["in", variants]})]

    plan = plan_party_resolution(
        doc, supplier_by_identifier=supplier_by_identifier,
        employee_matches=employee_matches, supplier_matches=supplier_matches,
    )

    if plan["create_supplier"]:
        supplier_name = frappe.db.get_value("Supplier", {"supplier_name": plan["party"]}, "name")
        if not supplier_name:
            supplier = frappe.get_doc({
                "doctype": "Supplier", "supplier_name": plan["party"],
                "supplier_group": frappe.db.get_single_value("Buying Settings", "supplier_group") or "All Supplier Groups",
                "mpesa_identifier": identifier,
            }).insert(ignore_permissions=True)
            supplier_name = supplier.name
        frappe.db.set_value("Supplier", supplier_name, "mpesa_identifier", identifier)
        return "Supplier", supplier_name

    if plan["party_type"] == "Supplier":
        frappe.db.set_value("Supplier", plan["party"], "mpesa_identifier", identifier)
    return plan["party_type"], plan["party"]


def _validate_payout_accounts(paid_from, paid_to, company):
    paid_from_type, paid_from_currency = frappe.db.get_value("Account", paid_from, ["account_type", "account_currency"])
    company_currency = frappe.get_cached_value("Company", company, "default_currency")
    if paid_from_type not in ("Bank", "Cash"):
        frappe.throw(f"Outbound Paid From Account {paid_from} must be a Bank or Cash account")
    paid_to_currency = frappe.db.get_value("Account", paid_to, "account_currency")
    for label, currency in (("Paid From", paid_from_currency), ("Paid To", paid_to_currency)):
        if currency and currency != company_currency:
            frappe.throw(f"{label} account currency {currency} does not match company currency {company_currency}")


def create_payout_payment_entry(doc, settings):
    if doc.payment_entry:
        return doc.payment_entry  # idempotent: a retry must not double-book

    from erpnext.accounts.party import get_party_account
    from frappe.utils import now_datetime, getdate

    party_type, party = resolve_or_create_party(doc)
    paid_to = get_party_account(party_type, party, settings.company)

    _validate_payout_accounts(settings.outbound_paid_from_account, paid_to, settings.company)

    payment_entry = frappe.new_doc("Payment Entry")
    payment_entry.update({
        "payment_type": "Pay",
        "company": settings.company,
        "posting_date": getdate(now_datetime()),
        "mode_of_payment": settings.outbound_mode_of_payment,
        "party_type": party_type,
        "party": party,
        "paid_from": settings.outbound_paid_from_account,
        "paid_to": paid_to,
        "paid_amount": doc.amount,
        "received_amount": doc.amount,
        "reference_no": doc.mpesa_transaction_id,
        "reference_date": getdate(now_datetime()),
        "custom_remarks": 1,
        "remarks": f"M-Pesa payout {doc.name}: {doc.payee_name}",
        "project": doc.project or None,
    })
    payment_entry.insert(ignore_permissions=True)
    payment_entry.submit()

    doc.db_set({"party_type": party_type, "party": party, "payment_entry": payment_entry.name})
    return payment_entry.name
