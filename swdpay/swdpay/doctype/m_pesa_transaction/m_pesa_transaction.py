import frappe
from frappe.model.document import Document


class MPesaTransaction(Document):
    def validate(self):
        if self.amount is not None and self.amount <= 0:
            self.status = "Exception"
            self.error_message = "Amount is zero"

    @frappe.whitelist()
    def retry_reconciliation(self):
        from swdpay.reconciliation import reconcile_transaction

        reconcile_transaction(self.name, manual=True)
        self.reload()
        return self

    @frappe.whitelist()
    def create_payment_entry(self):
        from swdpay.reconciliation import reconcile_transaction

        reconcile_transaction(self.name, manual=True)
        self.reload()
        return self

    @frappe.whitelist()
    def mark_as_ignored(self):
        self.status = "Ignored"
        self.save()
        return self
