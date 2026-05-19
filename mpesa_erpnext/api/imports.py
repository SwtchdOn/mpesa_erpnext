import frappe

from mpesa_erpnext.reconciliation import process_payload


@frappe.whitelist()
def import_transaction(payload):
    transaction = process_payload(payload)
    return {
        "transaction": transaction.name,
        "status": transaction.status,
        "payment_entry": transaction.payment_entry,
        "error_message": transaction.error_message,
    }

