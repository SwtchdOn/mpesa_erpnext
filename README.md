# M-Pesa ERPNext

ERPNext custom app for receiving Safaricom M-Pesa Paybill C2B payments, matching them to Sales Invoices, and creating Payment Entries.

The MVP flow is:

1. Receive or manually import an M-Pesa transaction.
2. Save the raw transaction payload first.
3. Prevent duplicates using the M-Pesa receipt number.
4. Match `BillRefNumber` to a submitted Sales Invoice.
5. Create a Receive Payment Entry and allocate it to the invoice.
6. If invoice matching fails and phone matching is enabled, match the transaction to a Customer by phone number and create an unallocated customer Payment Entry.
7. Save clear exception messages for manual review.

## Features

- `M-Pesa Settings` Single DocType.
- `M-Pesa Transaction` DocType with unique receipt number.
- `M-Pesa Payment Account Mapping` child table for Paybill-specific accounting setup.
- Manual payload import endpoint.
- Daraja C2B confirmation callback endpoint.
- Idempotent transaction saving.
- Sales Invoice matching by `BillRefNumber`.
- Optional Customer matching by phone number.
- Payment Entry creation and optional submission.
- Partial payment support.
- Overpayment blocking by default, configurable in settings.
- Exception status and error message capture.
- Manual retry and ignore actions from the transaction form.

## Settings

Configure `M-Pesa Settings` before processing payments.

Required base settings:

- `Company`
- `Paybill Number`
- `Environment`
- `Callback Secret / Token`

Default accounting fields:

- `Default Mode of Payment`
- `Paid To Account`
- `Default Receivable Account`

These defaults are used when no payment account mapping row matches.

### Payment Account Mappings

Use the `Payment Account Mappings` table when different Paybill numbers, tills, or environments should post to different accounts.

Each row supports:

- `Paybill Number`
- `Default`
- `Mode of Payment`
- `Paid To Account`
- `Receivable Account`

Resolution order when creating a Payment Entry:

1. Exact mapping for the transaction Paybill number.
2. Mapping row marked as `Default`.
3. Legacy default fields on `M-Pesa Settings`.

`Paid To Account` is the M-Pesa clearing/bank account where the receipt is posted. `Receivable Account` is used for Customer phone matches when there is no Sales Invoice to provide the receivable account.

## Transaction Matching

### Invoice Matching

The app first checks whether `BillRefNumber` matches a Sales Invoice name.

The invoice must be:

- Submitted.
- In the configured Company.
- Not fully paid.
- Not overpaid unless overpayments are explicitly enabled.

If valid, the Payment Entry is allocated to the Sales Invoice.

### Customer Phone Matching

If no Sales Invoice matches and `Allow Customer Phone Matching` is enabled, the app normalizes the M-Pesa phone number and checks `Customer.mobile_no` and `Customer.phone`.

Supported Kenyan formats include:

- `254712345678`
- `0712345678`
- `712345678`
- `+254 712 345 678`

Phone matching only succeeds when exactly one Customer matches. If zero or multiple Customers match, the transaction is marked as `Exception`.

Customer phone matches create an unallocated Receive Payment Entry for that Customer.

## Endpoints

### Daraja C2B Confirmation

```text
/api/method/mpesa_erpnext.api.callbacks.c2b_confirmation?token=YOUR_SECRET
```

The callback accepts JSON or form-encoded payloads. It returns a top-level Daraja-style success response:

```json
{
  "ResultCode": 0,
  "ResultDesc": "Accepted"
}
```

The callback token is compared with `Callback Secret / Token` in `M-Pesa Settings`.

### Manual Import

```text
/api/method/mpesa_erpnext.api.imports.import_transaction
```

Pass a `payload` argument containing the raw M-Pesa JSON.

Example payload:

```json
{
  "TransID": "RHF123ABC",
  "TransAmount": "10000",
  "BusinessShortCode": "123456",
  "BillRefNumber": "SINV-2026-00045",
  "MSISDN": "254712345678",
  "FirstName": "John",
  "MiddleName": "",
  "LastName": "Kamau",
  "TransTime": "20260519124500"
}
```

## Statuses

`M-Pesa Transaction` supports:

- `New`
- `Matched`
- `Reconciled`
- `Duplicate`
- `Exception`
- `Ignored`

Duplicate callbacks do not create another Payment Entry. The existing transaction is returned.

## Payment Entry Behaviour

For invoice matches:

- `Payment Type`: `Receive`
- `Party Type`: `Customer`
- `Party`: Sales Invoice Customer
- `Paid Amount`: M-Pesa amount
- `Received Amount`: M-Pesa amount
- `Paid From`: Sales Invoice receivable account
- `Paid To`: resolved from Payment Account Mapping or settings default
- `Mode of Payment`: resolved from Payment Account Mapping or settings default
- `Reference No`: M-Pesa receipt number
- `Reference Date`: M-Pesa transaction date
- References table: Sales Invoice allocation

For customer phone matches:

- No Sales Invoice reference is added.
- The receipt is posted against the matched Customer.
- The amount remains unallocated for later reconciliation.

## Development Notes

This repository currently contains the custom app source only. Full integration testing requires an ERPNext/Frappe bench site with the app installed.

Basic local checks used during development:

```bash
python3 -m py_compile mpesa_erpnext/reconciliation.py mpesa_erpnext/api/callbacks.py mpesa_erpnext/api/imports.py
python3 -m json.tool mpesa_erpnext/mpesa_erpnext/doctype/m_pesa_settings/m_pesa_settings.json
python3 -m json.tool mpesa_erpnext/mpesa_erpnext/doctype/m_pesa_transaction/m_pesa_transaction.json
python3 -m json.tool mpesa_erpnext/mpesa_erpnext/doctype/m_pesa_payment_account_mapping/m_pesa_payment_account_mapping.json
```

