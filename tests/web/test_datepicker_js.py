"""app.js: the History page's calendar popup, driven by the keyboard.

Runs the real app.js in headless Chromium (Playwright, from the dev extras) on the real
history.html, served under the real Content-Security-Policy. Playwright answers every
request itself (no app server, no database). The browser clock is fixed at 17/11/2026.

Skips when Playwright or its Chromium is not installed
(``.venv\\Scripts\\python.exe -m playwright install chromium``).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from app.web.history import NOTE  # noqa: E402
from app.web.main import CONTENT_SECURITY_POLICY, STATIC_DIR  # noqa: E402
from app.web.templating import templates  # noqa: E402

expect = sync_api.expect
Page = sync_api.Page
Route = sync_api.Route

ORIGIN = "https://esim.test"
FROM_DATE = "15/03/2026"  # a Sunday


def render_history(from_date: str) -> str:
    return templates.env.get_template("history.html").render(
        fields={"from_date": from_date, "from_time": "00:00", "to_date": "", "to_time": ""},
        note=NOTE,
        is_admin=False,
        error=None,
        result=None,
        org="",
        orgs=[],
        current_user=None,
        csrf_token=None,
        pending_requests=0,
    )


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
class Calendar:
    page: Page

    @property
    def field(self) -> sync_api.Locator:
        return self.page.locator("#from_date")

    @property
    def toggle(self) -> sync_api.Locator:
        return self.page.locator("[data-datepicker]:has(#from_date) .datepicker-toggle")

    @property
    def popup(self) -> sync_api.Locator:
        return self.page.locator(".datepicker-popup")

    @property
    def title(self) -> sync_api.Locator:
        return self.popup.locator("h2")

    def focused(self) -> str | None:
        """The ``data-date`` of the focused element ("" when it has none)."""
        return self.page.evaluate("document.activeElement.dataset.date || ''")

    def focused_label(self) -> str:
        return self.page.evaluate("document.activeElement.getAttribute('aria-label') || ''")

    def open_by_keyboard(self) -> None:
        self.field.focus()
        self.page.keyboard.press("Tab")  # to the calendar button
        expect(self.toggle).to_be_focused()
        self.page.keyboard.press("Enter")
        expect(self.popup).to_be_visible()

    def press(self, *keys: str) -> None:
        for key in keys:
            self.page.keyboard.press(key)


def make_calendar(browser: sync_api.Browser, from_date: str) -> Iterator[Calendar]:
    context = browser.new_context()
    page = context.new_page()
    problems: list[str] = []
    page.on("pageerror", lambda error: problems.append(f"page error: {error}"))
    page.on(
        "console",
        lambda m: (
            problems.append(m.text)
            if m.type == "error" or "Content Security Policy" in m.text
            else None
        ),
    )
    page.clock.set_fixed_time(datetime(2026, 11, 17, 10, 0))  # noqa: DTZ001 (browser local time)

    def handle(route: Route) -> None:
        path = route.request.url.removeprefix(ORIGIN).split("?")[0]
        if path == "/history":
            route.fulfill(
                body=render_history(from_date),
                content_type="text/html",
                headers={"Content-Security-Policy": CONTENT_SECURITY_POLICY},
            )
        elif path.startswith("/static/"):
            route.fulfill(path=STATIC_DIR / path.removeprefix("/static/"))
        else:
            route.fulfill(status=404, body="")

    page.route(f"{ORIGIN}/**", handle)
    page.goto(f"{ORIGIN}/history")
    yield Calendar(page)
    context.close()
    assert not problems, problems  # no CSP violation, no script error


@pytest.fixture
def calendar(browser: sync_api.Browser) -> Iterator[Calendar]:
    yield from make_calendar(browser, FROM_DATE)


@pytest.fixture
def calendar_bad_value(browser: sync_api.Browser) -> Iterator[Calendar]:
    yield from make_calendar(browser, "31/02/2026")


def test_every_date_field_gets_a_calendar_button(calendar: Calendar) -> None:
    toggles = calendar.page.locator(".datepicker-toggle")
    expect(toggles).to_have_count(2)  # From and To dates; the time fields have none
    expect(calendar.toggle).to_have_attribute("aria-haspopup", "dialog")
    expect(calendar.toggle).to_have_attribute("aria-expanded", "false")
    expect(calendar.toggle).to_have_attribute("aria-label", "Calendar for From date")
    expect(calendar.popup).to_have_count(0)


def test_opens_by_keyboard_on_the_fields_date(calendar: Calendar) -> None:
    calendar.open_by_keyboard()
    expect(calendar.toggle).to_have_attribute("aria-expanded", "true")
    expect(calendar.popup).to_have_attribute("role", "dialog")
    expect(calendar.title).to_have_text("March 2026")
    assert calendar.focused() == FROM_DATE
    assert calendar.focused_label() == "Sunday 15 March 2026"
    selected = calendar.popup.locator("td[aria-selected='true'] button")
    expect(selected).to_have_text("15")
    headers = calendar.popup.locator("thead th")
    expect(headers).to_have_text(["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"])


def test_arrow_home_end_and_page_keys_move_the_focus(calendar: Calendar) -> None:
    calendar.open_by_keyboard()
    steps = [
        ("ArrowRight", "16/03/2026", "March 2026"),
        ("ArrowDown", "23/03/2026", "March 2026"),
        ("ArrowLeft", "22/03/2026", "March 2026"),
        ("ArrowUp", "15/03/2026", "March 2026"),
        ("Home", "09/03/2026", "March 2026"),  # Monday of that week
        ("End", "15/03/2026", "March 2026"),  # Sunday
        ("ArrowUp", "08/03/2026", "March 2026"),
        ("ArrowUp", "01/03/2026", "March 2026"),
        ("ArrowLeft", "28/02/2026", "February 2026"),  # into the previous month
        ("PageDown", "28/03/2026", "March 2026"),
        ("Shift+PageDown", "28/03/2027", "March 2027"),
        ("Shift+PageUp", "28/03/2026", "March 2026"),
        ("PageUp", "28/02/2026", "February 2026"),
    ]
    for key, date, month in steps:
        calendar.press(key)
        assert calendar.focused() == date, key
        expect(calendar.title).to_have_text(month)
    # Only the focused day is in the Tab order.
    tab_stops = calendar.popup.locator(".datepicker-day[tabindex='0']")
    expect(tab_stops).to_have_count(1)
    expect(tab_stops).to_have_attribute("data-date", "28/02/2026")


def test_page_down_keeps_the_day_or_takes_the_months_last(calendar: Calendar) -> None:
    calendar.open_by_keyboard()
    calendar.press("PageUp", "PageUp")  # 15/01/2026
    for _ in range(16):  # to 31/01/2026
        calendar.press("ArrowRight")
    assert calendar.focused() == "31/01/2026"
    calendar.press("PageDown")
    assert calendar.focused() == "28/02/2026"


def test_enter_picks_the_date_and_returns_focus(calendar: Calendar) -> None:
    calendar.open_by_keyboard()
    calendar.press("ArrowRight", "ArrowRight", "Enter")
    expect(calendar.field).to_have_value("17/03/2026")
    expect(calendar.popup).to_have_count(0)
    expect(calendar.toggle).to_be_focused()
    expect(calendar.toggle).to_have_attribute("aria-expanded", "false")
    # Opening again starts on the picked date.
    calendar.press("Enter")
    assert calendar.focused() == "17/03/2026"


def test_space_picks_the_date_too(calendar: Calendar) -> None:
    calendar.open_by_keyboard()
    calendar.press("PageDown", "Space")
    expect(calendar.field).to_have_value("15/04/2026")
    expect(calendar.toggle).to_be_focused()


def test_escape_closes_without_a_change(calendar: Calendar) -> None:
    calendar.open_by_keyboard()
    calendar.press("ArrowDown", "PageDown", "Escape")
    expect(calendar.popup).to_have_count(0)
    expect(calendar.field).to_have_value(FROM_DATE)
    expect(calendar.toggle).to_be_focused()


def test_tab_stays_inside_the_popup(calendar: Calendar) -> None:
    calendar.open_by_keyboard()
    calendar.press("Tab")
    assert calendar.focused_label() == "Previous month"
    calendar.press("Tab")
    assert calendar.focused_label() == "Next month"
    calendar.press("Tab")
    assert calendar.focused() == FROM_DATE
    calendar.press("Shift+Tab")
    assert calendar.focused_label() == "Next month"
    calendar.press("Enter")  # the month buttons work by keyboard too
    expect(calendar.title).to_have_text("April 2026")
    assert calendar.focused_label() == "Next month"
    calendar.press("Tab")
    assert calendar.focused() == "15/04/2026"


def test_a_bad_value_opens_on_today(calendar_bad_value: Calendar) -> None:
    c = calendar_bad_value
    c.open_by_keyboard()
    expect(c.title).to_have_text("November 2026")
    assert c.focused() == "17/11/2026"
    expect(c.popup.locator("[aria-current='date']")).to_have_text("17")
    expect(c.popup.locator("td[aria-selected='true']")).to_have_count(0)


def test_typing_still_works_and_a_click_outside_closes(calendar: Calendar) -> None:
    calendar.field.fill("01/02/2026")
    calendar.toggle.click()
    expect(calendar.title).to_have_text("February 2026")
    calendar.page.locator("h1").click()
    expect(calendar.popup).to_have_count(0)
    expect(calendar.field).to_have_value("01/02/2026")
    calendar.toggle.click()
    calendar.popup.locator("[data-date='10/02/2026']").click()
    expect(calendar.field).to_have_value("10/02/2026")
