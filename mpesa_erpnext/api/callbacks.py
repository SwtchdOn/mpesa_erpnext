import frappe

from mpesa_erpnext.reconciliation import process_payload


@frappe.whitelist(allow_guest=True)
def c2b_confirmation(token=None):
    _validate_callback_token(token)

    payload = _get_callback_payload()
    transaction = process_payload(payload, auto_commit=True)

    frappe.local.response.update(
        {
            "ResultCode": 0,
            "ResultDesc": "Accepted",
            "Transaction": transaction.name,
            "Status": transaction.status,
        }
    )


def _validate_callback_token(token):
    settings = frappe.get_single("M-Pesa Settings")
    expected = settings.get_password("callback_secret") if settings.callback_secret else None

    if expected and token != expected:
        frappe.throw("Invalid callback token", frappe.PermissionError)


def _get_callback_payload():
    if frappe.request and frappe.request.is_json:
        payload = frappe.request.get_json() or {}
    else:
        payload = dict(frappe.local.form_dict)

    payload.pop("cmd", None)
    payload.pop("token", None)
    return payload
