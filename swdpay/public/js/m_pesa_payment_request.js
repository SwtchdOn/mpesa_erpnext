frappe.ui.form.on("M-Pesa Payment Request", {
  refresh(frm) {
    if (frm.doc.status === "Requested" && frappe.user.has_role("M-Pesa Payment Approver")) {
      frm.add_custom_button(__("Approve & Pay"), () => {
        frappe.confirm(
          __(`Send KES ${frm.doc.amount} to ${frm.doc.payee_name}?`),
          () =>
            frm
              .call({ method: "swdpay.outbound.approve_and_send", args: { name: frm.doc.name } })
              .then(() => frm.reload_doc()),
        );
      }).addClass("btn-primary");

      frm.add_custom_button(__("Reject"), () => {
        frappe.prompt(
          { fieldname: "reason", fieldtype: "Small Text", label: __("Reason"), reqd: 1 },
          (values) =>
            frm
              .call({ method: "swdpay.outbound.reject_request", args: { name: frm.doc.name, reason: values.reason } })
              .then(() => frm.reload_doc()),
          __("Reject payment request"),
        );
      });
    }
  },
});
