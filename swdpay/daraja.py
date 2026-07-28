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
    from hashlib import sha1

    # Key the cache on the consumer key too: a credential change (or a deploy)
    # must never reuse a token minted for a different Daraja app.
    consumer_key = settings.get_password("consumer_key")
    key_hash = sha1((consumer_key or "").encode()).hexdigest()[:8]
    cache_key = f"swdpay_daraja_token_{settings.environment}_{key_hash}"
    cached = frappe.cache().get_value(cache_key)
    if cached:
        return cached

    base = daraja_base_url(settings.environment)
    response = requests.get(
        f"{base}/oauth/v1/generate?grant_type=client_credentials",
        auth=(consumer_key, settings.get_password("consumer_secret")),
        timeout=30,
    )
    _raise_with_body(response)
    data = response.json()
    token = data["access_token"]
    ttl = max(int(float(data.get("expires_in", 3599))) - TOKEN_TTL_SLACK, TOKEN_TTL_SLACK)
    frappe.cache().set_value(cache_key, token, expires_in_sec=ttl)
    return token


def _raise_with_body(response):
    """raise_for_status, but keep Daraja's error body — it names the actual
    problem (invalid token, product not provisioned, bad initiator), which a
    bare HTTP status hides. Response bodies carry no secrets."""
    if response.status_code >= 400:
        detail = (response.text or "").strip()[:500]
        raise requests.HTTPError(
            f"{response.status_code} error for {response.url}: {detail or 'no response body'}",
            response=response,
        )


def daraja_post(settings, path, body):
    base = daraja_base_url(settings.environment)
    token = get_daraja_token(settings)
    response = requests.post(
        f"{base}{path}", json=body,
        headers={"Authorization": f"Bearer {token}"}, timeout=30,
    )
    _raise_with_body(response)
    return response.json()
