"""Thin hand-rolled wrapper around the Pluggy REST API (no SDK, decided
2026-08-08 — the surface we use is four endpoints).

Auth model: POST /auth with CLIENT_ID/CLIENT_SECRET returns a short-lived
apiKey (~2h). The key is cached in-process and refreshed once on a 401/403,
so callers never handle expiry. Items carry no per-connection secret — the
UUID is the only reference — which is why, unlike Plaid, nothing here needs
Fernet.

The API deliberately has NO list-items endpoint (privacy): our
`pluggy_items` table is the only inventory of connections.
"""
from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from app.config import settings

BASE_URL = "https://api.pluggy.ai"
_TIMEOUT = 30.0
_MAX_PAGES = 200  # cursor-loop backstop, ~4000 tx at v2's 20-per-page

_api_key: str | None = None


class PluggyError(RuntimeError):
    """API-level failure with enough context to log usefully."""


def is_configured() -> bool:
    return bool(settings.pluggy_client_id and settings.pluggy_client_secret)


def _authenticate() -> str:
    if not is_configured():
        raise PluggyError(
            "Pluggy credentials not configured. Set PLUGGY_CLIENT_ID and "
            "PLUGGY_CLIENT_SECRET in .env (get them from dashboard.pluggy.ai)."
        )
    resp = httpx.post(
        f"{BASE_URL}/auth",
        json={
            "clientId": settings.pluggy_client_id,
            "clientSecret": settings.pluggy_client_secret,
        },
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        raise PluggyError(f"Pluggy auth failed: HTTP {resp.status_code} {resp.text[:200]}")
    key = resp.json().get("apiKey")
    if not key:
        raise PluggyError("Pluggy auth response had no apiKey")
    return key


def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """GET with the cached apiKey; re-auth once on 401/403 (expiry)."""
    global _api_key
    for attempt in (1, 2):
        if _api_key is None:
            _api_key = _authenticate()
        resp = httpx.get(
            f"{BASE_URL}{path}",
            params=params,
            headers={"X-API-KEY": _api_key},
            timeout=_TIMEOUT,
        )
        if resp.status_code in (401, 403) and attempt == 1:
            _api_key = None
            continue
        if resp.status_code == 404:
            raise PluggyError(f"Pluggy: {path} not found (404)")
        if resp.status_code != 200:
            raise PluggyError(
                f"Pluggy GET {path} failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        return resp.json()
    raise PluggyError(f"Pluggy GET {path}: unauthorized after re-auth")


def _post_authed(path: str, json_body: dict[str, Any]) -> dict[str, Any]:
    """POST with the cached apiKey; re-auth once on 401/403 (expiry)."""
    global _api_key
    for attempt in (1, 2):
        if _api_key is None:
            _api_key = _authenticate()
        resp = httpx.post(
            f"{BASE_URL}{path}",
            json=json_body,
            headers={"X-API-KEY": _api_key},
            timeout=_TIMEOUT,
        )
        if resp.status_code in (401, 403) and attempt == 1:
            _api_key = None
            continue
        if resp.status_code not in (200, 201):
            raise PluggyError(
                f"Pluggy POST {path} failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        return resp.json()
    raise PluggyError(f"Pluggy POST {path}: unauthorized after re-auth")


def create_connect_token(item_id: str | None = None) -> str:
    """Short-lived (~30 min) token the Connect widget boots from.

    Without item_id: a NEW connection (the widget's OAuth into the user's
    meu.pluggy.ai — the only real connector this Application sees). With
    item_id: update mode, re-authorizing an existing connection instead of
    creating a duplicate item."""
    body: dict[str, Any] = {"itemId": item_id} if item_id else {}
    token = _post_authed("/connect_token", body).get("accessToken")
    if not token:
        raise PluggyError("Pluggy connect_token response had no accessToken")
    return token


def get_item(item_id: str) -> dict[str, Any]:
    """One connection: connector, status, executionStatus, timestamps."""
    return _get(f"/items/{item_id}")


def list_accounts(item_id: str) -> list[dict[str, Any]]:
    """Accounts under a connection. type BANK|CREDIT, subtype
    CHECKING_ACCOUNT|SAVINGS_ACCOUNT|CREDIT_CARD, balance, currencyCode."""
    return _get("/accounts", params={"itemId": item_id}).get("results", [])


def list_transactions(account_id: str, since: date, until: date) -> list[dict[str, Any]]:
    """All transactions for one account in [since, until].

    Uses GET /v2/transactions (v1 returns 410 ENDPOINT_DEPRECATED, found
    during the 2026-08-08 sandbox validation). v2 has NO transaction-date
    filter and paginates via a `next` cursor (null on the last page), so we
    fetch every page and filter the window client-side. `next` handling is
    defensive: an absolute URL is followed as-is, anything else is passed
    back as the `cursor` param.

    Fields used downstream: id, description, amount, date, type
    (DEBIT|CREDIT), category, status (POSTED|PENDING), currencyCode.
    Sign convention is validated against REAL data before any commit path
    opens: the sandbox connector inverts it."""
    out: list[dict[str, Any]] = []
    params: dict[str, Any] = {"accountId": account_id}
    path = "/v2/transactions"
    for _ in range(_MAX_PAGES):
        data = _get(path, params=params)
        out.extend(data.get("results", []))
        nxt = data.get("next")
        if not nxt:
            break
        if isinstance(nxt, str) and nxt.startswith("http"):
            from urllib.parse import urlparse

            parsed = urlparse(nxt)
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            params = None
        else:
            params = {"accountId": account_id, "cursor": nxt}
            path = "/v2/transactions"

    def _in_window(tx: dict[str, Any]) -> bool:
        raw = str(tx.get("date", ""))[:10]
        try:
            d = date.fromisoformat(raw)
        except ValueError:
            return False
        return since <= d <= until

    return [tx for tx in out if _in_window(tx)]


def list_investments(item_id: str) -> list[dict[str, Any]]:
    """Investment positions under a connection (separate product from
    /accounts — Nubank money boxes arrive here as CDB positions, one id per
    deposit, closed ones lingering with balance 0). Fields used: id, name,
    type, subtype, balance, currencyCode."""
    return _get("/investments", params={"itemId": item_id}).get("results", [])


def list_connectors(name: str | None = None) -> list[dict[str, Any]]:
    """Connector catalog — used to check whether a given institution is
    covered before wiring an account to it."""
    params: dict[str, Any] = {"name": name} if name else {}
    return _get("/connectors", params=params).get("results", [])
