import frappe

from mpesa_erpnext.reconciliation import process_payload


@frappe.whitelist()
def import_transaction(payload):
    _validate_import_permission()
    transaction = process_payload(payload)
    return {
        "transaction": transaction.name,
        "status": transaction.status,
        "payment_entry": transaction.payment_entry,
        "error_message": transaction.error_message,
    }


def _validate_import_permission():
    allowed_roles = {"System Manager", "Accounts Manager", "M-Pesa Reconciliation Manager"}
    user_roles = set(frappe.get_roles())

    if not allowed_roles.intersection(user_roles):
        frappe.throw("Not permitted to import M-Pesa transactions", frappe.PermissionError)
