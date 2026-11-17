# eSIM

Web app: upload a CSV/XLSX of eSIM activation codes, validate them,
generate a branded PDF per row with a QR code, return everything as a ZIP.

## Stack

Python 3.12+ (developed on 3.14), FastAPI, Jinja2 + HTMX, segno (QR), ReportLab (PDF),
openpyxl (XLSX), pytest, ruff. Phase 2: Postgres, SQLAlchemy, Alembic.

## Layout

app/core/ parser.py validator.py qr.py render.py sheets.py zipper.py pipeline.py (no webimports)
app/web/ main.py routes.py templates/
tests/ fixtures/ (synthetic data only)

## Hard rules

- ICCID, IMSI, MSISDN are ALWAYS strings. Never cast to int/float.
- Never write activation codes to disk, logs, or a database. Process in memory.
- The confirmation code is never embedded in the QR payload or the built
  LPA string. The QR encodes only the activation code (LPA string).
- Never use real activation codes. Tests use synthetic fixtures only.
- app/core must not import FastAPI or anything from app/web.
- Do not add dependencies without asking.
- Nothing may write to disk, including library temp files (openpyxl, Starlette uploads).
- reportlab, segno and openpyxl are pinned because the code relies on their internals or exact output.
- Never run git stash, reset, checkout or any command that discards or moves uncommitted changes.

## Commands

- Test: pytest -q
- Lint: ruff check . && ruff format --check .
- Run: uvicorn app.web.main:app --reload
- Always use the project venv: .venv\Scripts\python.exe -m pytest / -m ruff. Never the python on PATH.

## Definition of done

Tests pass, lint clean, only the requested module changed.

## Phase 2: accounts and database

- Invite-only accounts plus a public "Request access" form; no public sign-up.
- Organizations with several users (roles: owner, member) and a super-admin (me).
- The database stores metadata only: accounts, sessions, invites, access requests,
  branding, batches, HMAC fingerprints of codes, audit log, usage. NEVER activation codes,
  matching IDs, confirmation codes, uploaded files, PDFs, customer names or customer emails.
- Fingerprints are HMAC-SHA256 with FINGERPRINT_KEY, never plain hashes.
- Secrets come from environment variables (.env locally), never from code or git.
- app/core stays free of database and web imports.
- Audit log keeps 90 days; access requests 30 days.
- On Render, uvicorn must run with --proxy-headers and --forwarded-allow-ips, otherwise all
  users share one IP prefix.
- Daily quota default: 20,000 rows per organization.
- Settings come only from app/settings.py (get_settings()); secrets are Secret objects,
  read with .reveal() where used. Never log the database URL (use safe_database_url).
- Schema changes go through an Alembic migration (migrations/); `alembic check` and
  tests/db/test_migrations.py must report no difference between models and migrations.
- branding.logo_png is always PNG (the database checks the PNG signature). Store the
  downscaled logo as a PNG file. Today prepare_branding (app/core/render.py) does not
  produce one: it encodes PDF image streams (PNG pixel data or JPEG), and uploads may be
  JPEG. The branding step needs a core helper that returns a downscaled PNG.
- Every route that takes an ID (batch, user, invite, access request) must check it
  belongs to the current user's organization (or the user is superadmin). Return 404,
  not 403, for other organizations' IDs.
- Batches with generated parts are never deleted in normal use. Batches still "validated"
  (checked, nothing generated) are deleted by the daily purge after 24 h; "partial" ones
  become "incomplete" after 24 h. Fingerprints are kept 12 months (app/batches.py
  DUPLICATE_WINDOW), then purged; they are also deleted with their batch.
- Voiding a batch (owners for their organization, superadmin) deletes its fingerprints so its
  codes are accepted again, keeps the history row (status voided, voided_at, voided_by) and
  writes a batch_voided audit row. void() and record_part share the per-organization lock.
- A batch keeps the part size it was created with (batches.chunk_rows); code working on an
  existing batch must use it, never GENERATE_CHUNK_ROWS directly.
- IDs in URLs. Sequential bigint IDs are fine, but the login step must check every request that takes an ID (batch, user, invite) against the user’s organization, so nobody can see another company’s data by changing /batches/41 to /batches/42
- Never set the sqlalchemy loggers to DEBUG in production (they print row data, including emails).
