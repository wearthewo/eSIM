"""app/mail.py (Resend over urllib, fake mailer, deliver) and app/emails.py (content).

No network: ``urllib.request.urlopen`` is replaced. All addresses, keys and links are
synthetic.
"""

from __future__ import annotations

import io
import json
import logging
import re
import urllib.error
import urllib.request
from email.message import Message as HeaderMap

import pytest

from app import emails, mail
from app.mail import FakeMailer, MailError, Message, ResendMailer, deliver, mailer_from
from app.settings import Secret, Settings

API_KEY = "re_synthetic_test_key_0123456789abcdef"
SENDER = "eSIM Sheet <no-reply@mail.example.invalid>"
TO = "recipient.person@org.test"
LINK = "https://esim.example.invalid/invite/" + "A" * 43
BASE = "https://esim.example.invalid"


class _Reply(io.BytesIO):
    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@pytest.fixture
def urlopen(monkeypatch):
    """Replace urlopen; ``calls`` gets ``(request, timeout)``; set ``reply`` / ``error``."""

    class Fake:
        calls: list[tuple[urllib.request.Request, float]] = []
        reply: bytes = b'{"id": "49a3999c-0ce1-4ea6-ab68-afcd6dc2e794"}'
        error: BaseException | None = None

        def __call__(self, request, timeout):
            self.calls.append((request, timeout))
            if self.error is not None:
                raise self.error
            return _Reply(self.reply)

    fake = Fake()
    fake.calls = []
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return fake


def _send(**kw: str) -> None:
    ResendMailer(Secret(API_KEY), SENDER).send(
        kw.get("to", TO), "Subject (test)", f"text {LINK}", f"<p>html {LINK}</p>"
    )


# --- ResendMailer -------------------------------------------------------------------------


def test_resend_request(urlopen) -> None:
    _send()
    [(request, timeout)] = urlopen.calls
    assert timeout == 10
    assert request.full_url == "https://api.resend.com/emails"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {API_KEY}"
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("User-agent") == "esim-sheet/0.1"
    assert json.loads(request.data) == {
        "from": SENDER,
        "to": [TO],
        "subject": "Subject (test)",
        "text": f"text {LINK}",
        "html": f"<p>html {LINK}</p>",
    }


def test_resend_request_has_no_reply_to_unless_set(urlopen) -> None:
    _send()
    [(request, _)] = urlopen.calls
    assert "reply_to" not in json.loads(request.data)


def test_resend_request_with_reply_to(urlopen) -> None:
    """EMAIL_REPLY_TO goes into every request (via mailer_from, as the app builds it)."""
    settings = Settings(
        email_backend="resend",
        resend_api_key=Secret(API_KEY),
        email_from=SENDER,
        email_reply_to="eSIM Sheet Support <support@example.invalid>",
    )
    mailer = mailer_from(settings)
    for n in range(2):
        mailer.send(f"person{n}@org.test", "Subject (test)", "text", "<p>html</p>")
    bodies = [json.loads(request.data) for request, _ in urlopen.calls]
    assert bodies == [
        {
            "from": SENDER,
            "to": [f"person{n}@org.test"],
            "subject": "Subject (test)",
            "text": "text",
            "html": "<p>html</p>",
            "reply_to": "eSIM Sheet Support <support@example.invalid>",
        }
        for n in range(2)
    ]


def _http_error(code: int) -> urllib.error.HTTPError:
    body = json.dumps({"statusCode": code, "message": f"bad recipient {TO}"}).encode()
    return urllib.error.HTTPError(
        "https://api.resend.com/emails", code, f"error for {TO}", HeaderMap(), io.BytesIO(body)
    )


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (_http_error(422), "HTTP 422"),
        (_http_error(403), "HTTP 403"),
        (_http_error(500), "HTTP 500"),
        (urllib.error.URLError(f"cannot reach {TO}"), "URLError"),
        (TimeoutError("timed out"), "TimeoutError"),
        (ConnectionResetError(), "ConnectionResetError"),
    ],
    ids=["422", "403", "500", "urlerror", "timeout", "reset"],
)
def test_resend_failures_become_safe_mail_errors(urlopen, error, reason: str) -> None:
    urlopen.error = error
    with pytest.raises(MailError) as info:
        _send()
    assert info.value.reason == reason
    assert str(info.value) == reason
    assert info.value.__cause__ is None and info.value.__suppress_context__  # no chained text


@pytest.mark.parametrize("reply", [b"not json", b"{}", b'{"id": ""}', b"[1]"])
def test_resend_unexpected_reply(urlopen, reply: bytes) -> None:
    urlopen.reply = reply
    with pytest.raises(MailError):
        _send()


def test_nothing_secret_in_reprs() -> None:
    assert API_KEY not in repr(ResendMailer(Secret(API_KEY), SENDER))
    message = Message(TO, "Subject (test)", LINK, LINK)
    assert TO not in repr(message) and LINK not in repr(message)
    audit = mail.AuditContext(target_email=TO)
    assert TO not in repr(audit)


def test_mailer_from_settings() -> None:
    assert mailer_from(Settings()) is None
    resend = Settings(email_backend="resend", resend_api_key=Secret(API_KEY), email_from=SENDER)
    assert isinstance(mailer_from(resend), ResendMailer)


# --- deliver ------------------------------------------------------------------------------


def test_deliver_sends() -> None:
    outbox = FakeMailer()
    message = emails.reset(TO, link=LINK, hours=1, base_url=BASE, requested=True)
    assert deliver(outbox, message, sessions=None, audit=mail.AuditContext()) is True
    assert outbox.outbox == [message]


def test_deliver_failure_is_logged_without_secrets(caplog, urlopen) -> None:
    caplog.set_level(logging.DEBUG)
    urlopen.error = _http_error(422)
    message = emails.invite(
        TO, link=LINK, org_name="Example Org (test)", role="owner", base_url=BASE
    )
    mailer = ResendMailer(Secret(API_KEY), SENDER)
    assert deliver(mailer, message, sessions=None, audit=mail.AuditContext()) is False
    assert "email not sent (HTTP 422)" in caplog.text
    logged = "\n".join(f"{r.getMessage()} {r.args!s} {r.exc_text or ''}" for r in caplog.records)
    for secret in (API_KEY, TO, LINK, "A" * 43, message.subject, "Example Org"):
        assert secret not in logged


def test_deliver_survives_a_broken_backend(caplog) -> None:
    class Broken:
        def send(self, *args: str) -> None:
            raise RuntimeError(f"bug with {TO}")

    message = emails.reset(TO, link=LINK, hours=48, base_url=BASE, requested=False)
    assert deliver(Broken(), message, sessions=None, audit=mail.AuditContext()) is False
    assert "email not sent (RuntimeError)" in caplog.text
    assert TO not in caplog.text


# --- Content ------------------------------------------------------------------------------

URL = re.compile(r"https?://[^\s<>\"']+")


def _all_messages() -> list[Message]:
    return [
        emails.invite(TO, link=LINK, org_name="Example Org (test)", role="member", base_url=BASE),
        emails.reset(TO, link=LINK, hours=1, base_url=BASE, requested=True),
        emails.reset(TO, link=LINK, hours=48, base_url=BASE, requested=False),
        emails.access_request(
            TO,
            company_name="Example Telecom (test)",
            volume_label="1,000 to 10,000",
            review_url=f"{BASE}/admin/requests",
            base_url=BASE,
        ),
        emails.test_message(TO, base_url=BASE),
    ]


@pytest.mark.parametrize("message", _all_messages(), ids=lambda m: m.subject)
def test_every_email_is_plain_with_a_footer(message: Message) -> None:
    footer = f"eSIM Sheet · {BASE}"
    assert message.text.rstrip().endswith(footer)
    assert footer in message.html
    assert message.subject.startswith(("Your invitation", "Reset your", "New access", "eSIM"))
    for forbidden in ("<img", "src=", "<script", "<link", "url(", "LPA:", "<style"):
        assert forbidden not in message.html.lower()
    # Only our own links: the one-time link, the admin page and the footer's base URL.
    for url in URL.findall(message.text) + URL.findall(message.html):
        assert url.startswith(BASE), url


def test_invite_and_reset_say_the_right_things() -> None:
    invite = emails.invite(
        TO, link=LINK, org_name="Example Org (test)", role="owner", base_url=BASE
    )
    assert "join Example Org (test) on eSIM Sheet as an owner" in invite.text
    assert LINK in invite.text and f'href="{LINK}"' in invite.html
    assert "expires in 48 hours" in invite.text
    requested = emails.reset(TO, link=LINK, hours=1, base_url=BASE, requested=True)
    assert "expires in 1 hour." in requested.text and "asked to reset" in requested.text
    by_admin = emails.reset(TO, link=LINK, hours=48, base_url=BASE, requested=False)
    assert "expires in 48 hours" in by_admin.text and "administrator" in by_admin.text


def test_user_text_is_escaped_and_never_in_the_subject() -> None:
    evil = '<b onmouseover="x">Evil & Co</b>'
    invite = emails.invite(TO, link=LINK, org_name=evil, role="member", base_url=BASE)
    request = emails.access_request(
        TO, company_name=evil, volume_label="Over 100,000", review_url=BASE, base_url=BASE
    )
    for message in (invite, request):
        assert evil not in message.html
        assert "&lt;b onmouseover=&quot;x&quot;&gt;Evil &amp; Co&lt;/b&gt;" in message.html
        assert "Evil" not in message.subject


def test_access_request_email_has_company_and_band_only() -> None:
    message = emails.access_request(
        TO,
        company_name="Example Telecom (test)",
        volume_label="1,000 to 10,000",
        review_url=f"{BASE}/admin/requests",
        base_url=BASE,
    )
    assert "Company: Example Telecom (test)" in message.text
    assert "Expected monthly volume: 1,000 to 10,000" in message.text
    assert "@" not in message.text  # no contact email


# --- scripts/send_test_email.py -------------------------------------------------------------


@pytest.fixture
def script(monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "send_test_email.py"
    spec = importlib.util.spec_from_file_location("send_test_email", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(script, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    return script.main(list(argv), out, err), out.getvalue(), err.getvalue()


def test_script_refuses_without_email(script, monkeypatch) -> None:
    monkeypatch.setattr(script, "get_settings", lambda: Settings())
    code, out, err = _run(script, TO)
    assert code == 1 and out == "" and "EMAIL_BACKEND is none" in err
    assert _run(script)[0] == 2 and _run(script, "not-an-address")[0] == 2


def test_script_sends_one_test_message(script, monkeypatch) -> None:
    settings = Settings(
        email_backend="resend",
        resend_api_key=Secret(API_KEY),
        email_from=SENDER,
        public_base_url=BASE,
    )
    fake = FakeMailer()
    monkeypatch.setattr(script, "get_settings", lambda: settings)
    monkeypatch.setattr(script, "mailer_from", lambda s: fake)
    code, out, err = _run(script, TO)
    assert code == 0 and out.startswith("sent") and err == ""
    [message] = fake.outbox
    assert message.to == TO and message.subject == "eSIM Sheet test email"
    assert API_KEY not in out

    fake.fail = True
    code, out, err = _run(script, TO)
    assert code == 1 and err == "not sent (fake outage)\n"
