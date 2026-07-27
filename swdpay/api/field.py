import frappe

from swdpay.outbound import parse_etims_invoice_number, plan_receipt_attach


@frappe.whitelist()
def attach_receipt(name, file_url, etims_url=None):
    doc = frappe.get_doc("M-Pesa Payment Request", name)
    error = plan_receipt_attach(doc.status, doc.requested_by, frappe.session.user)
    if error:
        frappe.throw(error)

    doc.db_set({
        "receipt_photo": file_url,
        "etims_url": etims_url or None,
        "etims_invoice_number": parse_etims_invoice_number(etims_url),
        "receipt_captured_on": frappe.utils.now_datetime(),
        "receipt_pending": 0,
    })
    return {"etims_invoice_number": parse_etims_invoice_number(etims_url)}
