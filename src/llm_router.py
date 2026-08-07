# -*- coding: utf-8 -*-
"""
llm_router.py — Llama70BRouter (Cerebras 1 + Groq 2 + NIM 폴백 멀티 라우터)
============================================================================
Llama 3.3 70B 등급 작업(고빈도·간단 번역/생성)을 위한 경량 LLM 라우터.
NVIDIA NIM 혼잡 완화를 목적으로, 저비용·저지연 프로바이더를 우선 사용하고
전부 실패 시 기존 NIM API 로 순차 폴백한다.

우회 순서 (Priority Flow):
  1. Cerebras          (llama-3.3-70b)               — CEREBRAS_API_KEY
  2. Groq Key 1        (llama-3.3-70b-versatile)     — GROQ_API_KEY_1
  3. Groq Key 2        (llama-3.3-70b-versatile)     — GROQ_API_KEY_2
  4. NIM 폴백          (deepseek-v4-flash)           — NIM_BASE_URL (기존)

각 프로바이더는 OpenAI 호환 API. 키가 없으면 해당 단계를 건너뛰고, 전부
키가 없으면 NIM 폴백만 사용(기존 동작과 동일).
"""
from __future__ import annotations

from openai import OpenAI

from src.config import settings
from src.llm_providers import (
    CompletionResult,
    ProviderChainError,
    ProviderStep,
    execute_provider_chain,
)

# ── NIM 폴백 (기존 3-Tier 공용) ──
NIM_BASE_URL = settings.llm.nim_base_url
NIM_API_KEY = settings.llm.nim_api_key
NIM_MODEL = settings.llm.tier2_model

# ── 프로바이더 정의 (이름, env 키, base_url, 모델) ──
# Cerebras: llama-3.3-70b 는 이 키에서 접근 불가(404) → 키로 접근 가능한 gpt-oss-120b 사용
# (실측: /v1/models → zai-glm-4.7 / gemma-4-31b / gpt-oss-120b)
_PROVIDER_SPECS = [
    ("Cerebras", settings.llm.cerebras_api_key, "https://api.cerebras.ai/v1", "gpt-oss-120b"),
    ("Groq_Key1", settings.llm.groq_api_key_1, "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    ("Groq_Key2", settings.llm.groq_api_key_2, "https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
]


class Llama70BRouter:
    """Llama 3.3 70B 급 경량 라우터 — 우선순위 순차 폴백 + NIM 안전장치."""

    def __init__(self, timeout: float = 120.0):
        self._providers: list[tuple[str, OpenAI, str]] = []  # (name, client, model)
        for name, api_key, base_url, model in _PROVIDER_SPECS:
            if api_key:
                self._providers.append(
                    (name, OpenAI(base_url=base_url, api_key=api_key, timeout=timeout), model)
                )
        # NIM 폴백 클라이언트 (기존 proxy 경유, 혼잡 시에도 안전장치)
        self._nim = OpenAI(base_url=NIM_BASE_URL, api_key=NIM_API_KEY, timeout=300.0)
        self._nim_model = NIM_MODEL
        self._rr_index = 0  # 라운드로빈 시작 포인터
        self.last_result: CompletionResult | None = None

    @property
    def available_providers(self) -> list[str]:
        """구성된 프로바이더 이름 목록 (키 미설정 시 빈 리스트)."""
        return [p[0] for p in self._providers]

    def generate(
        self,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
        **kwargs,
    ) -> str:
        """Backward-compatible text-only generation API."""
        return self.generate_result(
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=temperature,
            **kwargs,
        ).content

    def generate_result(
        self,
        system: str,
        user: str,
        max_tokens: int = 2048,
        temperature: float = 0.3,
        **kwargs,
    ) -> CompletionResult:
        """시스템+유저 프롬프트로 텍스트 생성. 우선순위 프로바이더 순차 시도,
        전부 실패(429/오류/빈응답) 시 NIM 폴백하고 관측 메타데이터를 반환."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        # 라운드로빈 — 매 호출마다 시작 프로바이더를 순환 (예: Groq Key1 ↔ Key2 번갈아 사용)
        n = len(self._providers)
        start = self._rr_index % n if n else 0
        ordered: list[tuple[int, str, OpenAI, str]] = []
        for offset in range(n):
            idx = (start + offset) % n
            name, client, model = self._providers[idx]
            ordered.append((idx, name, client, model))

        def openai_call(client: OpenAI, model: str) -> str:
            def call() -> str:
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                return str(resp.choices[0].message.content or "").strip()

            return call

        steps = [
            ProviderStep(name, model, openai_call(client, model))
            for _, name, client, model in ordered
        ]
        steps.append(
            ProviderStep("NIM", self._nim_model, openai_call(self._nim, self._nim_model))
        )
        try:
            result = execute_provider_chain(steps)
        except ProviderChainError as exc:
            if n:
                self._rr_index = (ordered[-1][0] + 1) % n
            raise RuntimeError(
                f"Llama70BRouter: 모든 프로바이더 실패. 마지막 오류: {exc}"
            ) from exc

        for attempt in result.attempts:
            if not attempt.succeeded:
                print(f"[Llama70BRouter] {attempt.provider} 실패 ({attempt.error}) — failover")
        if result.provider != "NIM":
            success_idx = next(idx for idx, name, _, _ in ordered if name == result.provider)
            self._rr_index = (success_idx + 1) % n
        elif n:
            self._rr_index = (ordered[-1][0] + 1) % n
        self.last_result = result
        print(
            f"[Llama70BRouter] Success via {result.provider} "
            f"({result.model}, {result.latency_ms:.0f}ms)"
        )
        return result

    def _fallback_nim(self, messages: list, max_tokens: int, temperature: float) -> str:
        """Compatibility helper retained for callers that use the former private method."""
        try:
            resp = self._nim.chat.completions.create(
                model=self._nim_model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            content = (resp.choices[0].message.content or "").strip()
            print(f"[Llama70BRouter] Fallback via NIM ({self._nim_model})")
            return content
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"Llama70BRouter: 모든 프로바이더 실패. 마지막 오류: {type(e).__name__}: {e}"
            )


if __name__ == "__main__":
    router = Llama70BRouter()
    print(f"[Llama70BRouter] 구성 프로바이더: {router.available_providers or '(키 미설정 → NIM 폴백만)'}")

    # 1) Failover 시뮬레이션 — 1번 키 강제 실패 → 2번 키 성공 확인 (API 불필요, 항상 빠름)
    class _FakeCompletions:
        def __init__(self, fail: bool):
            self.fail = fail

        def create(self, **kwargs):
            if self.fail:
                raise RuntimeError("simulated failure (429 Rate Limit)")
            return type("R", (), {"choices": [type("C", (), {"message": type("M", (), {"content": "OK (failover success)"})()})()]})()  # noqa: E501

    class _FakeClient:
        def __init__(self, fail: bool):
            self.chat = type("c", (), {"completions": _FakeCompletions(fail)})()

    orig = router._providers
    router._providers = [
        ("Cerebras", _FakeClient(True), "llama-3.3-70b"),
        ("Groq_Key1", _FakeClient(False), "llama-3.3-70b-versatile"),
    ]
    try:
        out = router.generate("s", "u", max_tokens=5)
        assert "OK (failover success)" in out, "Failover 실패"
        print(f"[test] ✅ Failover 동작: 1번 키 실패 → 2번 키 성공 → {out!r}")
    finally:
        router._providers = orig

    # 2) 라이브 생성 테스트 — 키 설정 시 Cerebras/Groq 사용, 키 없으면 NIM 폴백(느릴 수 있음)
    try:
        out = router.generate(
            "You are a helpful assistant.",
            "Reply with the single word: OK",
            max_tokens=8,
            temperature=0,
        )
        print(f"[test] 라이브 생성 결과: {out.strip()[:40]!r}")
    except Exception as e:
        print(f"[test] 라이브 생성 실패: {e}")
