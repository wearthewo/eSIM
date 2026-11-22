"""What the app's emails say: plain text plus simple HTML.

Rules: no images, no remote resources, no tracking; never activation codes,
confirmation codes or customer data (customer names or emails from uploads). Every
value put into the HTML is escaped. Subjects are fixed text: nothing a user typed (a
company name, say) is ever put in a subject. Every email ends with the footer line
``eSIM Sheet · <PUBLIC_BASE_URL>``.
"""

from __future__ import annotations

from html import escape

from app.core.numbers import thousands
from app.mail import Message

SITE_NAME = "eSIM Sheet"

_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{title}</title></head>
<body style="margin:0;padding:24px;font-family:Arial,Helvetica,sans-serif;font-size:15px;\
line-height:1.5;color:#1a1a1a;background:#ffffff">
<div style="max-width:560px">
{body}
<hr style="border:none;border-top:1px solid #dddddd;margin:24px 0 12px">
<p style="font-size:13px;color:#666666">{footer}</p>
</div>
</body>
</html>
"""


def _footer(base_url: str) -> str:
    return f"{SITE_NAME} · {base_url}"


def _message(
    to: str, subject: str, base_url: str, paragraphs: list[str | tuple[str, str]]
) -> Message:
    """``paragraphs``: plain strings, or ``(label, url)`` for a link (shown as a button-ish
    link in HTML and as the bare URL on its own line in the text part)."""
    text_parts: list[str] = []
    html_parts: list[str] = []
    for p in paragraphs:
        if isinstance(p, tuple):
            label, url = p
            text_parts.append(url)
            html_parts.append(
                f'<p><a href="{escape(url)}" style="color:#1a4d8f;font-weight:bold">'
                f"{escape(label)}</a></p>\n"
                f'<p style="font-size:13px;color:#666666;word-break:break-all">{escape(url)}</p>'
            )
        else:
            text_parts.append(p)
            html_parts.append(f"<p>{escape(p)}</p>")
    footer = _footer(base_url)
    text = "\n\n".join(text_parts) + f"\n\n-- \n{footer}\n"
    html = _HTML.format(title=escape(subject), body="\n".join(html_parts), footer=escape(footer))
    return Message(to=to, subject=subject, text=text, html=html)


def invite(to: str, *, link: str, org_name: str, role: str, base_url: str) -> Message:
    """An invitation to an organization (approve request, admin invite, team invite)."""
    return _message(
        to,
        f"Your invitation to {SITE_NAME}",
        base_url,
        [
            "Hello,",
            f"You have been invited to join {org_name} on {SITE_NAME} as "
            f"{'an owner' if role == 'owner' else 'a member'}. "
            "Open the link below to choose your password.",
            ("Set your password", link),
            "The link works once and expires in 48 hours. "
            "If you did not expect this invitation, you can ignore this email.",
        ],
    )


def reset(to: str, *, link: str, hours: int, base_url: str, requested: bool) -> Message:
    """A set-password link: ``requested`` (the person used "Forgot password?") or made by
    an administrator or the organization's owner."""
    why = (
        "Someone (hopefully you) asked to reset the password for this address."
        if requested
        else "An administrator created a link for you to set a new password."
    )
    unit = "hour" if hours == 1 else "hours"
    return _message(
        to,
        f"Reset your {SITE_NAME} password",
        base_url,
        [
            "Hello,",
            f"{why} Open the link below to choose a new password.",
            ("Set a new password", link),
            f"The link works once and expires in {thousands(hours)} {unit}. "
            "Two-factor authentication stays on if you use it. "
            "If you did not ask for this, you can ignore this email: your password stays "
            "the same.",
        ],
    )


def access_request(
    to: str, *, company_name: str, volume_label: str, review_url: str, base_url: str
) -> Message:
    """Tells the platform administrator about a new access request (company and volume
    band only: no contact name, email or note)."""
    return _message(
        to,
        f"New access request on {SITE_NAME}",
        base_url,
        [
            "A new access request is waiting for review.",
            f"Company: {company_name}",
            f"Expected monthly volume: {volume_label}",
            ("Review access requests", review_url),
        ],
    )


def test_message(to: str, *, base_url: str) -> Message:
    """scripts/send_test_email.py: checks delivery and spam placement."""
    return _message(
        to,
        f"{SITE_NAME} test email",
        base_url,
        [
            "Hello,",
            f"This is a test email from {SITE_NAME}. If it arrived in your inbox (not "
            "spam), email delivery works.",
            "No action is needed.",
        ],
    )
