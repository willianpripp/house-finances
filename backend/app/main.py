from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from urllib.parse import quote

from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.db import SessionLocal, engine, get_db
from app.services import household, ui
from app.services.import_hints import hints_for_payment_methods
from app.services.auth import COOKIE_NAME, user_id_from_token
from app.services.plaid_client import is_configured as plaid_is_configured
from app.services.pluggy_client import is_configured as pluggy_is_configured
from app.routers import assets as assets_router
from app.routers import auth as auth_router
from app.routers import categories as categories_router
from app.routers import categorization_rules as categorization_rules_router
from app.routers import debts as debts_router
from app.routers import exchange_rates as exchange_rates_router
from app.routers import home as home_router
from app.routers import imports as imports_router
from app.routers import income as income_router
from app.routers import merchants as merchants_router
from app.routers import payment_methods as payment_methods_router
from app.routers import receivables as receivables_router
from app.routers import reports as reports_router
from app.routers import savings as savings_router
from app.routers import transactions as transactions_router
from app.routers import users as users_router
from app.routers import warnings as warnings_router
from app.routers import plaid as plaid_router
from app.routers import pluggy as pluggy_router

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # First, before anything touches data or banks: refuse to serve a database
    # the code was not migrated for. Raising here kills the boot; the fix is
    # `docker compose run --rm app alembic upgrade head`.
    from app.services.schema_guard import assert_schema_current
    assert_schema_current(engine)

    # An unset AUTH_SECRET in production would mean every session token is
    # unverifiable and every route just redirects to /login: an outage that
    # looks like being logged out. Refuse loudly instead.
    if settings.environment == "production" and not settings.auth_secret:
        raise RuntimeError(
            "AUTH_SECRET is not set. Generate one "
            "(python -c 'import secrets; print(secrets.token_urlsafe(48))') "
            "and put it in the app's .env as AUTH_SECRET."
        )

    # Boot only refreshes BALANCES (safe, just snapshots). Transactions are
    # pulled on demand through the /connections review flow (preview before
    # commit) — never auto-committed. Quiet on failure; UI surfaces errors.
    if plaid_is_configured():
        try:
            from app.services.plaid_balances import refresh_balances_for_all_items
            with SessionLocal() as session:
                refresh_balances_for_all_items(session)
        except Exception:
            pass
    if pluggy_is_configured():
        try:
            from app.services.pluggy_balances import (
                refresh_balances_for_all_items as pluggy_refresh_all,
            )
            with SessionLocal() as session:
                pluggy_refresh_all(session)
        except Exception:
            pass
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
# Deployment config every template may need, without threading it through
# each route's context.
templates.env.globals["portal_url"] = settings.portal_url

# Pages with a phone-shaped version in templates/phone/. Grows as screens are
# built; anything not in here serves the desktop template on every device
# (kept usable there by the narrow-screen safety net in styles.css).
PHONE_PAGES = {
    "index.html",
    "receivables.html",
    "connections.html",
    "reports_monthly.html",
    "transactions.html",
    "debts.html",
    "savings.html",
    "income.html",
    "exchange_rates.html",
    "assets.html",
    "warnings.html",
    "reports_annual.html",
    # Deliberately desktop-only: imports.html (PDF/paste is a desktop task),
    # guide.html, rules.html.
}


def render(request: Request, name: str, ctx: dict):
    """Every page route goes through here; phone/desktop is decided per
    request: shared routes and context, swapped templates."""
    ctx.setdefault("base", ui.forwarded_prefix(request))
    if ui.is_phone(request) and name in PHONE_PAGES:
        return templates.TemplateResponse(request, f"phone/{name}", ctx)
    return templates.TemplateResponse(request, name, ctx)

@app.middleware("http")
async def require_session(request: Request, call_next):
    """Auth for every route — middleware, not per-router dependencies, so a
    future router cannot be forgotten. Browsers get the login page; API
    callers get a 401. Exempt: /login and /logout (the door itself), /health
    (the container healthcheck needs it unauthenticated; it exposes only
    liveness, no data), /static (stylesheets, not data), and the PWA
    manifest (Add to Home Screen must work before login)."""
    path = request.url.path
    if path.rstrip("/") in (
        "/login", "/logout", "/health", "/manifest.webmanifest", "/favicon.ico"
    ) or path.startswith("/static/"):
        return await call_next(request)

    token = request.cookies.get(COOKIE_NAME)
    if token:
        user_id = user_id_from_token(token)
        if user_id is not None:
            request.state.user_id = user_id
            return await call_next(request)

    if "text/html" in request.headers.get("accept", ""):
        target = quote(path + (f"?{request.url.query}" if request.url.query else ""))
        return RedirectResponse(f"/login?next={target}", status_code=303)
    return JSONResponse({"detail": "Not authenticated"}, status_code=401)

app.include_router(auth_router.router)
app.include_router(assets_router.router)
app.include_router(categories_router.router)
app.include_router(categorization_rules_router.router)
app.include_router(debts_router.router)
app.include_router(exchange_rates_router.router)
app.include_router(home_router.router)
app.include_router(imports_router.router)
app.include_router(income_router.router)
app.include_router(merchants_router.router)
app.include_router(payment_methods_router.router)
app.include_router(receivables_router.router)
app.include_router(reports_router.router)
app.include_router(savings_router.router)
app.include_router(transactions_router.router)
app.include_router(users_router.router)
app.include_router(warnings_router.router)
app.include_router(plaid_router.router)
app.include_router(pluggy_router.router)


def check_db() -> tuple[bool, str]:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True, "Connected"
    except SQLAlchemyError as exc:
        return False, str(exc.__cause__ or exc)


def _current_user_name(request: Request) -> str:
    """Display name of the session's user, for greetings. Empty when unknown."""
    user_id = getattr(request.state, "user_id", None)
    if user_id is None:
        return ""
    from app.models import User

    with SessionLocal() as session:
        user = session.get(User, user_id)
        return user.name if user else ""


def _salary_labels() -> dict[str, str]:
    """Member-facing labels for the two salary income sources. Resolved from
    the household config so no template carries a member's name."""
    with SessionLocal() as session:
        return household.salary_labels(session)


@app.get("/")
def index(request: Request):
    db_ok, db_message = check_db()
    return render(
        request,
        "index.html",
        {
            "app_name": settings.app_name,
            "environment": settings.environment,
            "db_ok": db_ok,
            "db_message": db_message,
            "user_name": _current_user_name(request),
        },
    )


@app.get("/guide")
def guide_page(request: Request):
    db_ok, db_message = check_db()
    return render(
        request,
        "guide.html",
        {
            "app_name": settings.app_name,
            "environment": settings.environment,
            "db_ok": db_ok,
            "db_message": db_message,
        },
    )


@app.get("/warnings")
def warnings_page(request: Request):
    return render(
        request,
        "warnings.html",
        {
            "app_name": settings.app_name,
            "environment": settings.environment,
        },
    )


@app.get("/imports")
def imports_page(request: Request, db: Session = Depends(get_db)):
    return render(
        request,
        "imports.html",
        {
            "app_name": settings.app_name,
            "environment": settings.environment,
            "import_hints": hints_for_payment_methods(db),
        },
    )


@app.get("/connections")
def connections_page(request: Request):
    return render(
        request,
        "connections.html",
        {
            "app_name": settings.app_name,
            "environment": settings.environment,
            "plaid_configured": plaid_is_configured(),
            "pluggy_configured": pluggy_is_configured(),
        },
    )


@app.get("/transactions")
def transactions_page(request: Request):
    return render(
        request,
        "transactions.html",
        {
            "app_name": settings.app_name,
            "environment": settings.environment,
        },
    )


@app.get("/income")
def income_page(request: Request):
    return render(
        request,
        "income.html",
        {
            "app_name": settings.app_name,
            "environment": settings.environment,
            "salary_labels": _salary_labels(),
        },
    )


@app.get("/savings")
def savings_page(request: Request):
    return render(
        request,
        "savings.html",
        {
            "app_name": settings.app_name,
            "environment": settings.environment,
        },
    )


@app.get("/debts")
def debts_page(request: Request):
    return render(
        request,
        "debts.html",
        {
            "app_name": settings.app_name,
            "environment": settings.environment,
        },
    )


@app.get("/receivables")
def receivables_page(request: Request):
    return render(
        request,
        "receivables.html",
        {
            "app_name": settings.app_name,
            "environment": settings.environment,
        },
    )


@app.get("/assets")
def assets_page(request: Request):
    return render(
        request,
        "assets.html",
        {
            "app_name": settings.app_name,
            "environment": settings.environment,
        },
    )


@app.get("/exchange-rates")
def exchange_rates_page(request: Request):
    return render(
        request,
        "exchange_rates.html",
        {
            "app_name": settings.app_name,
            "environment": settings.environment,
        },
    )


@app.get("/rules")
def rules_page(request: Request):
    return render(
        request,
        "rules.html",
        {
            "app_name": settings.app_name,
            "environment": settings.environment,
        },
    )


@app.get("/reports/monthly")
def reports_monthly_page(request: Request):
    return render(
        request,
        "reports_monthly.html",
        {
            "app_name": settings.app_name,
            "environment": settings.environment,
            "salary_labels": _salary_labels(),
        },
    )


@app.get("/reports/annual")
def reports_annual_page(request: Request):
    return render(
        request,
        "reports_annual.html",
        {
            "app_name": settings.app_name,
            "environment": settings.environment,
        },
    )


@app.post("/ui")
async def switch_ui(request: Request):
    """Phone/desktop override cookie. choice=auto deletes it (back to
    user-agent detection); anything else is ignored."""
    form = await request.form()
    choice = str(form.get("choice", "")).strip().lower()
    response = RedirectResponse(request.headers.get("referer") or "/", status_code=303)
    if choice in ("phone", "desktop"):
        response.set_cookie(
            ui.COOKIE, choice, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax"
        )
    elif choice == "auto":
        response.delete_cookie(ui.COOKIE)
    return response


@app.get("/manifest.webmanifest")
def manifest(request: Request):
    """Generated per request, never a static file: start_url and icon paths
    must carry X-Forwarded-Prefix, or an icon installed through a subpath
    proxy opens the proxy root instead of this app."""
    base = ui.forwarded_prefix(request)
    return JSONResponse(
        {
            "name": settings.app_name,
            "short_name": "Finances",
            "start_url": f"{base}/",
            "display": "standalone",
            "background_color": "#0f172a",
            "theme_color": "#0f172a",
            "icons": [
                {"src": f"{base}/static/img/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": f"{base}/static/img/icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        },
        media_type="application/manifest+json",
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Browsers request this unconditionally; without it every page logs a
    401 (the auth middleware) to the console. PNG at .ico is fine for them."""
    return RedirectResponse("/static/img/icon-192.png", status_code=308)


@app.get("/health")
def health():
    db_ok, _ = check_db()
    return {
        "app": "ok",
        "db": "ok" if db_ok else "down",
        "plaid_configured": plaid_is_configured(),
        "plaid_env": settings.plaid_env,
    }
