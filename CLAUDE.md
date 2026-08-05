# Project Context

Stack: Python (FastMCP, pydantic, openai, youtube-transcript-api, yt-dlp, python-telegram-bot, turbovec)

## LLM Backend (3-Tier 전략, 의도된 분리)

| Tier | 파이프라인 | 실제 백엔드 | 위치 |
|---|---|---|---|
| Tier 1 (Hot) | Stage 2 (구조화 추출) | **`deepseek-ai/deepseek-v4-flash` via nvidia-api-proxy** (`http://localhost:8000`, 6-key rotation, NIM API) | `src/local_llm_client.py` (Ver 3.1, 구 `gemini_client.py`) |
| Tier 1.5 (RAG) | TurboVec 시맨틱 검색 | Ollama Pro 임베딩 / 결정론적 폴백 (256-dim) | `src/embedder.py` + `src/turbovec_server.py` (Ver 4.0) |
| Tier 2 (Daily) | Morning Synthesis (24h 한국어 종합) | **NIM `deepseek-ai/deepseek-v4-flash`** via nvidia-api-proxy (가벼움 + 한국어) | `src/report_generator.py` |
| Tier 3 (Strategic) | Grand Reasoner (CIO 페르소나 종합) | **NIM `deepseek-ai/deepseek-v4-pro`** (R1 추론 증류, 32B 가벼움) | `src/orchestrator.py` |
| Tier 3.5 (Telegram) | 텔레그램 봇 tool-calling | **Ollama Pro Cloud** (`llama3.1:70b` 기본) | `src/telegram_bot.py` (Ver 4.0, MCP Host) |

> 👑 [NIM 3-Tier 통일] Tier 2/3 를 nvidia-api-proxy(NIM)로 통일 — Google Gemini/Anthropic API 키 불필요. Tier 2: deepseek-v4-flash(가벼움+한국어, qwen3-next-80b-a3b EOL 이관), Tier 3: deepseek-v4-pro(추론+가벼움). env: `NIM_BASE_URL`(기본 localhost:8000), `TIER2_MODEL`, `TIER3_MODEL`.

## Key Directories
- `src/` — Ingestion, LLM client (Ver 3.1: `local_llm_client.py`), embedder, exporter, turbovec_server, MCP server, orchestrator, report generator, telegram_bot
- `obsidian_vault/` — Obsidian 노트 (Daily_Reports + 개별 노트 + Grand reports). **백필 100% 완료 (DB ↔ MD)**
- `data/` — SQLite (`macro_knowledge.db`, reports 1,417 @2026-08-03) + `macro_vectors.tvim` (TurboVec 인덱스, Ver 4.0) + `blog_publish.db` (블로그 중복방지)
- `configs/` — MCP 템플릿 + **`channels.json`** (Tier 1 6채널 활성, Tier 2/3/4 69채널 `_enabled:false`)
- `src/insights/` — Ver 4.3+ 인사이트 패키지 (cross_matrix / knowledge_graph / rag_insights / timebox / normalize)
- `scripts/` — **`validate_channels.py`**, **`insight_report.py`**(주간 인사이트), **`generate_blog_draft.py`**, **`auto_blog.py`**(블로그 자동 발행)
- `run_frequent.sh`(6h) / `run_morning_report.sh`(07:00) / `run_auto_blog.sh`(08:30) / `run_insight_report.sh`(금 05:00) / `run_weekly_orchestrator.sh`(월 08:00) / **`run_batch_backfill.sh`** (chunked 백필)

## Key Docs
- `README.md` — 사용법, 환경변수, 실행 진입점, 3-Tier LLM 전략
- `ARCHITECTURE.md` — 시스템 아키텍처, 컴포넌트 책임, 시퀀스 다이어그램

## Ver 3.1 신규 기능
- `main.py --backfill_from_db` — DB → Obsidian MD 일괄 export (LLM 재호출 없음)
- `main.py --tiers tier_X,...` — 채널 tier 필터 (`configs/channels.json` 기반)
- `src/exporter.py _load_db_report_as_schema()` — DB row를 `MacroViewSchema` dict로 재조립하는 백필 헬퍼
- `src/local_llm_client.py` (구 `gemini_client.py`) + `LocalLLMClient` (구 `GeminiMacroClient`) 리네이밍, back-compat alias 유지
- `scripts/validate_channels.py` — YouTube RSS 기반 channel_id 실측 (User-Agent rotation + retry)
- `run_batch_backfill.sh` — chunked 백필 (tier/윈도우/청크 크기 인자화)

## Ver 4.6 Single-shot 구조화 (Tier 1 — 절삭 제거, 사용자 결정)
`local_llm_client.py::analyze_transcript()` — **Hybrid 절삭/Map-Reduce 모두 제거**, transcript 전체를 single-shot 으로 전달.

| 항목 | 값 |
|---|---|
| 모델 | `deepseek-ai/deepseek-v4-flash` via nvidia-api-proxy (구 `meta/llama-3.1-70b-instruct`) |
| 처리 | SYSTEM_PROMPT + Pydantic schema + `response_format=json_object` 1-shot |
| LLM 호출 | **1회** (transcript 길이 무관) |

- `_extract_json()` sanitization + JSON parse retry(원본 transcript 전체 재주입) + Pydantic soft validation 유지.
- 제거: `_hybrid_truncate`/`_summarize_middle`/`_hybrid_extract`, `CHUNK_TRIGGER_CHARS`/`HYBRID_*` 상수, `CHUNK_SUMMARY_PROMPT`.

## Ver 4.0 기능 (활성, README가 누락했음)
- `src/embedder.py` — 3-tier 임베딩 (remote Ollama Pro/OpenAI / local sentence-transformers / 결정론적 해시 폴백)
- `src/turbovec_server.py` — FastMCP RAG 서버 (2 tool: `semantic_search_macro`, `get_vect_index_status`)
- `src/telegram_bot.py` — MCP Host, Telegram ↔ Ollama Pro tool-calling 루프 (10 tool 통합)

## Ver 4.8 — TurboVec → LanceDB 전면 교체
- `src/lancedb_store.py` (upsert_document/search_hybrid/backfill_from_sqlite) — `data/lancedb_store/macro_vectors`, 벡터 4096-dim(`EMBEDDING_DIM`).
- RAG 소비 전환: `rag_insights.search_macro_sync`/`exporter.export_data`/`market_narrative`/telegram tool. `turbovec_server.py`·`.tvim`·`macro_video_handles.json` 삭제.

## Ver 4.7 — 4대 내러티브 필드 + 저장 게이트키퍼 + 마켓 내러티브 엔진
- **4대 필드**: `MacroViewSchema`에 `expectation_gap`/`causal_chain`/`tracking_indicators`/`tactical_stance` (하위호환 Optional/기본값). SQLite `reports` 4컬럼 + RAG/일일/CIO/인사이트 렌더에 노출.
- **게이트키퍼**: `main.py is_macro_relevant()` — 홍보/소음(티커·전술·비중립점수 全無) 영상은 DB·Obsidian·TurboVec 저장 생략 + `SYSTEM_PROMPT` 지시 #12(비매크로 빈 값 반환) 연동.
- **마켓 내러티브 엔진**: `scripts/insights/market_narrative.py` + `run_market_narrative.py` + `run_market_narrative_report.sh` — 6대 RAG 쿼리 → NIM deepseek-v4-pro 추론, systemd timer(수·일 06:00) 자동 실행.

## 리팩토링 이력 (Ver 4.2 리팩토링)

### 버그 수정 (A, Behavior-Preserved)
- **A1** `mcp_server.run_macro_query`: 부분문자열 블랙리스트(합법 SELECT 차단 + ATTACH/PRAGMA 우회) → 첫 키워드 허용목(SELECT/WITH) 재작성
- **A2** `mcp_server.read_obsidian_report`: `video_id` 11자 YouTube ID 패턴 검증(경로 순회/glob 메타 방어)
- **A3** `mcp_server.get_adjacent_nodes`: `COUNT(*)` → `COUNT(DISTINCT n1.video_id)` (weight 인플레이션 방지)
- **A4** `main.check_processed`: `with sqlite3.connect` (연결 누수 방지)
- **A5** `main.backfill_from_db`: stem 파싱 정규식 `_(\d{4}-\d{2}-\d{2})_([A-Za-z0-9_-]{11})$` (video_id 내 `_` 누락 버그 수정)
- **A6** `exporter.export_markdown`: YAML frontmatter 모든 값 quote+escape
- **A7** `exporter._load_db_report_as_schema`: try/finally 연결 close 보장
- **A8** `main --video_id`: upload_date=None 전달 (LLM 추출 방송일 사용, 실행일 덮어쓰기 방지)
- **A9** `local_llm_client._parse_and_validate`: retry 시 90K+ 단일 호출 → head+tail truncated 단축
- **A10** `local_llm_client`: Pydantic soft validation(스키마 위반 시 warning, 원본 유지)
- **A11** `_hybrid_truncate`: middle_start 반환 → 위치 메타데이터 정확화
- **A12** `_chat`: max_tokens 실제 캡(`min(max_tokens,2048)`)
- **A13** `_hybrid_truncate`: tail 연속 슬라이스(middle_end 기준, 1-char gap 제거)
- **A14** source_channel 무조건 override(broadcast_date와 일관)
- **A15** `report_generator`: 빈 응답 가드(SAFETY 차단 시 저장 방지)
- **A16** `orchestrator`: retry/backoff + 빈 text 가드 + content 파싱(o1/reasoner) + MCP 실패 warning
- **A17-A21** `embedder`/`turbovec_server`: dead conditional 제거, ST 싱글톤 캐싱, backend_name 실제 추적, public property, lock lazy
- **A22-A23** `telegram_bot`: silent fallback 경고화, TypeError no-arg 재시도 제거, 라인 경계 청킹
- **A24** `channels.json` Tier4↔Tier1 ID 중복 주석 명시
- **A25** `requirements.txt` 상한 추가(major bump 방어)
- **A26** `validate_channels.py` dead code 제거

### 고위험 동작변경 버그 (B, 사용자 결정 수정)
- **B1** `orchestrator`: 기본 `REASONER_MODEL=claude-3-7-sonnet-20250219`(3.5+thinking 무효 400 해소), thinking 블록 모델명 기반 conditional — **이후 NIM 통일(NIM 마이그레이션 섹션)으로 대체, 현재 미사용**
- **B2** `embedder`: dim 불일치 시 truncate 대신 거부+폴백(1536→256 품질 파괴 방지)
- **B3** `report_generator`: SQL `datetime('now','+9 hours')` KST 보정(24h 윈도우 KST 기준 정정)

### NIM 3-Tier 통일 (사용자 결정 — Tier 2/3 백엔드 교체)
- **Tier 2** `report_generator.py`: Google Gemini(`gemini-2.5-flash`, `google-genai` SDK) → NIM `deepseek-ai/deepseek-v4-flash`(가벼움+한국어). **2026-07-27 qwen3-next-80b-a3b EOL로 deepseek-v4-flash 이관**. OpenAI 클라이언트 재사용. `TIER2_MODEL` env 오버라이드.
- **Tier 3** `orchestrator.py`: Anthropic/OpenAI/DeepSeek 분기·thinking 블록 → NIM `deepseek-ai/deepseek-v4-pro`(DeepSeek 추론 강력, 한국어 정확). `TIER3_MODEL` env 오버라이드. (B1 thinking conditional 폐지)
- **공통 env**: `NIM_BASE_URL`(기본 localhost:8000), `NIM_API_KEY`(기본 proxy-rotates-keys). `google-genai`/Anthropic API 키 불필요.
- **모델 선정 근거**: NIM 카탈로그 실측(121 모델) — qwen2.5-7b/deepseek-r1-distill 미포함 → qwen3-next-80b-a3b(가용+한국어 정확), deepseek-v4-pro(가용+추론+한국어 정확) 선정.

### 백필 데이터 손실 (현상 유지)
- `exporter._load_db_report_as_schema`: `conditional_catalysts`/`invalidation_risks` DB 미저장 → 백필 MD 빈 항목. 스키마 확장 없이 현상 유지(사용자 결정). 백필 MD 해당 섹션은 손실.

### 👑 Ver 4.3 — Daily 보고서 근거 강화(기사 원고화) + reports 스키마 확장
- **reports 스키마 확장**: `conditional_catalysts TEXT`, `invalidation_risks TEXT` 컬럼 추가(리스트 → JSON). CREATE TABLE + ALTER 마이그레이션(기존 행은 NULL). INSERT 시 `json.dumps`, 백필 시 `_safe_json_list`로 복원 → 과거 빈 리스트 손실 현상 해소(신규 수집부터 정상).
- **Daily 보고서(Tier 2, `report_generator.py`) 강화** — 기사 원고화:
  - `format_feed_payload`: GURU VIEW 블록에 촉매/리스크/`Source URL(YouTube)` 추가(이전엔 verbatim_quote만).
  - `SYSTEM_INSTRUCTION`: "확신도" → "내러티브 강도" 라벨; Data Check "출처(화자/기관)+시점(날짜) 명시" 지시; 섹션5 자동부착 안내(LLM 섹션5 생성 금지).
  - `generate_morning_report`: 결정론적 후처리 — YAML frontmatter(date/model/source_videos/generated_at) + `## 5. 핵심 근거 & 직접 인용` 부착. 인용/촉매/리스크/링크는 **DB 원본에서 결정론적 렌더**(LLM 재생성=편파/환각 위험 차단). 선별: contrarian 전원 + conviction_score 상위 ~8건. 이메일 본문엔 frontmatter 제외(본문+섹션5).
- **비범위**: Tier 3 Grand Report 강화 안 함. 과거 행(신규 필드 추가 전) catalysts/risks는 NULL(신규 수집부터 적재). 블로그 자동 발행(publish_all_blogs.py/Tistory)은 별도.

### 👑 Ver 4.4 — 수집 스키마 강화 → 4 산출물(Daily·insight·CIO·블로그) 기초 정보 확충
**목표**: Tier 1 추출 스키마(`MacroViewSchema`)에 증거 필드를 추가해 4 산출물이 더 많은 기초 정보로 작성되도록. 인용·수치·링크는 LLM 재생성 지양, DB 원본에서 결정론적 렌더(환각 차단).

**Tier 1 스키마 확장** (`src/local_llm_client.py`):
- `MetadataSchema.speaker_institution`, `QuantSignalsSchema.view_time_horizon`(Days/Weeks/Months/Years)
- `ViewDetailsSchema`: `key_data_points: list[dict]`({indicator,value,unit,context}), `additional_quotes: list[str]`(2-3건, DO NOT paraphrase), `price_targets: list[dict]`({ticker,direction,target,horizon})
- **catalysts/risks 추출 강제** (0건 버그 수정) — SYSTEM_PROMPT에 "Extract at least one catalyst/risk when stated; empty only if truly none" 지시.
- max_tokens 캡 2048→4096 (L386, L510). CHUNK_SUMMARY_PROMPT에 수치/가격목표/타임스탬프 보존 추가. post_process_json에 price_targets ticker [[ ]] 보정.

**DB 영구화** (`src/exporter.py`): reports 4컬럼(key_data_points/additional_quotes/price_targets/speaker_institution) + quant_signals view_time_horizon. ALTER 마이그레이션 + INSERT(json.dumps) + `_load_db_report_as_schema` 복원 + export_markdown 렌더. 신규 필드는 과거 행 NULL.

**4 산출물 연동**:
- **Daily** (`report_generator.py`): fetch_past_24h_data SELECT에 quant 신규 필드, format_feed_payload에 증거 입력, _build_evidence_section에 추가 인용·수치·가격목표·소속·시간지평 렌더, Data Check에 key_data_points 활용 지시.
- **CIO** (`mcp_server.py` get_recent_reports/get_contrarian_opinions SELECT + `orchestrator.py` `_render_report_block` 헬퍼 + system_instruction 근거 기반 작성 지침).
- **Insight** (`turbovec_server.py` SELECT/ordered + `insights/rag_insights.py` `_enriched_view_line` 헬퍼 + KEY_CONCLUSIONS_PROMPT 인용/리스크/tactical 지시).
- **블로그** (`scripts/generate_blog_draft.py` 신규): DB reports + 최신 Daily 내러티브 → 결정론적 증거 블록 + NIM qwen 산문 → `tistory_draft.md`. `--days/--theme/--top/--no-llm`. [[ ]] strip. 기존 초안 .md.bak 백업. 발행은 `publish_all_blogs.py` 수동.

**비범위**: 블로그 자동 발행 cron(별도 승인), 과거 634건 신규 필드 백필(신규 수집부터), cross_matrix tactical 매트릭스(stretch).

<!-- Auto-generated by IJFW from repo scan. Edit freely -- IJFW only touches the managed block below. -->

<!-- IJFW-MEMORY-START (managed -- do not edit manually) -->
<ijfw-memory>
Project memory at .ijfw/memory/. Call `ijfw_memory_prelude` for full context.
</ijfw-memory>
<!-- IJFW-MEMORY-END -->
