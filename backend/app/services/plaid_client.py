"""Thin wrapper around `plaid-python`. Keeps API-shape decisions
(host URL, products, country_codes) in one place so the rest of the app
calls high-level functions.

Plaid environments:
  - `sandbox` — fake data, free, no signup needed beyond client_id+secret
  - `development` — real bank data, free up to ~100 Items lifetime
  - `production` — paid

`PLAID_ENV` in .env switches between them. Default for v0 is sandbox
until user provides dev credentials and confirms by connecting a real
bank.
"""
from __future__ import annotations

from plaid import Configuration, ApiClient, Environment
from plaid.api import plaid_api

from app.config import settings


# Plaid retired the Development environment in 2024-2025 (SDK 27.x).
# The free trial (~100 Items lifetime) now runs under Production credentials.
_PLAID_HOSTS = {
    "sandbox": Environment.Sandbox,
    "production": Environment.Production,
}


_client: plaid_api.PlaidApi | None = None


def get_client() -> plaid_api.PlaidApi:
    """Lazy-init Plaid API client. Raises if credentials aren't configured."""
    global _client
    if _client is not None:
        return _client

    if not settings.plaid_client_id or not settings.plaid_secret:
        raise RuntimeError(
            "Plaid credentials not configured. Set PLAID_CLIENT_ID and "
            "PLAID_SECRET in .env (get them from dashboard.plaid.com)."
        )

    env = settings.plaid_env.lower()
    if env not in _PLAID_HOSTS:
        raise RuntimeError(
            f"PLAID_ENV='{env}' invalid; must be sandbox|production"
        )

    configuration = Configuration(
        host=_PLAID_HOSTS[env],
        api_key={
            "clientId": settings.plaid_client_id,
            "secret": settings.plaid_secret,
        },
    )
    _client = plaid_api.PlaidApi(ApiClient(configuration))
    return _client


def is_configured() -> bool:
    """Cheap check used by /api/health and the UI to flag missing creds
    without raising.

    fernet_key is part of "configured", not an extra: without it every stored
    access_token is undecryptable, so Plaid calls fail at the first token load
    rather than at the API. Omitting it here made /health report healthy on a
    deployment that could not actually talk to any bank, which is the worst
    moment to be told everything is fine.
    """
    return bool(
        settings.plaid_client_id and settings.plaid_secret and settings.fernet_key
    )
