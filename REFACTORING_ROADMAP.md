# Refactoring Roadmap

> Baseline: `cc28423`, 2026-08-07. 이 문서는 동작 변경 전에 작성한 위험 지도이며
> 각 파동의 측정 결과를 누적한다.

## 성공 조건

- 전체 transcript를 절삭 없이 LLM에 전달한다.
- video ID 멱등성 및 non-macro skip 기록을 보존한다.
- Markdown 선저장, SQLite 완료 마커 순서를 보존한다.
- SQLite 1,628건을 기준으로 Markdown/LanceDB 누락이 0건이어야 한다.
- MCP SQLite 연결은 read-only이며 SQL allow-list를 유지한다.
- 단계별로 전체 테스트와 저장소 reconciliation을 통과한 뒤 커밋한다.

## 측정 방법

`scripts/audit_refactoring.py`는 외부 패키지 없이 AST를 읽어 모듈 LOC, 내부 import 수,
최대 함수 길이와 decision point 수를 계산한다.

```bash
.venv/bin/python scripts/audit_refactoring.py
.venv/bin/python scripts/audit_refactoring.py --json
```

점수는 절대 품질 등급이 아니라 변경 순서를 비교하기 위한 휴리스틱이다. 생성 코드와 긴
상수도 LOC에 포함되므로 수치만으로 자동 분리하지 않는다.

## 현재 위험 지도

| 영역 | 관측값 | 위험 | 판단 |
|---|---:|---|---|
| `main.py::main` | 192줄, 32 decision points | 높음 | CLI 파싱·대상 수집·backfill·실행 요약이 결합됨 |
| `SQLiteExporter.export_data` | 144줄 | 매우 높음 | 원본 저장과 LanceDB projection이 연결되므로 먼저 characterization test 보강 |
| `ObsidianMDExporter.export_markdown` | 124줄 | 매우 높음 | 파일명·YAML·근거 렌더링 계약이 한 함수에 집중됨 |
| `PipelineService.process` | 94줄 | 중간 | typed 결과 테스트가 있어 작은 단계 추출에 적합 |
| `LocalLLMClient` parse 경로 | 각 79줄 | 높음 | 복구 호출·soft validation·metadata override 순서가 중요 |
| `report_generator.py` | 839줄 | 높음 | 렌더링 함수가 크지만 핵심 ingestion과 분리되어 후순위 가능 |
| `orchestrator.py` | 562줄, 내부 import 7개 | 높음 | MCP·시각화·메일을 함께 조정하며 직접 테스트가 부족함 |
| `telegram_bot.py` | 483줄 | 중간 | 도구 schema 상수 비중이 크므로 LOC만으로 분리하지 않음 |

## 테스트 보호 수준

강하게 보호된 경계는 pipeline 결과 상태, 전체 transcript 전달, provider failover,
SQLite/Markdown round-trip, MCP 보안, reconciliation이다. 직접 characterization test가
ready to add인 영역은 `main.py`의 대상 수집/backfill 조립, report 생성, CIO orchestrator,
Telegram tool loop, insight 파이프라인이다.

## 실행 순서

1. **CLI 조립 분리** — `main.py`에서 argument parser, target discovery, backfill runner를
   순수 함수로 추출한다. 기존 CLI 인자와 종료 코드는 유지하고 characterization test를 먼저 추가한다.
2. **도메인 타입 도입** — extraction dict의 metadata/graph/view 접근을 typed boundary로 감싼다.
   SQLite·Markdown·LanceDB가 같은 필드 해석을 공유하게 하되 저장 schema는 바꾸지 않는다.
3. **저장소 경계 분리** — SQLite 원본 저장과 Markdown/LanceDB projection adapter를 분리한다.
   projection 부분 성공 상태와 재시도 계약을 먼저 테스트한다.
4. **LLM 분석 단계 분리** — sanitize, parse recovery, validation, metadata override를 독립 함수로
   이동한다. provider 호출 횟수와 전체 transcript 계약을 고정한다.
5. **파생 파이프라인 정리** — report/CIO/Telegram/insight의 중복 메일·렌더링·LLM helper를 통합한다.
   각 진입점의 출력 snapshot test를 먼저 확보한다.
6. **구조화 실행 기록** — 기존 콘솔 출력을 유지하면서 run/video별 JSON event sink를 추가한다.

각 항목은 별도 커밋으로 제한한다. schema migration, provider 변경, 고아 vector 삭제 및 출력
형식 재설계는 이 리팩터링과 함께 수행하지 않는다.

## 첫 구현 파동의 승인 기준

- `main.py::main`이 CLI wiring과 결과 집계만 담당한다.
- target discovery/backfill 함수는 임시 디렉터리와 mock으로 네트워크 없이 검증된다.
- 기존 CLI 옵션, 처리 순서, 지연 적용 조건, 종료 코드가 동일하다.
- 전체 테스트, compileall, read-only reconciliation 결과가 기준선과 동일하다.

## 첫 구현 파동 결과

- `build_parser()`, `collect_video_targets()`, `run_backfill()` 경계를 추출했다.
- `main.py::main`은 192줄/32 decision points에서 93줄/11 decision points로 감소했다.
- `main.py` 위험 점수는 16에서 11로 감소했다.
- 수동/RSS 우선순위와 중복 제거, tier 전달, underscore video ID backfill,
  부분 실패 종료 코드, 처리 순서와 지연 조건을 characterization test로 고정했다.
- 공개 CLI 옵션과 저장 schema는 변경하지 않았다.

## 두 번째 구현 파동 결과

- 공개 dict 입력을 보존하는 immutable `MacroView` adapter를 추가했다.
- section 접근은 read-only이며 malformed section을 빈 mapping으로 격리한다.
- list projection은 복사본을 반환해 저장 단계의 원본 mutation을 방지한다.
- SQLite 신규 저장과 reconciliation 복구가 단일 `vector_document()`를 사용한다.
- `SQLiteExporter.export_data`의 decision points는 19에서 9, 위험 점수는 12에서 11로 감소했다.
- 엄격한 Pydantic `MacroViewSchema`는 LLM 검증에 유지해 legacy 저장 호환성과 역할을 분리했다.
- SQLite schema, Markdown 형식, LanceDB schema는 변경하지 않았다.

## 세 번째 구현 파동 결과

- `SQLiteExporter`에서 LanceDB import와 암묵적 upsert side effect를 제거했다.
- `LanceDbProjection` adapter를 추가하고 `PipelineService`에 명시적으로 주입했다.
- 저장 순서를 Markdown → SQLite → LanceDB로 characterization test에 고정했다.
- Markdown 실패 시 DB 미완료, SQLite 실패 시 vector 미실행 계약을 유지했다.
- LanceDB 실패는 원본 성공을 유지하며 `vector_projection_pending` 경고와 reconciliation
  복구 경로를 제공한다.
- `src/exporter.py` 위험 점수는 11에서 9로 감소했고 최대 위험 함수가 원본 upsert에서
  schema initialization으로 이동했다.

## 네 번째 구현 파동 결과

- provider-neutral `ExtractionResponseProcessor`를 추가했다.
- sanitize/parse, 1회 recovery, trusted metadata override, backlink normalization,
  soft validation 순서를 독립 테스트로 고정했다.
- `LocalLLMClient`는 recovery callback과 전체 transcript prompt 구성만 담당한다.
- 최초 성공은 LLM 1회, parse recovery는 정확히 총 2회 호출하며 양쪽 모두 원문 전체를 전달한다.
- `_parse_and_validate`는 79줄에서 43줄로 감소했고 공개 helper/API는 유지했다.

## 다섯 번째 구현 파동 사전 특성화 결과

- Daily의 가중 심리·tail-risk 차감·빈 표본 레짐을 결정론적 테스트로 고정했다.
- CIO의 근거 필드 context, JSON 시각화 payload 추출, Markdown 표 치환과 pipe
  escape 계약을 고정했다.
- Telegram의 JSON tool 인자, 명시적 dispatch 오류, 줄 우선 메시지 분할을 고정했다.
- Insight의 matrix headline, 빈 결과 안내, narrative graph node 보존을 고정했다.
- 네 파이프라인의 순수 계산/렌더링과 LLM·파일·메일·Telegram 부작용 경계를
  `DERIVED_PIPELINE_CONTRACTS.md`에 기록했다.
- 런타임 코드, 출력 형식, provider, 저장 schema와 운영 데이터는 변경하지 않았다.

다음 구현은 가장 위험이 낮은 JSON/list 정규화 helper 통합부터 시작한다. 이후 메일
delivery adapter, 렌더링 envelope, 파생 LLM 경계, 얇은 진입점 순으로 진행한다.

## 다섯 번째 구현 파동 1차 결과

- Daily, CIO, RAG Insight, Blog, Weekly Analytics, Exporter, LanceDB에 흩어진 JSON/list
  parser를 `src/json_utils.py::parse_json_list`로 통합했다.
- 기존 계약 차이는 `accept_native` 옵션으로 명시했다. 이미 파싱된 list를 허용하던
  호출부는 같은 객체를 반환하고, SQLite JSON 컬럼 전용 호출부는 계속 거부한다.
- 빈 값, 파손 JSON, JSON object, 비문자 입력은 모두 기존처럼 빈 list로 정규화한다.
- 공개 출력, 저장 schema, 운영 데이터와 외부 서비스 호출은 변경하지 않았다.

다음 2차 구현은 Daily/CIO/Insight의 메일 발송 계약을 characterization test로 먼저
고정한 뒤 공통 delivery adapter로 이동한다.

## 다섯 번째 구현 파동 2차 결과

- MIME plain/HTML 조립, HTML 파일 첨부, SMTP login/send를
  `src/email_delivery.py::send_multipart_email`로 통합했다.
- Daily의 timeout 미지정·비밀번호 공백 제거·전송 실패 경고 계약을 유지했다.
- CIO의 mixed 첨부·60초 timeout·비밀번호 공백 제거·plain fallback 계약을 유지했다.
- Insight의 mixed 첨부·실재 파일만 집계·60초 timeout·plain fallback 계약을 유지했다.
- 설정 부족 시 기존 skip 문구와 공개 `send_email_report`, `_send_cio_email_with_visuals`,
  `send_email_with_visuals` 진입점을 유지했다.
- 실제 SMTP 연결 없는 테스트로 MIME 구조, 로그인 인자, 수신자, 첨부 파일명과 fallback
  호출 횟수를 검증한다.

다음 3차 구현은 frontmatter, Markdown 표 escape, HTML envelope의 공통 렌더링 후보를
분류하고 출력 snapshot을 보강한 뒤 순수 rendering helper만 이동한다.

## 다섯 번째 구현 파동 3차 결과

- ordered YAML frontmatter envelope, Markdown table cell escape, Gmail inline-style HTML을
  `src/report_rendering.py`의 순수 함수로 분리했다.
- Daily, CIO, Market Narrative의 frontmatter 필드·순서·태그는 호출부에 남겨 보고서별
  schema 차이를 보존했다.
- CIO와 Weekly Analytics가 공통 table-cell escape를 사용하되 기존 newline/strip 정책
  차이는 명시적 옵션으로 유지한다.
- Daily의 `_md_to_html_email`은 호환 alias로 유지하고 CIO, Insight, Market Narrative의
  불필요한 Daily 모듈 의존을 제거했다.
- frontmatter의 delimiter·개행, Daily metadata 전체, Obsidian backlink 제거, Gmail
  heading/table/bold 스타일과 표 셀 whitespace를 snapshot 성격의 테스트로 고정했다.

다음 4차 구현은 기존 `cloud_client` 위에 파생 리포트 요청/결과 metadata 경계를 두되
provider, model, prompt, timeout과 재시도 횟수를 그대로 유지한다.

## 다섯 번째 구현 파동 4차 결과

- `DerivedLLMRequest`가 pipeline 이름과 system/user 전문, token budget, temperature,
  Ollama/NIM model 및 attempt budget을 명시한다.
- `DerivedLLMResult`가 기존 `CompletionResult`의 content, provider, model, latency와 전체
  attempt 이력을 pipeline 이름에 연결한다.
- Daily(4096/0.2/5회), CIO(8192/0.3/4회), Insight(2048/0.3/3회), Narrative
  (설정 token budget/0.3/3회)의 호출 인자를 그대로 유지했다.
- 공통 경계는 기존 `cloud_client.chat_completion_result`를 정확히 한 번 호출하며 자체
  retry를 추가하지 않는다.
- 기존 `generate_morning_report`, `_call_nim_reasoner_sync`, `_call_llm`,
  `_call_llm_narrative`는 계속 text를 반환하고 기존 빈 응답/예외 처리를 유지한다.
- Groq/Cerebras 우선인 번역과 Blog `Llama70BRouter`는 provider 순서를 보존하기 위해
  이번 공통화 범위에서 제외했다.

다음 5차 구현은 네 파생 진입점을 계산·렌더링·delivery adapter를 연결하는 얇은
orchestration으로 정리하되 파일 경로와 실행 순서를 characterization test로 먼저 고정한다.

## 다섯 번째 구현 파동 5차 결과

- `ReportArtifact`와 공통 writer가 부모 디렉터리 생성, UTF-8 저장과 반환 경로를 담당한다.
- Daily, CIO, Insight, Narrative의 기존 디렉터리와 대소문자를 포함한 파일명을 순수 path
  factory와 테스트로 고정했다.
- Narrative의 일반 report를 먼저, frontmatter가 포함된 Obsidian artifact를 다음에 쓰는
  선언 순서를 보존했다.
- Daily 파일 본문과 이메일 본문의 유일한 차이인 frontmatter 경계를
  `_assemble_daily_outputs`로 분리했다.
- CIO 시각화 링크는 pie → bar → conflicts 순서를 유지하는 `_append_visual_links`로
  분리했으며 시각화가 없을 때 원문을 그대로 반환한다.
- Narrative의 직접 MIME/SMTP 코드를 공통 delivery adapter로 연결하고 60초 timeout,
  비밀번호 공백 제거, 실패 시 경고-only 정책을 유지했다.
- 공개 CLI 옵션, 파일 저장 시점, 이메일 발송 순서와 반환형은 변경하지 않았다.

다음 단계는 다섯 번째 구현 파동 전체를 교차 검토하고, 남은 대형 함수의 위험 지표와
테스트 보호 수준을 다시 측정해 구조화 실행 기록 단계로 넘어갈지 판단하는 안정화 gate다.

## 다섯 번째 구현 파동 안정화 gate 결과

- Stage 12 기준 `14f1496`부터 Stage 17 `125d089`까지 전체 diff를 재검토했다.
- 전체 테스트는 93개에서 112개로 증가했고 핵심 파생 모듈 LOC는 모두 감소했다.
- Codex·Gemini Trident 감사에서 보안·논리·신뢰성·테스트 공백 finding이 없었다.
- 전체 테스트, compileall, diff check와 운영 reconciliation 기준을 충족했다.
- 상세 수치와 잔여 hotspot은 `REFACTORING_STABILIZATION_REPORT.md`에 기록했다.

**판정: PASS.** 다음 파동은 기존 콘솔 출력을 유지하면서 opt-in 구조화 event sink를
추가하는 6번 실행 순서로 진행한다.
