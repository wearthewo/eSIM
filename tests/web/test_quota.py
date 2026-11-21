"""Daily row quotas: /validate and /generate refuse what would take the organization's
rows of the UTC day (``usage``) over ``organizations.daily_row_quota``.

All data is synthetic: ICCIDs with the fake 8999 prefix, activation codes on
smdp.example.invalid.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from markupsafe import escape
from sqlalchemy.orm import Session, sessionmaker

from app import batches
from app.batches import DailyUsage
from app.core import pipeline
from app.core.parser import parse_table
from app.db.models import Role
from app.settings import Settings
from app.web import routes
from tests.web.conftest import Accounts, LoggedIn
from tests.web.test_batches import (
    FORM,
    count,
    execute,
    rows_csv,
    validate,
)
from tests.web.test_routes import _async_client, _Gate, _set_slots

NAME = "order.csv"
EIGHT = list(range(1, 9))  # 3 parts of 3, 3 and 2 rows with chunk_of_three
# As app.js sends a part without the data file: the empty logo part keeps it multipart.
NO_FILE = {"logo": ("", b"", "application/octet-stream")}


# --- Helpers ---------------------------------------------------------------------------------


def org_of(accts: Accounts, user_id: int) -> int:
    return count(accts, "SELECT organization_id FROM users WHERE id = :id", id=user_id)


def set_quota(accts: Accounts, org_id: int, quota: int) -> None:
    execute(accts, "UPDATE organizations SET daily_row_quota = :q WHERE id = :o", q=quota, o=org_id)


def set_used(accts: Accounts, org_id: int, rows: int) -> None:
    """Today's (UTC) usage of the organization."""
    execute(
        accts,
        "INSERT INTO usage (organization_id, day, rows_generated) VALUES (:o, :d, :r) "
        "ON CONFLICT (organization_id, day) DO UPDATE SET rows_generated = :r",
        o=org_id,
        d=datetime.now(UTC).date(),
        r=rows,
    )


def used_today(accts: Accounts, org_id: int) -> int:
    return count(
        accts,
        "SELECT coalesce(sum(rows_generated), 0) FROM usage "
        "WHERE organization_id = :o AND day = :d",
        o=org_id,
        d=datetime.now(UTC).date(),
    )


def quota_audits(accts: Accounts) -> list[tuple]:
    """``(outcome, user_id, organization_id, target_batch_id)`` of quota_exceeded rows."""
    rows = accts.sql(
        "SELECT outcome, user_id, organization_id, target_batch_id FROM audit_log "
        "WHERE action = 'quota_exceeded' ORDER BY id"
    )
    return [tuple(r) for r in rows]


def assert_audit_rows_hold_no_codes(accts: Accounts) -> None:
    for (row,) in accts.sql("SELECT row_to_json(a)::text FROM audit_log a"):
        assert "TEST-" not in row and "8999" not in row and "smdp" not in row


def refusal(used: int, quota: int) -> str:
    message = batches.quota_message(DailyUsage(used, quota), datetime.now(UTC))
    return str(escape(message))


def parts_of(accts: Accounts, batch_id: int) -> list[int]:
    rows = accts.sql("SELECT part FROM batch_parts WHERE batch_id = :b ORDER BY part", b=batch_id)
    return [r[0] for r in rows]


def generate_cached(client: TestClient, batch_id: int, part: int = 1, parts: int = 1):
    """POST /generate without the file (the rows come from the upload cache)."""
    form = {**FORM, "batch_id": str(batch_id), "part": str(part), "parts": str(parts)}
    return client.post("/generate", data=form, files=NO_FILE)


@pytest.fixture
def key(web_settings: Settings) -> bytes:
    assert web_settings.fingerprint_key is not None
    return web_settings.fingerprint_key.reveal().encode()


@pytest.fixture
def chunk_of_three(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)


@pytest.fixture
def org_id(logged_in: LoggedIn, accts: Accounts) -> int:
    return org_of(accts, logged_in.user_id)


# --- The message -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "text"),
    [
        (
            datetime(2026, 1, 15, 10, 0, tzinfo=UTC),
            "Daily limit reached: 19,500 of 20,000 rows used today. "
            "The limit resets at 02:00 Greek time.",
        ),
        (
            datetime(2026, 7, 15, 10, 0, tzinfo=UTC),
            "Daily limit reached: 19,500 of 20,000 rows used today. "
            "The limit resets at 03:00 Greek time.",
        ),
    ],
)
def test_quota_message(now: datetime, text: str) -> None:
    assert batches.quota_message(DailyUsage(19_500, 20_000), now) == text


def test_part_rows_and_remaining_rows() -> None:
    assert [batches.part_rows(8, p, 3) for p in (1, 2, 3, 4)] == [3, 3, 2, 0]
    assert batches.remaining_rows(8, 3, 3, ()) == 8
    assert batches.remaining_rows(8, 3, 3, (1,)) == 5
    assert batches.remaining_rows(8, 3, 3, (1, 3)) == 3
    assert DailyUsage(25, 20).remaining == 0 and DailyUsage(5, 20).remaining == 15


# --- /validate -------------------------------------------------------------------------------


def test_validate_refuses_a_file_over_the_remaining_quota(
    client: TestClient, logged_in: LoggedIn, accts: Accounts, org_id: int
) -> None:
    set_quota(accts, org_id, 10)
    set_used(accts, org_id, 8)
    response, batch_id = validate(client, NAME, rows_csv(range(1, 4)))
    assert response.status_code == 429
    assert refusal(8, 10) in response.text
    assert "Daily limit reached: 8 of 10 rows used today. The limit resets at" in response.text
    assert batch_id is None
    assert count(accts, "SELECT count(*) FROM batches") == 0
    assert len(logged_in.app.state.upload_cache) == 0
    assert quota_audits(accts) == [("failure", logged_in.user_id, org_id, None)]
    assert_audit_rows_hold_no_codes(accts)
    assert used_today(accts, org_id) == 8


def test_validate_allows_reaching_the_quota_exactly(
    client: TestClient, accts: Accounts, org_id: int
) -> None:
    set_quota(accts, org_id, 10)
    set_used(accts, org_id, 7)
    response, batch_id = validate(client, NAME, rows_csv(range(1, 4)))
    assert response.status_code == 200 and batch_id is not None
    assert quota_audits(accts) == []


def test_validate_refuses_a_file_larger_than_the_whole_quota(
    client: TestClient, accts: Accounts, org_id: int
) -> None:
    """Nothing used today, but the file alone does not fit: refused before part 1."""
    set_quota(accts, org_id, 2)
    response, batch_id = validate(client, NAME, rows_csv(range(1, 4)))
    assert response.status_code == 429 and batch_id is None
    assert refusal(0, 2) in response.text


def test_file_with_errors_shows_its_errors_not_the_quota(
    client: TestClient, accts: Accounts, org_id: int
) -> None:
    set_quota(accts, org_id, 0)
    data = rows_csv([1]) + b"123,LPA:1$smdp.example.invalid$TEST-00002-AAAA-BBBB\n"
    response, batch_id = validate(client, NAME, data)
    assert response.status_code == 200 and batch_id is None
    assert "Daily limit reached" not in response.text
    assert quota_audits(accts) == []


def test_quota_zero_blocks_everything(
    client: TestClient, logged_in: LoggedIn, accts: Accounts, org_id: int
) -> None:
    _, batch_id = validate(client, NAME, rows_csv([1]))  # checked before the quota was 0
    assert batch_id is not None
    set_quota(accts, org_id, 0)
    response, none = validate(client, "other.csv", rows_csv([2]))
    assert response.status_code == 429 and none is None
    assert refusal(0, 0) in response.text
    response = generate_cached(client, batch_id)
    assert response.status_code == 429 and "<html" in response.text
    assert refusal(0, 0) in response.text
    assert parts_of(accts, batch_id) == []
    assert quota_audits(accts) == [
        ("failure", logged_in.user_id, org_id, None),
        ("failure", logged_in.user_id, org_id, batch_id),
    ]


def test_superadmin_quota_change_applies_to_the_next_upload(
    web_app: FastAPI, client: TestClient, accts: Accounts, org_id: int
) -> None:
    set_quota(accts, org_id, 1)
    assert validate(client, NAME, rows_csv(range(1, 4)))[0].status_code == 429
    admin_id = accts.user("admin@platform.test", superadmin=True, role=Role.MEMBER)
    with LoggedIn(web_app, admin_id, accts.session(admin_id)).client(
        follow_redirects=False
    ) as admin:
        changed = admin.post(f"/admin/orgs/{org_id}/quota", data={"daily_row_quota": "3"})
    assert changed.status_code == 303
    response, batch_id = validate(client, NAME, rows_csv(range(1, 4)))
    assert response.status_code == 200 and batch_id is not None


def test_continuation_counts_only_the_parts_not_downloaded(
    client: TestClient, logged_in: LoggedIn, accts: Accounts, org_id: int, chunk_of_three
) -> None:
    data = rows_csv(EIGHT)
    _, batch_id = validate(client, NAME, data)
    assert batch_id is not None
    assert generate_cached(client, batch_id, 1, 3).status_code == 200
    assert used_today(accts, org_id) == 3

    set_quota(accts, org_id, 7)  # 3 used + 5 rows left = 8: does not fit
    response, refused = validate(client, NAME, data)
    assert response.status_code == 429 and refused is None
    assert refusal(3, 7) in response.text
    assert quota_audits(accts) == [("failure", logged_in.user_id, org_id, batch_id)]

    set_quota(accts, org_id, 8)  # exactly enough for parts 2 and 3 (not for 8 more rows)
    response, continued = validate(client, NAME, data)
    assert response.status_code == 200 and continued == batch_id


# --- /generate -------------------------------------------------------------------------------


def test_generate_refuses_a_part_over_the_quota_before_rendering(
    client: TestClient,
    logged_in: LoggedIn,
    accts: Accounts,
    org_id: int,
    chunk_of_three,
    monkeypatch,
) -> None:
    set_quota(accts, org_id, 8)
    _, batch_id = validate(client, NAME, rows_csv(EIGHT))
    assert batch_id is not None
    assert generate_cached(client, batch_id, 1, 3).status_code == 200
    assert used_today(accts, org_id) == 3
    set_used(accts, org_id, 6)  # another batch generated 3 rows meanwhile

    def never(*args, **kwargs):
        raise AssertionError("the pipeline ran for a part over the quota")

    monkeypatch.setattr(routes, "_generate_part", never)
    response = generate_cached(client, batch_id, 2, 3)  # 3 rows: 6 + 3 > 8
    assert response.status_code == 429
    assert "<html" in response.text and refusal(6, 8) in response.text
    assert parts_of(accts, batch_id) == [1]
    assert used_today(accts, org_id) == 6
    assert quota_audits(accts) == [("failure", logged_in.user_id, org_id, batch_id)]
    assert_audit_rows_hold_no_codes(accts)
    assert batch_id in logged_in.app.state.upload_cache  # kept: the order can go on tomorrow


def test_last_part_fits_when_it_is_smaller(
    client: TestClient, accts: Accounts, org_id: int, chunk_of_three
) -> None:
    """Part 3 has 2 rows (the batch's own part size decides): 6 + 2 = 8 fits."""
    set_quota(accts, org_id, 8)
    _, batch_id = validate(client, NAME, rows_csv(EIGHT))
    assert batch_id is not None
    for part in (1, 2, 3):
        assert generate_cached(client, batch_id, part, 3).status_code == 200
    assert used_today(accts, org_id) == 8
    assert quota_audits(accts) == []


def test_redownloaded_part_never_counts_or_is_refused(
    client: TestClient, accts: Accounts, org_id: int, chunk_of_three
) -> None:
    _, batch_id = validate(client, NAME, rows_csv(EIGHT))
    assert batch_id is not None
    assert generate_cached(client, batch_id, 1, 3).status_code == 200
    for quota in (3, 0):  # used up, then blocked
        set_quota(accts, org_id, quota)
        again = generate_cached(client, batch_id, 1, 3)
        assert again.status_code == 200
        assert again.headers["content-type"] == "application/zip"
        assert used_today(accts, org_id) == 3
    assert parts_of(accts, batch_id) == [1]
    assert quota_audits(accts) == []


def test_record_part_refuses_when_the_quota_ran_out_while_rendering(
    client: TestClient, logged_in: LoggedIn, accts: Accounts, org_id: int, monkeypatch
) -> None:
    """The check before the slot passed; another part used the quota up meanwhile."""
    set_quota(accts, org_id, 10)
    _, batch_id = validate(client, NAME, rows_csv(range(1, 4)))
    assert batch_id is not None
    real = pipeline.build_zip_for

    def meanwhile(*args, **kwargs):
        set_used(accts, org_id, 9)
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "build_zip_for", meanwhile)
    response = generate_cached(client, batch_id)
    assert response.status_code == 429 and refusal(9, 10) in response.text
    assert parts_of(accts, batch_id) == []
    assert used_today(accts, org_id) == 9
    assert count(accts, "SELECT status FROM batches WHERE id = :b", b=batch_id) == "validated"
    assert quota_audits(accts) == [("failure", logged_in.user_id, org_id, batch_id)]
    assert batch_id in logged_in.app.state.upload_cache


# --- Two parts at once -----------------------------------------------------------------------


def test_two_parts_at_once_cannot_both_pass_the_quota(
    client: TestClient,
    accts: Accounts,
    org_id: int,
    web_sessions: sessionmaker[Session],
    key: bytes,
) -> None:
    """record_part holds the organization's lock: the second part waits, then is refused."""
    set_quota(accts, org_id, 5)
    first_data, second_data = rows_csv(range(1, 4)), rows_csv(range(4, 7))
    _, first = validate(client, "first.csv", first_data)
    _, second = validate(client, "second.csv", second_data)
    assert first is not None and second is not None  # 3 rows each fit on their own
    now = datetime.now(UTC)

    recording = web_sessions()
    recording.begin()
    check = batches.record_part(
        recording, org_id, first, 1, parse_table(first_data, "x.csv").records, key=key, now=now
    )
    assert not check.found  # lock held, 3 rows counted but not committed

    outcome: dict[str, object] = {}

    def record() -> None:
        records = parse_table(second_data, "x.csv").records
        try:
            with web_sessions.begin() as db:
                batches.record_part(db, org_id, second, 1, records, key=key, now=now)
            outcome["result"] = "recorded"
        except batches.QuotaExceeded as exc:
            outcome["result"] = exc.usage

    worker = threading.Thread(target=record)
    worker.start()
    worker.join(0.5)
    assert worker.is_alive(), "record_part did not wait for the other part's lock"
    recording.commit()
    recording.close()
    worker.join(10)
    assert outcome == {"result": DailyUsage(used=3, quota=5)}
    assert used_today(accts, org_id) == 3
    assert parts_of(accts, first) == [1] and parts_of(accts, second) == []


def test_two_generate_requests_at_once(
    logged_in: LoggedIn, client: TestClient, accts: Accounts, org_id: int, monkeypatch
) -> None:
    """Both pass the check before the slot; under the lock only one fits."""
    set_quota(accts, org_id, 5)
    _, first = validate(client, "first.csv", rows_csv(range(1, 4)))
    _, second = validate(client, "second.csv", rows_csv(range(4, 7)))
    assert first is not None and second is not None
    _set_slots(logged_in, 2)
    gate = _Gate(routes._generate_part)
    monkeypatch.setattr(routes, "_generate_part", gate)

    async def scenario() -> list[int]:
        async with _async_client(logged_in) as http:
            jobs = [
                asyncio.create_task(
                    http.post("/generate", data={**FORM, "batch_id": str(batch_id)}, files=NO_FILE)
                )
                for batch_id in (first, second)
            ]
            assert await gate.entered(2)  # both passed the first check
            gate.release.set()
            return [(await job).status_code for job in jobs]

    assert sorted(asyncio.run(scenario())) == [200, 429]
    assert used_today(accts, org_id) == 3
    assert len(parts_of(accts, first) + parts_of(accts, second)) == 1
    assert len(quota_audits(accts)) == 1


# --- "Today: X / Y rows" ---------------------------------------------------------------------


def test_today_usage_on_the_upload_and_history_pages(
    web_app: FastAPI, client: TestClient, accts: Accounts, org_id: int
) -> None:
    set_quota(accts, org_id, 20_000)
    set_used(accts, org_id, 1_500)
    other = accts.org("Other Org (test)")
    set_used(accts, other, 999)
    for path in ("/", "/history"):
        response = client.get(path)
        assert response.status_code == 200
        assert "Today: 1,500 / 20,000 rows" in response.text
        assert "999" not in response.text

    admin_id = accts.user("admin@platform.test", superadmin=True, role=Role.MEMBER)
    with LoggedIn(web_app, admin_id, accts.session(admin_id)).client() as admin:
        assert "Today:" not in admin.get("/history").text
        assert "Today:" not in admin.get("/").text


def test_today_usage_counts_generated_parts(
    client: TestClient, accts: Accounts, org_id: int
) -> None:
    _, batch_id = validate(client, NAME, rows_csv(range(1, 4)))
    assert batch_id is not None
    assert "Today: 0 / 20,000 rows" in client.get("/").text
    assert generate_cached(client, batch_id).status_code == 200
    assert "Today: 3 / 20,000 rows" in client.get("/").text
