import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import macro_view
from src.exporter import ObsidianMDExporter, SQLiteExporter
from src.reconciliation import (
    ReconciliationPlan,
    StorageSnapshot,
    audit_storage,
    build_plan,
    create_backup,
    repair_missing,
    scan_markdown_ids,
)


class ReconciliationAuditTests(unittest.TestCase):
    def test_build_plan_distinguishes_missing_and_orphan_projections(self):
        snapshot = StorageSnapshot(
            sqlite_ids=frozenset({"a", "b"}),
            markdown_ids=frozenset({"a", "orphan-md"}),
            vector_ids=frozenset({"b", "orphan-vector"}),
        )
        plan = build_plan(snapshot)
        self.assertEqual(plan.missing_markdown, ("b",))
        self.assertEqual(plan.missing_vectors, ("a",))
        self.assertEqual(plan.orphan_markdown, ("orphan-md",))
        self.assertEqual(plan.orphan_vectors, ("orphan-vector",))
        self.assertTrue(plan.has_drift)

    def test_unavailable_vector_store_does_not_claim_every_vector_is_missing(self):
        plan = build_plan(
            StorageSnapshot(
                sqlite_ids=frozenset({"a"}),
                markdown_ids=frozenset({"a"}),
                vector_ids=None,
            )
        )
        self.assertEqual(plan.missing_vectors, ())
        self.assertEqual(plan.orphan_vectors, ())
        self.assertFalse(plan.has_drift)

    def test_audit_uses_sqlite_as_source_of_truth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "macro.db"
            vault_path = root / "vault"
            exporter = SQLiteExporter(str(db_path))
            first = macro_view(video_id="abcdefghijk")
            second = macro_view(video_id="lmnopqrstuv")
            with patch("src.lancedb_store.upsert_document"):
                exporter.export_data(first)
                exporter.export_data(second)
            ObsidianMDExporter(str(vault_path)).export_markdown(first)

            snapshot, plan, schemas = audit_storage(
                db_path,
                vault_path,
                {"abcdefghijk", "zzzzzzzzzzz"},
            )

        self.assertEqual(snapshot.sqlite_ids, frozenset({"abcdefghijk", "lmnopqrstuv"}))
        self.assertEqual(plan.missing_markdown, ("lmnopqrstuv",))
        self.assertEqual(plan.missing_vectors, ("lmnopqrstuv",))
        self.assertEqual(plan.orphan_vectors, ("zzzzzzzzzzz",))
        self.assertEqual(set(schemas), {"abcdefghijk", "lmnopqrstuv"})

    def test_markdown_scan_ignores_unrelated_notes(self):
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            (vault / "ordinary-note.md").write_text("note", encoding="utf-8")
            report = vault / "Analyst_2026-08-07_abcdefghijk.md"
            report.write_text("report", encoding="utf-8")
            self.assertEqual(scan_markdown_ids(vault), frozenset({"abcdefghijk"}))


class ReconciliationRepairTests(unittest.TestCase):
    def test_repair_creates_only_missing_items_and_never_deletes_orphans(self):
        plan = ReconciliationPlan(
            missing_markdown=("abcdefghijk",),
            missing_vectors=("lmnopqrstuv",),
            orphan_markdown=("orphan-md",),
            orphan_vectors=("orphan-vector",),
        )
        schemas = {
            "abcdefghijk": macro_view(video_id="abcdefghijk"),
            "lmnopqrstuv": macro_view(video_id="lmnopqrstuv"),
        }
        markdown = Mock()
        vector_upsert = Mock(return_value=True)
        result = repair_missing(
            plan,
            schemas,
            markdown_exporter=markdown,
            vector_upsert=vector_upsert,
        )
        self.assertEqual(result.markdown_repaired, ("abcdefghijk",))
        self.assertEqual(result.vectors_repaired, ("lmnopqrstuv",))
        markdown.export_markdown.assert_called_once_with(schemas["abcdefghijk"])
        vector_upsert.assert_called_once_with(schemas["lmnopqrstuv"])

    def test_backup_contains_consistent_sqlite_and_lancedb_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "macro.db"
            with sqlite3.connect(db_path) as connection:
                connection.execute("CREATE TABLE sample (value TEXT)")
                connection.execute("INSERT INTO sample VALUES ('preserved')")
            lancedb_dir = root / "vectors"
            lancedb_dir.mkdir()
            (lancedb_dir / "marker").write_text("vector", encoding="utf-8")

            backup = create_backup(db_path, lancedb_dir, root / "backups")

            with sqlite3.connect(backup / "macro.db") as connection:
                value = connection.execute("SELECT value FROM sample").fetchone()[0]
            self.assertEqual(value, "preserved")
            self.assertEqual((backup / "vectors" / "marker").read_text(), "vector")
            self.assertTrue((backup / "manifest.json").is_file())


class LanceDbPathTests(unittest.TestCase):
    def test_list_video_ids_uses_requested_store_without_creating_default(self):
        from src import lancedb_store
        import pyarrow as pa

        with tempfile.TemporaryDirectory() as directory:
            db_dir = Path(directory) / "vectors"
            get_table = Mock()
            with patch.object(lancedb_store, "_get_table", get_table):
                self.assertEqual(lancedb_store.list_video_ids(db_dir), frozenset())
            get_table.assert_not_called()

            db_dir.mkdir()
            table = Mock()
            table.count_rows.return_value = 1
            table.to_lance.return_value.scanner.return_value.to_table.return_value = (
                pa.table({"video_id": ["abcdefghijk"]})
            )
            with patch.object(lancedb_store, "_get_table", return_value=table) as get_table:
                ids = lancedb_store.list_video_ids(db_dir)
            self.assertEqual(ids, frozenset({"abcdefghijk"}))
            get_table.assert_called_once_with(create=False, db_dir=db_dir)
            table.to_lance.return_value.scanner.assert_called_once_with(
                columns=["video_id"]
            )


class ReconciliationCliTests(unittest.TestCase):
    def test_default_audit_is_read_only_and_returns_drift_status(self):
        from scripts import reconcile_storage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "macro.db"
            exporter = SQLiteExporter(str(db_path))
            with patch("src.lancedb_store.upsert_document"):
                exporter.export_data(macro_view())
            vault = root / "vault-that-does-not-exist"
            vectors = root / "vectors-that-do-not-exist"
            with patch.object(reconcile_storage, "list_video_ids", return_value=frozenset()):
                with redirect_stdout(StringIO()):
                    exit_code = reconcile_storage.main(
                        [
                            "--db-path",
                            str(db_path),
                            "--vault-dir",
                            str(vault),
                            "--lancedb-dir",
                            str(vectors),
                        ]
                    )
            self.assertEqual(exit_code, 2)
            self.assertFalse(vault.exists())
            self.assertFalse(vectors.exists())

    def test_apply_requires_explicit_confirmation(self):
        from scripts import reconcile_storage

        with redirect_stderr(StringIO()):
            self.assertEqual(reconcile_storage.main(["--apply"]), 64)

    def test_markdown_only_apply_repairs_with_backup_without_vector_access(self):
        from scripts import reconcile_storage

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "macro.db"
            exporter = SQLiteExporter(str(db_path))
            with patch("src.lancedb_store.upsert_document"):
                exporter.export_data(macro_view())
            vault = root / "vault"
            vectors = root / "vectors"
            list_ids = Mock(side_effect=AssertionError("vector store must not open"))
            with patch.object(reconcile_storage, "list_video_ids", list_ids):
                with redirect_stdout(StringIO()):
                    exit_code = reconcile_storage.main(
                        [
                            "--db-path",
                            str(db_path),
                            "--vault-dir",
                            str(vault),
                            "--lancedb-dir",
                            str(vectors),
                            "--backup-root",
                            str(root / "backups"),
                            "--markdown-only",
                            "--apply",
                            "--yes",
                        ]
                    )
            self.assertEqual(exit_code, 0)
            self.assertEqual(scan_markdown_ids(vault), frozenset({"abcdefghijk"}))
            self.assertEqual(len(list((root / "backups").iterdir())), 1)
            list_ids.assert_not_called()
