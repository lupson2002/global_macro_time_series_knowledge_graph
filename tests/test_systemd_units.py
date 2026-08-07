import unittest
from pathlib import Path


class SystemdUnitContractTests(unittest.TestCase):
    def test_all_job_timers_are_persistent_and_services_use_wrappers(self):
        root = Path(__file__).resolve().parent.parent
        unit_dir = root / "deploy" / "systemd" / "user"
        jobs = {
            "macro-pipeline": "run_frequent.sh",
            "macro-daily-report": "run_morning_report.sh",
            "macro-auto-blog": "run_auto_blog.sh",
            "macro-insight-report": "run_insight_report.sh",
            "macro-cio-report": "run_weekly_orchestrator.sh",
            "macro-narrative-report": "run_market_narrative_report.sh",
        }
        for unit, wrapper in jobs.items():
            with self.subTest(unit=unit):
                timer = (unit_dir / f"{unit}.timer").read_text(encoding="utf-8")
                service = (unit_dir / f"{unit}.service").read_text(encoding="utf-8")
                self.assertIn("Persistent=true", timer)
                self.assertIn("WantedBy=timers.target", timer)
                self.assertIn(wrapper, service)
                self.assertIn("Environment=PYTHONUNBUFFERED=1", service)

    def test_watchdog_checks_job_timers_and_journal(self):
        root = Path(__file__).resolve().parent.parent
        unit_dir = root / "deploy" / "systemd" / "user"
        service = (unit_dir / "macro-watchdog.service").read_text(encoding="utf-8")
        timer = (unit_dir / "macro-watchdog.timer").read_text(encoding="utf-8")

        self.assertEqual(service.count(".timer"), 6)
        self.assertIn("check_automation_health.py", service)
        self.assertIn("*:00/15:00", timer)
        self.assertIn("Persistent=true", timer)
