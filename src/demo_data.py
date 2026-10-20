"""Explicitly synthetic telemetry for local dashboard demonstrations."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.dbmodels import FaultEvent, KpiMetric, Tower

DEMO_TOWERS = (
    {
        "tower_id": "DEMO-ADD-001",
        "region": "Addis Ababa",
        "latitude": 9.03,
        "longitude": 38.74,
        "users": 620,
        "prb": 48.0,
    },
    {
        "tower_id": "DEMO-ORO-002",
        "region": "Oromia",
        "latitude": 8.98,
        "longitude": 38.72,
        "users": 390,
        "prb": 62.0,
    },
    {
        "tower_id": "DEMO-AMH-003",
        "region": "Amhara",
        "latitude": 11.59,
        "longitude": 37.39,
        "users": 280,
        "prb": 72.0,
    },
)


def seed_demo_data(db: Session) -> bool:
    """Add one labeled synthetic telemetry set if the database has no KPI data."""
    if db.scalar(select(func.count()).select_from(KpiMetric)):
        return False

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    towers: dict[str, Tower] = {}
    for details in DEMO_TOWERS:
        tower = db.get(Tower, details["tower_id"])
        if tower is None:
            tower = Tower(
                tower_id=details["tower_id"],
                region=details["region"],
                latitude=details["latitude"],
                longitude=details["longitude"],
                hardware_status="Optimal",
                enabled=True,
            )
            db.add(tower)
        towers[details["tower_id"]] = tower
    db.flush()

    metrics: list[KpiMetric] = []
    for tower_index, details in enumerate(DEMO_TOWERS):
        for hour in range(72, -1, -1):
            timestamp = now - timedelta(hours=hour)
            cycle = math.sin((timestamp.hour + tower_index * 3) * math.pi / 12)
            prb = details["prb"] + 11 * cycle + 4 * math.sin(hour / 7)
            if tower_index == 2 and hour == 0:
                prb = 94.0
            metrics.append(
                KpiMetric(
                    tower_id=details["tower_id"],
                    timestamp=timestamp,
                    connected_users=max(
                        0, int(details["users"] + 95 * cycle + 20 * math.sin(hour / 5))
                    ),
                    prb_usage_pct=max(0.0, min(100.0, prb)),
                    throughput_mbps=max(
                        1.0, details["users"] * 0.24 * (1 - prb / 130)
                    ),
                    latency_ms=45 + prb * 0.58 + tower_index * 5,
                    call_drop_rate_pct=(
                        5.8 if tower_index == 2 and hour == 0 else 0.45 + prb / 80
                    ),
                    handover_failure_rate_pct=0.55 + prb / 90,
                    hardware_status=(
                        "Degraded" if tower_index == 2 and hour == 0 else "Optimal"
                    ),
                    source="synthetic-demo",
                )
            )
    db.add_all(metrics)
    towers[DEMO_TOWERS[2]["tower_id"]].hardware_status = "Degraded"
    db.flush()
    db.add_all(
        [
            FaultEvent(
                tower_id=DEMO_TOWERS[2]["tower_id"],
                occurred_at=now - timedelta(minutes=35),
                severity="major",
                alarm_code="DEMO-PRB-HIGH",
                description="Synthetic demo alarm: elevated PRB utilization.",
                raw_payload={"synthetic": True},
                source="synthetic-demo",
            ),
            FaultEvent(
                tower_id=DEMO_TOWERS[0]["tower_id"],
                occurred_at=now - timedelta(hours=7),
                cleared_at=now - timedelta(hours=6),
                severity="minor",
                alarm_code="DEMO-LATENCY",
                description="Synthetic demo alarm: transient latency increase.",
                raw_payload={"synthetic": True},
                source="synthetic-demo",
            ),
        ]
    )
    return True
