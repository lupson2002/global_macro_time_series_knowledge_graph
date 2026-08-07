# Refactoring Stabilization Report

> Scope: `14f1496..125d089` (Stages 13–17), audited 2026-08-07

## Verdict

**PASS — 파생 파이프라인 구현 파동을 닫고 구조화 실행 기록 단계로 진행할 수 있다.**

공개 CLI, provider 순서, 전체 prompt, 저장 schema, 파일명, 이메일 정책과 운영 데이터는
유지됐다. 전체 테스트와 compileall이 통과했고 Codex·Gemini Trident 감사 모두 별도
finding 없이 종료됐다.

## Measured change

| 측정 항목 | Stage 12 기준 | Stage 17 결과 | 판단 |
|---|---:|---:|---|
| 전체 테스트 | 93 | 112 | 파생 경계 보호 19개 증가 |
| `src/orchestrator.py` LOC | 562 | 522 | 40줄 감소 |
| `src/report_generator.py` LOC | 839 | 790 | 49줄 감소 |
| `scripts/insight_report.py` LOC | 273 | 256 | 17줄 감소 |
| `src/lancedb_store.py` LOC | 411 | 397 | 14줄 감소 |
| `generate_morning_report` | 115줄 / 13 decisions | 102줄 / 13 decisions | 조립 일부 분리, 분기 수 유지 |
| `hydrate_views` | 68줄 / 10 decisions | 59줄 / 7 decisions | 복원 경계 단순화 |

AST 위험 점수 일부는 공통 모듈 import 증가로 상승했다. 이 점수는 내부 import 수를
결합도로 단순 계산하므로, 중복 구현이 `json_utils`, `email_delivery`, `report_rendering`,
`derived_llm`, `report_artifacts`의 단방향 adapter 의존으로 바뀐 이번 구조에서는 LOC·최대
함수·decision point·계약 테스트와 함께 해석해야 한다.

## Contract evidence

- JSON/list 입력의 두 legacy 모드와 malformed fallback을 고정했다.
- Daily/CIO/Insight/Narrative의 SMTP timeout, 비밀번호 처리와 fallback 차이를 고정했다.
- frontmatter 개행, Gmail HTML style, Markdown table escape를 고정했다.
- derived LLM token/temperature/attempt budget과 provider metadata를 고정했다.
- Daily/CIO/Insight/Narrative 파일 경로, 대소문자, UTF-8 및 Narrative 이중 저장 순서를 고정했다.
- transcript는 기존 전체 single-shot 계약 테스트를 계속 통과한다.

## Remaining hotspots

| 영역 | 현재 관측값 | 다음 판단 |
|---|---:|---|
| `aggregate_macro_context` | 101줄 / 19 decisions | MCP 수집·예산 정책 테스트를 추가하기 전 추가 분리 보류 |
| `build_report` | 133줄 / 12 decisions | matrix/graph 시각화 mock 경계 보강 후 단계 추출 |
| `generate_morning_report` | 102줄 / 13 decisions | wordcloud optional adapter를 먼저 격리 |
| `build_evidence_block` | 67줄 / 32 decisions | Blog 파동에서 별도 snapshot 보호 후 검토 |

CIO context의 `MAX_CONTEXT_CHARS` 제한은 ingestion transcript 절삭과 다른 파생 집계 예산
정책이다. transcript 전체 전달 계약은 유지되며, 이 제한을 바꾸는 일은 별도 동작 변경으로
취급한다.

## Next-stage entry gate

구조화 실행 기록은 기존 콘솔 출력을 유지하는 additive event sink로 구현한다.

- 기본 실행 동작과 종료 코드를 바꾸지 않는다.
- run/video/report 단계와 상태를 JSON 직렬화 가능한 event로 정의한다.
- sink가 없을 때 추가 파일 쓰기를 하지 않는다.
- sink 실패가 핵심 파이프라인 성공을 뒤집지 않도록 정책을 먼저 테스트한다.
- 단계별 전체 테스트, compileall, read-only reconciliation을 계속 적용한다.

운영 기준은 SQLite 1,628 / Markdown 1,628 / vectors 1,629, 누락 0이며 기존 고아 vector
`abc123def45`는 삭제하지 않는다.
