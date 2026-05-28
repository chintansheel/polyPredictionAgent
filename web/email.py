"""Transactional email via Brevo."""

from __future__ import annotations

import logging
import time
import uuid

import httpx

from web.config import BREVO_API_KEY, BREVO_FROM_EMAIL, BREVO_FROM_NAME

logger = logging.getLogger(__name__)

_BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def _mask_email(email: str) -> str:
    try:
        local, domain = email.split("@", 1)
    except ValueError:
        return "***"
    if not local:
        return f"***@{domain}"
    if len(local) <= 2:
        return f"{local[0]}***@{domain}"
    return f"{local[:2]}***@{domain}"


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

    request_id = str(uuid.uuid4())
    started = time.monotonic()
    logger.warning(
        "password_reset_email_send_start request_id=%s to=%s from=%s",
        request_id,
        _mask_email(to_email),
        BREVO_FROM_EMAIL,
    )

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            _BREVO_URL,
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            json=payload,
        )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code >= 400:
        logger.error(
            "password_reset_email_send_failed request_id=%s status=%s elapsed_ms=%s body=%s",
            request_id,
            resp.status_code,
            elapsed_ms,
            resp.text[:1000],
        )
        raise RuntimeError(f"Brevo API error: {resp.status_code}")
    message_id = None
    try:
        message_id = resp.json().get("messageId")
    except Exception:
        message_id = None
    logger.warning(
        "password_reset_email_send_ok request_id=%s status=%s elapsed_ms=%s message_id=%s",
        request_id,
        resp.status_code,
        elapsed_ms,
        message_id,
    )
