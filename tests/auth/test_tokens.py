"""Random tokens, digests, CSRF derivation and signed form tokens. Keys are synthetic."""

from __future__ import annotations

import ast
import hashlib
import time
from pathlib import Path

import pytest

from app.auth.tokens import (
    FORM_TOKEN_MAX_AGE_SECONDS,
    check_form_token,
    csrf_for,
    csrf_matches,
    digest,
    is_token_shaped,
    new_token,
    sign_form_token,
)

KEY = b"k" * 40
NONCE = new_token()
AUTH_DIR = Path(__file__).resolve().parents[2] / "app" / "auth"


def test_new_tokens_are_32_random_bytes_urlsafe() -> None:
    tokens = {new_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(len(t) == 43 and is_token_shaped(t) for t in tokens)


@pytest.mark.parametrize("value", ["", "abc", "x" * 42, "x" * 44, "+" * 43, "/" * 43, "=" * 43])
def test_is_token_shaped_rejects(value: str) -> None:
    assert not is_token_shaped(value)


def test_digest_is_sha256() -> None:
    token = new_token()
    assert digest(token) == hashlib.sha256(token.encode()).digest()
    assert len(digest(token)) == 32


def test_csrf_is_derived_per_session() -> None:
    a, b = new_token(), new_token()
    assert csrf_for(a) == csrf_for(a)
    assert csrf_for(a) != csrf_for(b)
    assert csrf_for(a) != a and a not in csrf_for(a)
    stored = digest(csrf_for(a))
    assert csrf_matches(csrf_for(a), stored)
    assert not csrf_matches(csrf_for(b), stored)
    assert not csrf_matches("", stored)
    assert not csrf_matches(None, stored)


def test_form_token_round_trip() -> None:
    token = sign_form_token(KEY, "login", NONCE)
    assert check_form_token(KEY, "login", token, NONCE)


@pytest.mark.parametrize(
    "case", ["purpose", "key", "nonce", "no-cookie", "tampered", "malformed", "empty"]
)
def test_form_token_rejected(case: str) -> None:
    token = sign_form_token(KEY, "login", NONCE)
    args = {"key": KEY, "purpose": "login", "token": token, "cookie_nonce": NONCE}
    match case:
        case "purpose":
            args["purpose"] = "invite:abc"
        case "key":
            args["key"] = b"z" * 40
        case "nonce":
            args["cookie_nonce"] = new_token()
        case "no-cookie":
            args["cookie_nonce"] = None
        case "tampered":
            ts, nonce, mac = token.split(".")
            args["token"] = f"{int(ts) + 1000}.{nonce}.{mac}"
        case "malformed":
            args["token"] = "a.b"
        case "empty":
            args["token"] = ""
    assert not check_form_token(args["key"], args["purpose"], args["token"], args["cookie_nonce"])


def test_form_token_expires() -> None:
    now = time.time()
    old = sign_form_token(KEY, "login", NONCE, now=now - FORM_TOKEN_MAX_AGE_SECONDS - 1)
    fresh = sign_form_token(KEY, "login", NONCE, now=now - FORM_TOKEN_MAX_AGE_SECONDS + 5)
    future = sign_form_token(KEY, "login", NONCE, now=now + 600)
    assert not check_form_token(KEY, "login", old, NONCE, now=now)
    assert check_form_token(KEY, "login", fresh, NONCE, now=now)
    assert not check_form_token(KEY, "login", future, NONCE, now=now)


@pytest.mark.parametrize("path", sorted(AUTH_DIR.glob("*.py")), ids=lambda p: p.name)
def test_auth_package_has_no_web_imports(path: Path) -> None:
    """app.auth is shared with the admin CLI: no FastAPI, Starlette or app.web."""
    names: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    bad = [
        n for n in names if n.split(".")[0] in {"fastapi", "starlette"} or n.startswith("app.web")
    ]
    assert not bad
