"""Login and logout. Two fixed users, no registration, no reset flows.

Passwords are set from the terminal (`scripts/set_password.py`); the session
is a JWT in an httpOnly cookie (`services/auth.py`). The middleware in
main.py is what enforces auth everywhere else — these routes are on its
allowlist.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.services.auth import COOKIE_NAME, TOKEN_LIFETIME, authenticate, create_token

router = APIRouter(tags=["auth"])

templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "templates")


def _safe_next(next_path: str | None) -> str:
    """Only ever redirect within the app: no scheme, no host, no `//host`."""
    if next_path and next_path.startswith("/") and not next_path.startswith("//"):
        return next_path
    return "/"


@router.get("/login")
def login_page(request: Request, next: str = "/"):
    return templates.TemplateResponse(
        request,
        "login.html",
        {"app_name": settings.app_name, "error": None, "next": _safe_next(next)},
    )


@router.post("/login")
def login_submit(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    user = authenticate(db, login, password)
    if user is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "app_name": settings.app_name,
                "error": "Wrong user or password.",
                "next": _safe_next(next),
            },
            status_code=401,
        )

    response = RedirectResponse(_safe_next(next), status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        create_token(user.id),
        max_age=int(TOKEN_LIFETIME.total_seconds()),
        httponly=True,
        samesite="lax",
        # NOT `secure`, so the session cookie survives plain http on a trusted
        # home LAN. If you expose this app beyond such a network, set
        # secure=True and serve it over TLS.
        path="/",
    )
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    return response
