# -*- coding: utf-8 -*-
"""
Structured extraction client for Global Macro Time-Series Knowledge Graph
=======================================================================
Builds and validates the macro extraction request. Generation is delegated to
src.cloud_client: Ollama Cloud first, then an OpenAI-compatible NIM fallback.
The NIM fallback model is deepseek-ai/deepseek-v4-flash by default.

[Ver 3.1 Renamed]  Formerly src/gemini_client.py / class GeminiMacroClient.
Class name and file name are retained for compatibility.

[Ver 4.6] 모델 교체 + 절삭 제거 (사용자 결정)
- Tier 1 모델: meta/llama-3.1-70b-instruct → deepseek-ai/deepseek-v4-flash.
- deepseek-v4-flash 는 대형 컨텍스트라 Hybrid 절삭(90K 분기) 불필요 —
  transcript 를 절삭 없이 통째로 single-shot 으로 전달.
- Hybrid 헬퍼(_hybrid_truncate/_summarize_middle/_hybrid_extract) 및
  관련 상수/CHUNK_SUMMARY_PROMPT 제거.
"""

import json
import re
import time
from typing import Optional
from pydantic import BaseModel, Field

from src.llm_response import ExtractionResponseProcessor

# ---------------------------------------------------------------------------
# Production endpoint / model constants
# ---------------------------------------------------------------------------
DEFAULT_MODEL_NAME = "deepseek-ai/deepseek-v4-flash"

# ---------------------------------------------------------------------------
# Pydantic Schemas for Structured JSON output (for type safety and documentation)
# ---------------------------------------------------------------------------
class MetadataSchema(BaseModel):
    speaker_name: str = Field(description="Name of the main speaker (e.g., Stanley Druckenmiller).")
    speaker_role: str = Field(description="Role of the speaker (e.g., Hedge Fund Manager, Economist).")
    source_channel: str = Field(description="YouTube channel or news source name.")
    broadcast_date: str = Field(description="Estimated broadcast or publication date (YYYY-MM-DD).")
    video_id: str = Field(description="11-character YouTube video ID.")
    # 👑 [Ver 4.4] 화자 소속 기관 — CIO/블로그 신뢰도 보강
    speaker_institution: str = Field(
        default="",
        description="The institution/firm the speaker belongs to (e.g., 'Duquesne', 'Bridgewater', 'JPMorgan'). Empty string if not stated."
    )

class GraphNodesSchema(BaseModel):
    time_box: str = Field(description="Target period of the macro view. MUST be formatted with double brackets like [[YYYY-QN]], [[YYYY-HN]], or [[YYYY]] (e.g., [[2026-H2]]).")
    macro_themes: list[str] = Field(description="Macro themes discussed. Every element MUST be enclosed in double brackets like [[Fed QT]].")
    asset_classes: list[str] = Field(description="Asset classes discussed. Every element MUST be enclosed in double brackets like [[Equities]].")
    specific_tickers: list[str] = Field(description="Specific asset tickers. Every element MUST be enclosed in double brackets like [[NVDA]].")

class QuantSignalsSchema(BaseModel):
    bull_bear_score: int = Field(description="Sentiment score: 1 (extremely bearish) to 5 (neutral) to 10 (extremely bullish).")
    conviction_score: int = Field(description="Speaker's conviction: 1 (weak guess) to 10 (extremely high conviction).")
    contrarian_flag: bool = Field(description="True if the view is a contrarian/consensus-defying opinion, False otherwise.")
    # 👑 [Ver 3.0] Tactical multi-dimensional signals
    sector_tilt: str = Field(
        default="",
        description="The single equity sector theme the speaker is overweight or underweight (e.g., '[[AI Infrastructure]]', '[[Energy]]', '[[Financials]]'). Wrap in double brackets or leave empty string if not discussed."
    )
    duration_call: str = Field(
        default="",
        description="The speaker's bond duration stance. Must be EXACTLY one of: 'Short', 'Neutral', 'Long', or empty string if not discussed."
    )
    macro_factor: str = Field(
        default="",
        description="The macro driver the speaker emphasizes most. Must be EXACTLY one of: 'Growth', 'Inflation', 'Liquidity', or empty string if not discussed."
    )
    # 👑 [Ver 4.4] 뷰 시간지평 — time_box(타겟기간)와 별개인 발언 호리즌
    view_time_horizon: str = Field(
        default="",
        description="The time horizon of the speaker's view. Must be EXACTLY one of: 'Days', 'Weeks', 'Months', 'Years', or empty string if not explicitly stated."
    )

class ViewDetailsSchema(BaseModel):
    core_thesis: str = Field(description="A 1-sentence summary of the main argument or thesis.")
    conditional_catalysts: list[str] = Field(description="Required conditional events or catalysts for this view to realize.")
    invalidation_risks: list[str] = Field(description="Key risks or factors that would invalidate this view.")
    verbatim_quote: str = Field(description="A direct English verbatim quote from the transcript expressing the core thesis (1-2 sentences).")
    # 👑 [Ver 4.4] 기초 정보 확충 — 구조화 수치·다수 인용·가격 목표
    key_data_points: list[dict] = Field(
        default_factory=list,
        description="Structured numeric data points the speaker cites. Each item: {indicator, value, unit, context}. E.g. {'indicator':'CPI','value':'3.2','unit':'%','context':'YoY May'}. Only numbers explicitly stated; empty list if none."
    )
    additional_quotes: list[str] = Field(
        default_factory=list,
        description="2-3 additional direct English verbatim quotes supporting the thesis (DO NOT paraphrase). Empty list if only one quote available."
    )
    price_targets: list[dict] = Field(
        default_factory=list,
        description="Explicit price targets/forecasts the speaker states. Each item: {ticker, direction, target, horizon}. E.g. {'ticker':'[[BTC]]','direction':'bearish','target':'50000','horizon':'Months'}. Only explicit targets; empty list if none."
    )

# 👑 [Ver 4.7] 4대 고가치 내러티브 필드 — 마켓 내러티브/CIO 추론 품질 극대화
class TrackingIndicator(BaseModel):
    metric: str = Field(description="모니터링 지표명 (예: Core PCE, 10Y Treasury)")
    threshold: str = Field(description="임계값 및 관전 포인트 (예: 2.8% 하회 필요)")
    implication: Optional[str] = Field(default=None, description="지표 달성 시 파급효과")

class TacticalStance(BaseModel):
    asset: str = Field(description="자산군/섹터/티커 (예: Big Tech, US 10Y, Cash)")
    stance: str = Field(description="포지셔닝 액션 (overweight, underweight, neutral, hedge 중 하나)")
    reason: Optional[str] = Field(default=None, description="포지셔닝 이유")

class MacroViewSchema(BaseModel):
    metadata: MetadataSchema
    graph_nodes: GraphNodesSchema
    quant_signals: QuantSignalsSchema
    view_details: ViewDetailsSchema

    # 👑 [Ver 4.7] 신규 4대 내러티브 필드 — 하위 호환 위해 Optional/기본값 필수
    expectation_gap: Optional[str] = Field(
        default=None,
        description="현재 시장 컨센서스(Priced-in) vs 화자 시각의 차이 1-2문장. "
                    "예: '시장 컨센서스는 연내 2회 인하 반영 중이나 화자는 동결 주장.'"
    )
    causal_chain: list[str] = Field(
        default_factory=list,
        description="인과관계 체인 (원인 -> 전달 경로 -> 최종 결과). "
                    "예: ['유가 상승($90 돌파)', '헤드라인 CPI 재반등', '연준 인하 지연', '고밸류 테크주 멀티플 축소']"
    )
    tracking_indicators: list[TrackingIndicator] = Field(
        default_factory=list,
        description="내러티브 관측을 위한 핵심 모니터링 지표 및 임계값. "
                    "예: [{metric:'미 국채 10년물 금리', threshold:'4.5% 돌파 시 위험', implication:'테크주 매도'}]"
    )
    tactical_stance: list[TacticalStance] = Field(
        default_factory=list,
        description="화자가 제안하는 구체적 포지셔닝/자산배분 변경 액션. "
                    "예: [{asset:'미국 대형 테크', stance:'underweight'}, {asset:'단기 국채', stance:'overweight'}]"
    )


SYSTEM_PROMPT = """
You are an expert financial analyst and knowledge graph builder specializing in global macroeconomics.
Your task is to analyze the provided YouTube transcript (or transcript summary) and extract structured macro views.

Follow these strict constraints:
1. Extract the primary speaker's views.
2. In 'graph_nodes':
   - 'time_box': Must be wrapped in double brackets: [[YYYY-QN]] (quarter), [[YYYY-HN]] (half-year), or [[YYYY]] (year). E.g. [[2026-H2]].
   - 'macro_themes', 'asset_classes', 'specific_tickers': Every single array item MUST be wrapped in double brackets [[ ]]. E.g. ["[[Fed QT]]", "[[Inflation]]"].
3. 'bull_bear_score': Integer from 1 (extreme bearish/recession) to 10 (extreme bullish/bubble), with 5 being neutral.
4. 'conviction_score': Integer from 1 (uncertain/low) to 10 (very strong conviction).
5. 'contrarian_flag': Boolean. Set true if the view challenges current market consensus.
6. 'verbatim_quote': Extract the exact English text representing the core thesis from the transcript. DO NOT paraphrase.
7. [Ver 3.0 TACTICAL SIGNALS] In 'quant_signals', additionally extract:
   - 'sector_tilt': The single equity sector theme the speaker is overweight or underweight. Wrap in [[ ]] (e.g. "[[AI Infrastructure]]", "[[Energy]]", "[[Financials]]", "[[Healthcare]]", "[[Consumer Discretionary]]"). If the speaker does NOT explicitly mention a sector tilt, output an empty string "".
   - 'duration_call': The bond duration stance. EXACTLY one of: "Short", "Neutral", "Long". If not discussed, output "".
   - 'macro_factor': The macro driver the speaker emphasizes most. EXACTLY one of: "Growth", "Inflation", "Liquidity". If not discussed, output "".
   - 'view_time_horizon': The time horizon of the speaker's view. EXACTLY one of: "Days", "Weeks", "Months", "Years". If not explicitly stated, output "".
   These four fields enable tactical asset allocation backtesting — extract them ONLY when the speaker explicitly opines. Never fabricate. Empty string is always acceptable.
8. 'core_thesis': A faithful 1-sentence summary of the speaker's main argument.
9. 'conditional_catalysts' & 'invalidation_risks': Extract at least one catalyst and one risk WHENEVER the speaker states conditions for the view to play out or risks that would invalidate it. Empty list is acceptable ONLY if the speaker truly states no conditions or risks — do NOT leave these empty when the speaker does mention them.
10. [Ver 4.4 EVIDENCE FIELDS] In 'view_details' and 'metadata', additionally extract:
   - 'metadata.speaker_institution': The firm/institution the speaker belongs to (e.g. "Duquesne", "Bridgewater", "JPMorgan"). Empty string if not stated.
   - 'view_details.key_data_points': Structured numeric data points the speaker cites, each as {indicator, value, unit, context}. Only numbers EXPLICITLY stated in the transcript (e.g. CPI 3.2% YoY, 10Y yield 4.5%, EPS growth 15%). Never fabricate numbers. Empty list if none.
   - 'view_details.additional_quotes': 2-3 additional direct English verbatim quotes supporting the thesis. DO NOT paraphrase. Empty list if only one quote is available.
   - 'view_details.price_targets': Explicit price targets/forecasts the speaker states, each as {ticker (wrapped [[ ]]), direction (bullish|bearish|neutral), target, horizon}. Only explicit targets — never fabricate. Empty list if none.
   These evidence fields enrich daily reports, CIO reports, insight reports, and blog articles. Extract ONLY what is explicitly stated; empty values are always acceptable.
11. [Ver 4.7 NARRATIVE FIELDS] At the TOP LEVEL of the JSON (sibling of metadata/graph_nodes/quant_signals/view_details), extract 4 high-value narrative fields:
   - 'expectation_gap': 1-2 sentences on how the speaker's view differs from current market consensus/Priced-in expectations (e.g., "market prices 2 cuts this year, speaker argues for hold"). Null/omit if not stated.
   - 'causal_chain': the macro causality chain (cause → transmission path → final outcome) as a list of strings (e.g., ["oil above $90", "headline CPI re-accelerates", "Fed delays cuts", "high-valuation tech multiple compression"]). Empty list if not stated.
   - 'tracking_indicators': list of {metric, threshold, implication} — concrete indicators/thresholds to monitor to validate/invalidate the speaker's narrative. Empty list if none.
   - 'tactical_stance': list of {asset, stance, reason} — concrete positioning/asset-allocation changes the speaker proposes (stance ∈ overweight|underweight|neutral|hedge). Empty list if none.
   Extract ONLY what is explicitly stated; empty values are always acceptable (backward compatible).
12. [GATEKEEPER / NON-MACRO DETECTION] If the transcript is a COMPANY PRODUCT PITCH, a promotional/corporate video, or contains NO macroeconomic / market / asset-allocation analysis, then output EMPTY values:
   - graph_nodes.macro_themes / asset_classes / specific_tickers = []
   - quant_signals: bull_bear_score=5, conviction_score=5, contrarian_flag=false, sector_tilt="", duration_call="", macro_factor="", view_time_horizon=""
   - view_details: core_thesis = one-line factual summary only; leave catalysts/risks/verbatim_quote/additional_quotes empty if truly absent.
   - Also empty the Ver 4.7 fields (expectation_gap=null, causal_chain=[], tracking_indicators=[], tactical_stance=[]).
   This is REQUIRED so promo/noise videos are filtered by the downstream gatekeeper.

Ensure output exactly conforms to the requested JSON schema.
Your response MUST be a valid JSON object matching the schema below:
{
  "metadata": {
    "speaker_name": "string",
    "speaker_role": "string",
    "source_channel": "string",
    "broadcast_date": "string (YYYY-MM-DD)",
    "video_id": "string (11 chars)",
    "speaker_institution": "string or empty"
  },
  "graph_nodes": {
    "time_box": "string (wrapped in [[ ]])",
    "macro_themes": ["string (each wrapped in [[ ]])"],
    "asset_classes": ["string (each wrapped in [[ ]])"],
    "specific_tickers": ["string (each wrapped in [[ ]])"]
  },
  "quant_signals": {
    "bull_bear_score": int (1-10),
    "conviction_score": int (1-10),
    "contrarian_flag": bool,
    "sector_tilt": "string (wrapped in [[ ]] or empty)",
    "duration_call": "string (Short|Neutral|Long|empty)",
    "macro_factor": "string (Growth|Inflation|Liquidity|empty)",
    "view_time_horizon": "string (Days|Weeks|Months|Years|empty)"
  },
  "view_details": {
    "core_thesis": "string",
    "conditional_catalysts": ["string"],
    "invalidation_risks": ["string"],
    "verbatim_quote": "string",
    "key_data_points": [{"indicator":"string","value":"string","unit":"string","context":"string"}],
    "additional_quotes": ["string"],
    "price_targets": [{"ticker":"string ([[ ]])","direction":"bullish|bearish|neutral","target":"string","horizon":"string"}]
  },
  "expectation_gap": "string or null",
  "causal_chain": ["string"],
  "tracking_indicators": [{"metric":"string","threshold":"string","implication":"string or null"}],
  "tactical_stance": [{"asset":"string","stance":"string (overweight|underweight|neutral|hedge)","reason":"string or null"}]
}
"""

# ---------------------------------------------------------------------------
# Post-Processor to guarantee double brackets format (무결성 보정 장치)
# ---------------------------------------------------------------------------
def _ensure_double_brackets(val: str) -> str:
    """Helper to ensure a string is enclosed in double brackets [[ ]]"""
    val = val.strip()
    if not val.startswith("[["):
        val = "[[" + val
    if not val.endswith("]]"):
        val = val + "]]"
    return val

def post_process_json(data: dict) -> dict:
    """Guarantee that all specified graph nodes contain Obsidian backlinks [[ ]]"""
    try:
        nodes = data.get("graph_nodes", {})
        if "time_box" in nodes and nodes["time_box"]:
            nodes["time_box"] = _ensure_double_brackets(nodes["time_box"])

        for key in ["macro_themes", "asset_classes", "specific_tickers"]:
            if key in nodes and isinstance(nodes[key], list):
                nodes[key] = [_ensure_double_brackets(item) for item in nodes[key] if item]

        # 👑 [Ver 3.0] sector_tilt도 [[ ]] 보정 대상 (있을 때만)
        quant = data.get("quant_signals", {})
        if isinstance(quant, dict) and quant.get("sector_tilt"):
            quant["sector_tilt"] = _ensure_double_brackets(quant["sector_tilt"])
        # 👑 [Ver 4.4] enum 필드(duration_call/macro_factor/view_time_horizon)는
        # LLM이 과잉 [[ ]] 감쌀 수 있음 → strip 해서 bare 값으로 정규화(sector_tilt와 다름).
        if isinstance(quant, dict):
            for k in ("duration_call", "macro_factor", "view_time_horizon"):
                v = quant.get(k)
                if isinstance(v, str) and v.startswith("[[") and v.endswith("]]"):
                    quant[k] = v[2:-2]

        # 👑 [Ver 4.4] price_targets 내 ticker [[ ]] 보정
        view = data.get("view_details", {})
        if isinstance(view, dict) and isinstance(view.get("price_targets"), list):
            for pt in view["price_targets"]:
                if isinstance(pt, dict) and pt.get("ticker"):
                    pt["ticker"] = _ensure_double_brackets(str(pt["ticker"]))
    except Exception as e:
        print(f"[WARN] Post-processing JSON brackets failed: {e}")
    return data

# ---------------------------------------------------------------------------
# Ver 4.1 — JSON sanitization helper
# ---------------------------------------------------------------------------
def _extract_json(text: str) -> str:
    """Sanitize LLM output to extract a clean JSON object.

    Gemma occasionally returns JSON wrapped in markdown fences (```json ... ```)
    or with a leading prose explanation. This helper strips those wrappers and
    returns the first well-formed JSON object found in the text.

    Strategy (in order):
      1. Strip leading/trailing whitespace
      2. Strip markdown fences (```json, ```)
      3. Find the FIRST '{' and the matching LAST '}' (greedy from the end)
      4. Return that substring
    """
    if not text:
        raise ValueError("Empty LLM response")

    s = text.strip()

    # Strip BOM and zero-width chars
    s = s.lstrip("\ufeff\u200b\u200c\u200d")

    # Strip markdown code fences
    fence_match = re.search(r"```(?:json)?\s*\n(.*?)\n```", s, re.DOTALL | re.IGNORECASE)
    if fence_match:
        s = fence_match.group(1).strip()
    else:
        # Strip lone leading/trailing fences
        if s.startswith("```"):
            s = s[3:]
            if s.lower().startswith("json"):
                s = s[4:]
            s = s.lstrip("\n")
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()

    # Match the first balanced { … } object (handles nested objects + braces
    # inside string literals). We do this by walking through the string,
    # tracking depth, while correctly ignoring braces inside JSON strings.
    first = s.find("{")
    if first == -1:
        raise ValueError(
            f"No JSON object start '{{' found in LLM response (first 200 chars): {s[:200]!r}"
        )

    depth = 0
    in_string = False
    escape_next = False
    end_idx = -1
    for i in range(first, len(s)):
        ch = s[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\":
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end_idx = i
                break

    if end_idx == -1:
        # Unbalanced — fall back to greedy rfind as last resort
        last = s.rfind("}")
        if last == -1 or last <= first:
            raise ValueError(
                f"Unbalanced JSON braces in LLM response (first 200 chars): {s[:200]!r}"
            )
        return s[first:last + 1]
    return s[first:end_idx + 1]


# ---------------------------------------------------------------------------
# Shared Provider Client Wrapper (Stage 2 LLM)
# ---------------------------------------------------------------------------
class LocalLLMClient:
    def __init__(self, timeout: float = 140.0):
        # Kept for constructor compatibility; provider timeouts are centrally configured.
        self.timeout = timeout
        self.model_name = DEFAULT_MODEL_NAME
        self.last_call_time = 0.0
        self.min_delay = 0.2  # Remote NIM via proxy — proxy handles rate limiting via 6-key rotation

    # -----------------------------------------------------------------------
    # Map-Reduce 3-Stage pipeline (Ver 3.1+4.0)
    # -----------------------------------------------------------------------
    def _throttle(self) -> None:
        """Enforce minimum delay between local LLM calls."""
        elapsed = time.time() - self.last_call_time
        if elapsed < self.min_delay:
            time.sleep(self.min_delay - elapsed)

    def _chat(self, system: str, user: str, response_format_json: bool = False, max_tokens: int = None, max_retries: int = 3) -> str:
        """Single provider-chain completion with centrally bounded retries.

        Returns the raw message content (str). The caller is responsible for
        JSON parsing — this allows callers to operate in TEXT mode (for chunk
        summaries) or JSON mode (for final structured extraction).

        IMPORTANT: Original gemma-4-e4b required `response_format=json_object`
        to terminate cleanly. deepseek-ai/deepseek-v4-flash on NIM does not have
        this pathology. JSON mode is used for structured extraction
        (`analyze_transcript` final call).
        """
        self._throttle()
        # 👑 [Ollama 전환] NIM → cloud_client (Ollama Cloud 우선, NIM 폴백)
        response_format = {"type": "json_object"} if response_format_json else None
        max_tok = min(max_tokens, 4096) if max_tokens is not None else 4096

        from src import cloud_client
        try:
            content = cloud_client.chat_completion(
                system=system,
                user=user,
                max_tokens=max_tok,
                temperature=0.1,
                nim_model=self.model_name,
                response_format=response_format,
                ollama_attempts=max_retries,
            )
            self.last_call_time = time.time()
            if not content.strip():
                raise RuntimeError(f"Empty completion from {self.model_name}")
            return content
        except Exception as exc:
            self.last_call_time = time.time()
            raise RuntimeError(f"Local LLM inference failed: {exc}") from exc

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------
    def analyze_transcript(
        self,
        transcript_text: str,
        video_id: str,
        source_channel: str = "Unknown_Channel",
        upload_date: str = None,
    ) -> dict:
        """Send the transcript through the shared LLM routing layer and return
        a MacroViewSchema-compatible dict.

        transcript 전체를 절삭 없이 single-shot으로 전달한다.
        """
        if not transcript_text or not transcript_text.strip():
            raise ValueError("Empty transcript_text passed to analyze_transcript")

        print(
            f"   [LLM] transcript {len(transcript_text):,} chars → full single-shot extraction"
        )
        prompt = self._build_prompt(transcript_text, video_id, source_channel, upload_date)
        raw_json = self._chat(
            system=SYSTEM_PROMPT,
            user=prompt,
            response_format_json=True,
        )

        # ── JSON parsing with _extract_json sanitization + retry ─────────
        parsed_data = self._parse_and_validate(raw_json, video_id, source_channel, upload_date, transcript_text)
        return parsed_data

    def _build_prompt(
        self,
        transcript_text: str,
        video_id: str,
        source_channel: str,
        upload_date: str | None,
    ) -> str:
        """Build the user prompt for single-shot extraction."""
        prompt = (
            f"Source YouTube Video ID: {video_id}\n"
            f"Source Channel Name: {source_channel}\n"
        )
        if upload_date:
            prompt += f"Source Upload Date: {upload_date}\n"
        prompt += f"\nTranscript text to analyze:\n{transcript_text}"
        return prompt

    def _parse_and_validate(
        self,
        raw_text: str,
        video_id: str,
        source_channel: str,
        upload_date: str | None,
        transcript_text: str | None = None,
    ) -> dict:
        """Parse the LLM's JSON output with sanitization, retry on parse failure.

        Robustness chain:
          1. Try _extract_json() to strip markdown fences / extract first {…} block
          2. Try json.loads() on the sanitized string
          3. If both fail → retry the LLM call once (re-feed original transcript)
          4. On retry success, re-parse
        """
        def recover() -> str:
            retry_prompt = (
                "Your previous response could not be parsed as JSON. "
                "Respond with ONLY a single valid JSON object — no markdown fences, "
                "no prose before or after, no code blocks. Start with '{' and end with '}'.\n\n"
            )
            retry_prompt += self._build_prompt(
                transcript_text=(
                    transcript_text if transcript_text else "(no transcript available)"
                ),
                video_id=video_id,
                source_channel=source_channel,
                upload_date=upload_date,
            )
            return self._chat(
                system=SYSTEM_PROMPT,
                user=retry_prompt,
                response_format_json=True,
            )

        return ExtractionResponseProcessor(MacroViewSchema.model_validate).process(
            raw_text,
            video_id=video_id,
            source_channel=source_channel,
            upload_date=upload_date,
            recover=recover,
        )


if __name__ == "__main__":
    print("OpenAI-compatible LocalLLMClient module loaded successfully.")
