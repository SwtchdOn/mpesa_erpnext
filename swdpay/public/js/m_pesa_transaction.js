frappe.ui.form.on("M-Pesa Transaction", {
  setup(frm) {
    frm.set_query("payment_entry", () => ({
      filters: {
        docstatus: ["<", 2],
        payment_type: "Receive",
        party_type: "Customer",
      },
    }));
  },

  refresh(frm) {
    if (frm.doc.docstatus !== 0) {
      return;
    }

    frm.add_custom_button(__("Retry Reconciliation"), () => {
      frm.call("retry_reconciliation").then(() => frm.reload_doc());
    });

    if (!frm.doc.payment_entry) {
      frm.add_custom_button(__("Create Payment Entry"), () => {
        const dialog = new frappe.ui.Dialog({
          title: __("Create Payment Entry"),
          fields: [
            {
              fieldname: "sales_invoice",
              fieldtype: "Link",
              label: __("Sales Invoice"),
              options: "Sales Invoice",
              default: frm.doc.sales_invoice,
              get_query: () => ({
                filters: {
                  docstatus: 1,
                },
              }),
            },
            {
              fieldname: "sales_order",
              fieldtype: "Link",
              label: __("Sales Order"),
              options: "Sales Order",
              default: frm.doc.sales_order,
              depends_on: "eval:!doc.sales_invoice",
              get_query: () => ({
                filters: {
                  docstatus: 1,
                },
              }),
            },
            {
              fieldname: "customer",
              fieldtype: "Link",
              label: __("Customer"),
              options: "Customer",
              default: frm.doc.customer,
              depends_on: "eval:!doc.sales_invoice && !doc.sales_order",
            },
          ],
          primary_action_label: __("Create"),
          primary_action(values) {
            if (!values.sales_invoice && !values.sales_order && !values.customer) {
              frappe.msgprint(__("Select a Sales Invoice, Sales Order, or Customer."));
              return;
            }

            dialog.hide();
            frm
              .call("create_payment_entry", {
                sales_invoice: values.sales_invoice,
                sales_order: values.sales_order,
                customer: values.customer,
              })
              .then(() => frm.reload_doc());
          },
        });

        dialog.show();
      });
    }

    if (frm.doc.status !== "Ignored") {
      frm.add_custom_button(__("Mark as Ignored"), () => {
        frm.call("mark_as_ignored").then(() => frm.reload_doc());
      });
    }
  },
});

