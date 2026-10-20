"""Regression tests for upgrading the legacy local users table."""

from __future__ import annotations

import unittest

from pwdlib import PasswordHash
from sqlalchemy import create_engine, inspect, text

from src.migrate import migrate_legacy_sqlite_users


class LegacyUserMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE users ("
                    "username TEXT PRIMARY KEY, password TEXT, role TEXT)"
                )
            )

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_migrates_password_and_normalizes_admin_role(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (username, password, role) "
                    "VALUES (:username, :password, :role)"
                ),
                {
                    "username": "local-admin",
                    "password": "example-local-password",
                    "role": "admin",
                },
            )
            self.assertTrue(migrate_legacy_sqlite_users(connection))
            migrated = connection.execute(
                text(
                    "SELECT username, password_hash, role "
                    "FROM users WHERE username = :username"
                ),
                {"username": "local-admin"},
            ).mappings().one()
            remaining_tables = inspect(connection).get_table_names()

        self.assertEqual(migrated["role"], "system_admin")
        self.assertNotEqual(migrated["password_hash"], "example-local-password")
        self.assertTrue(
            PasswordHash.recommended().verify(
                "example-local-password", migrated["password_hash"]
            )
        )
        self.assertNotIn("users_legacy_migration", remaining_tables)

    def test_unsupported_legacy_role_rolls_back_without_data_loss(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (username, password, role) "
                    "VALUES ('local-user', 'not-logged', 'unknown-role')"
                )
            )
        with self.assertRaisesRegex(RuntimeError, "unsupported role"):
            with self.engine.begin() as connection:
                migrate_legacy_sqlite_users(connection)
        with self.engine.connect() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns("users")}
            remaining = connection.execute(
                text("SELECT COUNT(*) FROM users")
            ).scalar_one()
        self.assertEqual(columns, {"username", "password", "role"})
        self.assertEqual(remaining, 1)


if __name__ == "__main__":
    unittest.main()
