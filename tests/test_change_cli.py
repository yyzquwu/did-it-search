import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from diditsearch.cli import _save_baseline, main
from diditsearch.summary import format_summary
from diditsearch.baseline import record
from diditsearch.fixtures import Case


class ChangeCliTests(unittest.TestCase):
    def test_baseline_redacts_transient_fixture_path_from_native_evidence(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = Case("missing", root, {"path": "absent"}, "missing_path", "Absent file")
            message = f"Path not found: {root / 'absent'}"
            row = dict(id="missing", expected="missing_path", actual="missing_path", detail=message,
                       reason=case.reason, passed=True, skipped=[], expected_skipped=[], raw={"message": message})
            report = dict(version=1, cases=[row], passed=1, total=1, errors=0)
            saved = record([case], report, {"name": "pi", "version": "1.5.1"})
            self.assertEqual(saved["cases"][0]["raw"]["message"], "Path not found: <fixture>/absent")
            self.assertEqual(saved["cases"][0]["detail"], saved["cases"][0]["raw"]["message"])

    def test_baseline_creation_is_exclusive_and_replacement_is_explicit(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            _save_baseline(path, {"value": 1}, False)
            with self.assertRaises(FileExistsError):
                _save_baseline(path, {"value": 2}, False)
            self.assertEqual(json.loads(path.read_bytes()), {"value": 1})
            _save_baseline(path, {"value": 2}, True)
            self.assertEqual(json.loads(path.read_bytes()), {"value": 2})
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_invalid_baseline_reports_error_before_running_tool(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            path.write_bytes(b'{"version":1,"version":2}')
            out = StringIO()
            with redirect_stdout(out), redirect_stderr(StringIO()):
                self.assertEqual(main(["compare", "--baseline", str(path), "--json"]), 2)
            self.assertIn("Duplicate", json.loads(out.getvalue())["error"])

    def test_summary_lists_changes_and_escapes_tool_output(self):
        outcome = {"actual": "no_match", "skipped": [], "detail": "empty",
                   "raw": {"text": "</pre><b>raw</b>"}}
        row = {"id": "target", "classification": "regression", "request": {"path": "repo"},
               "expected": "matched", "expected_skipped": [], "before": None, "after": outcome}
        report = {"exit_code": 1, "tool": {"name": "pi", "version": "1.5.0"},
                  "cases": [row, dict(row, id="omitted", classification="known_failure")]}
        md = format_summary(report, markdown=True)
        self.assertIn("Regressions: 1", md)
        self.assertIn("Unchanged known failures: 1", md)
        self.assertIn("1.5.0", md)
        self.assertIn("&lt;/pre&gt;&lt;b&gt;raw", md)
        self.assertNotIn("omitted", md)
