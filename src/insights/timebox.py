"""time_box 유효기간 파싱 + 유효 판정.

time_box 는 구루 전망의 타겟 기간 = 유효기간. 오늘이 기간 end 이전/내이면 유효, 기간이 과거에 끝났으면 만료.

형식:
  [[YYYY]]      → YYYY-01-01 ~ YYYY-12-31
  [[YYYY-H1|H2]]→ H1: 01-01~06-30, H2: 07-01~12-31
  [[YYYY-Q1..Q4]]→ Q1:01-03 Q2:04-06 Q3:07-09 Q4:10-12 (월 말일)
  [[YYYY-YYYY]] → start YYYY-01-01 ~ end YYYY-12-31 (연도 범위)
빈값/None → 판정 불가 (기본 유효로 간주, 최근 자료).
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Optional, Tuple

_WRAPPER_RE = re.compile(r"^\[\[(.+)\]\]$")

# 분기 종료월 → (시작월, 종료월)
_Q_MONTHS = {"Q1": (1, 3), "Q2": (4, 6), "Q3": (7, 9), "Q4": (10, 12)}

# time_box 없는 자료의 발간일 기준 유효 기간 (일)
EMPTY_LOOKBACK_DAYS = 90


def _month_end(year: int, month: int) -> date:
    """해당 월의 마지막 날."""
    if month == 12:
        return date(year, 12, 31)
    nxt = date(year, month + 1, 1)
    return nxt - timedelta(days=1)


def parse_time_box(time_box: str) -> Optional[Tuple[date, date]]:
    """time_box → (start, end). 파싱 불가/빈 → None."""
    if not time_box:
        return None
    s = str(time_box).strip()
    m = _WRAPPER_RE.match(s)
    inner = m.group(1).strip() if m else s
    if not inner:
        return None

    # [[YYYY-YYYY]] 범위
    rng = re.match(r"^(\d{4})-(\d{4})$", inner)
    if rng:
        y1, y2 = int(rng.group(1)), int(rng.group(2))
        return date(y1, 1, 1), date(y2, 12, 31)

    # [[YYYY-HN]]
    h = re.match(r"^(\d{4})-H([12])$", inner)
    if h:
        y, half = int(h.group(1)), int(h.group(2))
        if half == 1:
            return date(y, 1, 1), date(y, 6, 30)
        return date(y, 7, 1), date(y, 12, 31)

    # [[YYYY-QN]]
    q = re.match(r"^(\d{4})-Q([1-4])$", inner)
    if q:
        y, qn = int(q.group(1)), q.group(2)
        sm, em = _Q_MONTHS["Q" + qn]
        return date(y, sm, 1), _month_end(y, em)

    # [[YYYY]]
    yonly = re.match(r"^(\d{4})$", inner)
    if yonly:
        y = int(yonly.group(1))
        return date(y, 1, 1), date(y, 12, 31)

    return None  # 파싱 불가


def is_valid_time_box(
    time_box: str,
    today: Optional[date] = None,
    broadcast_date: Optional[date] = None,
    empty_lookback_days: int = EMPTY_LOOKBACK_DAYS,
) -> bool:
    """유효 판정:
      - time_box 있고 오늘 <= end → 유효 (미래/현재 전망)
      - time_box 있고 만료 → False
      - time_box 없음(빈/파싱불가) → broadcast_date 기준 최근 empty_lookback_days 일 이내면 유효.
        broadcast_date 모르면 관대하게 True (최근 자료로 간주).
    """
    today = today or date.today()
    if time_box and str(time_box).strip():
        rng = parse_time_box(time_box)
        if rng is not None:
            _, end = rng
            return today <= end
        # 파싱 불가 time_box → 빈값과 동일 취급
    # 빈값/파싱불가 → broadcast_date 기준 90일
    if broadcast_date is None:
        return True  # broadcast_date 모르면 관대 포함
    cutoff = today - timedelta(days=empty_lookback_days)
    return broadcast_date >= cutoff


def valid_time_box_values(today: Optional[date] = None) -> list[str]:
    """DB 에서 유효한 time_box 값 리스트(빈값 제외). IN 절용."""
    import sqlite3
    from pathlib import Path

    db = Path(__file__).resolve().parent.parent.parent / "data" / "macro_knowledge.db"
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT DISTINCT time_box FROM reports WHERE time_box IS NOT NULL AND time_box != ''").fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows if is_valid_time_box(r[0], today)]


if __name__ == "__main__":
    samples = ["[[2026-H2]]", "[[2026]]", "[[2026-Q2]]", "[[2026-Q3]]", "[[2027]]",
               "[[2024-2027]]", "[[2017-2020]]", "[[2025-H1]]", "", "[[2023-H2]]"]
    t = date(2026, 7, 8)
    for s in samples:
        rng = parse_time_box(s)
        v = is_valid_time_box(s, t)
        print(f"  {s!r:18} → {rng} → 유효={v}")