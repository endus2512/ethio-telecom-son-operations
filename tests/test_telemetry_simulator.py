"""Tests for the session-only network telemetry simulator."""

from __future__ import annotations

import random
import unittest

from src.telemetry_simulator import (
    create_simulated_telemetry,
    refresh_simulated_telemetry,
    simulate_self_healing,
)


class TelemetrySimulatorTests(unittest.TestCase):
    def test_creates_labeled_addis_ababa_samples(self) -> None:
        sites = create_simulated_telemetry()

        self.assertEqual(len(sites), 6)
        self.assertEqual(
            {site["hardware_status"] for site in sites},
            {"Optimal", "Warning", "Critical"},
        )
        self.assertTrue(
            all(site["region"] == "Addis Ababa (simulated)" for site in sites)
        )
        self.assertTrue(
            all(site["source"] == "illustrative-synthetic" for site in sites)
        )

    def test_refresh_changes_dynamic_metrics(self) -> None:
        sites = create_simulated_telemetry()
        before = [
            (site["prb_usage_pct"], site["call_drop_rate_pct"], site["throughput_mbps"])
            for site in sites
        ]

        refresh_simulated_telemetry(sites, random.Random(17))

        after = [
            (site["prb_usage_pct"], site["call_drop_rate_pct"], site["throughput_mbps"])
            for site in sites
        ]
        self.assertNotEqual(before, after)

    def test_self_healing_only_normalizes_selected_sample(self) -> None:
        sites = create_simulated_telemetry()
        critical = next(site for site in sites if site["hardware_status"] == "Critical")
        other_site = next(site for site in sites if site is not critical)
        original_status = other_site["hardware_status"]

        action = simulate_self_healing(sites, critical["tower_id"], "noc-supervisor")

        self.assertEqual(critical["hardware_status"], "Optimal")
        self.assertLessEqual(critical["call_drop_rate_pct"], 1.5)
        self.assertEqual(other_site["hardware_status"], original_status)
        self.assertTrue(action["simulation_only"])
        self.assertEqual(action["actor"], "noc-supervisor")
        with self.assertRaisesRegex(ValueError, "already in a normal"):
            simulate_self_healing(sites, critical["tower_id"], "noc-supervisor")


if __name__ == "__main__":
    unittest.main()
