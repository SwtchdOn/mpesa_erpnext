import frappe
from frappe.model.document import Document


class MPesaSettings(Document):
    def validate(self):
        if self.auto_submit_payment_entry and not self.paid_to_account and not self.payment_account_mappings:
            frappe.throw(
                "Paid To Account or at least one Payment Account Mapping is required "
                "when auto-submitting Payment Entries."
            )
