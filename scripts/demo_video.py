"""Record a scripted product demo video (about 85 s) with Playwright, then encode it.

Usage (from the repo root; needs TEST_DATABASE_URL in .env, the dev extras, Chromium
and ffmpeg on PATH):
    .venv\\Scripts\\python.exe -m playwright install chromium
    .venv\\Scripts\\python.exe scripts\\demo_video.py

Writes to ``output/video/`` (gitignored): ``demo.webm``, ``demo.mp4`` (H.264),
``demo-720p.mp4`` (1280x720, letterboxed) and ``demo.gif`` (scenes 2-6, under 8 MB).
Without ffmpeg only Playwright's own recording is kept, as ``demo.webm``.

This is not a test. Like ``scripts/screens.py`` it runs the app on 127.0.0.1 against the
TEST database, which it empties and fills with a synthetic demo organization ("Aegean
Travel eSIM (demo)", ``*.test`` accounts); it refuses to run on the DATABASE_URL
database. Keys are random per run, email goes to an in-memory fake mailer, and every data
row is synthetic (``8999`` ICCIDs, ``smdp.example.invalid`` codes). Downloads are refused
by the browser: the ZIP and the PDF shown full screen are read from the HTTP response in
memory and served back to the page by Playwright, never written to disk.

The captions, the cursor, the dragged file card, the ZIP listing, the PDF view and the
"sped up" tag are an overlay injected by this script only (``add_init_script``, with
Playwright's CSP bypass for this browser context); the app's templates and CSP are not
changed. The overlay also blurs the one-time invite link. Long waits for PDF generation
are fast-forwarded in the edited video, and the scene they are in carries the "sped up"
tag.
"""

from __future__ import annotations

import argparse
import io
import secrets
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402
from make_big_csv import make_csv, pool_name, synthetic_iccid  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from playwright.sync_api import Locator, Page, Response, Route, sync_playwright  # noqa: E402
from screens import free_port, test_engine  # noqa: E402
from sqlalchemy import Engine, text  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.auth import accounts, mfa  # noqa: E402
from app.auth.passwords import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.models import Batch, BatchStatus, Organization, Role, Usage, User  # noqa: E402
from app.mail import FakeMailer  # noqa: E402
from app.settings import Settings, load_settings  # noqa: E402
from app.web.auth import SESSION_COOKIE  # noqa: E402
from app.web.main import create_app  # noqa: E402

OUTPUT = ROOT / "output" / "video"
RAW = OUTPUT / "raw"
WIDTH, HEIGHT = 1440, 900
FPS = 25

ORG_NAME = "Aegean Travel eSIM (demo)"
COMPANY = "Aegean Travel eSIM"
OWNER_EMAIL = "owner@aegean-travel.test"
ADMIN_EMAIL = "admin@platform.test"
BRAND_COLOR = "#0b6e99"
FOOTER = "Καλό ταξίδι! Questions: help@aegean-travel.test"

FIRST_FILE = "aegean_orders.csv"
FIXED_FILE = "aegean_orders_fixed.csv"
BIG_FILE = "aegean_orders_10000.csv"
REUSE_FILE = "aegean_orders_week48.csv"
BIG_ROWS = 10_000
SMALL_ROWS = 12
SMALL_FIRST = 500_001  # ICCID keys of the small files (the big file uses 1-10,000)
REUSE_FIRST = 500_101

EDGE = 0.6  # seconds kept at normal speed at each end of a fast-forwarded wait
FAST_TARGET = 1.0  # a fast-forwarded wait plays in about this many seconds
GIF_MAX_BYTES = 8 * 1024 * 1024
GIF_TRIES = ((10, 800), (8, 720), (8, 640), (6, 640), (6, 560), (5, 480))

# --- The demo overlay (injected into every document of the recorded page) -------------------

DEMO_LAYER = r"""
(() => {
  if (window.top !== window) return;
  const CSS = `
  #demo-layer { position: fixed; inset: 0; pointer-events: none; z-index: 2147483647;
    font: 500 18px/1.35 Inter, "Segoe UI", system-ui, sans-serif; }
  #demo-caption { position: absolute; left: 50%; bottom: 28px; max-width: 76%;
    transform: translate(-50%, 12px); opacity: 0; padding: 12px 22px; border-radius: 14px;
    background: rgba(15, 23, 42, .88); color: #fff; text-align: center;
    box-shadow: 0 8px 30px rgba(0,0,0,.25); transition: opacity .35s ease, transform .35s ease; }
  #demo-caption.on { opacity: 1; transform: translate(-50%, 0); }
  #demo-tag { position: absolute; top: 76px; right: 18px; padding: 4px 10px;
    border-radius: 999px; background: rgba(15, 23, 42, .78); color: #fde68a;
    font-size: 13px; letter-spacing: .04em; opacity: 0; transition: opacity .25s ease; }
  #demo-tag.on { opacity: 1; }
  #demo-cursor { position: absolute; left: 0; top: 0; width: 26px; height: 26px;
    transform: translate(-3px, -2px); filter: drop-shadow(0 2px 3px rgba(0,0,0,.35)); }
  .demo-ripple { position: absolute; width: 34px; height: 34px; margin: -17px 0 0 -17px;
    border-radius: 50%; border: 3px solid rgba(11, 110, 153, .75);
    animation: demo-ripple .45s ease-out forwards; }
  @keyframes demo-ripple { from { transform: scale(.3); opacity: 1; }
    to { transform: scale(1.4); opacity: 0; } }
  #demo-chip { position: absolute; left: 0; top: 0; display: none; align-items: center;
    gap: 10px; padding: 10px 14px; border-radius: 10px; background: #fff; color: #0f172a;
    font-size: 15px; box-shadow: 0 10px 30px rgba(0,0,0,.25); transform: translate(18px, 14px); }
  #demo-chip.on { display: flex; }
  #demo-chip b { display: inline-grid; place-items: center; width: 34px; height: 40px;
    border-radius: 5px; background: #16a34a; color: #fff; font-size: 11px; }
  #demo-panel { position: absolute; right: 40px; top: 110px; width: 470px; display: none;
    padding: 20px 22px; border-radius: 14px; background: #fff; color: #0f172a;
    box-shadow: 0 20px 50px rgba(0,0,0,.28); font-size: 15px; }
  #demo-panel.on { display: block; animation: demo-in .3s ease-out; }
  #demo-panel h3 { margin: 0 0 12px; font-size: 16px; word-break: break-all; }
  #demo-panel li { list-style: none; padding: 5px 0; border-top: 1px solid #e2e8f0;
    font-family: ui-monospace, Consolas, monospace; font-size: 14px; }
  #demo-panel ul { margin: 0; padding: 0; }
  #demo-pdf { position: absolute; inset: 0; display: none; background: #1f2937; }
  #demo-pdf.on { display: block; animation: demo-in .35s ease-out; }
  #demo-pdf embed { width: 100%; height: 100%; border: 0; }
  @keyframes demo-in { from { opacity: 0; } to { opacity: 1; } }
  #one-time-link { filter: blur(5px); }  /* no invite token in the video */
  `;
  const ARROW = '<svg viewBox="0 0 24 24" width="26" height="26"><path d="M4 2l16 11.5-7 1.2 ' +
    '4.2 7.3-2.9 1.6-4.2-7.4L4 21z" fill="#0f172a" stroke="#fff" stroke-width="1.6" ' +
    'stroke-linejoin="round"/></svg>';
  function build() {
    if (document.getElementById("demo-layer")) return;
    const style = document.createElement("style");
    style.textContent = CSS;
    document.head.appendChild(style);
    const layer = document.createElement("div");
    layer.id = "demo-layer";
    layer.innerHTML = '<div id="demo-pdf"></div><div id="demo-panel"></div>' +
      '<div id="demo-caption"></div><div id="demo-tag">sped up</div>' +
      '<div id="demo-chip"><b>CSV</b><span></span></div><div id="demo-cursor">' + ARROW + "</div>";
    document.body.appendChild(layer);
    const $ = (id) => document.getElementById(id);
    const store = window.sessionStorage;
    function place(x, y) {
      $("demo-cursor").style.left = x + "px";
      $("demo-cursor").style.top = y + "px";
      $("demo-chip").style.left = x + "px";
      $("demo-chip").style.top = y + "px";
    }
    const saved = JSON.parse(store.getItem("demo.cursor") || "[720, 450]");
    place(saved[0], saved[1]);
    document.addEventListener("mousemove", (e) => {
      place(e.clientX, e.clientY);
      store.setItem("demo.cursor", JSON.stringify([e.clientX, e.clientY]));
    }, true);
    document.addEventListener("mousedown", (e) => {
      const ripple = document.createElement("div");
      ripple.className = "demo-ripple";
      ripple.style.left = e.clientX + "px";
      ripple.style.top = e.clientY + "px";
      layer.appendChild(ripple);
      setTimeout(() => ripple.remove(), 500);
    }, true);
    function caption(text) {
      store.setItem("demo.caption", text || "");
      const el = $("demo-caption");
      if (text) el.textContent = text;
      el.classList.toggle("on", Boolean(text));
    }
    function tag(on) {
      store.setItem("demo.tag", on ? "1" : "");
      $("demo-tag").classList.toggle("on", Boolean(on));
    }
    caption(store.getItem("demo.caption"));
    tag(store.getItem("demo.tag"));
    window.__demo = {
      caption, tag,
      chip(name) {
        $("demo-chip").querySelector("span").textContent = name || "";
        $("demo-chip").classList.toggle("on", Boolean(name));
      },
      panel(title, lines) {
        const panel = $("demo-panel");
        panel.replaceChildren();
        if (!title) { panel.classList.remove("on"); return; }
        const h = document.createElement("h3");
        h.textContent = title;
        const ul = document.createElement("ul");
        lines.forEach((line) => {
          const li = document.createElement("li");
          li.textContent = line;
          ul.appendChild(li);
        });
        panel.append(h, ul);
        panel.classList.add("on");
      },
      pdf(url) {
        const box = $("demo-pdf");
        box.replaceChildren();
        if (!url) { box.classList.remove("on"); return; }
        const embed = document.createElement("embed");
        embed.type = "application/pdf";
        embed.src = url;
        box.appendChild(embed);
        box.classList.add("on");
      },
      scroll(top, ms) {
        const from = window.scrollY;
        const max = document.documentElement.scrollHeight - window.innerHeight;
        const to = Math.max(0, Math.min(top, max));
        const started = performance.now();
        const ease = (t) => (t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);
        return new Promise((resolve) => {
          function frame(now) {
            const t = Math.min(1, (now - started) / ms);
            window.scrollTo(0, from + (to - from) * ease(t));
            if (t < 1) requestAnimationFrame(frame); else resolve(true);
          }
          requestAnimationFrame(frame);
        });
      },
    };
  }
  if (document.body) build(); else document.addEventListener("DOMContentLoaded", build);
})();
"""


# --- Synthetic data ---------------------------------------------------------------------------


def lpa(n: int) -> str:
    return f"LPA:1$smdp.example.invalid$DEMO-{n:06d}-AEGN"


def small_csv(keys: list[int], iccids: dict[int, str] | None = None) -> bytes:
    """Rows with Greek names; ``iccids`` replaces the ICCID of some rows (by position)."""
    plans = ("Ταξίδι 10GB", "Ευρώπη 5GB", "Ελλάδα 20GB", "Κόσμος 3GB")
    lines = ["iccid,activation_code,customer_name,plan_name,email"]
    for i, n in enumerate(keys):
        iccid = (iccids or {}).get(i, synthetic_iccid(n))
        lines.append(
            f"{iccid},{lpa(n)},{pool_name(n)},{plans[n % len(plans)]},"
            f"customer{n}@mail.example.invalid"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def demo_files() -> dict[str, bytes]:
    keys = list(range(SMALL_FIRST, SMALL_FIRST + SMALL_ROWS))
    good = synthetic_iccid(keys[3])
    bad_luhn = good[:-1] + str((int(good[-1]) + 1) % 10)  # wrong check digit
    mistakes = {3: bad_luhn, 6: "8.99901E+18", 9: synthetic_iccid(keys[1])}  # dup of row 3
    reuse_keys = list(range(REUSE_FIRST, REUSE_FIRST + 10)) + [keys[2], keys[7]]
    return {
        FIRST_FILE: small_csv(keys, mistakes),
        FIXED_FILE: small_csv(keys),
        BIG_FILE: make_csv(BIG_ROWS),
        REUSE_FILE: small_csv(reuse_keys),
    }


def demo_logo() -> bytes:
    """A made-up logo (sun over waves and the company name), drawn in memory."""
    img = Image.new("RGBA", (720, 220), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((40, 30, 150, 140), fill=(245, 158, 11, 255))
    for i, y in enumerate((120, 150, 180)):
        draw.arc((20, y - 40, 190, y + 30), 200, 340, fill=(11, 110, 153, 255 - 50 * i), width=12)
    font_path = ROOT / "app" / "core" / "assets" / "fonts" / "DejaVuSans.ttf"
    big = ImageFont.truetype(str(font_path), 70)
    small = ImageFont.truetype(str(font_path), 34)
    draw.text((220, 40), "Aegean", font=big, fill=(11, 110, 153, 255))
    draw.text((224, 128), "TRAVEL eSIM", font=small, fill=(15, 23, 42, 255))
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


# --- Database and server ----------------------------------------------------------------------


@dataclass
class Seeded:
    owner_token: str
    admin_token: str


def seed(engine: Engine, sessions: sessionmaker[Session], mfa_keys: mfa.MfaKeys) -> Seeded:
    """Empty the TEST database; add the demo organization, its owner and a superadmin."""
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    now = datetime.now(UTC)
    unusable = hash_password(secrets.token_urlsafe(32))  # nobody signs in with a password
    with sessions.begin() as db:
        org = Organization(name=ORG_NAME)
        db.add(org)
        db.flush()
        owner = User(
            email=OWNER_EMAIL,
            organization_id=org.id,
            role=Role.OWNER,
            password_hash=unusable,
            last_login_at=now,
        )
        ops = User(
            email="ops@aegean-travel.test",
            organization_id=org.id,
            role=Role.MEMBER,
            password_hash=unusable,
            last_login_at=now - timedelta(days=2),
        )
        admin = User(
            email=ADMIN_EMAIL,
            is_superadmin=True,
            role=Role.MEMBER,
            password_hash=unusable,
            last_login_at=now,
        )
        db.add_all([owner, ops, admin])
        db.flush()
        admin.mfa_secret = mfa.encrypt_secret(mfa_keys, admin.id, mfa.new_secret())
        admin.mfa_enabled_at = now - timedelta(days=30)
        db.add(Usage(organization_id=org.id, day=now.date(), rows_generated=1_480))
        history = (  # metadata only, so History does not start empty
            (owner.id, "summer_islands.csv", 480, 1, 24 * 61),
            (ops.id, "hotel_partners_q3.xlsx", 2_400, 5, 24 * 35),
            (owner.id, "aegean_orders_october.xlsx", 1_200, 3, 24 * 20),
            (ops.id, "ferry_desk_refill.csv", 1_000, 2, 24 * 6),
        )
        for user_id, name, rows, parts, hours in history:
            db.add(
                Batch(
                    organization_id=org.id,
                    user_id=user_id,
                    created_at=now - timedelta(hours=hours),
                    file_name=name,
                    row_count=rows,
                    parts=parts,
                    parts_downloaded=parts,
                    warning_count=0,
                    error_count=0,
                    status=BatchStatus.COMPLETED,
                )
            )
        owner_id, admin_id = owner.id, admin.id
    with sessions.begin() as db:
        owner_token = accounts.create_session(db, owner_id, now=now)
    with sessions.begin() as db:
        admin_token = accounts.create_session(db, admin_id, now=now)
    return Seeded(owner_token, admin_token)


def start_server(settings: Settings, sessions: sessionmaker[Session], port: int) -> uvicorn.Server:
    app = create_app(settings, sessions=sessions, run_retention=False, mailer=FakeMailer())
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            sys.exit("the app did not start")
        time.sleep(0.05)
    return server


# --- Timeline ---------------------------------------------------------------------------------


@dataclass
class Scene:
    number: int
    title: str
    target: float
    start: float = 0.0
    end: float = 0.0
    saved: float = 0.0  # seconds removed by fast-forwarding inside the scene

    @property
    def length(self) -> float:
        """Length in the edited video."""
        return self.end - self.start - self.saved


@dataclass
class Timeline:
    page: Page
    t0: float = field(default_factory=time.monotonic)
    scenes: list[Scene] = field(default_factory=list)
    fast: list[tuple[float, float, float]] = field(default_factory=list)  # start, end, speed
    offset: float = 0.0  # how much later than t0 the recording really started

    def now(self) -> float:
        return time.monotonic() - self.t0

    @contextmanager
    def scene(
        self, number: int, title: str, target: float, caption: str = "", sped_up: bool = False
    ) -> Iterator[None]:
        scene = Scene(number, title, target, start=self.now())
        self.scenes.append(scene)
        set_caption(self.page, caption)
        if sped_up:
            self.page.evaluate("window.__demo.tag(true)")
        yield
        left = target - (self.now() - scene.start - scene.saved)
        if left > 0:
            self.page.wait_for_timeout(left * 1000)
        if sped_up:
            self.page.evaluate("window.__demo.tag(false)")
        scene.end = self.now()

    @contextmanager
    def fast_forward(self) -> Iterator[None]:
        """Play the wait inside the block faster in the edited video (scene says "sped up")."""
        started = self.now()
        yield
        a, b = started + EDGE, self.now() - EDGE
        if b - a > FAST_TARGET * 2:
            speed = (b - a) / FAST_TARGET
            self.fast.append((a, b, speed))
            self.scenes[-1].saved += (b - a) - FAST_TARGET

    def segments(self) -> list[tuple[float, float, float]]:
        """(start, end, speed) of the raw video, from the first scene to the last."""
        out, at = [], self.scenes[0].start
        for a, b, speed in self.fast:
            out.append((at, a, 1.0))
            out.append((a, b, speed))
            at = b
        out.append((at, self.scenes[-1].end, 1.0))
        return [s for s in out if s[1] - s[0] > 0.01]

    def edited(self, raw: float) -> float:
        """Where raw time ``raw`` lands in the edited video."""
        total = 0.0
        for a, b, speed in self.segments():
            if raw <= a:
                break
            total += (min(raw, b) - a) / speed
        return total


# --- Page helpers -----------------------------------------------------------------------------


def set_caption(page: Page, text_: str) -> None:
    page.evaluate("t => window.__demo.caption(t)", text_)


def pause(page: Page, seconds: float) -> None:
    page.wait_for_timeout(seconds * 1000)


def ready(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready.then(() => true)")
    page.wait_for_function("Boolean(window.__demo)")


def goto(page: Page, url: str) -> None:
    page.goto(url)
    ready(page)


def cursor(page: Page) -> tuple[float, float]:
    x, y = page.evaluate("JSON.parse(sessionStorage.getItem('demo.cursor') || '[720, 450]')")
    return float(x), float(y)


def move_to(page: Page, target: Locator | tuple[float, float], steps: int = 28) -> None:
    """Glide the mouse to a locator's centre (or a point), visibly."""
    if isinstance(target, tuple):
        x, y = target
    else:
        target.scroll_into_view_if_needed()
        box = target.bounding_box()
        if box is None:
            raise RuntimeError(f"not visible: {target}")
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(x, y, steps=steps)


def click(page: Page, target: Locator, steps: int = 28) -> None:
    move_to(page, target, steps)
    pause(page, 0.2)
    target.click()


def type_slowly(page: Page, target: Locator, value: str, delay: int = 55) -> None:
    click(page, target)
    target.press_sequentially(value, delay=delay)


def scroll_to(page: Page, top: float, ms: int = 900) -> None:
    page.evaluate("([t, ms]) => window.__demo.scroll(t, ms)", [top, ms])
    x, y = cursor(page)
    page.mouse.move(x, y)  # keep the hover state honest after the scroll


def scroll_into_view(page: Page, target: Locator, offset: float = 140, ms: int = 900) -> None:
    top = page.evaluate(
        "([el, o]) => el.getBoundingClientRect().top + scrollY - o",
        [
            target.element_handle(),
            offset,
        ],
    )
    scroll_to(page, top, ms)


def drag_file(
    page: Page, name: str, data: bytes, steps: int = 40, waiting: Timeline | None = None
) -> None:
    """Carry a file card from the right edge onto the drop zone and drop it there.

    With ``waiting``, the wait for the check result is fast-forwarded in the video."""
    zone = page.locator("#dropzone")
    scroll_to(page, 0, 600)
    page.mouse.move(WIDTH - 60, HEIGHT * 0.55, steps=8)
    page.evaluate("n => window.__demo.chip(n)", name)
    pause(page, 0.4)
    move_to(page, zone, steps)
    page.dispatch_event("#file", "dragenter")
    pause(page, 0.5)
    page.evaluate(
        "document.querySelector('#results > .results')?.setAttribute('data-demo-old', '')"
    )
    page.evaluate("window.__demo.chip('')")
    page.set_input_files("#file", files=[{"name": name, "mimeType": "text/csv", "buffer": data}])
    page.dispatch_event("#file", "drop")
    with waiting.fast_forward() if waiting else nullcontext():
        page.wait_for_selector("#results > .results:not([data-demo-old])", timeout=90_000)


def is_zip(response: Response) -> bool:
    return urlsplit(response.url).path == "/generate" and response.status == 200


# --- Scenes -----------------------------------------------------------------------------------


@dataclass
class Demo:
    page: Page
    base: str
    seeded: Seeded
    files: dict[str, bytes]
    pdf: dict[str, bytes] = field(default_factory=dict)

    def serve_demo(self, route: Route) -> None:
        body = self.pdf.get("current", b"")
        route.fulfill(status=200, body=body, content_type="application/pdf")

    def run(self, tl: Timeline) -> None:
        page, base = self.page, self.base
        page.route(f"{base}/__demo__/**", self.serve_demo)
        goto(page, f"{base}/")
        page.mouse.move(720, 300)
        pause(page, 0.5)

        with tl.scene(
            1,
            "Intro",
            5,
            "Turning eSIM activation codes into customer-ready PDFs is slow and error-prone.",
        ):
            move_to(page, page.locator("#dropzone"), 60)

        with tl.scene(2, "Upload: drag a CSV onto the drop zone", 10):
            drag_file(page, FIRST_FILE, self.files[FIRST_FILE], steps=55)
            pause(page, 0.6)
            scroll_into_view(page, page.locator("#review-title"), 90, 1200)

        with tl.scene(
            3, "Validation: errors shown", 12, "Catches mistakes before your customers do."
        ):
            rows = page.locator("#results table tbody tr")
            for i in range(rows.count()):
                move_to(page, rows.nth(i).locator("td").nth(2), 30)
                pause(page, 1.2)
            pause(page, 0.8)

        with tl.scene(4, "Fixed file: all rows pass", 8):
            drag_file(page, FIXED_FILE, self.files[FIXED_FILE])
            scroll_into_view(page, page.locator("#review-title"), 90)
            move_to(page, page.locator("#results .results-status"), 25)

        with tl.scene(5, "Branding: company name, colour, logo", 8):
            scroll_to(page, 0, 700)
            type_slowly(page, page.locator("#company_name"), COMPANY, delay=50)
            move_to(page, page.locator("#primary_color"), 20)
            page.locator("#primary_color").fill(BRAND_COLOR)
            pause(page, 0.4)
            move_to(page, page.locator("#logo"), 20)
            page.set_input_files(
                "#logo",
                files=[{"name": "aegean-logo.png", "mimeType": "image/png", "buffer": demo_logo()}],
            )
            pause(page, 0.4)
            type_slowly(page, page.locator("#footer_text"), FOOTER, delay=22)

        with tl.scene(6, "Download: the ZIP, then one PDF", 10):
            download = page.locator("#download")
            scroll_into_view(page, download, 420, 800)
            move_to(page, download, 25)
            with page.expect_response(is_zip, timeout=120_000) as info:
                download.click()
            response = info.value
            body = response.body()
            page.wait_for_selector("#generating:has-text('Done')")
            disposition = response.headers.get("content-disposition", "")
            zip_name = disposition.split("filename=")[-1].strip('"') or "order.zip"
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                names = zf.namelist()
                pdfs = [n for n in names if n.endswith(".pdf")]
                self.pdf["current"] = zf.read(pdfs[0])
            others = [n for n in names if not n.endswith(".pdf")]
            lines = [*pdfs[:3], f"… {len(pdfs)} PDFs in all", *others]
            pause(page, 0.6)
            page.evaluate(
                "([t, l]) => window.__demo.panel(t, l)", [f"Downloaded: {zip_name}", lines]
            )
            pause(page, 2.2)
            page.evaluate("window.__demo.panel('', [])")
            page.evaluate(
                "u => window.__demo.pdf(u)", f"{base}/__demo__/esim.pdf#toolbar=0&view=Fit"
            )
            pause(page, 3.5)
            page.evaluate("window.__demo.pdf('')")

        with tl.scene(
            7, "Big orders: 10,000 rows in parts", 8, "Up to 10,000 eSIMs per upload.", True
        ):
            drag_file(page, BIG_FILE, self.files[BIG_FILE], steps=24, waiting=tl)
            download = page.locator("#download")
            scroll_into_view(page, page.locator("#review-title"), 90, 450)
            pause(page, 0.3)
            click(page, download, 16)
            one_by_one = page.locator("#one-by-one")
            try:
                one_by_one.wait_for(state="visible", timeout=3_000)
                pause(page, 0.4)
                click(page, one_by_one, 16)
            except Exception:  # noqa: BLE001, S110  (no folder API: straight to the list)
                pass
            page.wait_for_selector("#parts-manual:not([hidden])")
            # Part 1's own button: the status line and the top of the list stay in view.
            scroll_into_view(page, page.locator("#review-title"), 90, 400)
            first = page.locator("#part-list tr[data-part='1']")
            click(page, first.locator("button"), 18)
            with tl.fast_forward():
                page.wait_for_selector(
                    "#part-list tr[data-part='1'] .badge-success", timeout=180_000
                )
            move_to(page, first.locator(".badge-success"), 16)
            pause(page, 0.4)

        with tl.scene(8, "History, and a re-used code blocked", 8):
            click(page, page.locator("nav.site-nav a:has-text('History')"), 22)
            ready(page)
            move_to(page, page.locator("td.file-name").first, 20)
            pause(page, 0.8)
            click(page, page.locator("nav.site-nav a:has-text('Upload')"), 18)
            ready(page)
            drag_file(page, REUSE_FILE, self.files[REUSE_FILE], steps=24)
            scroll_into_view(page, page.locator("#used-before"), 110, 700)
            move_to(page, page.locator("#results table tbody tr").first.locator("td").nth(2), 20)

        with tl.scene(
            9, "Request access, superadmin approves", 8, "Invite-only. Two-factor login."
        ):
            page.context.clear_cookies()
            goto(page, f"{base}/request-access")
            type_slowly(page, page.locator("#company_name"), "Cyclades Roaming (demo)", 16)
            type_slowly(page, page.locator("#contact_name"), "Nefeli Example", 16)
            type_slowly(page, page.locator("#email"), "nefeli@cyclades-roaming.test", 14)
            page.select_option("#volume_band", index=2)
            click(page, page.locator("form button[type=submit]"), 16)
            page.wait_for_selector("text=We have received your request")
            pause(page, 0.4)
            page.context.add_cookies(
                [{"name": SESSION_COOKIE, "value": self.seeded.admin_token, "url": base}]
            )
            goto(page, f"{base}/admin/requests")
            row = page.locator("tr", has_text="Cyclades Roaming (demo)")
            click(page, row.locator("button:has-text('Approve')"), 22)
            page.wait_for_selector("text=Invitation sent to")
            move_to(page, page.locator("text=Invitation sent to").first, 18)
            pause(page, 1.6)  # long enough to read

        with tl.scene(10, "Privacy", 4, "Codes never written to disk or database. EU hosting."):
            page.context.clear_cookies()
            goto(page, f"{base}/privacy")
            scroll_to(page, 260, 2200)

        with tl.scene(11, "Call to action", 3, "esimsheet.com: request access."):
            goto(page, f"{base}/request-access")
            move_to(page, page.locator("form button[type=submit]"), 30)


# --- Encoding ---------------------------------------------------------------------------------


def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, capture_output=True)  # noqa: S603


def edit_graph(tl: Timeline, extra: str = "") -> str:
    parts, labels = [], []
    for i, (a, b, speed) in enumerate(tl.segments()):
        a, b = max(0.0, a - tl.offset), max(0.0, b - tl.offset)
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=(PTS-STARTPTS)/{speed:.4f}[s{i}]")
        labels.append(f"[s{i}]")
    tail = f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0,fps={FPS}{extra}[out]"
    return ";".join([*parts, tail])


def duration(ffprobe: str | None, path: Path) -> float | None:
    if ffprobe is None:
        return None
    result = subprocess.run(  # noqa: S603
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:  # no duration in the container
        return None


def encode(tl: Timeline, raw: Path) -> list[Path]:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    webm = OUTPUT / "demo.webm"
    if ffmpeg is None:
        shutil.move(raw, webm)
        print("ffmpeg is not on PATH: kept the unedited recording only (demo.webm).")  # noqa: T201
        return [webm]
    base = [ffmpeg, "-y", "-loglevel", "error", "-i", str(raw)]
    mp4_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
    mp4_args += ["-movflags", "+faststart"]
    mp4 = OUTPUT / "demo.mp4"
    run([*base, "-filter_complex", edit_graph(tl), "-map", "[out]", *mp4_args, str(mp4)])
    vp9 = ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "33", "-row-mt", "1", "-cpu-used", "4"]
    run([*base, "-filter_complex", edit_graph(tl), "-map", "[out]", *vp9, str(webm)])
    letterbox = ",scale=1152:720:flags=lanczos,pad=1280:720:64:0:color=0x0f172a"
    hd = OUTPUT / "demo-720p.mp4"
    run([*base, "-filter_complex", edit_graph(tl, letterbox), "-map", "[out]", *mp4_args, str(hd)])

    by_number = {s.number: s for s in tl.scenes}
    start, end = tl.edited(by_number[2].start), tl.edited(by_number[6].end)
    gif = OUTPUT / "demo.gif"
    for fps, width in GIF_TRIES:
        palette = (
            f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];"
            "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5"
            ":diff_mode=rectangle"
        )
        run(
            [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}"]
            + ["-i", str(mp4), "-filter_complex", palette, "-loop", "0", str(gif)]
        )
        if gif.stat().st_size < GIF_MAX_BYTES:
            print(f"demo.gif: {fps} fps, {width} px wide")  # noqa: T201
            break
    else:
        print("demo.gif is still over 8 MB at the smallest setting")  # noqa: T201
    raw.unlink()
    outputs = [webm, mp4, hd, gif]
    for path in outputs:
        seconds = duration(ffprobe, path)
        size = path.stat().st_size / (1024 * 1024)
        shown = f"{seconds:5.1f} s" if seconds is not None else "     ?"
        print(f"  {path.relative_to(ROOT)}  {shown}  {size:5.1f} MB")  # noqa: T201
    return outputs


def report(tl: Timeline) -> None:
    print("scene                                         target  edited  note")  # noqa: T201
    for s in tl.scenes:
        note = f"sped up (saved {s.saved:.1f} s)" if s.saved else ""
        if s.length > s.target + 0.5:
            note = (note + "; " if note else "") + "over target"
        line = f"{s.number:2d} {s.title:<42} {s.target:5.1f}  {s.length:6.1f}  {note}"
        print(line)  # noqa: T201
    total = sum(s.length for s in tl.scenes)
    print(f"   {'total':<42} {sum(s.target for s in tl.scenes):5.1f}  {total:6.1f}")  # noqa: T201


# --- Main -------------------------------------------------------------------------------------


def main() -> int:
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    engine = test_engine()  # refuses the DATABASE_URL database
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    mfa_key = secrets.token_urlsafe(48)
    settings = load_settings(
        {
            "DATABASE_URL": engine.url.render_as_string(hide_password=False),
            "SECRET_KEY": secrets.token_urlsafe(48),
            "FINGERPRINT_KEY": secrets.token_urlsafe(48),
            "MFA_KEY": mfa_key,
            "PUBLIC_BASE_URL": base,
            "ADMIN_NOTIFY_EMAIL": "requests@platform.test",
            "OPERATOR_NAME": "Example Operator Ltd (demo)",
            "CONTACT_EMAIL": "support@operator.test",
            "EMAIL_FROM": "eSIM Sheet <no-reply@mail.platform.test>",
        }
    )
    seeded = seed(engine, sessions, mfa.keys_from(mfa_key))
    server = start_server(settings, sessions, port)
    files = demo_files()
    if RAW.exists():
        shutil.rmtree(RAW)
    RAW.mkdir(parents=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="chromium")  # new headless: has a PDF viewer
            try:
                context = browser.new_context(
                    viewport={"width": WIDTH, "height": HEIGHT},
                    record_video_dir=str(RAW),
                    record_video_size={"width": WIDTH, "height": HEIGHT},
                    bypass_csp=True,  # only for the injected overlay; the app's CSP is unchanged
                    accept_downloads=False,  # nothing is saved to disk
                )
                context.add_init_script(DEMO_LAYER)
                context.add_cookies(
                    [{"name": SESSION_COOKIE, "value": seeded.owner_token, "url": base}]
                )
                started = time.monotonic()  # the recording starts with the page
                page = context.new_page()
                page.set_default_timeout(60_000)
                tl = Timeline(page, t0=started)
                Demo(page, base, seeded, files).run(tl)
                if page.video is None:
                    raise RuntimeError("the page was not recorded")
                raw = Path(page.video.path())
                closing = tl.now()
                context.close()  # finishes the recording
            finally:
                browser.close()
    finally:
        server.should_exit = True
        engine.dispose()
    recorded = duration(shutil.which("ffprobe"), raw)
    if recorded is not None:  # the recording starts a little after the page is created
        tl.offset = max(0.0, closing - recorded)
    report(tl)
    encode(tl, raw)
    shutil.rmtree(RAW, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
