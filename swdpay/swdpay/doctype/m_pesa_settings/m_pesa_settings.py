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
            from swdpay.reconciliation import get_write_off_account, get_write_off_cost_center

            if not self.max_write_off_difference or self.max_write_off_difference <= 0:
                frappe.throw("Maximum Write Off Difference must be greater than zero.")

            if not get_write_off_account(self):
                frappe.throw(
                    "Write Off Account is required when small difference write off is enabled. "
                    "Set it here or as the Company default Write Off Account."
                )

            if not get_write_off_cost_center(self):
                frappe.throw(
                    "Write Off Cost Center is required when small difference write off is enabled. "
                    "Set it here or as the Company default Cost Center."
                )

        self._validate_account_types()

    def _validate_account_types(self):
        receivable_accounts = [("Default Receivable Account", self.default_receivable_account)]
        paid_to_accounts = [("Paid To Account", self.paid_to_account)]

        for row in self.payment_account_mappings or []:
            receivable_accounts.append((f"Receivable Account (mapping row {row.idx})", row.receivable_account))
            paid_to_accounts.append((f"Paid To Account (mapping row {row.idx})", row.paid_to_account))

        for label, account in receivable_accounts:
            if account and frappe.get_cached_value("Account", account, "account_type") != "Receivable":
                frappe.throw(
                    f"{label} {account} must be an account of type Receivable. "
                    "Party payments can only be posted against a Receivable account."
                )

        for label, account in paid_to_accounts:
            if account and frappe.get_cached_value("Account", account, "account_type") not in ("Bank", "Cash"):
                frappe.throw(
                    f"{label} {account} must be an account of type Bank or Cash. "
                    "This is the account the M-Pesa money is deposited into."
                )
