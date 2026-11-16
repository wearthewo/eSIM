"""Password policy and argon2id hashing. All passwords are synthetic."""

from __future__ import annotations

import threading
import time

import pytest
from argon2 import PasswordHasher

from app.auth import passwords
from app.auth.passwords import (
    MSG_IS_EMAIL,
    MSG_TOO_LONG,
    MSG_TOO_SHORT,
    hash_password,
    password_problem,
    verify_dummy,
    verify_password,
)

EMAIL = "someone.long@org.test"
PASSWORD = "correct horse battery staple"


@pytest.mark.parametrize(
    ("password", "problem"),
    [
        ("x" * 11, MSG_TOO_SHORT),
        ("", MSG_TOO_SHORT),
        ("x" * 12, None),
        ("x" * 128, None),
        ("x" * 129, MSG_TOO_LONG),
        (EMAIL, MSG_IS_EMAIL),
        (EMAIL.upper(), MSG_IS_EMAIL),
        (f" {EMAIL} ", MSG_IS_EMAIL),
        (EMAIL + "!", None),
    ],
)
def test_password_policy(password: str, problem: str | None) -> None:
    assert password_problem(password, EMAIL) == problem


def test_hash_is_argon2id_with_library_defaults() -> None:
    hashed = hash_password(PASSWORD)
    assert hashed.startswith("$argon2id$")
    defaults = PasswordHasher()
    assert f"m={defaults.memory_cost},t={defaults.time_cost},p={defaults.parallelism}" in hashed
    assert PASSWORD not in hashed


def test_verify() -> None:
    hashed = hash_password(PASSWORD)
    assert verify_password(hashed, PASSWORD) == (True, False)
    assert verify_password(hashed, PASSWORD + "x") == (False, False)
    assert verify_password("not a hash", PASSWORD) == (False, False)


def test_rehash_needed_for_old_parameters() -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash(PASSWORD)
    assert verify_password(weak, PASSWORD) == (True, True)
    assert verify_password(weak, "wrong password!!") == (False, False)


class _CountingHasher:
    def __init__(self, real: PasswordHasher) -> None:
        self.real = real
        self.verified: list[str] = []

    def hash(self, password: str) -> str:
        return self.real.hash(password)

    def verify(self, hashed: str, password: str) -> bool:
        self.verified.append(hashed)
        return self.real.verify(hashed, password)

    def check_needs_rehash(self, hashed: str) -> bool:
        return self.real.check_needs_rehash(hashed)


def test_verify_dummy_runs_a_real_verify(monkeypatch) -> None:
    counting = _CountingHasher(passwords._hasher)
    monkeypatch.setattr(passwords, "_hasher", counting)
    verify_dummy(PASSWORD)
    assert len(counting.verified) == 1 and counting.verified[0].startswith("$argon2id$")


def test_at_most_two_hashes_at_once(monkeypatch) -> None:
    assert passwords.MAX_CONCURRENT_HASHES == 2
    lock = threading.Lock()
    state = {"running": 0, "peak": 0}

    class SlowHasher:
        def hash(self, password: str) -> str:
            with lock:
                state["running"] += 1
                state["peak"] = max(state["peak"], state["running"])
            time.sleep(0.05)
            with lock:
                state["running"] -= 1
            return "$argon2id$fake"

    monkeypatch.setattr(passwords, "_hasher", SlowHasher())
    threads = [threading.Thread(target=hash_password, args=(PASSWORD,)) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["peak"] == 2
