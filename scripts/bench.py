"""Benchmark a large order: parse -> validate -> duplicate check -> parts, on synthetic data.

Usage (from the repo root):
    .venv\\Scripts\\python.exe scripts\\bench.py --rows 10000                 (end to end)
    .venv\\Scripts\\python.exe scripts\\bench.py --rows 100000 --names pool   (local only)
    .venv\\Scripts\\python.exe scripts\\bench.py --rows 10000 --chunk 1000
    .venv\\Scripts\\python.exe scripts\\bench.py --core --rows 500           (no database)

End to end (default; needs TEST_DATABASE_URL in .env): the real app (``create_app``)
on the TEST database, driven through ``TestClient`` like the page drives it. POST
/validate once (parse, validate, duplicate check against earlier batches, a new batch,
the rows into the upload cache), then POST /generate for every part without the file
(cache hits; ``record_part`` checks each part's rows again under the organization's
lock). Before part ``--miss-part`` the cache is cleared, so that part shows the
fallback: 428, then the same part again with the file. The script refuses to run when
TEST_DATABASE_URL is the DATABASE_URL database, and empties it before and after
(``bench_duplicates.py``). ``--rows`` above the product limit (10,000) raises the
app's MAX_ROWS and MAX_UPLOAD_MB settings for this run only.

``--core``: no database, no HTTP. Every part re-parses and re-validates the whole file
(``pipeline.process_upload``), as the app did before the upload cache. Up to 10,000
rows. Run it once on the target machine (e.g. a Render shell) to calibrate the
estimates below.

Estimates for Render: rendering is single-threaded Python, so CPU time is what counts.
The script measures the process's CPU time per stage and estimates
    wall time = (local wall - local CPU) + local CPU / CPU share x core factor
with a CPU share of 0.5 (Starter) or 1.0 (Standard) and a core factor of 1.0-1.5
(a shared cloud vCPU against this machine's core). Calibrate the factor with ``--core
--rows 500`` on the instance: factor = its ms per PDF x share / this machine's.

Everything stays in memory; nothing is written to disk except the database's metadata
rows (batches, fingerprints, usage), which are deleted at the end. Data is synthetic
only (``make_big_csv.py``). Never point this at real data.

Name modes (``--names``):
  same    every row has the same Greek name
  pool    names from a pool of Greek first names and surnames, like a real order
  unique  every row has a different set of Greek characters: worst case (largest
          embedded font subsets; ``--unique-names`` is short for ``--names unique``)
  ascii   ASCII names and plans: no embedded font
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import io
import math
import re
import secrets
import sys
import time
import tracemalloc
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import segno  # noqa: E402
from make_big_csv import MAX_SCRIPT_ROWS, make_csv, unique_charset_name  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from app.core import pipeline, render  # noqa: E402
from app.core.models import Branding  # noqa: E402
from app.core.parser import MAX_ROWS  # noqa: E402

NAME_MODES = {
    "same": "same Greek name every row",
    "pool": "Greek names from a pool",
    "unique": "unique Greek character set per row",
    "ascii": "ASCII names",
}
COMPANY = "Example Telecom (bench)"
PRIMARY = "#1A73E8"
FOOTER = "Support: help@example.invalid"
SHARES = {"Starter (0.5 CPU)": 0.5, "Standard (1 CPU)": 1.0}
CORE_FACTORS = (1.0, 1.5)
MB = 1024 * 1024
_BATCH_INPUT = re.compile(r'<input type="hidden" name="batch_id" value="([0-9]+)">')


# --- Logos --------------------------------------------------------------------------------


def qr_logo() -> bytes:
    """The logo earlier benchmarks used (README): a 580 x 580 QR-code PNG."""
    logo = io.BytesIO()
    segno.make_qr("logo").save(logo, kind="png", scale=20, dark="#1A73E8")
    return logo.getvalue()


def flat_logo() -> bytes:
    """A 1600 x 600 RGBA logo: transparent background, shapes and a word."""
    image = Image.new("RGBA", (1600, 600), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((40, 60, 520, 540), fill=(26, 115, 232, 255))
    draw.ellipse((160, 180, 400, 420), fill=(255, 255, 255, 255))
    draw.rectangle((260, 60, 300, 540), fill=(251, 188, 4, 255))
    font = ImageFont.truetype(str(render.FONT_DIR / "DejaVuSans.ttf"), 220)
    draw.text((580, 150), "Example", font=font, fill=(32, 33, 36, 255))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


LOGOS = {"qr": qr_logo, "flat": flat_logo}


def make_branding(logo: bytes) -> Branding:
    return Branding(company_name=COMPANY, logo_png=logo, primary_hex=PRIMARY, footer_text=FOOTER)


# --- Measuring ----------------------------------------------------------------------------


def peak_memory_bytes() -> int:
    """The process's peak resident memory so far (Windows: peak working set)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        psapi = ctypes.WinDLL("psapi")
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        return int(counters.PeakWorkingSetSize)
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # Linux: KiB


@dataclass
class Stage:
    """Wall and CPU seconds of one measured step."""

    wall: float = 0.0
    cpu: float = 0.0

    def __iadd__(self, other: Stage) -> Stage:
        self.wall += other.wall
        self.cpu += other.cpu
        return self


class timed:  # noqa: N801  (used as a context manager: ``with timed() as t``)
    def __enter__(self) -> Stage:
        self.stage = Stage()
        self._wall, self._cpu = time.perf_counter(), time.process_time()
        return self.stage

    def __exit__(self, *exc: object) -> None:
        self.stage.wall = time.perf_counter() - self._wall
        self.stage.cpu = time.process_time() - self._cpu


def estimate(stage: Stage, share: float, factor: float) -> float:
    """Estimated wall seconds on an instance with ``share`` of a CPU (see module doc)."""
    waiting = max(0.0, stage.wall - stage.cpu)  # database, sockets
    return waiting + stage.cpu / share * factor


def fmt_s(seconds: float) -> str:
    if seconds >= 120:
        return f"{seconds / 60:.1f} min"
    return f"{seconds:.1f} s"


def cached_rows_memory(data: bytes, max_rows: int) -> tuple[int, int]:
    """(bytes measured by tracemalloc, bytes the cache estimates) for the checked rows
    of ``data`` as the upload cache holds them (a tuple of the valid records)."""
    from app.upload_cache import estimate_bytes

    gc.collect()
    tracemalloc.start()
    try:
        parsed, report = pipeline.check_upload(data, "bench.csv", max_rows=max_rows)
        records = tuple(report.valid)
        del parsed, report
        gc.collect()
        current, _ = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return current, estimate_bytes(records)


# --- End to end ---------------------------------------------------------------------------


@dataclass
class PartResult:
    part: int
    rows: int
    stage: Stage
    size: int
    missed: bool = False  # this part went through the 428 + re-upload fallback
    miss_stage: Stage | None = None  # the 428 answer alone


@dataclass
class EndToEnd:
    rows: int
    chunk_rows: int
    upload_bytes: int
    check: Stage  # parse + validate, outside the app
    duplicate: Stage  # find_used on every row
    validate: Stage  # POST /validate, whole request
    parts: list[PartResult] = field(default_factory=list)
    peak_bytes: int = 0
    start_bytes: int = 0
    cache_measured: int = 0
    cache_estimated: int = 0
    digest: str = ""


def run_end_to_end(opts: argparse.Namespace, data: bytes, logo: bytes) -> EndToEnd:
    from bench_duplicates import empty, seed, test_engine
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker

    from app import batches
    from app.auth import accounts
    from app.auth.tokens import csrf_for
    from app.settings import Secret, Settings
    from app.web import routes
    from app.web.auth import CSRF_HEADER, SESSION_COOKIE
    from app.web.main import create_app

    max_rows = max(opts.rows, MAX_ROWS)
    max_upload_mb = max(5, math.ceil(len(data) / MB) + 1)
    if opts.chunk:
        pipeline.GENERATE_CHUNK_ROWS = opts.chunk  # new batches take today's part size
    chunk_rows = pipeline.GENERATE_CHUNK_ROWS

    engine = test_engine()
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    key = secrets.token_urlsafe(48)
    settings = Settings(
        env="development",
        database_url=Secret(engine.url.render_as_string(hide_password=False)),
        secret_key=Secret(secrets.token_urlsafe(48)),
        fingerprint_key=Secret(key),
        public_base_url="http://testserver",
        generate_slots=1,
        max_rows=max_rows,
        max_upload_mb=max_upload_mb,
    )
    try:
        org_id, user_id = seed(engine, sessions, opts.fingerprints)
        if opts.fingerprints:
            print(f"seeded {opts.fingerprints:,} earlier fingerprints")  # noqa: T201
        app = create_app(settings, sessions=sessions, run_retention=False)
        cache = app.state.upload_cache
        with sessions.begin() as db:
            token = accounts.create_session(db, user_id, now=datetime.now(UTC))
        client = TestClient(app, follow_redirects=False)
        client.cookies.set(SESSION_COOKIE, token)
        client.headers[CSRF_HEADER] = csrf_for(token)
        # Warm-up: imports, fonts, templates, QR tables (a separate, small batch).
        warm = make_csv(20, "same", first_key=10**6)
        warm_page = client.post("/validate", files={"file": ("warm.csv", warm, "text/csv")})
        warm_id = int(_BATCH_INPUT.search(warm_page.text).group(1))
        _post_part(client, warm_id, 1, 1, logo)
        with engine.begin() as conn:  # its rows (1-20) are the order's first rows too
            conn.execute(text("DELETE FROM code_fingerprints WHERE batch_id = :b"), {"b": warm_id})

        start_bytes = peak_memory_bytes()
        with timed() as check:
            _, report = pipeline.check_upload(data, "bench.csv", max_rows=max_rows)
        with timed() as duplicate:
            with sessions.begin() as db:
                used = batches.find_used(
                    db, org_id, report.valid, key=key.encode(), now=datetime.now(UTC)
                )
        if used.found:
            raise SystemExit("the synthetic rows were already used")
        del report, used

        files = {"file": ("bench.csv", data, "text/csv")}
        with timed() as validate:
            page = client.post("/validate", files=files)
        match = _BATCH_INPUT.search(page.text)
        if page.status_code != 200 or match is None:
            raise SystemExit(f"/validate did not pass (HTTP {page.status_code})")
        batch_id = int(match.group(1))
        parts = pipeline.part_count(opts.rows, chunk_rows)
        result = EndToEnd(opts.rows, chunk_rows, len(data), check, duplicate, validate)

        digest = hashlib.sha256()
        for part in range(1, parts + 1):
            rows = min(chunk_rows, opts.rows - (part - 1) * chunk_rows)
            missed = part == opts.miss_part
            if missed:
                cache.clear()  # as after a restart
            miss_stage = None
            with timed() as stage:
                with timed() as first:
                    response = _post_part(client, batch_id, part, parts, logo)
                if response.status_code == routes.UPLOAD_NEEDED_STATUS:
                    miss_stage = first
                    response = _post_part(client, batch_id, part, parts, logo, data=data)
            if missed != (miss_stage is not None):
                raise SystemExit(f"part {part}: cache miss expected={missed}")
            if response.status_code != 200:
                raise SystemExit(f"part {part}: HTTP {response.status_code}")
            digest.update(hashlib.sha256(response.content).digest())
            result.parts.append(
                PartResult(part, rows, stage, len(response.content), missed, miss_stage)
            )
            del response
        result.digest = digest.hexdigest()[:16]
        result.peak_bytes = peak_memory_bytes()
        result.start_bytes = start_bytes
        client.close()
    finally:
        empty(engine)
        engine.dispose()
    result.cache_measured, result.cache_estimated = cached_rows_memory(data, max_rows)
    return result


def _post_part(client, batch_id: int, part: int, parts: int, logo: bytes, data: bytes = b""):
    """POST /generate as app.js does: without the data file, unless ``data`` is given."""
    form = {
        "batch_id": str(batch_id),
        "part": str(part),
        "parts": str(parts),
        "company_name": COMPANY,
        "primary_color": PRIMARY,
        "footer_text": FOOTER,
        "client_stamp": "21112026_1200",
    }
    files = {"logo": ("logo.png", logo, "image/png")}
    if data:
        files["file"] = ("bench.csv", data, "text/csv")
    return client.post("/generate", data=form, files=files)


def report_end_to_end(opts: argparse.Namespace, r: EndToEnd) -> None:
    parts = len(r.parts)
    render_stage = Stage()
    for p in r.parts:
        render_stage += p.stage
    total = Stage(r.validate.wall + render_stage.wall, r.validate.cpu + render_stage.cpu)
    hits = [p for p in r.parts if not p.missed]
    hit_rows = sum(p.rows for p in hits)
    hit_wall = sum(p.stage.wall for p in hits)
    size = sum(p.size for p in r.parts)
    out = print  # noqa: T202
    out(f"rows: {r.rows:,} ({NAME_MODES[opts.names]}), {parts} parts of {r.chunk_rows:,}")
    out(f"upload: {r.upload_bytes:,} bytes CSV ({r.upload_bytes / MB:.1f} MB)")
    out(f"logo: {opts.logo}")
    out(f"parse + validate (whole file):     {r.check.wall * 1000:9.0f} ms")
    out(f"duplicate check (HMAC + lookups):  {r.duplicate.wall * 1000:9.0f} ms")
    out(f"POST /validate (whole request):    {r.validate.wall * 1000:9.0f} ms")
    out(
        f"parts from the cache: {hit_wall:.1f} s for {hit_rows:,} PDFs, "
        f"{hit_wall * 1000 / max(1, hit_rows):.2f} ms per PDF (render + ZIP + record_part + HTTP)"
    )
    for p in r.parts:
        if p.missed and p.miss_stage is not None:
            out(
                f"part {p.part} after a cache miss: {p.stage.wall:.2f} s for {p.rows} PDFs, "
                f"{p.stage.wall * 1000 / p.rows:.2f} ms per PDF "
                f"(428 answer {p.miss_stage.wall * 1000:.0f} ms, then the whole file again)"
            )
    out(
        f"total (validate + all parts): {fmt_s(total.wall)} wall, {fmt_s(total.cpu)} CPU, "
        f"{total.wall * 1000 / r.rows:.2f} ms per PDF"
    )
    out(f"ZIP size: {size:,} bytes ({size / 1e6:.1f} MB), {size / r.rows / 1024:.1f} KB per PDF")
    out(f"largest part: {max(p.size for p in r.parts) / 1e6:.2f} MB, sha256 {r.digest}")
    out(
        f"peak process memory: {r.peak_bytes / MB:.0f} MB "
        f"({r.start_bytes / MB:.0f} MB before the order)"
    )
    out(
        f"cached rows: {r.cache_measured / MB:.1f} MB measured (tracemalloc), "
        f"{r.cache_estimated / MB:.1f} MB estimated by the cache, "
        f"{r.cache_measured / r.rows:.0f} bytes per row"
    )
    out("estimates (wall = waiting + CPU / share x core factor 1.0-1.5):")
    for label, share in SHARES.items():
        low, high = (estimate(total, share, f) for f in CORE_FACTORS)
        per_pdf = [estimate(render_stage, share, f) * 1000 / r.rows for f in CORE_FACTORS]
        out(
            f"  {label:18} total {fmt_s(low)}-{fmt_s(high)}, "
            f"{per_pdf[0]:.0f}-{per_pdf[1]:.0f} ms per PDF"
        )
    if parts > 1 and opts.verbose:
        for p in r.parts:
            tag = " (miss)" if p.missed else ""
            out(f"  part {p.part:03d}: {p.rows} rows, {p.stage.wall:.2f} s, {p.size:,} bytes{tag}")


# --- Core only ----------------------------------------------------------------------------


def run_core(data: bytes, branding: Branding, chunk_rows: int) -> list[tuple[int, Stage, int]]:
    """Every part in order, each re-validating the whole file; (rows, stage, zip bytes)."""
    parsed, report = pipeline.check_upload(data, "bench.csv")
    if not report.ok:
        raise SystemExit(f"validation failed: {len(report.errors)} error(s)")
    rows = len(parsed.records)
    parts = pipeline.part_count(rows, chunk_rows)
    out = []
    for part in range(1, parts + 1):
        with timed() as stage:
            parsed, report = pipeline.check_upload(data, "bench.csv")
            pipeline.check_part(parsed, part, parts, chunk_rows)
            archive = pipeline.build_part(
                parsed, report, branding, part=part, chunk_rows=chunk_rows
            )
        out.append((len(pipeline.part_records(report, part, chunk_rows)), stage, len(archive)))
    return out


def main_core(opts: argparse.Namespace, branding: Branding) -> int:
    if opts.rows > MAX_SCRIPT_ROWS or opts.rows > MAX_ROWS:
        raise SystemExit(f"--core runs up to {MAX_ROWS:,} rows")
    chunk_rows = opts.chunk or pipeline.GENERATE_CHUNK_ROWS
    repeat = opts.repeat or (3 if pipeline.part_count(opts.rows, chunk_rows) == 1 else 1)
    uploads = [
        make_csv(opts.rows, opts.names, first_key=1 + run * opts.rows) for run in range(repeat)
    ]
    run_core(make_csv(20, "same"), branding, chunk_rows)  # warm-up
    with timed() as validation:
        pipeline.check_upload(uploads[0], "bench.csv")
    runs = [run_core(data, branding, chunk_rows) for data in uploads]
    totals = [sum(s.wall for _, s, _ in run) for run in runs]
    best = runs[totals.index(min(totals))]
    total = Stage()
    for _, stage, _ in best:
        total += stage
    size = sum(n for _, _, n in best)
    print(f"rows: {opts.rows:,} ({NAME_MODES[opts.names]}), parts: {len(best)} (core only)")  # noqa: T201
    print(f"validation only (parse + validate, whole file): {validation.wall * 1000:.0f} ms")  # noqa: T201
    print(f"runs (s): {[round(t, 1) for t in totals]}")  # noqa: T201
    print(  # noqa: T201
        f"total: {fmt_s(total.wall)} wall, {fmt_s(total.cpu)} CPU, "
        f"{total.wall * 1000 / opts.rows:.2f} ms per PDF (each part re-validates the whole file)"
    )
    print(f"ZIP size: {size:,} bytes, {size / opts.rows / 1024:.1f} KB per PDF")  # noqa: T201
    print(f"peak process memory: {peak_memory_bytes() / MB:.0f} MB")  # noqa: T201
    return 0


# --- Main ---------------------------------------------------------------------------------


def main() -> int:
    args = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    args.add_argument("--rows", type=int, default=500, help=f"data rows, 1-{MAX_SCRIPT_ROWS:,}")
    args.add_argument("--names", choices=sorted(NAME_MODES), default="same", help="see above")
    args.add_argument("--unique-names", action="store_true", help="same as --names unique")
    args.add_argument("--core", action="store_true", help="no database, no HTTP (see above)")
    args.add_argument("--chunk", type=int, default=0, help="rows per part for the new batch")
    args.add_argument("--logo", choices=sorted(LOGOS), default="qr", help="qr (README) or flat")
    args.add_argument("--fingerprints", type=int, default=0, help="earlier fingerprints to seed")
    args.add_argument(
        "--miss-part",
        type=int,
        default=None,
        help="part served after clearing the cache (default 2 when there are 2+ parts; 0: none)",
    )
    args.add_argument("--repeat", type=int, default=None, help="--core: timed runs, best shown")
    args.add_argument("--verbose", action="store_true", help="print every part")
    opts = args.parse_args()
    if not 1 <= opts.rows <= MAX_SCRIPT_ROWS:
        args.error(f"--rows must be between 1 and {MAX_SCRIPT_ROWS:,}")
    if opts.chunk < 0:
        args.error("--chunk must be positive")
    if opts.unique_names:
        opts.names = "unique"
    if opts.names == "unique":
        keys = range(1, 1 + 3 * opts.rows)
        if len({frozenset(unique_charset_name(k)) for k in keys}) != len(keys):
            raise SystemExit("character sets are not unique")
    logo = LOGOS[opts.logo]()
    if opts.core:
        return main_core(opts, make_branding(logo))

    parts = pipeline.part_count(opts.rows, opts.chunk or None)
    if opts.miss_part is None:
        opts.miss_part = 2 if parts >= 2 else 0
    data = make_csv(opts.rows, opts.names)
    report_end_to_end(opts, run_end_to_end(opts, data, logo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
