"""Focused tests for RBAC, feed parsing, and constrained configuration changes."""

from __future__ import annotations

import json
import os
import unittest

os.environ.setdefault(
    "SON_DATABASE_URL", "postgresql+psycopg://son:test@127.0.0.1:5432/son"
)
os.environ.setdefault("SON_JWT_SECRET", "unit-test-signing-key-never-use-in-prod-000000")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.api import app
from src.db import Base, get_db
from src.dbmodels import FaultEvent, KpiMetric, OptimizationAction, Tower, User
from src.ingestion import parse_feed
from src.network_driver import NetconfDriver, NetworkDriverError
from src.security import Role, hash_password
from src.settings import settings


class SONCoreTests(unittest.TestCase):
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
            db.add_all(
                [
                    User(
                        username="engineer",
                        password_hash=hash_password("EngineerLongPass123!"),
                        role=Role.ENGINEER.value,
                    ),
                    User(
                        username="supervisor",
                        password_hash=hash_password("SupervisorLongPass123!"),
                        role=Role.SUPERVISOR.value,
                    ),
                    User(
                        username="administrator",
                        password_hash=hash_password("AdministratorLongPass123!"),
                        role=Role.ADMIN.value,
                    ),
                ]
            )

        def override_db():
            session = self.session_factory()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self) -> None:
        app.dependency_overrides.clear()
        self.engine.dispose()
        settings.son_netconf_enabled = False
        settings.netconf_action_templates_json = None
        settings.__dict__.pop("netconf_action_templates", None)

    def _token(self, username: str, password: str) -> str:
        response = self.client.post(
            "/v1/auth/login", json={"username": username, "password": password}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    def test_role_boundaries_and_three_person_action_workflow(self) -> None:
        engineer = self._token("engineer", "EngineerLongPass123!")
        supervisor = self._token("supervisor", "SupervisorLongPass123!")
        administrator = self._token("administrator", "AdministratorLongPass123!")
        self.assertEqual(self.client.get("/v1/towers").status_code, 401)

        engineer_headers = {"Authorization": f"Bearer {engineer}"}
        supervisor_headers = {"Authorization": f"Bearer {supervisor}"}
        admin_headers = {"Authorization": f"Bearer {administrator}"}
        self.assertEqual(
            self.client.post(
                "/v1/towers",
                headers=engineer_headers,
                json={"tower_id": "ET-001", "region": "Addis Ababa"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/v1/towers",
                headers=admin_headers,
                json={"tower_id": "ET-001", "region": "Addis Ababa"},
            ).status_code,
            201,
        )
        created = self.client.post(
            "/v1/actions",
            headers=engineer_headers,
            json={
                "tower_id": "ET-001",
                "action_type": "ret_adjustment",
                "parameters": {"tilt_degrees": 4.5},
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        action_id = created.json()["id"]
        self.assertEqual(
            self.client.post(
                f"/v1/actions/{action_id}/approve", headers=engineer_headers
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/v1/actions/{action_id}/approve", headers=supervisor_headers
            ).status_code,
            200,
        )
        # NETCONF is disabled by default, so execution fails closed.
        self.assertEqual(
            self.client.post(
                f"/v1/actions/{action_id}/execute", headers=admin_headers
            ).status_code,
            502,
        )
        actions = self.client.get("/v1/actions", headers=engineer_headers).json()
        self.assertEqual(actions[0]["status"], "failed")

    def test_five_failed_passwords_lock_account(self) -> None:
        for _ in range(5):
            response = self.client.post(
                "/v1/auth/login",
                json={"username": "engineer", "password": "incorrect-password"},
            )
            self.assertEqual(response.status_code, 401)
        locked = self.client.post(
            "/v1/auth/login",
            json={"username": "engineer", "password": "EngineerLongPass123!"},
        )
        self.assertEqual(locked.status_code, 401)

    def test_pm_and_fm_json_csv_xml_parsers(self) -> None:
        pm = parse_feed(
            b'{"records":[{"towerId":"ET-1","time":"2026-10-19T18:30:00Z","prb":72}]}',
            "pm",
            "json",
        )
        self.assertEqual(pm[0]["tower_id"], "ET-1")
        csv_records = parse_feed(
            b"tower_id,timestamp,alarm_code,severity,description\n"
            b"ET-1,2026-10-19T18:30:00Z,A1,major,Link down\n",
            "fm",
            "csv",
        )
        self.assertEqual(csv_records[0]["alarm_code"], "A1")
        xml_records = parse_feed(
            b"<feed><alarm><towerId>ET-1</towerId><alarmCode>A2</alarmCode>"
            b"<timestamp>2026-10-19T18:30:00Z</timestamp>"
            b"<severity>major</severity><description>Link down</description>"
            b"</alarm></feed>",
            "fm",
            "xml",
        )
        self.assertEqual(xml_records[0]["alarm_code"], "A2")

    def test_feed_parser_rejects_invalid_json_and_duplicate_xml_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON feed must be"):
            parse_feed(b"null", "fm", "json")

        xml_records = parse_feed(
            b"<record><towerId>ET-1</towerId>"
            b"<timestamp>2026-10-19T18:30:00Z</timestamp>"
            b"<alarmCode>A2</alarmCode><severity>major</severity>"
            b"<description>Link down</description></record>",
            "fm",
            "xml",
        )
        self.assertEqual(len(xml_records), 1)

    def test_fm_ingestion_requires_documented_fields(self) -> None:
        engineer = self._token("engineer", "EngineerLongPass123!")
        response = self.client.post(
            "/v1/ingest/fm?format=json",
            headers={"Authorization": f"Bearer {engineer}"},
            json={
                "records": [
                    {
                        "tower_id": "ET-1",
                        "timestamp": "2026-10-19T18:30:00Z",
                    }
                ]
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("alarm_code", response.json()["detail"])
        self.assertIn("severity", response.json()["detail"])
        self.assertIn("description", response.json()["detail"])

    def test_load_balancing_requires_enabled_inventory_neighbors(self) -> None:
        admin = self._token("administrator", "AdministratorLongPass123!")
        engineer = self._token("engineer", "EngineerLongPass123!")
        admin_headers = {"Authorization": f"Bearer {admin}"}
        engineer_headers = {"Authorization": f"Bearer {engineer}"}
        for tower_id in ("ET-TARGET", "ET-NEIGHBOR"):
            response = self.client.post(
                "/v1/towers",
                headers=admin_headers,
                json={"tower_id": tower_id, "region": "Addis Ababa"},
            )
            self.assertEqual(response.status_code, 201, response.text)

        request = {
            "tower_id": "ET-TARGET",
            "action_type": "load_balancing",
            "parameters": {
                "neighbor_cell_ids": ["ET-MISSING"],
                "offload_percent": 10,
            },
        }
        response = self.client.post(
            "/v1/actions", headers=engineer_headers, json=request
        )
        self.assertEqual(response.status_code, 422, response.text)

        request["parameters"]["neighbor_cell_ids"] = ["ET-TARGET"]
        response = self.client.post(
            "/v1/actions", headers=engineer_headers, json=request
        )
        self.assertEqual(response.status_code, 422, response.text)

        disabled = self.client.patch(
            "/v1/towers/ET-NEIGHBOR",
            headers=admin_headers,
            json={"enabled": False},
        )
        self.assertEqual(disabled.status_code, 200, disabled.text)
        request["parameters"]["neighbor_cell_ids"] = ["ET-NEIGHBOR"]
        response = self.client.post(
            "/v1/actions", headers=engineer_headers, json=request
        )
        self.assertEqual(response.status_code, 422, response.text)

        enabled = self.client.patch(
            "/v1/towers/ET-NEIGHBOR",
            headers=admin_headers,
            json={"enabled": True},
        )
        self.assertEqual(enabled.status_code, 200, enabled.text)
        request["parameters"]["neighbor_cell_ids"] = ["ET-NEIGHBOR"]
        response = self.client.post(
            "/v1/actions", headers=engineer_headers, json=request
        )
        self.assertEqual(response.status_code, 201, response.text)

    def test_sqlite_pm_and_fm_ingestion_upserts(self) -> None:
        admin = self._token("administrator", "AdministratorLongPass123!")
        engineer = self._token("engineer", "EngineerLongPass123!")
        admin_headers = {"Authorization": f"Bearer {admin}"}
        engineer_headers = {"Authorization": f"Bearer {engineer}"}
        created = self.client.post(
            "/v1/towers",
            headers=admin_headers,
            json={"tower_id": "ET-SQLITE-1", "region": "Addis Ababa"},
        )
        self.assertEqual(created.status_code, 201, created.text)

        timestamp = "2026-10-19T18:30:00Z"
        pm_url = "/v1/ingest/pm?format=json"
        for prb in (72, 81):
            response = self.client.post(
                pm_url,
                headers=engineer_headers,
                json={
                    "records": [
                        {
                            "tower_id": "ET-SQLITE-1",
                            "timestamp": timestamp,
                            "connected_users": 450,
                            "prb_usage_pct": prb,
                            "throughput_mbps": 95.2,
                            "latency_ms": 58.1,
                            "call_drop_rate_pct": 1.2,
                            "handover_failure_rate_pct": 0.8,
                            "hardware_status": "Optimal",
                        }
                    ]
                },
            )
            self.assertEqual(response.status_code, 200, response.text)

        latest = self.client.get(
            "/v1/kpis/latest", headers=engineer_headers
        )
        self.assertEqual(latest.status_code, 200, latest.text)
        self.assertEqual(len(latest.json()), 1)
        self.assertEqual(latest.json()[0]["prb_usage_pct"], 81)
        self.assertEqual(latest.json()[0]["source"], "nms-pm")

        fm_url = "/v1/ingest/fm?format=json"
        for cleared_at in (None, "2026-10-19T19:00:00Z"):
            alarm = {
                "tower_id": "ET-SQLITE-1",
                "timestamp": timestamp,
                "external_id": "alarm-sqlite-1",
                "alarm_code": "A1",
                "severity": "major",
                "description": "Test alarm",
            }
            if cleared_at:
                alarm["cleared_at"] = cleared_at
            response = self.client.post(
                fm_url,
                headers=engineer_headers,
                json={"records": [alarm]},
            )
            self.assertEqual(response.status_code, 200, response.text)

        with self.session_factory() as db:
            self.assertIsNotNone(
                db.scalar(
                    select(KpiMetric.id).where(KpiMetric.tower_id == "ET-SQLITE-1")
                ),
            )
            faults = db.scalars(
                select(FaultEvent).where(
                    FaultEvent.external_id == "alarm-sqlite-1"
                )
            ).all()
            self.assertEqual(len(faults), 1)
            self.assertIsNotNone(faults[0].cleared_at)

    def test_vendor_template_and_device_allowlist_are_enforced(self) -> None:
        settings.son_netconf_enabled = True
        settings.netconf_username = "lab-user"
        settings.netconf_password = "lab-password"
        settings.netconf_known_hosts = "/tmp/known-hosts"
        settings.allowed_device_cidrs = "10.12.0.0/16"
        settings.netconf_action_templates_json = json.dumps(
            {
                "vendor-x:ret_adjustment": (
                    '<config xmlns="urn:test"><radio><cell>__TOWER_ID__</cell>'
                    "<tilt>__RET_TILT_DEGREES__</tilt></radio></config>"
                )
            }
        )
        settings.__dict__.pop("netconf_action_templates", None)
        tower = Tower(
            tower_id="ET-001",
            region="Addis Ababa",
            netconf_host="10.12.1.5",
            yang_profile="vendor-x",
            enabled=True,
        )
        action = OptimizationAction(
            tower_id="ET-001",
            action_type="ret_adjustment",
            parameters={"tilt_degrees": 6},
            status="approved",
            created_by="engineer",
        )
        rendered = NetconfDriver()._render_config(tower, action)
        self.assertIn("<tilt>6</tilt>", rendered)
        outside = Tower(
            tower_id="ET-002",
            region="Addis Ababa",
            netconf_host="203.0.113.10",
            yang_profile="vendor-x",
            enabled=True,
        )
        with self.assertRaisesRegex(NetworkDriverError, "allowlist"):
            NetconfDriver()._validate_target(outside)


if __name__ == "__main__":
    unittest.main()
