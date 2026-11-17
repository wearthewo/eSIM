"""app.js: how the parts of an order are saved (folder mode) or downloaded (parts list).

Runs the real app.js and htmx in headless Chromium (Playwright, from the dev extras) on
the real index.html, served under the real Content-Security-Policy. Everything is in
memory: Playwright answers the page, /static, /validate and /generate itself (no app
server, no database), and the File System Access API is an in-memory fake, so nothing
is written to disk. The "ZIP files" are short synthetic byte strings.

Skips when Playwright or its Chromium is not installed
(``.venv\\Scripts\\python.exe -m playwright install chromium``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from app.batches import Continuation  # noqa: E402
from app.core.models import ValidationReport  # noqa: E402
from app.web import routes  # noqa: E402
from app.web.main import CONTENT_SECURITY_POLICY, STATIC_DIR  # noqa: E402
from app.web.templating import templates  # noqa: E402

expect = sync_api.expect
Page = sync_api.Page
Route = sync_api.Route

ORIGIN = "https://esim.test"  # https: the folder API needs a secure context
FOLDER = "Orders"  # the fake folder's name in FAKES
CHUNK = routes.pipeline.GENERATE_CHUNK_ROWS
PLAIN_ROWS = 2 * CHUNK + 250  # the parts list's order: 500 + 500 + 250 rows


def zip_name(part: int, parts: int) -> str:
    return routes._zip_filename("17112026_0930", part, parts)


def zip_body(part: int) -> bytes:
    return f"synthetic zip, part {part}".encode()


# In-memory stand-ins, installed before app.js runs. window.__t.log records, in order:
# "fetch N" (a part request), "cancel fetch N" (its signal aborted), "picker",
# "create/open/close/abort/remove NAME" (folder), "click NAME" and "revoke" (browser
# downloads). The download link's click is stubbed, so the browser never downloads anything.
FAKES = """
(() => {
  const t = { log: [], files: {}, picker: "ok", stallName: null, release: null, denyWrite: false,
              chunked: false };
  window.__t = t;
  const fetch0 = window.fetch.bind(window);
  // Playwright delivers a body as one chunk; t.chunked splits it into 64 KB chunks, as
  // a real network response arrives.
  const split = () => new TransformStream({
    transform(chunk, controller) {
      for (let i = 0; i < chunk.length; i += 65536) controller.enqueue(chunk.slice(i, i + 65536));
    },
  });
  window.fetch = (url, init) => {
    const part = init.body.get("part");
    t.log.push("fetch " + part);
    init.signal.addEventListener("abort", () => t.log.push("cancel fetch " + part));
    return fetch0(url, init).then((r) => !t.chunked || !r.ok ? r :
      new Response(r.body.pipeThrough(split(), { signal: init.signal }),
                   { status: r.status, headers: r.headers }));
  };
  const revoke0 = URL.revokeObjectURL.bind(URL);
  URL.revokeObjectURL = (url) => { t.log.push("revoke"); revoke0(url); };
  HTMLAnchorElement.prototype.click = function () { t.log.push("click " + this.download); };

  const notFound = () => new DOMException("not found", "NotFoundError");
  const folder = {
    kind: "directory",
    name: "Orders",  // FOLDER
    async getFileHandle(name, options) {
      if (!(name in t.files)) {
        if (!(options && options.create)) throw notFound();
        t.files[name] = "";
        t.log.push("create " + name);
      }
      return {
        kind: "file",
        name,
        async createWritable() {
          if (t.denyWrite) throw new DOMException("write access denied", "NotAllowedError");
          t.log.push("open " + name);
          let text = "";
          let signal;
          return new WritableStream({
            start(controller) {
              signal = controller.signal;
              signal.addEventListener("abort", () => t.log.push("abort " + name));
            },
            async write(chunk) {
              if (t.stallName === name) {  // a slow disk: this write ends on t.release()
                t.log.push("stalled " + name);
                await new Promise((resolve) => { t.release = resolve; });
              }
              text += new TextDecoder().decode(chunk);
            },
            close() { t.files[name] = text; t.log.push("close " + name); },
          });
        },
      };
    },
    async removeEntry(name) {
      if (!(name in t.files)) throw notFound();
      delete t.files[name];
      t.log.push("remove " + name);
    },
    async queryPermission() { return "granted"; },
  };
  window.showDirectoryPicker = async () => {
    t.log.push("picker");
    if (t.picker === "abort") throw new DOMException("The user aborted a request.", "AbortError");
    return folder;
  };
})();
"""

NO_FOLDER_API = """
delete window.showDirectoryPicker;
if (typeof window.showDirectoryPicker === "function") window.showDirectoryPicker = undefined;
"""


def render_index() -> str:
    return templates.env.get_template("index.html").render(
        routes._context(
            form={
                "company_name": "Demo (synthetic)",
                "primary_color": "#0055AA",
                "footer_text": "",
            },
            csrf_token="synthetic-csrf",
            current_user=None,
            pending_requests=0,
        )
    )


def render_results(rows: int, downloaded: tuple[int, ...] | None = None) -> str:
    """The check result; with ``downloaded``, the file continues an unfinished batch."""
    continuation = None
    if downloaded is not None:
        continuation = Continuation(
            batch_id=7,
            file_name="order.csv",
            created_at=datetime(2026, 11, 1, 9, 0, tzinfo=UTC),
            parts=routes.pipeline.part_count(rows),
            parts_downloaded=len(downloaded),
            chunk_rows=CHUNK,
            downloaded=downloaded,
        )
    results = routes.Results(
        row_count=rows,
        report=ValidationReport(),
        batch_id=7 if continuation else None,
        continuation=continuation,
    )
    return templates.env.get_template("_results.html").render(routes._context(results=results))


def error_page(message: str) -> str:
    return f'<!DOCTYPE html><div id="results"><p class="error">{message}</p></div>'


@dataclass
class FakeServer:
    """Answers the page's requests. Parts in ``hold`` wait until ``release()``."""

    rows: int = 3 * CHUNK
    padding: int = 0  # extra bytes per ZIP: a large body arrives in several chunks
    hold: set[int] = field(default_factory=set)
    fail_once: dict[int, tuple[int, str]] = field(default_factory=dict)
    held: list[tuple[Route, int, int]] = field(default_factory=list)
    downloaded: tuple[int, ...] | None = None  # set: the upload continues a batch

    def handle(self, route: Route) -> None:
        request = route.request
        path = request.url.removeprefix(ORIGIN).split("?")[0]
        headers = {"Content-Security-Policy": CONTENT_SECURITY_POLICY}
        if path == "/":
            route.fulfill(body=render_index(), content_type="text/html", headers=headers)
        elif path.startswith("/static/"):
            route.fulfill(path=STATIC_DIR / path.removeprefix("/static/"))
        elif path == "/validate":
            route.fulfill(body=render_results(self.rows, self.downloaded), content_type="text/html")
        elif path == "/generate":
            body = request.post_data_buffer or b""
            part = int(re.search(rb'name="part"\r\n\r\n(\d+)', body).group(1))
            parts = int(re.search(rb'name="parts"\r\n\r\n(\d+)', body).group(1))
            if part in self.fail_once:
                status, message = self.fail_once.pop(part)
                route.fulfill(status=status, body=error_page(message), content_type="text/html")
            elif part in self.hold:
                self.hold.discard(part)
                self.held.append((route, part, parts))
            else:
                self.send_zip(route, part, parts)
        else:
            route.fulfill(status=404, body="")

    def send_zip(self, route: Route, part: int, parts: int) -> None:
        name = zip_name(part, parts)
        route.fulfill(
            body=zip_body(part) + b"\0" * self.padding,
            content_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    def release(self) -> None:
        while self.held:
            route, part, parts = self.held.pop(0)
            try:
                self.send_zip(route, part, parts)
            except sync_api.Error:
                pass  # the page aborted this request (Cancel)


@pytest.fixture(scope="module")
def browser() -> Iterator[sync_api.Browser]:
    with sync_api.sync_playwright() as pw:
        try:
            chromium = pw.chromium.launch()
        except sync_api.Error as exc:
            pytest.skip(f"Playwright's Chromium is not installed: {exc.message.splitlines()[0]}")
        yield chromium
        chromium.close()


@dataclass
class Harness:
    page: Page
    server: FakeServer
    problems: list[str]

    def log(self) -> list[str]:
        return self.page.evaluate("window.__t.log")

    def files(self) -> dict[str, str]:
        return self.page.evaluate("window.__t.files")

    def wait_log(self, entry: str) -> None:
        self.page.wait_for_function("(e) => window.__t.log.includes(e)", arg=entry)

    def set(self, script: str) -> None:
        self.page.evaluate(script)

    def upload_and_download(self) -> None:
        self.page.set_input_files(
            "#file", files=[{"name": "codes.csv", "mimeType": "text/csv", "buffer": b"synthetic"}]
        )
        self.page.click("#download")

    def status(self) -> sync_api.Locator:
        return self.page.locator("#generating")

    def bar(self) -> tuple[float, float]:
        value, maximum = self.page.eval_on_selector("#progress-bar", "b => [b.value, b.max]")
        return value, maximum

    # The parts list.
    def rows(self) -> sync_api.Locator:
        return self.page.locator("#part-list tbody tr")

    def part_status(self, n: int) -> sync_api.Locator:
        return self.page.locator(f'#part-list tr[data-part="{n}"] > td:nth-child(2)')

    def part_button(self, n: int) -> sync_api.Locator:
        return self.page.locator(f'[data-download-part="{n}"]')

    def download_part(self, n: int) -> None:
        self.part_button(n).click()

    def fetches(self) -> list[str]:
        return [e for e in self.log() if e.startswith("fetch")]

    def clicks(self) -> list[str]:
        return [e for e in self.log() if e.startswith("click")]


def make_harness(
    browser: sync_api.Browser,
    *scripts: str,
    rows: int,
    downloaded: tuple[int, ...] | None = None,
) -> Iterator[Harness]:
    context = browser.new_context(accept_downloads=False)
    page = context.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda error: problems.append(f"page error: {error}"))

    def on_console(message: sync_api.ConsoleMessage) -> None:
        # Chrome logs every 4xx/5xx answer as an error; those are expected in some tests.
        if message.type == "error" and not message.text.startswith("Failed to load resource"):
            problems.append(message.text)
        elif "Content Security Policy" in message.text:
            problems.append(message.text)

    page.on("console", on_console)
    for script in scripts:
        page.add_init_script(script)
    server = FakeServer(rows=rows, downloaded=downloaded)
    page.route(f"{ORIGIN}/**", server.handle)
    page.goto(f"{ORIGIN}/")
    yield Harness(page, server, problems)
    server.release()
    context.close()
    assert not problems, problems  # no CSP violation, no script error


@pytest.fixture
def folder_page(browser: sync_api.Browser) -> Iterator[Harness]:
    """Three parts; the browser has the folder API (the in-memory fake)."""
    yield from make_harness(browser, FAKES, rows=3 * CHUNK)


@pytest.fixture
def plain_page(browser: sync_api.Browser) -> Iterator[Harness]:
    """Three parts, the last one partly filled; no folder API (e.g. Firefox, Safari)."""
    yield from make_harness(browser, FAKES, NO_FOLDER_API, rows=PLAIN_ROWS)


@pytest.fixture
def continue_page(browser: sync_api.Browser) -> Iterator[Harness]:
    """Four parts of an unfinished batch, 1 and 3 downloaded before; folder API present."""
    yield from make_harness(browser, FAKES, rows=4 * CHUNK, downloaded=(1, 3))


def saved_in_order(parts: int) -> list[str]:
    """The log of saving every part, each one closed before the next is requested."""
    log = []
    for n in range(1, parts + 1):
        name = zip_name(n, parts)
        log += [f"fetch {n}", f"create {name}", f"open {name}", f"close {name}"]
    return log


# --- Folder mode -------------------------------------------------------------------------


def test_folder_mode_asks_for_a_folder_before_generating(folder_page: Harness) -> None:
    h = folder_page
    h.upload_and_download()
    expect(h.page.locator("#choose-folder")).to_be_visible()
    expect(h.page.locator("#choose-folder")).to_have_text("Choose a folder for the parts")
    expect(h.page.locator("#one-by-one")).to_be_visible()
    expect(h.page.locator("#one-by-one")).to_have_text("Download parts one by one")
    expect(h.page.locator("#parts-manual")).to_be_hidden()
    expect(h.page.locator("#cancel")).to_be_visible()
    expect(h.page.locator("#progress-bar")).to_be_hidden()
    expect(h.status()).to_contain_text("This order has 3 parts. Choose a folder")
    assert h.log() == []  # nothing requested yet

    h.page.click("#cancel")  # back to the review, nothing generated
    expect(h.page.locator("#progress")).to_be_hidden()
    expect(h.page.locator("#download")).to_be_enabled()
    assert h.log() == []


def test_folder_mode_saves_each_part_before_requesting_the_next(folder_page: Harness) -> None:
    h = folder_page
    h.server.hold = {2}
    h.upload_and_download()
    h.page.click("#choose-folder")
    expect(h.status()).to_have_text(f"Saved part 1 of 3 to {FOLDER}. Generating part 2 of 3…")
    assert h.bar() == (1, 3)
    expect(h.page.locator("#choose-folder")).to_be_hidden()

    h.server.release()
    expect(h.status()).to_have_text(f"Done: all 3 ZIP files were saved to {FOLDER}.")
    assert h.bar() == (3, 3)
    assert h.log() == ["picker", *saved_in_order(3)]  # strictly one part after another
    assert h.files() == {zip_name(n, 3): zip_body(n).decode() for n in (1, 2, 3)}
    expect(h.page.locator("#cancel")).to_be_hidden()
    expect(h.page.locator("#retry")).to_be_hidden()
    expect(h.page.locator("#download")).to_be_enabled()


def test_folder_mode_never_overwrites_a_file(folder_page: Harness) -> None:
    h = folder_page
    first = zip_name(1, 3)
    stem = first.removesuffix(".zip")
    h.page.evaluate(
        "([a, b]) => { window.__t.files[a] = 'older file'; window.__t.files[b] = 'older too'; }",
        [first, f"{stem} (2).zip"],
    )
    h.upload_and_download()
    h.page.click("#choose-folder")
    expect(h.status()).to_contain_text("Done: all 3 ZIP files were saved")
    expect(h.status()).to_contain_text(f'1 file was given a number, such as "{stem} (3).zip"')
    files = h.files()
    assert files[first] == "older file" and files[f"{stem} (2).zip"] == "older too"
    assert files[f"{stem} (3).zip"] == zip_body(1).decode()
    assert files[zip_name(2, 3)] == zip_body(2).decode()


def test_cancel_while_generating_then_resume_skips_saved_parts(folder_page: Harness) -> None:
    h = folder_page
    h.server.hold = {2}
    h.upload_and_download()
    h.page.click("#choose-folder")
    expect(h.status()).to_contain_text("Generating part 2 of 3")
    h.page.click("#cancel")
    expect(h.status()).to_have_text(
        f"Cancelled before part 2 of 3 was saved. Part 1 is saved in {FOLDER}."
    )
    expect(h.page.locator("#retry")).to_have_text("Resume from part 2")
    expect(h.page.locator("#cancel")).to_be_hidden()
    assert h.bar() == (1, 3)
    assert list(h.files()) == [zip_name(1, 3)]

    h.page.click("#retry")
    expect(h.status()).to_have_text(f"Done: all 3 ZIP files were saved to {FOLDER}.")
    fetches = [e for e in h.log() if e.startswith(("fetch", "picker"))]
    assert fetches == ["picker", "fetch 1", "fetch 2", "fetch 2", "fetch 3"]  # same folder
    assert sorted(h.files()) == [zip_name(n, 3) for n in (1, 2, 3)]


def test_cancel_during_a_write_removes_the_partial_file(folder_page: Harness) -> None:
    h = folder_page
    h.server.padding = 1_000_000  # 16 chunks: most of part 2 is still to come
    second = zip_name(2, 3)
    h.page.evaluate("(name) => { window.__t.stallName = name; window.__t.chunked = true; }", second)
    h.upload_and_download()
    h.page.click("#choose-folder")
    h.wait_log(f"stalled {second}")
    expect(h.status()).to_have_text(f"Saving part 2 of 3 to {FOLDER}…")
    h.page.click("#cancel")
    # A stream finishes the chunk being written before it aborts (a real write takes
    # milliseconds); the rest of the part is never written.
    h.set("window.__t.release()")
    expect(h.status()).to_have_text(
        f"Cancelled before part 2 of 3 was saved. Part 1 is saved in {FOLDER}."
    )
    log = h.log()
    assert f"abort {second}" in log and f"remove {second}" in log
    assert f"close {second}" not in log
    assert "fetch 3" not in log  # stopped
    assert list(h.files()) == [zip_name(1, 3)]
    expect(h.page.locator("#retry")).to_have_text("Resume from part 2")

    h.set("window.__t.stallName = null")
    h.page.click("#retry")
    expect(h.status()).to_contain_text("Done: all 3 ZIP files were saved")
    assert len(h.files()[second]) == len(zip_body(2)) + 1_000_000


def test_cancel_after_the_last_byte_arrived_keeps_that_part(folder_page: Harness) -> None:
    h = folder_page
    second = zip_name(2, 3)
    h.page.evaluate("(name) => { window.__t.stallName = name; }", second)
    h.upload_and_download()
    h.page.click("#choose-folder")
    h.wait_log(f"stalled {second}")
    h.page.click("#cancel")  # the whole (small) part is already received, only the write is left
    h.set("window.__t.release()")
    expect(h.status()).to_have_text(
        f"Cancelled before part 3 of 3 was saved. Parts 1 to 2 are saved in {FOLDER}."
    )
    assert sorted(h.files()) == [zip_name(1, 3), second]
    assert "fetch 3" not in h.log()
    expect(h.page.locator("#retry")).to_have_text("Resume from part 3")


def test_failed_part_is_retried_from_that_part(folder_page: Harness) -> None:
    h = folder_page
    h.server.fail_once = {2: (503, "The server is busy. Please try again in a minute.")}
    h.upload_and_download()
    h.page.click("#choose-folder")
    expect(h.status()).to_have_text(
        "Part 2 of 3 failed. The server is busy. Please try again in a minute."
        f" Part 1 is saved in {FOLDER}."
    )
    expect(h.page.locator("#retry")).to_have_text("Retry part 2")
    h.page.click("#retry")
    expect(h.status()).to_contain_text("Done: all 3 ZIP files were saved")
    assert [e for e in h.log() if e.startswith("fetch")] == [
        "fetch 1", "fetch 2", "fetch 2", "fetch 3"
    ]  # fmt: skip


def test_a_folder_that_cannot_be_written_is_reported(folder_page: Harness) -> None:
    h = folder_page
    h.set("window.__t.denyWrite = true")
    h.upload_and_download()
    h.page.click("#choose-folder")
    expect(h.status()).to_have_text(
        f"Part 1 of 3 failed. It could not be saved in {FOLDER} (write access denied)."
    )
    assert h.files() == {}  # the empty file was removed again
    expect(h.page.locator("#retry")).to_have_text("Retry part 1")


def test_dismissed_picker_asks_again_and_offers_the_parts_list(folder_page: Harness) -> None:
    h = folder_page
    h.set("window.__t.picker = 'abort'")
    h.upload_and_download()
    h.page.click("#choose-folder")
    expect(h.status()).to_contain_text("No folder was chosen. This order has 3 parts.")
    expect(h.page.locator("#choose-folder")).to_be_visible()
    assert h.log() == ["picker"]  # nothing generated

    h.page.click("#one-by-one")
    expect(h.page.locator("#parts-manual")).to_be_visible()
    expect(h.rows()).to_have_count(3)
    expect(h.page.locator("#choose-folder")).to_be_hidden()
    expect(h.page.locator("#one-by-one")).to_be_hidden()
    expect(h.page.locator("#next-part")).to_be_focused()
    assert h.log() == ["picker"]  # still nothing generated: each part waits for its button

    h.page.click("#next-part")
    expect(h.part_status(1)).to_have_text("Downloaded")
    assert h.log() == ["picker", "fetch 1", f"click {zip_name(1, 3)}", "revoke"]


def test_single_part_needs_no_folder(folder_page: Harness) -> None:
    h = folder_page
    h.server.rows = CHUNK
    h.upload_and_download()
    expect(h.status()).to_have_text("Done: the ZIP file was generated; your browser is saving it.")
    assert h.log() == ["fetch 1", f"click {zip_name(1, 1)}", "revoke"]
    expect(h.page.locator("#parts-manual")).to_be_hidden()


# --- Parts list: download parts one by one ------------------------------------------------


def test_without_folder_api_the_parts_list_is_shown_directly(plain_page: Harness) -> None:
    h = plain_page
    h.upload_and_download()
    expect(h.page.locator("#parts-manual")).to_be_visible()
    expect(h.page.locator("#choose-folder")).to_be_hidden()
    expect(h.page.locator("#one-by-one")).to_be_hidden()
    expect(h.status()).to_have_text(
        "This order has 3 parts. Download them one by one: each part is generated when you"
        " click its button."
    )
    expect(h.rows().locator("td:first-child")).to_have_text(
        ["Part 1 · rows 1–500", "Part 2 · rows 501–1,000", "Part 3 · rows 1,001–1,250"]
    )
    for n in (1, 2, 3):
        expect(h.part_status(n)).to_have_text("Not downloaded")
        expect(h.part_button(n)).to_have_text("Download")
        expect(h.part_button(n)).to_have_accessible_name(f"Download part {n}")
    expect(h.page.locator("#next-part")).to_have_text("Download next part")
    expect(h.page.locator("#cancel")).to_be_hidden()
    expect(h.page.locator("#retry")).to_be_hidden()
    expect(h.page.locator("#download")).to_be_enabled()
    assert h.bar() == (0, 3)
    assert h.log() == []  # nothing is generated before a button is clicked


def test_a_part_is_generated_only_when_its_button_is_clicked(plain_page: Harness) -> None:
    h = plain_page
    h.upload_and_download()
    h.download_part(2)
    expect(h.part_status(2)).to_have_text("Downloaded")
    expect(h.status()).to_have_text("Part 2 of 3 was downloaded. 1 of 3 parts downloaded.")
    expect(h.part_button(2)).to_have_text("Download again")
    expect(h.part_button(2)).to_have_accessible_name("Download part 2 again")
    for n in (1, 3):
        expect(h.part_status(n)).to_have_text("Not downloaded")
        expect(h.part_button(n)).to_be_enabled()
    assert h.log() == ["fetch 2", f"click {zip_name(2, 3)}", "revoke"]
    assert h.bar() == (1, 3)


def test_only_one_part_is_generated_at_a_time(plain_page: Harness) -> None:
    h = plain_page
    h.server.hold = {1}
    h.upload_and_download()
    h.download_part(1)
    expect(h.status()).to_have_text("Generating part 1 of 3…")
    expect(h.part_status(1)).to_have_text("Generating…")
    for n in (1, 2, 3):
        expect(h.part_button(n)).to_be_disabled()
    expect(h.page.locator("#next-part")).to_be_disabled()
    expect(h.page.locator("#cancel")).to_be_visible()
    expect(h.page.locator("#download")).to_be_disabled()

    # Even a button that is enabled again by other means starts no second request.
    h.page.evaluate(
        "document.querySelectorAll('[data-download-part], #next-part')"
        ".forEach((b) => { b.disabled = false; b.click(); })"
    )
    assert h.fetches() == ["fetch 1"]

    h.server.release()
    expect(h.part_status(1)).to_have_text("Downloaded")
    for n in (1, 2, 3):
        expect(h.part_button(n)).to_be_enabled()
    expect(h.page.locator("#cancel")).to_be_hidden()
    expect(h.page.locator("#download")).to_be_enabled()
    assert h.log() == ["fetch 1", f"click {zip_name(1, 3)}", "revoke"]


def test_buttons_wait_until_the_handed_off_part_is_released(plain_page: Harness) -> None:
    h = plain_page
    h.upload_and_download()
    h.download_part(1)
    h.wait_log(f"click {zip_name(1, 3)}")
    # Until the blob URL is revoked the part is still held, so no other part may start.
    if "revoke" not in h.log():
        expect(h.part_button(2)).to_be_disabled()
    expect(h.part_button(2)).to_be_enabled()
    assert h.log()[-1] == "revoke"


def test_cancel_aborts_the_request(plain_page: Harness) -> None:
    h = plain_page
    h.server.hold = {2}
    h.upload_and_download()
    h.download_part(2)
    expect(h.status()).to_have_text("Generating part 2 of 3…")
    with h.page.expect_event("requestfailed") as failed:
        h.page.click("#cancel")
    assert failed.value.url.endswith("/generate")
    expect(h.status()).to_have_text("Cancelled: part 2 of 3 was not downloaded.")
    expect(h.part_status(2)).to_have_text("Cancelled")
    expect(h.part_button(2)).to_have_text("Download")
    for n in (1, 2, 3):
        expect(h.part_button(n)).to_be_enabled()
    expect(h.page.locator("#cancel")).to_be_hidden()
    expect(h.page.locator("#retry")).to_be_hidden()
    assert h.log() == ["fetch 2", "cancel fetch 2"]

    h.server.release()  # the aborted request is gone: nothing reaches the page
    h.page.wait_for_timeout(100)
    assert h.clicks() == []

    h.download_part(2)  # the same part again, from the start
    expect(h.part_status(2)).to_have_text("Downloaded")
    assert h.fetches() == ["fetch 2", "fetch 2"]
    assert h.clicks() == [f"click {zip_name(2, 3)}"]


def test_a_downloaded_part_can_be_downloaded_again(plain_page: Harness) -> None:
    h = plain_page
    h.upload_and_download()
    h.download_part(1)
    expect(h.part_button(1)).to_have_text("Download again")
    expect(h.part_button(1)).to_be_enabled()
    h.download_part(1)
    expect(h.part_button(1)).to_be_enabled()
    expect(h.part_status(1)).to_have_text("Downloaded")
    expect(h.status()).to_have_text("Part 1 of 3 was downloaded. 1 of 3 parts downloaded.")
    assert h.log() == [
        "fetch 1", f"click {zip_name(1, 3)}", "revoke",
        "fetch 1", f"click {zip_name(1, 3)}", "revoke",
    ]  # fmt: skip
    assert h.bar() == (1, 3)

    # A cancelled download-again leaves the part marked as downloaded.
    h.server.hold = {1}
    h.download_part(1)
    expect(h.status()).to_have_text("Generating part 1 of 3…")
    h.page.click("#cancel")
    expect(h.status()).to_have_text("Cancelled: part 1 of 3 was not downloaded again.")
    expect(h.part_status(1)).to_have_text("Downloaded")
    expect(h.part_button(1)).to_have_text("Download again")


def test_download_next_part_takes_the_first_part_not_downloaded(plain_page: Harness) -> None:
    h = plain_page
    h.upload_and_download()
    h.download_part(2)
    expect(h.part_status(2)).to_have_text("Downloaded")
    expect(h.page.locator("#next-part")).to_be_enabled()

    h.page.click("#next-part")  # part 1: the lowest part not downloaded
    expect(h.part_status(1)).to_have_text("Downloaded")
    expect(h.page.locator("#next-part")).to_be_enabled()
    h.page.click("#next-part")  # part 3: part 2 is already downloaded
    expect(h.status()).to_have_text("Done: all 3 parts were downloaded.")
    expect(h.page.locator("#next-part")).to_be_hidden()
    assert h.fetches() == ["fetch 2", "fetch 1", "fetch 3"]
    assert h.clicks() == [f"click {zip_name(n, 3)}" for n in (2, 1, 3)]
    assert h.bar() == (3, 3)
    for n in (1, 2, 3):
        expect(h.part_button(n)).to_have_text("Download again")


def test_next_part_in_order_from_the_start(plain_page: Harness) -> None:
    h = plain_page
    h.upload_and_download()
    for n in (1, 2, 3):
        expect(h.page.locator("#next-part")).to_be_enabled()
        h.page.click("#next-part")
        expect(h.part_status(n)).to_have_text("Downloaded")
    expect(h.status()).to_have_text("Done: all 3 parts were downloaded.")
    assert h.fetches() == ["fetch 1", "fetch 2", "fetch 3"]


def test_a_failed_part_can_be_tried_again(plain_page: Harness) -> None:
    h = plain_page
    h.server.fail_once = {2: (503, "The server is busy. Please try again in a minute.")}
    h.upload_and_download()
    h.download_part(2)
    expect(h.status()).to_have_text(
        "Part 2 of 3 failed. The server is busy. Please try again in a minute."
    )
    expect(h.part_status(2)).to_have_text("Failed")
    expect(h.part_button(2)).to_have_text("Try again")
    expect(h.part_button(2)).to_have_accessible_name("Try part 2 again")
    expect(h.page.locator("#retry")).to_be_hidden()
    h.download_part(2)
    expect(h.part_status(2)).to_have_text("Downloaded")
    assert h.fetches() == ["fetch 2", "fetch 2"]


def test_a_part_that_no_longer_validates_shows_the_table(plain_page: Harness) -> None:
    h = plain_page
    table = (
        '<!DOCTYPE html><div id="results"><section class="results"><table><tbody>'
        '<tr class="error"><td>7</td><td>iccid</td><td>Synthetic problem</td><td>error</td>'
        "</tr></tbody></table></section></div>"
    )
    h.upload_and_download()
    h.page.unroute(f"{ORIGIN}/**")
    h.page.route(f"{ORIGIN}/generate", lambda r: r.fulfill(status=422, body=table))
    h.download_part(1)
    expect(h.status()).to_have_text("Stopped at part 1 of 3: the file did not pass validation.")
    expect(h.page.locator("#results tr.error")).to_have_count(1)
    expect(h.page.locator("#parts-manual")).to_be_hidden()
    expect(h.page.locator("#next-part")).to_be_hidden()
    expect(h.page.locator("#download")).to_be_hidden()  # the table replaced the results


def test_single_part_without_folder_api_is_downloaded_at_once(plain_page: Harness) -> None:
    h = plain_page
    h.server.rows = CHUNK
    h.upload_and_download()
    expect(h.status()).to_have_text("Done: the ZIP file was generated; your browser is saving it.")
    assert h.log() == ["fetch 1", f"click {zip_name(1, 1)}", "revoke"]
    expect(h.page.locator("#parts-manual")).to_be_hidden()


def test_cancel_a_single_part(plain_page: Harness) -> None:
    h = plain_page
    h.server.rows = CHUNK
    h.server.hold = {1}
    h.upload_and_download()
    expect(h.status()).to_have_text("Generating part 1 of 1…")
    h.page.click("#cancel")
    expect(h.status()).to_have_text(
        "Cancelled before part 1 of 1 was generated. Nothing was handed to your browser."
    )
    expect(h.page.locator("#retry")).to_have_text("Start again")
    h.page.click("#retry")
    expect(h.status()).to_have_text("Done: the ZIP file was generated; your browser is saving it.")


# --- Continuing an unfinished batch -------------------------------------------------------


def test_continuing_opens_the_parts_list_with_parts_marked(continue_page: Harness) -> None:
    h = continue_page
    h.upload_and_download()
    expect(h.page.locator("#parts-manual")).to_be_visible()
    assert "picker" not in h.log()  # no folder mode: the parts left are not in sequence
    expect(h.status()).to_have_text(
        "Continuing this batch: 2 of 4 parts were downloaded before. "
        "Download the remaining parts one by one."
    )
    for n, status in (
        (1, "Downloaded"),
        (2, "Not downloaded"),
        (3, "Downloaded"),
        (4, "Not downloaded"),
    ):
        expect(h.part_status(n)).to_have_text(status)
    expect(h.part_button(1)).to_have_text("Download again")
    expect(h.part_button(2)).to_have_text("Download")
    assert h.bar() == (2, 4)

    h.page.click("#next-part")  # the first part not downloaded
    expect(h.part_status(2)).to_have_text("Downloaded")
    expect(h.page.locator("#next-part")).to_be_enabled()
    h.page.click("#next-part")
    expect(h.status()).to_have_text("Done: all 4 parts were downloaded.")
    assert h.fetches() == ["fetch 2", "fetch 4"]
    assert h.clicks() == [f"click {zip_name(2, 4)}", f"click {zip_name(4, 4)}"]

    h.download_part(1)  # download again
    expect(h.part_button(1)).to_be_enabled()
    expect(h.status()).to_contain_text("Done: all 4 parts were downloaded.")
    assert h.fetches() == ["fetch 2", "fetch 4", "fetch 1"]
