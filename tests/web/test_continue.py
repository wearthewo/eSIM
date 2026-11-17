"""Continuing an unfinished batch: a re-upload of its own file is not a duplicate.

Parts are 3 rows here (``chunk_of_three``), so an 8-row file has parts of rows 1-3, 4-6
and 7-8 (file rows 2-4, 5-7 and 8-9). All data is synthetic.
"""

from __future__ import annotations

import io
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.core import pipeline, zipper
from app.localtime import format_athens
from tests.conftest import read_xlsx
from tests.web.conftest import Accounts
from tests.web.test_batches import (
    batch_row,
    count,
    execute,
    generate,
    rows_csv,
    squash,
    validate,
)

NAME = "order.csv"
EIGHT = list(range(1, 9))  # three parts: [1, 2, 3], [4, 5, 6], [7, 8]


@pytest.fixture
def chunk_of_three(monkeypatch) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)


def started(client: TestClient, numbers: list[int], parts_done: tuple[int, ...] = (1,)) -> int:
    """A batch for ``numbers`` with ``parts_done`` generated; returns its id."""
    data = rows_csv(numbers)
    _, batch_id = validate(client, NAME, data)
    assert batch_id is not None
    parts = pipeline.part_count(len(numbers))
    for part in parts_done:
        response = generate(client, batch_id, NAME, data, part=part, parts=parts)
        assert response.status_code == 200, response.text
    return batch_id


def continue_note(html: str) -> str | None:
    match = re.search(r'id="continue-batch"><div>\s*<p><strong>(.*?)</strong>', html, re.S)
    return " ".join(match.group(1).split()) if match else None


def download_button(html: str) -> str:
    match = re.search(r'<button type="submit" id="download"[^>]*>[^<]*</button>', html)
    assert match, "no download button"
    return match.group(0)


def manifest_rows(response) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        return [row[0] for row in read_xlsx(zf.read(zipper.MANIFEST_NAME))[1:]]


def assert_blocked(html: str) -> None:
    assert continue_note(html) is None
    assert 'id="used-before"' in html and 'id="download"' not in html


# --- The match --------------------------------------------------------------------------------


def test_reupload_continues_the_batch(client: TestClient, accts: Accounts, chunk_of_three) -> None:
    batch_id = started(client, EIGHT)
    created = accts.sql("SELECT created_at FROM batches WHERE id = :id", id=batch_id)[0][0]

    response, continued = validate(client, "order (copy).csv", rows_csv(EIGHT))
    html = response.text
    assert response.status_code == 200 and continued == batch_id
    assert continue_note(html) == (
        f"This file matches your unfinished batch '{NAME}' from "
        f"{format_athens(created)} (1 / 3 parts downloaded)."
    )
    assert download_button(html) == (
        '<button type="submit" id="download" data-parts="3" data-downloaded="1">'
        "Continue this batch</button>"
    )
    assert "8 rows read: 0 errors, 0 warnings." in html
    assert 'id="used-before"' not in html and "Already used" not in html
    assert count(accts, "SELECT count(*) FROM batches") == 1  # no new batch

    data = rows_csv(EIGHT)
    for part in (2, 3, 1, 3):  # the rest, then "download again"
        assert generate(client, batch_id, NAME, data, part=part, parts=3).status_code == 200
    row = batch_row(accts, batch_id)
    assert (row["status"], row["parts_downloaded"]) == ("completed", 3)
    assert count(accts, "SELECT sum(rows_generated) FROM usage") == 8  # each part once
    assert count(accts, "SELECT count(*) FROM batch_parts") == 3


def test_parts_already_downloaded_are_listed(client: TestClient, chunk_of_three) -> None:
    batch_id = started(client, list(range(1, 11)), parts_done=(1, 3))  # 4 parts
    response, continued = validate(client, NAME, rows_csv(list(range(1, 11))))
    assert continued == batch_id
    assert 'data-parts="4" data-downloaded="1,3"' in download_button(response.text)
    assert "(2 / 4 parts downloaded)" in continue_note(response.text)


def test_incomplete_batch_continues(client: TestClient, accts: Accounts, chunk_of_three) -> None:
    batch_id = started(client, EIGHT)
    execute(accts, "UPDATE batches SET status = 'incomplete' WHERE id = :id", id=batch_id)
    _, continued = validate(client, NAME, rows_csv(EIGHT))
    assert continued == batch_id
    assert generate(client, batch_id, NAME, rows_csv(EIGHT), part=2, parts=3).status_code == 200
    assert batch_row(accts, batch_id)["status"] == "partial"


def test_rows_reordered_within_their_part_still_continue(
    client: TestClient, chunk_of_three
) -> None:
    batch_id = started(client, EIGHT)
    _, continued = validate(client, NAME, rows_csv([3, 1, 2, 6, 5, 4, 8, 7]))
    assert continued == batch_id


# --- Not a match: the duplicates stand ------------------------------------------------------------


def test_downloaded_row_in_another_part_is_an_error(
    client: TestClient, accts: Accounts, chunk_of_three
) -> None:
    started(client, EIGHT)
    response, batch_id = validate(client, NAME, rows_csv([4, 1, 2, 3, 5, 6, 7, 8]))
    assert batch_id is None  # row 3 (number 3) moved from part 1 to part 2
    assert_blocked(response.text)
    assert count(accts, "SELECT count(*) FROM batches") == 1


def test_downloaded_row_replaced_is_an_error(client: TestClient, chunk_of_three) -> None:
    started(client, EIGHT)
    response, batch_id = validate(client, NAME, rows_csv([1, 2, 99, 4, 5, 6, 7, 8]))
    assert batch_id is None
    assert_blocked(response.text)


def test_different_row_count_is_an_error(client: TestClient, chunk_of_three) -> None:
    started(client, EIGHT)
    response, batch_id = validate(client, NAME, rows_csv([*EIGHT, 9]))
    assert batch_id is None
    assert_blocked(response.text)


def test_completed_batch_is_an_error(client: TestClient, chunk_of_three) -> None:
    started(client, EIGHT, parts_done=(1, 2, 3))
    response, batch_id = validate(client, NAME, rows_csv(EIGHT))
    assert batch_id is None
    assert_blocked(response.text)


def test_failed_batch_is_an_error(client: TestClient, accts: Accounts, chunk_of_three) -> None:
    batch_id = started(client, EIGHT)
    execute(accts, "UPDATE batches SET status = 'failed' WHERE id = :id", id=batch_id)
    response, continued = validate(client, NAME, rows_csv(EIGHT))
    assert continued is None
    assert_blocked(response.text)


def test_rows_of_two_batches_are_errors(client: TestClient, chunk_of_three) -> None:
    started(client, [1, 2, 3, 4, 5, 6])  # part 1 = numbers 1-3
    started(client, [11, 12, 13, 14, 15, 16])  # part 1 = numbers 11-13
    response, batch_id = validate(client, NAME, rows_csv([1, 2, 3, 11, 12, 13]))
    assert batch_id is None
    assert_blocked(response.text)
    assert "6 rows were already used in earlier batches" in squash(response.text)


def test_other_errors_in_the_file_prevent_continuing(client: TestClient, chunk_of_three) -> None:
    started(client, EIGHT)
    lines = rows_csv(EIGHT).decode().splitlines()
    lines[-1] = lines[-1].replace("LPA:1$", "NOPE$")  # a broken activation code in part 3
    response, batch_id = validate(client, NAME, ("\n".join(lines) + "\n").encode())
    assert batch_id is None
    assert continue_note(response.text) is None
    assert "Already used" in response.text


def test_fingerprints_without_a_part_never_continue(
    client: TestClient, accts: Accounts, chunk_of_three
) -> None:
    started(client, EIGHT)
    execute(accts, "UPDATE code_fingerprints SET part = NULL")  # recorded before 0005
    response, batch_id = validate(client, NAME, rows_csv(EIGHT))
    assert batch_id is None
    assert_blocked(response.text)


# --- The part size belongs to the batch ----------------------------------------


def test_changed_chunk_size_keeps_the_batch_parts(
    client: TestClient, accts: Accounts, monkeypatch
) -> None:
    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 3)
    batch_id = started(client, EIGHT)
    assert batch_row(accts, batch_id)["chunk_rows"] == 3

    monkeypatch.setattr(pipeline, "GENERATE_CHUNK_ROWS", 4)  # e.g. a later release
    response, continued = validate(client, NAME, rows_csv(EIGHT))
    assert continued == batch_id
    assert 'data-parts="3" data-downloaded="1"' in download_button(response.text)
    assert 'data-chunk-rows="3"' in response.text

    data = rows_csv(EIGHT)
    part2 = generate(client, batch_id, NAME, data, part=2, parts=3)
    assert part2.status_code == 200 and manifest_rows(part2) == ["5", "6", "7"]
    part3 = generate(client, batch_id, NAME, data, part=3, parts=3)
    assert part3.status_code == 200 and manifest_rows(part3) == ["8", "9"]
    assert generate(client, batch_id, NAME, data, part=2, parts=2).status_code == 400
    assert batch_row(accts, batch_id)["status"] == "completed"

    # A new upload uses the new size.
    response, new_id = validate(client, "new.csv", rows_csv(list(range(101, 109))))
    assert new_id is not None and new_id != batch_id
    assert 'data-parts="2"' in download_button(response.text)
    assert batch_row(accts, new_id)["chunk_rows"] == 4
