"""Transactional email for signup verification.

Matches Mail Orbyt: Resend when `RESEND_API_KEY` is set, otherwise log to the
console so local testing still works. Only the SHA-256 of a verification token
is stored; the raw token exists in the email (and the URL the user clicks).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from . import config

logger = logging.getLogger("leadorbyt.email")

RESEND_API_URL = "https://api.resend.com/emails"
EMAIL_VERIFICATION_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class TransactionalEmail:
    to: str
    subject: str
    text: str


class EmailSender(Protocol):
    def send(self, email: TransactionalEmail) -> None: ...


class ConsoleEmailSender:
    """Development fallback when Resend is not configured."""

    def send(self, email: TransactionalEmail) -> None:
        logger.info("[email:dev] to=%s subject=%s\n%s", email.to, email.subject, email.text)


def resend_error_message(cause: object) -> str:
    if isinstance(cause, Exception):
        return str(cause)
    if isinstance(cause, dict) and cause.get("message"):
        return str(cause["message"])
    return str(cause)


class ResendSendError(Exception):
    def __init__(self, cause: object):
        super().__init__(f"Resend request failed: {resend_error_message(cause)}")
        self.cause = cause


class ResendEmailSender:
    def __init__(self, api_key: str, from_address: str):
        self.api_key = api_key
        self.from_address = from_address

    def send(self, email: TransactionalEmail) -> None:
        try:
            response = httpx.post(
                RESEND_API_URL,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "from": self.from_address,
                    "to": [email.to],
                    "subject": email.subject,
                    "text": email.text,
                },
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            raise ResendSendError(exc) from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {"message": response.text}
            raise ResendSendError(payload)


def verification_email(*, to: str, app_url: str, raw_token: str) -> TransactionalEmail:
    link = f"{app_url}/verify-email?token={raw_token}"
    return TransactionalEmail(
        to=to,
        subject="Verify your Lead Orbyt email address",
        text=(
            f"Confirm your email address by visiting:\n{link}\n\n"
            "This link expires in 24 hours. If you did not request access to "
            "Lead Orbyt, you can ignore this email."
        ),
    )


def get_email_sender() -> EmailSender:
    if config.RESEND_API_KEY:
        return ResendEmailSender(config.RESEND_API_KEY, config.EMAIL_FROM_ADDRESS)
    return ConsoleEmailSender()
