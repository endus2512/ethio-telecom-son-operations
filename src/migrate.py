"""Apply ordered database migrations and safely upgrade legacy local SQLite users."""

from __future__ import annotations

from pathlib import Path

from pwdlib import PasswordHash
from sqlalchemy import Connection, inspect, text

from src import dbmodels
from src.db import Base, engine
from src.security import Role

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
PASSWORD_HASHER = PasswordHash.recommended()

LEGACY_ROLE_MAP = {
    "admin": Role.ADMIN.value,
    "system_admin": Role.ADMIN.value,
    "administrator": Role.ADMIN.value,
    "engineer": Role.ENGINEER.value,
    "network_engineer": Role.ENGINEER.value,
    "noc_engineer": Role.ENGINEER.value,
    "supervisor": Role.SUPERVISOR.value,
    "noc_supervisor": Role.SUPERVISOR.value,
}


def migrate_legacy_sqlite_users(connection: Connection) -> bool:
    """Upgrade the old username/password/role table without retaining plaintext."""
    inspector = inspect(connection)
    if "users" not in inspector.get_table_names():
        return False
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "password_hash" in columns:
        return False
    if not {"username", "password", "role"}.issubset(columns):
        raise RuntimeError(
            "Unsupported legacy users schema; database was left unchanged."
        )

    legacy_rows = connection.execute(
        text("SELECT username, password, role FROM users")
    ).mappings().all()
    migrated_rows: list[dict[str, str]] = []
    for row in legacy_rows:
        username = row["username"]
        password = row["password"]
        role = row["role"]
        if not isinstance(username, str) or not username:
            raise RuntimeError("Legacy user record has an invalid username.")
        if not isinstance(password, str) or not password:
            raise RuntimeError(
                f"Legacy account {username!r} has no usable password; "
                "database was left unchanged."
            )
        if not isinstance(role, str) or role.lower() not in LEGACY_ROLE_MAP:
            raise RuntimeError(
                f"Legacy account {username!r} has an unsupported role; "
                "database was left unchanged."
            )
        migrated_rows.append(
            {
                "username": username,
                "password_hash": PASSWORD_HASHER.hash(password),
                "role": LEGACY_ROLE_MAP[role.lower()],
            }
        )

    connection.execute(text("ALTER TABLE users RENAME TO users_legacy_migration"))
    Base.metadata.create_all(bind=connection)
    if migrated_rows:
        connection.execute(dbmodels.User.__table__.insert(), migrated_rows)
    connection.execute(text("DROP TABLE users_legacy_migration"))
    return True


def apply_migrations() -> None:
    """Apply PostgreSQL migrations or create the local SQLite schema."""
    if engine.dialect.name == "sqlite":
        with engine.begin() as connection:
            migrated = migrate_legacy_sqlite_users(connection)
            if not migrated:
                Base.metadata.create_all(bind=connection)
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version VARCHAR(64) PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        )
        applied = set(
            connection.execute(text("SELECT version FROM schema_migrations")).scalars()
        )
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            statements = [statement.strip() for statement in path.read_text().split(";")]
            for statement in statements:
                if statement:
                    connection.execute(text(statement))
            connection.execute(
                text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                {"version": path.name},
            )


if __name__ == "__main__":
    apply_migrations()
    print("Database migrations are current.")
