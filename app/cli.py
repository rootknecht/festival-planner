"""Administrative command line.

Used by the installer and by an operator on the box. Deliberately tiny: create
the first account, reset a password, purge expired sessions.

    python -m app.cli create-operator --email you@example.org --name "You"
"""

from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import func, select

from app.db import session_scope
from app.models.user import User
from app.services.passwords import PasswordTooShort, hash_password, validate_password
from app.services.sessions import purge_expired


def _read_password(provided: str | None) -> str:
    password = provided or getpass.getpass("Password: ")
    validate_password(password)
    return password


def create_operator(args: argparse.Namespace) -> int:
    email = args.email.strip().lower()
    try:
        password = _read_password(args.password)
    except PasswordTooShort as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    with session_scope() as db:
        existing = db.scalar(select(User).where(func.lower(User.email) == email))
        if existing is not None:
            print(f"error: a user with {email} already exists", file=sys.stderr)
            return 1
        db.add(
            User(
                email=email,
                password_hash=hash_password(password),
                display_name=args.name or email.split("@")[0],
                is_instance_operator=True,
                language=args.language,
            )
        )
    print(f"instance operator created: {email}")
    return 0


def set_password(args: argparse.Namespace) -> int:
    email = args.email.strip().lower()
    try:
        password = _read_password(args.password)
    except PasswordTooShort as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    with session_scope() as db:
        user = db.scalar(select(User).where(func.lower(User.email) == email))
        if user is None:
            print(f"error: no user with {email}", file=sys.stderr)
            return 1
        user.password_hash = hash_password(password)
    print(f"password updated for {email}")
    return 0


def purge_sessions(_args: argparse.Namespace) -> int:
    with session_scope() as db:
        removed = purge_expired(db)
    print(f"removed {removed} expired session(s)")
    return 0


def generate_vapid(_args: argparse.Namespace) -> int:
    """Print a fresh VAPID key pair for the env file."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from py_vapid import Vapid01

    vapid = Vapid01()
    vapid.generate_keys()
    private_key = vapid.private_pem().decode("utf-8").strip()
    raw_public = vapid.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    public_key = base64.urlsafe_b64encode(raw_public).decode("utf-8").rstrip("=")
    print("# add these to the service env file, then restart")
    print(f"FP_VAPID_PUBLIC_KEY={public_key}")
    print("FP_VAPID_PRIVATE_KEY<<EOF")
    print(private_key)
    print("EOF")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.cli", description="Festival Planner admin tasks")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-operator", help="create the first, instance-operator account")
    create.add_argument("--email", required=True)
    create.add_argument("--name", default="")
    create.add_argument("--language", default="en")
    create.add_argument("--password", default=None, help="omit to be prompted (recommended)")
    create.set_defaults(func=create_operator)

    reset = sub.add_parser("set-password", help="set a user's password")
    reset.add_argument("--email", required=True)
    reset.add_argument("--password", default=None)
    reset.set_defaults(func=set_password)

    vapid = sub.add_parser("generate-vapid", help="create Web Push keys")
    vapid.set_defaults(func=generate_vapid)

    purge = sub.add_parser("purge-sessions", help="delete expired sessions")
    purge.set_defaults(func=purge_sessions)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
