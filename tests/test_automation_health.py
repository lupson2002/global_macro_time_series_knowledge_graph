import unittest
from datetime import datetime, timezone

from scripts.check_automation_health import evaluate_health


class AutomationHealthTests(unittest.TestCase):
    def test_latest_success_supersedes_an_older_unfinished_run(self):
        summaries = [
            {
                "run_id": "new", "mode": "pipeline", "report": None,
                "status": "success", "started_at": "2026-08-07T09:00:00+00:00",
            },
            {
                "run_id": "old", "mode": "pipeline", "report": None,
                "status": "running", "started_at": "2026-08-06T00:00:00+00:00",
            },
        ]

        health = evaluate_health(
            summaries, now=datetime(2026, 8, 7, 10, tzinfo=timezone.utc), stale_hours=8,
        )

        self.assertTrue(health["healthy"])
        self.assertEqual(health["stale_runs"], [])

    def test_latest_failure_and_stale_run_are_unhealthy(self):
        summaries = [
            {
                "run_id": "daily-fail", "mode": "report", "report": "daily",
                "status": "failed", "started_at": "2026-08-07T09:00:00+00:00",
            },
            {
                "run_id": "pipeline-stale", "mode": "pipeline", "report": None,
                "status": "running", "started_at": "2026-08-06T00:00:00+00:00",
            },
        ]

        health = evaluate_health(
            summaries, now=datetime(2026, 8, 7, 10, tzinfo=timezone.utc), stale_hours=8,
        )

        self.assertFalse(health["healthy"])
        self.assertEqual(health["failed_subjects"], ["daily"])
        self.assertEqual(health["stale_runs"], ["pipeline-stale"])
