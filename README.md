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
- **What the server keeps**: the uploaded file is processed in memory and never
  written to disk, logged or saved in the database. While an order is downloading, its
  checked rows (activation codes included) are kept only in the server's memory,
  never on disk or in the database, so each part can be made without sending the file
  again. They are deleted as soon as the order is complete (or fails, or its batch is
  voided), and at the latest `UPLOAD_CACHE_TTL_MINUTES` (default 30) minutes after they
  were last used: a sweep every minute deletes them even when no request comes. If they
  are gone (expired, the memory cap `UPLOAD_CACHE_MB` was reached, or the server
  restarted), the page simply sends the file again for the next part.

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
   MFA_KEY=dev-replace-with-a-long-random-value
   ```

   Generate real keys with
   `.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"`.
   Real environment variables override `.env`, and `.env` is not read at all
   when `ENV=production`. In production, `SECRET_KEY`, `FINGERPRINT_KEY` and
   `MFA_KEY` must be at least 32 characters and must not start with `dev-`, or the
   settings refuse to load. The app refuses to start without `MFA_KEY` in every
   environment, because superadmins cannot sign in without two-factor authentication. `postgres://` and `postgresql://` URLs (as Render provides
   them) are rewritten to `postgresql+psycopg://` automatically.
   Changing `FINGERPRINT_KEY` later makes every stored fingerprint useless.
   Changing `MFA_KEY` later makes every stored two-factor secret and recovery code
   useless: everyone with 2FA then needs `reset-2fa` (see "Signing in").

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

## Deploy to Render

`render.yaml` is a Render Blueprint. It creates:

- a PostgreSQL 18 database `esim-db`, free for the first month, reachable only from
  Render's private network;
- a web service `esim` (Starter, Python 3.14.10) in Frankfurt.

Every start of the service runs `alembic upgrade head`, then
`python -m app.admin bootstrap`, then uvicorn: one worker, with the proxy headers
trusted so that rate limits see real client IPs. Render's health check is `/healthz`,
which needs no database and no login. In production the app refuses to start if a key,
`DATABASE_URL` or an `https://` `PUBLIC_BASE_URL` is missing. It also sends HSTS and
uses `__Host-` Secure cookies.

1. **Create the Blueprint.** Push the repo to GitHub. In the Render Dashboard choose
   **New > Blueprint**, connect the repository and pick the branch. Render reads
   `render.yaml` and lists `esim-db` and `esim`.
2. **Fill in the prompted values.** Render asks for the variables marked `sync: false`:
   - `PUBLIC_BASE_URL`: the service's `.onrender.com` address, for example
     `https://esim.onrender.com`. If that name is taken, Render adds a suffix. Check
     the real address at the top of the service page once it exists. If it differs,
     correct the variable under **Environment** (this redeploys). The app does not start
     without an `https://` value.
   - `BOOTSTRAP_SUPERADMIN_EMAIL`: your email address.
   - `RESEND_API_KEY`, `EMAIL_FROM`, `ADMIN_NOTIFY_EMAIL`: leave them empty for now
     (step 11).
3. **Deploy.** Click **Apply** and wait for the first deploy to go live.
4. **Copy the link from the logs.** Open the service's **Logs** and search for
   `BOOTSTRAP:`. That line holds the one-time set-password link. It works once and
   expires after 48 hours. Until it is used, treat it like a password.
   - Later starts print nothing, because a superadmin then exists.
   - If you lose the link or it expires, open the service's **Shell** and run
     `python -m app.admin reset-link --email you@example.com` for a new one.
   - If the host in the link is wrong (step 2), keep the `/invite/<token>` part and
     open it on the right address.
5. **Set your password** on the page the link opens.
6. **Turn on two-factor authentication.** You are taken straight to `/account/2fa`
   and can reach no other page until 2FA is on. Scan the QR code with an
   authenticator app and enter the code. Store the 10 recovery codes in your password
   manager, not on the phone (see "Signing in").
7. **Remove `BOOTSTRAP_SUPERADMIN_EMAIL`** under **Environment** and save. Left in
   place it does nothing while a superadmin exists. But if the service were ever pointed
   at an empty database, it would create a superadmin again and print a link to the
   logs.
8. **Save the keys.** Under **Environment**, reveal `SECRET_KEY`, `FINGERPRINT_KEY` and
   `MFA_KEY`. Render generated them as random 256-bit values when it created the
   Blueprint. Copy all three into your password manager. They are not in git, and a new
   Blueprint generates different ones.
   - **`MFA_KEY` lost or changed**: every stored 2FA secret and recovery code becomes
     unreadable, so nobody with 2FA can sign in, you included. If you have the old
     value, put it back. Otherwise set a new value, run
     `python -m app.admin reset-2fa --email ...` in **Shell** for every user with 2FA
     (starting with yourself), and everyone sets 2FA up again.
   - **`FINGERPRINT_KEY` lost or changed**: the duplicate check stops recognising
     every code already used in the last 12 months, so the same eSIM could be issued
     twice. This cannot be repaired, because the codes themselves are never stored.
     Only the old value helps.
   - **`SECRET_KEY`**: it only signs short-lived form tokens (1 hour) and the
     5-minute step between password and 2FA code. Changing it makes sign-in forms that
     are open at that moment fail once; sessions stay valid. If it leaks, change it.
9. **Add the custom domain `app.esimsheet.com`.**
   - In Render: service **Settings > Custom Domains > Add**, enter `app.esimsheet.com`.
   - In Cloudflare: zone `esimsheet.com`, **DNS > Add record**. Type `CNAME`, name
     `app`, target the service's `.onrender.com` host (without `https://`), proxy
     status **DNS only** (grey cloud).
   - Back in Render, click **Verify** and wait until the certificate is issued.
   - Stay on DNS only. Proxying through Cloudflare adds a second proxy in front of
     Render, which changes the client IPs the rate limits see. Don't turn it on without
     checking that.
10. **Update `PUBLIC_BASE_URL`** to `https://app.esimsheet.com` under **Environment**
    (this redeploys). Invite and reset links then use the new domain. Links already
    issued with the `.onrender.com` host keep working.
11. **Later, when the email feature exists**, set:
    - `EMAIL_BACKEND` = `resend`
    - `RESEND_API_KEY` = a production API key
    - `EMAIL_FROM` = `eSIM Sheet <no-reply@mail.esimsheet.com>`, once
      `mail.esimsheet.com` is verified in Resend
    - `ADMIN_NOTIFY_EMAIL`

    Until then the app does not read these variables. `EMAIL_BACKEND=none` is only a
    placeholder.
12. **Upgrade the database before day 30.** A free Render database expires 30 days
    after it was created, and 14 days after that it is deleted with all its data. It
    also has no backups. Before day 30, change `plan: free` to `plan: 0.1c-256mb` (or
    larger) in `render.yaml` and push, so the Blueprint syncs. You can also change the
    instance type of `esim-db` in the Dashboard. The data and the connection string stay
    the same.

Admin commands on Render run in the service's **Shell** as `python -m app.admin ...`
(no `.venv` path). The daily retention purge runs inside the app, so no cron job is
needed.

## Signing in

Accounts are by invitation only. The invite (or a reset link from an administrator)
sets the password. A new password never changes 2FA: someone with 2FA on who sets a
password from a link still has to enter the code before they are signed in.

1. Go to `/login` and enter your email and password.
2. If two-factor authentication (2FA) is on for your account, the next page asks for
   the 6-digit code from your authenticator app (Google Authenticator, Microsoft
   Authenticator or any other TOTP app). The page is valid for 5 minutes; after that,
   start again with your password. A code works once, and the code from just before
   the current one is still accepted (for a slow phone clock or a slow typist).
3. After too many wrong passwords, or 5 wrong codes, sign-in for the account pauses
   for 15 minutes.

**Setting up 2FA**: open **Two-factor** in the top bar (`/account/2fa`), scan the QR
code with the app (or type in the key shown under it), and enter the code the app
shows. Only then is 2FA on. The next page shows 10 recovery codes **once**: save them
somewhere safe, away from the phone.

- **Superadmins must use 2FA.** A superadmin without it is sent to `/account/2fa`
  right after signing in and can reach no other page (except Log out) until it is on.
  Superadmins cannot switch it off.
- **Everyone else can switch it off** on the same page, with a current code from the
  app or a recovery code.
- **New recovery codes**: "Generate new recovery codes" on `/account/2fa` needs a
  current code from the app (not a recovery code). It replaces all the old codes, which
  stop working, and shows the new ones once.
- **Lost the phone?** Sign in with a recovery code instead of the 6-digit code; each
  recovery code works once.
- **Lost the phone and the recovery codes?** The platform administrator runs
  `.venv\Scripts\python.exe -m app.admin reset-2fa --email person@example.test`. It
  switches 2FA off for that user and ends their sessions. A superadmin must then set up
  2FA again at the next sign-in.

The TOTP secret is stored only encrypted (AES-256-GCM, with a key derived from
`MFA_KEY`), the recovery codes only as keyed hashes, and neither is ever logged.
Successful and failed codes, recovery codes used, switching 2FA on or off, new
recovery codes (`mfa_recovery_regenerated`) and `reset-2fa` are all in the audit log.

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
  default 100).
- Estimated on Render: Starter (0.5 CPU) 7-10.5 min, Standard (1 CPU) 3.5-5.3 min.
  The script measures CPU time per stage and assumes a Render vCPU is 1.0-1.5x
  slower than this machine's core; `scripts/bench.py --core --rows 500` on the
  instance calibrates that factor.

**A 100,000-row order** (`scripts/bench.py --rows 100000 --names pool`, Greek names,
200 parts of 500; the limits are raised for the benchmark only):

| | |
|---|---|
| Parse + validate | 5.4 s |
| Duplicate check | 4.8 s |
| /validate, whole request | 11.0 s |
| Per PDF, parts from the cache | 22.5 ms |
| Total | 37.9 min |
| ZIPs | 2.82 GB (14.1 MB per part) |
| Cached rows | 67 MB (about 700 bytes per row) |
| Peak process memory | 570 MB |
| Estimate on Starter (0.5 CPU) | 75-112 min |
| Estimate on Standard (1 CPU) | 38-56 min |

- The 570 MB peak includes the benchmark client's own copies of the 19 MB file.
  Measured separately, parsing 100,000 rows peaks at 194 MB and the duplicate check
  adds 40 MB, so a server alone would still reach about 400-450 MB.
- A part sent after a forced cache miss (the 428 fallback) cost about 10 s more than a
  part from the cache. Before the cache every part paid that, about 33 minutes more
  for this order.
- **MAX_ROWS stays 10,000 on Render Starter**: 100,000 rows peaked above Starter's
  512 MB of memory.

**Waiting for a slot**: with GENERATE_SLOTS=1, a part requested while another part is
being generated queues for up to GENERATE_WAIT_SECONDS (default 60) instead of being
refused at once. One part of 500 Greek-name rows takes about 21-32 s on Starter (the
estimate above, per part), so a second user's part waits its turn behind one running
part. Only after the wait does it get 503 "Server busy" with `Retry-After: 60`.
