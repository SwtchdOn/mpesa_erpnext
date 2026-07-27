app_name = "swdpay"
app_title = "SwdPay"
app_publisher = "William Luke"
app_description = "Match M-Pesa Paybill payments to ERPNext Sales Invoices"
app_email = "william@switchedon.world"
app_license = "MIT"

doctype_js = {
    "M-Pesa Transaction": "public/js/m_pesa_transaction.js",
}

jinja = {
    "methods": [
        "swdpay.qr.mpesa_qr",
        "swdpay.qr.mpesa_qr_payload",
    ],
}

scheduler_events = {
    "cron": {
        "*/5 * * * *": ["swdpay.outbound.resolve_stuck_payouts"],
    },
}

fixtures = [
    {
        "dt": "Role",
        "filters": [["role_name", "in", ["M-Pesa Reconciliation Manager", "M-Pesa Payment Requester", "M-Pesa Payment Approver"]]],
    },
    {
        "dt": "Custom Field",
        "filters": [["dt", "=", "Supplier"], ["fieldname", "=", "mpesa_identifier"]],
    },
]
