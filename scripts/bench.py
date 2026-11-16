"""Benchmark the upload pipeline (parse -> validate -> render -> zip) on synthetic data.

Usage (from the repo root):
    .venv\\Scripts\\python.exe scripts\\bench.py --rows 500 --unique-names

Everything stays in memory; nothing is written to disk. Data is synthetic only:
ICCIDs use the fake ``8999`` prefix and activation codes point at
``smdp.example.invalid``. Never point this at real data.

By default every row has the same Greek customer name, which is the best case
for the per-subset font cache in ``render.py``. ``--unique-names`` gives every
row a different set of Greek characters, so the bold-font subset differs per
PDF and the font cache misses (worst case).
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

import segno  # noqa: E402

from app.core import pipeline  # noqa: E402
from app.core.models import Branding  # noqa: E402
from app.core.parser import MAX_ROWS  # noqa: E402

PLAN = "Ταξίδι 10GB"
# Greek letters (with and without tonos) used to build per-row character sets.
# Letters of PLAN are left out so the set of non-ASCII characters drawn in the
# bold font (name + plan) is unique per row, not just the name's.
GREEK = "".join(
    ch for ch in "αβγδεζηθικλμνξοπρστυφχψωάέήίόύώΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ" if ch not in PLAN
)


def luhn_check_digit(payload: str) -> str:
    total = 0
    for i, ch in enumerate(reversed(payload)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def synthetic_iccid(n: int) -> str:
    payload = f"8999{n:014d}"
    return payload + luhn_check_digit(payload)


def customer_name(key: int | None) -> str:
    """The shared default name, or (``key`` >= 1) the ``GREEK`` letters picked by its bits.

    Distinct keys give distinct character sets, for keys below 2**len(GREEK).
    """
    if key is None:
        return "Γιώργος Παπαδόπουλος"
    return "".join(ch for k, ch in enumerate(GREEK) if (key >> k) & 1)


def make_csv(rows: int, name_keys: range | None) -> bytes:
    lines = ["iccid,activation_code,confirmation_code,customer_name,plan_name"]
    for n in range(1, rows + 1):
        name = customer_name(None if name_keys is None else name_keys[n - 1])
        lines.append(
            f"{synthetic_iccid(n)},LPA:1$smdp.example.invalid$TEST-{n:04d}-AAAA-BBBB,"
            f"CONF{n:04d},{name},{PLAN}"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def make_branding() -> Branding:
    logo = io.BytesIO()
    segno.make_qr("logo").save(logo, kind="png", scale=20, dark="#1A73E8")  # any PNG logo
    return Branding(
        company_name="Example Telecom (bench)",
        logo_png=logo.getvalue(),
        primary_hex="#1A73E8",
        footer_text="Support: help@example.invalid",
    )


def main() -> int:
    args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    args.add_argument("--rows", type=int, default=500, help=f"data rows, 1-{MAX_ROWS}")
    args.add_argument(
        "--unique-names",
        action="store_true",
        help="give every row a different set of Greek characters (font cache worst case)",
    )
    args.add_argument("--repeat", type=int, default=3, help="timed runs; the best is reported")
    opts = args.parse_args()
    if not 1 <= opts.rows <= MAX_ROWS:
        args.error(f"--rows must be between 1 and {MAX_ROWS}")

    # One CSV per timed run (plus a warm-up). With --unique-names each run uses a
    # fresh range of name keys, so no run can hit subsets cached by an earlier one.
    def name_keys(run: int) -> range | None:
        return range(1 + run * opts.rows, 1 + (run + 1) * opts.rows) if opts.unique_names else None

    uploads = [make_csv(opts.rows, name_keys(run)) for run in range(opts.repeat + 1)]
    if opts.unique_names:
        all_keys = range(1, 1 + (opts.repeat + 1) * opts.rows)
        if len({frozenset(customer_name(k)) for k in all_keys}) != len(all_keys):
            raise SystemExit("character sets are not unique")
    branding = make_branding()

    def run(data: bytes) -> bytes:
        report, archive = pipeline.process_upload(data, "bench.csv", branding)
        if archive is None:
            raise SystemExit(f"validation failed: {len(report.errors)} error(s)")
        return archive

    run(uploads[0])  # warm-up: imports, font registration, QR layout tables
    times: list[float] = []
    archive = b""
    for data in uploads[1:]:
        start = time.perf_counter()
        archive = run(data)
        times.append(time.perf_counter() - start)
    best = min(times)
    mode = "unique Greek character set per row" if opts.unique_names else "same name every row"
    print(f"rows: {opts.rows} ({mode})")  # noqa: T201
    print(f"runs (ms): {[round(t * 1000) for t in times]}")  # noqa: T201
    print(f"total: {best * 1000:.0f} ms, {best * 1000 / opts.rows:.2f} ms per PDF")  # noqa: T201
    print(f"ZIP size: {len(archive):,} bytes, sha256 {hashlib.sha256(archive).hexdigest()[:16]}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
