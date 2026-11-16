"""Admin command line: ``python -m app.admin <command> ...``

Commands:

  create-superadmin --email X                       prints a one-time set-password link
  create-org --name X                               prints the new organization's id
  invite --org-id N --email X --role owner|member   prints a one-time invite link
  reset-link --email X                              prints a one-time set-password link
  disable-user --email X                            also ends the user's sessions
  enable-user --email X

Links are ``PUBLIC_BASE_URL/invite/<token>`` and expire after 48 hours. Each link is
printed once and only its SHA-256 is stored, so a lost link cannot be shown again:
create a new one. Error messages never include tokens or the database URL.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import TextIO

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.auth import accounts
from app.auth.accounts import AdminError
from app.db.models import Role
from app.settings import Settings, SettingsError, get_settings

SessionScope = Callable[[], AbstractContextManager[Session]]


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
    out.write(f"{output}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
