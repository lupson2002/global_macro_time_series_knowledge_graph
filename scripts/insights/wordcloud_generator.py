# -*- coding: utf-8 -*-
"""
wordcloud_generator.py — [일간] 워드 카운터 & 워드 클라우드 생성기 (신규 독립 모듈)
====================================================================================
최근 N일간 수집된 reports(core_thesis/verbatim_quote) + nodes(macro_theme/ticker) 텍스트를
추출해 매크로 금융 불용어를 제거하고 단어 빈도로 워드클라우드 PNG + TOP-N 키워드 표를 생성.

Usage:
    .venv/bin/python scripts/insights/wordcloud_generator.py --days 1
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "data" / "macro_knowledge.db"
OUTPUT_DIRS = [PROJECT_ROOT / "reports" / "wordclouds", PROJECT_ROOT / "obsidian_vault" / "wordclouds"]

# 한국어 포함 대비 NanumGothic (워드클라우드는 한글 미지원 폰트면 깨짐)
FONT_PATH = "/home/mikey/.local/share/fonts/NanumGothic-Bold.ttf"

# ── 매크로 금융 불용어 (시맨틱 없는 조사/동사/일반 시장용어) ──
MACRO_STOPWORDS = {
    "market", "markets", "think", "say", "says", "said", "fed", "rate", "rates",
    "inflation", "percent", "also", "would", "will", "cpi", "one", "get", "going",
    "make", "make", "like", "just", "really", "much", "many", "even", "still",
    "well", "now", "new", "way", "year", "years", "quarter", "quarterly", "month",
    "week", "day", "today", "price", "prices", "stock", "stocks", "bond", "bonds",
    "fund", "funds", "etf", "etfs", "company", "companies", "business", "economy",
    "economic", "financial", "money", "investor", "investors", "investment", "invest",
    "investing", "return", "returns", "risk", "risks", "level", "levels", "point",
    "data", "number", "numbers", "result", "results", "long", "short", "term",
    "time", "thing", "things", "good", "bad", "big", "small", "high", "low",
    "first", "second", "third", "last", "next", "make", "take", "give", "use",
    "could", "should", "might", "may", "can", "will", "going", "need", "need",
    "look", "looks", "looking", "come", "comes", "going", "know", "see", "saw",
    "going", "actually", "basically", "really", "sort", "kind", "type", "part",
    "lot", "lot", "bit", "etc", "e.g", "i.e", "vs", "versus", "vs", "vs",
}

# 추가 영어 일반 불용어 (NLTK 스톱워즈 수준)
_COMMON = {
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "for", "of",
    "on", "in", "to", "from", "by", "at", "as", "it", "its", "this", "that",
    "these", "those", "is", "are", "was", "were", "be", "been", "being", "have",
    "has", "had", "do", "does", "did", "i", "you", "we", "they", "he", "she",
    "them", "his", "her", "their", "our", "your", "my", "me", "us", "what",
    "which", "who", "whom", "when", "where", "why", "how", "all", "any", "both",
    "each", "few", "more", "most", "other", "some", "such", "no", "nor", "not",
    "only", "own", "same", "so", "than", "too", "very", "can", "just", "because",
    # 전치사/접속사/부사류 (의미 없는 단어 추가)
    "with", "without", "there", "here", "about", "into", "out", "over", "under",
    "between", "through", "during", "before", "after", "up", "down", "again",
    "further", "once", "twice", "around", "within", "along", "across", "toward",
    "towards", "while", "whereas", "whether", "although", "though", "via",
}

# 👑 [Ver 4.9] 감성 점수 고도화 — 형용사/동사/일반 노이즈 추가
EXTRA_STOPWORDS = {
    "deal", "strong", "driven", "growth", "policy", "say", "think",
    "market", "percent", "would", "also", "about", "year", "time",
    "due", "remains",
}
STOPWORDS = MACRO_STOPWORDS | _COMMON | EXTRA_STOPWORDS


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _clean_node(v: str) -> str:
    """[[Fed QT]] → Fed QT (백링크 문법 제거)."""
    return re.sub(r"[\[\]]", "", str(v or "")).strip()


def get_period_keywords(days: int = 1) -> dict:
    """최근 N일 단어별 {count, avg_score} 반환.

    - count: 단어가 등장한 **레코드 수** (레코드 내 중복은 1회)
    - avg_score: 단어가 등장한 레코드들의 **bull_bear_score 평균** (bull_bear 없는 레코드는 제외)
      → 평균 6.0↑ 강세 / 4.0↓ 약세 / 사이 중립
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT r.video_id, r.core_thesis, r.verbatim_quote, q.bull_bear_score "
            "FROM reports r LEFT JOIN quant_signals q ON r.video_id = q.video_id "
            "WHERE r.broadcast_date >= date('now', ?) AND r.core_thesis IS NOT NULL",
            (f"-{days} days",),
        ).fetchall()
        node_rows = conn.execute(
            "SELECT n.video_id, n.node_value FROM nodes n JOIN reports r ON n.video_id = r.video_id "
            "WHERE r.broadcast_date >= date('now', ?) AND n.node_type IN ('macro_theme','ticker')",
            (f"-{days} days",),
        ).fetchall()
    finally:
        conn.close()

    score_map = {r["video_id"]: r["bull_bear_score"] for r in rows}
    nodes_by_vid: dict[str, set[str]] = {}
    for n in node_rows:
        phrase = _clean_node(n["node_value"]).lower()
        if phrase and len(phrase.split()) <= 4:
            nodes_by_vid.setdefault(n["video_id"], set()).add(phrase)

    data: dict[str, dict] = {}
    _TOK = re.compile(r"[a-zA-Z]+")

    def _add(word: str, score) -> None:
        d = data.setdefault(word, {"count": 0, "sum": 0.0, "scored": 0})
        d["count"] += 1
        if score is not None:
            d["sum"] += float(score)
            d["scored"] += 1

    for r in rows:
        vid = r["video_id"]
        score = score_map.get(vid)
        # 노드 구문(테마/티커) — 레코드당 1회
        for phrase in nodes_by_vid.get(vid, []):
            _add(phrase, score)
        # core_thesis / verbatim_quote → 레코드 내 중복 단어 dedup 후 1회
        words: set[str] = set()
        for field in ("core_thesis", "verbatim_quote"):
            for tok in _TOK.findall(r[field] or ""):
                tok = tok.lower()
                if len(tok) < 3 or tok in STOPWORDS:
                    continue
                words.add(tok)
        for w in words:
            _add(w, score)

    return {
        w: {"count": d["count"], "avg_score": round(d["sum"] / d["scored"], 1) if d["scored"] else 5.0}
        for w, d in data.items()
    }


def _sentiment_label(score: float) -> str:
    """평균 심리 점수 → 감성 라벨."""
    if score >= 6.0:
        return "🟢 강세 (Bullish)"
    if score <= 4.0:
        return "🔴 약세 (Bearish)"
    return "⚪ 중립 (Neutral)"


def generate_wordcloud_image(days: int = 1, output_name: str = "wordcloud_daily.png",
                             data: dict | None = None) -> str | None:
    """최근 N일 키워드 워드클라우드 PNG 생성 — 단어별 감성 점수로 Green/Red/Gold 컬러링.
    reports/wordclouds + obsidian_vault/wordclouds 에 저장. 실패 시 None.
    data 를 주면 get_period_keywords 재계산 생략(효율). 반환: Obsidian vault 상대경로."""
    data = data or get_period_keywords(days)
    if not data:
        print(f"[WARN] 워드클라우드용 키워드 없음 (days={days})")
        return None

    freq = {w: d["count"] for w, d in data.items()}
    sent = {w: d["avg_score"] for w, d in data.items()}

    from wordcloud import WordCloud

    def sentiment_color_func(word, font_size, position, orientation, random_state=None, **kwargs):
        score = sent.get(word, 5.0)
        if score >= 6.0:
            return "#2ecc71"   # Bullish Green
        if score <= 4.0:
            return "#e74c3c"   # Bearish Red
        return "#f1c40f"       # Neutral Yellow/Gold

    for d in OUTPUT_DIRS:
        d.mkdir(parents=True, exist_ok=True)

    try:
        wc = WordCloud(
            font_path=FONT_PATH,
            width=1200, height=600,
            background_color="#0d1117",  # 다크모드
            color_func=sentiment_color_func,
            max_words=120,
            prefer_horizontal=0.8,
            random_state=42,
        ).generate_from_frequencies(freq)

        paths = [d / output_name for d in OUTPUT_DIRS]
        for p in paths:
            wc.to_file(str(p))
        print(f"   ✅ 워드클라우드 생성 ({len(freq)}단어, 감성컬러): {paths[0]}")
        # 👑 Obsidian vault 상대경로 반환 — MD(reports/ 밖)에서 이미지가 렌더되도록
        return f"wordclouds/{output_name}"
    except Exception as e:
        print(f"[WARN] 워드클라우드 생성 실패: {e}")
        return None


def get_top_keywords_table(days: int = 1, top_k: int = 10, data: dict | None = None) -> str:
    """최근 N일 핵심 키워드/노드 TOP-k 마크다운 표 — 언급수 + 평균 심리점수 + 감성.
    data 를 주면 get_period_keywords 재계산 생략(효율)."""
    data = data or get_period_keywords(days)
    if not data:
        return "*(키워드 데이터 없음)*"
    top = sorted(data.items(), key=lambda x: -x[1]["count"])[:top_k]
    lines = [
        f"### 🔤 최근 {days}일 핵심 키워드 TOP {len(top)}",
        "",
        "| # | 키워드 | 언급 횟수 | 평균 심리 점수 | 감성 (Sentiment) |",
        "|---|--------|----------|--------------|----------------|",
    ]
    for i, (kw, d) in enumerate(top, 1):
        s = d["avg_score"]
        lines.append(f"| {i} | **{kw}** | {d['count']}회 | {s} / 10 | {_sentiment_label(s)} |")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="일간 워드카운터/워드클라우드")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--table", action="store_true", help="TOP 키워드 표만 출력")
    args = ap.parse_args()

    if args.table:
        print(get_top_keywords_table(days=args.days))
    else:
        print(get_top_keywords_table(days=args.days, top_k=10))
        print()
        print(generate_wordcloud_image(days=args.days) or "(생성 실패)")
