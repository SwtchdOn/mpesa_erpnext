import math
from io import BytesIO

import frappe
from frappe.utils import flt


MPESA_GREEN = "#16a34a"


def mpesa_qr(doc=None, amount=None, account=None, paybill=None, till=None, phone=None, size=160, branded=True, label="SCAN WITH M-PESA"):
    """Inline M-Pesa scan-to-pay QR code for print formats.

    Use in a Print Format Builder Custom HTML block: {{ mpesa_qr(doc) }}

    Defaults: Paybill from M-Pesa Settings, account reference = document name
    (so the payment reconciles back to the document), amount = the document's
    outstanding amount in company currency rounded up to a whole shilling.
    Renders a PesaQR-style branded card with a green "SCAN WITH M-PESA" header;
    pass branded=False for the bare SVG. Returns an empty string when nothing
    is outstanding."""
    if not (till or phone or paybill):
        paybill = frappe.db.get_single_value("M-Pesa Settings", "paybill_number")

    if amount is None and doc is not None:
        amount = get_document_outstanding_amount(doc)

    if flt(amount) <= 0:
        return ""

    if not account and doc is not None:
        account = doc.get("name")

    payload = mpesa_qr_payload(amount, paybill=paybill, till=till, phone=phone, account=account)
    svg = _render_qr_svg(payload, size)
    if not branded:
        return svg

    return _render_branded_card(svg, label)


def mpesa_qr_payload(amount, paybill=None, till=None, phone=None, account=None):
    """Payload scanned by the M-Pesa app, compatible with PesaQR
    (https://github.com/DavidAmunga/pesaqr): BG|till|amount,
    SM|phone|amount, or PB|paybill|amount|account."""
    qr_amount = format_qr_amount(amount)

    if till:
        return f"BG|{till}|{qr_amount}"

    if phone:
        return f"SM|{phone}|{qr_amount}"

    if not paybill:
        frappe.throw(
            "A Paybill, Till, or Phone Number is required for an M-Pesa QR code. "
            "Set the Paybill Number in M-Pesa Settings."
        )

    if not account:
        frappe.throw("An account reference is required for a Paybill M-Pesa QR code")

    return f"PB|{paybill}|{qr_amount}|{account}"


def format_qr_amount(amount):
    # M-Pesa payments are whole shillings; round up so the suggested payment
    # settles the document in full and the excess clears as a small difference
    return str(int(math.ceil(flt(amount))))


def get_document_outstanding_amount(doc):
    doctype = doc.get("doctype")

    if doctype == "Sales Invoice":
        from swdpay.reconciliation import get_invoice_outstanding_in_company_currency

        return get_invoice_outstanding_in_company_currency(doc)

    if doctype == "Sales Order":
        from swdpay.reconciliation import _get_sales_order_outstanding_amount, get_sales_order_exchange_rate

        exchange_rate = get_sales_order_exchange_rate(doc)
        return flt(_get_sales_order_outstanding_amount(doc, exchange_rate) * exchange_rate, 2)

    if doctype == "Quotation":
        return flt(doc.get("base_rounded_total") or doc.get("base_grand_total") or doc.get("grand_total"))

    return flt(doc.get("outstanding_amount") or doc.get("base_grand_total") or doc.get("grand_total"))


def _render_branded_card(svg, label):
    # solid header bar instead of PesaQR's transform-positioned pill:
    # wkhtmltopdf's WebKit does not render CSS transforms reliably
    html = (
        f'<div style="display: inline-block; border: 2px solid {MPESA_GREEN}; border-radius: 8px; '
        'background: #ffffff; text-align: center; font-family: Arial, sans-serif;">'
        f'<div style="background: {MPESA_GREEN}; border-radius: 5px 5px 0 0; color: #ffffff; '
        'font-weight: bold; font-size: 12px; letter-spacing: 0.5px; padding: 4px 10px;">'
        f"{label}</div>"
        f'<div style="padding: 8px 8px 4px;">{svg}</div>'
        "</div>"
    )

    try:
        from markupsafe import Markup

        return Markup(html)
    except ImportError:
        return html


def _render_qr_svg(payload, size):
    import pyqrcode

    buffer = BytesIO()
    pyqrcode.create(payload, error="M").svg(
        buffer,
        scale=1,
        module_color="#000000",
        background="#ffffff",
        xmldecl=False,
        omithw=True,
    )
    svg = buffer.getvalue().decode("utf-8")
    svg = svg.replace("<svg", f'<svg width="{size}" height="{size}"', 1)

    try:
        from markupsafe import Markup

        return Markup(svg)
    except ImportError:
        return svg
