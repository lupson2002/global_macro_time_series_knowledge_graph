# ARCHITECTURE

> 코드 검증 완료 — `src/*.py` 직접 분석 기반

## 1. 시스템 개요 (System Overview)

본 프로젝트는 **3-Stage E2E 파이프라인 + 2개 사이드 파이프라인**으로 구성된다. 핵심 Stage 2는 **`nvidia-api-proxy`(FastAPI, 6-key rotation, pm2 daemon `nvidia-proxy`)를 경유한 `deepseek-ai/deepseek-v4-flash` (NVIDIA NIM API)**를 사용한다.

```
┌────────────────────────────────────────────────────────────────────┐
│                         INGESTION (Stage 1)                        │
│  ┌──────────────┐  ┌────────────────────┐  ┌────────────────────┐ │
│  │ YouTube RSS  │→ │ youtube-transcript │→ │ yt-dlp fallback    │ │
│  │ (7 채널)     │  │ -api + cookies.txt │  │ (WebVTT 파싱)      │ │
│  └──────────────┘  └────────────────────┘  └────────────────────┘ │
│         ↓ (video_id, pub_date)                                    │
│      Check SQLite ─[exists?]→ SKIP (Pre-Ingestion Optimization)   │
└────────────────────────────────────────────────────────────────────┘
                                ↓ transcript text
┌────────────────────────────────────────────────────────────────────┐
│              LLM EXTRACTION (Stage 2) — ★ NVIDIA NIM PROXY ★       │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │ http://localhost:8000   (nvidia-api-proxy, FastAPI)         │  │
│  │ Model: deepseek-ai/deepseek-v4-flash (NVIDIA NIM API)         │  │
│  │ Context: 128K tokens → transcript 90,000 chars trigger      │  │
│  │ response_format: {"type": "json_object"}                    │  │
│  │ Output: MacroViewSchema (Pydantic 강제)                     │  │
│  │   ├ metadata       (speaker, role, channel, date, video_id)  │  │
│  │   ├ graph_nodes    (time_box, themes, assets, tickers [[ ]]) │  │
│  │   ├ quant_signals  (bull_bear, conviction, contrarian)      │  │
│  │   └ view_details   (thesis, catalysts, risks, quote)        │  │
│  │                                                              │  │
│  │ post_process_json() → [[ ]] 무결성 보정                      │  │
│  └──────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────┘
                                ↓ structured JSON
┌────────────────────────────────────────────────────────────────────┐
│                    DUAL EXPORT (Stage 3)                           │
│  ┌─────────────────────────┐  ┌──────────────────────────────────┐ │
│  │ SQLite (3 테이블)        │  │ Obsidian MD (백링크 노트)         │ │
│  │ ├ reports               │  │ obsidian_vault/                   │ │
│  │ ├ nodes (FK→reports)    │  │  └ {Speaker}_{Date}_{videoID}.md │ │
│  │ └ quant_signals         │  │   Frontmatter: speaker/role/      │ │
│  │   (FK→reports)          │  │   score/time_box/tags             │ │
│  └─────────────────────────┘  │   Body: [[백링크]] 사용            │ │
│                                └──────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────┐
│         SIDE PIPELINE A: Morning Synthesis (24h 종합)              │
│  fetch_past_24h_data(DB) → NIM deepseek-ai/deepseek-v4-flash → MD  │
│  (구 Google Gemini → qwen3-next-80b-a3b → NIM 통일, google-genai 제거)│
├────────────────────────────────────────────────────────────────────┤
│         SIDE PIPELINE B: Grand Reasoner (NIM 추론 모델)            │
│  MCP 집계 → NIM deepseek-v4-pro (R1 추론 증류, 32B)  │
│  → obsidian_vault/reports/Grand_Report_{YYYY-MM-DD}.md            │
└────────────────────────────────────────────────────────────────────┘
```

---

## 2. 컴포넌트 책임 (Component Responsibilities)

### 2.1 `main.py` — E2E 오케스트레이터
- CLI: `--video_id`, `--channel_id`, `--fetch_latest`, `--max_age_hours`, `--vault_dir`, `--db_path`, `--source`, `--overwrite`, `--ingest_delay`, `--llm_delay`, `--max_videos`, `--backfill_from_db`, `--tiers`
- 채널 로드: `configs/channels.json` (Tier 1 6채널 활성 / Tier 2~4 69 disabled) — `load_channels()`가 `_enabled` 필터, `DEFAULT_CHANNELS`(7개)는 파일 누락 시 폴백
- `check_processed(db, video_id)` — Pre-Ingestion Skip
- 호출 흐름: Ingestion → LLM → Dual Export (지연은 `--ingest_delay`/`--llm_delay` 인자, 기본 0s)
- IP 차단 패턴 감지 시 큐 조기 중단

### 2.2 `src/ingestion.py` — YouTube 데이터 수집
- `get_youtube_transcript(video_id)`:
  1. `youtube-transcript-api` 우선 시도 (`en`, `ko` → default)
  2. 실패 시 `yt-dlp` 폴백 (WebVTT 파싱, `ejs:github` remote components)
- `fetch_video_ids_from_channel(channel_id, max_age_hours)`:
  - YouTube RSS (`feeds/videos.xml?channel_id=...`) Atom XML 파싱
  - `max_age_hours` 필터 (UTC 기준)
- 옵션: `YOUTUBE_PROXY`, `YOUTUBE_COOKIES_FILE` (MozillaCookieJar)

### 2.3 `src/local_llm_client.py` — LLM 추출 ★ 핵심
> **구 `gemini_client.py` / `GeminiMacroClient` (back-compat alias는 Ver 4.1에서 삭제)**

```python
DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_MODEL_NAME = "deepseek-ai/deepseek-v4-flash"
DUMMY_API_KEY = "proxy-rotates-keys"

class LocalLLMClient:
    def __init__(self):
        self.client = OpenAI(base_url=DEFAULT_BASE_URL, api_key=DUMMY_API_KEY, timeout=140.0)
        self.model_name = DEFAULT_MODEL_NAME
        self.min_delay = 0.2    # NIM ~0.5s, proxy REQUEST_TIMEOUT=120s, client 140s 헤드룸
```

- **Proxy**: nvidia-api-proxy (FastAPI, port 8000, pm2 daemon `nvidia-proxy`) — 6개 NVIDIA NIM API 키 로테이션, Authorization 헤더 override. `/health` 엔드포인트는 catch-all 라우트上方 배치.
- **Safeguard**: Hybrid 절삭 — 90,000자 트리거 시 head/middle/tail 30,000자씩 슬라이스 (128K ctx 대응)
- **Schema 강제**: `response_format={"type": "json_object"}` + Pydantic `MacroViewSchema` 후 검증
- **Retry**: 3회, 지수 백오프 (2.0×)
- **`_summarize_middle`**: JSON envelope 제거, 평문 직접 (`response_format_json=False`)
- **`_parse_and_validate`**: `transcript_text` 파라미터로 원본 transcript 재주입 (JSON parse retry 시 컨텍스트 유실 방지)
- **Post-process**: `post_process_json()` — LLM이 `[[ ]]` 누락 시 보정
- **Override**: `video_id`, `source_channel`, `broadcast_date`(=upload_date) 강제 덮어쓰기

### 2.4 `src/exporter.py` — 듀얼 익스포트
- **`SQLiteExporter`**: 3 테이블 스키마 생성 (`reports`, `nodes`, `quant_signals`), `INSERT OR REPLACE` + 노드 테이블 DELETE-INSERT (clean update)
- **`ObsidianMDExporter`**: 파일명 `{Speaker}_{Date}_{videoID}.md`, 일자별 폴더 분리, YAML Frontmatter + 마크다운 본문
- 인덱스: `idx_nodes_video`, `idx_reports_speaker`, `idx_reports_date`

### 2.5 `src/report_generator.py` — 일일 종합 (한국어)
- `fetch_past_24h_data()` — `reports.created_at >= datetime('now','+9 hours','-24 hours')` LEFT JOIN `quant_signals` + 노드 조회. 👑 [B3] KST 보정.
- `format_feed_payload()` — LLM 입력 직렬화
- 👑 [NIM 통일] OpenAI 클라이언트 → nvidia-api-proxy. 모델 `deepseek-ai/deepseek-v4-flash`(가벼움+한국어, qwen3-next-80b-a3b EOL 이관). `TIER2_MODEL` env 오버라이드. retry/backoff, 빈 응답 가드. (구 `google-genai`/Gemini 제거)
- 👑 [Ver 4.3/4.5] 결정론적 후처리 — YAML frontmatter + 섹션5(핵심 근거 & 직접 인용)/섹션6(24h 수집 요약) DB 원본 렌더 + NIM 한국어 번역(재생성 금지). Gmail SMTP로 MD→HTML 메일 발송.
- 출력: `obsidian_vault/Daily_Reports/Daily_Macro_Synthesis_{YYYY-MM-DD}.md`
- 부가: Gmail SMTP 발송 (선택)

### 2.6 `src/orchestrator.py` — Grand Reasoner (NIM)
- `aggregate_macro_context()`: MCP 도구 비동기 호출 → 컨텍스트 문자열 (MCP 실패 시 warning + 빈 기본값)
- `query_reasoner_llm()`: 👑 [NIM 통일] **OpenAI SDK 동기 클라이언트**(`OpenAI(base_url=NIM_BASE_URL)`, `asyncio.to_thread` 비동기화) → nvidia-api-proxy. 모델 `deepseek-ai/deepseek-v4-pro`(R1 추론 증류, 32B 가벼움). `TIER3_MODEL` env 오버라이드. retry/backoff, null content 가드, 빈 text 가드. (구 httpx/Anthropic/OpenAI 분기·thinking 블록 폐지)
- `save_report_to_vault()`: `obsidian_vault/reports/Grand_Report_{YYYY-MM-DD}.md` (frontmatter model=TIER3_MODEL, provider=nim) — ```json 블록은 마크다운 표로 치환
- 👑 [Ver 4.4] 시각화 3종(자산배분 파이/자산심리 바/핵심갈등 pyvis) → `obsidian_vault/insights/` + **CIO 메일 본문 HTML + 시각화 HTML 첨부** 발송. `aggregate_macro_context`는 timebox 유효 판정(`insights.timebox.is_valid_time_box`)으로 만료 전망 필터 + 컨텍스트 200K chars 캡.
- 명시: NIM proxy 는 원격 API (로컬 Ollama 추론 아님)

### 2.7 `src/mcp_server.py` — MCP 서버 (8 tools)
- `FastMCP("Macro_Wiki_Analyst")`, stdio transport
- `query_db_async()` — `file:?mode=ro` URI로 read-only 강제, aiosqlite 비동기
- **8개 도구** (전부 read-only):
  1. `get_recent_reports(limit=None)`
  2. `get_speaker_views(speaker_name)` — LIKE 검색
  3. `get_contrarian_opinions(limit=None)` — `quant_signals.contrarian_flag=1`
  4. `get_reports_by_timebox(time_box)` — `[[...]]` 자동 wrap
  5. `read_obsidian_report(video_id, read_all=True, max_chars=1000000)` — aiofiles, 11자 YouTube ID 패턴 검증(경로 순회 방어), 트렁케이션 안전장치
  6. `get_adjacent_nodes(node_value)` — `nodes` 테이블 self-JOIN, weight(빈도) 정렬
  7. `run_macro_query(sql_query)` — **허용목 기반 검증**: 첫 키워드가 SELECT/WITH 인 경우만 실행(주석 제거 후 판정). ATTACH/PRAGMA/INSERT/UPDATE/DELETE 등 거부. mode=ro URI 가 1차 쓰기 차단
  8. `get_pipeline_status()` — DB 통계

---

## 3. 데이터 플로우 시퀀스 (Sequence)

### 3.1 단일 비디오 처리
```
main.py: argparse(video_id)
  ↓ check_processed() → [기존] SKIP
  ↓ get_youtube_transcript() (지연은 --ingest_delay, 기본 0s)
       ├─ youtube-transcript-api (en/ko → default)
       └─ yt-dlp fallback (WebVTT)
  ↓ LocalLLMClient.analyze_transcript()
       ├─ 90,000자 트리거 시 Hybrid 절삭 (head/middle/tail 30K, 128K ctx)
       ├─ OpenAI client.chat.completions.create() → localhost:8000 (nvidia-api-proxy)
       ├─ 3회 retry, exp backoff
       └─ post_process_json() [[ ]] 보정
  ↓ SQLiteExporter.export_data() → INSERT OR REPLACE
  ↓ ObsidianMDExporter.export_markdown() → MD 파일 저장
```

### 3.2 RSS 자동 수집 (6시간 크론 — crontab: 0 */6 * * *)
```
run_frequent.sh → main.py --fetch_latest --max_age_hours 36
  → fetch_video_ids_from_channel(Tier 1 6채널, configs/channels.json)
  → 각 비디오에 대해 3.1 시퀀스
```

### 3.3 Grand Reasoner 종합 (NIM deepseek-v4-pro)
```
orchestrator.py
  → mcp_server.get_pipeline_status() / get_recent_reports() / get_contrarian_opinions() (asyncio.gather 병렬)
  → timebox 유효 판정 필터 → OpenAI SDK 동기 클라이언트(asyncio.to_thread) → NIM deepseek-v4-pro
       └─ thinking 블록 불필요(DeepSeek 자체 추론), max_tokens=8192
  → obsidian_vault/reports/Grand_Report_{date}.md + 시각화 3종 + CIO 메일(HTML+첨부)
```

---

## 4. 스키마 진화 (Schema Constraints)

### `MacroViewSchema` (Pydantic v2, Stage 2)
```python
class MacroViewSchema(BaseModel):
    metadata: MetadataSchema          # speaker_*, source_channel, broadcast_date, video_id
    graph_nodes: GraphNodesSchema     # time_box, macro_themes[], asset_classes[], specific_tickers[]
    quant_signals: QuantSignalsSchema # bull_bear_score (1-10), conviction_score (1-10), contrarian_flag
    view_details: ViewDetailsSchema   # core_thesis, conditional_catalysts[], invalidation_risks[], verbatim_quote
```

**무결성 보장 두 단계**:
1. **LLM 단계**: SYSTEM_PROMPT에 `[[ ]]` 규칙 명시 + `response_format=json_object`
2. **후처리**: `_ensure_double_brackets()` — 모델이 누락 시 자동 보정

---

## 5. 운영 / Rate Limit 매트릭스

| 자원 | 제약 | 위치 | 완화 전략 |
|---|---|---|---|
| deepseek-v4-flash ctx | 대형 (128K+) | `local_llm_client.py` | 절삭 없음 — transcript 전체 single-shot (Ver 4.6) |
| NVIDIA NIM 지연 | ~0.5s/호출 (deepseek-v4-flash) | `nvidia-api-proxy` | 6-key rotation; llama-3.3-70b 회피 (129s 큐 지연) |
| OpenAI client timeout | 140s | `local_llm_client.py` | proxy REQUEST_TIMEOUT=120s + 헤드룸 |
| YouTube IP 차단 | 회당 차단 누적 | `main.py:184` | 패턴 감지 → 큐 조기 중단 |
| YouTube 호출 간 | `--ingest_delay` 기본 0s | `main.py` | 백필 청크는 `--ingest_delay 3` |
| LLM 호출 간 | 1.0s | `local_llm_client.py` | — |
| Pre-Ingestion Skip | — | `main.py:159` | SQLite `reports.video_id` 조회 |
| MCP DB | read-only | `mcp_server.py:33` | `file:?mode=ro` URI |
| MCP SQL | 키워드 차단 | `mcp_server.py:251` | 8개 mutation 키워드 |

---

## 6. 디렉터리 트리 (실측, Ver 3.1+4.0)

```
global_macro_time_series_knowledge_graph/
├── main.py                          # E2E 오케스트레이터 (--video_id/--fetch_latest/--backfill_from_db/--tiers)
├── configs/
│   ├── mcp_config.json              # Claude Desktop / Cursor 템플릿
│   └── channels.json                # 매크로 채널 풀 (Tier 1 6 활성 / Tier 2~4 69 disabled)
├── scripts/
│   └── validate_channels.py         # channel_id 실측 검증 (User-Agent rotation + retry)
├── src/
│   ├── ingestion.py                 # YouTube RSS + transcript + yt-dlp
│   ├── local_llm_client.py          # ★ nvidia-api-proxy 경유 deepseek-v4-flash-instruct 클라이언트 (Ver 4.1, 구 gemini_client.py)
│   ├── exporter.py                  # SQLiteExporter + ObsidianMDExporter + TurboVec 인덱싱 + DB→MD 백필 헬퍼
│   ├── embedder.py                  # Ver 4.0 임베딩 (remote/local/해시 폴백, 256-dim)
│   ├── turbovec_server.py           # Ver 4.0 FastMCP RAG 서버 (2 tool: semantic_search_macro, get_vect_index_status)
│   ├── report_generator.py          # Tier 2 일일 종합 (NIM deepseek-ai/deepseek-v4-flash)
│   ├── orchestrator.py              # Tier 3 Grand Reasoner (NIM deepseek-ai/deepseek-v4-pro)
│   ├── mcp_server.py                # 8 tool MCP 서버 (SQLite, read-only)
│   ├── telegram_bot.py              # Ver 4.0 텔레그램 MCP Host (10 tool 통합, Ollama Pro)
│   ├── insights/                    # Ver 4.3+ 인사이트 패키지 (cross_matrix/knowledge_graph/rag_insights/timebox/normalize)
│   └── embedder.py                  # Ver 4.0 임베딩 프로바이더 (remote/local/해시 폴백)
├── data/
│   ├── macro_knowledge.db           # SQLite (reports 1,417 @2026-08-03)
│   ├── macro_vectors.tvim           # Ver 4.0 TurboVec 인덱스
│   ├── macro_video_handles.json     # Ver 4.0 video_id → u64 handle map
│   ├── channel_validation.csv       # channel_id 실측 결과
│   └── blog_publish.db              # 블로그 자동 발행 중복 방지
├── obsidian_vault/                  # Obsidian 노트 (백필 100%, DB ↔ MD)
│   ├── Daily_Reports/               # 일일 종합 MD (섹션5/6 결정론적 부록 포함)
│   ├── {Speaker}_{Date}_{videoID}.md # 개별 노트
│   ├── 2026-06-{01..05}/            # 일자별 폴더
│   ├── reports/                     # Grand Reasoner 산출물
│   └── insights/                    # CIO 시각화 HTML (파이/심리바/갈등)
├── .venv/                           # python venv
├── cookies.txt                      # YouTube 인증 (1.5MB)
├── .env                             # 환경변수
├── requirements.txt
├── run_frequent.sh                  # 6시간 크론 (RSS 수집, nvidia-proxy 자동 재시작)
├── run_morning_report.sh            # 일일 크론 (종합, 07:00)
├── run_auto_blog.sh                 # 일일 크론 (블로그 자동 발행, 08:30, xvfb-run)
├── run_insight_report.sh            # 주간 크론 (인사이트 리포트, 금 05:00)
├── run_weekly_orchestrator.sh       # 주간 크론 (CIO Grand Report, 월 08:00)
├── run_batch_backfill.sh            # Ver 3.1 chunked 백필 (tier/윈도우/청크 인자화)
├── global_macro_pipeline.logrotate
├── pipeline_cron.log
├── morning_report_cron.log
├── test_cookies.py
└── CLAUDE.md                        # IJFW 메모리 인덱스
```

---

## 6.5 Ver 4.6~4.8 변경 (실측)

- **Ver 4.8**: TurboVec → **LanceDB 전면 교체** — `src/lancedb_store.py`(upsert_document/search_hybrid/backfill_from_sqlite), RAG 소비자(rag_insights·exporter·market_narrative·telegram) 전환, `turbovec_server.py`/`.tvim` 삭제. 벡터 차원 `.env EMBEDDING_DIM`(4096) 일관.
- **Ver 4.6**: Tier 1 모델 `deepseek-ai/deepseek-v4-flash` 교체 + **Hybrid 절삭 제거** (transcript 전체 single-shot).
- **Ver 4.7 — 4대 내러티브 필드**: `MacroViewSchema`에 `expectation_gap`/`causal_chain`/`tracking_indicators`/`tactical_stance` 추가(하위호환 Optional/기본값) → SQLite `reports` 4컬럼 + RAG(`turbovec_server` SELECT)·일일(`format_feed_payload`)·CIO(`_render_report_block`)·인사이트(`_enriched_view_line`) 노출.
- **Ver 4.7 — 저장 게이트키퍼**: `main.py is_macro_relevant()` — 티커/매크로 전술신호/비중립 점수 全無 시 홍보/소음으로 판단, DB·Obsidian·TurboVec 저장 생략. `SYSTEM_PROMPT` 지시 #12와 연동.
- **마켓 내러티브 엔진**: `scripts/insights/market_narrative.py`(6대 RAG 쿼리 + NIM deepseek-v4-pro) + `run_market_narrative.py`(저장/Obsidian/메일) + `run_market_narrative_report.sh`(시스템드 타이머 수·일 06:00).

## 7. 알려진 코드 불일치 (Ver 3.1 — 모두 해결)

| 파일 | 라인 | 문제 | Ver 3.1 상태 |
|---|---|---|---|
| `src/gemini_client.py` | 1 | `gemini_client.py`로 명명, 클래스명 `GeminiMacroClient` | ✅ **리네이밍**: `local_llm_client.py` / `LocalLLMClient`. back-compat alias는 Ver 4.1에서 삭제 |
| `src/report_generator.py` | 20-22 | `from google import genai` import 후 미사용 (dead) | ✅ **오해 정정**: 실제로 Tier 2 정상 호출. 이후 NIM 통일로 `deepseek-ai/deepseek-v4-flash` 사용 |
| `main.py:36` | DEFAULT_CHANNELS | Gemini 20 RPD 제약 기반 우선순위 | ✅ **외부화**: `configs/channels.json` (Tier 1 6 활성 / Tier 2~4 69 disabled) + `--tiers`/`--include-disabled` 필터 |
| `requirements.txt` | 1 | `google-genai>=0.1.0` | ✅ **Tier 2용으로 정당화** (Tier 1과 의도적 분리) |
| `main.py:38` | 우선순위 코멘트 | "20 RPD free tier" | ✅ **갱신**: Ver 4.1 nvidia-api-proxy (6-key rotation, RPD-free) |

### ⏳ 남은 한계 (Ver 3.1)
- **Tier 2~4 channel_id 69개 미검증** — 추정 ID. 실 사용 전 `scripts/validate_channels.py` 실행
- `cookies.txt` (1.5MB) 만료 시 갱신 필요
- Bloomberg Podcasts RSS가 차단되어 Tier 1에서 제외 (정확한 ID 발견 시 복귀)

---

## 8. 확장 포인트 (Ver 3.1+4.0)

- **NVIDIA NIM 모델 교체**: `DEFAULT_BASE_URL`/`DEFAULT_MODEL_NAME`만 변경 → nvidia-api-proxy 경유 다른 NIM 모델 즉시 사용 (llama-3.3-70b-instruct는 129s 큐 지연 주의)
- **MCP 클라이언트**: `configs/mcp_config.json`을 Claude Desktop / Cursor에 붙여넣기 (10 tool 통합 가능: SQLite 8 + TurboVec 2)
- **NIM 모델 교체 (Tier 2/3)**: `TIER2_MODEL`/`TIER3_MODEL` env 오버라이드로 nvidia-api-proxy 경유 NIM 모델 즉시 교체 (구 `REASONER_MODEL`/`REASONER_PROVIDER` env는 NIM 통일로 폐지)
- **백필 파이프라인**: `python main.py --backfill_from_db` (DB→MD) 또는 `./run_batch_backfill.sh 30d all 100` (RSS→전체 파이프라인)
- **시맨틱 검색**: `src/turbovec_server.py` (Ver 4.0) — `semantic_search_macro(query, top_k=5)`로 RAG
- **텔레그램 인터페이스**: `src/telegram_bot.py` (Ver 4.0) — Telegram ↔ 10 tool 통합, Ollama Pro tool-calling 루프
