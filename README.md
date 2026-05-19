# M-Pesa ERPNext

ERPNext custom app for matching Safaricom M-Pesa Paybill C2B transactions to Sales Invoices and creating Payment Entries.

MVP features:

- M-Pesa Settings Single DocType
- M-Pesa Transaction DocType with unique receipt number
- Manual payload import endpoint
- Daraja C2B confirmation callback
- Idempotent transaction saving
- Sales Invoice matching by `BillRefNumber`
- Payment Entry creation and optional submission
- Exception status and error message capture
- Optional fallback matching by Customer phone number when no Sales Invoice matches

Callback endpoint:

```text
/api/method/mpesa_erpnext.api.callbacks.c2b_confirmation?token=YOUR_SECRET
```

Manual import endpoint:

```text
/api/method/mpesa_erpnext.api.imports.import_transaction
```
