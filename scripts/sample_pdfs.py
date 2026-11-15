"""Render sample PDFs from the synthetic fixtures so they can be inspected by eye.

Usage (from the repo root):
    .venv\\Scripts\\python.exe scripts\\sample_pdfs.py

Writes ``output/<fixture>/esim_<iccid>.pdf``. ``output/`` is gitignored. Only the
synthetic test fixtures listed below are rendered; never point this at real data.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import parser, render, validator  # noqa: E402
from app.core.models import Branding  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
OUTPUT = ROOT / "output"
SAMPLES = ("valid_full.csv", "valid_greek_names.csv")
LOGO = ROOT / "app" / "web" / "static" / "brand" / "logo.png"


def main() -> int:
    branding = Branding(
        company_name="Example Telecom (sample)",
        logo_png=LOGO.read_bytes() if LOGO.is_file() else None,
        primary_hex="#1A73E8",
        footer_text="Sample output rendered from synthetic test fixtures. Not a real eSIM.",
    )
    for name in SAMPLES:
        records = parser.parse_csv((FIXTURES / name).read_bytes())
        report = validator.validate(records)
        if not report.ok:
            print(f"{name}: skipped, {len(report.errors)} validation error(s)")  # noqa: T201
            continue
        target = OUTPUT / Path(name).stem
        target.mkdir(parents=True, exist_ok=True)
        for record in report.valid:
            (target / render.pdf_filename(record)).write_bytes(render.render_pdf(record, branding))
        print(f"{name}: {len(report.valid)} PDF(s) -> {target.relative_to(ROOT)}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
