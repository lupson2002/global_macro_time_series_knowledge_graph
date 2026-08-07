import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from scripts.audit_refactoring import analyze_file, audit, main


class RefactoringAuditTests(unittest.TestCase):
    def test_metrics_capture_function_span_branches_and_internal_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "main.py"
            source.write_text(
                "from src.config import settings\n"
                "def choose(value):\n"
                "    if value:\n"
                "        return 1\n"
                "    return 0\n",
                encoding="utf-8",
            )
            metric = analyze_file(source, root)

        self.assertEqual(metric.path, "main.py")
        self.assertEqual(metric.internal_imports, 1)
        self.assertEqual(metric.largest_function.name, "choose")
        self.assertEqual(metric.largest_function.lines, 4)
        self.assertEqual(metric.largest_function.decision_points, 1)

    def test_audit_orders_modules_by_risk_and_json_is_machine_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "scripts").mkdir()
            (root / "main.py").write_text("def small():\n    return 1\n", encoding="utf-8")
            (root / "src" / "large.py").write_text(
                "def large(value):\n" + "    value += 1\n" * 100 + "    return value\n",
                encoding="utf-8",
            )
            metrics = audit(root)
            output = StringIO()
            with redirect_stdout(output):
                exit_code = main(["--root", str(root), "--json", "--limit", "1"])

        self.assertEqual(metrics[0].path, "src/large.py")
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(output.getvalue())[0]["path"], "src/large.py")
