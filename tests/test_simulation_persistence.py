"""API tests for persistent synthetic telemetry and dry-run controls."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault(
    "SON_DATABASE_URL", "postgresql+psycopg://son:son@127.0.0.1:5432/son"
)
os.environ.setdefault("SON_JWT_SECRET", "unit-test-signing-key-never-use-in-prod-000000")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api import app
from src.db import Base, get_db
from src.dbmodels import User
from src.security import Role, create_access_token, hash_password
from src.telemetry_simulator import create_simulated_telemetry


class SimulationPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        with self.session_factory.begin() as db:
            db.add(
                User(
                    username="supervisor",
                    password_hash=hash_password("SupervisorLongPass123!"),
                    role=Role.SUPERVISOR.value,
                )
            )
            db.add(
                User(
                    username="engineer",
                    password_hash=hash_password("EngineerLongPass123!"),
                    role=Role.ENGINEER.value,
                )
            )

        def override_db():
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)
        self.supervisor_headers = {
            "Authorization": f"Bearer {create_access_token('supervisor', Role.SUPERVISOR)}"
        }
        self.engineer_headers = {
            "Authorization": f"Bearer {create_access_token('engineer', Role.ENGINEER)}"
        }

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.engine.dispose()

    def _save_initial_samples(self) -> list[dict[str, object]]:
        sites = create_simulated_telemetry()
        fields = (
            "tower_id",
            "region",
            "latitude",
            "longitude",
            "timestamp",
            "connected_users",
            "prb_usage_pct",
            "throughput_mbps",
            "latency_ms",
            "call_drop_rate_pct",
            "hardware_status",
        )
        response = self.client.post(
            "/v1/simulation/telemetry",
            headers=self.engineer_headers,
            json={"records": [{field: site[field] for field in fields} for site in sites]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["accepted"], len(sites))
        return sites

    def test_persistence_history_and_control_logs_across_requests(self) -> None:
        sites = self._save_initial_samples()
        latest = self.client.get(
            "/v1/simulation/latest", headers=self.engineer_headers
        )
        self.assertEqual(latest.status_code, 200, latest.text)
        persisted = latest.json()
        critical = next(row for row in persisted if row["hardware_status"] == "Critical")

        history_before = self.client.get(
            "/v1/simulation/history",
            headers=self.engineer_headers,
            params={"days": 7},
        )
        self.assertEqual(history_before.status_code, 200, history_before.text)
        self.assertTrue(history_before.json()["trends"])
        self.assertTrue(history_before.json()["anomalies"])
        self.assertEqual(history_before.json()["trends"][0]["sample_count"], len(sites))

        forbidden = self.client.post(
            "/v1/simulation/actions",
            headers=self.engineer_headers,
            json={"cell_id": critical["tower_id"]},
        )
        self.assertEqual(forbidden.status_code, 403)

        action = self.client.post(
            "/v1/simulation/actions",
            headers=self.supervisor_headers,
            json={"cell_id": critical["tower_id"]},
        )
        self.assertEqual(action.status_code, 200, action.text)
        result = action.json()
        self.assertTrue(result["simulation_only"])
        self.assertIn("no network connection", result["execution_log"])
        self.assertEqual(result["telemetry"]["hardware_status"], "Optimal")

        repeated_action = self.client.post(
            "/v1/simulation/actions",
            headers=self.supervisor_headers,
            json={"cell_id": critical["tower_id"]},
        )
        self.assertEqual(repeated_action.status_code, 409)

        latest_after = self.client.get(
            "/v1/simulation/latest", headers=self.engineer_headers
        ).json()
        healed = next(row for row in latest_after if row["tower_id"] == critical["tower_id"])
        self.assertEqual(healed["hardware_status"], "Optimal")

        history_after = self.client.get(
            "/v1/simulation/history",
            headers=self.engineer_headers,
            params={"days": 7},
        ).json()
        self.assertTrue(any(event["resolved_at"] for event in history_after["anomalies"]))
        self.assertTrue(history_after["actions"])
        self.assertIn("SIMULATION ONLY", history_after["actions"][0]["execution_log"])


if __name__ == "__main__":
    unittest.main()
