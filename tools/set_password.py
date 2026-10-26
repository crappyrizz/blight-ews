"""Set a new password for one account (admin or farmer), typed by you, never shown.

    .\\.venv\\Scripts\\python.exe -m tools.set_password 0700000001 --hosted   # admin, on Render's database (Neon)
    .\\.venv\\Scripts\\python.exe -m tools.set_password 0700000002            # farmer, on the local database

- Asks for the new password twice (hidden input) and stores only its bcrypt
  hash, exactly as registration does. The password is never printed, logged
  or written to a file.
- Changes only the account with that phone number; nothing else.
- By default it uses the local database (DATABASE_URL in .env). --hosted uses
  the hosted one instead (DATABASE_URL in neon.local.env), so a mistyped
  command cannot change the wrong database. Reaching Neon needs outbound port
  5432, which the student Wi-Fi blocks (use the hotspot).

For when a password printed once by tools/seed.py is lost: seeding never
replaces an existing account's password.
"""
import argparse
import getpass
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.auth import hash_password
from backend.config import REPO_ROOT, Settings
from backend.models import Farmer
from backend.schemas import normalise_phone

MIN_LENGTH, MAX_LENGTH = 8, 72  # the same limits as registration (bcrypt reads at most 72 bytes)
HOSTED_ENV_FILE = REPO_ROOT / "neon.local.env"


class PasswordError(Exception):
    """The password could not be set: the message says why."""


def set_password(session: Session, phone: str, password: str) -> Farmer:
    """Store the hash of `password` for the account with this phone number. Commits."""
    if not MIN_LENGTH <= len(password.encode("utf-8")) <= MAX_LENGTH:
        raise PasswordError(f"the password must be {MIN_LENGTH} to {MAX_LENGTH} characters")
    try:
        phone = normalise_phone(phone)
    except ValueError as error:
        raise PasswordError(str(error)) from error
    farmer = session.scalar(select(Farmer).where(Farmer.phone_number == phone))
    if farmer is None:
        raise PasswordError(f"no account with phone number {phone}")
    farmer.password_hash = hash_password(password)
    session.commit()
    return farmer


def hosted_session(env_file: Path = HOSTED_ENV_FILE) -> Session:
    from dotenv import dotenv_values

    url = dotenv_values(env_file).get("DATABASE_URL") if env_file.exists() else None
    if not url:
        raise PasswordError(f"no DATABASE_URL in {env_file.name}: the hosted database is not set up here")
    engine = create_engine(Settings._use_the_psycopg_driver(url), connect_args={"connect_timeout": 20})
    return Session(engine)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Set a new password for one account.")
    parser.add_argument("phone", help="the account's phone number, e.g. 0700000001")
    parser.add_argument("--hosted", action="store_true", help="change it on the hosted database (Neon)")
    args = parser.parse_args(argv)

    where = "the HOSTED database (Render + Neon)" if args.hosted else "the LOCAL database"
    print(f"Setting a new password for {args.phone} on {where}.")
    password = getpass.getpass("New password: ")
    if getpass.getpass("Type it again: ") != password:
        print("Not changed: the two passwords differ.")
        return 1

    try:
        if args.hosted:
            open_session = hosted_session
        else:
            from backend.db import SessionLocal as open_session
        with open_session() as session:
            farmer = set_password(session, args.phone, password)
    except PasswordError as error:
        print(f"Not changed: {error}")
        return 1
    print(f"Password changed for {farmer.phone_number} ({farmer.role}, {farmer.name}) on {where}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
