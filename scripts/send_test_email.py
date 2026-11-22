"""Send one test email with the configured backend, to check delivery and spam placement.

Usage (from the repo root):
    .venv\\Scripts\\python.exe scripts\\send_test_email.py you@gmail.com
On Render (service Shell):
    python scripts/send_test_email.py you@outlook.com

It uses the app's settings (``EMAIL_BACKEND``, ``RESEND_API_KEY``, ``EMAIL_FROM``,
``PUBLIC_BASE_URL``; locally from ``.env``) and refuses to run with
``EMAIL_BACKEND=none``. The message is ``app.emails.test_message``: no links but the
footer, no tracking. It prints ``sent`` or ``not sent (<reason>)`` and exits 0 or 1;
it never prints the API key. Needs no database.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import emails  # noqa: E402
from app.mail import MailError, mailer_from, send_message  # noqa: E402
from app.settings import SettingsError, get_settings  # noqa: E402

USAGE = "usage: send_test_email.py <address>"


def main(argv: list[str], out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    if len(argv) != 1 or "@" not in argv[0]:
        err.write(f"{USAGE}\n")
        return 2
    try:
        settings = get_settings()
    except SettingsError as exc:
        err.write(f"error: {exc}\n")
        return 1
    mailer = mailer_from(settings)
    if mailer is None:
        err.write("error: EMAIL_BACKEND is none; set it to resend first\n")
        return 1
    message = emails.test_message(argv[0].strip(), base_url=settings.public_base_url)
    try:
        send_message(mailer, message)
    except MailError as exc:
        err.write(f"not sent ({exc.reason})\n")
        return 1
    out.write(f"sent (backend {settings.email_backend}, from {settings.email_from})\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
