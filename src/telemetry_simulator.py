"""Session-scoped synthetic telecom telemetry for dashboard demonstrations."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

SIMULATED_CELLS = (
    (9.019, 38.752, 64.0, 1.1, 58.0, 112.0, 540),
    (9.034, 38.761, 83.0, 3.8, 104.0, 67.0, 720),
    (8.997, 38.729, 94.0, 6.2, 132.0, 43.0, 810),
    (9.051, 38.783, 52.0, 0.8, 54.0, 138.0, 430),
    (9.008, 38.792, 87.0, 4.1, 111.0, 61.0, 650),
    (9.067, 38.735, 42.0, 0.6, 49.0, 154.0, 380),
)


def _status_for(traffic: float, drop_rate: float, latency: float) -> str:
    if traffic >= 90 or drop_rate > 5 or latency > 120:
        return "Critical"
    if traffic >= 80 or drop_rate > 3 or latency > 100:
        return "Warning"
    return "Optimal"


def create_simulated_telemetry() -> list[dict[str, Any]]:
    """Return synthetic Addis Ababa cell readings; no database is accessed."""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    sites: list[dict[str, Any]] = []
    for index, (lat, lon, traffic, drop, latency, throughput, users) in enumerate(
        SIMULATED_CELLS, start=1
    ):
        sites.append(
            {
                "tower_id": f"SIM-ADD-{index:03d}",
                "region": "Addis Ababa (simulated)",
                "latitude": lat,
                "longitude": lon,
                "prb_usage_pct": traffic,
                "call_drop_rate_pct": drop,
                "latency_ms": latency,
                "throughput_mbps": throughput,
                "connected_users": users,
                "hardware_status": _status_for(traffic, drop, latency),
                "timestamp": timestamp,
                "source": "illustrative-synthetic",
                "optimized": False,
            }
        )
    return sites


def refresh_simulated_telemetry(
    sites: list[dict[str, Any]], rng: random.Random | None = None
) -> list[dict[str, Any]]:
    """Advance synthetic KPIs by one interval and return the updated site list."""
    randomizer = rng or random
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for site in sites:
        optimized = bool(site.get("optimized"))
        traffic_floor, traffic_ceiling = (45.0, 70.0) if optimized else (15.0, 99.0)
        traffic = min(
            traffic_ceiling,
            max(
                traffic_floor,
                float(site["prb_usage_pct"]) + randomizer.uniform(-3.0, 3.0),
            ),
        )
        drop_ceiling = 1.5 if optimized else 8.0
        drop = min(
            drop_ceiling,
            max(
                0.1,
                float(site["call_drop_rate_pct"])
                + randomizer.uniform(-0.35 if optimized else -0.7, 0.35 if optimized else 0.7),
            ),
        )
        latency_floor, latency_ceiling = (40.0, 85.0) if optimized else (25.0, 155.0)
        latency = min(
            latency_ceiling,
            max(
                latency_floor,
                float(site["latency_ms"]) + randomizer.uniform(-5.0, 5.0),
            ),
        )
        user_factor = randomizer.uniform(0.96, 1.04)
        site.update(
            {
                "prb_usage_pct": round(traffic, 1),
                "call_drop_rate_pct": round(drop, 2),
                "latency_ms": round(latency, 1),
                "throughput_mbps": round(
                    max(1.0, float(site["throughput_mbps"]) * (1 + randomizer.uniform(-0.06, 0.06))),
                    1,
                ),
                "connected_users": max(0, round(int(site["connected_users"]) * user_factor)),
                "hardware_status": _status_for(traffic, drop, latency),
                "timestamp": timestamp,
            }
        )
    return sites


def simulate_self_healing(
    sites: list[dict[str, Any]], cell_id: str, actor: str
) -> dict[str, Any]:
    """Apply a clearly simulated optimization to one site in session memory."""
    site = next((row for row in sites if row["tower_id"] == cell_id), None)
    if site is None:
        raise ValueError(f"Unknown simulated cell: {cell_id}")
    if site["hardware_status"] == "Optimal":
        raise ValueError(f"{cell_id} is already in a normal simulated state.")

    site.update(
        {
            "prb_usage_pct": 58.0,
            "call_drop_rate_pct": 0.7,
            "latency_ms": 62.0,
            "throughput_mbps": round(max(95.0, float(site["throughput_mbps"]) * 1.8), 1),
            "hardware_status": "Optimal",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "optimized": True,
        }
    )
    return {
        "cell_id": cell_id,
        "actor": actor,
        "action": "Simulated load balancing and radio parameter optimization",
        "result": "Simulated KPIs returned to normal thresholds",
        "occurred_at": site["timestamp"],
        "simulation_only": True,
    }
