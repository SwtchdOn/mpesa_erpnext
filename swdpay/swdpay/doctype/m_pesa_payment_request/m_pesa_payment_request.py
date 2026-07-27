import frappe
from frappe.model.document import Document

from swdpay.outbound import validate_payment_request


class MPesaPaymentRequest(Document):
    def before_insert(self):
        self.requested_by = frappe.session.user
        self.status = "Requested"
        self.originator_conversation_id = None  # set to doc name after autoname

    def after_insert(self):
        frappe.db.set_value(self.doctype, self.name, "originator_conversation_id", self.name, update_modified=False)

    def validate(self):
        validate_payment_request(self)
