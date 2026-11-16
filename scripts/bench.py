"""Benchmark the upload pipeline (parse -> validate -> render -> zip) on synthetic data.

Usage (from the repo root):
    .venv\\Scripts\\python.exe scripts\\bench.py --rows 500
    .venv\\Scripts\\python.exe scripts\\bench.py --rows 10000              (20 parts)
    .venv\\Scripts\\python.exe scripts\\bench.py --rows 10000 --unique-names

The run simulates what the web page does: it asks for every part of the upload,
one after another, and each request carries the whole CSV, which is parsed and
validated again before that part's rows are rendered. Up to 500 rows that is a
single part; 10,000 rows are 20 parts.

Everything stays in memory; nothing is written to disk. Data is synthetic only
(see ``make_big_csv.py``). Never point this at real data.

Name modes (``--names``):
  same    every row has the same Greek name: best case for the font-subset cache
  pool    names from a pool of Greek first names and surnames, like a real order
  unique  every row has a different set of Greek characters: worst case
          (``--unique-names`` is short for ``--names unique``)
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import segno  # noqa: E402
from make_big_csv import make_csv, unique_charset_name  # noqa: E402

from app.core import pipeline  # noqa: E402
from app.core.models import Branding  # noqa: E402
from app.core.parser import MAX_ROWS  # noqa: E402

NAME_MODES = {
    "same": "same name every row",
    "pool": "names from a pool",
    "unique": "unique Greek character set per row",
}


def make_branding() -> Branding:
    logo = io.BytesIO()
    segno.make_qr("logo").save(logo, kind="png", scale=20, dark="#1A73E8")  # any PNG logo
    return Branding(
        company_name="Example Telecom (bench)",
        logo_png=logo.getvalue(),
        primary_hex="#1A73E8",
        footer_text="Support: help@example.invalid",
    )


def run_parts(data: bytes, branding: Branding) -> list[tuple[int, float, int, bytes]]:
    """Generate every part in order; return ``(rows, seconds, zip_bytes, sha256)`` per part."""
    parsed, report = pipeline.check_upload(data, "bench.csv")
    if not report.ok:
        raise SystemExit(f"validation failed: {len(report.errors)} error(s)")
    rows = len(parsed.records)
    parts = pipeline.part_count(rows)
    out: list[tuple[int, float, int, bytes]] = []
    for part in range(1, parts + 1):
        start = time.perf_counter()
        _, archive = pipeline.process_upload(data, "bench.csv", branding, part=part, parts=parts)
        seconds = time.perf_counter() - start
        if archive is None:
            raise SystemExit("validation failed")
        part_rows = min(
            pipeline.GENERATE_CHUNK_ROWS, rows - (part - 1) * pipeline.GENERATE_CHUNK_ROWS
        )
        out.append((part_rows, seconds, len(archive), hashlib.sha256(archive).digest()))
    return out


def time_validation(data: bytes, repeat: int = 3) -> float:
    """Best time, in seconds, to parse and validate the whole upload (no rendering)."""
    best = float("inf")
    for _ in range(repeat):
        start = time.perf_counter()
        pipeline.check_upload(data, "bench.csv")
        best = min(best, time.perf_counter() - start)
    return best


def main() -> int:
    args = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    args.add_argument("--rows", type=int, default=500, help=f"data rows, 1-{MAX_ROWS}")
    args.add_argument("--names", choices=sorted(NAME_MODES), default="same", help="see above")
    args.add_argument("--unique-names", action="store_true", help="same as --names unique")
    args.add_argument(
        "--repeat",
        type=int,
        default=None,
        help="timed runs, the best is reported (default: 3 for one part, 1 for several)",
    )
    opts = args.parse_args()
    if not 1 <= opts.rows <= MAX_ROWS:
        args.error(f"--rows must be between 1 and {MAX_ROWS}")
    names = "unique" if opts.unique_names else opts.names
    parts = pipeline.part_count(opts.rows)
    repeat = opts.repeat or (3 if parts == 1 else 1)

    # One CSV per timed run. With unique names each run uses a fresh range of name
    # keys, so no run can hit font subsets cached by an earlier one.
    uploads = [make_csv(opts.rows, names, first_key=1 + run * opts.rows) for run in range(repeat)]
    if names == "unique":
        keys = range(1, 1 + repeat * opts.rows)
        if len({frozenset(unique_charset_name(k)) for k in keys}) != len(keys):
            raise SystemExit("character sets are not unique")
    branding = make_branding()

    # Warm-up: imports, font registration, QR layout tables (names outside the timed ranges).
    run_parts(make_csv(20, "same"), branding)

    validation = time_validation(uploads[0])
    runs = [run_parts(data, branding) for data in uploads]
    totals = [sum(seconds for _, seconds, _, _ in run) for run in runs]
    best = runs[totals.index(min(totals))]
    total_seconds = min(totals)
    total_bytes = sum(size for _, _, size, _ in best)
    digest = hashlib.sha256(b"".join(sha for _, _, _, sha in best)).hexdigest()[:16]

    print(f"rows: {opts.rows} ({NAME_MODES[names]}), parts: {parts}")  # noqa: T201
    print(f"upload: {len(uploads[0]):,} bytes CSV")  # noqa: T201
    print(f"validation only (parse + validate, whole file): {validation * 1000:.0f} ms")  # noqa: T201
    print(f"runs (ms): {[round(t * 1000) for t in totals]}")  # noqa: T201
    print(  # noqa: T201
        f"total: {total_seconds:.2f} s, {total_seconds * 1000 / opts.rows:.2f} ms per PDF "
        "(each part includes re-validating the whole file)"
    )
    print(  # noqa: T201
        f"ZIP size: {total_bytes:,} bytes, {total_bytes / opts.rows / 1024:.1f} KB per PDF, "
        f"sha256 {digest}"
    )
    if parts > 1:
        seconds = [s for _, s, _, _ in best]
        sizes = [size for _, _, size, _ in best]
        print(  # noqa: T201
            f"per part: time {min(seconds):.2f}-{max(seconds):.2f} s "
            f"(mean {sum(seconds) / parts:.2f} s), "
            f"size {min(sizes) / 1e6:.2f}-{max(sizes) / 1e6:.2f} MB "
            f"(mean {sum(sizes) / parts / 1e6:.2f} MB)"
        )
        for number, (part_rows, part_seconds, size, _) in enumerate(best, start=1):
            print(f"  part {number:02d}: {part_rows} rows, {part_seconds:.2f} s, {size:,} bytes")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
