from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "House Finances"
    environment: str = "development"
    database_url: str = "postgresql+psycopg://finances:finances@localhost:5432/finances"

    # Plaid (US auto-pull). Get from dashboard.plaid.com.
    plaid_client_id: str = ""
    plaid_secret: str = ""
    plaid_env: str = "sandbox"  # sandbox | production (Development tier retired 2024)
    # Clean-start anchor: Plaid transactions BEFORE this date are dropped on
    # ingest (prior months are frozen; we don't touch them). YYYY-MM-DD.
    plaid_start_date: str = "2026-06-01"

    # Pluggy (BR auto-pull, free personal path). Get from dashboard.pluggy.ai.
    # Auth is app-level (client credentials -> short-lived apiKey); items
    # carry no per-connection secret, so Fernet is not involved.
    pluggy_client_id: str = ""
    pluggy_client_secret: str = ""
    # Clean-start anchor, same semantics as plaid_start_date: Pluggy
    # transactions BEFORE this date are dropped on ingest. YYYY-MM-DD.
    pluggy_start_date: str = "2026-08-01"

    # Fernet key for encrypting Plaid access_tokens at rest. Generate via
    # python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    fernet_key: str = ""

    # Emergency hatch for the boot-time schema guard: 1 logs the drift and
    # serves anyway instead of refusing to start. Never leave it on.
    allow_schema_drift: bool = False

    # HS256 secret for the session JWT (cookie login). Generate via
    # python -c "import secrets; print(secrets.token_urlsafe(48))"
    # If you feed it in through docker compose, escape any `$` as `$$`:
    # compose interpolates it.
    auth_secret: str = ""

    # Optional link to a household portal, shown as a house icon in both
    # headers. Deployment data, not code: empty (the default) renders no
    # icon at all, which is what a fresh clone gets.
    portal_url: str = ""

    # Transit operator whose tap-and-go pre-authorizations should be dropped
    # on ingest (see services/plaid_import.py). Deployment data: which operator
    # a household rides is not something this repo should assert. Empty (the
    # default) disables the filter entirely.
    transit_preauth_keyword: str = ""
    transit_fare_min: str = "2.00"


settings = Settings()
