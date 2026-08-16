"""Testable single-video pipeline orchestration and explicit processing outcomes."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from src.ingestion import TranscriptUnavailableError, get_youtube_transcript


class PipelineStatus(str, Enum):
    SUCCESS = "success"
    SKIPPED = "skipped"
    FAILED = "failed"
    ABORTED = "aborted"


class PipelineStage(str, Enum):
    PRECHECK = "precheck"
    INGESTION = "ingestion"
    ANALYSIS = "analysis"
    RELEVANCE = "relevance"
    STORAGE = "storage"


@dataclass(frozen=True)
class VideoTarget:
    video_id: str
    source_channel: str
    upload_date: str | None = None


@dataclass(frozen=True)
class PipelineResult:
    target: VideoTarget
    status: PipelineStatus
    stage: PipelineStage
    message: str = ""
    transcript_chars: int = 0
    markdown_path: Path | None = None
    warnings: tuple[str, ...] = ()

    @property
    def abort_queue(self) -> bool:
        return self.status is PipelineStatus.ABORTED


def check_processed(db_path: str, video_id: str, include_skipped: bool = True) -> bool:
    """Return whether a video is already successful or intentionally skipped."""
    if not Path(db_path).exists():
        return False
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM reports WHERE video_id = ?", (video_id,))
            if cur.fetchone() is not None:
                return True
            if include_skipped:
                cur.execute("SELECT 1 FROM skipped_videos WHERE video_id = ?", (video_id,))
                return cur.fetchone() is not None
            return False
    except (sqlite3.Error, OSError):
        # Legacy DBs may not have skipped_videos yet; conservatively allow retry.
        return False


def is_macro_relevant(data: dict) -> bool:
    """Return whether extracted data contains a meaningful macro signal."""
    graph_nodes = data.get("graph_nodes", {}) or {}
    tickers = [ticker for ticker in (graph_nodes.get("specific_tickers") or []) if ticker]
    signals = data.get("quant_signals", {}) or {}
    bull_bear = signals.get("bull_bear_score")
    conviction = signals.get("conviction_score")
    has_tactical = any(
        signals.get(key) for key in ("duration_call", "macro_factor", "view_time_horizon")
    )
    return bool(
        tickers
        or has_tactical
        or (bull_bear is not None and bull_bear != 5)
        or (conviction is not None and conviction != 5)
    )


def is_youtube_ip_block(message: str) -> bool:
    normalized = message.lower()
    return any(
        marker in normalized
        for marker in ("blocking requests from your ip", "blocking your requests", "ipblocked")
    )


class PipelineService:
    """Coordinate one video without owning CLI parsing, counters, or process exit."""

    def __init__(
        self,
        *,
        db_path: str,
        llm_client,
        sqlite_exporter,
        obsidian_exporter,
        vector_projection=None,
        ingest: Callable[[str], str] = get_youtube_transcript,
        relevance_check: Callable[[dict], bool] = is_macro_relevant,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.db_path = db_path
        self.llm_client = llm_client
        self.sqlite_exporter = sqlite_exporter
        self.obsidian_exporter = obsidian_exporter
        if vector_projection is None:
            from src.projections import LanceDbProjection

            vector_projection = LanceDbProjection()
        self.vector_projection = vector_projection
        self.ingest = ingest
        self.relevance_check = relevance_check
        self.sleep = sleep

    def process(
        self,
        target: VideoTarget,
        *,
        overwrite: bool = False,
        apply_delays: bool = False,
        ingest_delay: float = 0.0,
        llm_delay: float = 0.0,
    ) -> PipelineResult:
        if not overwrite and check_processed(self.db_path, target.video_id):
            return PipelineResult(
                target, PipelineStatus.SKIPPED, PipelineStage.PRECHECK, "already_processed"
            )

        try:
            if apply_delays and ingest_delay > 0:
                self.sleep(ingest_delay)
            transcript = self.ingest(target.video_id)
        except TranscriptUnavailableError as exc:
            warnings: tuple[str, ...] = ()
            try:
                self.sqlite_exporter.mark_skipped(
                    target.video_id, reason="no_transcript"
                )
            except Exception as sql_exc:  # noqa: BLE001
                warnings = (f"skip persistence: {type(sql_exc).__name__}: {sql_exc}",)
            return PipelineResult(
                target,
                PipelineStatus.SKIPPED,
                PipelineStage.INGESTION,
                f"no_transcript: {exc}",
                warnings=warnings,
            )
        except Exception as exc:  # noqa: BLE001 - converted to a typed pipeline outcome
            message = f"{type(exc).__name__}: {exc}"
            status = (
                PipelineStatus.ABORTED
                if is_youtube_ip_block(str(exc))
                else PipelineStatus.FAILED
            )
            return PipelineResult(target, status, PipelineStage.INGESTION, message)

        try:
            if apply_delays and llm_delay > 0:
                self.sleep(llm_delay)
            extracted = self.llm_client.analyze_transcript(
                transcript,
                target.video_id,
                source_channel=target.source_channel,
                upload_date=target.upload_date,
            )
        except Exception as exc:  # noqa: BLE001
            return PipelineResult(
                target,
                PipelineStatus.FAILED,
                PipelineStage.ANALYSIS,
                f"{type(exc).__name__}: {exc}",
                transcript_chars=len(transcript),
            )

        if not self.relevance_check(extracted):
            warnings: tuple[str, ...] = ()
            try:
                self.sqlite_exporter.mark_skipped(
                    target.video_id, reason="not_macro_relevant"
                )
            except Exception as exc:  # noqa: BLE001 - skip remains valid but retry may recur
                warnings = (f"skip persistence: {type(exc).__name__}: {exc}",)
            return PipelineResult(
                target,
                PipelineStatus.SKIPPED,
                PipelineStage.RELEVANCE,
                "not_macro_relevant",
                transcript_chars=len(transcript),
                warnings=warnings,
            )

        # Markdown first: a DB success remains the authoritative completion marker.
        # If Markdown fails, the video stays retryable instead of becoming a fast-skip.
        try:
            markdown_path = Path(self.obsidian_exporter.export_markdown(extracted))
        except Exception as exc:  # noqa: BLE001
            return PipelineResult(
                target,
                PipelineStatus.FAILED,
                PipelineStage.STORAGE,
                f"{type(exc).__name__}: {exc}",
                transcript_chars=len(transcript),
            )
        try:
            self.sqlite_exporter.export_data(extracted)
        except Exception as exc:  # noqa: BLE001
            return PipelineResult(
                target,
                PipelineStatus.FAILED,
                PipelineStage.STORAGE,
                f"{type(exc).__name__}: {exc}",
                transcript_chars=len(transcript),
                markdown_path=markdown_path,
                warnings=("markdown_saved_database_pending",),
            )

        warnings: tuple[str, ...] = ()
        try:
            self.vector_projection.project(extracted)
        except Exception as exc:  # noqa: BLE001 - source is committed; projection is repairable
            warnings = (
                f"vector_projection_pending: {type(exc).__name__}: {exc}",
            )

        return PipelineResult(
            target,
            PipelineStatus.SUCCESS,
            PipelineStage.STORAGE,
            transcript_chars=len(transcript),
            markdown_path=markdown_path,
            warnings=warnings,
        )
