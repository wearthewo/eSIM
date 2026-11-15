# eSIM

Web app: upload a CSV/XLSX of eSIM activation codes, validate them,
generate a branded PDF per row with a QR code, return everything as a ZIP.

## Stack

Python 3.12+ (developed on 3.14), FastAPI, Jinja2 + HTMX, segno (QR), ReportLab (PDF),
openpyxl (XLSX), pytest, ruff. Phase 2: Postgres, SQLAlchemy, Alembic.

## Layout

app/core/ parser.py validator.py qr.py render.py zipper.py (no web imports)
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

## Commands

- Test: pytest -q
- Lint: ruff check . && ruff format --check .
- Run: uvicorn app.web.main:app --reload
- Always use the project venv: .venv\Scripts\python.exe -m pytest / -m ruff. Never the python on PATH.

## Definition of done

Tests pass, lint clean, only the requested module changed.
