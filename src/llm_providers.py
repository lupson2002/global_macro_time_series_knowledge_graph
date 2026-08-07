"""Provider-neutral LLM execution, failover, and observability primitives."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterable


ProviderCall = Callable[[], str]


@dataclass(frozen=True)
class ProviderAttempt:
    provider: str
    model: str
    attempt: int
    latency_ms: float
    succeeded: bool
    error: str = ""


@dataclass(frozen=True)
class CompletionResult:
    content: str
    provider: str
    model: str
    latency_ms: float
    attempts: tuple[ProviderAttempt, ...]


@dataclass(frozen=True)
class ProviderStep:
    name: str
    model: str
    call: ProviderCall
    max_attempts: int = 1
    retry_delay_s: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.retry_delay_s < 0:
            raise ValueError("retry_delay_s cannot be negative")


class ProviderChainError(RuntimeError):
    """Raised after every configured provider step has been exhausted."""

    def __init__(self, attempts: tuple[ProviderAttempt, ...]):
        self.attempts = attempts
        detail = attempts[-1].error if attempts else "no providers configured"
        super().__init__(f"all providers exhausted; last result: {detail}")


def execute_provider_chain(
    steps: Iterable[ProviderStep],
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.perf_counter,
) -> CompletionResult:
    """Try provider steps in order and return content plus complete attempt metadata."""
    started = clock()
    history: list[ProviderAttempt] = []
    for step in steps:
        for attempt_number in range(1, step.max_attempts + 1):
            attempt_started = clock()
            try:
                content = str(step.call() or "").strip()
                if not content:
                    raise RuntimeError("empty response")
            except Exception as exc:  # noqa: BLE001 - provider failures must fail over
                history.append(
                    ProviderAttempt(
                        provider=step.name,
                        model=step.model,
                        attempt=attempt_number,
                        latency_ms=(clock() - attempt_started) * 1000,
                        succeeded=False,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                if attempt_number < step.max_attempts and step.retry_delay_s:
                    sleep(step.retry_delay_s)
                continue

            history.append(
                ProviderAttempt(
                    provider=step.name,
                    model=step.model,
                    attempt=attempt_number,
                    latency_ms=(clock() - attempt_started) * 1000,
                    succeeded=True,
                )
            )
            return CompletionResult(
                content=content,
                provider=step.name,
                model=step.model,
                latency_ms=(clock() - started) * 1000,
                attempts=tuple(history),
            )
    raise ProviderChainError(tuple(history))
