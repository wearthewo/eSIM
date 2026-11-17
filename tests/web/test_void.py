"""Voiding a batch: who may, what it removes, what stays, and the lock with record_part.

All data is synthetic.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app import batches
from app.auth.accounts import CurrentUser
from app.core import pipeline
from app.core.parser import parse_table
from app.db.models import Role
from app.localtime import format_athens
from app.web import history, routes
from app.web.auth import CSRF_HEADER
from tests.web.conftest import Accounts, LoggedIn
from tests.web.test_batches import (
    _all_rows_as_text,
    batch_row,
    count,
    execute,
    generate,
    rows_csv,
    validate,
)

NAME = "order.csv"
EIGHT = list(range(1, 9))
CONFIRM = {history.CONFIRM_FIELD: "yes"}


@pytest.fixture
def chunk_of_three(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)


@pytest.fixture
def key(web_settings) -> bytes:
    return web_settings.fingerprint_key.reveal().encode()


class World:
    """Org A (owner + member), org B (owner), a superadmin; each with a logged-in client."""

    def __init__(self, app: FastAPI, accts: Accounts) -> None:
        self.app, self.accts = app, accts
        self.org_a = accts.org("Alpha (test)")
        self.org_b = accts.org("Beta (test)")
        self.owner_a = accts.user("owner@alpha.test", org_id=self.org_a, role=Role.OWNER)
        self.member_a = accts.user("member@alpha.test", org_id=self.org_a, role=Role.MEMBER)
        self.owner_b = accts.user("owner@beta.test", org_id=self.org_b, role=Role.OWNER)
        self.admin = accts.user("admin@platform.test", superadmin=True, role=Role.MEMBER)

    def client(self, user_id: int) -> TestClient:
        return LoggedIn(self.app, user_id, self.accts.session(user_id)).client(
            follow_redirects=False
        )


@pytest.fixture
def world(web_app: FastAPI, accts: Accounts) -> World:
    return World(web_app, accts)


@pytest.fixture
def owner(world: World) -> Iterator[TestClient]:
    with world.client(world.owner_a) as c:
        yield c


def started(client: TestClient, numbers: list[int] = EIGHT, parts_done=(1,)) -> int:
    data = rows_csv(numbers)
    _, batch_id = validate(client, NAME, data)
    assert batch_id is not None
    for part in parts_done:
        response = generate(
            client, batch_id, NAME, data, part=part, parts=pipeline.part_count(len(numbers))
        )
        assert response.status_code == 200
    return batch_id


def void_url(batch_id: int) -> str:
    return f"/history/batches/{batch_id}/void"


# --- The page and the action -------------------------------------------------------------------


def test_owner_voids_a_batch(
    world: World, owner: TestClient, accts: Accounts, chunk_of_three
) -> None:
    voided = started(owner)
    kept = started(owner, [21, 22, 23])
    assert count(accts, "SELECT count(*) FROM code_fingerprints WHERE batch_id = :b", b=voided) == 6

    page = owner.get(void_url(voided))
    assert page.status_code == 200
    assert history.VOID_WARNING in page.text
    assert f'name="{history.CONFIRM_FIELD}" value="yes" required' in page.text
    assert history.CONFIRM_LABEL in page.text and 'name="csrf_token"' in page.text

    response = owner.post(void_url(voided), data=CONFIRM)
    assert response.status_code == 303 and response.headers["location"] == "/history"

    row = batch_row(accts, voided)
    assert row["status"] == "voided" and row["voided_by"] == world.owner_a
    assert row["voided_at"] is not None and row["parts_downloaded"] == 1  # history kept
    assert count(accts, "SELECT count(*) FROM code_fingerprints WHERE batch_id = :b", b=voided) == 0
    assert count(accts, "SELECT count(*) FROM code_fingerprints WHERE batch_id = :b", b=kept) == 6

    audit = accts.sql(
        "SELECT user_id, organization_id, target_batch_id, outcome, target_user_id"
        " FROM audit_log WHERE action = 'batch_voided'"
    )
    assert audit == [(world.owner_a, world.org_a, voided, "success", None)]
    dump = _all_rows_as_text(accts)
    assert "TEST-" not in dump and "LPA:" not in dump  # no codes anywhere

    html = owner.get("/history").text
    voided_at = accts.sql("SELECT voided_at FROM batches WHERE id = :id", id=voided)[0][0]
    assert f"Voided by owner@alpha.test on {format_athens(voided_at)}" in html
    assert f'href="{void_url(voided)}"' not in html  # no button on a voided batch
    assert f'href="{void_url(kept)}">Void batch</a>' in html


def test_superadmin_voids_any_batch(world: World, owner: TestClient, accts: Accounts) -> None:
    batch_id = started(owner, [1, 2])
    with world.client(world.admin) as admin:
        assert admin.get(void_url(batch_id)).status_code == 200
        assert admin.post(void_url(batch_id), data=CONFIRM).status_code == 303
        assert f'href="{void_url(batch_id)}"' not in admin.get("/history").text
    assert batch_row(accts, batch_id)["voided_by"] == world.admin


def test_members_and_other_organizations_get_404(
    world: World, owner: TestClient, accts: Accounts
) -> None:
    batch_id = started(owner, [1, 2])
    _, checked_only = validate(owner, "checked.csv", rows_csv([50, 51]))
    for user_id, target in (
        (world.member_a, batch_id),
        (world.owner_b, batch_id),
        (world.owner_a, batch_id + 1000),  # unknown
        (world.owner_a, checked_only),  # validated: nothing to void
    ):
        with world.client(user_id) as c:
            assert c.get(void_url(target)).status_code == 404
            assert c.post(void_url(target), data=CONFIRM).status_code == 404
    assert batch_row(accts, batch_id)["status"] == "completed"
    assert count(accts, "SELECT count(*) FROM audit_log WHERE action = 'batch_voided'") == 0


def test_members_see_no_void_button(world: World, owner: TestClient) -> None:
    batch_id = started(owner, [1, 2])
    with world.client(world.member_a) as member:
        html = member.get("/history").text
    assert "Void batch" not in html and void_url(batch_id) not in html


@pytest.mark.parametrize("form", [{}, {history.CONFIRM_FIELD: "on"}, {history.CONFIRM_FIELD: ""}])
def test_confirmation_is_required(owner: TestClient, accts: Accounts, form: dict) -> None:
    batch_id = started(owner, [1, 2])
    # As a browser sends it: the CSRF field always, the checkbox only when ticked.
    response = owner.post(
        void_url(batch_id), data={"csrf_token": owner.headers[CSRF_HEADER], **form}
    )
    assert response.status_code == 400
    assert history.MSG_CONFIRM_REQUIRED in response.text
    assert batch_row(accts, batch_id)["status"] == "completed"
    assert count(accts, "SELECT count(*) FROM code_fingerprints") == 4


def test_void_needs_the_csrf_token(owner: TestClient, accts: Accounts) -> None:
    batch_id = started(owner, [1, 2])
    del owner.headers[CSRF_HEADER]
    assert owner.post(void_url(batch_id), data=CONFIRM).status_code == 403
    assert owner.post(void_url(batch_id), data={**CONFIRM, "csrf_token": "x"}).status_code == 403
    assert batch_row(accts, batch_id)["status"] == "completed"


def test_already_voided_is_409(owner: TestClient) -> None:
    batch_id = started(owner, [1, 2])
    assert owner.post(void_url(batch_id), data=CONFIRM).status_code == 303
    for response in (owner.get(void_url(batch_id)), owner.post(void_url(batch_id), data=CONFIRM)):
        assert response.status_code == 409
        assert history.MSG_ALREADY_VOIDED in response.text
        assert 'name="confirm_not_sent"' not in response.text


# --- After voiding ------------------------------------------------------------------------------


def test_codes_are_accepted_again(owner: TestClient, accts: Accounts) -> None:
    data = rows_csv([1, 2, 3])
    batch_id = started(owner, [1, 2, 3])
    blocked, none = validate(owner, "again.csv", data)
    assert none is None and "Already used" in blocked.text
    assert owner.post(void_url(batch_id), data=CONFIRM).status_code == 303
    response, new_id = validate(owner, "again.csv", data)
    assert new_id is not None and new_id != batch_id
    assert "All rows passed the check" in response.text
    assert generate(owner, new_id, "again.csv", data).status_code == 200


def test_voided_batch_cannot_be_downloaded_or_continued(
    owner: TestClient, accts: Accounts, chunk_of_three
) -> None:
    data = rows_csv(EIGHT)
    batch_id = started(owner)  # partial: part 1 of 3
    assert owner.post(void_url(batch_id), data=CONFIRM).status_code == 303
    for part in (1, 2):
        response = generate(owner, batch_id, NAME, data, part=part, parts=3)
        assert response.status_code == 409
        assert routes.MSG_BATCH_VOIDED in response.text
    response, new_id = validate(owner, NAME, data)
    assert new_id is not None and new_id != batch_id  # a new batch, not a continuation
    assert 'id="continue-batch"' not in response.text
    row = batch_row(accts, batch_id)
    assert (row["status"], row["parts_downloaded"]) == ("voided", 1)


def test_purge_leaves_voided_batches_alone(owner: TestClient, accts: Accounts) -> None:
    from app.auth import retention

    batch_id = started(owner, [1, 2])
    owner.post(void_url(batch_id), data=CONFIRM)
    execute(accts, "UPDATE batches SET created_at = now() - interval '30 days'")
    accts.run(lambda db: retention.purge(db, now=datetime.now(UTC)))
    assert batch_row(accts, batch_id)["status"] == "voided"


# --- The lock shared with record_part -----------------------------------------------------------


def _actor(world: World) -> CurrentUser:
    return CurrentUser(
        user_id=world.owner_a,
        email="owner@alpha.test",
        organization_id=world.org_a,
        role=Role.OWNER,
        is_superadmin=False,
        session_id=0,
        csrf_token_hash=b"",
    )


def _part_two(chunk: int = 3) -> list:
    return parse_table(rows_csv(EIGHT), "x.csv").records[chunk : 2 * chunk]


def _state(accts: Accounts, batch_id: int) -> tuple:
    return (
        batch_row(accts, batch_id)["status"],
        count(accts, "SELECT count(*) FROM code_fingerprints WHERE batch_id = :b", b=batch_id),
        [r[0] for r in accts.sql("SELECT part FROM batch_parts ORDER BY part")],
        count(accts, "SELECT coalesce(sum(rows_generated), 0) FROM usage"),
    )


def test_part_finishing_during_a_void_is_refused(
    world: World,
    owner: TestClient,
    accts: Accounts,
    web_sessions: sessionmaker[Session],
    key: bytes,
    chunk_of_three,
) -> None:
    """The void holds the lock first: record_part waits, then refuses; nothing is re-added."""
    batch_id = started(owner)
    now = datetime.now(UTC)
    voiding = web_sessions()
    voiding.begin()
    assert batches.void(voiding, _actor(world), batch_id, ip=None, now=now)  # lock held

    outcome: dict[str, str] = {}

    def record() -> None:
        with web_sessions.begin() as db:
            try:
                batches.record_part(db, world.org_a, batch_id, 2, _part_two(), key=key, now=now)
                outcome["result"] = "recorded"
            except batches.BatchVoided:
                outcome["result"] = "refused"

    worker = threading.Thread(target=record)
    worker.start()
    worker.join(0.5)
    assert worker.is_alive(), "record_part did not wait for the void's lock"
    voiding.commit()
    voiding.close()
    worker.join(10)
    assert outcome == {"result": "refused"}
    assert _state(accts, batch_id) == ("voided", 0, [1], 3)


def test_void_during_a_part_deletes_its_fingerprints(
    world: World,
    owner: TestClient,
    accts: Accounts,
    web_sessions: sessionmaker[Session],
    key: bytes,
    chunk_of_three,
) -> None:
    """record_part holds the lock first: the void waits, then deletes what it added."""
    batch_id = started(owner)
    now = datetime.now(UTC)
    recording = web_sessions()
    recording.begin()
    check = batches.record_part(recording, world.org_a, batch_id, 2, _part_two(), key=key, now=now)
    assert not check.found  # lock held, part 2 recorded but not committed

    outcome: dict[str, bool] = {}

    def void() -> None:
        with web_sessions.begin() as db:
            outcome["voided"] = batches.void(db, _actor(world), batch_id, ip=None, now=now)

    worker = threading.Thread(target=void)
    worker.start()
    worker.join(0.5)
    assert worker.is_alive(), "void did not wait for record_part's lock"
    recording.commit()
    recording.close()
    worker.join(10)
    assert outcome == {"voided": True}
    assert _state(accts, batch_id) == ("voided", 0, [1, 2], 6)
