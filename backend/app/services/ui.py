"""Phone-vs-desktop UI selection.

"Mobi" appears in every phone browser's user-agent and in no desktop or
tablet one, so it is the whole detection. The cookie overrides in BOTH
directions ("Desktop version" link at the phone UI's foot, a phone button
in the desktop header), and "auto" falls back to the user-agent.
"""
from fastapi import Request

COOKIE = "fin_ui"


def is_phone(request: Request) -> bool:
    chosen = request.cookies.get(COOKIE, "").strip().lower()
    if chosen == "phone":
        return True
    if chosen == "desktop":
        return False
    return "Mobi" in request.headers.get("user-agent", "")


def forwarded_prefix(request: Request) -> str:
    """Subpath the reverse proxy serves us under ("" when reached directly).

    The PWA manifest's start_url and icon paths must carry it, or an icon
    installed through a proxy opens the proxy root instead of this app.
    """
    return request.headers.get("x-forwarded-prefix", "").rstrip("/")
