"""No page may need the internet to render.

`the project's constraints`: "No internet dependency at runtime, apart from the Plaid calls."
Tailwind, HTMX, Alpine and Chart.js used to come from CDNs, so an offline
house got unstyled, dead pages. They are vendored under `app/static/vendor/`
now, and this guard keeps them there: it scans every template for `<script>`
and stylesheet `<link>` tags pointing at an external host.

Anchors and prose may link outward freely (the household portal link, docs
links) — only asset tags are checked.
"""
from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"

# Bank-sync widgets: the exemption the project's constraints already grants. Each must match
# its provider's live backend, and neither can do anything offline.
ALLOWED_HOSTS = {
    "cdn.plaid.com",
    "cdn.pluggy.ai",
}

SCRIPT_SRC = re.compile(r"<script[^>]*\ssrc=[\"']([^\"']+)[\"']", re.IGNORECASE)
LINK_TAG = re.compile(r"<link[^>]*>", re.IGNORECASE)
LINK_HREF = re.compile(r"\shref=[\"']([^\"']+)[\"']", re.IGNORECASE)
STYLESHEET = re.compile(r"\srel=[\"'][^\"']*stylesheet[^\"']*[\"']", re.IGNORECASE)
HOST = re.compile(r"^(?:https?:)?//([^/]+)", re.IGNORECASE)


def _asset_urls(html: str) -> list[str]:
    urls = SCRIPT_SRC.findall(html)
    for tag in LINK_TAG.findall(html):
        if not STYLESHEET.search(tag):
            continue
        href = LINK_HREF.search(tag)
        if href:
            urls.append(href.group(1))
    return urls


def _external_host(url: str) -> str | None:
    match = HOST.match(url.strip())
    return match.group(1).lower() if match else None


def test_no_template_loads_an_asset_from_an_external_host():
    offenders: list[str] = []
    for template in sorted(TEMPLATES.rglob("*.html")):
        for url in _asset_urls(template.read_text(encoding="utf-8")):
            host = _external_host(url)
            if host and host not in ALLOWED_HOSTS:
                offenders.append(f"{template.relative_to(TEMPLATES)}: {url}")

    assert not offenders, "templates load assets from external hosts:\n" + "\n".join(
        offenders
    )


def test_every_vendored_asset_a_template_asks_for_exists():
    vendor = Path(__file__).resolve().parents[1] / "app" / "static" / "vendor"
    referenced: set[str] = set()
    for template in TEMPLATES.rglob("*.html"):
        for url in _asset_urls(template.read_text(encoding="utf-8")):
            if url.startswith("/static/vendor/"):
                referenced.add(url.split("?", 1)[0].rsplit("/", 1)[-1])

    assert referenced, "no vendored asset is referenced any more — did a CDN creep back?"
    missing = sorted(name for name in referenced if not (vendor / name).is_file())
    assert not missing, f"templates reference missing vendor files: {missing}"


def test_the_four_vendored_libraries_are_real_files():
    vendor = Path(__file__).resolve().parents[1] / "app" / "static" / "vendor"
    expected = {
        "tailwind-3.4.17.js": 100_000,
        "htmx-1.9.12.min.js": 20_000,
        "alpine-3.14.1.min.js": 20_000,
        "chart-4.4.4.min.js": 50_000,
    }
    for name, floor in expected.items():
        path = vendor / name
        assert path.is_file(), f"{name} is missing from app/static/vendor/"
        assert path.stat().st_size > floor, f"{name} looks truncated or is an error page"
