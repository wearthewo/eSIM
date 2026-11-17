"""Batches and the duplicate check across batches, through /validate and /generate.

All data is synthetic: ICCIDs with the fake 8999 prefix, activation codes on
smdp.example.invalid, *.test / example.invalid emails.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from markupsafe import escape
from sqlalchemy import text

from app import batches
from app.core import pipeline
from app.core.parser import parse_table
from app.db.models import BatchStatus, Role
from app.localtime import format_athens
from app.settings import Settings
from app.web import routes
from tests.conftest import FIXTURES_DIR, synthetic_iccid
from tests.web.conftest import Accounts, LoggedIn

FORM = {"company_name": "Example Telecom (test)", "primary_color": "#1a4d8f"}
_BATCH_INPUT = re.compile(r'<input type="hidden" name="batch_id" value="([0-9]+)">')


def rows_csv(numbers: range | list[int], *, extra_header: str = "", extra: str = "") -> bytes:
    """A CSV whose row for ``n`` has ICCID ``synthetic_iccid(n)`` and code ``TEST-n``."""
    lines = ["iccid,activation_code" + extra_header]
    lines += [
        f"{synthetic_iccid(n)},LPA:1$smdp.example.invalid$TEST-{n:05d}-AAAA-BBBB{extra}"
        for n in numbers
    ]
    return ("\n".join(lines) + "\n").encode()


def csv_file(name: str, data: bytes) -> dict:
    return {"file": (name, data, "text/csv")}


def validate(client: TestClient, name: str, data: bytes):
    """POST /validate; returns the response and the batch id in it (``None`` if none)."""
    response = client.post("/validate", files=csv_file(name, data))
    match = _BATCH_INPUT.search(response.text)
    return response, int(match.group(1)) if match else None


def generate(
    client: TestClient, batch_id: object, name: str, data: bytes, part: int = 1, parts: int = 1
):
    form = {**FORM, "part": str(part), "parts": str(parts)}
    if batch_id is not None:
        form["batch_id"] = str(batch_id)
    return client.post("/generate", data=form, files=csv_file(name, data))


def batch_row(accts: Accounts, batch_id: int) -> dict:
    rows = accts.sql("SELECT row_to_json(b)::text FROM batches b WHERE id = :id", id=batch_id)
    return json.loads(rows[0][0]) if rows else {}


def count(accts: Accounts, sql: str, **params: object) -> int:
    return accts.sql(sql, **params)[0][0]


def execute(accts: Accounts, sql: str, **params: object) -> None:
    """Run a statement that returns no rows (``accts.sql`` reads rows)."""
    accts.run(lambda db: db.execute(text(sql), params))


def used_message(file_name: str, when: str) -> str:
    return str(escape(f"Already used in batch '{file_name}' on {when}"))


def squash(text: str) -> str:
    """Whitespace collapsed, so template line breaks do not matter."""
    return " ".join(text.split())


@pytest.fixture
def other_org(web_app: FastAPI, accts: Accounts) -> Iterator[TestClient]:
    """A logged-in owner of a second organization."""
    org_id = accts.org("Other Org (test)")
    user_id = accts.user("owner@other.test", org_id=org_id)
    with LoggedIn(web_app, user_id, accts.session(user_id)).client() as c:
        yield c


@pytest.fixture
def key(web_settings: Settings) -> bytes:
    assert web_settings.fingerprint_key is not None
    return web_settings.fingerprint_key.reveal().encode()


@pytest.fixture
def chunk_of_three(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)


# --- /validate creates a batch ---------------------------------------------------------------


def test_passing_file_creates_a_validated_batch(
    client: TestClient, logged_in: LoggedIn, accts: Accounts
) -> None:
    data = (FIXTURES_DIR / "warning_iccid_prefix.csv").read_bytes()
    # (A tab or quote in a file name is percent-encoded by the HTTP client, so not used here.)
    response, batch_id = validate(client, "C:\\orders\\Orders\u200b   March \u2028 2026.csv", data)
    assert response.status_code == 200 and batch_id is not None
    row = batch_row(accts, batch_id)
    org_id = count(accts, "SELECT organization_id FROM users WHERE id = :id", id=logged_in.user_id)
    assert row["organization_id"] == org_id and row["user_id"] == logged_in.user_id
    assert row["file_name"] == "Orders March 2026.csv"
    assert row["status"] == "validated"
    rows = len(parse_table(data, "w.csv").records)
    assert (row["row_count"], row["parts"], row["parts_downloaded"]) == (rows, 1, 0)
    assert row["error_count"] == 0 and row["warning_count"] > 0
    assert row["created_at"] is not None


def test_failing_file_creates_no_batch(client: TestClient, accts: Accounts) -> None:
    data = (FIXTURES_DIR / "mixed_valid_invalid.csv").read_bytes()
    response, batch_id = validate(client, "m.csv", data)
    assert response.status_code == 200 and "Some rows have errors" in response.text
    assert batch_id is None and 'name="batch_id"' not in response.text
    header_only, _ = validate(client, "h.csv", b"iccid,activation_code\n")
    assert routes.MSG_NO_ROWS in header_only.text
    assert count(accts, "SELECT count(*) FROM batches") == 0


@pytest.mark.parametrize(
    ("name", "kept"),
    [
        ("orders.csv", "orders.csv"),
        ("x/y/../orders.csv", "orders.csv"),
        ("C:\\Users\\me\\Desktop\\orders.xlsx", "orders.xlsx"),
        ("  a\x00b\x1fc\u202e d\u00a0\u00a0e.csv  ", "ab c d e.csv"),
        ("cafe\u0301.csv", "caf\u00e9.csv"),  # NFC
        ("\x07\x08", "upload"),
        ("", "upload"),
        ("x" * 250 + ".csv", "x" * 200),
    ],
)
def test_sanitize_file_name(name: str, kept: str) -> None:
    assert batches.sanitize_file_name(name) == kept


def test_superadmin_without_organization_cannot_generate(web_app: FastAPI, accts: Accounts) -> None:
    admin_id = accts.user("admin@platform.test", superadmin=True, role=Role.MEMBER)
    with LoggedIn(web_app, admin_id, accts.session(admin_id)).client() as admin:
        page = admin.get("/")
        assert page.status_code == 200
        assert routes.MSG_NO_ORGANIZATION in page.text
        assert 'id="generate-form"' not in page.text and 'href="/template.xlsx"' not in page.text
        assert ">Upload</a>" not in page.text and 'href="/history">History</a>' in page.text
        response, batch_id = validate(admin, "v.csv", rows_csv(range(1, 4)))
        assert response.status_code == 403 and batch_id is None
        assert routes.MSG_NO_ORGANIZATION in response.text
        generated = generate(admin, 1, "v.csv", rows_csv(range(1, 4)))
        assert generated.status_code == 404 and routes.MSG_BATCH_GONE in generated.text
    assert count(accts, "SELECT count(*) FROM batches") == 0


def test_members_and_owners_see_the_upload_link(client: TestClient) -> None:
    page = client.get("/").text
    assert 'href="/">Upload</a>' in page and 'href="/history">History</a>' in page


# --- /generate needs the batch ------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "abc", "0", "-1", "1.0", " 1", "\u0661", "9" * 20])
def test_generate_without_a_usable_batch_id_is_400(client: TestClient, value) -> None:
    data = rows_csv(range(1, 4))
    validate(client, "v.csv", data)
    response = generate(client, value, "v.csv", data)
    assert response.status_code == 400
    assert routes.MSG_NO_BATCH in response.text and "<html" in response.text


def test_batch_of_another_organization_is_404(
    client: TestClient, other_org: TestClient, accts: Accounts
) -> None:
    data = rows_csv(range(1, 4))
    _, theirs = validate(other_org, "theirs.csv", data)
    assert theirs is not None
    stolen = generate(client, theirs, "v.csv", data)
    unknown = generate(client, theirs + 1000, "v.csv", data)
    for response in (stolen, unknown):
        assert response.status_code == 404
        assert routes.MSG_BATCH_GONE in response.text
        assert response.headers["content-type"].startswith("text/html")
    assert stolen.text == unknown.text  # nothing tells the two apart
    row = batch_row(accts, theirs)
    assert (row["status"], row["parts_downloaded"]) == ("validated", 0)
    assert count(accts, "SELECT count(*) FROM code_fingerprints") == 0
    assert count(accts, "SELECT count(*) FROM usage") == 0


def test_file_must_match_its_batch(client: TestClient) -> None:
    _, batch_id = validate(client, "v.csv", rows_csv(range(1, 4)))
    response = generate(client, batch_id, "v.csv", rows_csv(range(1, 6)))
    assert response.status_code == 400 and routes.MSG_BAD_PART in response.text


# --- Parts are counted once ---------------------------------------------


def test_parts_and_usage_are_counted_once(
    client: TestClient, accts: Accounts, chunk_of_three
) -> None:
    data = rows_csv(range(1, 9))  # 8 rows: parts of 3, 3 and 2
    _, batch_id = validate(client, "order.csv", data)
    assert batch_row(accts, batch_id)["parts"] == 3

    def state() -> tuple[str, int, int, int, int]:
        row = batch_row(accts, batch_id)
        return (
            row["status"],
            row["parts_downloaded"],
            count(accts, "SELECT coalesce(sum(rows_generated), 0) FROM usage"),
            count(accts, "SELECT count(*) FROM code_fingerprints"),
            count(accts, "SELECT count(*) FROM batch_parts"),
        )

    steps = [
        (1, ("partial", 1, 3, 6, 1)),
        (1, ("partial", 1, 3, 6, 1)),  # retry / "download again"
        (2, ("partial", 2, 6, 12, 2)),
        (3, ("completed", 3, 8, 16, 3)),
        (3, ("completed", 3, 8, 16, 3)),
        (1, ("completed", 3, 8, 16, 3)),
    ]
    for part, expected in steps:
        response = generate(client, batch_id, "order.csv", data, part=part, parts=3)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/zip"
        assert state() == expected, part
    day = accts.sql("SELECT day FROM usage")[0][0]
    assert day == datetime.now(UTC).date()  # usage days are UTC


# --- Duplicates across batches ---------------------------------------------


def _generated(client: TestClient, name: str, data: bytes) -> int:
    _, batch_id = validate(client, name, data)
    assert batch_id is not None
    assert generate(client, batch_id, name, data).status_code == 200
    return batch_id


def test_codes_used_by_an_earlier_batch_are_errors(client: TestClient, accts: Accounts) -> None:
    earlier = _generated(client, "orders_march.csv", rows_csv(range(1, 6)))
    execute(
        accts, "UPDATE batches SET created_at = '2026-03-05 08:30+00' WHERE id = :id", id=earlier
    )  # 10:30 in Athens (winter time)
    response, batch_id = validate(client, "orders_april.csv", rows_csv([3, 4, 10, 11]))
    html = response.text
    assert response.status_code == 200 and batch_id is None
    assert "4 rows read: 4 errors, 0 warnings." in html
    message = used_message("orders_march.csv", "05/03/2026 10:30")
    for row in (2, 3):  # file rows 2 and 3 hold numbers 3 and 4
        for field in ("iccid", "activation_code"):
            assert f'<tr class="error"><td>{row}</td><td>{field}</td><td>{message}</td>' in html
    assert html.count('<tr class="error">') == 4
    assert (
        "<strong>2 rows were already used in earlier batches:</strong>"
        " 'orders_march.csv' on 05/03/2026 10:30 (2 rows).</p>"
    ) in squash(html)
    assert "Remove those rows and upload again." in html
    for number in (3, 4):
        assert synthetic_iccid(number) not in html and f"TEST-{number:05d}" not in html
    assert 'id="download"' not in html


def test_activation_code_alone_is_a_duplicate(client: TestClient) -> None:
    _generated(client, "a.csv", rows_csv([1]))
    code = "LPA:1$smdp.example.invalid$TEST-00001-AAAA-BBBB"
    reused = f"iccid,activation_code\n{synthetic_iccid(99)},{code}\n"
    response, batch_id = validate(client, "b.csv", reused.encode())
    assert batch_id is None
    assert '<tr class="error"><td>2</td><td>activation_code</td><td>Already used' in response.text
    assert "<td>iccid</td>" not in response.text


def test_other_organizations_codes_never_match(
    client: TestClient, other_org: TestClient, accts: Accounts
) -> None:
    data = rows_csv(range(1, 6))
    _generated(other_org, "theirs.csv", data)
    response, batch_id = validate(client, "ours.csv", data)
    assert batch_id is not None and "All rows passed the check" in response.text
    assert generate(client, batch_id, "ours.csv", data).status_code == 200
    # Same codes, different organizations: unrelated fingerprints.
    assert count(accts, "SELECT count(DISTINCT fingerprint) FROM code_fingerprints") == 20


def test_twelve_month_window(client: TestClient, accts: Accounts) -> None:
    data = rows_csv(range(1, 4))
    earlier = _generated(client, "old.csv", data)
    now = datetime.now(UTC)
    execute(accts, "UPDATE code_fingerprints SET created_at = :t", t=now - timedelta(days=364))
    _, inside = validate(client, "again.csv", data)
    assert inside is None
    execute(accts, "UPDATE code_fingerprints SET created_at = :t", t=now - timedelta(days=366))
    response, outside = validate(client, "again.csv", data)
    assert outside is not None and "All rows passed the check" in response.text
    # Generating takes the old fingerprints over (they wait for the daily purge).
    assert generate(client, outside, "again.csv", data).status_code == 200
    owners = accts.sql("SELECT DISTINCT batch_id FROM code_fingerprints")
    assert owners == [(outside,)] and outside != earlier
    assert count(accts, "SELECT count(*) FROM code_fingerprints") == 6


def test_generate_rechecks_and_marks_the_batch_failed(client: TestClient, accts: Accounts) -> None:
    data = rows_csv(range(1, 4))
    _, first = validate(client, "first.csv", data)
    _, second = validate(client, "second.csv", data)  # nothing generated yet: both pass
    assert first is not None and second is not None
    assert generate(client, first, "first.csv", data).status_code == 200
    response = generate(client, second, "second.csv", data)
    assert response.status_code == 422 and "<html" in response.text
    assert used_message("first.csv", format_athens(_created(accts, first))) in response.text
    assert "3 rows were already used in earlier batches" in response.text
    row = batch_row(accts, second)
    assert (row["status"], row["error_count"], row["parts_downloaded"]) == ("failed", 6, 0)
    assert count(accts, "SELECT count(*) FROM code_fingerprints WHERE batch_id = :b", b=second) == 0


def _created(accts: Accounts, batch_id: int) -> datetime:
    return accts.sql("SELECT created_at FROM batches WHERE id = :id", id=batch_id)[0][0]


def test_record_part_closes_the_race_between_batches(
    client: TestClient, accts: Accounts, key: bytes
) -> None:
    data = rows_csv(range(1, 4))
    _, first = validate(client, "first.csv", data)
    _, second = validate(client, "second.csv", data)
    records = parse_table(data, "x.csv").records
    org_id = count(accts, "SELECT organization_id FROM batches WHERE id = :id", id=first)
    now = datetime.now(UTC)

    def record(batch_id: int) -> batches.UsedCheck:
        return accts.run(
            lambda db: batches.record_part(db, org_id, batch_id, 1, records, key=key, now=now)
        )

    assert not record(first).found
    lost = record(second)
    assert lost.found and lost.summary is not None and lost.summary.rows == 3
    assert {i.code for i in lost.issues} == {"already_used_iccid", "already_used_activation_code"}
    assert batch_row(accts, second)["parts_downloaded"] == 0
    assert not record(first).found  # its own fingerprints never count


def test_file_names_in_messages_are_escaped(client: TestClient) -> None:
    _generated(client, "<b>x & y<i>.csv", rows_csv([1]))
    response, _ = validate(client, "y.csv", rows_csv([1]))
    assert "<b>" not in response.text
    assert "&#39;&lt;b&gt;x &amp; y&lt;i&gt;.csv&#39; on" in response.text  # the message
    assert "'&lt;b&gt;x &amp; y&lt;i&gt;.csv' on" in response.text  # the summary


def test_summary_of_a_large_reupload(
    client: TestClient, logged_in: LoggedIn, accts: Accounts, key: bytes
) -> None:
    """10,000 rows, 3,500 of them generated before by 7 earlier batches."""
    sizes = [1000, 800, 600, 500, 300, 200, 100]
    user_id = logged_in.user_id
    org_id = count(accts, "SELECT organization_id FROM users WHERE id = :id", id=user_id)
    start = 1
    expected: list[tuple[str, int, datetime]] = []
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    for i, size in enumerate(sizes, 1):
        numbers = range(start, start + size)
        start += size
        records = parse_table(rows_csv(numbers), "x.csv").records
        when = now - timedelta(days=40 - i)

        def make(db, i=i, size=size, records=records, when=when) -> None:
            batch_id = batches.create_batch(
                db,
                organization_id=org_id,
                user_id=user_id,
                file_name=f"earlier_{i}.csv",
                row_count=size,
                parts=pipeline.part_count(size),
                warning_count=0,
                now=when,
            )
            check = batches.record_part(db, org_id, batch_id, 1, records, key=key, now=when)
            assert not check.found

        accts.run(make)
        expected.append((f"earlier_{i}.csv", size, when))

    data = rows_csv(range(1, 10_001))
    response, batch_id = validate(client, "orders.csv", data)
    html = response.text
    assert batch_id is None and 'id="download"' not in html
    assert "10000 rows read: 7000 errors, 0 warnings." in html
    groups = ", ".join(
        f"'{name}' on {format_athens(when)} ({size} rows)" for name, size, when in expected[:5]
    )
    summary = f"<strong>3500 rows were already used in earlier batches:</strong> {groups}"
    assert f"{summary}, and others.</p>" in squash(html)
    assert "earlier_6.csv" not in html.split("</strong>", 1)[1].split("</p>", 1)[0]
    assert html.count('<tr class="error">') == routes.MAX_ISSUES_SHOWN


# --- Nothing but metadata in the database ---------------------------------------------


def _all_rows_as_text(accts: Accounts) -> str:
    tables = [
        "organizations",
        "users",
        "batches",
        "batch_parts",
        "code_fingerprints",
        "usage",
        "audit_log",
        "sessions",
        "branding",
    ]
    return "\n".join(
        row[0]
        for table in tables
        for row in accts.sql(f"SELECT row_to_json(t)::text FROM {table} t")  # noqa: S608
    )


def test_no_codes_or_customer_data_in_the_database(client: TestClient, accts: Accounts) -> None:
    for name in ("valid_full.csv", "emails_greek_mixed.csv", "valid_split_smdp.csv"):
        data = (FIXTURES_DIR / name).read_bytes()
        response, batch_id = validate(client, name, data)
        if batch_id is not None:
            assert generate(client, batch_id, name, data).status_code == 200
    assert count(accts, "SELECT count(*) FROM code_fingerprints") > 0
    dump = _all_rows_as_text(accts)
    for name in ("valid_full.csv", "emails_greek_mixed.csv", "valid_split_smdp.csv"):
        text = (FIXTURES_DIR / name).read_text(encoding="utf-8-sig")
        cells = {
            cell.strip()
            for line in text.splitlines()[1:]
            for cell in line.split(",")
            if len(cell.strip()) >= 5
        }
        leaked = sorted(cell for cell in cells if cell in dump)
        assert leaked == [], name
    # (Not "8999": a fingerprint's hex digits can contain any short digit run.)
    for fragment in ("LPA:", "smdp.example", "TEST-", "CONF", "example.invalid", "Test Customer"):
        assert fragment not in dump, fragment


def test_failed_generation_stores_no_codes_either(client: TestClient, accts: Accounts) -> None:
    data = rows_csv(range(1, 4))
    _, first = validate(client, "first.csv", data)
    _, second = validate(client, "second.csv", data)
    generate(client, first, "first.csv", data)
    generate(client, second, "second.csv", data)
    dump = _all_rows_as_text(accts)
    assert "TEST-" not in dump and synthetic_iccid(1) not in dump
    assert BatchStatus.FAILED.value in dump
