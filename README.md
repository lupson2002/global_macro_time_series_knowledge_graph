# Global Macro Time-Series Knowledge Graph

YouTube의 거시경제·금융 전문가 발언을 수집하고 LLM으로 구조화해 SQLite, Obsidian, LanceDB에 누적하는 로컬 우선 매크로 인텔리전스 파이프라인입니다. 축적된 데이터로 일일·주간·CIO·마켓 내러티브 보고서와 블로그 원고를 생성합니다.

파생 리포트(Daily/CIO/Telegram/Insight)의 현재 입력·출력·부작용 경계와 안전한 통합
순서는 [DERIVED_PIPELINE_CONTRACTS.md](DERIVED_PIPELINE_CONTRACTS.md)를 참고하세요.
13~17단계 구현 파동의 측정 결과와 다음 진입 조건은
[REFACTORING_STABILIZATION_REPORT.md](REFACTORING_STABILIZATION_REPORT.md)에 정리했습니다.

## 실제 아키텍처

```text
YouTube RSS
  -> youtube-transcript-api (yt-dlp fallback)
  -> 전체 transcript single-shot LLM 구조화
  -> 매크로 가치 gate
  -> SQLite (source of truth)
  -> Obsidian Markdown + LanceDB derived index
  -> Daily / Insight / CIO / Narrative / Blog / Telegram
```

### LLM 호출 경로

- 일반 생성: `src/cloud_client.py`
  1. Ollama Cloud (`OLLAMA_PRO_MODEL`, 기본 `deepseek-v4-flash:0731-cloud`)
  2. 3회 실패 시 OpenAI 호환 NIM proxy (`NIM_BASE_URL`) 폴백
- 번역/고빈도 경량 작업: `src/llm_router.py`
  - Cerebras -> Groq key 1/2 -> NIM 폴백
- Tier 2/3/Insight의 기본 NIM 모델은 현재 `deepseek-ai/deepseek-v4-flash`.
- `TIER2_MODEL`, `TIER3_MODEL`, `INSIGHT_MODEL`로 오버라이드할 수 있습니다.

### Transcript 처리 정책

`LocalLLMClient.analyze_transcript()`는 transcript 길이와 관계없이 **원문 전체를 절삭 없이** single-shot으로 전달합니다. JSON parse retry에서도 원문 전체를 다시 전달합니다. 대형 입력은 provider context limit이나 timeout으로 실패할 수 있으며, 현재 정책은 내용 누락보다 해당 영상의 명시적 실패를 선택합니다.

## 핵심 데이터

`MacroViewSchema`:

- metadata: 화자, 역할, 소속, 채널, 방송일, video ID
- graph_nodes: time box, macro themes, asset classes, tickers
- quant_signals: bull/bear, conviction, contrarian, sector/duration/macro factor/time horizon
- view_details: thesis, catalysts, invalidation risks, quotes, data points, price targets
- narrative: expectation gap, causal chain, tracking indicators, tactical stance

SQLite `data/macro_knowledge.db`:

- `reports`: 영상별 메타데이터·논지·근거·내러티브
- `nodes`: macro theme, asset class, ticker
- `quant_signals`: 점수·기간·매크로 신호
- `skipped_videos`: 비매크로 영상 멱등성 스킵
- `daily_sentiment`: 일일 결정론적 심리 집계

SQLite가 정형 원본이며 Obsidian과 LanceDB는 재생성 가능한 파생 저장소입니다.

## 주요 모듈

| 파일 | 역할 |
|---|---|
| `main.py` | CLI 대상 탐색, 진행 집계, DB→Markdown backfill 진입점 |
| `src/domain.py` | 추출 dict의 read-only domain view와 공통 vector projection |
| `src/projections.py` | 재생성 가능한 LanceDB projection adapter |
| `src/pipeline.py` | 단일 영상 처리 서비스, 단계별 결과·중단·부분 저장 상태 |
| `src/ingestion.py` | YouTube RSS·transcript·yt-dlp 폴백 |
| `src/local_llm_client.py` | Pydantic 스키마, 프롬프트, JSON 복구/Validation |
| `src/llm_response.py` | provider-neutral JSON sanitize·복구·override·soft validation |
| `src/cloud_client.py` | Ollama Cloud 우선 + NIM 폴백 |
| `src/llm_router.py` | Cerebras/Groq/NIM 라우팅 |
| `src/exporter.py` | SQLite 원본 저장, Obsidian projection, DB→MD backfill |
| `src/embedder.py` | 원격/local-ST/hash 임베딩 폴백 |
| `src/lancedb_store.py` | LanceDB upsert, hybrid search, SQLite backfill |
| `src/reconciliation.py` | SQLite 기준 파생 저장소 감사·누락 복구·시점 백업 |
| `src/mcp_server.py` | SQLite 기반 read-only MCP 8 tools |
| `src/report_generator.py` | 24시간 Daily report, 번역, 근거, 심리, 이메일 |
| `src/orchestrator.py` | CIO 전략 보고서·시각화·이메일 |
| `src/insights/` | 크로스 매트릭스, 지식 그래프, RAG, timebox |
| `scripts/insight_report.py` | 주간 인사이트 보고서 |
| `scripts/insights/market_narrative.py` | RAG + 통계 기반 마켓 내러티브 |
| `src/telegram_bot.py` | SQLite 8 + LanceDB 2 tools를 쓰는 Telegram host |
| `scripts/auto_blog.py` | 원고 생성, 아카이브, 블로그 발행, 결과 이메일 |

## 채널 구성

`configs/channels.json`은 70개 채널을 정의합니다.

| Tier | 채널 | 상태 |
|---|---:|---|
| `tier_1_highest_density` | 6 | 활성, 검증됨 |
| `tier_2_macro_analysts` | 5 | 활성, 2026-08-03 교체/검증 |
| `tier_3_macro_independent` | 30 | 비활성, placeholder ID |
| `tier_4_market_news` | 29 | 비활성, placeholder/중복 ID |

## 설치

```bash
cd /home/mikey/global_macro_time_series_knowledge_graph
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Playwright 블로그 발행을 쓰려면 브라우저 설치가 추가로 필요합니다.

```bash
playwright install chromium
```

## 실행

```bash
# 특정 영상
python main.py --video_id uMMwAbYSmr4 --source CNBC_Bloomberg

# 활성 tier의 RSS 수집
python main.py --fetch_latest --max_age_hours 36

# 특정 tier
python main.py --fetch_latest --tiers tier_1_highest_density --max_age_hours 36

# SQLite -> Obsidian 복구
python main.py --backfill_from_db

# 선택적 구조화 실행 기록(JSONL)
python main.py --video_id uMMwAbYSmr4 --source CNBC_Bloomberg \
  --event_log logs/pipeline-events.jsonl

# 저장소 일관성 감사(기본 read-only; drift 발견 시 종료 코드 2)
python scripts/reconcile_storage.py

# 누락 Markdown/vector만 복구(자동 백업 + 명시적 확인 필요)
python scripts/reconcile_storage.py --apply --yes

# LanceDB가 응답하지 않을 때 Markdown 범위만 감사/복구
python scripts/reconcile_storage.py --markdown-only
python scripts/reconcile_storage.py --markdown-only --apply --yes

# Daily
python src/report_generator.py --lookback_hours 24 --event-log logs/report-events.jsonl

# CIO: package import 경로를 위해 -m 사용
python -m src.orchestrator --event-log logs/report-events.jsonl

# MCP / Telegram
python -m src.mcp_server
python -m src.telegram_bot

# 인사이트 / 마켓 내러티브
python scripts/insight_report.py --event-log logs/report-events.jsonl
python scripts/insights/run_market_narrative.py --event-log logs/report-events.jsonl
```

`--event_log`를 생략하면 추가 디렉터리나 파일을 만들지 않습니다. 지정하면 각 줄에 동일한
`run_id`로 연결된 `run.started`, `video.started`, `video.finished`, `run.finished` 이벤트를
추가합니다. 상태·단계·처리 문자 수와 집계만 기록하며 transcript, prompt, API key는
기록하지 않습니다. 이벤트 파일 쓰기가 중단되어도 기존 콘솔 출력과 파이프라인 종료 코드는
유지됩니다.

Daily, CIO, Insight, Narrative도 같은 파일에 `run.started`, `report.started`,
`report.finished`, `run.finished`를 추가할 수 있습니다. `report`에는 파이프라인 이름이,
`stage`에는 aggregation/generation/storage/delivery 중 실제 종료 지점이 기록됩니다.

## 자동화 랩퍼

- `run_frequent.sh`: 6시간 수집용
- `run_morning_report.sh`: Daily report
- `run_insight_report.sh`: 주간 Insight report
- `run_weekly_orchestrator.sh`: CIO report
- `run_market_narrative_report.sh`: Market Narrative report
- `run_auto_blog.sh`: 08:30 블로그 원고/발행
- `run_batch_backfill.sh`: chunked reprocessing

스크립트의 일정 주석은 권장 설정입니다. 실제 설치 상태는 `crontab -l`/`systemctl list-timers`로 별도 확인해야 합니다.

## 환경변수

`.env.example`을 기준으로 합니다. 모든 설정은 `src/config.py`의 immutable dataclass로 한 번만 로드·검증되며 기존 환경변수 이름은 그대로 유지됩니다.

- Ollama: `OLLAMA_PRO_API_KEY`, `OLLAMA_PRO_BASE_URL`, `OLLAMA_PRO_MODEL`
- NIM fallback: `NIM_BASE_URL`, `NIM_API_KEY`, `TIER2_MODEL`, `TIER3_MODEL`, `INSIGHT_MODEL`
- Router: `CEREBRAS_API_KEY`, `GROQ_API_KEY_1`, `GROQ_API_KEY_2`
- Embedding: `EMBEDDING_API_URL`, `EMBEDDING_MODEL`, `EMBEDDING_DIM`, optional `EMBEDDING_LOCAL_MODEL`
- YouTube: `YOUTUBE_PROXY`, `YOUTUBE_COOKIES_FILE`
- Email: `GMAIL_USER`, `GMAIL_APP_PASSWORD`, `EMAIL_TO`
- Telegram: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`

숫자 설정은 0보다 커야 하고 URL은 `http://` 또는 `https://`로 시작해야 합니다. `MCP_TRANSPORT`는 `inproc` 또는 `stdio`만 허용됩니다. 선택 provider API key가 비어 있으면 해당 provider를 즉시 건너뛰고 다음 폴백으로 진행합니다.

LLM 호출은 공통 provider 실행 계층이 재시도와 fallback을 한 번만 관리합니다. 기존 `chat_completion()`/`generate()` 문자열 API는 유지되며 결과 API에서는 실제 provider, 모델, 지연시간과 시도 이력을 확인할 수 있습니다.

### 저장소 reconciliation

`scripts/reconcile_storage.py`는 SQLite `reports`를 원본 집합으로 보고 Obsidian Markdown과 LanceDB video ID를 비교합니다. 기본 실행은 읽기 전용이며 누락 항목과 DB에 없는 고아 항목을 구분합니다. LanceDB native read가 지정 시간 안에 끝나지 않으면 감사를 중단하지 않고 vector 상태를 unavailable로 표시하며 적용을 거부합니다. `--markdown-only`로 Markdown 범위만 독립적으로 감사·복구할 수 있습니다. `--apply --yes`는 누락 항목만 생성하고 고아 항목은 삭제하지 않습니다. 누락 벡터는 한 번의 배치 임베딩과 merge transaction으로 복구합니다. 적용 전 SQLite snapshot과 LanceDB 디렉터리를 `backups/reconciliation/`에 자동 보관합니다. 복구 실행 중에는 수집 파이프라인을 중지해야 합니다.

종료 코드는 `0=일치/복구 완료`, `1=감사·복구 미완료`, `2=drift 발견`, `64=--apply 확인 부족`입니다. 일반 영상 파이프라인도 영상 또는 backfill 실패가 하나 이상이면 이제 `1`을 반환합니다.

`.env`, `cookies.txt`, 실패 스크린샷과 운영 로그에는 민감 정보가 포함될 수 있습니다.

## 리팩토링 전 안전 순서

1. 현재 미커밋 작업을 검토하고 기준 커밋으로 보존
2. SQLite·Obsidian·LanceDB 백업/재생성 절차 확인
3. 외부 API를 mock한 characterization test 추가
4. 한 번에 하나의 경계(LLM, storage, report)만 리팩토링
5. 각 단계에서 같은 입력의 DB/Markdown 결과를 비교

## 현재 제한

- 대형 transcript는 전체를 보존하므로 provider context/timeout 한계에 도달할 수 있습니다.
- SQLite·Obsidian·LanceDB에 분산 저장되지만 read-only 감사와 누락 복구 명령을 제공합니다. 고아 항목 삭제는 수동 판단이 필요합니다.
- 단일 영상 결과는 성공·스킵·실패·큐 중단 및 실패 단계로 구분됩니다. Markdown 저장이 완료되어야 SQLite 완료 마커를 기록하므로 Markdown 실패 영상은 다음 실행에서 재시도됩니다.
- 정식 회귀 테스트 커버리지가 아직 낮습니다.
- 비활성 Tier 3/4 channel ID는 운영 전 검증이 필요합니다.
- 블로그 발행은 외부 플랫폼 로그인 세션에 의존합니다.

## 테스트

리팩터링 변경 순서와 보존 계약은 [`REFACTORING_ROADMAP.md`](REFACTORING_ROADMAP.md)를
기준으로 하며, `scripts/audit_refactoring.py`로 모듈 크기와 결합도 지표를 재측정할 수 있습니다.

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q main.py src scripts publish_all_blogs.py tests
```

테스트는 임시 SQLite/Obsidian 경로와 mock provider를 사용하며 운영 DB, LanceDB, `.env`, cookies, 외부 API에 접근하지 않습니다. 세부 계약과 확장 규칙은 `TESTING.md`를 참고하세요.
