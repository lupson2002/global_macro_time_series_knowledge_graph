"""Opt-in structured execution events for pipeline observability."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

EventKind = Literal[
    "run.started", "run.finished",
    "video.started", "video.finished",
    "report.started", "report.finished",
]
EventStatus = Literal["running", "success", "skipped", "failed"]


@dataclass(frozen=True)
class RunEvent:
    """A JSON-serializable pipeline lifecycle event."""

    event: EventKind
    status: EventStatus
    run_id: str
    timestamp: str
    video_id: str | None = None
    source: str | None = None
    report: str | None = None
    stage: str | None = None
    details: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = 1

    def as_dict(self) -> dict[str, object]:
        """Return the stable on-disk representation."""
        return asdict(self)


class EventSink(Protocol):
    """Destination contract for structured run events."""

    def emit(self, event: RunEvent) -> None: ...


class JsonlEventSink:
    """Append one UTF-8 JSON object per event."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def emit(self, event: RunEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            json.dump(event.as_dict(), stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")


class SafeEventEmitter:
    """Keep observability failures outside the core pipeline result."""

    def __init__(
        self,
        sink: EventSink | None,
        *,
        warn: Callable[[str], None],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sink = sink
        self._warn = warn
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def emit(
        self,
        event: EventKind,
        status: EventStatus,
        run_id: str,
        *,
        video_id: str | None = None,
        source: str | None = None,
        report: str | None = None,
        stage: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        if self._sink is None:
            return
        record = RunEvent(
            event=event,
            status=status,
            run_id=run_id,
            timestamp=self._clock().astimezone(timezone.utc).isoformat(),
            video_id=video_id,
            source=source,
            report=report,
            stage=stage,
            details=details or {},
        )
        try:
            self._sink.emit(record)
        except Exception as exc:  # noqa: BLE001 - telemetry is best-effort
            self._sink = None
            self._warn(f"Structured event log disabled after write failure: {exc}")


class RunJournal:
    """Convenience facade for one pipeline run's lifecycle."""

    def __init__(self, emitter: SafeEventEmitter, run_id: str | None = None) -> None:
        self._emitter = emitter
        self.run_id = run_id or uuid.uuid4().hex

    @classmethod
    def from_path(
        cls, path: Path | None, *, warn: Callable[[str], None]
    ) -> RunJournal:
        sink = JsonlEventSink(path) if path else None
        return cls(SafeEventEmitter(sink, warn=warn))

    def run_started(self, mode: str) -> None:
        self._emitter.emit(
            "run.started", "running", self.run_id, details={"mode": mode}
        )

    def video_started(self, video_id: str, source: str) -> None:
        self._emitter.emit(
            "video.started", "running", self.run_id,
            video_id=video_id, source=source,
        )

    def video_finished(
        self,
        video_id: str,
        source: str,
        status: EventStatus,
        stage: str,
        *,
        abort_queue: bool,
        transcript_chars: int,
        warning_count: int,
    ) -> None:
        self._emitter.emit(
            "video.finished", status, self.run_id,
            video_id=video_id, source=source, stage=stage,
            details={
                "abort_queue": abort_queue,
                "transcript_chars": transcript_chars,
                "warning_count": warning_count,
            },
        )

    def report_started(self, report: str) -> None:
        self._emitter.emit(
            "report.started", "running", self.run_id, report=report
        )

    def report_finished(
        self,
        report: str,
        status: EventStatus,
        stage: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self._emitter.emit(
            "report.finished", status, self.run_id,
            report=report, stage=stage, details=details,
        )

    def run_finished(
        self,
        exit_code: int,
        *,
        stage: str | None = None,
        counts: Mapping[str, int] | None = None,
    ) -> None:
        details: dict[str, object] = {"exit_code": exit_code}
        details.update(counts or {})
        self._emitter.emit(
            "run.finished",
            "failed" if exit_code else "success",
            self.run_id,
            stage=stage,
            details=details,
        )


class ReportRunJournal:
    """Record one derived-report invocation without owning its control flow."""

    def __init__(self, run: RunJournal, report: str) -> None:
        self._run = run
        self.report = report

    @classmethod
    def from_path(
        cls,
        path: Path | None,
        report: str,
        *,
        warn: Callable[[str], None],
    ) -> ReportRunJournal:
        return cls(RunJournal.from_path(path, warn=warn), report)

    def started(self) -> None:
        self._run.run_started("report")
        self._run.report_started(self.report)

    def finished(
        self,
        *,
        success: bool,
        stage: str,
        error: BaseException | None = None,
    ) -> None:
        status: EventStatus = "success" if success else "failed"
        details = {"error_type": type(error).__name__} if error else {}
        self._run.report_finished(self.report, status, stage, details)
        self._run.run_finished(0 if success else 1, stage=stage)
