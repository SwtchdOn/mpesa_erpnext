frappe.ui.form.on("M-Pesa Settings", {
  refresh(frm) {
    if (frm.doc.enable_outbound_payments) {
      frm.add_custom_button(__("Test Initiator (Balance Query)"), () => {
        frappe.call({ method: "swdpay.outbound.test_initiator_balance", freeze: true }).then((r) => {
          const response = r.message || {};
          const code = frappe.utils.escape_html(String(response.ResponseCode ?? ""));
          const description = frappe.utils.escape_html(response.ResponseDescription || "");
          frappe.msgprint({
            title: __("Balance Query Submitted"),
            message: `
              <p><strong>${__("Response Code")}:</strong> ${code}</p>
              <p><strong>${__("Response Description")}:</strong> ${description}</p>
              <p>${__(
                "This only confirms Daraja accepted the query — it does not prove the balance itself. " +
                  "The actual balance arrives asynchronously via the balance_result callback and, on a " +
                  "publicly reachable site, populates the Last Balance Result field below once processed."
              )}</p>
            `,
          });
        });
      });
    }
  },
});
