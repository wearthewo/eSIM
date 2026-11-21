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

- `scripts/bench.py --rows 10000 [--names same|pool|unique|ascii] [--chunk N]`: time
  a whole order end to end through the real app on `TEST_DATABASE_URL` (/validate,
  then every part from the upload cache, one part through the 428 fallback), with
  estimates for Render Starter and Standard. `--rows` up to 100,000 (the app's limits
  are raised for that run only). `--core`: no database, the pipeline only (for
  calibrating on the target machine).
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
- **Logo**: decoded once per part and downscaled (box filter, never enlarged)
  to the pixels needed at 200 dpi for its printed size (max 5 x 2 cm, so at most
  394 x 157 px).
  It is then stored as PNG (Flate with PNG predictors) or JPEG, whichever is
  smaller. Transparency is kept as a lossless soft mask.
- **QR code**: vector rectangles that encode only the activation code. The
  activation code, matching ID and confirmation code are never printed as text.
- Output is deterministic: the same input gives byte-identical PDFs.

## Size and speed

Measured on the development machine (Intel Core, Windows 11) with Python 3.14,
ReportLab 5.1 and Pillow 13. Timings vary by 10-20% with machine load.

**Logo** (a 1600 x 600 RGBA PNG, drawn at 5 x 1.875 cm). The logo is the largest
part of an ASCII PDF, so it is stored at 200 dpi of its printed size (was 300):

| Logo stored at | Pixels    | Flat logo (shapes + text) | ASCII PDF | Photo-like logo |
|----------------|-----------|--------------------------:|----------:|----------------:|
| 300 dpi        | 591 x 221 |                   12.7 KB |   16.3 KB |        109.2 KB |
| 200 dpi        | 394 x 148 |                    8.4 KB |   12.0 KB |         70.5 KB |

**Per PDF**, end to end (`scripts/bench.py --rows 1000`, parts from the upload
cache, including ZIP, `record_part` and HTTP):

| Names | Flat logo above | Small QR logo (bench default) |
|-------|-----------------|-------------------------------|
| ASCII | 13.5 ms, 12.2 KB | 12.8 ms, 5.1 KB |
| Greek | 22.0 ms, 34.7 KB | 21.1 ms, 27.6 KB |

Greek PDFs carry their own DejaVu Sans subset (about 22 KB), which is built
again for each PDF. That is the main cost of non-ASCII names. Unique names per
row are no worse than repeated ones. Building the subsets is serialised (a
lock around ReportLab's save), because ReportLab's TrueType parser is not
thread-safe.

**A 10,000-row order** (`scripts/bench.py --rows 10000 --names pool`, Greek names):

- /validate (parse, validate, duplicate check, cache): about 1.0 s, of which
  parse + validate 0.5 s and the duplicate check 0.46 s.
- All 20 parts: about 213-216 s, 21.3-21.8 ms per PDF, 282 MB of ZIPs (14.1 MB
  per part). Before the upload cache each part also re-parsed and re-checked the
  whole file (about 1 s per part, 20 s per order) and uploaded it again.
- Peak process memory 261 MB with parts of 500 rows, 304 MB with parts of 1,000.
  Parts of 1,000 are not faster (211-222 s), so new batches keep 500.
- Cached rows: about 700 bytes per row, 6.7 MB for 10,000 rows (UPLOAD_CACHE_MB,
  default 150).
- Estimated on Render: Starter (0.5 CPU) 7-10.5 min, Standard (1 CPU) 3.5-5.3 min.
  The script measures CPU time per stage and assumes a Render vCPU is 1.0-1.5x
  slower than this machine's core; `scripts/bench.py --core --rows 500` on the
  instance calibrates that factor.
