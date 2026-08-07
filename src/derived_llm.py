"""Typed request/result boundary for derived-report LLM generation."""

from __future__ import annotations

from dataclasses import dataclass

from src.llm_providers import ProviderAttempt


@dataclass(frozen=True)
class DerivedLLMRequest:
    pipeline: str
    system: str
    user: str
    max_tokens: int
    temperature: float
    nim_model: str
    ollama_attempts: int = 3
    model: str | None = None
    response_format: dict | None = None


@dataclass(frozen=True)
class DerivedLLMResult:
    pipeline: str
    content: str
    provider: str
    model: str
    latency_ms: float
    attempts: tuple[ProviderAttempt, ...]


def _chat_completion_result(**kwargs):
    """Keep provider SDK loading lazy, matching the legacy derived call sites."""
    from src import cloud_client

    return cloud_client.chat_completion_result(**kwargs)


def complete_derived(request: DerivedLLMRequest) -> DerivedLLMResult:
    """Execute through the existing cloud route and attach pipeline identity."""
    result = _chat_completion_result(
        system=request.system,
        user=request.user,
        max_tokens=request.max_tokens,
        temperature=request.temperature,
        model=request.model,
        nim_model=request.nim_model,
        response_format=request.response_format,
        ollama_attempts=request.ollama_attempts,
    )
    return DerivedLLMResult(
        pipeline=request.pipeline,
        content=result.content,
        provider=result.provider,
        model=result.model,
        latency_ms=result.latency_ms,
        attempts=result.attempts,
    )
