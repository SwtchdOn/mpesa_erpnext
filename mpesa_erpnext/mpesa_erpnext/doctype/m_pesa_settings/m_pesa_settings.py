import frappe
from frappe.model.document import Document


class MPesaSettings(Document):
    def validate(self):
        if self.auto_submit_payment_entry and not self.paid_to_account:
            frappe.throw("Paid To Account is required when auto-submitting Payment Entries.")

