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
    # Neither Daraja v1 API (B2C or B2B) documents OriginatorConversationID as a
    # request field — Daraja generates its own and returns it in the synchronous
    # response. Correlation is captured from the response, not supplied here.
    common = {
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
        return {"path": "/mpesa/b2c/v1/paymentrequest", "body": body}

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


def normalize_result_code(code):
    """Daraja result codes are usually numeric but not always — B2B failures can
    carry string codes like "SFC_IC0003" ("Receiver party is invalid"). int() on
    those crashed the callback and left the request stuck in Sending; keep
    non-numeric codes verbatim (payout_outcome only cares about == 0)."""
    if code is None:
        return None
    text = str(code).strip()
    if not text:
        return None
    return int(text) if text.lstrip("-").isdigit() else text


def parse_payout_result(payload):
    result = (payload or {}).get("Result") or {}
    params = {}
    for row in ((result.get("ResultParameters") or {}).get("ResultParameter") or []):
        if isinstance(row, dict) and row.get("Key"):
            params[row["Key"]] = row.get("Value")

    receiver = params.get("ReceiverPartyPublicName") or params.get("CreditPartyPublicName")
    return {
        "originator_conversation_id": result.get("OriginatorConversationID"),
        "conversation_id": result.get("ConversationID"),
        "result_code": normalize_result_code(result.get("ResultCode")),
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


def plan_auto_approval(amount, *, enabled, auto_limit, daily_limit, todays_auto_total):
    """Pure decision: may this request skip the human approver?

    Auto-send needs the feature on, a configured per-request limit the amount
    stays within, and the requester's day total (auto-approved only) to remain
    inside the per-employee cap. Anything else → the normal approval flow.
    """
    if not enabled:
        return False
    if not auto_limit or amount > auto_limit:
        return False
    if daily_limit and (todays_auto_total + amount) > daily_limit:
        return False
    return True


def todays_auto_approved_total(requested_by, exclude=None):
    """Sum of the requester's auto-approved amounts today (excluding failures)."""
    from frappe.utils import nowdate

    filters = {
        "requested_by": requested_by,
        "auto_approved": 1,
        "status": ["not in", ["Rejected", "Failed"]],
        "creation": ["between", [f"{nowdate()} 00:00:00", f"{nowdate()} 23:59:59"]],
    }
    if exclude:
        filters["name"] = ["!=", exclude]
    rows = frappe.get_all("M-Pesa Payment Request", filters=filters, fields=["amount"])
    return sum((r.amount or 0) for r in rows)


def maybe_auto_approve(doc):
    """Auto-approve + dispatch a just-created request when it clears the limits.

    Called from after_insert. Best-effort: any error leaves the request in the
    normal Requested flow — creation must never fail because auto-approval did.
    """
    try:
        settings = frappe.get_single("M-Pesa Settings")
        if not settings.enable_outbound_payments:
            return False
        ok = plan_auto_approval(
            doc.amount or 0,
            enabled=bool(getattr(settings, "enable_auto_approval", 0)),
            auto_limit=getattr(settings, "auto_approve_limit", 0) or 0,
            daily_limit=getattr(settings, "daily_auto_limit_per_employee", 0) or 0,
            todays_auto_total=todays_auto_approved_total(doc.requested_by, exclude=doc.name),
        )
        if not ok:
            return False
        validate_payment_request(doc, max_amount=settings.max_request_amount)
        doc.db_set({"status": "Approved", "auto_approved": 1})
        frappe.enqueue(
            "swdpay.outbound.send_payment_request",
            queue="short",
            job_id=f"swdpay-payout-{doc.name}",
            deduplicate=True,
            enqueue_after_commit=True,
            request_name=doc.name,
        )
        return True
    except Exception:
        frappe.log_error(title=f"swdpay auto-approval failed: {doc.name}")
        return False


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
    # Row-lock the claim: don't decide off a possibly-stale in-memory doc.status.
    locked_status = frappe.db.get_value("M-Pesa Payment Request", name, "status", for_update=True)
    error = plan_approval(
        locked_status,
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
    # Row-lock the claim: don't decide off a possibly-stale in-memory doc.status.
    locked_status = frappe.db.get_value("M-Pesa Payment Request", name, "status", for_update=True)
    assert_transition(locked_status, "Rejected")
    doc.db_set({"status": "Rejected", "rejection_reason": reason.strip()})


def send_payment_request(request_name):
    from swdpay.daraja import daraja_post, encrypt_security_credential

    doc = frappe.get_doc("M-Pesa Payment Request", request_name)
    # Row-lock the claim: a concurrent worker blocks here, then sees "Sending" and bails.
    locked_status = frappe.db.get_value("M-Pesa Payment Request", request_name, "status", for_update=True)
    if locked_status != "Approved":
        return  # idempotent: stale/duplicate job

    settings = frappe.get_single("M-Pesa Settings")

    # An auto-approved request re-proves its limits at dispatch (the settings, or
    # the day's total, may have moved since approval). Failing the re-check is not
    # an error — it just falls back to the human approval flow.
    if getattr(doc, "auto_approved", 0) and not plan_auto_approval(
        doc.amount or 0,
        enabled=bool(getattr(settings, "enable_auto_approval", 0)),
        auto_limit=getattr(settings, "auto_approve_limit", 0) or 0,
        daily_limit=getattr(settings, "daily_auto_limit_per_employee", 0) or 0,
        todays_auto_total=todays_auto_approved_total(doc.requested_by, exclude=doc.name),
    ):
        doc.db_set({"status": "Requested", "auto_approved": 0})
        return

    try:
        # Re-check the amount cap at dispatch time too: settings (or the request,
        # via some other path) may have changed between approval and now.
        validate_payment_request(doc, max_amount=settings.max_request_amount)
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
        # Pre-dispatch failure: nothing sent, safe to mark Failed — nothing was dispatched.
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

    # A gate rejection here is unambiguous — Daraja never dispatched anything
    # downstream, so Failed is safe (unlike a transport-level exception above).
    response_code = str(response.get("ResponseCode", "0")).strip()
    if response_code != "0":
        doc.db_set({
            "status": "Failed",
            "error_message": f"Daraja rejected the request (ResponseCode {response_code}): {response.get('ResponseDescription') or ''}".strip(),
        })
        return
    # v1 doesn't accept a caller-supplied OriginatorConversationID — Daraja mints its
    # own and returns both ids here. `or doc.name` keeps v3-style/edge responses (or a
    # response missing the field) falling back to the pre-dispatch default already set
    # by after_insert, which is what callbacks/watchdog correlate against pre-dispatch.
    doc.db_set({
        "conversation_id": response.get("ConversationID"),
        "originator_conversation_id": response.get("OriginatorConversationID") or doc.name,
    })
    # Close the race: Safaricom's callback can arrive before this job's own
    # transaction would otherwise commit (at job completion) — commit explicitly so
    # the callback handler's lookup by the new originator_conversation_id can see it.
    frappe.db.commit()


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


PLACEHOLDER_ITEM = "FIELD-PURCHASE"


def ensure_placeholder_item():
    """A generic non-stock service item that draft Purchase Invoices start with.

    Ops replaces the line with real items before submitting — the placeholder
    just lets the PI exist (and carry the amount) the moment money moves.
    """
    if frappe.db.exists("Item", PLACEHOLDER_ITEM):
        return PLACEHOLDER_ITEM
    frappe.get_doc({
        "doctype": "Item",
        "item_code": PLACEHOLDER_ITEM,
        "item_name": "Field Purchase (placeholder)",
        "item_group": (
            "All Item Groups"
            if frappe.db.exists("Item Group", "All Item Groups")
            else frappe.get_all("Item Group", filters={"is_group": 0}, limit=1)[0].name
        ),
        "stock_uom": "Nos",
        "is_stock_item": 0,
        "is_purchase_item": 1,
        "is_sales_item": 0,
        "description": "Placeholder line for field M-Pesa purchases — replace with the real items before submitting.",
    }).insert(ignore_permissions=True)
    return PLACEHOLDER_ITEM


def create_payout_purchase_invoice(doc, settings):
    """Draft PI mirroring a Paid payout (2026-07-30 meeting): a worklist marker
    for ops carrying the amount, the receipt attachments, and links both ways.

    Only for Supplier parties — an Employee payout books against the employee
    (a reimbursement), where a supplier invoice would double-count the expense.
    Idempotent via the purchase_invoice link. Never submits anything.
    """
    if doc.purchase_invoice:
        return doc.purchase_invoice
    if doc.party_type != "Supplier" or not doc.party:
        return None

    from frappe.utils import getdate, now_datetime

    ensure_placeholder_item()
    pi = frappe.new_doc("Purchase Invoice")
    pi.update({
        "supplier": doc.party,
        "company": settings.company,
        "posting_date": getdate(now_datetime()),
        "bill_no": doc.mpesa_transaction_id or None,
        "bill_date": getdate(now_datetime()),
        "project": doc.project or None,
        "remarks": f"M-Pesa payout {doc.name} (Payment Entry {doc.payment_entry or '—'}): {doc.description or doc.payee_name}",
        "items": [{
            "item_code": PLACEHOLDER_ITEM,
            "qty": 1,
            "rate": doc.amount,
            "description": (doc.description or "Field purchase") + f" — M-Pesa {doc.mpesa_transaction_id or doc.name}",
            "project": doc.project or None,
        }],
    })
    pi.insert(ignore_permissions=True)
    doc.db_set("purchase_invoice", pi.name)
    return pi.name


def payout_outcome(parsed):
    return "Paid" if parsed.get("result_code") == 0 else "Failed"


def find_request_by_originator(originator):
    """Resolve a callback's originator id to a doc name.

    Under Daraja v1, OriginatorConversationID is minted by Daraja (not supplied by
    us), so the parsed originator may be either the doc name (pre-dispatch default,
    or a v3-style/back-compat response) or a Daraja-generated id captured from the
    dispatch response and stored on `originator_conversation_id`.
    """
    if not originator:
        return None
    if frappe.db.exists("M-Pesa Payment Request", originator):
        return originator  # doc-name correlation (pre-dispatch/back-compat)
    return frappe.db.get_value("M-Pesa Payment Request", {"originator_conversation_id": originator}, "name")


def handle_payout_callback(payload, timeout=False):
    """Raw-first: persist the payload on the request, then queue reconciliation."""
    import json

    parsed = parse_payout_result(payload)
    name = find_request_by_originator(parsed.get("originator_conversation_id"))
    if not name:
        frappe.log_error(
            title="swdpay payout callback for unknown request",
            message=json.dumps(payload, default=str),
        )
        return

    frappe.db.set_value("M-Pesa Payment Request", name, "raw_result", json.dumps(payload, default=str))
    frappe.enqueue(
        "swdpay.outbound.reconcile_payout", queue="short",
        # Separate ids per callback kind: a queued TIMEOUT job must not dedup-swallow
        # the later RESULT enqueue (and vice versa) for the same request.
        job_id=f"swdpay-payout-reconcile-{name}-{'timeout' if timeout else 'result'}",
        deduplicate=True,  # job_id alone does not dedupe in frappe; this makes the RQ id enforce it
        enqueue_after_commit=True,  # don't let the worker start before the raw_result commit lands
        request_name=name, parsed=parsed, timeout=timeout,
    )


def reconcile_payout(request_name, parsed, timeout=False, verify_conversation=True):
    # Row-lock + re-read the guarded fields: a stale in-memory doc could otherwise
    # let two concurrent callbacks both see "Sending" and both create a Payment Entry.
    locked = frappe.db.get_value(
        "M-Pesa Payment Request", request_name, ["status", "payment_entry", "conversation_id"], for_update=True
    )
    if not locked:
        return
    locked_status, locked_pe, locked_conversation = locked
    if locked_status != "Sending":
        return  # terminal or not-yet-dispatched: idempotent no-op

    if (
        verify_conversation
        and locked_conversation
        and parsed.get("conversation_id")
        and parsed["conversation_id"] != locked_conversation
    ):
        # Guards against a spoofed/guessed OriginatorConversationID: the callback must
        # carry the ConversationID Daraja returned for THIS request's dispatch.
        frappe.log_error(
            title=f"swdpay payout callback ConversationID mismatch: {request_name}",
            message=f"stored={locked_conversation} callback={parsed.get('conversation_id')}",
        )
        return

    doc = frappe.get_doc("M-Pesa Payment Request", request_name)
    doc.payment_entry = locked_pe  # trust the locked read, not a possibly-stale cache

    if timeout:
        # Queue timeout ≠ failure; leave for the watchdog to resolve authoritatively.
        doc.db_set("error_message", "Daraja queue timeout — awaiting status check")
        return

    outcome = payout_outcome(parsed)
    code = parsed.get("result_code")
    updates = {
        "status": outcome,
        "conversation_id": parsed.get("conversation_id") or locked_conversation,
        "result_code": str(code) if code is not None else None,
        "result_desc": parsed.get("result_desc"),
    }
    if outcome == "Paid":
        updates.update({
            "mpesa_transaction_id": parsed.get("transaction_id"),
            "receiver_public_name": parsed.get("receiver_public_name"),
            "receipt_pending": 1,
            "error_message": None,  # clear a stale "queue timeout" annotation
        })
    else:
        updates["error_message"] = parsed.get("result_desc") or "Payment failed"
    doc.db_set(updates)

    if outcome == "Paid":
        settings = frappe.get_single("M-Pesa Settings")
        try:
            create_payout_payment_entry(doc, settings)
        except Exception:
            # Money moved; accounting failed. Keep Paid, surface the error for ops.
            doc.db_set("error_message", "Paid, but Payment Entry creation failed — see Error Log")
            frappe.log_error(title=f"swdpay payout PE failed: {request_name}")
        try:
            create_payout_purchase_invoice(doc, settings)
        except Exception:
            # The PI is an ops convenience — its failure must not disturb the
            # Paid status or the Payment Entry that already booked the money.
            frappe.log_error(title=f"swdpay payout PI failed: {request_name}")


STUCK_AFTER_MINUTES = 5
STUCK_AGE_CAP_HOURS = 24
STUCK_AGE_CAP_MESSAGE = "Unresolved after 24h — manual review required"


def resolve_stuck_payouts():
    from frappe.utils import add_to_date, now_datetime
    from swdpay.daraja import daraja_post, encrypt_security_credential

    settings = frappe.get_single("M-Pesa Settings")
    if not settings.enable_outbound_payments:
        return

    cutoff = add_to_date(now_datetime(), minutes=-STUCK_AFTER_MINUTES)

    # Rescue: an Approved doc past the same cutoff means its send_payment_request
    # RQ job was lost (worker crash, queue drop, etc.) before it ever reached
    # Sending. Re-enqueuing is safe — send's own locked-status claim (only
    # Approved -> Sending) makes a duplicate/late job an idempotent no-op.
    stuck_approved = frappe.get_all(
        "M-Pesa Payment Request",
        filters={"status": "Approved", "modified": ["<", cutoff]},
        pluck="name",
    )
    for name in stuck_approved:
        frappe.enqueue(
            "swdpay.outbound.send_payment_request",
            queue="short",
            job_id=f"swdpay-payout-{name}",  # dedup: matches approve_and_send's job_id
            deduplicate=True,
            enqueue_after_commit=True,
            request_name=name,
        )

    stuck = frappe.get_all(
        "M-Pesa Payment Request",
        filters={"status": "Sending", "modified": ["<", cutoff]},
        fields=["name", "modified", "error_message", "originator_conversation_id"],
    )
    if not stuck:
        return

    # Age cap: past 24h a Sending doc is treated as needing manual review instead
    # of being queried forever — flag it once (idempotent) and stop asking Daraja.
    age_cap_cutoff = add_to_date(now_datetime(), hours=-STUCK_AGE_CAP_HOURS)
    queryable = []
    for row in stuck:
        if row["modified"] < age_cap_cutoff:
            if row.get("error_message") != STUCK_AGE_CAP_MESSAGE:
                # update_modified=False: this stamp must not reset `modified`, or the
                # doc falls back out of the >24h window and re-enters the query loop
                # forever — defeating the age cap it's meant to enforce.
                frappe.db.set_value(
                    "M-Pesa Payment Request", row["name"], "error_message", STUCK_AGE_CAP_MESSAGE,
                    update_modified=False,
                )
            continue
        queryable.append(row)

    if not queryable:
        return

    credential = encrypt_security_credential(
        settings.get_password("initiator_password"), settings.security_certificate
    )
    callback_base = (settings.callback_base_url or "").rstrip("/")
    token = settings.get_password("callback_secret")
    for row in queryable:
        name = row["name"]
        try:
            daraja_post(settings, "/mpesa/transactionstatus/v1/query", {
                "Initiator": settings.initiator_name,
                "SecurityCredential": credential,
                "CommandID": "TransactionStatusQuery",
                "TransactionID": "",
                # The doc's STORED originator id — after dispatch this is Daraja's own
                # id (captured from the payout response), not the doc name.
                "OriginalConversationID": row.get("originator_conversation_id") or name,
                "PartyA": settings.paybill_number,
                "IdentifierType": "4",
                # NOTE: deliberately NOT payout_result — a status query's top-level
                # ConversationID/OriginatorConversationID belong to the QUERY, not the
                # original payment, so handle_payout_callback would drop it as an
                # unknown originator (or reject it on the ConversationID check).
                # status_result carries its own tolerant parser (parse_status_result)
                # that reads OriginalConversationID/Occasion instead.
                "ResultURL": _callback_url(callback_base, token, "status_result"),
                "QueueTimeOutURL": _callback_url(callback_base, token, "status_result"),
                "Remarks": "Stuck payout status check",
                "Occasion": name,
            })
        except Exception:
            frappe.log_error(title=f"swdpay watchdog query failed: {name}")


def parse_balance_result(payload):
    """Extract the AccountBalance parameter from a Daraja AccountBalance result.

    Tolerant of a missing/empty Result or ResultParameters — an inconclusive or
    malformed callback should still return a usable (mostly-None) dict rather
    than raising.
    """
    result = (payload or {}).get("Result") or {}
    params = {}
    for row in ((result.get("ResultParameters") or {}).get("ResultParameter") or []):
        if isinstance(row, dict) and row.get("Key"):
            params[row["Key"]] = row.get("Value")

    return {
        "result_code": normalize_result_code(result.get("ResultCode")),
        "result_desc": result.get("ResultDesc"),
        "account_balance": params.get("AccountBalance"),
    }


@frappe.whitelist()
def test_initiator_balance():
    """Fire a read-only AccountBalance query to validate the configured initiator.

    Proves consumer key/secret (OAuth), initiator name, and security-credential
    encryption against the live Daraja environment without moving any money.
    The synchronous response is returned to the caller; the full result arrives
    at the balance_result callback (requires a publicly reachable site).
    """
    from swdpay.daraja import daraja_post, encrypt_security_credential

    if "System Manager" not in frappe.get_roles() and "M-Pesa Payment Approver" not in frappe.get_roles():
        frappe.throw("You do not have permission to test the initiator")

    settings = frappe.get_single("M-Pesa Settings")
    if not settings.enable_outbound_payments:
        frappe.throw("Enable outbound payments in M-Pesa Settings first")

    credential = encrypt_security_credential(
        settings.get_password("initiator_password"), settings.security_certificate
    )
    callback_base = (settings.callback_base_url or "").rstrip("/")
    token = settings.get_password("callback_secret")
    response = daraja_post(settings, "/mpesa/accountbalance/v1/query", {
        "Initiator": settings.initiator_name,
        "SecurityCredential": credential,
        "CommandID": "AccountBalance",
        "PartyA": settings.paybill_number,
        "IdentifierType": "4",
        "Remarks": "swdpay initiator test",
        "ResultURL": _callback_url(callback_base, token, "balance_result"),
        "QueueTimeOutURL": _callback_url(callback_base, token, "balance_result"),
    })
    return response


def parse_status_result(payload):
    """Map a Daraja Transaction Status result onto the payout-result shape.

    Key names vary across Daraja products; parse tolerantly and prefer a no-op
    (doc stays Sending, watchdog retries next cron) over a wrong resolution.
    """
    result = (payload or {}).get("Result") or {}
    params = {}
    for row in ((result.get("ResultParameters") or {}).get("ResultParameter") or []):
        if isinstance(row, dict) and row.get("Key"):
            params[row["Key"]] = row.get("Value")

    # A "not found" result (code 2033) carries no ResultParameters at all — the
    # echoed Occasion lives under Result.ReferenceData.ReferenceItem instead
    # (a single dict or a list, and sometimes Safaricom's "Occassion" spelling).
    reference = {}
    items = (result.get("ReferenceData") or {}).get("ReferenceItem") or []
    if isinstance(items, dict):
        items = [items]
    for row in items:
        if isinstance(row, dict) and row.get("Key"):
            reference[row["Key"]] = row.get("Value")

    originator = (
        params.get("OriginatorConversationID")
        or params.get("OriginalConversationID")
        or params.get("Occasion")
        or result.get("Occasion")
        or reference.get("Occasion")
        or reference.get("Occassion")
    )
    status_text = str(params.get("TransactionStatus") or "").strip().lower()
    receipt = params.get("ReceiptNo") or params.get("TransactionReceipt")
    top_code = normalize_result_code(result.get("ResultCode"))
    if status_text in ("completed", "complete", "success", "successful"):
        outcome_code = 0
    elif status_text in ("failed", "cancelled", "canceled", "declined", "expired"):
        outcome_code = 1
    elif top_code == 2033:
        # "The transaction receipt number cannot be found by the specified
        # OriginatorConversationID or ConversationID" — Daraja has no record of
        # the payout, so nothing was dispatched or paid: conclusively Failed.
        outcome_code = top_code
    else:
        outcome_code = None  # unknown → no-op

    return {
        "originator_conversation_id": originator,
        "conversation_id": params.get("ConversationID"),
        "result_code": outcome_code,
        "result_desc": params.get("ResultDesc") or result.get("ResultDesc"),
        "transaction_id": receipt,
        "receiver_public_name": params.get("CreditPartyPublicName") or params.get("ReceiverPartyPublicName"),
    }


def plan_receipt_attach(status, requested_by, session_user):
    if status != "Paid":
        return "Receipts can only be attached to Paid requests"
    if requested_by != session_user:
        return "You can only attach receipts to your own requests"
    return None


def handle_status_result(payload):
    import json

    parsed = parse_status_result(payload)
    name = find_request_by_originator(parsed.get("originator_conversation_id"))
    if not name:
        frappe.log_error(title="swdpay status result for unknown request", message=json.dumps(payload, default=str))
        return
    # Raw-first, mirroring handle_payout_callback: persist before any downstream action.
    frappe.db.set_value("M-Pesa Payment Request", name, "raw_result", json.dumps(payload, default=str))
    if parsed.get("result_code") is None:
        frappe.log_error(title=f"swdpay status result inconclusive: {name}", message=json.dumps(payload, default=str))
        return
    frappe.enqueue(
        "swdpay.outbound.reconcile_payout", queue="short",
        job_id=f"swdpay-payout-reconcile-{name}-status", deduplicate=True, enqueue_after_commit=True,
        request_name=name, parsed=parsed, verify_conversation=False,
    )
