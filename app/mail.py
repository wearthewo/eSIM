"""Sending email: the Resend HTTP API (standard library only) and a fake for tests.

Rules (CLAUDE.md):

- Never log the API key, recipients, subjects, bodies or links. A failure is logged as
  ``email not sent (<reason>)`` where the reason is an HTTP status or an exception class.
  The error body from Resend is never read into a message (it may echo the address).
- Send only after the database transaction that made the link has committed, so an
  email never carries a link that does not exist.
- A failure never breaks the action: ``deliver`` returns False and writes an
  ``email_failed`` audit row (ids only, never the address); the page then shows the
  link to copy instead.

``EMAIL_BACKEND=none`` means no mailer at all (``mailer_from`` returns ``None``): every
page shows its links exactly as before email existed.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from app.auth import accounts
from app.auth.accounts import IpPrefix
from app.db.models import AuditOutcome
from app.settings import Secret, Settings

RESEND_URL = "https://api.resend.com/emails"
TIMEOUT_SECONDS = 10
# Resend rejects requests without a User-Agent (403, error 1010). No contact details.
USER_AGENT = "esim-sheet/0.1"
EMAIL_FAILED = "email_failed"  # audit_log.action

logger = logging.getLogger(__name__)


class MailError(Exception):
    """The email was not sent. ``reason`` is safe to log (no address, key or content)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Message:
    to: str
    subject: str
    text: str
    html: str

    def __repr__(self) -> str:  # never the address, subject or body
        return "Message(...)"


class Mailer(Protocol):
    def send(self, to: str, subject: str, text: str, html: str) -> None:
        """Send one email. Raises ``MailError``."""


class ResendMailer:
    """POST https://api.resend.com/emails with a 10 s timeout. ``reply_to``
    (``EMAIL_REPLY_TO``), when set, goes into every request."""

    __slots__ = ("_api_key", "_reply_to", "_sender")

    def __init__(self, api_key: Secret, sender: str, *, reply_to: str | None = None) -> None:
        self._api_key = api_key
        self._sender = sender
        self._reply_to = reply_to

    def __repr__(self) -> str:
        return "ResendMailer(...)"

    def send(self, to: str, subject: str, text: str, html: str) -> None:
        payload = {"from": self._sender, "to": [to], "subject": subject, "text": text, "html": html}
        if self._reply_to is not None:
            payload["reply_to"] = self._reply_to
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310  (a fixed https URL)
            RESEND_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key.reveal()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
                reply = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            exc.close()  # the body is never read: it may echo the address
            raise MailError(f"HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise MailError(type(exc).__name__) from None
        if not isinstance(reply, dict) or not reply.get("id"):
            raise MailError("unexpected reply")


@dataclass
class FakeMailer:
    """In-memory mailer for tests: ``outbox`` holds every sent ``Message``. With
    ``fail=True`` every send raises ``MailError`` (and nothing is added)."""

    outbox: list[Message] = field(default_factory=list)
    fail: bool = False

    def send(self, to: str, subject: str, text: str, html: str) -> None:
        if self.fail:
            raise MailError("fake outage")
        self.outbox.append(Message(to, subject, text, html))


def mailer_from(settings: Settings) -> Mailer | None:
    """The configured mailer; ``None`` with ``EMAIL_BACKEND=none``."""
    if settings.email_backend == "resend":
        if settings.resend_api_key is None or settings.email_from is None:
            raise RuntimeError("EMAIL_BACKEND is resend without a key or sender")  # load_settings
        return ResendMailer(
            settings.resend_api_key, settings.email_from, reply_to=settings.email_reply_to
        )
    return None


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Who the email was for, as ids: written to ``email_failed`` (never the address).

    ``target_email`` is only used to look up the recipient's user id; it is not written
    anywhere and not shown in ``repr``."""

    user_id: int | None = None  # who acted
    organization_id: int | None = None
    target_user_id: int | None = None  # the recipient, if a user
    target_email: str | None = field(default=None, repr=False)
    ip: IpPrefix | None = None


def send_message(mailer: Mailer, message: Message) -> None:
    mailer.send(message.to, message.subject, message.text, message.html)


def deliver(
    mailer: Mailer,
    message: Message,
    *,
    sessions: sessionmaker[Session] | None,
    audit: AuditContext,
) -> bool:
    """Send ``message``; True if sent. On failure: log the reason, write ``email_failed``
    in its own transaction, return False. Never raises (call it after the commit)."""
    try:
        send_message(mailer, message)
        return True
    except MailError as exc:
        reason = exc.reason
    except Exception as exc:  # a bug in a backend must not break the page either
        reason = type(exc).__name__
    logger.warning("email not sent (%s)", reason)
    if sessions is not None:
        try:
            _audit_failure(sessions, audit)
        except Exception as exc:  # never the message: it could include the database URL
            logger.warning("email_failed audit row not written (%s)", type(exc).__name__)
    return False


def _audit_failure(sessions: sessionmaker[Session], audit: AuditContext) -> None:
    with sessions.begin() as db:
        target = audit.target_user_id
        if target is None and audit.target_email is not None:
            user = accounts.user_by_email(db, audit.target_email)
            target = user.id if user is not None else None
        accounts.audit(
            db,
            EMAIL_FAILED,
            AuditOutcome.FAILURE,
            now=datetime.now(UTC),
            user_id=audit.user_id,
            organization_id=audit.organization_id,
            target_user_id=target,
            ip=audit.ip,
        )
