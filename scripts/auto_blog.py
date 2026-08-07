#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""블로그 발행 완전 자동화 오케스트레이터 (안C).

흐름:
  1. 중복방지: data/blog_publish.db 에 오늘 양 플랫폼 OK 있으면 스킵 + 이메일
  2. 원고 생성: generate_blog_draft.py --days 7 → tistory_draft.md
  3. 원고 아카이브: blog_drafts/YYYY-MM-DD_HHMM.md
  4. 발행: publish_all_blogs.py --headed (xvfb-run 가상 디스플레이 권장)
  5. 결과 파싱 → blog_publish_log 적재 → 이메일 알림(성공/실패+스크린샷)

사용:
  .venv/bin/python scripts/auto_blog.py            # 정상(중복시 스킵)
  .venv/bin/python scripts/auto_blog.py --force    # 중복 무시 강제 실행
  .venv/bin/python scripts/auto_blog.py --dry-run   # 발행 스킵, 원고+아카이브만
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import shutil
import smtplib
import sqlite3
import subprocess
import sys
from email.mime.text import MIMEText
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))
from src.config import settings

VENV_PY = PROJECT_DIR / ".venv" / "bin" / "python"
DB_PATH = PROJECT_DIR / "data" / "blog_publish.db"
DRAFT_PATH = PROJECT_DIR / "tistory_draft.md"
ARCHIVE_DIR = PROJECT_DIR / "blog_drafts"
LOG_DIR = PROJECT_DIR / "logs"
RESULT_JSON = LOG_DIR / "blog_publish_result.json"

PLATFORMS = ("naver", "tistory")
DAYS_DEFAULT = 7


def now_kst() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=9)))


def today_kst_str() -> str:
    return now_kst().strftime("%Y-%m-%d")


# ── DB ───────────────────────────────────────────────────────────
def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(DB_PATH)) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS blog_publish_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT, published_at TEXT, platform TEXT,
            status TEXT, url TEXT, title TEXT, error TEXT, screenshot TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_run_platform ON blog_publish_log(run_date, platform)")


def already_published_today(platform: str) -> bool:
    today = today_kst_str()
    with sqlite3.connect(str(DB_PATH)) as c:
        row = c.execute(
            "SELECT 1 FROM blog_publish_log WHERE run_date=? AND platform=? AND status='OK' LIMIT 1",
            (today, platform),
        ).fetchone()
    return row is not None


def insert_log(platform: str, status: str, url: str, title: str, error: str, screenshot: str) -> None:
    with sqlite3.connect(str(DB_PATH)) as c:
        c.execute(
            "INSERT INTO blog_publish_log(run_date, published_at, platform, status, url, title, error, screenshot) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (today_kst_str(), now_kst().isoformat(timespec="seconds"), platform, status, url, title, error, screenshot),
        )


# ── 이메일 알림 ───────────────────────────────────────────────────
def _resolve_recipients() -> list[str]:
    """이메일 수신자 목록 — EMAIL_TO(콤마 구분 복수 수신자) 우선, 없으면 GMAIL_USER 자기발송."""
    return list(settings.email.recipients)


def send_email(subject: str, body: str) -> None:
    user = settings.email.user
    pw = settings.email.password
    if not user or not pw:
        print("[이메일] GMAIL_USER/GMAIL_APP_PASSWORD 미설정 — 스킵")
        return
    recipients = _resolve_recipients()
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["From"] = user
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        with smtplib.SMTP_SSL(settings.email.smtp_host, settings.email.smtp_port) as s:
            s.login(user, pw.replace(" ", ""))
            s.sendmail(user, recipients, msg.as_string())
        print(f"[이메일] 발송 완료: {subject} → {', '.join(recipients)}")
    except Exception as e:
        print(f"[이메일] 발송 실패: {e}")


# ── 단계 ──────────────────────────────────────────────────────────
def step_generate_draft(days: int) -> bool:
    print(f"\n[1] 원고 생성: generate_blog_draft.py --days {days}")
    cmd = [str(VENV_PY), str(PROJECT_DIR / "scripts" / "generate_blog_draft.py"), "--days", str(days)]
    r = subprocess.run(cmd, cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=300)
    print(r.stdout[-1500:])
    if r.returncode != 0 or not DRAFT_PATH.exists():
        print(f"[1] 실패(rc={r.returncode})\n{r.stderr[-800:]}")
        return False
    print(f"[1] 완료: {DRAFT_PATH} ({DRAFT_PATH.stat().st_size}B)")
    return True


def step_archive() -> str | None:
    ts = now_kst().strftime("%Y-%m-%d_%H%M")
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    dst = ARCHIVE_DIR / f"{ts}.md"
    shutil.copy2(DRAFT_PATH, dst)
    print(f"[2] 아카이브: {dst}")
    return str(dst)


def step_publish() -> dict:
    print("\n[3] 발행: publish_all_blogs.py --headed (xvfb-run)")
    # xvfb-run -a 로 가상 디스플레이 + headed(봇탐지 회피 + 클립보드)
    cmd = ["xvfb-run", "-a", str(VENV_PY), str(PROJECT_DIR / "publish_all_blogs.py")]
    r = subprocess.run(cmd, cwd=str(PROJECT_DIR), capture_output=True, text=True, timeout=600)
    print(r.stdout[-2000:])
    if r.returncode != 0:
        print(f"[3] publish 프로세스 비정상 종료(rc={r.returncode})\n{r.stderr[-800:]}")
    if RESULT_JSON.exists():
        try:
            return json.loads(RESULT_JSON.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[3] 결과 JSON 파싱 실패: {e}")
    return {}


def build_email_body(results: dict, draft_archive: str | None) -> str:
    title = (results.get("naver") or {}).get("title") or "(제목 미확보)"
    lines = [
        f"📰 블로그 자동 발행 리포트  [{today_kst_str()} {now_kst().strftime('%H:%M')} KST]",
        f"제목: {title}",
        "",
    ]
    any_fail = False
    for plat in PLATFORMS:
        r = results.get(plat) or {}
        st = r.get("status", "UNKNOWN")
        if st != "OK":
            any_fail = True
        lines.append(f"▶ {plat}: status={st}")
        if r.get("url"):
            lines.append(f"   url: {r['url']}")
        if r.get("error"):
            lines.append(f"   error: {r['error']}")
        if r.get("screenshot"):
            lines.append(f"   screenshot: {r['screenshot']}")
    if draft_archive:
        lines += ["", f"원고 아카이브: {draft_archive}"]
    if any_fail:
        lines += ["", "⚠️ 일부/전체 실패 — 세션 만료 시 publish_all_blogs.py --login 으로 재로그인 필요."]
    lines += ["", "— global_macro_time_series_knowledge_graph / auto_blog.py"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="블로그 발행 완전 자동화")
    ap.add_argument("--days", type=int, default=DAYS_DEFAULT)
    ap.add_argument("--force", action="store_true", help="오늘 발행 이미 완료여도 강제 실행")
    ap.add_argument("--dry-run", action="store_true", help="발행 스킵(원고+아카이브만)")
    args = ap.parse_args(argv)

    init_db()
    print(f"=== auto_blog 시작 {now_kst().isoformat(timespec='seconds')} (force={args.force}, dry={args.dry_run}) ===")

    # 0) 중복방지
    if not args.force and not args.dry_run:
        done = [p for p in PLATFORMS if already_published_today(p)]
        if len(done) == len(PLATFORMS):
            msg = f"[중복방지] 오늘({today_kst_str()}) 양 플랫폼 이미 발행 완료({', '.join(done)}) — 스킵"
            print(msg)
            send_email(f"[블로그발행] 스킵(이미 완료) {today_kst_str()}", msg)
            return 0

    # 1) 원고 생성
    if not step_generate_draft(args.days):
        send_email(f"[블로그발행] 실패-원고생성 {today_kst_str()}", "원고 생성 실패. DB reports / NIM 상태 확인 필요.")
        return 1

    # 2) 아카이브
    archive = step_archive()

    if args.dry_run:
        print("\n[dry-run] 발행 스킵 — 원고+아카이브만 완료.")
        send_email(f"[블로그발행] dry-run 완료 {today_kst_str()}", f"원고: {DRAFT_PATH}\n아카이브: {archive}")
        return 0

    # 3) 발행
    results = step_publish()

    # 4) 로그 적재
    for plat in PLATFORMS:
        r = results.get(plat) or {}
        insert_log(plat, r.get("status", "UNKNOWN"), r.get("url", ""), r.get("title", ""),
                   r.get("error", ""), r.get("screenshot", ""))

    # 5) 이메일 알림
    body = build_email_body(results, archive)
    all_ok = all((results.get(p) or {}).get("status") == "OK" for p in PLATFORMS)
    subj = f"[블로그발행] {'성공' if all_ok else '실패/부분실패'} {today_kst_str()}"
    send_email(subj, body)

    print(f"\n=== auto_blog 종료 (all_ok={all_ok}) ===")
    return 0 if all_ok else 2


if __name__ == "__main__":
    sys.exit(main())
