"""Set a user's login password. The only way passwords get set — the app has
no registration or reset flows on purpose (two fixed users).

Takes the email of an EXISTING user; it sets a password, it does not create
accounts. Run it interactively — no `-T`, the prompt needs a tty:

    docker compose run --rm app python scripts/set_password.py you@example.com

On the deployed stack that means `ssh -t <host>` first, from the compose
directory.
"""
from __future__ import annotations

import sys
from getpass import getpass
from pathlib import Path

# Runnable as `python scripts/set_password.py`: sys.path[0] is then scripts/,
# so the `app` package would not be importable without this.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.db import SessionLocal
from app.models import User
from app.services.auth import hash_password


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python scripts/set_password.py <email>", file=sys.stderr)
        return 2

    email = sys.argv[1].strip().lower()
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.email == email))
        if user is None:
            emails = [u.email for u in session.scalars(select(User)).all()]
            print(f"no user {email!r}; users are: {emails}", file=sys.stderr)
            return 1

        password = getpass(f"New password for {user.name} <{email}>: ")
        if len(password) < 8:
            print("refusing a password under 8 characters", file=sys.stderr)
            return 1
        if getpass("Repeat: ") != password:
            print("passwords do not match", file=sys.stderr)
            return 1

        user.password_hash = hash_password(password)
        session.commit()
        print(f"password set for {user.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
