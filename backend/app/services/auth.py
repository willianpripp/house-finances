"""Session auth: bcrypt password check, JWT in an httpOnly cookie.

Hand-rolled on purpose. The obvious pick was FastAPI-Users + JWT, but
FastAPI-Users is built on async SQLAlchemy and this app is sync end to end,
so adopting it would mean a second, async engine just for auth. Same
JWT-in-cookie outcome, a fixed set of users, no registration or reset flows.

The secret is AUTH_SECRET in the app's .env (if you feed it in through docker
compose, escape any `$` as `$$`: compose interpolates it). Rotating it logs
everyone out, nothing worse.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User

COOKIE_NAME = "fin_session"
TOKEN_LIFETIME = timedelta(days=30)
_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        # No password set for this user yet — never a valid login.
        return False
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def authenticate(session: Session, login: str, password: str) -> User | None:
    """Login by username (`users.name`) or full email, case-insensitive."""
    identifier = login.strip().lower()
    if "@" in identifier:
        stmt = select(User).where(func.lower(User.email) == identifier)
    else:
        stmt = select(User).where(func.lower(User.name) == identifier)
    user = session.scalar(stmt)
    if user is None:
        # Burn a hash comparison anyway so a missing user costs the same as a
        # wrong password (no user-enumeration timing signal).
        bcrypt.checkpw(b"x", bcrypt.hashpw(b"y", bcrypt.gensalt()))
        return None
    return user if verify_password(password, user.password_hash) else None


def create_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "iat": now, "exp": now + TOKEN_LIFETIME}
    return jwt.encode(payload, settings.auth_secret, algorithm=_ALGORITHM)


def user_id_from_token(token: str) -> int | None:
    """The user id the token vouches for, or None for anything invalid."""
    if not settings.auth_secret:
        return None
    try:
        payload = jwt.decode(token, settings.auth_secret, algorithms=[_ALGORITHM])
        return int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        return None
