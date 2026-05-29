import frappe
from frappe.model.document import Document


class MPesaSettings(Document):
    def validate(self):
        if self.auto_submit_payment_entry and not self.paid_to_account and not self.payment_account_mappings:
            frappe.throw(
                "Paid To Account or at least one Payment Account Mapping is required "
                "when auto-submitting Payment Entries."
            )

        if self.enable_small_difference_write_off:
            if not self.max_write_off_difference or self.max_write_off_difference <= 0:
                frappe.throw("Maximum Write Off Difference must be greater than zero.")

            if not self.write_off_account:
                frappe.throw("Write Off Account is required when small difference write off is enabled.")

            if not self.write_off_cost_center:
                frappe.throw("Write Off Cost Center is required when small difference write off is enabled.")
