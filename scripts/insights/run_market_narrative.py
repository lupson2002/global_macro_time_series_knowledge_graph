# -*- coding: utf-8 -*-
"""
run_market_narrative.py — 마켓 내러티브 서치 엔진 리포트 실행기 (신규 독립 모듈)
================================================================================
market_narrative.generate_narrative_report() 로 내러티브 리포트를 생성한 뒤:
  1. `reports/narrative/market_narrative_YYYY-MM-DD.md` 저장
  2. Obsidian Vault `Narrative_Reports/Market_Narrative_YYYY-MM-DD.md` 동기화 (frontmatter)
  3. Gmail HTML 메일 발송 (기존 `_md_to_html_email` 재사용)

스케줄: systemd timer — 매주 수요일/일요일 06:00 KST (Persistent=true 로 부팅 시 누락분 자동 실행).

Usage:
    .venv/bin/python scripts/insights/run_market_narrative.py              # 전체 + 메일
    .venv/bin/python scripts/insights/run_market_narrative.py --no-send    # 리포트만
    .venv/bin/python scripts/insights/run_market_narrative.py --expiry all # 전체 DB
"""
from __future__ import annotations

import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import settings  # noqa: E402

# 같은 디렉터리 코어 엔진 (sys.path[0] = scripts/insights)
from market_narrative import generate_narrative_report, INSIGHT_MODEL  # noqa: E402
from src.report_generator import _md_to_html_email, _resolve_recipients  # noqa: E402

REPORTS_DIR = PROJECT_ROOT / "reports" / "narrative"
OBSIDIAN_DIR = PROJECT_ROOT / "obsidian_vault" / "Narrative_Reports"


def _kst_iso() -> str:
    """KST ISO8601 (UTC+9)."""
    now = datetime.now(timezone.utc) + timedelta(hours=9)
    return now.strftime("%Y-%m-%dT%H:%M:%S+09:00")


def _frontmatter(today: str, model: str, source_count: int = 0) -> str:
    return (
        "---\n"
        f"date: {today}\n"
        "type: market_narrative\n"
        f"model: {model}\n"
        "provider: nim\n"
        f"generated_at: {_kst_iso()}\n"
        "tags: [macro, narrative, market_bottleneck]\n"
        "---\n\n"
    )


def save_outputs(md: str, today: str) -> tuple[Path, Path]:
    """마크다운 저장 (reports/narrative + obsidian_vault/Narrative_Reports)."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    OBSIDIAN_DIR.mkdir(parents=True, exist_ok=True)

    report_path = REPORTS_DIR / f"market_narrative_{today}.md"
    report_path.write_text(md, encoding="utf-8")

    vault_path = OBSIDIAN_DIR / f"Market_Narrative_{today}.md"
    vault_path.write_text(_frontmatter(today, INSIGHT_MODEL) + md, encoding="utf-8")
    return report_path, vault_path


def send_narrative_email(md: str, subject: str) -> None:
    """내러티브 리포트 Gmail 발송 — 본문 plain+HTML(백링크 strip). 실패 시 경고만."""
    user = settings.email.user
    pwd = settings.email.password
    recipients = _resolve_recipients()
    host = settings.email.smtp_host
    port = settings.email.smtp_port
    if not all([user, pwd, recipients]):
        print("[INFO] Gmail 설정 없음 — 메일 스킵")
        return

    html_body = _md_to_html_email(md)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(md, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL(host, port, timeout=60) as s:
            s.login(user, pwd.replace(" ", ""))
            s.sendmail(user, recipients, msg.as_string())
        print(f"✓ 내러티브 메일 발송 완료 → {', '.join(recipients)}")
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ 내러티브 메일 발송 실패: {e}")


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Market Narrative Search Engine Report 실행기")
    ap.add_argument("--no-send", action="store_true", help="메일 미발송 (저장만)")
    ap.add_argument("--no-llm", action="store_true", help="LLM 스킵 — 원자료만")
    ap.add_argument("--expiry", choices=["timebox", "all"], default="timebox",
                    help="timebox=time_box 유효기간(기본) | all=전체 DB")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"🧠 마켓 내러티브 서치 엔진 리포트 시작 ({today})...")
    md = generate_narrative_report(
        use_timebox=args.expiry == "timebox", top_k=args.top_k, no_llm=args.no_llm,
    )

    report_path, vault_path = save_outputs(md, today)
    print(f"✓ 리포트: {report_path} ({report_path.stat().st_size} bytes)")
    print(f"✓ Obsidian 동기화: {vault_path}")

    if not args.no_send:
        subject = f"🎯 마켓 내러티브 & 핵심 병목 진단 리포트 - {today}"
        send_narrative_email(md, subject)

    print("🏁 내러티브 리포트 완료.")


if __name__ == "__main__":
    main()
