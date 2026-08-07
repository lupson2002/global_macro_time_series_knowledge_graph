# Derived Pipeline Contracts

12단계는 Daily, CIO, Telegram, Insight 파이프라인을 합치기 전에 현재 동작을
characterization test로 고정한 안전 기준선이다. 이 단계에서는 출력 형식, 외부 호출,
저장 위치를 변경하지 않는다.

## 현재 경계

| 파이프라인 | 입력 | 결정론적 출력 | 외부 부작용 | 현재 실패 계약 | 보호 테스트 |
|---|---|---|---|---|---|
| Daily report | 최근 SQLite 리포트와 quant signal | 가중 심리, tail 차감, regime, Markdown 섹션 | SQLite 일별 심리 저장, 파일 저장, SMTP | 빈 표본은 neutral, 메일 실패는 경고 | 점수·차감·빈 표본 |
| CIO orchestrator | MCP 집계 리포트와 LLM 본문 | 근거 context block, JSON 추출, Markdown 표 | MCP/LLM 호출, Plotly·Pyvis 파일, SMTP | JSON 파손은 빈 dict, 개별 시각화 실패는 경고 | 근거 필드·표 변환·이스케이프 |
| Telegram bot | 사용자 메시지, tool name/arguments | tool 결과 문자열, 메시지 chunk | Telegram polling/send, 로컬 MCP, LLM HTTP | unknown/argument mismatch는 명시적 JSON error | dispatch·JSON 인자·chunk 경계 |
| Insight report | matrix/graph/RAG 결과 | matrix headline, narrative node context, Markdown | SQLite/LanceDB 조회, 시각화 파일, LLM, SMTP | 빈 matrix는 안내문, 개별 RAG 실패는 본문에 표시 | headline·빈 결과·graph node 보존 |

## 관측된 중복과 통합 순서

1. JSON/list 정규화 helper를 먼저 통합한다. 순수 함수이며 오류 입력 계약을 작은 테스트로
   고정할 수 있어 변경 위험이 가장 낮다. **13단계 완료:** `src/json_utils.py`의
   `parse_json_list()`로 통합했고, native list 허용 여부를 명시적 옵션으로 보존했다.
2. SMTP 전송과 plain-text fallback을 delivery adapter로 모은다. Daily, CIO, Insight의
   수신자·제목·첨부·fallback 계약을 각각 고정한 뒤 이동한다. **14단계 완료:**
   `src/email_delivery.py`가 MIME/SMTP transport를 담당하고 파이프라인별 skip·경고·fallback
   정책은 기존 진입점에 유지했다.
3. frontmatter, Markdown 표, HTML envelope 등 렌더링 경계를 통합한다. 파이프라인별 본문
   형식은 그대로 두고 공통 escape/조립 기능만 이동한다. **15단계 완료:**
   `src/report_rendering.py`로 frontmatter envelope, table-cell escape, Gmail HTML 렌더링을
   이동했고 보고서별 필드·순서·태그는 각 호출부에 유지했다.
4. 기존 cloud client 위에 파생 리포트용 요청/결과 metadata 경계를 둔다. 모델, prompt,
   재시도 횟수는 이 단계에서 변경하지 않는다. **16단계 완료:** `src/derived_llm.py`의
   typed request/result가 Daily, CIO, Insight, Narrative의 pipeline·provider·model·latency·
   attempt 이력을 연결하며 기존 공개 함수는 계속 문자열을 반환한다.
5. 마지막으로 네 진입점을 순수 계산과 외부 부작용을 연결하는 얇은 orchestration으로 줄인다.

## 변경 금지 범위

- 이메일 수신자, 제목, 본문 template과 fallback 정책
- LLM provider, model, prompt, timeout과 재시도 정책
- 보고서 파일명, 저장 경로, frontmatter schema
- Telegram tool schema, 이름, 반복 횟수와 메시지 제한
- matrix, graph, sentiment 계산 알고리즘
- 운영 SQLite, Obsidian, LanceDB 데이터 및 고아 vector 삭제

구현은 위 순서를 한 커밋씩 진행하며 각 커밋마다 전체 테스트, compileall, read-only
reconciliation을 다시 실행한다.
