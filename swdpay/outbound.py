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
