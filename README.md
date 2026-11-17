# eSIM

Upload a CSV or XLSX of eSIM activation codes. The app validates every row,
renders a branded one-page PDF with a QR code per row, and returns the PDFs as
ZIP files (500 PDFs per part, up to 10,000 rows per upload).

## Input and output files

- **Template**: `GET /template.xlsx` ("Download template (Excel)" on the upload
  page) has every supported column, all formatted as text, and one synthetic
  example row. Starting from it keeps Excel from turning ICCIDs, IMSIs and
  MSISDNs into numbers.
- **Excel number damage**: if an ICCID, IMSI or MSISDN contains `E+` (for
  example `8.999E+18` in a CSV saved by Excel), the row fails with
  `excel_scientific`: "Excel converted this value to a number. Format the
  column as Text and re-enter the values." The lost digits cannot be recovered.
- **Each ZIP** holds the part's PDFs plus `manifest.xlsx` (`row_number`,
  `iccid`, `pdf_filename`). If the upload has an `email` column, it also holds
  `mailmerge.xlsx` (`email`, `customer_name`, `plan_name`, `iccid`,
  `pdf_filename`) for your own mail merge. The server never sends mail.
- Both are Excel files rather than CSV. Excel 2007 ignores a CSV's UTF-8 BOM, so
  Greek turned into mojibake, and it showed ICCIDs as `8.999E+18`, losing
  digits. In the XLSX files every cell is a text cell with the text format
  (`@`), including `row_number`: no numbers, dates or formulas. A value such as
  `=cmd|...` stays literal text, so the CSV-era `'` escaping is gone. The header
  row is bold and frozen. Neither file ever holds activation codes, matching IDs
  or confirmation codes. The files are built in memory (never in a temporary
  file) with fixed timestamps, so the same input gives byte-identical ZIPs.

Project rules (strings for ICCID/IMSI/MSISDN, no secrets on disk or in logs, QR
holds only the activation code, synthetic test data only) are in `CLAUDE.md`.

## Setup and commands

Always use the project venv, never the `python` on PATH.

```
.venv\Scripts\python.exe -m pip install -e .[dev]
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check . && .venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m uvicorn app.web.main:app --reload
```

## Database (local development)

Phase 2 (accounts, organizations, quotas) uses PostgreSQL 14 or newer. The
database stores metadata only: never activation codes, matching IDs,
confirmation codes, uploaded files, PDFs, customer names or customer emails.

1. Create the role and the two databases (as a PostgreSQL superuser, e.g. in
   `psql -U postgres`). Pick your own password:

   ```sql
   CREATE ROLE esim LOGIN PASSWORD 'change-me';
   CREATE DATABASE esim OWNER esim;
   CREATE DATABASE esim_test OWNER esim;
   ```

2. Create `.env` in the project root (it is gitignored; never commit it):

   ```
   ENV=development
   DATABASE_URL=postgresql+psycopg://esim:change-me@localhost:5432/esim
   TEST_DATABASE_URL=postgresql+psycopg://esim:change-me@localhost:5432/esim_test
   SECRET_KEY=dev-replace-with-a-long-random-value
   FINGERPRINT_KEY=dev-replace-with-a-long-random-value
   ```

   Generate real keys with
   `.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"`.
   Real environment variables override `.env`, and `.env` is not read at all
   when `ENV=production`. In production, `SECRET_KEY` and `FINGERPRINT_KEY` must
   be at least 32 characters and must not start with `dev-`, or the settings
   refuse to load. `postgres://` and `postgresql://` URLs (as Render provides
   them) are rewritten to `postgresql+psycopg://` automatically.
   Changing `FINGERPRINT_KEY` later makes every stored fingerprint useless.

3. Apply the migrations (they read `DATABASE_URL`; nothing is configured in
   `alembic.ini`):

   ```
   .venv\Scripts\python.exe -m alembic upgrade head
   ```

   Undo everything with `-m alembic downgrade base`. After changing
   `app/db/models.py`, create a migration with
   `-m alembic revision --autogenerate -m "describe the change"`, review it, and
   check with `-m alembic check`.

Tests use `TEST_DATABASE_URL`: the schema is rebuilt with Alembic once per run
and every test is rolled back. If `TEST_DATABASE_URL` is not set, the database
tests are skipped and all other tests still run. Tests refuse to run if it
points at the same database as `DATABASE_URL`.

Scripts (synthetic data only, output goes to the gitignored `output/`):

- `scripts/bench.py --rows 10000 [--names same|pool|unique]`: time the full pipeline.
- `scripts/make_big_csv.py`: write a synthetic CSV for load tests.
- `scripts/sample_pdfs.py`: render the fixture PDFs so you can look at them.
- `scripts/screens.py`: screenshot every page at desktop (1440 px) and phone (390 px)
  width, light and dark, into `output/screens/`. One-time setup:
  `.venv\Scripts\python.exe -m pip install -e .[dev]` then
  `.venv\Scripts\python.exe -m playwright install chromium`. It starts the app on
  127.0.0.1 against `TEST_DATABASE_URL` (which it empties and fills with synthetic
  accounts, as the tests do; it refuses to run on the `DATABASE_URL` database) and
  uses random keys, never the `.env` ones. Downloads are refused, so no ZIP or PDF
  is saved. Options: `--only login,upload` (page-name prefixes), `--skip-generate`
  (no PDF generation, much faster), `--no-dark`.

## Web UI

Templates are in `app/web/templates`, styles in `app/web/static/app.css` (design
tokens as CSS variables at the top, with a dark scheme via `prefers-color-scheme`),
behaviour in `app/web/static/app.js`. The Content-Security-Policy allows scripts,
styles, images and fonts from the app itself only, so no inline `style=` or
`<script>` and no external URLs (`tests/web/test_templates.py` checks this, and
that the colour tokens meet WCAG AA). The font is Inter 4.1 (variable,
`static/fonts/InterVariable.woff2`, SIL Open Font License in `static/fonts/OFL.txt`),
with system fonts as fallback.

## PDF contents

- **Fixed text** (headings, labels, install steps, ICCID, confirmation note)
  uses Helvetica / Helvetica-Bold. These fonts are built into every PDF viewer,
  so they are not embedded.
- **User text** (company name, customer name, plan name, footer) uses
  Helvetica if it is pure ASCII. Otherwise it uses DejaVu Sans (regular only,
  bundled in `app/core/assets/fonts`), and ReportLab embeds a subset of it.
  User text is never bold; the layout uses size for emphasis, so ASCII and
  Greek pages look the same.
- **Logo**: decoded once per batch and downscaled (box filter, never enlarged)
  to the pixels needed at 300 dpi for its printed size (max 5 x 2 cm).
  It is then stored as PNG (Flate with PNG predictors) or JPEG, whichever is
  smaller. Transparency is kept as a lossless soft mask.
- **QR code**: vector rectangles that encode only the activation code. The
  activation code, matching ID and confirmation code are never printed as text.
- Output is deterministic: the same input gives byte-identical PDFs.

## Size and speed

Measured on the development machine with Python 3.14, ReportLab 5.1 and
Pillow 13. The test logo is a 1600 x 600 RGBA PNG; per-PDF figures are means
over 500 PDFs.

| Names                 | PDF size | of which font | of which logo | render time |
|-----------------------|---------:|--------------:|--------------:|------------:|
| ASCII                 |  17.3 KB |        0.2 KB |       14.1 KB |   11-16 ms  |
| Greek                 |  39.7 KB |       22.5 KB |       14.1 KB |     ~21 ms  |
| Greek, unique per row |  39.7 KB |       22.5 KB |       14.1 KB |     ~21 ms  |

Greek PDFs carry their own DejaVu Sans subset (about 20 KB), which is built
again for each PDF. That is the main cost of non-ASCII names. Unique names per
row are no worse than repeated ones. Building the subsets is serialised (a
lock around ReportLab's save), because ReportLab's TrueType parser is not
thread-safe.

Full run, `scripts/bench.py --rows 10000` (same Greek name on every row, bench
logo, every part re-validates the whole file):

- total about 210-240 s (10.2-15.8 s per part, varies with machine load)
- total size 284.8 MB, 14.24 MB per part, 27.8 KB per PDF
