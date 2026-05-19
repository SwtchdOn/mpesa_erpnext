frappe.ui.form.on("M-Pesa Transaction", {
  refresh(frm) {
    if (frm.doc.docstatus !== 0) {
      return;
    }

    frm.add_custom_button(__("Retry Reconciliation"), () => {
      frm.call("retry_reconciliation").then(() => frm.reload_doc());
    });

    if (!frm.doc.payment_entry && frm.doc.sales_invoice) {
      frm.add_custom_button(__("Create Payment Entry"), () => {
        frm.call("create_payment_entry").then(() => frm.reload_doc());
      });
    }

    if (frm.doc.status !== "Ignored") {
      frm.add_custom_button(__("Mark as Ignored"), () => {
        frm.call("mark_as_ignored").then(() => frm.reload_doc());
      });
    }
  },
});

