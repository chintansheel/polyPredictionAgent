"""Transactional email via Brevo."""

from __future__ import annotations

import logging

import httpx

from web.config import BREVO_API_KEY, BREVO_FROM_EMAIL, BREVO_FROM_NAME

logger = logging.getLogger(__name__)

_BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def send_password_reset_email(*, to_email: str, to_name: str, reset_url: str) -> None:
    if not BREVO_API_KEY:
        raise RuntimeError("BREVO_API_KEY is not configured")
    if not BREVO_FROM_EMAIL:
        raise RuntimeError("BREVO_FROM_EMAIL is not configured")

    subject = "Reset your Foretell password"
    text = (
        f"Hi {to_name},\n\n"
        "We received a request to reset your Foretell password.\n\n"
        f"Reset your password: {reset_url}\n\n"
        "This link expires in one hour. If you did not request this, you can ignore this email.\n"
    )
    html = (
        f"<p>Hi {to_name},</p>"
        "<p>We received a request to reset your Foretell password.</p>"
        f'<p><a href="{reset_url}">Reset your password</a></p>'
        "<p>This link expires in one hour. If you did not request this, you can ignore this email.</p>"
    )

    payload = {
        "sender": {"name": BREVO_FROM_NAME, "email": BREVO_FROM_EMAIL},
        "to": [{"email": to_email, "name": to_name}],
        "subject": subject,
        "textContent": text,
        "htmlContent": html,
    }

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            _BREVO_URL,
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            json=payload,
        )
    if resp.status_code >= 400:
        logger.error("Brevo send failed: %s %s", resp.status_code, resp.text)
        raise RuntimeError(f"Brevo API error: {resp.status_code}")
