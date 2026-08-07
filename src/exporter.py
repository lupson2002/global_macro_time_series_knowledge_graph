# -*- coding: utf-8 -*-
"""
Exporter Module for Global Macro Time-Series Knowledge Graph
===========================================================
Dual exporters:
1. SQLite: Stores consensus & numeric view details in structured relational tables.
2. Obsidian MD: Formats extracted views into backlinked Markdown files.
"""

import json
import sqlite3
import re
from pathlib import Path

from src.domain import MacroView
from src.json_utils import parse_json_list

# ---------------------------------------------------------------------------
# 1. SQLite Exporter
# ---------------------------------------------------------------------------
class SQLiteExporter:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_tables()

    def _init_tables(self):
        """Initializes tables for relational storage.
        Ver 3.0: quant_signals has multi-dimensional tactical fields
        (sector_tilt, duration_call, macro_factor) for richer backtesting.
        """
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")
        cursor = conn.cursor()

        # Reports / Views table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                video_id TEXT PRIMARY KEY,
                speaker_name TEXT,
                speaker_role TEXT,
                source_channel TEXT,
                broadcast_date TEXT,
                time_box TEXT,
                core_thesis TEXT,
                verbatim_quote TEXT,
                conditional_catalysts TEXT,
                invalidation_risks TEXT,
                key_data_points TEXT,
                additional_quotes TEXT,
                price_targets TEXT,
                speaker_institution TEXT,
                expectation_gap TEXT,
                causal_chain TEXT,
                tracking_indicators TEXT,
                tactical_stance TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Graph nodes link table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id TEXT,
                node_type TEXT, -- 'macro_theme', 'asset_class', 'ticker'
                node_value TEXT, -- e.g., '[[Fed QT]]'
                FOREIGN KEY(video_id) REFERENCES reports(video_id) ON DELETE CASCADE
            )
        """)

        # Quantitative signals table (Ver 3.0: multi-dimensional tactical view)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS quant_signals (
                video_id TEXT PRIMARY KEY,
                bull_bear_score INTEGER,
                conviction_score INTEGER,
                contrarian_flag INTEGER, -- 0 or 1
                sector_tilt TEXT,        -- e.g., '[[AI Infrastructure]]', '[[Energy]]', '[[Financials]]'
                duration_call TEXT,      -- 'Short' | 'Neutral' | 'Long'
                macro_factor TEXT,       -- 'Growth' | 'Inflation' | 'Liquidity'
                view_time_horizon TEXT,  -- 👑 [Ver 4.4] 'Days'|'Weeks'|'Months'|'Years'
                FOREIGN KEY(video_id) REFERENCES reports(video_id) ON DELETE CASCADE
            )
        """)

        # 👑 [Ver 3.0 Migration] ALTER TABLE additive for existing DBs.
        # SQLite supports ADD COLUMN. Skip silently if column already exists.
        new_columns = [
            ("sector_tilt", "TEXT"),
            ("duration_call", "TEXT"),
            ("macro_factor", "TEXT"),
            ("view_time_horizon", "TEXT"),  # 👑 [Ver 4.4]
        ]
        for col_name, col_type in new_columns:
            try:
                cursor.execute(
                    f"ALTER TABLE quant_signals ADD COLUMN {col_name} {col_type}"
                )
            except sqlite3.OperationalError:
                # Column already exists (duplicate column name) → safe skip.
                pass

        # 👑 [Ver 4.3] reports 스키마 확장 — conditional_catalysts / invalidation_risks
        # 영구화(이전엔 DB 미저장 → 백필 손실). 리스트 → JSON 문자열 저장. 기존 행은 NULL.
        # 👑 [Ver 4.4] 증거 필드 추가 — key_data_points / additional_quotes / price_targets(JSON) + speaker_institution.
        report_new_columns = [
            ("conditional_catalysts", "TEXT"),
            ("invalidation_risks", "TEXT"),
            ("key_data_points", "TEXT"),
            ("additional_quotes", "TEXT"),
            ("price_targets", "TEXT"),
            ("speaker_institution", "TEXT"),
            ("expectation_gap", "TEXT"),
            ("causal_chain", "TEXT"),
            ("tracking_indicators", "TEXT"),
            ("tactical_stance", "TEXT"),
        ]
        for col_name, col_type in report_new_columns:
            try:
                cursor.execute(
                    f"ALTER TABLE reports ADD COLUMN {col_name} {col_type}"
                )
            except sqlite3.OperationalError:
                # Column already exists (duplicate column name) → safe skip.
                pass

        # 👑 [Ver 4.9] 일일 종합 심리지수 시계열 — Python 결정론 가중평균 + DB 영속화
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_sentiment (
                date TEXT PRIMARY KEY,
                raw_weighted_avg REAL,
                adjusted_score REAL,
                sentiment_regime TEXT,
                sample_count INTEGER,
                stddev REAL,
                tail_risk_count INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 👑 [2026-08-06 H2] 게이트키퍼로 스킵된 영상 영속화 — 6시간 크론마다
        # 동일 영상 재다운로드+재LLM 호출 방지 (멱등화).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS skipped_videos (
                video_id TEXT PRIMARY KEY,
                reason TEXT,
                skipped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create helper indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_video ON nodes(video_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_reports_speaker ON reports(speaker_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(broadcast_date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_factor ON quant_signals(macro_factor)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_duration ON quant_signals(duration_call)")

        conn.commit()
        conn.close()

    def mark_skipped(self, video_id: str, reason: str = "not_macro_relevant"):
        """👑 [2026-08-06 H2] 게이트키퍼 스킵 영상을 영속화 — 다음 크론에서 재수집 방지."""
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO skipped_videos (video_id, reason) VALUES (?, ?)",
                    (video_id, reason),
                )
        except sqlite3.OperationalError:
            # 구DB에 테이블 미존재 시 안전 무시 (재수집되지만 크래시는 없음)
            pass
        finally:
            conn.close()

    def export_data(self, data: dict):
        """Inserts or replaces the extracted view data into SQLite tables."""
        view = MacroView.from_mapping(data)
        metadata = view.metadata
        graph_nodes = view.graph_nodes
        quant_signals = view.quant_signals
        view_details = view.view_details
        
        video_id = view.video_id
        if not video_id:
            raise ValueError("Cannot export to SQLite: Missing video_id in metadata.")

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        try:
            # 1. Insert into reports
            # 👑 [Ver 4.3] conditional_catalysts / invalidation_risks 영구화 — 리스트 → JSON.
            # 👑 [Ver 4.4] key_data_points / additional_quotes / price_targets(JSON) + speaker_institution.
            cats_json = json.dumps(
                view.section_list(view_details, "conditional_catalysts"), ensure_ascii=False
            )
            risks_json = json.dumps(
                view.section_list(view_details, "invalidation_risks"), ensure_ascii=False
            )
            kdp_json = json.dumps(
                view.section_list(view_details, "key_data_points"), ensure_ascii=False
            )
            aq_json = json.dumps(
                view.section_list(view_details, "additional_quotes"), ensure_ascii=False
            )
            pt_json = json.dumps(
                view.section_list(view_details, "price_targets"), ensure_ascii=False
            )
            # 👑 [Ver 4.7] 4대 내러티브 필드 영구화 — 리스트 → JSON. 기존 행은 NULL.
            exp_gap = view.raw.get("expectation_gap")
            causal_json = json.dumps(view.list_value("causal_chain"), ensure_ascii=False)
            tracking_json = json.dumps(
                view.list_value("tracking_indicators"), ensure_ascii=False
            )
            tactical_json = json.dumps(
                view.list_value("tactical_stance"), ensure_ascii=False
            )
            # 👑 [2026-08-06 H3] INSERT OR REPLACE → ON CONFLICT DO UPDATE:
            # OR REPLACE 는 행 삭제 후 재삽입이라 created_at(CURRENT_TIMESTAMP)이
            # "지금"으로 리셋 → 백필 시 과거 영상이 24h 보고서에 오염. created_at 은
            # UPDATE 목록에서 제외해 최초 수집 시각 보존.
            cursor.execute("""
                INSERT INTO reports
                (video_id, speaker_name, speaker_role, source_channel, broadcast_date, time_box, core_thesis, verbatim_quote, conditional_catalysts, invalidation_risks, key_data_points, additional_quotes, price_targets, speaker_institution, expectation_gap, causal_chain, tracking_indicators, tactical_stance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(video_id) DO UPDATE SET
                    speaker_name = excluded.speaker_name,
                    speaker_role = excluded.speaker_role,
                    source_channel = excluded.source_channel,
                    broadcast_date = excluded.broadcast_date,
                    time_box = excluded.time_box,
                    core_thesis = excluded.core_thesis,
                    verbatim_quote = excluded.verbatim_quote,
                    conditional_catalysts = excluded.conditional_catalysts,
                    invalidation_risks = excluded.invalidation_risks,
                    key_data_points = excluded.key_data_points,
                    additional_quotes = excluded.additional_quotes,
                    price_targets = excluded.price_targets,
                    speaker_institution = excluded.speaker_institution,
                    expectation_gap = excluded.expectation_gap,
                    causal_chain = excluded.causal_chain,
                    tracking_indicators = excluded.tracking_indicators,
                    tactical_stance = excluded.tactical_stance
            """, (
                video_id,
                metadata.get("speaker_name"),
                metadata.get("speaker_role"),
                metadata.get("source_channel"),
                metadata.get("broadcast_date"),
                graph_nodes.get("time_box"),
                view_details.get("core_thesis"),
                view_details.get("verbatim_quote"),
                cats_json,
                risks_json,
                kdp_json,
                aq_json,
                pt_json,
                metadata.get("speaker_institution") or "",
                exp_gap,
                causal_json,
                tracking_json,
                tactical_json,
            ))

            # 2. Insert into quant_signals (Ver 3.0: tactical multi-dimensional)
            cursor.execute("""
                INSERT OR REPLACE INTO quant_signals
                (video_id, bull_bear_score, conviction_score, contrarian_flag,
                 sector_tilt, duration_call, macro_factor, view_time_horizon)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                video_id,
                quant_signals.get("bull_bear_score"),
                quant_signals.get("conviction_score"),
                1 if quant_signals.get("contrarian_flag") else 0,
                quant_signals.get("sector_tilt"),
                quant_signals.get("duration_call"),
                quant_signals.get("macro_factor"),
                quant_signals.get("view_time_horizon") or "",
            ))

            # 3. Clean and Insert into nodes (delete past references first for clean updates)
            cursor.execute("DELETE FROM nodes WHERE video_id = ?", (video_id,))
            
            # Helper to insert list elements
            def insert_nodes(items, node_type):
                if not items or not isinstance(items, list):
                    return
                for item in items:
                    cursor.execute("""
                        INSERT INTO nodes (video_id, node_type, node_value)
                        VALUES (?, ?, ?)
                    """, (video_id, node_type, item.strip()))

            insert_nodes(view.section_list(graph_nodes, "macro_themes"), "macro_theme")
            insert_nodes(view.section_list(graph_nodes, "asset_classes"), "asset_class")
            insert_nodes(view.section_list(graph_nodes, "specific_tickers"), "ticker")

            conn.commit()

        except Exception as e:
            conn.rollback()
            raise RuntimeError(f"Database insertion failed: {e}")
        finally:
            conn.close()
class ObsidianMDExporter:
    def __init__(self, vault_path: str):
        self.vault_path = Path(vault_path)
        self.vault_path.mkdir(parents=True, exist_ok=True)

    def _sanitize_filename(self, filename: str) -> str:
        """Removes illegal characters from system filenames."""
        return re.sub(r'[\\/*?:"<>|]', "_", filename)

    def export_markdown(self, data: dict) -> Path:
        """Generates an Obsidian Markdown file conforming to the required template."""
        view = MacroView.from_mapping(data)
        metadata = view.metadata
        graph_nodes = view.graph_nodes
        quant_signals = view.quant_signals
        view_details = view.view_details

        speaker = metadata.get("speaker_name", "Unknown_Speaker")
        date_str = metadata.get("broadcast_date") or "Unknown_Date"
        video_id = metadata.get("video_id", "Unknown_ID")
        
        # Build sanitized filename
        base_name = f"{speaker}_{date_str}_{video_id}"
        sanitized_name = self._sanitize_filename(base_name) + ".md"
        
        # Create date subdirectory inside vault path
        date_folder = self.vault_path / date_str
        date_folder.mkdir(parents=True, exist_ok=True)
        md_file_path = date_folder / sanitized_name

        # Parse nodes and lists to displayable formats
        time_box_raw = graph_nodes.get("time_box", "[[Unknown]]")
        # Extract plain version for YAML frontmatter (without brackets [[ ]])
        time_box_clean = time_box_raw.replace("[[", "").replace("]]", "")
        
        macro_themes_str = ", ".join(view.section_list(graph_nodes, "macro_themes"))
        asset_classes = view.section_list(graph_nodes, "asset_classes")
        specific_tickers = view.section_list(graph_nodes, "specific_tickers")
        
        # Merge asset classes and tickers for display
        assets_and_tickers_str = ", ".join(asset_classes + specific_tickers)

        # Build lists of catalysts and risks
        def list_to_bullets(items):
            if not items or not isinstance(items, list):
                return "* (None extracted)"
            return "\n".join([f"* {item.strip()}" for item in items if item])

        catalysts_bullets = list_to_bullets(
            view.section_list(view_details, "conditional_catalysts")
        )
        risks_bullets = list_to_bullets(
            view.section_list(view_details, "invalidation_risks")
        )

        # Escape double quotes to prevent YAML/Markdown breakdown
        core_thesis = view_details.get("core_thesis", "").replace('"', '\\"')
        verbatim_quote = view_details.get("verbatim_quote", "").replace('"', '\\"')

        # Build markdown text based on template (Ver 3.0: multi-dimensional signals)
        # Sanitize optional tactical fields for YAML safety (strip [[ ]] wrappers)
        def _clean_brackets(val):
            if not val or not isinstance(val, str):
                return ""
            return val.replace("[[", "").replace("]]", "")

        sector_tilt_raw = quant_signals.get("sector_tilt", "")
        sector_tilt_clean = _clean_brackets(sector_tilt_raw)
        duration_call = quant_signals.get("duration_call", "")
        macro_factor = quant_signals.get("macro_factor", "")
        # 👑 [Ver 4.4] 신규 증거 필드
        view_time_horizon = quant_signals.get("view_time_horizon", "")
        speaker_institution = metadata.get("speaker_institution", "")
        key_data_points = view.section_list(view_details, "key_data_points")
        additional_quotes = view.section_list(view_details, "additional_quotes")
        price_targets = view.section_list(view_details, "price_targets")

        # 👑 YAML frontmatter 안전 인용 — speaker/role/source/date 등에
        # : / # / \n 포함 시 YAML 깨짐 방지. 모든 문자열 값을 quote+escape.
        def _yaml_str(val) -> str:
            s = str(val) if val is not None else ""
            s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            return f'"{s}"'

        md_content = f"""---
speaker: {_yaml_str(speaker)}
role: {_yaml_str(metadata.get("speaker_role", ""))}
source: {_yaml_str(metadata.get("source_channel", ""))}
date: {_yaml_str(date_str)}
time_box: {_yaml_str(time_box_clean)}
bull_bear_score: {quant_signals.get("bull_bear_score", 5)}
conviction_score: {quant_signals.get("conviction_score", 5)}
contrarian: {str(quant_signals.get("contrarian_flag", False)).lower()}
sector_tilt: {_yaml_str(sector_tilt_clean) if sector_tilt_clean else '"N/A"'}
duration_call: {_yaml_str(duration_call) if duration_call else '"N/A"'}
macro_factor: {_yaml_str(macro_factor) if macro_factor else '"N/A"'}
view_time_horizon: {_yaml_str(view_time_horizon) if view_time_horizon else '"N/A"'}
speaker_institution: {_yaml_str(speaker_institution) if speaker_institution else '"N/A"'}
tags: [macro_view, system_generated]
---

# 📊 Core Thesis (핵심 논거)
화자 [[{speaker}]]는 **{time_box_raw}**를 타겟으로 다음과 같이 주장합니다:
> "{core_thesis}"

### 🎯 전술적 자산배분 시그널 (Ver 3.0 Tactical Signals)
* **섹터 과체중/축소:** {sector_tilt_raw if sector_tilt_raw else "_(명시적 언급 없음)_"}
* **채권 듀레이션 콜:** **{duration_call if duration_call else "_(명시 없음)_"}** (Short / Neutral / Long)
* **핵심 매크로 드라이버:** **{macro_factor if macro_factor else "_(명시 없음)_"}** (Growth / Inflation / Liquidity)
* **뷰 시간지평:** **{view_time_horizon if view_time_horizon else "_(명시 없음)_"}** (Days / Weeks / Months / Years)

### 🌐 연결 노드 (Graph Nodes)
* **매크로 테마:** {macro_themes_str if macro_themes_str else "(None)"}
* **자산군 및 티커:** {assets_and_tickers_str if assets_and_tickers_str else "(None)"}

### ⚡ 촉매제 및 리스크 (Catalysts & Risks)
**조건부 촉매제 (이것이 발생하면 상승/하락한다):**
{catalysts_bullets}

**무효화 리스크 (이 뷰가 틀릴 수 있는 조건):**
{risks_bullets}

### 🎙️ 원문 발췌 (Verbatim Quote)
> "{verbatim_quote}"
*(출처: {metadata.get("source_channel", "")} - {video_id})*

### 📈 핵심 데이터 포인트 (Key Data Points — Ver 4.4)
{(chr(10).join([f"* **{dp.get('indicator','?')}**: {dp.get('value','?')} {dp.get('unit','')} — {dp.get('context','')}" for dp in key_data_points if isinstance(dp, dict)]) if key_data_points else "*(명시된 수치 없음)*")}

### 💬 추가 직접 인용 (Additional Quotes — Ver 4.4)
{(chr(10).join([f"> {q.replace(chr(34), chr(92)+chr(34))}" for q in additional_quotes if q]) if additional_quotes else "*(추가 인용 없음)*")}

### 🎯 가격 목표 / 예측 (Price Targets — Ver 4.4)
{(chr(10).join([f"* **{pt.get('ticker','?')}** ({pt.get('direction','?')}): 목표 {pt.get('target','?')} · 호리즌 {pt.get('horizon','?')}" for pt in price_targets if isinstance(pt, dict)]) if price_targets else "*(명시된 가격 목표 없음)*")}
"""
        
        md_file_path.write_text(md_content.strip() + "\n", encoding="utf-8")
        return md_file_path


def _safe_json_list(raw) -> list:
    """👑 [Ver 4.3] reports JSON 컬럼(conditional_catalysts/invalidation_risks)을
    리스트로 안전 복원. NULL/빈/파손 → []. 백필·Daily 섹션5 양쪽에서 사용.
    """
    return parse_json_list(raw, accept_native=False)


if __name__ == "__main__":
    print("Exporter module loaded successfully.")


# ---------------------------------------------------------------------------
# 👑 [Ver 3.1] Backfill helper — rehydrate MacroViewSchema dicts from SQLite
# ---------------------------------------------------------------------------
def _load_db_report_as_schema(db_path: str):
    """Yield MacroViewSchema-shaped dicts for every row in `reports`,
    with nodes and quant_signals joined in.  Used by `main.py --backfill_from_db`
    to regenerate Obsidian MD files without re-running the LLM.

    👑 [Ver 4.3] conditional_catalysts / invalidation_risks 는 reports 테이블에
    JSON 으로 영구화되어 백필 시 그대로 복원됨(이전 빈 리스트 손실 현상 해소).
    단, 스키마 확장(Ver 4.3) 이전에 수집된 과거 행은 컬럼이 NULL → 빈 리스트로
    복원됨(해당 시점엔 DB 미저장이었으므로 복구 불가). 연결: try/finally 로
    conn.close() 보장(generator 중간 break 시 누수 방지).
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM reports")
        rows = cur.fetchall()
        for row in rows:
            vid = row["video_id"]
            cur.execute("SELECT node_type, node_value FROM nodes WHERE video_id = ?", (vid,))
            nrows = cur.fetchall()
            macro_themes, asset_classes, tickers = [], [], []
            for nr in nrows:
                v = nr["node_value"]
                if nr["node_type"] == "macro_theme":
                    macro_themes.append(v)
                elif nr["node_type"] == "asset_class":
                    asset_classes.append(v)
                elif nr["node_type"] == "ticker":
                    tickers.append(v)

            cur.execute("SELECT * FROM quant_signals WHERE video_id = ?", (vid,))
            qrow = cur.fetchone()

            yield {
                "metadata": {
                    "speaker_name": row["speaker_name"],
                    "speaker_role": row["speaker_role"],
                    "source_channel": row["source_channel"],
                    "broadcast_date": row["broadcast_date"],
                    "video_id": vid,
                    # 👑 [Ver 4.4] 화자 소속 복원. 과거 행(NULL)은 "".
                    "speaker_institution": row["speaker_institution"] if "speaker_institution" in row.keys() else "",
                },
                "graph_nodes": {
                    "time_box": row["time_box"] or "[[Unknown]]",
                    "macro_themes": macro_themes,
                    "asset_classes": asset_classes,
                    "specific_tickers": tickers,
                },
                "quant_signals": {
                    "bull_bear_score": qrow["bull_bear_score"] if qrow else 5,
                    "conviction_score": qrow["conviction_score"] if qrow else 5,
                    "contrarian_flag": bool(qrow["contrarian_flag"]) if qrow else False,
                    "sector_tilt": qrow["sector_tilt"] if qrow else "",
                    "duration_call": qrow["duration_call"] if qrow else "",
                    "macro_factor": qrow["macro_factor"] if qrow else "",
                    # 👑 [Ver 4.4] 시간지평 복원. 과거 행(NULL)은 "".
                    "view_time_horizon": qrow["view_time_horizon"] if qrow and "view_time_horizon" in qrow.keys() else "",
                },
                "view_details": {
                    "core_thesis": row["core_thesis"] or "",
                    # 👑 [Ver 4.3] DB 영구화 복원 — JSON → 리스트. 과거 행(NULL)은 빈 리스트.
                    "conditional_catalysts": _safe_json_list(row["conditional_catalysts"]),
                    "invalidation_risks": _safe_json_list(row["invalidation_risks"]),
                    "verbatim_quote": row["verbatim_quote"] or "",
                    # 👑 [Ver 4.4] 증거 필드 복원 — JSON → list[dict]/list[str]. 과거 행은 빈.
                    "key_data_points": _safe_json_list(row["key_data_points"]) if "key_data_points" in row.keys() else [],
                    "additional_quotes": _safe_json_list(row["additional_quotes"]) if "additional_quotes" in row.keys() else [],
                    "price_targets": _safe_json_list(row["price_targets"]) if "price_targets" in row.keys() else [],
                },
                # 👑 [Ver 4.7] 4대 내러티브 필드 복원 — 과거 행(NULL)은 None/빈 리스트(하위 호환).
                "expectation_gap": row["expectation_gap"] if "expectation_gap" in row.keys() else None,
                "causal_chain": _safe_json_list(row["causal_chain"]) if "causal_chain" in row.keys() else [],
                "tracking_indicators": _safe_json_list(row["tracking_indicators"]) if "tracking_indicators" in row.keys() else [],
                "tactical_stance": _safe_json_list(row["tactical_stance"]) if "tactical_stance" in row.keys() else [],
            }
    finally:
        conn.close()
