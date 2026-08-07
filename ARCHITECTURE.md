# Architecture

> 2026-08-07 현재 코드 기준. 과거 버전 이력이 아닌 현재 실행 구조를 기술한다.

## 1. System boundary

본 프로젝트는 단일 Python 저장소의 배치/CLI 파이프라인이다. YouTube, LLM provider, embedding provider, SMTP, Telegram, 블로그 플랫폼은 외부 시스템이다.

```text
Discovery -> Ingestion -> Analysis -> Relevance gate -> Persistence -> Derived products
```

## 2. Core ingestion sequence

1. `main.py` 가 `configs/channels.json`의 활성 tier를 로드한다.
2. `src.ingestion.fetch_video_ids_from_channel()`이 RSS에서 `(video_id, pub_date)`를 얻는다.
3. `src.pipeline.PipelineService`가 단일 영상의 precheck부터 저장까지 조정하며 `check_processed()`로 SQLite `reports`/`skipped_videos`를 조회한다.
4. `get_youtube_transcript()`가 youtube-transcript-api를 시도하고 yt-dlp로 폴백한다.
5. `LocalLLMClient.analyze_transcript()`가 원문 전체를 절삭 없이 single-shot으로 전달한다.
6. JSON을 sanitize/parse하고 Pydantic soft validation과 Obsidian backlink 보정을 적용한다.
7. `is_macro_relevant()`가 티커, 전술 신호, 비중립 점수를 기준으로 저장 여부를 결정한다.
8. Obsidian Markdown을 먼저 기록하고 SQLite를 완료 마커로 저장한다. Markdown 실패 시 DB에 완료 표시를 남기지 않아 다음 실행에서 재시도할 수 있다.

## 3. LLM architecture

런타임 설정은 `src/config.py`가 `.env`를 로드해 `Settings`/`LLMSettings`/`EmbeddingSettings`/`EmailSettings`/`TelegramSettings`로 검증한다. 소비 모듈은 직접 `os.environ`을 읽지 않고 공통 `settings`를 사용한다. 기존 모듈 상수는 호환성을 위한 alias로 유지한다.

`src/llm_providers.py`가 provider-neutral 실행, 빈 응답 판정, 재시도, 순차 failover와 시도 메타데이터를 담당한다. `cloud_client.chat_completion()`과 `Llama70BRouter.generate()`는 기존 문자열 API를 유지하며, 각각의 `*_result()` API는 성공 provider·모델·지연시간·전체 시도 이력을 반환한다.

### General generation

`src/cloud_client.py::chat_completion`

```text
Ollama Cloud (up to 3 attempts)
  -> NIM OpenAI-compatible fallback
  -> raise RuntimeError if both fail
```

- Ollama에서 reasoning 토큰이 content 예산을 소진하지 않도록 `think=False`를 사용한다.
- Ollama/OpenAI client는 lazy singleton으로 재사용한다.
- `LocalLLMClient._chat()`도 이 공통 client를 사용한다.

### Translation/high-frequency generation

`src/llm_router.py::Llama70BRouter`

```text
configured Cerebras/Groq providers (round-robin/failover)
  -> NIM fallback
```

### Current defaults

| Purpose | Default |
|---|---|
| Ollama primary | `deepseek-v4-flash:0731-cloud` |
| NIM fallback | `deepseek-ai/deepseek-v4-flash` |
| Tier 2 Daily | `deepseek-ai/deepseek-v4-flash` |
| Tier 3 CIO | `deepseek-ai/deepseek-v4-flash` |
| Insight | `TIER3_MODEL` or `deepseek-ai/deepseek-v4-flash` |

`.env`가 기본값을 오버라이드할 수 있다.

## 4. Data ownership

### SQLite: source of truth

- `reports`: 메타데이터, thesis, quotes, evidence, narrative fields
- `nodes`: video-node 관계
- `quant_signals`: 정량 신호
- `skipped_videos`: 분석했지만 저장하지 않은 video ID
- `daily_sentiment`: 일일 집계

`SQLiteExporter` 초기화 시 CREATE/ALTER 마이그레이션이 수행된다.

### Obsidian: human-readable projection

`ObsidianMDExporter`가 YAML frontmatter와 `[[...]]` backlink를 가진 Markdown을 생성한다. `main.py --backfill_from_db`로 SQLite에서 재생성할 수 있다.

### LanceDB: search projection

`src/lancedb_store.py`가 `data/lancedb_store/macro_vectors`를 관리한다.

- `upsert_document()`
- `search_hybrid()`
- `backfill_from_sqlite()`
- `hydrate_views()`
- `semantic_search_macro()`
- `get_vect_index_status()`

TurboVec server와 `.tvim` 인덱스는 현재 아키텍처에 존재하지 않는다.

### Reconciliation

`src.reconciliation`은 SQLite report ID를 기준 집합으로 삼아 Markdown/LanceDB의 누락과 고아 ID를 계산한다. `scripts/reconcile_storage.py`는 기본 read-only이며 `--apply --yes`일 때만 누락 projection을 생성한다. LanceDB ID 감사는 별도 프로세스와 시간 제한으로 격리되며 timeout 시 unavailable로 표시하고 전체 적용을 거부한다. `--markdown-only`는 vector 저장소를 열지 않는다. 벡터 복구는 전체 누락분을 한 번에 임베딩하고 단일 merge transaction으로 upsert한다. 적용 전 SQLite online backup API와 LanceDB 디렉터리 복사본을 `backups/reconciliation/<UTC timestamp>/`에 만들며, 고아 항목은 자동 삭제하지 않는다.

## 5. Derived pipelines

| Pipeline | Input | Output |
|---|---|---|
| Daily | 최근 24h SQLite reports | `obsidian_vault/Daily_Reports`, wordcloud, email |
| Weekly Insight | matrices + graph + LanceDB RAG | `reports/insights`, HTML visuals, email |
| CIO | MCP aggregate context | `obsidian_vault/reports`, visuals, email |
| Market Narrative | SQLite statistics + six RAG queries | `reports/narrative`, `Narrative_Reports`, email |
| Blog | recent reports + latest Daily | `tistory_draft.md`, archive, browser publishing |
| Telegram | SQLite 8 tools + LanceDB 2 tools | Telegram answer/tool loop |

## 6. MCP and security boundaries

`src/mcp_server.py`는 FastMCP stdio server이다.

- SQLite를 `mode=ro`로 연다.
- arbitrary SQL은 첫 keyword가 `SELECT` 또는 `WITH`인 경우만 허용한다.
- Obsidian 조회는 11자 YouTube ID를 검증한다.
- Telegram은 in-process로 SQLite 8 tools와 LanceDB 2 tools를 노출한다.

## 7. Failure semantics

- `PipelineResult`가 `SUCCESS`, `SKIPPED`, `FAILED`, `ABORTED`와 실패 단계(`PRECHECK`, `INGESTION`, `ANALYSIS`, `RELEVANCE`, `STORAGE`)를 반환한다.
- 영상 단위 ingestion/LLM/export 실패는 CLI가 카운트하고 다음 영상으로 진행한다.
- YouTube IP block 패턴은 `ABORTED`로 반환되어 남은 큐를 중단한다.
- Markdown 저장 실패 시 SQLite 저장을 실행하지 않는다. SQLite 저장 실패 시 이미 생성된 Markdown 경로와 `markdown_saved_database_pending` 경고를 결과에 남긴다.
- 영상 또는 backfill 실패가 하나 이상이면 `main.py`는 1로 종료한다.
- SQLite, Obsidian, LanceDB는 하나의 ACID transaction으로 묶이지 않는다.
- 대형 transcript는 절삭하지 않으며 provider 한계 시 해당 영상 분석이 실패한다.

## 8. Refactoring invariants

리팩토링에서 보존해야 할 핵심 계약:

1. transcript 원문 전체 전달
2. video ID 기반 멱등성
3. 입력 source channel/upload date의 최종 override
4. JSON sanitization + one parse recovery call
5. backlink normalization
6. non-macro skip의 영속화
7. SQLite 기준 Obsidian/LanceDB 재생성
8. MCP read-only 제한
9. DB 원문 기반 근거/인용 결정론적 렌더링

## 9. Known refactoring targets

측정된 위험도와 단계별 승인 기준은 `REFACTORING_ROADMAP.md`를 기준으로 한다.

- 스키마를 도메인 모듈로 분리
- 고아 projection의 검토·삭제 정책 추가
- 실행별 구조화 JSON 로그와 영속 상태 추가
- 외부 API mock 기반 characterization test 확대
- 중복된 email/Markdown/LLM 보조 로직 통합
