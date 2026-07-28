"""Daraja HTTP layer: OAuth token, security credential encryption, JSON posts.

Kept separate from outbound.py so the pure payload/parsing logic stays
importable under the stub-frappe test harness without requests/cryptography.
"""

from base64 import b64encode

import frappe
import requests

TOKEN_TTL_SLACK = 60

_BASE_URLS = {
    "Sandbox": "https://sandbox.safaricom.co.ke",
    "Production": "https://api.safaricom.co.ke",
}


def daraja_base_url(environment):
    try:
        return _BASE_URLS[environment]
    except KeyError:
        frappe.throw(f"Unknown Daraja environment: {environment}")


def encrypt_security_credential(password, certificate_pem):
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import padding

    cert = x509.load_pem_x509_certificate(certificate_pem.encode())
    encrypted = cert.public_key().encrypt(password.encode(), padding.PKCS1v15())
    return b64encode(encrypted).decode()


def get_daraja_token(settings):
    cache_key = f"swdpay_daraja_token_{settings.environment}"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    base = daraja_base_url(settings.environment)
    response = requests.get(
        f"{base}/oauth/v1/generate?grant_type=client_credentials",
        auth=(settings.get_password("consumer_key"), settings.get_password("consumer_secret")),
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    token = data["access_token"]
    ttl = max(int(float(data.get("expires_in", 3599))) - TOKEN_TTL_SLACK, TOKEN_TTL_SLACK)
    frappe.cache().set_value(cache_key, token, expires_in_sec=ttl)
    return token


def daraja_post(settings, path, body):
    base = daraja_base_url(settings.environment)
    token = get_daraja_token(settings)
    response = requests.post(
        f"{base}{path}", json=body,
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    response.raise_for_status()
    return response.json()
