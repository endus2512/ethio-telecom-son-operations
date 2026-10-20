"""Apply ordered SQL migrations to the configured PostgreSQL database."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from src import dbmodels
from src.db import engine

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


def apply_migrations() -> None:
    """Apply PostgreSQL migrations or create the local SQLite schema."""
    if engine.dialect.name == "sqlite":
        dbmodels.Base.metadata.create_all(engine)
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
