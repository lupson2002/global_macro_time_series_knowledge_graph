import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from src.report_artifacts import (
    ReportArtifact,
    cio_artifact,
    daily_artifact,
    insight_artifact,
    narrative_artifacts,
    write_report_artifact,
    write_report_artifacts,
)


class ReportArtifactPathTests(unittest.TestCase):
    def test_public_report_paths_preserve_existing_names(self):
        root = Path("/project")
        self.assertEqual(
            daily_artifact(root / "vault", "2026-08-07", "daily").path,
            root / "vault/Daily_Reports/Daily_Macro_Synthesis_2026-08-07.md",
        )
        self.assertEqual(
            cio_artifact(root / "vault", "2026-08-07", "cio").path,
            root / "vault/reports/Grand_Report_2026-08-07.md",
        )
        self.assertEqual(
            insight_artifact(root, "2026-08-07", "insight").path,
            root / "reports/insights/insight_report_2026-08-07.md",
        )
        narrative = narrative_artifacts(
            root / "reports/narrative", root / "vault/Narrative_Reports",
            "2026-08-07", "report", "vault",
        )
        self.assertEqual(narrative[0].path.name, "market_narrative_2026-08-07.md")
        self.assertEqual(narrative[1].path.name, "Market_Narrative_2026-08-07.md")

    def test_writer_creates_parent_and_preserves_utf8_content(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = ReportArtifact(Path(directory) / "nested/report.md", "한글 report")
            result = write_report_artifact(artifact)
            self.assertEqual(result, artifact.path)
            self.assertEqual(result.read_text(encoding="utf-8"), "한글 report")

    def test_multiple_artifacts_are_written_in_declared_order(self):
        artifacts = (
            ReportArtifact(Path("first.md"), "first"),
            ReportArtifact(Path("second.md"), "second"),
        )
        with patch("src.report_artifacts.write_report_artifact", side_effect=lambda a: a.path) as write:
            paths = write_report_artifacts(artifacts)
        self.assertEqual(paths, (Path("first.md"), Path("second.md")))
        self.assertEqual(write.call_args_list, [call(artifacts[0]), call(artifacts[1])])


if __name__ == "__main__":
    unittest.main()
