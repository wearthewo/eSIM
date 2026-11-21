"""The upload cache through /validate and /generate: hits, misses, the re-upload
fallback, organization scoping, and when the cached rows are dropped.

All data is synthetic: ICCIDs with the fake 8999 prefix, activation codes on
smdp.example.invalid.
"""

from __future__ import annotations

import dataclasses
import io
import logging
import zipfile
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import pipeline
from app.db.models import Role
from app.upload_cache import UploadCache
from app.web import history, routes
from tests.conftest import synthetic_iccid
from tests.web.conftest import Accounts, LoggedIn
from tests.web.test_batches import FORM, batch_row, count, generate, rows_csv, validate

NAME = "order.csv"
EIGHT = list(range(1, 9))  # 3 parts of 3 rows (chunk_of_three)
# A browser's FormData without the data file still has the (empty) logo part, so the
# request stays multipart/form-data.
NO_FILE = {"logo": ("", b"", "application/octet-stream")}


@pytest.fixture
def chunk_of_three(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)


@pytest.fixture
def cache(web_app: FastAPI) -> UploadCache:
    return web_app.state.upload_cache


@pytest.fixture
def other_org(web_app: FastAPI, accts: Accounts) -> Iterator[TestClient]:
    org_id = accts.org("Other Org (test)")
    user_id = accts.user("owner@other.test", org_id=org_id)
    with LoggedIn(web_app, user_id, accts.session(user_id)).client() as c:
        yield c


def generate_cached(client: TestClient, batch_id: int, part: int = 1, parts: int = 1):
    """POST /generate without the data file, as app.js sends it first."""
    form = {**FORM, "batch_id": str(batch_id), "part": str(part), "parts": str(parts)}
    return client.post("/generate", data=form, files=NO_FILE)


def pdf_names(response) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        return sorted(n for n in zf.namelist() if n.endswith(".pdf"))


def expected_names(numbers) -> list[str]:
    return sorted(f"esim_{synthetic_iccid(n)}.pdf" for n in numbers)


def no_parse(monkeypatch) -> None:
    """Make any parse/validate of a file fail: proves a request was served from the cache."""

    def refuse(*args, **kwargs):
        raise AssertionError("the file was parsed again")

    monkeypatch.setattr(routes, "check_upload", refuse)


# --- Hits ----------------------------------------------------------------------------------


def test_validate_caches_the_checked_rows(
    client: TestClient, logged_in: LoggedIn, accts: Accounts, cache: UploadCache, chunk_of_three
) -> None:
    _, batch_id = validate(client, NAME, rows_csv(EIGHT))
    assert batch_id is not None
    org_id = count(accts, "SELECT organization_id FROM users WHERE id = :id", id=logged_in.user_id)
    entry = cache.get(org_id, batch_id)
    assert entry is not None
    assert (entry.row_count, entry.chunk_rows, entry.has_email) == (8, 3, False)
    assert [r.iccid for r in entry.records] == [synthetic_iccid(n) for n in EIGHT]


def test_failing_file_is_not_cached(client: TestClient, cache: UploadCache) -> None:
    lines = rows_csv(EIGHT).decode().splitlines()
    lines.append("8999000000000000001,not-an-lpa-code")
    response, batch_id = validate(client, NAME, ("\n".join(lines) + "\n").encode())
    assert response.status_code == 200 and batch_id is None
    assert len(cache) == 0


def test_parts_without_the_file_come_from_the_cache(
    client: TestClient, accts: Accounts, cache: UploadCache, monkeypatch, chunk_of_three
) -> None:
    _, batch_id = validate(client, NAME, rows_csv(EIGHT))
    no_parse(monkeypatch)
    for part, numbers in ((1, [1, 2, 3]), (2, [4, 5, 6]), (3, [7, 8])):
        response = generate_cached(client, batch_id, part, 3)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/zip"
        assert pdf_names(response) == expected_names(numbers)
        assert (batch_id in cache) is (part < 3)  # dropped once the batch is completed
    row = batch_row(accts, batch_id)
    assert (row["status"], row["parts_downloaded"]) == ("completed", 3)
    assert count(accts, "SELECT count(*) FROM code_fingerprints WHERE batch_id = :b", b=batch_id)
    assert count(accts, "SELECT sum(rows_generated) FROM usage") == 8


def test_cached_parts_must_match_the_batch(
    client: TestClient, cache: UploadCache, chunk_of_three
) -> None:
    _, batch_id = validate(client, NAME, rows_csv(EIGHT))
    for part, parts in ((1, 2), (4, 3), (0, 3)):
        response = generate_cached(client, batch_id, part, parts)
        assert response.status_code == 400 and routes.MSG_BAD_PART in response.text
    assert batch_id in cache  # a bad request does not drop the rows


# --- Misses and the re-upload fallback --------------------------------------------------------


def test_miss_answers_428_and_the_resent_file_refills_the_cache(
    client: TestClient, accts: Accounts, cache: UploadCache, monkeypatch, chunk_of_three
) -> None:
    data = rows_csv(EIGHT)
    _, batch_id = validate(client, NAME, data)
    cache.clear()  # e.g. a server restart

    missed = generate_cached(client, batch_id, 1, 3)
    assert missed.status_code == routes.UPLOAD_NEEDED_STATUS == 428
    assert missed.headers[routes.UPLOAD_NEEDED_HEADER] == "1"
    assert missed.headers["cache-control"] == "no-store"
    assert routes.MSG_UPLOAD_NEEDED in missed.text and "<html" in missed.text
    assert batch_row(accts, batch_id)["parts_downloaded"] == 0  # nothing recorded

    resent = generate(client, batch_id, NAME, data, part=1, parts=3)
    assert resent.status_code == 200 and pdf_names(resent) == expected_names([1, 2, 3])
    assert batch_id in cache

    no_parse(monkeypatch)
    hit = generate_cached(client, batch_id, 2, 3)
    assert hit.status_code == 200 and pdf_names(hit) == expected_names([4, 5, 6])


def test_expired_entry_is_a_miss(
    client: TestClient, cache: UploadCache, monkeypatch, chunk_of_three
) -> None:
    _, batch_id = validate(client, NAME, rows_csv(EIGHT))
    monkeypatch.setattr(cache, "ttl_seconds", -1.0)  # everything is past its TTL
    assert generate_cached(client, batch_id, 1, 3).status_code == 428
    assert batch_id not in cache


def test_cache_off_still_works_through_the_fallback(
    make_app, web_settings, accts: Accounts, chunk_of_three
) -> None:
    app = make_app(dataclasses.replace(web_settings, upload_cache_mb=0))
    user_id = accts.user()
    with LoggedIn(app, user_id, accts.session(user_id)).client() as client:
        data = rows_csv(EIGHT)
        _, batch_id = validate(client, NAME, data)
        assert generate_cached(client, batch_id, 1, 3).status_code == 428
        assert generate(client, batch_id, NAME, data, part=1, parts=3).status_code == 200
        assert len(app.state.upload_cache) == 0


# --- Organization scoping ----------------------------------------------------------------------


def test_another_organizations_batch_id_never_reaches_the_cache(
    client: TestClient, other_org: TestClient, cache: UploadCache, monkeypatch, chunk_of_three
) -> None:
    _, theirs = validate(other_org, NAME, rows_csv(EIGHT))
    assert theirs is not None and theirs in cache
    lookups: list[tuple[int, int]] = []
    real_get = cache.get

    def spy(organization_id: int, batch_id: int):
        lookups.append((organization_id, batch_id))
        return real_get(organization_id, batch_id)

    monkeypatch.setattr(cache, "get", spy)
    stolen = generate_cached(client, theirs, 1, 3)
    unknown = generate_cached(client, theirs + 1000, 1, 3)
    for response in (stolen, unknown):
        assert response.status_code == 404 and routes.MSG_BATCH_GONE in response.text
        assert "application/zip" not in response.headers["content-type"]
    assert stolen.text == unknown.text
    assert lookups == []  # 404 before any cache lookup
    assert theirs in cache  # their rows are untouched

    monkeypatch.setattr(cache, "get", real_get)
    assert generate_cached(other_org, theirs, 1, 3).status_code == 200  # still theirs


# --- Dropped on failure and void ----------------------------------------------------------------


def test_rows_dropped_when_another_batch_took_the_codes(
    client: TestClient, accts: Accounts, cache: UploadCache
) -> None:
    data = rows_csv([1, 2, 3])
    _, first = validate(client, "first.csv", data)
    _, second = validate(client, "second.csv", data)  # nothing generated yet: both pass
    assert first is not None and second is not None and first != second
    assert generate_cached(client, second, 1, 1).status_code == 200  # the codes are used now

    raced = generate_cached(client, first, 1, 1)  # cache hit; record_part finds them
    assert raced.status_code == 422
    assert "Already used in batch" in raced.text
    assert batch_row(accts, first)["status"] == "failed"
    assert first not in cache

    again = generate_cached(client, first, 1, 1)
    assert again.status_code == 428  # rows dropped: the next try checks the whole file
    full = generate(client, first, "first.csv", data)
    assert full.status_code == 422 and "Already used in batch" in full.text
    assert first not in cache


def test_rows_dropped_when_the_batch_is_voided(
    web_app: FastAPI, accts: Accounts, cache: UploadCache, chunk_of_three
) -> None:
    org_id = accts.org("Alpha (test)")
    owner_id = accts.user("owner@alpha.test", org_id=org_id, role=Role.OWNER)
    with LoggedIn(web_app, owner_id, accts.session(owner_id)).client(
        follow_redirects=False
    ) as owner:
        _, batch_id = validate(owner, NAME, rows_csv(EIGHT))
        assert generate_cached(owner, batch_id, 1, 3).status_code == 200
        assert batch_id in cache
        voided = owner.post(
            f"/history/batches/{batch_id}/void", data={history.CONFIRM_FIELD: "yes"}
        )
        assert voided.status_code == 303
        assert batch_id not in cache
        after = generate_cached(owner, batch_id, 2, 3)
        assert after.status_code == 409 and routes.MSG_BATCH_VOIDED in after.text


def test_continued_batch_is_cached_with_its_own_part_size(
    client: TestClient, cache: UploadCache, monkeypatch, chunk_of_three
) -> None:
    data = rows_csv(EIGHT)
    _, batch_id = validate(client, NAME, data)
    assert generate_cached(client, batch_id, 1, 3).status_code == 200
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 4)  # e.g. a later release
    cache.clear()
    response, continued = validate(client, NAME, data)
    assert continued == batch_id and "Continue this batch" in response.text
    entry = cache.get(cache_entry_org(cache, batch_id), batch_id)
    assert entry is not None and entry.chunk_rows == 3
    second = generate_cached(client, batch_id, 2, 3)
    assert second.status_code == 200 and pdf_names(second) == expected_names([4, 5, 6])


def cache_entry_org(cache: UploadCache, batch_id: int) -> int:
    """The organization of a cached entry (test helper; reads the private mapping)."""
    entry, _ = cache._entries[batch_id]
    return entry.organization_id


# --- No codes in logs or repr ------------------------------------------------------------------


def test_no_activation_code_in_logs_or_repr(
    client: TestClient, cache: UploadCache, caplog: pytest.LogCaptureFixture, chunk_of_three
) -> None:
    data = rows_csv(EIGHT)
    with caplog.at_level(logging.INFO):
        _, batch_id = validate(client, NAME, data)
        cache.clear()
        assert generate_cached(client, batch_id, 1, 3).status_code == 428
        assert generate(client, batch_id, NAME, data, part=1, parts=3).status_code == 200
        assert generate_cached(client, batch_id, 2, 3).status_code == 200
        logging.getLogger("test").info("cache state: %r %s", cache, cache)
    texts = [caplog.text, repr(cache), str(cache)]
    entry = cache.get(cache_entry_org(cache, batch_id), batch_id)
    assert entry is not None
    texts.append(repr(entry))
    for text in texts:
        assert "LPA:" not in text and "TEST-" not in text
