# -*- coding: utf-8 -*-
"""
cloud_client.py — Ollama Cloud 우선(공식 ollama Client) + NIM 폴백 공용 LLM 클라이언트
========================================================================================
NVIDIA NIM(504 장애)에서 Ollama Cloud(deepseek-v4-flash:0731-cloud)로 전환.
공식 `ollama` Python 라이브러리 규격 준수:
  - Client(host="https://ollama.com", headers={"Authorization": "Bearer <KEY>"})
  - client.chat(model, messages=[{role, content}], stream=False,
                format="json"(옵션), options={"num_predict":N, "temperature":T})

우선순위:
  1. Ollama Cloud (공식 ollama Client)  — model=OLLAMA_PRO_MODEL
  2. NIM 폴백      (OpenAI 호환 localhost:8000) — nim_model

`chat_completion()` 은 system/user 프롬프트로 텍스트를 생성, 성공 프로바이더를 로그.
"""
from __future__ import annotations

import time

from openai import OpenAI
from ollama import Client as OllamaClient

from src.config import settings

# ── Ollama Cloud (메인, 공식 ollama Client) ──
OLLAMA_PRO_BASE_URL = settings.llm.ollama_base_url
OLLAMA_PRO_API_KEY = settings.llm.ollama_api_key
OLLAMA_PRO_MODEL = settings.llm.ollama_model

# ── NIM 폴백 (보조) ──
NIM_BASE_URL = settings.llm.nim_base_url
NIM_API_KEY = settings.llm.nim_api_key
NIM_MODEL = settings.llm.tier2_model

_TIMEOUT = settings.llm.timeout

# 👑 [2026-08-06 M5] 클라이언트 lazy 싱글턴 — 호출마다 OllamaClient/OpenAI 를
# 재생성하면 연결이 재수립되어 6채널×재시도면 수십 개 연결이 누적됨.
_ollama_client: OllamaClient | None = None
_openai_client: OpenAI | None = None


def _get_ollama_client() -> OllamaClient:
    """Ollama 공식 Client 싱글턴. .env OLLAMA_PRO_BASE_URL 은 OpenAI 호환(/v1) 용이라
    ollama Client(native /api/chat) 호스트에선 /v1 접미사를 제거해야
    404("path /v1/api/chat not found")를 방지."""
    global _ollama_client
    if _ollama_client is None:
        host = OLLAMA_PRO_BASE_URL
        if host.rstrip("/").endswith("/v1"):
            host = host.rstrip("/")[:-3]
        _ollama_client = OllamaClient(
            host=host,
            headers={"Authorization": f"Bearer {OLLAMA_PRO_API_KEY}"},
        )
    return _ollama_client


def _get_openai_client() -> OpenAI:
    """NIM(OpenAI 호환) 클라이언트 싱글턴."""
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(base_url=NIM_BASE_URL, api_key=NIM_API_KEY, timeout=_TIMEOUT)
    return _openai_client


def _extract_content_ollama(resp) -> str:
    content = resp.message.content
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "").strip()


def chat_completion(
    system: str,
    user: str,
    max_tokens: int = 4096,
    temperature: float = 0.3,
    model: str | None = None,
    nim_model: str | None = None,
    response_format: dict | None = None,
) -> str:
    """Ollama Cloud(공식 Client) 우선 → NIM(OpenAI 호환) 폴백으로 텍스트 생성.

    model: Ollama 모델(기본 OLLAMA_PRO_MODEL). nim_model: NIM 폴백 모델.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    m = model or OLLAMA_PRO_MODEL
    nm = nim_model or NIM_MODEL
    options = {"num_predict": int(max_tokens), "temperature": float(temperature)}
    fmt = "json" if response_format else None

    # 1) Ollama Cloud (공식 ollama Client — Bearer 인증 헤더 + host 명시)
    # 👑 [2026-08-07] 빈 응답/일시 오류 시 즉시 NIM 폴백하지 않고 3회 재시도.
    #    합성 프롬프트(24h 전체 데이터)에서 Ollama가 간헐적 빈 응답을 반환하며,
    #    그때 NIM(504 장애)으로 바로 넘어가 병목이 됐던 사례 확인 → 재시도로 방어.
    for _attempt in range(1, 4) if OLLAMA_PRO_API_KEY else ():
        try:
            client = _get_ollama_client()
            # 👑 [2026-08-07] 근본원인: deepseek-v4-flash:0731-cloud 는 reasoning 모델이라
            #    매 응답이 thinking 필드를 먼저 생성 → num_predict(max_tokens) 예산이
            #    thinking에 소진되면 content='' + done_reason=length (빈 응답 오판).
            #    think=False 로 reasoning 비활성화 → 예산 100% content에 사용 (실측 검증).
            resp = client.chat(model=m, messages=messages, stream=False, think=False,
                               format=fmt, options=options)
            content = _extract_content_ollama(resp)
            if content:
                print(f"[CloudClient] Ollama OK ({m})")
                return content
            print(f"[CloudClient] Ollama 빈 응답 (시도 {_attempt}/3, done={getattr(resp, 'done_reason', '?')}) — 1.5s 후 재시도")
        except Exception as e:  # noqa: BLE001
            # ollama.ResponseError: e.status_code + e.error(서버 전체 메시지) — 400 원인 파악용
            err_detail = getattr(e, "error", None) or str(e)
            status = getattr(e, "status_code", "")
            print(f"[CloudClient] Ollama 실패 (status={status} {type(e).__name__}: {err_detail}) — 시도 {_attempt}/3, 1.5s 후 재시도")
        time.sleep(1.5)
    if OLLAMA_PRO_API_KEY:
        print("[CloudClient] Ollama 3회 빈 응답/실패 — NIM 폴백")
    else:
        print("[CloudClient] OLLAMA_PRO_API_KEY 미설정 — NIM 폴백")

    # 2) NIM 폴백 (OpenAI 호환)
    try:
        client = _get_openai_client()
        resp = client.chat.completions.create(
            model=nm,
            messages=messages,
            max_tokens=int(max_tokens),
            temperature=float(temperature),
            **({"response_format": response_format} if response_format else {}),
        )
        content = resp.choices[0].message.content
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = str(content or "").strip()
        print(f"[CloudClient] NIM 폴백 ({nm})")
        return content
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"CloudClient: Ollama+NIM 모두 실패. 마지막 오류: {type(e).__name__}: {e}"
        )


if __name__ == "__main__":
    out = chat_completion("You are helpful.", "Say the word OK", max_tokens=100, temperature=0)
    print(f"결과: {out.strip()[:40]!r}")
