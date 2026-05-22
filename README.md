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
- Daraja C2B validation endpoint.
- Idempotent transaction saving.
- Sales Invoice matching by `BillRefNumber`.
- Optional Customer matching by phone number.
- Payment Entry creation and optional submission.
- Partial payment support.
- Overpayment blocking by default, configurable in settings.
- Exception status and error message capture.
- Manual retry and ignore actions from the transaction form.
- Role-gated manual transaction imports.
- Dedicated `M-Pesa Reconciliation Manager` role fixture.
- Fail-closed callback token validation.

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
- `Enable Strict C2B Validation`

These defaults are used when no payment account mapping row matches.

Strict C2B validation is disabled by default so the validation URL does not accidentally block legitimate payments with mistyped or non-invoice account numbers. Enable it only when your customer payment reference process is clean.

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

It also checks linked Contact records where the Contact is linked to a Customer.

Supported Kenyan formats include:

- `254712345678`
- `0712345678`
- `712345678`
- `+254 712 345 678`

Phone matching only succeeds when exactly one Customer matches. If zero or multiple Customers match, the transaction is marked as `Exception`.

Customer phone matches create an unallocated Receive Payment Entry for that Customer.

## Endpoints

### Daraja C2B Confirmation

Use the neutral endpoint below when registering URLs with Daraja. Daraja rejects URLs containing words such as `mpesa`, `cmd`, `sql`, or `query`.

```text
/api/method/payments.callbacks.confirmation?token=YOUR_SECRET
```

Legacy internal endpoint:

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

The callback fails closed if no callback token is configured.

### Daraja C2B Validation

Use the neutral endpoint below when registering URLs with Daraja:

```text
/api/method/payments.callbacks.validation?token=YOUR_SECRET
```

Legacy internal endpoint:

```text
/api/method/mpesa_erpnext.api.callbacks.c2b_validation?token=YOUR_SECRET
```

Use this as the Daraja Validation URL.

The validation endpoint checks:

- Amount is greater than zero.
- Sales Invoice references are submitted, in the configured Company, outstanding, and not overpaid unless overpayments are enabled.
- Sales Order and Quotation references are accepted for downstream/manual handling.
- Customer phone fallback is accepted when enabled.

If `Enable Strict C2B Validation` is disabled, unknown or missing account numbers are accepted so the payment can be reviewed after confirmation. If strict validation is enabled, unknown or missing account numbers are rejected.

Accepted response:

```json
{
  "ResultCode": 0,
  "ResultDesc": "Accepted"
}
```

Rejected response:

```json
{
  "ResultCode": "C2B00011",
  "ResultDesc": "No matching reference found for account number BAD-REF"
}
```

### Manual Import

```text
/api/method/mpesa_erpnext.api.imports.import_transaction
```

Pass a `payload` argument containing the raw M-Pesa JSON.

Manual imports require one of these roles:

- `System Manager`
- `Accounts Manager`
- `M-Pesa Reconciliation Manager`

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

If a duplicate callback arrives concurrently while the first request is inserting the same receipt number, the unique receipt constraint is caught and the existing transaction is returned.

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

If Payment Entry submission fails after the draft Payment Entry is inserted, the transaction keeps the draft Payment Entry link so a retry can submit the existing document instead of creating another draft.

## Development Notes

This repository currently contains the custom app source only. Full integration testing requires an ERPNext/Frappe bench site with the app installed.

Basic local checks used during development:

```bash
python3 -m py_compile mpesa_erpnext/reconciliation.py mpesa_erpnext/api/callbacks.py mpesa_erpnext/api/imports.py
python3 -m json.tool mpesa_erpnext/mpesa_erpnext/doctype/m_pesa_settings/m_pesa_settings.json
python3 -m json.tool mpesa_erpnext/mpesa_erpnext/doctype/m_pesa_transaction/m_pesa_transaction.json
python3 -m json.tool mpesa_erpnext/mpesa_erpnext/doctype/m_pesa_payment_account_mapping/m_pesa_payment_account_mapping.json
```
