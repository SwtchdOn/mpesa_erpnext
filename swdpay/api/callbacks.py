import hmac

import frappe

from swdpay.reconciliation import enqueue_reconciliation, save_transaction_from_payload, validate_c2b_payload


@frappe.whitelist(allow_guest=True)
def c2b_confirmation(token=None):
    _validate_callback_token(token)

    payload = _get_callback_payload()
    transaction, duplicate = save_transaction_from_payload(payload)

    if not duplicate and transaction.status != "Exception":
        enqueue_reconciliation(transaction.name)

    frappe.db.commit()

    frappe.local.response.update(
        {
            "ResultCode": 0,
            "ResultDesc": "Accepted",
        }
    )


@frappe.whitelist(allow_guest=True)
def c2b_validation(token=None):
    _validate_callback_token(token)

    payload = _get_callback_payload()
    result_code, result_desc = validate_c2b_payload(payload)

    frappe.local.response.update(
        {
            "ResultCode": result_code,
            "ResultDesc": result_desc,
        }
    )


def _validate_callback_token(token):
    settings = frappe.get_single("M-Pesa Settings")
    expected = settings.get_password("callback_secret") if settings.callback_secret else None
    provided = _get_request_token(token)

    if not expected:
        frappe.throw("M-Pesa callback token is not configured", frappe.PermissionError)

    if not provided or not hmac.compare_digest(provided.strip(), expected.strip()):
        frappe.throw("Invalid callback token", frappe.PermissionError)


def _get_request_token(token=None):
    if token:
        return token

    if frappe.form_dict and frappe.form_dict.get("token"):
        return frappe.form_dict.get("token")

    if frappe.request and frappe.request.args:
        return frappe.request.args.get("token")

    return None


def _get_callback_payload():
    if frappe.request and frappe.request.is_json:
        payload = frappe.request.get_json() or {}
    else:
        payload = dict(frappe.local.form_dict)

    payload.pop("cmd", None)
    payload.pop("token", None)
    return payload
