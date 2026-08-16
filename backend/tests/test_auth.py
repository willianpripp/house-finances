"""The login door: everything closed without a session, one way in."""
from __future__ import annotations

import pytest

from app.services.auth import hash_password

PASSWORD = "correct-horse-battery"


@pytest.fixture
def bare_client():
    """A client with NO session cookie — the unauthenticated world."""
    from fastapi.testclient import TestClient

    import app.db as app_db
    from app.db import get_db
    from app.main import app

    def _get_db():
        session = app_db.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _get_db
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def primary_with_password(db):
    from sqlalchemy import select

    from app.models import User

    user = db.scalar(select(User).where(User.id == 1))
    user.password_hash = hash_password(PASSWORD)
    db.commit()
    yield user
    user.password_hash = None
    db.commit()


def test_api_without_session_is_401(bare_client):
    r = bare_client.get("/api/receivables")
    assert r.status_code == 401
    assert r.json() == {"detail": "Not authenticated"}


def test_browser_without_session_is_sent_to_login(bare_client):
    r = bare_client.get("/receivables", headers={"accept": "text/html"})
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/receivables"


def test_health_and_login_stay_open(bare_client):
    assert bare_client.get("/health").status_code == 200
    assert bare_client.get("/login").status_code == 200


def test_wrong_password_is_rejected(bare_client, primary_with_password):
    r = bare_client.post(
        "/login",
        data={"login": primary_with_password.email, "password": "nope", "next": "/"},
    )
    assert r.status_code == 401
    assert "fin_session" not in r.cookies


def test_login_sets_the_session_and_opens_the_app(bare_client, primary_with_password):
    r = bare_client.post(
        "/login",
        data={
            "login": primary_with_password.email,
            "password": PASSWORD,
            "next": "/receivables",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/receivables"
    assert "fin_session" in r.cookies

    # cookie jar carries the session from here on
    assert bare_client.get("/api/receivables").status_code == 200


def test_next_redirect_never_leaves_the_app(bare_client, primary_with_password):
    r = bare_client.post(
        "/login",
        data={
            "login": primary_with_password.email,
            "password": PASSWORD,
            "next": "//evil.example/phish",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_login_accepts_the_username_too(bare_client, primary_with_password):
    """Case-insensitive users.name works as the login, not only the email."""
    r = bare_client.post(
        "/login",
        data={"login": primary_with_password.name.lower(), "password": PASSWORD, "next": "/"},
    )
    assert r.status_code == 303
    assert "fin_session" in r.cookies


def test_greeting_names_the_session_user(bare_client, primary_with_password):
    bare_client.post(
        "/login",
        data={"login": primary_with_password.email, "password": PASSWORD, "next": "/"},
    )
    r = bare_client.get("/", headers={"accept": "text/html"})
    assert r.status_code == 200
    assert primary_with_password.name in r.text


def test_user_without_password_cannot_log_in(bare_client):
    # Fixture users have no password_hash by default.
    r = bare_client.post(
        "/login",
        data={"login": "partner@example.test", "password": "", "next": "/"},
    )
    assert r.status_code in (401, 422)


def test_logout_clears_the_session(bare_client, primary_with_password):
    bare_client.post(
        "/login",
        data={"login": primary_with_password.email, "password": PASSWORD, "next": "/"},
    )
    assert bare_client.get("/api/receivables").status_code == 200

    r = bare_client.post("/logout")
    assert r.status_code == 303
    assert bare_client.get("/api/receivables").status_code == 401
