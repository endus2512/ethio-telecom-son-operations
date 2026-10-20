"""Administrative commands for safe first-user creation."""

from __future__ import annotations

import argparse
from getpass import getpass

from sqlalchemy import func, select

from src.db import SessionLocal, engine
from src.dbmodels import KpiMetric, User
from src.demo_data import seed_demo_data
from src.migrate import apply_migrations
from src.security import Role, hash_password


def create_user(username: str, role_value: str) -> None:
    """Interactively create an account without putting its password in shell history."""
    role = Role(role_value)
    password = getpass("Password (minimum 12 characters): ")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        raise ValueError("Passwords do not match.")
    password_hash = hash_password(password)
    with SessionLocal.begin() as session:
        if session.scalar(select(User.id).where(User.username == username)):
            raise ValueError(f"User {username!r} already exists.")
        session.add(
            User(username=username, password_hash=password_hash, role=role.value)
        )
    print(f"Created {role.value} account {username!r}.")


def initialize_local() -> None:
    """Initialize a local SQLite database, administrator, and sample dashboard."""
    if engine.dialect.name != "sqlite":
        raise ValueError(
            "init-local requires SON_DATABASE_URL=sqlite:///./son-local.db."
        )
    apply_migrations()
    with SessionLocal.begin() as session:
        has_admin = session.scalar(
            select(User.id).where(User.role == Role.ADMIN.value).limit(1)
        )
        if has_admin is None:
            username = input("Initial system administrator username: ").strip()
            if not username:
                raise ValueError("Administrator username cannot be empty.")
            password = getpass("Administrator password (minimum 12 characters): ")
            confirmation = getpass("Confirm password: ")
            if password != confirmation:
                raise ValueError("Passwords do not match.")
            session.add(
                User(
                    username=username,
                    password_hash=hash_password(password),
                    role=Role.ADMIN.value,
                )
            )
            print(f"Created local system administrator {username!r}.")
        else:
            print("An administrator account already exists; leaving it unchanged.")
        seeded = seed_demo_data(session)
        if seeded:
            print("Added labeled synthetic PM telemetry and fault events.")
        elif session.scalar(select(func.count()).select_from(KpiMetric)):
            print("Existing KPI telemetry found; demo data was not added.")
    print("Local database is ready.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Ethio Telecom SON administration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    user_parser = subparsers.add_parser("create-user")
    user_parser.add_argument("--username", required=True)
    user_parser.add_argument(
        "--role", choices=[role.value for role in Role], required=True
    )
    subparsers.add_parser(
        "init-local",
        help="Initialize local SQLite, prompt for an admin, and add synthetic demo data.",
    )
    args = parser.parse_args()
    if args.command == "create-user":
        create_user(args.username, args.role)
    elif args.command == "init-local":
        initialize_local()


if __name__ == "__main__":
    main()
