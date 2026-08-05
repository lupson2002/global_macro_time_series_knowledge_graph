"""비파괴 동의어 정규화 — DB 직접 UPDATE 없이 분석 시 node_value/channel/speaker 정규화.

데이터 품질 이슈: NVDA/Nvidia/NVIDIA, Equities/Stocks, AI/Artificial Intelligence,
채널 표기 불일치(Bloomberg Markets & Finance / and Finance / Finance 등).
정규화 후 상위 빈도/심리 재산출이 의미를 가짐.
"""
from __future__ import annotations

import re
from typing import Optional

# node_value 는 "[[X]]" 래퍼 형태로 저장. 먼저 언랩.
_WRAPPER_RE = re.compile(r"^\[\[(.+)\]\]$")


def _unwrap(value: str) -> str:
    """[[X]] → X. 빈 문자열/None → ''."""
    if not value:
        return ""
    s = str(value).strip()
    m = _WRAPPER_RE.match(s)
    return m.group(1).strip() if m else s


# --- asset_class 정규화군 ---
ASSET_MAP: dict[str, str] = {
    # Equities 계열
    "Stocks": "Equities",
    "Equity": "Equities",
    "Stock Market": "Equities",
    "Growth Stocks": "Equities",
    "Technology Stocks": "Tech Equities",
    "Technology": "Tech Equities",
    "Technology Sector": "Tech Equities",
    "Technology Equities": "Tech Equities",
    # Bonds 계열
    "Fixed Income": "Bonds",
    "Government Bonds": "Bonds",
    "Sovereign Bonds": "Bonds",
    # Crypto
    "Cryptocurrency": "Cryptocurrencies",
    # Commodities 하위
    "Crude Oil": "Oil",
    "Precious Metals": "Gold",
    "Agricultural Commodities": "Agri Commodities",
    "Energy": "Energy",
}

# --- ticker 정규화군 ---
TICKER_MAP: dict[str, str] = {
    "NVIDIA": "NVDA",
    "Nvidia": "NVDA",
    "Microsoft": "MSFT",
    "SAMSUNG": "Samsung",
    "SK HYNIX": "SK Hynix",
    "Tesla": "TSLA",
    "Meta": "META",
    "Google": "GOOGL",
    "Broadcom": "AVGO",
    "Apple": "AAPL",
    "Intel": "INTC",
    "SPACEX": "SpaceX",
    "S&P 500": "SPX",
}

# --- macro_theme 정규화군 ---
THEME_MAP: dict[str, str] = {
    # AI 군
    "Artificial Intelligence": "AI",
    "AI Adoption": "AI",
    "AI Boom": "AI",
    "AI Bubble": "AI",
    "AI Revolution": "AI",
    "AI Trade": "AI",
    "AI Spending": "AI",
    "AI Investment": "AI",
    "AI Growth": "AI",
    # Monetary Policy 군
    "Fed QT": "Monetary Policy",
    "Fed Policy": "Monetary Policy",
    "Interest Rates": "Monetary Policy",
    # Geopolitics 군
    "Geopolitical Risk": "Geopolitics",
    "Geopolitical Tension": "Geopolitics",
    # Tech 군
    "Technological Disruption": "Tech Disruption",
    "Technological Advancement": "Tech Disruption",
    # Energy
    "Energy Crisis": "Energy",
}

# --- source_channel 정규화군 ---
CHANNEL_MAP: dict[str, str] = {
    "Bloomberg": "Bloomberg_Other",
    "Bloomberg Technology": "Bloomberg_Technology",
    "Bloomberg Tech": "Bloomberg_Technology",
    "Bloomberg Markets & Finance": "Bloomberg_Markets_Finance",
    "Bloomberg Markets and Finance": "Bloomberg_Markets_Finance",
    "Bloomberg Markets Finance": "Bloomberg_Markets_Finance",
    "Bloomberg Podcasts": "Bloomberg_Podcasts",
    "Bloomberg Surveillance": "Bloomberg_Other",
    "Bloomberg Real Yield": "Bloomberg_Other",
    "Bloomberg Daybreak Europe": "Bloomberg_Other",
    "Real Vision": "Real_Vision",
    "CNBC_Bloomberg": "CNBC",
}

# 매핑 키는 언랩된 원본 기준. 미정의 시 원본 반환.
_MAPS = {
    "asset_class": ASSET_MAP,
    "ticker": TICKER_MAP,
    "macro_theme": THEME_MAP,
}


def normalize_node(value: str, node_type: str) -> str:
    """node_value 를 node_type 에 맞춰 정규화. [[X]] 언랩 후 매핑, 미정의 시 언랩만."""
    s = _unwrap(value)
    if not s:
        return ""
    mp = _MAPS.get(node_type, {})
    return mp.get(s, s)


def normalize_channel(name: str) -> str:
    """source_channel 정규화."""
    s = (name or "").strip()
    return CHANNEL_MAP.get(s, s)


def normalize_speaker(name: str) -> str:
    """speaker_name 소문자 정규화 + Unknown/Reporter 계열 통일."""
    s = (name or "").strip()
    low = s.lower()
    if not s or low in ("unknown", "unknown speaker", "speaker", "analyst"):
        return "Unknown"
    if "reporter" in low or "correspondent" in low or "anchor" in low or "host" in low:
        return "Reporter/Anchor"
    return s


def normalize_factor(value: str) -> Optional[str]:
    """sector_tilt/macro_factor 정규화 — [[X]] 언랩 + 공백/None → None."""
    s = _unwrap(value)
    if not s or s.lower() in ("none", "n/a", ""):
        return None
    return s