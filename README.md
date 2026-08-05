# 🌍 Global Macro Time-Series Knowledge Graph

> **YouTube 매크로 구루의 정성적 의견을 자동 수집·정형화·시계열 그래프화하는 로컬-우선 LLM 매크로 인텔리전스 파이프라인**

## 핵심 목적

유튜브 매크로/금융 채널(Wealthion, Bloomberg, CNBC, Real Vision 등)의 전문가 의견을 자동으로 수집 → 로컬 LLM으로 정형화된 매크로 thesis로 변환 → 시간축 기반 지식 그래프(SQLite + Obsidian `[[백링크]]`)로 누적. 정성(텍스트) ↔ 정량(점수)을 듀얼로 저장해 정량 백테스트와 정성 그래프 탐색을 동시에 지원.

---

## 🏗️ 실제 아키텍처 (코드 검증 완료, Ver 3.1+4.0)

```
┌──────────────────────────────────────────────────────────────────┐
│ Stage 1: Ingestion       │ Stage 2: LLM (LOCAL)      │ Stage 3: Export │
│ ───────────────────────  │ ────────────────────────  │ ──────────────  │
│ YouTube RSS ─┐           │                           │                 │
│              ├→ Transcript │ deepseek-v4-flash-instruct     │                 │
│ 6 채널 (Tier 1) ─┤           │ via nvidia-api-proxy       │                 │
│              │           │ http://localhost:8000      │                 │
│ cookies.txt ─┘  yt-dlp ↓ │ Pydantic MacroViewSchema  │                 │
│                          │         ↓                  │ SQLite (3 테이블)│
│                          │ obsidian [[ ]] 보정        │ + Obsidian MD   │
└──────────────────────────────────────────────────────────────────┘
```

### 🧠 Stage 2: Single-shot 구조화 (Ver 4.6 — 절삭 없음)

`analyze_transcript()`는 transcript **전체를 절삭 없이 single-shot** 으로 전달한다
(deepseek-ai/deepseek-v4-flash 대형 컨텍스트 — 기존 Hybrid 절삭/Map-Reduce 제거).

| 항목 | 값 |
|---|---|
| 모델 | `deepseek-ai/deepseek-v4-flash` via nvidia-api-proxy |
| 처리 | SYSTEM_PROMPT + Pydantic schema + `response_format=json_object` 1-shot |
| LLM 호출 | **1회** (transcript 길이 무관) |

추가 안전장치:
- **`_extract_json()` sanitization** — markdown 펜스/문자열 내부 중괄호/nested object 모두 방어 (brace-counter, escape 인식).
- **JSON parse retry** — 1차 실패 시 prose-free prompt로 재호출 (원본 transcript 전체 재주입).
- **max_tokens=4096 캡** (Ver 4.4 상향 — 증거 필드(key_data_points/additional_quotes/price_targets) 추가로 출력 길이 증가) — `length` 컷 방어.
- **OpenAI client timeout=140s** — NIM 지연 방어.

> **Ver 4.6 변경 (사용자 결정)**: Tier 1 모델을 `meta/llama-3.1-70b-instruct` → `deepseek-ai/deepseek-v4-flash` 로 교체. 대형 컨텍스트라 90K 분기(Hybrid 절삭) 불필요 — `_hybrid_truncate`/`_summarize_middle`/`_hybrid_extract` 및 관련 상수 제거.

### 3-Tier LLM 전략 (의도된 분리)

각 단계는 호출 빈도/실패 비용/품질 요구가 다르므로 **다른 LLM**을 씁니다:

| Tier | 위치 | 백엔드 | 호출 빈도 | 선택 이유 |
|---|---|---|---|---|
| **Tier 1 (Hot)** | `src/local_llm_client.py` (Stage 2) | `deepseek-ai/deepseek-v4-flash` via nvidia-api-proxy (`localhost:8000`, 6-key NIM rotation, pm2 daemon) | 비디오당 1회 (하루 50~200회) | NIM ~0.5s/call, proxy 자체 키 로테이션 → API key 불필요 |
| **Tier 2 (Daily)** | `src/report_generator.py` (Side A) | **NIM `deepseek-ai/deepseek-v4-flash`** via nvidia-api-proxy | **하루 1회** | 가벼움 + 한국어 강점, 입력은 정형 JSON (5-10KB) → 4096 ctx 충분 |
| **Tier 3 (Strategic)** | `src/orchestrator.py` (Side B) | **NIM `deepseek-ai/deepseek-v4-pro`** | **주 1~수회** | R1 추론 증류(강력) + 32B(가벼움), CIO 페르소나 심추론 |

> 👑 **NIM 3-Tier 통일**: Tier 2/3 를 nvidia-api-proxy(NIM)로 통일 — `google-genai` SDK 제거, Anthropic/OpenAI API 키 불필요. Tier 2: `deepseek-ai/deepseek-v4-flash`(가벼움+한국어, qwen3-next-80b-a3b 2026-07-27 EOL → 이관), Tier 3: `deepseek-ai/deepseek-v4-pro`(추론+가벼움).

### Stage 2 진실 (Tier 1)

| 항목 | 실제 구현 | 이전 오해 |
|---|---|---|
| **모델** | `deepseek-ai/deepseek-v4-flash` via nvidia-api-proxy | ❌ Gemini 2.5 Flash / gemma-4-e4b |
| **프로토콜** | OpenAI 호환 (`/v1/chat/completions`) via FastAPI proxy | ❌ `google-genai` SDK |
| **엔드포인트** | `http://localhost:8000` (nvidia-api-proxy, pm2 `nvidia-proxy`) | ❌ Google API / 192.168.0.15:8080 |
| **API Key** | `proxy-rotates-keys` (proxy가 6-key 자동 로테이션, 클라이언트 불필요) | ❌ `GEMINI_API_KEY` / `local-dummy-key` |
| **컨텍스트** | Hybrid 절삭 (≤90K single-shot, >90K head 30K + middle 30K + tail 30K) | ❌ Map-Reduce (삭제됨) / 4,096 토큰 |

→ `src/local_llm_client.py` (Ver 3.1 리네이밍)는 OpenAI 클라이언트로 nvidia-api-proxy 호출. `LocalLLMClient.__init__`는 `api_key` 파라미터 미사용. `GeminiMacroClient` back-compat alias는 삭제됨.

### 모듈 책임 (실측, Ver 3.1)

| 파일 | 역할 | LLM 백엔드 |
|---|---|---|
| `main.py` | E2E 오케스트레이터 (`--video_id` / `--fetch_latest` / `--backfill_from_db` / `--tiers`) | — |
| `src/ingestion.py` | RSS 파싱 + youtube-transcript-api + yt-dlp 폴백 | — |
| **`src/local_llm_client.py`** | **Pydantic 스키마 강제 추출 (Stage 2)** | **`deepseek-ai/deepseek-v4-flash` via nvidia-api-proxy** |
| `src/exporter.py` | SQLite (3 테이블) + Obsidian MD + DB→MD 백필 헬퍼 | — |
| `src/report_generator.py` | 24h 데이터 종합 → 한국어 일일 리포트 (Tier 2) | **NIM `deepseek-ai/deepseek-v4-flash`** |
| `src/orchestrator.py` | Grand Reasoner (CIO 페르소나 종합, Tier 3) | **NIM `deepseek-ai/deepseek-v4-pro`** |
| `src/mcp_server.py` | 8개 read-only tool (Claude/Cursor 연동) | — |
| `configs/channels.json` | 매크로 채널 풀 (Tier 1 6채널 활성, Tier 2/3/4 69채널 disabled) | — |

---

## 📊 데이터 스키마

### `MacroViewSchema` (Pydantic, Stage 2 강제)

```python
class MacroViewSchema(BaseModel):
    metadata: MetadataSchema          # speaker_*, source_channel, broadcast_date, video_id
    graph_nodes: GraphNodesSchema     # time_box, macro_themes, asset_classes, specific_tickers (모두 [[ ]])
    quant_signals: QuantSignalsSchema # bull_bear_score (1-10), conviction_score (1-10), contrarian_flag
    view_details: ViewDetailsSchema   # core_thesis, conditional_catalysts, invalidation_risks, verbatim_quote
```

`post_process_json()` 후처리로 `[[ ]]` 백링크 무결성 보정.

### SQLite 3 테이블 (`data/macro_knowledge.db`)

```sql
reports        (video_id PK, speaker_*, source_channel, broadcast_date,
                time_box, core_thesis, verbatim_quote, created_at)
nodes          (id, video_id, node_type, node_value)   -- 'macro_theme' | 'asset_class' | 'ticker'
quant_signals  (video_id PK, bull_bear_score, conviction_score, contrarian_flag)
```

**현재 상태 (2026-08-03 실측)**: reports 1,417 / nodes 7,686 / quant_signals 1,417

---

## 🗂️ Obsidian Vault 구조

```
obsidian_vault/
├── Daily_Reports/
│   ├── Daily_Macro_Synthesis_2026-06-07.md   # 일일 종합
│   └── ...
├── Ray Dalio_2023-10-26_uMMwAbYSmr4.md       # 개별 노트 (Frontmatter + [[백링크]])
├── 2026-06-{01..05}/                         # 일자별 폴더
│   └── {Speaker}_{Date}_{videoID}.md
└── reports/
    └── Grand_Report_2026-06-XX.md            # Orchestrator(Frontier LLM) 산출물
```

**Frontmatter 예시** (`obsidian_vault/Ray Dalio_2023-10-26_uMMwAbYSmr4.md`):
```yaml
---
speaker: Ray Dalio
role: Bridgewater Founder
source: CNBC_Bloomberg
date: 2023-10-26
time_box: 2026-H2
bull_bear_score: 2
conviction_score: 8
contrarian: true
tags: [macro_view, system_generated]
---
```

---

## ⚙️ 운영 특성 (코드에서 검증)

| 항목 | 값 | 근거 |
|---|---|---|
| Tier 1 백엔드 | nvidia-api-proxy (6-key NIM rotation, pm2 `nvidia-proxy`) | `src/local_llm_client.py` |
| NIM latency | ~0.5s/call (이전 gemma ~100s 대비 200x) | NIM API |
| OpenAI client timeout | 140s | `src/local_llm_client.py` |
| Rate limit | 0.2s (`min_delay`) | proxy가 6-key 자체 로테이션하므로 빈도 제한 완화 |
| Pre-Ingestion Skip | SQLite `reports.video_id` 조회 | 중복 분석 방지 |
| Post-process | `_ensure_double_brackets()` | LLM이 `[[ ]]` 누락 시 보정 |
| YouTube IP 차단 감지 | `"blocking requests from your IP"` 패턴 | 큐 조기 드롭 |
| MCP DB 모드 | `file:?mode=ro` URI 강제 | 우연한 쓰기 방지 |
| SQL 허용목 | 첫 키워드 SELECT/WITH만 허용(ATTACH/PRAGMA 거부) | MCP `run_macro_query` 방어(부분문자열 블랙리스트 false positive 해소) |

---

## 🚀 실행 진입점

```bash
# 0. 의존성 설치
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 0a. nvidia-api-proxy 선행 (Tier 1 필수)
pm2 start nvidia-proxy   # FastAPI proxy @ localhost:8000, 6-key NIM rotation

# 1. 특정 비디오 처리
python main.py --video_id uMMwAbYSmr4 --source CNBC_Bloomberg

# 2. RSS 자동 수집 (6시간 크론 — crontab: 0 */6 * * *, 36시간 윈도우)
python main.py --fetch_latest --max_age_hours 36

# 2b. 특정 tier만 (Ver 3.1)
python main.py --fetch_latest --tiers tier_1_highest_density --max_age_hours 36

# 2c. Obsidian 백필 (Ver 3.1, LLM 재호출 없이 DB→MD 일괄 export)
python main.py --backfill_from_db

# 3. 일일 매크로 종합 리포트 (한국어, NIM deepseek-v4-flash)
python src/report_generator.py --lookback_hours 24

# 4. Grand Reasoner (NIM deepseek-v4-pro 종합)
python src/orchestrator.py
# env 오버라이드: TIER2_MODEL, TIER3_MODEL (기본값 생략 가능)

# 5. MCP 서버 (Claude Desktop / Cursor 연동, stdio)
python src/mcp_server.py
```

### 환경변수 (`.env`)

```ini
# LLM 3-Tier 모두 nvidia-api-proxy(NIM) 경유 — API key 불필요 (proxy가 6-key 자동 로테이션)
# proxy 선행 실행 필수: pm2 start nvidia-proxy (또는 등록된 pm2 프로세스)
# 엔드포인트: http://localhost:8000
NIM_BASE_URL=http://localhost:8000          # 3-Tier 공용 (기본값, 생략 가능)
NIM_API_KEY=proxy-rotates-keys              # proxy 가 override, 자리표시 (생략 가능)

# Tier 2 (일일 한국어 종합, report_generator.py) — 선택 오버라이드
TIER2_MODEL=deepseek-ai/deepseek-v4-flash            # 기본값(생략 가능) — qwen3-next-80b-a3b EOL 이관

# Tier 3 (Grand Reasoner, orchestrator.py) — 선택 오버라이드
TIER3_MODEL=deepseek-ai/deepseek-v4-pro  # 기본값(생략 가능)

# YouTube
YOUTUBE_PROXY=                              # 선택
YOUTUBE_COOKIES_FILE=cookies.txt

# Email (선택)
GMAIL_USER=                                 # 리포트 이메일 발송
GMAIL_APP_PASSWORD=
```

---

## 🤖 자동화 크론 (crontab 실측, 2026-08-03)

| 스크립트 | crontab | 호출 | 로그 |
|---|---|---|---|
| `run_frequent.sh` | `0 */6 * * *` (6시간) | `main.py --fetch_latest --max_age_hours 36` | `pipeline_cron.log` (logrotate) |
| `run_morning_report.sh` | `0 7 * * *` (매일 07:00) | `src/report_generator.py` | `morning_report_cron.log` |
| `run_auto_blog.sh` | `30 8 * * *` (매일 08:30) | `scripts/auto_blog.py` (xvfb-run 헤드리스 발행) | `logs/auto_blog.log` |
| `run_insight_report.sh` | `0 5 * * 5` (금요일 05:00) | `scripts/insight_report.py` (크로스매트릭스+지식그래프+RAG+메일) | `insight_report_cron.log` |
| `run_weekly_orchestrator.sh` | `0 8 * * 1` (월요일 08:00) | `python -m src.orchestrator` (Tier 3 CIO) | `orchestrator_cron.log` |

> **nvidia-api-proxy 선행 의존**: `run_frequent.sh` / `run_insight_report.sh` / `run_weekly_orchestrator.sh` 모두 시작 시 `localhost:8000/health` 확인 → 다운이면 pm2로 자동 재시작 후 진행.

---

## 🔌 MCP 서버 (8 tools)

`src/mcp_server.py` — `fastmcp` stdio transport, read-only SQLite URI 강제.

| # | Tool | 용도 |
|---|---|---|
| 1 | `get_recent_reports` | 최신 매크로 의견 |
| 2 | `get_speaker_views` | 전문가 일관성·conviction 추세 |
| 3 | `get_contrarian_opinions` | 합의 위반 시그널 |
| 4 | `get_reports_by_timebox` | `[[2026-H2]]` 같은 시간창별 |
| 5 | `read_obsidian_report` | 노트 원문 (1M 컨텍스트) |
| 6 | `get_adjacent_nodes` | 그래프 인접 (co-occurrence weight) |
| 7 | `run_macro_query` | 커스텀 SQL (read-only) |
| 8 | `get_pipeline_status` | 파이프라인 통계 |

`configs/mcp_config.json` 템플릿을 Claude Desktop / Cursor `config.json`에 붙여넣어 사용.

---

## 📌 추적 채널 (Ver 3.1 — 외부화 + 실측 검증)

`configs/channels.json` 4-tier 구조 (Ver 3.1+ 기준):

| Tier | 채널 수 | 활성 | 검증 상태 | 설명 |
|---|---|---|---|---|
| `tier_1_highest_density` | **6** (Bloomberg_Podcasts 제거) | ✅ enabled | ✅ 100% 실측 (2026-06-09) | Wealthion, Bloomberg x2, Real Vision, CNBC, Yahoo Finance |
| `tier_2_macro_analysts` | 10 | ⛔ disabled (`_enabled: false`) | ⏳ 추정 ID (0/10 VALID, 2026-06-09) | IB·자산운용·헤지펀드 (대부분 RSS 미공개) |
| `tier_3_macro_independent` | 30 | ⛔ disabled | ⏳ 추정 ID | 독립 분석가 (Lyn Alden, Tavi Costa, Dalio …) |
| `tier_4_market_news` | 29 | ⛔ disabled | ⏳ 추정 ID | 일반 시장 뉴스 (WSJ, FT, Reuters …) |

**활성 채널 합계: 6** (Tier 1). `load_channels()` (default)는 `_enabled: true`인 tier만 반환.

**사용**:
```bash
# Tier 1만 (안전, 즉시 사용 가능) — 디폴트
python main.py --fetch_latest --tiers tier_1_highest_density

# _enabled=true인 tier 모두 (현재 = Tier 1 6채널)
python main.py --fetch_latest --tiers all

# ⚠️ main.py 에는 --include-disabled CLI 가 없음 — disabled tier 포함하려면
# scripts/validate_channels.py --include-disabled 로 검증(채널 ID 실측)만 가능.
```

### 채널 ID 검증 스크립트 (Ver 3.1 신규)

YouTube RSS (`feeds/videos.xml?channel_id=...`)로 channel_id 실측:

```bash
# Tier 1 검증 (~12s, 6채널 × 2s delay)
python scripts/validate_channels.py --tiers tier_1_highest_density

# 전체 75채널 검증 (~3min, --include-disabled 필요)
python scripts/validate_channels.py --tiers all --include-disabled --timeout 15

# 결과는 data/channel_validation.csv에 저장
```

출력: `VALID` (200+entries) / `EMPTY` (200, no recent) / `INVALID` (404) / `TIMEOUT`. Exit code 1 = 미해결 채널 존재. CSV에 `tier_enabled` 컬럼(E/d) 포함.

> ⚠️ **Tier 2~4 ID는 추정값** — 실 사용 전 `scripts/validate_channels.py --include-disabled` 실행 필수. 미확인 ID는 RSS 404 → 자동 skip되므로 파이프라인은 안전하지만 데이터 커버리지 ↓. 재활성화는 `channels.json`에서 `_enabled: true`로 변경 후 RSS 응답 확인 권장.

---

## 📜 모듈 책임 (Ver 3.1 + 4.0 통합)

| 파일 | 역할 | LLM 백엔드 |
|---|---|---|
| `main.py` | E2E 오케스트레이터 (`--video_id` / `--fetch_latest` / `--backfill_from_db` / `--tiers`) | — |
| `src/ingestion.py` | RSS 파싱 + youtube-transcript-api + yt-dlp 폴백 | — |
| **`src/local_llm_client.py`** | **Pydantic 스키마 강제 추출 (Stage 2)** | **`deepseek-ai/deepseek-v4-flash` via nvidia-api-proxy (Tier 1)** |
| `src/exporter.py` | SQLite (3 테이블) + Obsidian MD + DB→MD 백필 헬퍼 + TurboVec 인덱싱 | `embedder.py` 사용 |
| `src/embedder.py` | 임베딩 프로바이더 (remote/local/해시 폴백) — `core_thesis` → 256-dim float32 | Ollama Pro / OpenAI / 결정론적 폴백 |
| `src/turbovec_server.py` | **FastMCP 시맨틱 RAG 서버** (Ver 4.0) — `semantic_search_macro`, `get_vect_index_status` | Tier 1과 공유 임베딩 |
| `src/report_generator.py` | 24h 데이터 종합 → 한국어 일일 리포트 (Tier 2) | **NIM `deepseek-ai/deepseek-v4-flash`** (구 Gemini → qwen3-next-80b-a3b → NIM 통일) |
| `src/orchestrator.py` | Grand Reasoner (CIO 페르소나 종합, Tier 3) | **NIM `deepseek-ai/deepseek-v4-pro`** (구 Anthropic/OpenAI → NIM 통일) |
| `src/mcp_server.py` | SQLite 8 read-only tool (Claude/Cursor 연동) | — |
| `src/telegram_bot.py` | **Ver 4.0 텔레그램 마스터 에이전트** (MCP Host: SQLite 8 + TurboVec 2 tool) | Ollama Pro (`llama3.1:70b` 기본) |
| `configs/channels.json` | 매크로 채널 풀 (Tier 1 6 활성 / Tier 2/3/4 disabled) | — |
| `configs/mcp_config.json` | Claude Desktop / Cursor MCP 템플릿 | — |
| `scripts/validate_channels.py` | **Ver 3.1 신규** — channel_id 실측 검증 | YouTube RSS |

## 🔌 MCP 서버 통합 (Ver 4.0)

| MCP | Tool 수 | 사용 |
|---|---|---|
| `src/mcp_server.py` (`Macro_Wiki_Analyst`) | 8 (read-only SQLite) | Claude Desktop, Cursor |
| `src/turbovec_server.py` (`Macro_TurboVec_RAG`) | 2 (시맨틱 ANN 검색 + 인덱스 상태) | `telegram_bot.py`, Claude Desktop |

→ **총 10 tool**. Claude Desktop / Cursor는 `configs/mcp_config.json`으로 둘 다 등록 가능.

## 🤖 텔레그램 봇 (Ver 4.0)

```bash
# 실행
python src/telegram_bot.py
# 필요 env: TELEGRAM_BOT_TOKEN, OLLAMA_PRO_API_KEY, OLLAMA_PRO_BASE_URL
```

- **MCP Host**로서 `mcp_server.py` (8) + `turbovec_server.py` (2) = 10 tool 통합
- **Ollama Pro Cloud** (`OLLAMA_PRO_MODEL=llama3.1:70b` 기본)와 tool-calling 루프
- 사용자 메시지 → tool_calls → 결과 피드백 → 최종 답변 → Telegram 응답

---

## 🔭 Ver 4.3~4.5 추가 기능 (코드 실측)

### 인사이트 패키지 (`src/insights/`) — 문서 미기재였던 모듈군
| 모듈 | 역할 |
|---|---|
| `src/insights/cross_matrix.py` | 자산/테마/채널별 평균 심리(bull_bear) + 분산 + contrarian 비율 크로스 집계 |
| `src/insights/knowledge_graph.py` | nodes 공동등장 기반 networkx 가중 그래프 + 커뮤니티 + 시각화(pyvis/plotly) |
| `src/insights/rag_insights.py` | TurboVec 시맨틱 검색(`semantic_search_macro`) + NIM LLM 정성 인사이트 생성 |
| `src/insights/timebox.py` | `[[YYYY-HN]]` 유효기간 파싱/판정 (CIO·인사이트 리포트의 만료 전망 필터) |
| `src/insights/normalize.py` | 비파괴 동의어 정규화(node_value/channel/speaker) |

### Daily 리포트 기사 원고화 (Tier 2, Ver 4.3/4.5)
- **결정론적 후처리**: YAML frontmatter(date/model/source_videos/generated_at) + `## 5. 핵심 근거 & 직접 인용` + `## 6. 24시간 수집 요약` 부착. 인용/촉매/리스크/수치는 **DB 원본에서 결정론적 렌더**(LLM 재생성·환각 차단), 영문 원문은 NIM으로 한국어 번역(재생성 금지, 실패 시 원문 폴백).
- **스키마 확장 (Ver 4.4)**: `reports`에 `conditional_catalysts`, `invalidation_risks`, `key_data_points`, `additional_quotes`, `price_targets`, `speaker_institution` JSON 컬럼 + `quant_signals.view_time_horizon`. (과거 행은 NULL)
- **메일**: Gmail SMTP(`GMAIL_USER`/`GMAIL_APP_PASSWORD`)로 MD→HTML 변환(백링크 strip, 인라인 스타일) 발송.

### CIO 시각화 + 메일 (Tier 3, orchestrator)
- 리포트 후처리에서 ```json 블록 추출 → **시각화 3종**: 자산배분 파이(plotly), 자산군 심리 바(cross_matrix 재사용), 핵심 갈등 다이어그램(pyvis) → `obsidian_vault/insights/`에 HTML 저장 + **메일 본문 HTML + 시각화 HTML 첨부** 발송.

### 주간 인사이트 리포트 (`scripts/insight_report.py`)
- 크로스 매트릭스 + 지식그래프 + RAG 인사이트 취합 → 마크다운 + plotly 대시보드 → `reports/insights/insight_report_YYYY-MM-DD.md` + Gmail 발송. `--no-send`로 메일 생략 가능. crontab: 금요일 05:00.

### 블로그 자동화 (`scripts/auto_blog.py` 등)
- `scripts/generate_blog_draft.py` — 강화된 DB reports + 최신 Daily 내러티브 → 한국어 블로그 원고 `tistory_draft.md` (`--days/--theme/--top/--no-llm`).
- `scripts/auto_blog.py` — 중복방지(`data/blog_publish.db`) + 원고 생성 + 아카이브(`blog_drafts/`) + `publish_all_blogs.py`(Playwright persistent 세션, 네이버+티스토리) 자동 발행. crontab: 매일 08:30 (xvfb-run 헤드리스).

## 🔭 Ver 4.6~4.8 변경 (코드 실측)

### Ver 4.6 — Tier 1 모델 교체 + 절삭 제거
- `meta/llama-3.1-70b-instruct` → **`deepseek-ai/deepseek-v4-flash`** (`local_llm_client.py`).
- 대형 컨텍스트라 **Hybrid 절삭(90K 분기) 제거** — transcript 전체 single-shot 1회. `_hybrid_truncate`/`_summarize_middle`/`_hybrid_extract`/관련 상수/`CHUNK_SUMMARY_PROMPT` 삭제.

### Ver 4.7 — 4대 내러티브 필드 + 저장 게이트키퍼
- **4대 고가치 필드 신규 수집** (`MacroViewSchema` 최상위, 하위호환 Optional/기본값):
  - `expectation_gap` (시장 컨센서스 vs 화자 시각 차이, Optional)
  - `causal_chain` (인과관계 체인 `list[str]`)
  - `tracking_indicators` (`list[{metric, threshold, implication}]`)
  - `tactical_stance` (`list[{asset, stance, reason}]`)
  - SQLite `reports` 4컬럼 추가(ALTER, 기존 행 NULL) → RAG/일일/CIO 리포트에 노출.
- **게이트키퍼 (Non-Macro Skip)**: `main.py is_macro_relevant()` — 실제 티커/매크로 전술신호(`duration_call`/`macro_factor`/`view_time_horizon`)/비중립 점수가 모두 없으면 **홍보/소음으로 판단, DB·Obsidian·TurboVec 저장 전부 생략** + `[SKIP]` 로그. `SYSTEM_PROMPT` 지시 #12(비매크로는 빈 값 반환)와 함께 동작.

### Ver 4.8 — TurboVec → LanceDB 전면 교체
- 기존 `.tvim` TurboVec 완전 은퇴 → **임베디드 Vector DB `LanceDB`** (`data/lancedb_store/macro_vectors`).
- `src/lancedb_store.py` 신규: `upsert_document`(수집 시 upsert)/`search_hybrid`(SQL 필터+벡터 하이브리드)/`backfill_from_sqlite`(전체 백필)/`get_table_count`.
- RAG 소비 전환: `rag_insights.search_macro_sync`, `exporter.export_data`(LanceDB upsert), `market_narrative`, 텔레그램 tool → LanceDB. `src/turbovec_server.py`/`.tvim`/`macro_video_handles.json` 삭제.
- 벡터 차원은 `.env EMBEDDING_DIM`(4096, NIM nv-embed-v1) 일관 사용 (load_dotenv 최상단 강제 — 차원 불일치 방지).

### 마켓 내러티브 서치 엔진 (신규 독립 모듈)
- `scripts/insights/market_narrative.py` — **6대 RAG 쿼리**(4대 진단 + 긍정/부정 시나리오) + SQLite 정량 통계 → NIM **deepseek-v4-pro**로 "지배 내러티브 + 핵심 병목(The Market's Bottleneck)" + 3x3 시나리오(상승 3/하락 3) 추론.
- `scripts/insights/run_market_narrative.py` — 마크다운 저장(`reports/narrative/`) + Obsidian 동기화(`Narrative_Reports/`) + Gmail 발송.
- `run_market_narrative_report.sh` — nvidia-proxy 헬스체크 래퍼. **systemd timer `market-narrative.timer`** (매주 수·일 06:00 KST, `Persistent=true` — 기기 꺼짐 시 부팅 직후 누락 보정)로 자동 실행.

## ⚠️ 알려진 한계 / 개선 후보 (Ver 3.1+4.0+4.1 통합)

### ✅ 해결됨 (Ver 3.1+4.0+4.1)
- `src/local_llm_client.py` 리네이밍 완료. `GeminiMacroClient` back-compat alias 삭제, `LocalLLMClient.__init__` `api_key` 파라미터 제거
- **gemma-4-e4b → nvidia-api-proxy 마이그레이션**: `meta/llama-3.1-70b-instruct` → (Ver 4.6) `deepseek-ai/deepseek-v4-flash` via FastAPI proxy (`localhost:8000`, 6-key NIM rotation, pm2 `nvidia-proxy`). `enable_thinking=False` extra_body 제거. Map-Reduce legacy 코드 삭제. NIM ~0.5s/call (이전 gemma ~100s 대비 200x)
- 👑 **NIM 3-Tier 통일**: Tier 2(Gemini→`qwen/qwen3-next-80b-a3b`→`deepseek-ai/deepseek-v4-flash` [EOL 이관]), Tier 3(Anthropic→`deepseek-ai/deepseek-v4-pro`) 모두 nvidia-api-proxy 경유. `google-genai` SDK 제거, Anthropic/OpenAI API 키 불필요.
- Obsidian 노트 100% 백필: `python main.py --backfill_from_db` (DB ↔ MD 동기화)
- 채널 7 → 75개 풀 (Tier 1 6 활성 / Tier 2~4 69 disabled), `configs/channels.json` 외부화
- Tier 1 6개 channel_id 실측 확정 (Wealthion, Bloomberg x2, Real Vision, CNBC, Yahoo Finance)
- 신규: `scripts/validate_channels.py` (channel_id 실측 검증, `--include-disabled` 플래그), `run_batch_backfill.sh` (chunked 백필)
- **Ver 4.2 Hybrid 절삭 (Plan B)**: Map-Reduce legacy 코드 삭제. Hybrid head 30K + middle 30K + tail 30K (single-shot 임계 90K chars), `HYBRID_SUMMARY_MAX_TOKENS=800`

### ⏳ 남은 이슈
- **Tier 2~4 channel_id 69개 disabled** — `_enabled: false`로 운영 차단. 실 사용 전 `scripts/validate_channels.py --include-disabled`로 ID 검증 후 `_enabled: true`로 변경 권장
- `cookies.txt` (1.5MB) 만료 시 갱신 필요
- Bloomberg Podcasts RSS가 차단되어 Tier 1에서 제외됨 (정확한 ID 발견 시 복귀 가능)
