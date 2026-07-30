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

    # Mirror the receipt onto the draft Purchase Invoice so ops sees it where
    # they complete the accounting. Best-effort — the receipt on the request is
    # the source of truth.
    if doc.purchase_invoice:
        try:
            frappe.get_doc({
                "doctype": "File",
                "file_url": file_url,
                "attached_to_doctype": "Purchase Invoice",
                "attached_to_name": doc.purchase_invoice,
            }).insert(ignore_permissions=True)
            if etims_url:
                frappe.db.set_value(
                    "Purchase Invoice", doc.purchase_invoice,
                    "bill_no",
                    parse_etims_invoice_number(etims_url)
                    or frappe.db.get_value("Purchase Invoice", doc.purchase_invoice, "bill_no"),
                )
        except Exception:
            frappe.log_error(title=f"swdpay receipt → PI attach failed: {name}")

    return {"etims_invoice_number": parse_etims_invoice_number(etims_url)}
