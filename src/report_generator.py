# -*- coding: utf-8 -*-
"""
Morning Macro Synthesis Agent
==============================
Fetches newly gathered macroeconomic data from the SQLite DB (last 24 hours),
uses Ollama Cloud with a NIM fallback to synthesize a
daily consensus report with Obsidian backlinks, and exports it to the Daily_Reports folder
within the Obsidian Vault. TIER2_MODEL env 오버라이드.
"""

import sys
import json
import sqlite3
import datetime
import math
import time
from pathlib import Path
import re as _re
from src.config import settings
from src.email_delivery import send_multipart_email
from src.derived_llm import DerivedLLMRequest, complete_derived
from src.json_utils import parse_json_list
from src.report_rendering import markdown_to_email_html as _md_to_html_email, render_frontmatter
from src.report_artifacts import daily_artifact, write_report_artifact
from src.run_events import ReportRunJournal

# src.llm_router 등 src.* 패키지 import 를 위해 프로젝트 루트를 sys.path 에 추가
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _safe_json_list(raw) -> list:
    """👑 [Ver 4.3] reports JSON 컬럼(catalysts/risks) 안전 파싱. NULL/빈/파손 → []."""
    return parse_json_list(raw, accept_native=False)


def _collect_translatable(reports: list) -> list:
    """👑 [Ver 4.5] 섹션5/6 렌더에 필요한 영문 텍스트를 (key, text) 쌍으로 수집.

    번역 대상: core_thesis(섹션5·6 공통), verbatim_quote, additional_quotes,
    conditional_catalysts, invalidation_risks, key_data_points 의 context.
    고유명사/티커/숫자가 포함된 짧은 코드(price_targets 등)는 번역 제외.
    """
    items = []
    for r in reports:
        vid = r.get("video_id") or ""
        thesis = (r.get("core_thesis") or "").strip()
        if thesis:
            items.append((f"{vid}::thesis", thesis))
        quote = (r.get("verbatim_quote") or "").strip()
        if quote:
            items.append((f"{vid}::quote", quote))
        for i, q in enumerate(_safe_json_list(r.get("additional_quotes"))):
            q = (q or "").strip()
            if q:
                items.append((f"{vid}::aq::{i}", q))
        for i, c in enumerate(_safe_json_list(r.get("conditional_catalysts"))):
            c = (c or "").strip()
            if c:
                items.append((f"{vid}::cat::{i}", c))
        for i, k in enumerate(_safe_json_list(r.get("invalidation_risks"))):
            k = (k or "").strip()
            if k:
                items.append((f"{vid}::risk::{i}", k))
        for i, d in enumerate(_safe_json_list(r.get("key_data_points"))):
            if isinstance(d, dict):
                ctx = (d.get("context") or "").strip()
                if ctx:
                    items.append((f"{vid}::kdpctx::{i}", ctx))
    return items


def _parse_korean_json_array(content: str) -> list:
    """👑 [Ver 4.5] 번역 LLM 응답에서 JSON 문자열 배열을 안전 추출."""
    if not content:
        return []
    s = content.strip()
    # 코드 펜스 제거
    if s.startswith("```"):
        s = _re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = _re.sub(r"\n?```$", "", s).strip()
    # 첫 [ ~ 마지막 ] 범위 추출
    start = s.find("[")
    end = s.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    frag = s[start:end + 1]
    try:
        arr = json.loads(frag)
        return arr if isinstance(arr, list) else []
    except (ValueError, TypeError):
        return []


# 👑 [2026-08-07 사용자 결정] 번역은 경량 라우터(Llama70BRouter) 우선, NIM 폴백.
# [2026-08-15] Groq 전면 비활성화 — Ollama Cloud 우선, NIM 폴백으로 통일.
# M4 의 클라이언트 재사용(싱글턴) 개선은 유지.
_router: "Llama70BRouter | None" = None


def _get_translation_router():
    """Llama70BRouter 싱글턴 — 호출마다 신규 생성(연결 재수립) 방지."""
    global _router
    if _router is None:
        from src.llm_router import Llama70BRouter
        _router = Llama70BRouter()
    return _router


def _translate_to_korean_map(items: list) -> dict:
    """👑 [Ver 4.9] (key, text) 쌍을 일괄 한국어 번역 → {key: korean} 맵.

    **Ollama Cloud(Llama70BRouter) 우선 → 공통 실행 계층의 NIM 폴백.**
    원문 결정론적 렌더 원칙 유지: LLM은 '번역'만 수행(재생성·요약 금지).
    빈 항목·파싱 실패 시 원문 그대로(fallback). 청크 단위 호출(출력 절삭 방지).
    """
    if not items:
        return {}
    tr_map = {}
    CHUNK = 20
    SYS = (
        "You are an expert financial and macroeconomic translator. "
        "Translate the English text into natural, professional Korean. "
        "Keep names, tickers, institutions, numbers, units, dates as-is. "
        "Output ONLY the translated text without commentary, "
        "as a JSON array of strings in the same order as the input."
    )
    router = _get_translation_router()
    for i in range(0, len(items), CHUNK):
        chunk = items[i:i + CHUNK]
        numbered = "\n".join(f"{j+1}. {txt}" for j, (_, txt) in enumerate(chunk))
        user = f"아래 {len(chunk)}개 텍스트를 한국어로 번역해 JSON 배열로 반환:\n\n{numbered}"
        try:
            content = router.generate(
                system=SYS, user=user, max_tokens=4096, temperature=0.1
            )
            arr = _parse_korean_json_array(content or "")
        except Exception as e:
            print(f"[WARN] Korean translation chunk {i//CHUNK} failed: {e}")
            arr = []
        for j, (key, orig) in enumerate(chunk):
            tr_map[key] = (arr[j].strip() if j < len(arr) and arr[j] else orig)
    return tr_map


def _tr(tr_map: dict, key: str, orig: str) -> str:
    """👑 [Ver 4.5] 번역 맵 조회 — 실패 시 원문 fallback."""
    if not orig:
        return orig
    return tr_map.get(key, orig)

# Load dotenv
# NIM fallback 설정. 일반 합성은 cloud_client의 Ollama Cloud 우선 경로를 사용한다.
NIM_BASE_URL = settings.llm.nim_base_url
NIM_API_KEY = settings.llm.nim_api_key
TIER2_MODEL = settings.llm.tier2_model
TIER2_TIMEOUT = settings.llm.tier2_timeout

# System Instruction for synthesis report generation
# 👑 Ver 4.0 — 테마별 컨센서스 구조 + Structural Alpha Spotlight + 충돌 입체화.
# 클립 나열식(구루별 행) → 테마/자산군별 통합 컨센서스. 한국어 자연스럽게, 백링크 핵심만.
SYSTEM_INSTRUCTION = """
당신은 최고 수준의 거시경제 연구 전략가입니다. 지난 24시간 동안 수집된 글로벌 금융 구루들의 매크로 의견을 분석해 **한국어 일일 종합 리포트**를 작성합니다.

【가독성 원칙 — 반드시 준수】
1. **테마별 컨센서스**: 구루 이름 단위가 아닌 [자산군/메가 테마] 단위로 묶어, 그 안의 전문가 의견 강도(Sentiment & Conviction)를 가중·통합해 한 행으로 표현. 구루 중복 등장 금지.
2. **표는 4열, 각 셀 1-2줄**(줄바꿈 최소화). 긴 논거는 압축.
3. **[[백링크]]는 핵심 엔티티만**: 화자 이름, 자산군, 티커, 핵심 매크로 테마. 일반 단어/숫자/형용사 금지.
4. **한국어 자연스럽게**: 번역투·기계문장 금지. 전문 투자 정보지 톤.
5. **분량은 데이터에 비례**: 24h 데이터가 적으면 짧게, 많으면 적절히. 과장·공허한 채우기 금지.
6. **장기 뷰 vs 단기 촉매 분리**: 구조적 알파(거대 뉴스·장기 내러티브)는 [Structural Alpha Spotlight]로 분리, 단기 테마와 섞지 말 것.

【리포트 구조 — 정확히 이 형식 준수】

# 📊 일일 매크로 종합 (YYYY-MM-DD KST)

> **오늘의 한 줄 결론:** (오늘의 시장 방향·핵심 원인·제약 요인을 담아 한국어 한 문장, 120자 이내)

## 핵심 브리프
- **시장 방향:** (현재 방향과 전일 대비 의미를 한 문장)
- **핵심 촉매:** (가장 강한 상방 또는 변화 촉매를 한 문장)
- **최대 리스크:** (가장 중요한 하방 위험과 확인 조건을 한 문장)

> **시장 스탠스 {stance_score}/100** · 전일 대비 {daily_change} · **{regime_name}**
> 합의도 {agreement_score}% · 신뢰도 {confidence_label} · 테일리스크 {tail_risk_label} ({tail_risk_count}/{sample_count})

## 오늘의 투자 시사점
- **선호:** (현재 데이터가 상대적으로 지지하는 자산·테마와 이유)
- **경계:** (피하거나 헤지가 필요한 자산·노출과 이유)
- **관찰:** (향후 방향을 바꿀 구체적 지표·임계값·이벤트)

## 1. 테마별 내러티브 지형도 (Thematic Consensus)
| 핵심 테마 / 자산군 | 내러티브 강도 | 시장의 지배적 뷰 & 핵심 근거 | 핵심 변수 및 서포트 구루 |
| :--- | :---: | :--- | :--- |
| **[[테마]]** ([[티커들]]) | X.X/10 | (한 줄 통합 논거) | [[구루1]], [[구루2]] |

(행은 자산군/메가 테마 기준 3-6개. 같은 구루가 여러 테마에 서포트로만 등장 가능, 행의 주체는 테마.)

## 2. 거시적 시각의 충돌 (Crucial Debates)
- 🥊 **(충돌 명칭 — 거시 매크로 축)**: 구루명 대신 **[정책·구조적 축] vs [유동성·밸류에이션 축]** 형태로 해설.
  - **Long ([[구루A]]):** (입장 한 줄 + 근거)
  - **Short ([[구루B]]):** (입장 한 줄 + 근거)
  - → 투자자 주목점: 위험 프리미엄 판단에 왜 중요한지 한 줄.

## 3. Structural Alpha Spotlight (장기 뷰 & 촉매제)
- 💡 **[[이벤트/종목]] — (한 줄 헤드라인)**
  - 장기 내러티브: (2-3줄 — 왜 구조적 알파인지, 지형 변화, 트리거)
- 💡 **[[이벤트/종목]] — (한 줄 헤드라인)**
  - 장기 내러티브: ...
(당일 거대 뉴스·장기 파트너십·상장·규제 변화 등만. 단기 잡음 제외. 0-2개.)

## 4. Data Check (핵심 데이터)
- (수치·신호 2-4줄 — 극단값/리스크 시그널/기술적 체크포인트. **각 수치에 출처(화자/기관)와 시점(날짜)을 명시. 출처 없는 단발 숫자는 지양. 입력의 Key Data Points·Price Targets 항목을 우선 활용.**)

---
*백링크 규칙: 화자·자산군·티커·핵심 테마만 [[ ]]. 일반 단어 금지.*

【주의】Executive Brief와 위 4개 번호 섹션까지만 작성. 이어지는 워드클라우드,
"## 5. 핵심 근거 & 직접 인용 (Evidence & Quotes)" 및
"## 부록 A. 24시간 전체 수집 관점 (24h Collected Views)"은 시스템이 입력 데이터 원문에서
결정론적으로 자동 부착하므로, 당신은 섹션 5와 부록을 작성하지 마시오.
대신 섹션 1-4에서 인용하는 구루명은 입력의 `[[구루명]]` 표기와 정확히 일치시켜 자동 부록과 매칭되게 하시오.

【Executive Brief 품질 게이트】
- 한 줄 결론은 하나의 문장만 작성하고 제목 다음에 즉시 배치한다.
- 핵심 브리프는 정확히 3개 불릿만 작성한다. 같은 사실을 표현만 바꿔 반복하지 않는다.
- 투자 시사점은 정확히 3개 불릿(선호·경계·관찰)만 작성한다.
- 제공된 심리 수치는 그대로 사용하고 재계산하거나 과장하지 않는다.
"""

def fetch_past_24h_data(db_path: str, lookback_hours: int = 24) -> list:
    """Fetches reports, signals, and nodes from the past X hours."""
    if not Path(db_path).exists():
        print(f"[WARN] Database file not found at: {db_path}")
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Query reports and quant signals created in the last X hours
    # 👑 [B3] KST 보정 — datetime('now') 는 UTC. KST 라벨(파일명)과 24h 윈도우
    # 불일치 해소: datetime('now','+9 hours', '-Nh') = KST now - N 시간.
    # 👑 [Ver 4.4] tactical + 시간지평 필드도 SELECT (Daily 피드/섹션5 활용).
    query = """
        SELECT r.*, q.bull_bear_score, q.conviction_score, q.contrarian_flag,
               q.sector_tilt, q.duration_call, q.macro_factor, q.view_time_horizon
        FROM reports r
        LEFT JOIN quant_signals q ON r.video_id = q.video_id
        WHERE r.created_at >= datetime('now', '+9 hours', ?)
    """
    try:
        cursor.execute(query, (f"-{lookback_hours} hours",))
        rows = cursor.fetchall()
        reports_data = []
        vids = [r["video_id"] for r in rows]

        # 👑 [효율] N+1 제거 — 보고서당 nodes 개별 조회 → video_id IN 단일 조회 후 그룹핑 (결과 동일)
        nodes_by_vid: dict[str, list] = {}
        if vids:
            ph = ",".join("?" * len(vids))
            cursor.execute(
                f"SELECT video_id, node_type, node_value FROM nodes WHERE video_id IN ({ph})",
                vids,
            )
            for n in cursor.fetchall():
                nodes_by_vid.setdefault(n["video_id"], []).append(
                    {"node_type": n["node_type"], "node_value": n["node_value"]}
                )

        for row in rows:
            rep_dict = dict(row)
            rep_dict["nodes"] = nodes_by_vid.get(rep_dict["video_id"], [])
            reports_data.append(rep_dict)

        return reports_data
    except Exception as e:
        print(f"[ERROR] Failed to query database: {e}")
        return []
    finally:
        conn.close()

def format_feed_payload(reports: list) -> str:
    """Formats the DB data into a clean feed format for the LLM prompt."""
    payload = []
    payload.append(f"총 {len(reports)}건의 신규 매크로 뷰가 수집되었습니다.\n")
    
    for idx, r in enumerate(reports, 1):
        payload.append(f"--- [GURU VIEW #{idx}] ---")
        payload.append(f"Speaker: [[{r.get('speaker_name')}]] (Role: {r.get('speaker_role')})")
        payload.append(f"Date: {r.get('broadcast_date')} | Source: {r.get('source_channel')} (Video ID: {r.get('video_id')})")
        payload.append(f"Source URL: https://www.youtube.com/watch?v={r.get('video_id')}")
        payload.append(f"Time Box: {r.get('time_box')}")
        payload.append(f"Core Thesis: \"{r.get('core_thesis')}\"")
        payload.append(f"Verbatim Quote: \"{r.get('verbatim_quote')}\"")
        # 👑 [Ver 4.3] 촉매/무효화 리스크 입력 누락 보완 — 이전엔 verbatim_quote 만 전달.
        cats = _safe_json_list(r.get('conditional_catalysts'))
        risks = _safe_json_list(r.get('invalidation_risks'))
        payload.append(f"Conditional Catalysts: {'; '.join(cats) if cats else 'None'}")
        payload.append(f"Invalidation Risks: {'; '.join(risks) if risks else 'None'}")
        payload.append(f"Bull/Bear Score: {r.get('bull_bear_score')}/10 | Conviction: {r.get('conviction_score')}/10 | Contrarian: {bool(r.get('contrarian_flag'))}")
        # 👑 [Ver 4.4] tactical + 시간지평 + 증거 필드 입력 추가.
        tactical = []
        for k, v in (("sector_tilt", r.get("sector_tilt")), ("duration_call", r.get("duration_call")),
                     ("macro_factor", r.get("macro_factor")), ("view_time_horizon", r.get("view_time_horizon"))):
            if v:
                tactical.append(f"{k}={v}")
        if r.get("speaker_institution"):
            payload.append(f"Institution: {r.get('speaker_institution')}")
        if tactical:
            payload.append(f"Tactical: {', '.join(tactical)}")
        kdp = _safe_json_list(r.get('key_data_points'))
        aq = _safe_json_list(r.get('additional_quotes'))
        pt = _safe_json_list(r.get('price_targets'))
        if kdp:
            payload.append("Key Data Points: " + "; ".join([f"{d.get('indicator','')}={d.get('value','')}{d.get('unit','')} ({d.get('context','')})" for d in kdp if isinstance(d, dict)]))
        if aq:
            payload.append("Additional Quotes: " + " | ".join([f'"{q}"' for q in aq if q]))
        if pt:
            payload.append("Price Targets: " + "; ".join([f"{p.get('ticker','')} {p.get('direction','')} -> {p.get('target','')} ({p.get('horizon','')})" for p in pt if isinstance(p, dict)]))
        # 👑 [Ver 4.7] 4대 내러티브 필드 입력 추가.
        if r.get("expectation_gap"):
            payload.append(f"Expectation Gap: {r.get('expectation_gap')}")
        causal = _safe_json_list(r.get('causal_chain'))
        if causal:
            payload.append("Causal Chain: " + " -> ".join(str(c) for c in causal))
        trk = _safe_json_list(r.get('tracking_indicators'))
        if trk:
            payload.append("Tracking Indicators: " + "; ".join([f"{t.get('metric','')}@{t.get('threshold','')}" for t in trk if isinstance(t, dict)]))
        tac_st = _safe_json_list(r.get('tactical_stance'))
        if tac_st:
            payload.append("Tactical Stance: " + "; ".join([f"{t.get('asset','')}={t.get('stance','')}" for t in tac_st if isinstance(t, dict)]))

        # Nodes
        themes = [n['node_value'] for n in r.get('nodes', []) if n['node_type'] == 'macro_theme']
        assets = [n['node_value'] for n in r.get('nodes', []) if n['node_type'] == 'asset_class']
        tickers = [n['node_value'] for n in r.get('nodes', []) if n['node_type'] == 'ticker']

        payload.append(f"Themes: {', '.join(themes) if themes else 'None'}")
        payload.append(f"Assets: {', '.join(assets) if assets else 'None'}")
        payload.append(f"Tickers: {', '.join(tickers) if tickers else 'None'}")
        payload.append("")

    return "\n".join(payload)

def _resolve_recipients() -> list[str]:
    """이메일 수신자 목록 — EMAIL_TO(콤마 구분 복수 수신자) 우선, 없으면 GMAIL_USER 자기발송."""
    return list(settings.email.recipients)


def send_email_report(subject: str, body_content: str):
    """Sends the generated report via Gmail SMTP server.

    본문은 HTML(MD→변환)로 발송 — 표/헤딩/굵게 가독성 확보. 백링크는 strip.
    수신자: EMAIL_TO(콤마 구분 복수) 또는 GMAIL_USER 자기발송.
    """
    gmail_user = settings.email.user
    gmail_password = settings.email.password

    if not gmail_user or not gmail_password:
        print("[INFO] Gmail SMTP config missing in .env. Skipping email notification.")
        return

    recipients = _resolve_recipients()

    try:
        print("📨 Attempting to send report via email...")
        html_body = _md_to_html_email(body_content)
        send_multipart_email(
            subject=subject,
            body_text=body_content,
            body_html=html_body,
            user=gmail_user,
            password=gmail_password,
            recipients=recipients,
            host=settings.email.smtp_host,
            port=settings.email.smtp_port,
            strip_password_spaces=True,
        )

        print(f"✓ Email successfully sent! ({', '.join(recipients)})")
    except Exception as e:
        print(f"[WARN] Failed to send email report: {e}")

def _build_frontmatter(today_str: str, model: str, source_count: int, kst_iso: str) -> str:
    """👑 [Ver 4.3] Daily 리포트 YAML frontmatter — 결정론적 생성(MD 파일용)."""
    return render_frontmatter((
        ("date", today_str), ("type", "daily_macro_synthesis"), ("model", model),
        ("provider", "nim"), ("source_videos", source_count), ("generated_at", kst_iso),
        ("tags", "[macro, daily_synthesis]"),
    ))


def _assemble_daily_outputs(
    frontmatter: str, report_body: str, evidence: str, appendix: str,
    wordcloud: str = "",
) -> tuple[str, str]:
    """Return the file body (with YAML) and email body (without YAML)."""
    email_body = report_body + wordcloud + evidence + appendix
    return frontmatter + email_body, email_body


def _build_evidence_section(reports: list, top_k: int = 8, tr_map: dict = None) -> str:
    """👑 [Ver 4.3/4.5] '## 5. 핵심 근거 & 직접 인용' — DB 원본에서 결정론적 렌더.

    LLM이 인용을 재생성(편파/환각)하지 않도록 verbatim_quote/catalysts/risks 는
    추출된 원본을 번역(LLM은 번역만, 재생성 금지)해 부착. 선별: contrarian 전원 +
    conviction_score 내림차순 상위, 합산 top_k 건(중복 video_id 제외).
    """
    tr_map = tr_map or {}
    if not reports:
        return ""

    # 정렬: contrarian 우선, 그 다음 conviction_score 내림차순.
    def sort_key(r):
        contra = 1 if r.get("contrarian_flag") else 0
        conv = r.get("conviction_score") or 0
        return (contra, conv)

    seen = set()
    selected = []
    for r in sorted(reports, key=sort_key, reverse=True):
        vid = r.get("video_id")
        if vid in seen:
            continue
        # 인용이 없으면 기사 인용 단위로 의미 없음 → 스킵(근거 부록 목적).
        if not (r.get("verbatim_quote") or "").strip():
            continue
        seen.add(vid)
        selected.append(r)
        if len(selected) >= top_k:
            break

    if not selected:
        return ""

    lines = [
        "\n---\n",
        "## 5. 핵심 근거 & 직접 인용 (Evidence & Quotes)",
        "*(아래 인용·촉매·리스크는 입력 원문을 한국어로 번역해 자동 부착. 번역은 LLM이 수행하되 재생성·요약 금지, 원문 의미 축약 없음.)*\n",
    ]
    for r in selected:
        speaker = r.get("speaker_name") or r.get("video_id") or "Unknown"
        role = r.get("speaker_role") or ""
        source = r.get("source_channel") or ""
        bdate = r.get("broadcast_date") or ""
        vid = r.get("video_id") or ""
        quote = _tr(tr_map, f"{vid}::quote", (r.get("verbatim_quote") or "").strip())
        thesis = _tr(tr_map, f"{vid}::thesis", (r.get("core_thesis") or "").strip())
        cats = [_tr(tr_map, f"{vid}::cat::{i}", c.strip()) for i, c in enumerate(_safe_json_list(r.get("conditional_catalysts")))]
        risks = [_tr(tr_map, f"{vid}::risk::{i}", k.strip()) for i, k in enumerate(_safe_json_list(r.get("invalidation_risks")))]
        # 👑 [Ver 4.4] 신규 증거 필드
        institution = r.get("speaker_institution") or ""
        horizon = r.get("view_time_horizon") or ""
        kdp = _safe_json_list(r.get("key_data_points"))
        aq = [_tr(tr_map, f"{vid}::aq::{i}", q.strip()) for i, q in enumerate(_safe_json_list(r.get("additional_quotes")))]
        pt = _safe_json_list(r.get("price_targets"))
        watch_url = f"https://www.youtube.com/watch?v={vid}" if vid else ""
        themes = [n["node_value"] for n in r.get("nodes", []) if n["node_type"] == "macro_theme"]
        tickers = [n["node_value"] for n in r.get("nodes", []) if n["node_type"] == "ticker"]
        node_tags = ", ".join([*themes, *tickers])

        # 소속·시간지평을 메타에 포함
        meta_bits = [x for x in [role, institution, source, bdate] if x]
        if horizon:
            meta_bits.append(f"호리즌 {horizon}")
        lines.append(f"### 💬 [[{speaker}]]" + (f" ({' · '.join(meta_bits)})" if meta_bits else ""))
        if quote:
            lines.append(f"> 「{quote}」")
        # 추가 직접 인용
        for q in aq:
            if q:
                lines.append(f"> 「{q}」")
        if thesis:
            lines.append(f"- 핵심 주장: {thesis}")
        if cats:
            lines.append(f"- 촉매: {'; '.join(cats)}")
        if risks:
            lines.append(f"- 무효화 리스크: {'; '.join(risks)}")
        # 구조화 수치 — context 만 번역, 지표/수치/단위는 원문 유지.
        if kdp:
            dp_str = " · ".join([f"{d.get('indicator','')}: {d.get('value','')}{d.get('unit','')} ({_tr(tr_map, f'{vid}::kdpctx::{i}', (d.get('context','') or '').strip())})"
                                  for i, d in enumerate(kdp) if isinstance(d, dict)])
            if dp_str.strip(" ·"):
                lines.append(f"- 핵심 수치: {dp_str}")
        # 가격 목표 — 코드성 짧음, 번역 제외.
        if pt:
            pt_str = " · ".join([f"{p.get('ticker','')} {p.get('direction','')}→{p.get('target','')} ({p.get('horizon','')})" for p in pt if isinstance(p, dict)])
            if pt_str:
                lines.append(f"- 가격 목표: {pt_str}")
        src_line = ""
        if watch_url:
            src_line += f"- 출처: [YouTube]({watch_url})"
            if node_tags:
                src_line += f" · 테마/티커: {node_tags}"
        elif node_tags:
            src_line = f"- 테마/티커: {node_tags}"
        if src_line:
            lines.append(src_line)
        lines.append("")
    return "\n".join(lines)


def _build_24h_summary_table(reports: list, tr_map: dict = None) -> str:
    """👑 [Ver 4.5] '## 6. 24시간 수집 요약' — 24h 수집 전체를 리스트로 렌더.

    핵심주장(core_thesis)을 가장 크게·全文 한국어로 표시. 나머지 메타(출처·구루·
    소속·대상자산·방향/확신·유효기간)는 한 줄 헤더로 축약. 표 대신 리스트 포맷 —
    핵심주장全文 가독성 확보. 무의미 행(speaker_name·core_thesis 모두 결측) 제외.
    정렬: contrarian 내림차순 → conviction_score 내림차순(섹션5와 일관).
    """
    tr_map = tr_map or {}
    if not reports:
        return ""

    def _dir_label(bb):
        if bb is None:
            return ""
        try:
            v = float(bb)
        except (TypeError, ValueError):
            return ""
        if v >= 5.5:
            return "Bull"
        if v <= 4.5:
            return "Bear"
        return "중립"

    # 무의미 행 제외: 구루명·핵심주장 모두 비면 스킵.
    rows = [r for r in reports
            if (r.get("speaker_name") or "").strip() or (r.get("core_thesis") or "").strip()]
    if not rows:
        return ""

    def _sort_key(r):
        contra = 1 if r.get("contrarian_flag") else 0
        conv = r.get("conviction_score") or 0
        return (contra, conv)

    rows = sorted(rows, key=_sort_key, reverse=True)

    lines = [
        "\n---\n",
        "## 부록 A. 24시간 전체 수집 관점 (24h Collected Views)",
        "*(요약이 아닌 지난 24시간 수집 구루 뷰 전체. **핵심주장은 원문을 한국어로全文 번역** — "
        "요약/절삭 없이 전문 표시. 정렬: 컨트리언→확신도.)*\n",
    ]
    for idx, r in enumerate(rows, 1):
        vid = r.get("video_id") or ""
        speaker = (r.get("speaker_name") or "").strip() or "(구루 미상)"
        inst = (r.get("speaker_institution") or "").strip()
        guru_hdr = f"{speaker}" + (f" ({inst})" if inst else "")

        source = (r.get("source_channel") or "").strip() or "-"
        dirn = _dir_label(r.get("bull_bear_score"))
        conv = r.get("conviction_score")
        conv_s = f"{conv}/10" if conv is not None else ""
        contra = " ★" if r.get("contrarian_flag") else ""
        dir_conv = (" ".join([p for p in [dirn, conv_s] if p]) + contra).strip() or "-"

        # 대상자산: asset_class/ticker 우선, 없으면 macro_theme 폴백.
        nodes = r.get("nodes", []) or []
        assets = [n["node_value"].strip("[]") for n in nodes
                  if n.get("node_type") in ("asset_class", "ticker") and n.get("node_value")]
        if not assets:
            assets = [n["node_value"].strip("[]") for n in nodes
                      if n.get("node_type") == "macro_theme" and n.get("node_value")]
        asset_s = ", ".join(assets) if assets else "-"

        horizon = (r.get("view_time_horizon") or "").strip()
        tbox = (r.get("time_box") or "").strip("[]").strip()
        valid_s = " · ".join([p for p in [horizon, tbox] if p]) if (horizon or tbox) else "-"

        # 핵심주장 — 전문 한국어 번역, 절삭 없음.
        thesis = _tr(tr_map, f"{vid}::thesis", (r.get("core_thesis") or "").strip())

        watch_url = f"https://www.youtube.com/watch?v={vid}" if vid else ""

        # 헤더: 번호 + 구루(소속). 메타는 한 줄로 축약(작게).
        lines.append(f"### {idx}. {guru_hdr}")
        meta = f"`{source}` · **{dir_conv}** · 대상: {asset_s} · 유효: {valid_s}"
        if watch_url:
            meta += f" · [영상]({watch_url})"
        lines.append(f"<sub>{meta}</sub>")
        lines.append("")
        # 핵심주장 — 가장 크게(blockquote + bold) 전문.
        if thesis:
            lines.append(f"> **{thesis}**")
        else:
            lines.append("> *(핵심주장 없음)*")
        lines.append("")
    return "\n".join(lines)


def _classify_regime(score: float) -> str:
    """1~10 방향 점수 기반의 대칭적인 투자 레짐 분류."""
    if score < 3.5:
        return "Extreme Panic / Cash Focus"
    if score < 4.75:
        return "Defensive Risk-Off"
    if score < 6.25:
        return "Neutral / Wait-and-See"
    if score < 7.5:
        return "Selective Quality Buy"
    return "Aggressive Risk-On"


def calculate_deterministic_sentiment(reports: list) -> dict:
    """24h 방향·합의도·신뢰도·테일리스크를 서로 섞지 않고 산출한다.

    방향 점수에는 sqrt(conviction) 가중치를 써 확신도 한 항목의 과도한 지배를
    줄인다. 고확신 약세는 방향 점수에서 고정 차감하지 않고 별도 위험 신호로
    표시한다. ``adjusted_score``는 기존 DB 스키마와 소비자 호환용 1~10 값이다.
    """
    pairs = []
    for r in reports:
        bb, conv = r.get("bull_bear_score"), r.get("conviction_score")
        if bb is None or conv is None:
            continue
        try:
            bb, conv = float(bb), float(conv)
        except (TypeError, ValueError):
            continue
        if 1 <= bb <= 10 and 0 < conv <= 10:
            pairs.append((bb, conv))

    n = len(pairs)
    if n == 0:
        return {"sample_count": 0, "raw_weighted_avg": None, "stddev": None,
                "tail_risk_count": 0, "tail_risk_ratio": 0.0,
                "tail_risk_label": "데이터 없음", "deduction": 0.0,
                "adjusted_score": None, "stance_score": None,
                "agreement_score": None, "confidence_label": "낮음",
                "sentiment_regime": "Neutral / Wait-and-See"}

    weighted = [(bb, math.sqrt(conv)) for bb, conv in pairs]
    total_w = sum(weight for _, weight in weighted)
    raw = sum(bb * weight for bb, weight in weighted) / total_w
    raw = max(1.0, min(10.0, raw))

    variance = sum(weight * ((bb - raw) ** 2) for bb, weight in weighted) / total_w
    std = math.sqrt(variance)
    agreement = round(max(0.0, min(100.0, 100.0 * (1.0 - std / 4.5))))

    tail = sum(1 for bb, conv in pairs if bb <= 4 and conv >= 8)
    tail_ratio = tail / n
    if tail_ratio >= 0.15:
        tail_label = "높음"
    elif tail_ratio >= 0.05:
        tail_label = "보통"
    else:
        tail_label = "낮음"

    if n >= 20 and agreement >= 55:
        confidence = "높음"
    elif n >= 8 and agreement >= 35:
        confidence = "보통"
    else:
        confidence = "낮음"

    adjusted = raw
    stance = round((adjusted - 1.0) / 9.0 * 100)

    return {
        "sample_count": n,
        "raw_weighted_avg": round(raw, 2),
        "stddev": round(std, 2),
        "tail_risk_count": tail,
        "tail_risk_ratio": round(tail_ratio, 3),
        "tail_risk_label": tail_label,
        "deduction": 0.0,
        "adjusted_score": round(adjusted, 2),
        "stance_score": stance,
        "agreement_score": agreement,
        "confidence_label": confidence,
        "sentiment_regime": _classify_regime(adjusted),
    }


def _store_daily_sentiment(db_path: str, date_str: str, sent: dict) -> None:
    """👑 [Ver 4.9] daily_sentiment 테이블에 오늘 심리지수 INSERT OR REPLACE."""
    try:
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT OR REPLACE INTO daily_sentiment
               (date, raw_weighted_avg, adjusted_score, sentiment_regime, sample_count, stddev, tail_risk_count)
               VALUES (?,?,?,?,?,?,?)""",
            (date_str, sent["raw_weighted_avg"], sent["adjusted_score"], sent["sentiment_regime"],
             sent["sample_count"], sent["stddev"], sent["tail_risk_count"]),
        )
        conn.commit()
        conn.close()
        print(f"   💾 daily_sentiment 저장: {date_str} (조정점수 {sent['adjusted_score']}, 레짐 {sent['sentiment_regime']})")
    except Exception as e:
        print(f"[WARN] daily_sentiment 저장 실패: {e}")


def _previous_stance_score(db_path: str, date_str: str) -> int | None:
    """오늘보다 이전인 가장 최근 방향 점수를 0~100으로 변환한다.

    과거 ``adjusted_score``에는 폐기한 고정 테일 차감이 섞여 있으므로 비교하지
    않고, 보정 전 방향을 담은 ``raw_weighted_avg``를 사용한다.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """SELECT raw_weighted_avg FROM daily_sentiment
                   WHERE date < ? AND raw_weighted_avg IS NOT NULL
                   ORDER BY date DESC LIMIT 1""",
                (date_str,),
            ).fetchone()
        if not row:
            return None
        return round((max(1.0, min(10.0, float(row[0]))) - 1.0) / 9.0 * 100)
    except (sqlite3.Error, TypeError, ValueError) as exc:
        print(f"[WARN] 전일 심리지수 조회 실패: {exc}")
        return None


def _format_stance_change(current: int | None, previous: int | None) -> str:
    """사람이 즉시 읽을 수 있는 전일 대비 표기."""
    if current is None or previous is None:
        return "비교 데이터 없음"
    delta = current - previous
    if delta == 0:
        return "변화 없음"
    return f"{delta:+d}p"


def generate_morning_report(db_path: str, vault_dir: str, api_key: str = None, lookback_hours: int = 24) -> str:
    # 1. Fetch data
    reports = fetch_past_24h_data(db_path, lookback_hours)
    if not reports:
        print(f"ℹ️ No new macro views collected in the last {lookback_hours} hours. Daily report generation skipped.")
        return ""

    print(f"📊 Gathered {len(reports)} guru views from the database.")

    # 2. Format LLM input feed
    feed_text = format_feed_payload(reports)

    # 3. Generate content
    print("🤖 Synthesizing daily consensus report via Ollama (deepseek-v4-flash:0731-cloud, NIM 폴백)...")

    # KST 보정 — 서버가 UTC라도 한국장 기준 날짜로 라벨링.
    today_str = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)).strftime("%Y-%m-%d")

    # 👑 [Ver 4.9] 결정론 심리지수 계산 → DB 영속화 → LLM 프롬프트에 값 주입 (하이브리드)
    sent = calculate_deterministic_sentiment(reports)
    previous_stance = _previous_stance_score(db_path, today_str)
    daily_change = _format_stance_change(sent["stance_score"], previous_stance)
    _store_daily_sentiment(db_path, today_str, sent)
    system_msg = SYSTEM_INSTRUCTION.format(
        stance_score=sent["stance_score"] if sent["stance_score"] is not None else "N/A",
        daily_change=daily_change,
        regime_name=sent["sentiment_regime"],
        sample_count=sent["sample_count"],
        agreement_score=sent["agreement_score"] if sent["agreement_score"] is not None else "N/A",
        confidence_label=sent["confidence_label"],
        tail_risk_label=sent["tail_risk_label"],
        tail_risk_count=sent["tail_risk_count"],
    )
    prompt = f"Today's Date: {today_str}\n\nHere is the raw input data:\n{feed_text}"

    # Provider retries and fallback are centralized to avoid retry-chain multiplication.
    report_content = complete_derived(DerivedLLMRequest(
        pipeline="daily", system=system_msg, user=prompt, max_tokens=4096,
        temperature=0.2, nim_model=TIER2_MODEL, ollama_attempts=5,
    )).content

    report_content = (report_content or "").strip()
    # 빈 응답 가드 — 빈 리포트 저장 방지.
    if not report_content:
        print("[WARN] LLM returned empty response — skipping save.")
        return ""

    # Double check/ensure date headers in report
    report_content = report_content.replace("YYYY-MM-DD", today_str)

    # 👑 [Ver 4.5] 섹션5/6 한국어 번역 맵 사전 생성(LLM 번역만, 재생성 금지).
    # 실패 시 원문 fallback 되므로 리포트 생성은 중단되지 않음.
    tr_items = _collect_translatable(reports)
    if tr_items:
        print(f"🌐 Translating {len(tr_items)} evidence texts to Korean via Ollama Cloud (Llama70BRouter, NIM 폴백)...")
        tr_map = _translate_to_korean_map(tr_items)
        orig_dict = dict(tr_items)
        success_tr = sum(1 for k, v in tr_map.items() if v != orig_dict.get(k))
        print(f"   translated {success_tr}/{len(tr_items)} items successfully.")
    else:
        tr_map = {}

    # 👑 [Ver 4.3] 결정론적 근거 부록 + frontmatter 부착(기사 원고화).
    # 근거(인용/촉매/리스크/링크)는 LLM 재생성이 아닌 DB 원본에서 부착.
    kst_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)
    kst_iso = kst_now.strftime("%Y-%m-%dT%H:%M:%S+09:00")
    section5 = _build_evidence_section(reports, top_k=8, tr_map=tr_map)
    section6 = _build_24h_summary_table(reports, tr_map=tr_map)
    frontmatter = _build_frontmatter(today_str, TIER2_MODEL, len(reports), kst_iso)

    # 일간 워드클라우드 + TOP 키워드: 핵심 분석을 방해하지 않도록 섹션 4 뒤에 배치.
    wc_section = ""
    try:
        _wcg = Path(__file__).resolve().parent.parent / "scripts" / "insights" / "wordcloud_generator.py"
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("wordcloud_generator", _wcg)
        _wcmod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_wcmod)
        wc_data = _wcmod.get_period_keywords(days=1)
        wc_table = _wcmod.get_top_keywords_table(days=1, top_k=10, data=wc_data)
        wc_image = _wcmod.generate_wordcloud_image(days=1, data=wc_data) or ""
        if wc_table and "키워드 데이터 없음" not in wc_table:
            wc_section = (
                "\n---\n\n## 오늘의 담론 분포 (Word Cloud)\n\n"
                "> 언급 빈도이며 중요도·시장 방향의 순위가 아닙니다.\n\n"
                + wc_table + "\n"
            )
            if wc_image:
                wc_section += f"\n![워드클라우드]({wc_image})\n"
    except Exception as _we:
        print(f"[WARN] 워드클라우드 섹션 실패: {_we}")

    report_body = report_content

    # 파일: frontmatter + 핵심본문 + 워드클라우드 + 근거 + 전체부록.
    # 이메일은 동일한 정보 순서를 유지하되 YAML만 노출하지 않는다.
    file_content, email_body = _assemble_daily_outputs(
        frontmatter, report_body, section5, section6, wordcloud=wc_section,
    )

    # 5. Export to Obsidian
    report_file_path = write_report_artifact(daily_artifact(vault_dir, today_str, file_content))
    print(f"✓ Daily report successfully saved to: {report_file_path}")

    # Send email notification
    email_subject = f"📊 일일 매크로 종합 보고서 (Daily Macro Synthesis Report) - {today_str}"
    send_email_report(email_subject, email_body)

    return file_content

def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Morning Macro Synthesis Agent")
    parser.add_argument("--db_path", default="data/macro_knowledge.db", help="SQLite DB path")
    parser.add_argument("--vault_dir", default="obsidian_vault", help="Obsidian Vault dir path")
    parser.add_argument("--lookback_hours", type=int, default=24, help="Hours to look back for new views")
    parser.add_argument("--event-log", "--event_log", dest="event_log",
                        help="Append report lifecycle events to this JSONL file")
    args = parser.parse_args(argv)
    
    project_dir = Path(__file__).resolve().parent.parent
    db_abs = project_dir / args.db_path
    vault_abs = project_dir / args.vault_dir
    event_path = project_dir / args.event_log if args.event_log else None
    journal = ReportRunJournal.from_path(event_path, "daily", warn=lambda message: print(f"[WARN] {message}"))
    journal.started()
    try:
        generate_morning_report(str(db_abs), str(vault_abs), lookback_hours=args.lookback_hours)
    except Exception as e:
        print(f"❌ Error generating report: {e}")
        journal.finished(success=False, stage="report", error=e)
        return 1
    journal.finished(success=True, stage="delivery")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
