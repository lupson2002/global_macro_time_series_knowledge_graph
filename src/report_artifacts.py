"""File artifact plans and writer for derived reports."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class ReportArtifact:
    path: Path
    content: str


def daily_artifact(vault_dir: str | Path, date: str, content: str) -> ReportArtifact:
    return ReportArtifact(
        Path(vault_dir) / "Daily_Reports" / f"Daily_Macro_Synthesis_{date}.md",
        content,
    )


def cio_artifact(vault_dir: str | Path, date: str, content: str) -> ReportArtifact:
    return ReportArtifact(Path(vault_dir) / "reports" / f"Grand_Report_{date}.md", content)


def insight_artifact(project_root: str | Path, date: str, content: str) -> ReportArtifact:
    return ReportArtifact(
        Path(project_root) / "reports" / "insights" / f"insight_report_{date}.md",
        content,
    )


def narrative_artifacts(
    reports_dir: str | Path,
    obsidian_dir: str | Path,
    date: str,
    report_content: str,
    vault_content: str,
) -> tuple[ReportArtifact, ReportArtifact]:
    return (
        ReportArtifact(Path(reports_dir) / f"market_narrative_{date}.md", report_content),
        ReportArtifact(Path(obsidian_dir) / f"Market_Narrative_{date}.md", vault_content),
    )


def weekly_artifacts(
    project_root: str | Path,
    vault_dir: str | Path,
    date: str,
    report_content: str,
    vault_content: str,
) -> tuple[ReportArtifact, ReportArtifact]:
    return (
        ReportArtifact(
            Path(project_root) / "reports" / "weekly" / f"weekly_investment_intelligence_{date}.md",
            report_content,
        ),
        ReportArtifact(
            Path(vault_dir) / "Weekly_Reports" / f"Weekly_Investment_Intelligence_{date}.md",
            vault_content,
        ),
    )


def write_report_artifact(artifact: ReportArtifact) -> Path:
    artifact.path.parent.mkdir(parents=True, exist_ok=True)
    artifact.path.write_text(artifact.content, encoding="utf-8")
    return artifact.path


def write_report_artifacts(artifacts: Iterable[ReportArtifact]) -> tuple[Path, ...]:
    return tuple(write_report_artifact(artifact) for artifact in artifacts)
