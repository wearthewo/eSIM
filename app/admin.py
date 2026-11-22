"""Admin command line: ``python -m app.admin <command> ...``

Commands:

  create-superadmin --email X                       prints a one-time set-password link
  create-org --name X                               prints the new organization's id
  invite --org-id N --email X --role owner|member   prints a one-time invite link
  reset-link --email X                              prints a one-time set-password link
  disable-user --email X                            also ends the user's sessions
  enable-user --email X
  reset-2fa --email X                               switches two-factor authentication off
                                                    and ends the user's sessions
  purge                                             deletes rows past retention; prints counts
  bootstrap                                         creates the superadmin named by
                                                    BOOTSTRAP_SUPERADMIN_EMAIL if none exists

``bootstrap`` runs on every start of the deployed app (render.yaml). If the variable is
set and there is no superadmin yet, it creates one and prints a single line starting with
``BOOTSTRAP_LABEL`` and ending with the one-time set-password link. Otherwise it prints
nothing and exits 0. The new superadmin must set up two-factor authentication at the
first login, before reaching any other page.

Links are ``PUBLIC_BASE_URL/invite/<token>`` and expire after 48 hours. Each link is
printed once and only its SHA-256 is stored, so a lost link cannot be shown again:
create a new one. Error messages never include tokens or the database URL.

``reset-2fa`` is the break-glass for a user who lost both their phone and their recovery
codes: two-factor authentication is switched off (a superadmin must set it up again at
the next login, before reaching any other page) and an ``mfa_reset`` audit row is added.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import TextIO

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import accounts, mfa, retention
from app.auth.accounts import AdminError
from app.db.models import Role
from app.settings import Settings, SettingsError, get_settings

SessionScope = Callable[[], AbstractContextManager[Session]]

BOOTSTRAP_LABEL = (
    "BOOTSTRAP: one-time superadmin set-password link "
    f"(expires in {accounts.LINK_TTL // timedelta(hours=1)} h):"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.admin", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    cmd = commands.add_parser("create-superadmin", help="create the superadmin; prints a link")
    cmd.add_argument("--email", required=True)
    cmd = commands.add_parser("create-org", help="create an organization; prints its id")
    cmd.add_argument("--name", required=True)
    cmd = commands.add_parser("invite", help="invite a user to an organization; prints a link")
    cmd.add_argument("--org-id", required=True, type=int)
    cmd.add_argument("--email", required=True)
    cmd.add_argument("--role", required=True, choices=[r.value for r in Role])
    cmd = commands.add_parser("reset-link", help="prints a set-password link for a user")
    cmd.add_argument("--email", required=True)
    cmd = commands.add_parser("disable-user", help="disable a user and end their sessions")
    cmd.add_argument("--email", required=True)
    cmd = commands.add_parser("enable-user", help="enable a user again")
    cmd.add_argument("--email", required=True)
    cmd = commands.add_parser(
        "reset-2fa", help="switch two-factor authentication off; ends the user's sessions"
    )
    cmd.add_argument("--email", required=True)
    commands.add_parser("purge", help="delete rows past their retention; prints counts")
    commands.add_parser(
        "bootstrap", help="create the BOOTSTRAP_SUPERADMIN_EMAIL superadmin if none exists"
    )
    return parser


def link(settings: Settings, token: str) -> str:
    return f"{settings.public_base_url}/invite/{token}"


def _run(args: argparse.Namespace, db: Session, settings: Settings, now: datetime) -> str:
    """Do the command; returns what to print."""
    match args.command:
        case "create-superadmin":
            return link(settings, accounts.create_superadmin(db, args.email, now=now))
        case "create-org":
            return str(accounts.create_org(db, args.name))
        case "invite":
            role = Role(args.role)
            return link(settings, accounts.invite(db, args.org_id, args.email, role, now=now))
        case "reset-link":
            return link(settings, accounts.reset_link(db, args.email, now=now))
        case "disable-user":
            accounts.set_user_disabled(db, args.email, True)
            return "disabled; their sessions have ended"
        case "enable-user":
            accounts.set_user_disabled(db, args.email, False)
            return "enabled"
        case "reset-2fa":
            mfa.reset(db, args.email, now=now)
            return "two-factor authentication switched off; their sessions have ended"
        case "purge":
            counts = retention.purge(db, now=now)
            return "\n".join(f"{table}: {n}" for table, n in counts.items())
        case "bootstrap":
            token = accounts.bootstrap_superadmin(db, settings.bootstrap_superadmin_email, now=now)
            return "" if token is None else f"{BOOTSTRAP_LABEL} {link(settings, token)}"
    raise AdminError("unknown command")  # argparse rejects it first


def main(
    argv: Sequence[str] | None = None,
    *,
    session_scope: SessionScope | None = None,
    settings: Settings | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = settings or get_settings()
        if session_scope is None:
            from app.db.engine import session_scope as default_scope

            session_scope = default_scope
        with session_scope() as db:
            output = _run(args, db, settings, datetime.now(UTC))
    except (AdminError, SettingsError) as exc:
        err.write(f"error: {exc}\n")
        return 1
    except SQLAlchemyError as exc:
        # The engine hides bound parameters; show only the error class.
        err.write(f"error: database error ({type(exc).__name__})\n")
        return 1
    if output:  # ``bootstrap`` with nothing to do prints nothing at all
        out.write(f"{output}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
