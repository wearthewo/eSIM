"""The History page: who sees which batches, the From/To filter, pages. Synthetic data."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.models import Batch, BatchStatus, Role
from app.localtime import ATHENS, format_athens
from app.web import history
from tests.web.conftest import Accounts, LoggedIn

NOW = datetime.now(UTC)


def squash(html: str) -> str:
    return " ".join(html.split())


def rows(html: str) -> list[str]:
    """The batch rows' file names, in page order."""
    return re.findall(r'<td class="file-name">([^<]*)</td>', html)


def add_batch(
    accts: Accounts,
    org_id: int,
    user_id: int | None,
    file_name: str,
    *,
    created_at: datetime = NOW - timedelta(hours=1),
    status: BatchStatus = BatchStatus.COMPLETED,
    parts: int = 1,
    parts_downloaded: int | None = None,
    row_count: int = 10,
    warning_count: int = 0,
) -> int:
    def _add(db) -> int:
        batch = Batch(
            organization_id=org_id,
            user_id=user_id,
            created_at=created_at,
            file_name=file_name,
            row_count=row_count,
            parts=parts,
            parts_downloaded=parts if parts_downloaded is None else parts_downloaded,
            warning_count=warning_count,
            status=status,
        )
        db.add(batch)
        db.flush()
        return batch.id

    return accts.run(_add)


class World:
    """Two organizations with a member each, and a superadmin without organization."""

    def __init__(self, app: FastAPI, accts: Accounts) -> None:
        self.accts = accts
        self.org_a = accts.org("Alpha Mobile (test)")
        self.org_b = accts.org("Beta Telecom (test)")
        self.member_a = accts.user("member@alpha.test", org_id=self.org_a, role=Role.MEMBER)
        self.owner_b = accts.user("owner@beta.test", org_id=self.org_b, role=Role.OWNER)
        self.admin = accts.user("admin@platform.test", superadmin=True, role=Role.MEMBER)
        self.app = app

    def client(self, user_id: int) -> TestClient:
        return LoggedIn(self.app, user_id, self.accts.session(user_id)).client()


@pytest.fixture
def world(web_app: FastAPI, accts: Accounts) -> World:
    return World(web_app, accts)


@pytest.fixture
def member(world: World) -> Iterator[TestClient]:
    with world.client(world.member_a) as c:
        yield c


@pytest.fixture
def admin(world: World) -> Iterator[TestClient]:
    with world.client(world.admin) as c:
        yield c


# --- Who sees what --------------------------------------------------


def test_top_bar_links_history_for_everyone(world: World) -> None:
    for user_id in (world.member_a, world.owner_b, world.admin):
        with world.client(user_id) as c:
            assert 'href="/history">History</a>' in c.get("/").text
            page = c.get("/history")
            assert page.status_code == 200
            assert 'aria-current="page" href="/history">History</a>' in page.text


def test_history_needs_login(anon: TestClient) -> None:
    response = anon.get("/history")
    assert response.status_code == 303 and response.headers["location"] == "/login"


def test_members_see_only_their_organization(world: World, member: TestClient) -> None:
    add_batch(world.accts, world.org_a, world.member_a, "alpha_orders.csv")
    add_batch(world.accts, world.org_b, world.owner_b, "beta_orders.csv")
    html = member.get("/history").text
    assert rows(html) == ["alpha_orders.csv"]
    assert "beta" not in html.lower() and "Organization</th>" not in html
    # Asking for another organization changes nothing.
    assert rows(member.get(f"/history?org={world.org_b}").text) == ["alpha_orders.csv"]
    with world.client(world.owner_b) as owner:
        assert rows(owner.get("/history").text) == ["beta_orders.csv"]


def test_superadmin_sees_all_and_can_filter(world: World, admin: TestClient) -> None:
    add_batch(world.accts, world.org_a, world.member_a, "alpha_orders.csv")
    add_batch(world.accts, world.org_b, world.owner_b, "beta_orders.csv", created_at=NOW)
    html = admin.get("/history").text
    assert rows(html) == ["beta_orders.csv", "alpha_orders.csv"]
    assert '<th scope="col">Organization</th>' in html
    assert "<td>Beta Telecom (test)</td>" in html and "<td>Alpha Mobile (test)</td>" in html
    assert f'<option value="{world.org_a}">Alpha Mobile (test)</option>' in html
    filtered = admin.get(f"/history?org={world.org_a}").text
    assert rows(filtered) == ["alpha_orders.csv"]
    assert f'<option value="{world.org_a}" selected>' in filtered
    bad = admin.get("/history?org=abc")
    assert bad.status_code == 400 and history.MSG_BAD_ORG in bad.text
    assert rows(bad.text) == []


def test_batches_only_checked_are_not_listed(world: World, member: TestClient) -> None:
    add_batch(world.accts, world.org_a, world.member_a, "checked_only.csv", **_validated())
    add_batch(world.accts, world.org_a, world.member_a, "generated.csv")
    assert rows(member.get("/history").text) == ["generated.csv"]


def _validated() -> dict:
    return {"status": BatchStatus.VALIDATED, "parts_downloaded": 0}


# --- Columns --------------------------------------------------


def test_columns(world: World, member: TestClient) -> None:
    created = datetime(2026, 7, 1, 6, 5, tzinfo=UTC)  # 09:05 in Athens (summer time)
    add_batch(
        world.accts,
        world.org_a,
        world.member_a,
        "big_order.xlsx",
        created_at=created,
        status=BatchStatus.INCOMPLETE,
        parts=20,
        parts_downloaded=7,
        row_count=10_000,
        warning_count=3,
    )
    html = squash(member.get("/history?from_date=01/07/2026&to_date=01/07/2026").text)
    assert (
        '<td class="muted">01/07/2026 09:05</td> <td>member@alpha.test</td>'
        ' <td class="file-name">big_order.xlsx</td> <td class="num">10000</td>'
        ' <td class="num">7 / 20</td> <td class="num">3</td>'
        ' <td><span class="badge badge-warning">Incomplete: 7 / 20 parts</span></td>'
    ) in html
    for header in ("Date and time", "User", "File name", "Rows", "Parts downloaded", "Warnings"):
        assert f">{header}</th>" in html
    assert ">Errors</th>" not in html
    assert history.NOTE in html


@pytest.mark.parametrize(
    ("status", "badge"),
    [
        (BatchStatus.PARTIAL, '<span class="badge badge-info">Partial</span>'),
        (BatchStatus.COMPLETED, '<span class="badge badge-success">Completed</span>'),
        (BatchStatus.FAILED, '<span class="badge badge-error">Failed</span>'),
    ],
)
def test_status_badges(world: World, member: TestClient, status: BatchStatus, badge: str) -> None:
    add_batch(world.accts, world.org_a, world.member_a, "x.csv", status=status)
    assert badge in member.get("/history").text


def test_deleted_user_shows_a_dash(world: World, member: TestClient) -> None:
    gone = world.accts.user("gone@alpha.test", org_id=world.org_a, role=Role.MEMBER)
    add_batch(world.accts, world.org_a, gone, "x.csv")
    world.accts.run(lambda db: db.execute(text("DELETE FROM users WHERE id = :id"), {"id": gone}))
    assert "<td>—</td>" in member.get("/history").text


# --- The From/To filter --------------------------------------------------


def test_default_range_is_the_last_thirty_days(world: World, member: TestClient) -> None:
    add_batch(world.accts, world.org_a, None, "recent.csv", created_at=NOW - timedelta(days=29))
    add_batch(world.accts, world.org_a, None, "old.csv", created_at=NOW - timedelta(days=32))
    html = member.get("/history").text
    assert rows(html) == ["recent.csv"]
    start = NOW.astimezone(ATHENS).date() - timedelta(days=30)
    assert f'value="{start:%d/%m/%Y}"' in html and 'value="00:00"' in html
    assert f'value="{format_athens(NOW)[:10]}"' in html  # To = today


def test_from_and_to_select_the_batches(world: World, member: TestClient) -> None:
    for day in (1, 2, 3):  # 10:00 Athens on 1, 2 and 3 March 2026 (winter time)
        add_batch(
            world.accts,
            world.org_a,
            None,
            f"march_{day}.csv",
            created_at=datetime(2026, 3, day, 8, 0, tzinfo=UTC),
        )
    page = member.get("/history?from_date=02/03/2026&from_time=&to_date=03/03/2026&to_time=")
    assert rows(page.text) == ["march_3.csv", "march_2.csv"]  # newest first
    exact = member.get(
        "/history?from_date=02/03/2026&from_time=10:00&to_date=02/03/2026&to_time=10:00"
    )
    assert rows(exact.text) == ["march_2.csv"]
    later = member.get("/history?from_date=02/03/2026&from_time=10:01&to_date=03/03/2026")
    assert rows(later.text) == ["march_3.csv"]


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("from_date=2026-03-01", "Enter the From date as dd/mm/yyyy, for example 05/03/2026."),
        ("from_date=31/02/2026", "The From date 31/02/2026 does not exist."),
        ("to_date=1/3/2026", "Enter the To date as dd/mm/yyyy"),
        ("from_date=01/03/2026&from_time=9:30", "Enter the From time as HH:mm (24-hour)"),
        ("from_date=01/03/2026&to_date=02/03/2026&to_time=24:00", "Enter the To time as HH:mm"),
        (
            "from_date=03/03/2026&to_date=02/03/2026",
            "The From date and time must be before the To date and time.",
        ),
        ("from_date=" + "1" * 40, history.MSG_TOO_LONG),
    ],
)
def test_bad_filter_input(world: World, member: TestClient, query: str, message: str) -> None:
    add_batch(world.accts, world.org_a, None, "x.csv")
    response = member.get(f"/history?{query}")
    assert response.status_code == 400
    html = response.text
    assert '<p class="error" role="alert">' in html and message in html
    assert rows(html) == [] and 'class="pagination"' not in html
    typed = query.split("&")[0].split("=", 1)[1]
    if len(typed) <= history.MAX_FIELD_CHARS:
        assert f'value="{typed}"' in html  # the form keeps what was typed


def test_filter_input_is_escaped(world: World, member: TestClient) -> None:
    html = member.get('/history?from_date="><b>x').text
    assert "<b>x" not in html and "&#34;&gt;&lt;b&gt;x" in html


# --- Pages --------------------------------------------------


def test_fifty_per_page_newest_first(world: World, member: TestClient) -> None:
    for n in range(120):
        add_batch(
            world.accts,
            world.org_a,
            world.member_a,
            f"order_{n:03d}.csv",
            created_at=NOW - timedelta(minutes=10 + n),
        )
    first = member.get("/history").text
    assert rows(first) == [f"order_{n:03d}.csv" for n in range(50)]
    assert "120 batches · page 1 of 3" in first
    assert '<form method="get" action="/history" class="pagination"' in first
    assert re.search(r'name="page" value="0" disabled>Previous', first)
    assert re.search(r'name="page" value="2">Next', first)
    third = member.get("/history?page=3").text
    assert rows(third) == [f"order_{n:03d}.csv" for n in range(100, 120)]
    assert re.search(r'name="page" value="4" disabled>Next', third)
    assert rows(member.get("/history?page=99").text) == rows(third)  # past the end: the last
    assert rows(member.get("/history?page=abc").text) == rows(first)


def test_pages_keep_the_filter(world: World, member: TestClient) -> None:
    for n in range(60):
        add_batch(
            world.accts,
            world.org_a,
            None,
            f"o{n}.csv",
            created_at=datetime(2026, 3, 2, 8, 0, tzinfo=UTC) - timedelta(minutes=n),
        )
    html = member.get("/history?from_date=02/03/2026&to_date=02/03/2026&page=2").text
    assert len(rows(html)) == 10
    form = html.split('class="pagination"', 1)[1].split("</form>", 1)[0]
    for name, value in (
        ("from_date", "02/03/2026"),
        ("from_time", "00:00"),
        ("to_date", "02/03/2026"),
        ("to_time", ""),
    ):
        assert f'<input type="hidden" name="{name}" value="{value}">' in form


def test_empty_period(member: TestClient) -> None:
    html = member.get("/history").text
    assert "No batches in this period" in html and "<table" not in html


def test_history_is_csp_clean(member: TestClient) -> None:
    html = member.get("/history").text
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline script"
    assert "<style" not in html and " style=" not in html
    assert not re.search(r"\son\w+=", html), "inline event handler"
    assert "data-datepicker" in html
