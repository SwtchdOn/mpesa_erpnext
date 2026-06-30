import frappe
from frappe.model.document import Document


class MPesaTransaction(Document):
    def validate(self):
        if self.amount is not None and self.amount <= 0:
            self.status = "Exception"
            self.error_message = "Amount is zero"

        if self.payment_entry:
            from swdpay.reconciliation import apply_selected_payment_entry

            apply_selected_payment_entry(self)

    def on_update(self):
        if self.payment_entry:
            from swdpay.reconciliation import complete_selected_payment_entry

            complete_selected_payment_entry(self)

    @frappe.whitelist()
    def retry_reconciliation(self):
        from swdpay.reconciliation import reconcile_transaction

        reconcile_transaction(self.name, manual=True)
        self.reload()
        return self

    @frappe.whitelist()
    def create_payment_entry(self, sales_invoice=None, sales_order=None, customer=None, payment_entry=None):
        from swdpay.reconciliation import manually_create_payment_entry

        manually_create_payment_entry(
            self.name,
            sales_invoice=sales_invoice,
            sales_order=sales_order,
            customer=customer,
            payment_entry=payment_entry,
        )
        self.reload()
        return self

    @frappe.whitelist()
    def mark_as_ignored(self):
        self.status = "Ignored"
        self.save()
        return self
