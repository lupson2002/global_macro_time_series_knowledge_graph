import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src import mcp_server


class MacroQueryValidationTests(unittest.TestCase):
    def test_rejects_mutating_and_recursive_queries_without_execution(self):
        rejected = [
            "INSERT INTO reports(video_id) VALUES ('x')",
            "UPDATE reports SET speaker_name='x'",
            "DELETE FROM reports",
            "PRAGMA table_info(reports)",
            "ATTACH DATABASE 'x' AS x",
            "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) SELECT * FROM x",
        ]
        for sql in rejected:
            with self.subTest(sql=sql), patch.object(
                mcp_server, "query_db_async", new=AsyncMock()
            ) as query:
                result = asyncio.run(mcp_server.run_macro_query(sql))
                self.assertIn("Rejected", result)
                query.assert_not_awaited()

    def test_select_without_limit_gets_result_cap(self):
        async def capture(query, params=()):
            self.captured = query
            return [{"ok": 1}]

        with patch.object(mcp_server, "query_db_async", side_effect=capture):
            result = asyncio.run(mcp_server.run_macro_query("SELECT 1 AS ok"))
        self.assertEqual(json.loads(result), [{"ok": 1}])
        self.assertTrue(self.captured.endswith("LIMIT 200"))

    def test_select_with_limit_is_preserved(self):
        async def capture(query, params=()):
            self.captured = query
            return []

        with patch.object(mcp_server, "query_db_async", side_effect=capture):
            asyncio.run(mcp_server.run_macro_query("SELECT 1 LIMIT 7"))
        self.assertEqual(self.captured, "SELECT 1 LIMIT 7")


class ReadOnlyDatabaseTests(unittest.TestCase):
    def test_sqlite_read_only_uri_cannot_write(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "readonly.db"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE sample (value INTEGER)")
                con.execute("INSERT INTO sample VALUES (1)")
            uri = f"file:{db}?mode=ro"
            with sqlite3.connect(uri, uri=True) as con:
                self.assertEqual(con.execute("SELECT value FROM sample").fetchall(), [(1,)])
                with self.assertRaises(sqlite3.OperationalError):
                    con.execute("INSERT INTO sample VALUES (2)")
