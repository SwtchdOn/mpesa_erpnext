# SwdPay ERPNext

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
- Background reconciliation for live confirmation callbacks.
- Sales Invoice matching by `BillRefNumber`.
- Optional Customer matching by phone number.
- Payment Entry creation and optional submission.
- Partial payment support.
- Overpayment blocking by default, configurable in settings.
- Optional small difference write-off for M-Pesa whole-shilling rounding, covering both small shortfalls and small overpayments.
- Multi-currency Sales Invoice and Sales Order references settled at the document exchange rate.
- Scan-to-pay M-Pesa QR codes for print formats via the `mpesa_qr` Jinja method.
- Exception status and error message capture.
- Reconciliation attempt history with attempt count and last attempt time.
- Frappe Error Log entries for reconciliation failures.
- Manual retry, manual Payment Entry creation, and ignore actions from the transaction form.
- Role-gated manual transaction imports.
- Dedicated `M-Pesa Reconciliation Manager` role fixture.
- Fail-closed callback token validation.

## App Name

The installed Frappe app name is:

```text
swdpay
```

This keeps Daraja callback URLs free from blocked keywords such as `mpesa`, `cmd`, `sql`, and `query`.

If you previously installed this app under the old `mpesa_erpnext` name, treat this as an app rename. In a production site, plan the migration carefully so existing DocTypes and data are preserved.

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

### Small Difference Write Off

M-Pesa payments are whole-shilling payments, so invoices with cents can leave very small outstanding balances or very small overpayments when customers round up. Enable `Small Difference Write Off` if these differences should be cleared automatically in both directions.

Configure:

- `Enable Small Difference Write Off`
- `Maximum Write Off Difference`
- `Write Off Account`
- `Write Off Cost Center`

When the difference between the invoice outstanding amount and the M-Pesa amount is within the configured maximum, the Payment Entry allocates the full invoice outstanding amount and adds a deductions row for the difference against the Write Off Account: a positive (loss) row for shortfalls, a negative (gain) row for overpayments. Small overpayments within the maximum are accepted even when `Allow Overpayments` is disabled, so rounded-up payments do not leave stray customer credits. For foreign-currency invoices the difference is booked to the Company Exchange Gain/Loss Account instead (see Multi-Currency References).

Larger shortfalls continue to follow the normal partial payment settings. Larger overpayments still require `Allow Overpayments` and are kept as unallocated customer credit.

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
- Not overpaid unless overpayments are explicitly enabled or the excess is within the small difference write-off maximum.

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

Some Daraja C2B callbacks send a hashed `MSISDN` instead of a plain phone number. The app stores this in `Phone Hash` and leaves `Phone Number` blank, because the hash cannot be safely used for Customer phone matching without a separate decoding/mapping process.

## Endpoints

### Daraja C2B Confirmation

Use this endpoint when registering the Daraja Confirmation URL. The installed app name is `swdpay`, so the URL avoids Daraja's blocked keywords such as `mpesa`, `cmd`, `sql`, and `query`.

```text
/api/method/swdpay.api.callbacks.c2b_confirmation?token=YOUR_SECRET
```

The callback accepts JSON or form-encoded payloads. It saves the raw transaction first, queues reconciliation in the background, commits the saved transaction, and returns quickly to Daraja.

It returns a top-level Daraja-style success response:

```json
{
  "ResultCode": 0,
  "ResultDesc": "Accepted"
}
```

The callback token is compared with `Callback Secret / Token` in `M-Pesa Settings`.

The callback fails closed if no callback token is configured.

### Daraja C2B Validation

Use this endpoint when registering the Daraja Validation URL:

```text
/api/method/swdpay.api.callbacks.c2b_validation?token=YOUR_SECRET
```

The validation endpoint checks:

- Amount is greater than zero.
- Sales Invoice references are submitted, in the configured Company, outstanding, and not overpaid unless overpayments are enabled or the excess is within the small difference write-off maximum.
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
/api/method/swdpay.api.imports.import_transaction
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

Before creating a Payment Entry, the app also checks for an existing non-cancelled Payment Entry with `Reference No` equal to the M-Pesa receipt number. If found, it links that Payment Entry to the transaction and submits it when auto-submit is enabled instead of creating a duplicate.

Each reconciliation run appends a read-only attempt row to the transaction. The parent transaction also stores `Reconciliation Attempt Count` and `Last Reconciliation Attempt`.

When reconciliation fails, the transaction is marked `Exception`, the visible `Error Message` is updated, the attempt history records the failure source, and a Frappe Error Log entry is written with the traceback.

Exception or unmatched transactions can be resolved manually from the transaction form. If the receipt was already created manually, select it in the Reconciliation section's `Payment Entry` field and save the transaction. Otherwise use **Create Payment Entry** to select a submitted Sales Invoice, submitted Sales Order, or Customer. New Payment Entries use the transaction receipt number as `Reference No`, and the app still links any existing non-cancelled Payment Entry with the same receipt before creating a new one.

## Scan to Pay QR Codes

Print formats can embed an M-Pesa scan-to-pay QR code that customers scan with the M-Pesa app. In the Print Format Builder, add a **Custom HTML** block containing:

```jinja
{{ mpesa_qr(doc) }}
```

Defaults when rendered on a Sales Invoice, Sales Order, or Quotation:

- Paybill: the `Paybill Number` from M-Pesa Settings.
- Account reference: the document name, so the payment reconciles back to the document automatically.
- Amount: the document's outstanding amount in company currency (converted for multi-currency documents), rounded **up** to a whole shilling — M-Pesa only accepts whole-shilling amounts, and the sub-shilling excess is cleared by the small difference write-off.
- Rendering: a PesaQR-style branded card — green border with a bold "SCAN WITH M-PESA" header — sized for reliable printing through wkhtmltopdf.
- Nothing is rendered when the document has no outstanding amount, so paid invoices print without a QR code.

Options:

```jinja
{{ mpesa_qr(doc, size=220) }}
{{ mpesa_qr(doc, amount=500) }}
{{ mpesa_qr(doc, paybill="888880", account="CUSTOM-REF") }}
{{ mpesa_qr(doc, branded=False) }}          {# bare QR SVG, no card #}
{{ mpesa_qr(doc, label="LIPA NA M-PESA") }} {# custom header text #}
{{ mpesa_qr(amount=100, till="123456") }}
{{ mpesa_qr(amount=100, phone="0712345678") }}
```

The QR payload uses the [PesaQR](https://github.com/DavidAmunga/pesaqr) format understood by the M-Pesa app: `PB|paybill|amount|account`, `BG|till|amount`, or `SM|phone|amount`. `mpesa_qr_payload(...)` is also exposed to Jinja if you want the raw payload string.

## Payment Entry Behaviour

For invoice matches:

- `Payment Type`: `Receive`
- `Party Type`: `Customer`
- `Party`: Sales Invoice Customer
- `Paid Amount`: M-Pesa amount in the receivable account currency (converted at the invoice exchange rate for multi-currency invoices)
- `Received Amount`: M-Pesa amount
- `Paid From`: Sales Invoice receivable account
- `Paid To`: resolved from Payment Account Mapping or settings default
- `Mode of Payment`: resolved from Payment Account Mapping or settings default
- `Reference No`: M-Pesa receipt number
- `Reference Date`: M-Pesa transaction date
- References table: Sales Invoice allocation
- Small configured shortfalls: full invoice outstanding allocation plus a deductions/loss row against the configured write-off account and cost center
- Small configured overpayments: full invoice outstanding allocation plus a negative deductions/gain row against the configured write-off account and cost center

For manual Sales Order matches:

- The receipt is posted as a Customer advance against the submitted Sales Order.
- The Sales Order reference allocation is limited to the order total less existing advances, in the receivable account currency.
- The selected Sales Order and resulting Payment Entry are linked back to the M-Pesa Transaction.

### Multi-Currency References

Sales Invoices and Sales Orders in a foreign currency (for example USD invoices paid in KES) are supported when the customer's receivable account is in the document currency:

- The KES amount is converted at the document's booked exchange rate, so the receivable is settled at the same rate it was booked at.
- `Paid Amount` and the reference allocation are in the document currency; `Received Amount` stays in KES with the M-Pesa amount.
- ERPNext automatically books the difference between the converted paid amount and the KES received amount to the Company `Exchange Gain/Loss Account` as a Payment Entry deduction. This covers both conversion rounding residues and small over/underpayments within the write-off tolerance, so the Payment Entry difference is always zero. Multi-currency payments therefore need the Company defaults `Exchange Gain/Loss Account` and `Cost Center` set; the M-Pesa Settings Write Off Account is only used for company-currency (KES) write-offs.
- The small difference write-off tolerance and the partial payment / overpayment checks compare amounts in company currency (KES), so thresholds keep their meaning regardless of the invoice currency. The M-Pesa Transaction still records the signed `Write Off Amount`, with the Exchange Gain/Loss account as the write-off account.
- Customer phone matches against a foreign-currency receivable convert at the current Currency Exchange rate for the transaction date; if no rate is found, the transaction goes to Exception with a clear message.
- The `Paid To` bank or cash account must be in the company currency, since M-Pesa settles in KES.

For manually linked Payment Entries:

- Cancelled Payment Entries are rejected.
- The Payment Entry must be a Customer Receive payment with an amount matching the M-Pesa transaction.
- If the Payment Entry has a `Reference No`, it must match the M-Pesa receipt number.
- Sales Invoice and Sales Order references on the Payment Entry are copied back to the M-Pesa Transaction where present.

For customer phone matches:

- No Sales Invoice reference is added.
- The receipt is posted against the matched Customer.
- The amount remains unallocated for later reconciliation.

If Payment Entry submission fails after the draft Payment Entry is inserted, the transaction keeps the draft Payment Entry link so a retry can submit the existing document instead of creating another draft.

## Outbound Payments (B2B/B2C)

In addition to receiving Paybill payments, SwdPay can **send** money out via Daraja's Business-to-Customer (B2C, phone payouts) and Business-to-Business (B2B, Buy Goods/Paybill payouts) products, driven by an in-app approval workflow on the `M-Pesa Payment Request` DocType.

### Settings

All outbound fields live in the `Outbound Payments (B2B / B2C)` section of `M-Pesa Settings`, gated by `Enable Outbound Payments` (off by default — see Production Prerequisites below). Once enabled, `validate()` fails closed and requires:

- `Initiator Name` — the Daraja API operator username.
- `Initiator Password` — the API operator's password (used to build the encrypted `SecurityCredential`).
- `Daraja Security Certificate (PEM)` — Safaricom's public certificate (sandbox `SandboxCertificate.cer` or the production cert) used to RSA-encrypt the initiator password. Encryption only needs a syntactically valid PEM; it does not need to chain to a trusted root for the app's own encryption step to work.
- `B2C Shortcode (defaults to Paybill)` — the shortcode used as `PartyA` for phone payouts; falls back to `Paybill Number` when blank.
- `Max Request Amount` — hard cap enforced at doc validation (every save), at approval (`approve_and_send`), and re-checked at dispatch (`send_payment_request`, the last line of defence against a cap change or stale approval landing between approve and send).
- `Outbound Paid From Account` — must be a `Bank` or `Cash` Account; this is where payout money is drawn from in the resulting Payment Entry.
- `Outbound Mode of Payment`.
- `Callback Base URL (public site URL)` — the externally-reachable base URL Daraja uses to reach this site; see Callback URLs below.

### Callback URLs (per-request, not registered)

Unlike the C2B Confirmation/Validation URLs (registered once against the Paybill via `bench execute` / the Daraja portal), the outbound callback URLs are **built per-request** by `build_payout_request` and sent as `ResultURL` / `QueueTimeOutURL` fields in each individual B2C/B2B API call:

- `swdpay.api.callbacks.payout_result` — Daraja's terminal result (success or failure) for a payout.
- `swdpay.api.callbacks.payout_timeout` — Daraja's queue-timeout notification; leaves the request in `Sending` for the watchdog to resolve.
- `swdpay.api.callbacks.status_result` — the result of a `TransactionStatusQuery`, used only by the stuck-payout watchdog (`resolve_stuck_payouts`, scheduled every 5 minutes).

There is nothing to paste into the Daraja portal for these — they are generated automatically from `Callback Base URL` + `Callback Secret / Token` on every dispatch.

### Roles

- `M-Pesa Payment Requester` — can create payment requests for themselves (`if_owner` read).
- `M-Pesa Payment Approver` — sees the **Approve & Pay** / **Reject** buttons on a `Requested` request, and can call `approve_and_send` / `reject_request`.
- `System Manager` — full access, same as other SwdPay DocTypes.

### Approval Flow

1. A Requester creates an `M-Pesa Payment Request` (Buy Goods Till, Paybill, or Phone) — status `Requested`.
2. An Approver clicks **Approve & Pay** on the form (`swdpay/public/js/m_pesa_payment_request.js`). This calls `swdpay.outbound.approve_and_send`, which re-validates the request, moves it to `Approved`, and enqueues `send_payment_request` (deduplicated by `job_id`, after commit).
3. The background job row-locks the request, builds the Daraja B2C/B2B payload, encrypts the security credential, moves the request to `Sending`, and POSTs to Daraja.
4. Daraja's asynchronous result callback (`payout_result`) is handled raw-first: the payload is persisted on the request before reconciliation is queued. `reconcile_payout` row-locks the request, verifies the callback's `ConversationID` against the one stored at dispatch time, and on success moves the request to `Paid`, sets `receipt_pending = 1`, resolves/creates the Supplier or Employee party, and creates + submits a `Pay`-type Payment Entry (idempotent — a duplicate callback is a no-op once the request is out of `Sending`).
5. Field staff attach a receipt via `swdpay.api.field.attach_receipt(name, file_url, etims_url=None)` — restricted to the requester, and only while the request is `Paid`. It parses an eTIMS invoice number out of a KRA-style URL query string and clears `receipt_pending`.
6. An Approver can instead **Reject** a `Requested` request with a reason, moving it straight to `Rejected`.
7. If a dispatch is ambiguous (network error, or Daraja never calls back) the request stays `Sending`; the `resolve_stuck_payouts` watchdog queries `TransactionStatusQuery` for anything stuck longer than 5 minutes and reconciles from that instead.

### Production Prerequisites

Keep `Enable Outbound Payments` **off** until all of the following are true:

- The B2C and B2B products are provisioned on your Daraja production app (these are separate approvals from C2B/Paybill access).
- A dedicated Initiator (API operator) user has been created against your shortcode, with its password known and the production security certificate obtained from Safaricom.
- The utility/working account behind your shortcode is funded — B2C/B2B payouts draw from that Safaricom-side balance, not from `Outbound Paid From Account` (which is only the ERPNext-side ledger account debited by the resulting Payment Entry).
- `Callback Base URL` is a publicly reachable HTTPS URL Daraja can call back on production.
- `Max Request Amount` reflects your actual payout policy — it is the only cap enforced beyond Daraja's own limits.

## Development Notes

This repository currently contains the custom app source only. Full integration testing requires an ERPNext/Frappe bench site with the app installed.

Basic local checks used during development:

```bash
python3 -m py_compile swdpay/reconciliation.py swdpay/api/callbacks.py swdpay/api/imports.py
python3 -m json.tool swdpay/swdpay/doctype/m_pesa_settings/m_pesa_settings.json
python3 -m json.tool swdpay/swdpay/doctype/m_pesa_transaction/m_pesa_transaction.json
python3 -m json.tool swdpay/swdpay/doctype/m_pesa_reconciliation_attempt/m_pesa_reconciliation_attempt.json
python3 -m json.tool swdpay/swdpay/doctype/m_pesa_payment_account_mapping/m_pesa_payment_account_mapping.json
```
