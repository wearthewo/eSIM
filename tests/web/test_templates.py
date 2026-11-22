"""The page templates keep the strict CSP: no inline code, assets only from /static.

- Static checks read every template and app.css (no database needed).
- Rendered checks fetch every page as the user who can see it (need TEST_DATABASE_URL).
- The colour tokens in app.css meet WCAG AA in the light and the dark scheme.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import AccessRequest, Role, VolumeBand
from app.web.main import CONTENT_SECURITY_POLICY, STATIC_DIR
from app.web.templating import TEMPLATES_DIR
from tests.web.conftest import Accounts, LoggedIn

TEMPLATES = sorted(TEMPLATES_DIR.glob("*.html"))
JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)", re.I)
STYLE_ATTR = re.compile(r"\sstyle\s*=", re.I)
EVENT_HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)
# src/href of the tags that load something: stylesheets, icons, preloads, scripts, images.
ASSET_REF = re.compile(
    r"<(?:link|script|img|source|iframe|embed|object)\b"
    r'[^>]*?\b(?:src|href|data)="([^"]*)"',
    re.I,
)
ANY_URL_ATTR = re.compile(r'\b(?:src|href|action|formaction|poster|srcset)="([^"]*)"', re.I)
CSS_URL = re.compile(r"url\(\s*['\"]?([^'\")]+)")


def _source(path: Path) -> str:
    """The template without Jinja comments (they may mention <style> or <img>)."""
    return JINJA_COMMENT.sub("", path.read_text(encoding="utf-8"))


def _assert_no_inline_code(html: str, where: str) -> None:
    assert "<style" not in html.lower(), f"<style> element in {where}"
    assert not STYLE_ATTR.search(html), f"style= attribute in {where}"
    assert not INLINE_SCRIPT.search(html), f"inline <script> in {where}"
    assert not EVENT_HANDLER.search(html), f"inline event handler in {where}"
    assert "hx-on" not in html, f"hx-on in {where}"


def _assert_local_urls(html: str, where: str) -> None:
    for ref in ASSET_REF.findall(html):
        assert ref.startswith("/static/"), f"{where} loads {ref!r} from outside /static"
    for url in ANY_URL_ATTR.findall(html):
        lowered = url.strip().lower()
        assert not lowered.startswith(("http:", "https:", "//", "javascript:", "data:")), (
            f"{where} links to {url!r}"
        )


# --- Static: the template sources ---------------------------------------------------------


def test_templates_found() -> None:
    assert len(TEMPLATES) >= 10


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_template_has_no_inline_code(path: Path) -> None:
    _assert_no_inline_code(_source(path), path.name)


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_template_links_only_to_static_assets(path: Path) -> None:
    source = _source(path)
    _assert_local_urls(source, path.name)
    assert "http://" not in source and "https://" not in source, path.name


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_template_static_references_exist(path: Path) -> None:
    for ref in re.findall(r'"/static/([^"{]+)"', _source(path)):
        assert (STATIC_DIR / ref).is_file(), f"{path.name}: /static/{ref} does not exist"


def test_stylesheet_loads_only_static_files() -> None:
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert "@import" not in css
    assert "http://" not in css and "https://" not in css
    urls = CSS_URL.findall(css)
    assert urls, "the bundled font should be referenced"
    for url in urls:
        assert url.startswith("/static/"), url
        assert (STATIC_DIR / url.removeprefix("/static/")).is_file(), url


def test_bundled_font_and_licence() -> None:
    font = STATIC_DIR / "fonts" / "InterVariable.woff2"
    assert font.read_bytes()[:4] == b"wOF2"
    assert "SIL Open Font License" in (STATIC_DIR / "fonts" / "OFL.txt").read_text("utf-8")


def test_csp_allows_only_own_fonts() -> None:
    assert "font-src 'self'" in CONTENT_SECURITY_POLICY
    assert "default-src 'none'" in CONTENT_SECURITY_POLICY
    assert "unsafe-inline" not in CONTENT_SECURITY_POLICY


# --- Contrast: the colour tokens in app.css -------------------------------------------------

# (foreground, background, minimum ratio): text 4.5:1, UI components and focus 3:1.
CONTRAST_PAIRS = [
    ("text", "bg", 4.5),
    ("text", "surface", 4.5),
    ("text-muted", "surface", 4.5),
    ("text-muted", "surface-2", 4.5),
    ("text-muted", "bg", 4.5),
    ("primary", "surface", 4.5),  # links
    ("primary", "bg", 4.5),
    ("on-primary-soft", "primary-soft", 4.5),  # current nav item, info alert, badge
    ("btn-primary-fg", "btn-primary-bg", 4.5),
    ("btn-primary-fg", "btn-primary-hover", 4.5),
    ("btn-primary-fg", "btn-primary-active", 4.5),
    ("btn-secondary-fg", "btn-secondary-bg", 4.5),
    ("btn-secondary-fg", "btn-secondary-hover", 4.5),
    ("btn-secondary-fg", "bg", 4.5),  # ghost buttons (transparent) on the page
    ("btn-secondary-fg", "surface", 4.5),  # ghost buttons on a card or the top bar
    ("btn-danger-fg", "surface", 4.5),
    ("btn-danger-hover-fg", "btn-danger-hover-bg", 4.5),
    ("btn-disabled-fg", "btn-disabled-bg", 4.5),
    ("success", "success-soft", 4.5),
    ("warning", "warning-soft", 4.5),
    ("error", "error-soft", 4.5),
    ("success", "surface", 4.5),
    ("warning", "surface", 4.5),
    ("error", "surface", 4.5),
    ("focus", "surface", 3.0),
    ("focus", "bg", 3.0),
    ("border-strong", "surface", 3.0),  # input borders
]


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})\s*;", block))


def _schemes() -> dict[str, dict[str, str]]:
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    light = re.search(r"^:root\s*\{(.*?)^\}", css, re.S | re.M)
    dark = re.search(r"@media \(prefers-color-scheme: dark\)\s*\{\s*:root\s*\{(.*?)\}", css, re.S)
    assert light and dark
    light_tokens = _tokens(light.group(1))
    return {"light": light_tokens, "dark": light_tokens | _tokens(dark.group(1))}


def _luminance(hex_colour: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_contrast_helper() -> None:
    assert round(contrast("#000000", "#FFFFFF"), 1) == 21.0


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_colour_tokens_meet_wcag_aa(scheme: str) -> None:
    tokens = _schemes()[scheme]
    failures = [
        f"{fg} on {bg}: {contrast(tokens[fg], tokens[bg]):.2f} < {minimum}"
        for fg, bg, minimum in CONTRAST_PAIRS
        if contrast(tokens[fg], tokens[bg]) < minimum
    ]
    assert not failures, failures


# --- Rendered: every page as the user who can open it -----------------------------------------


def _assert_page_is_clean(c: TestClient, url: str, expected_status: int = 200) -> None:
    response = c.get(url)
    assert response.status_code == expected_status, url
    assert "font-src 'self'" in response.headers["content-security-policy"]
    html = response.text
    _assert_no_inline_code(html, url)
    _assert_local_urls(html, url)
    assert html.count("<script") == html.count('<script src="/static/'), url
    for ref in sorted(set(ASSET_REF.findall(html))):
        assert c.get(ref).status_code == 200, f"{url}: {ref}"


@pytest.mark.parametrize(
    ("url", "status"),
    [
        ("/login", 200),
        ("/request-access", 200),
        ("/privacy", 200),
        ("/terms", 200),
        ("/invite/" + "x" * 43, 404),
    ],
)
def test_public_pages_are_clean(anon: TestClient, url: str, status: int) -> None:
    _assert_page_is_clean(anon, url, status)


@pytest.mark.parametrize("url", ["/", "/team"])
def test_owner_pages_are_clean(client: TestClient, url: str) -> None:
    _assert_page_is_clean(client, url)


def test_admin_pages_are_clean(web_app: FastAPI, accts: Accounts) -> None:
    admin_id = accts.user("admin@org.test", superadmin=True, role=Role.MEMBER)
    org_id = accts.org("Clean Pages Org (test)")
    accts.user("member@org.test", org_id=org_id, role=Role.MEMBER, password_hash=None)

    def _add(db: Session) -> None:
        db.add(
            AccessRequest(
                company_name="Pending Co (test)",
                contact_name="Test Applicant",
                email="applicant@org.test",
                volume_band=VolumeBand.UNDER_1K,
                note="Synthetic note.",
            )
        )

    accts.run(_add)
    admin = LoggedIn(web_app, admin_id, accts.session(admin_id))
    with admin.client() as c:
        for url in (
            "/",
            "/admin/orgs",
            f"/admin/orgs/{org_id}",
            "/admin/requests",
            "/admin/requests?show=handled",
        ):
            _assert_page_is_clean(c, url)


def test_font_is_served(anon: TestClient) -> None:
    response = anon.get("/static/fonts/InterVariable.woff2")
    assert response.status_code == 200
    assert response.content[:4] == b"wOF2"
