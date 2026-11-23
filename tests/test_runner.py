import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from unittest.mock import Mock, patch

from diditsearch.fixtures import Case
from diditsearch.runner import run_checks


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))

    def case(self, id_="one", expected="matched"):
        root = self.root / id_
        root.mkdir(exist_ok=True)
        return Case(id_, root, {"pattern": "café", "path": ".", "glob": None}, expected, f"Reason for {id_}")

    def adapter(self, code):
        return [sys.executable, "-I", "-c", code]

    def test_reference_receives_original_inputs_and_continues_after_error(self):
        cases = [self.case("first"), self.case("second")]
        reference = ModuleType("diditsearch.reference")
        reference.inspect_search = Mock(side_effect=[OSError("unavailable"), {"status": "matched", "detail": ""}])
        with patch.dict(sys.modules, {reference.__name__: reference}):
            report = run_checks(cases)
        self.assertEqual([call.args for call in reference.inspect_search.call_args_list],
                         [(case.root, case.request) for case in cases])
        self.assertEqual({key: report[key] for key in ("version", "passed", "total", "errors")},
                         {"version": 1, "passed": 1, "total": 2, "errors": 1})
        self.assertIn("Reference search error", report["cases"][0]["detail"])
        self.assertEqual(report["cases"][1], dict(id="second", expected="matched", actual="matched",
                                                  detail="", skipped=[], expected_skipped=[],
                                                  passed=True, reason="Reason for second"))

    def test_adapter_receives_only_request_keys_as_utf8_and_runs_in_case_root(self):
        case = self.case()
        case.request["expected"] = "do not send"
        (case.root / "marker").write_text("present", encoding="utf-8")
        script = self.root / "adapter with spaces.py"
        script.write_text(
            "import json, pathlib, sys\n"
            "assert sys.argv[1] == '& | > argument'\n"
            "assert pathlib.Path('marker').is_file()\n"
            "raw = sys.stdin.buffer.readline()\n"
            "assert raw.endswith(b'\\n') and not sys.stdin.buffer.read()\n"
            "request = json.loads(raw.decode('utf-8'))\n"
            "assert request == {'pattern': 'caf\\u00e9', 'path': '.', 'glob': None}\n"
            "sys.stderr.buffer.write(b'debug\\xff')\n"
            "sys.stdout.buffer.write(' {\"status\": \"matched\", \"detail\": \"caf\\u00e9\"}\\n'.encode('utf-8'))\n",
            encoding="utf-8",
        )
        result = run_checks([case], [sys.executable, str(script), "& | > argument"])["cases"][0]
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["detail"], "café")

    def test_mismatch_and_protocol_error_are_distinct_and_order_is_preserved(self):
        cases = [self.case("wrong"), self.case("broken"), self.case("last", "no_match")]
        argv = self.adapter(
            "from pathlib import Path; print('noise' if Path.cwd().name == 'broken' "
            "else '{\"status\":\"no_match\",\"detail\":\"\"}')"
        )
        report = run_checks(iter(cases), argv)
        self.assertEqual((report["passed"], report["total"], report["errors"]), (1, 3, 1))
        self.assertEqual([(result["id"], result["actual"], result["passed"]) for result in report["cases"]],
                         [("wrong", "no_match", False), ("broken", None, False), ("last", "no_match", True)])
        self.assertIn("Adapter protocol error", report["cases"][1]["detail"])

    def test_malformed_stdout_is_a_protocol_error(self):
        case = self.case()
        good = b'{"status":"matched","detail":""}'
        for raw, detail in [
            (b"\xff", "UTF-8"), (good + good, "exactly one JSON object"),
            (b"", "exactly one JSON object"), (b"true", "JSON object"),
            (b'{"status":"unknown","detail":""}', "recognized status"),
            (b'{"status":[],"detail":""}', "recognized status"),
            (b'{"status":"matched"}', "detail string"),
            (b'{"status":"matched","detail":5}', "detail string"),
            (b'{"status":"matched","detail":"","extra":NaN}', "Invalid JSON constant"),
        ]:
            with self.subTest(raw=raw):
                argv = self.adapter(f"import sys; sys.stdout.buffer.write({raw!r})")
                report = run_checks([case], argv)
                self.assertEqual(report["errors"], 1)
                self.assertIsNone(report["cases"][0]["actual"])
                self.assertIn(detail, report["cases"][0]["detail"])

    def test_nonzero_exit_retains_bounded_stderr_diagnostics(self):
        code = (
            "import sys; print('{\"status\":\"matched\",\"detail\":\"\"}'); "
            "sys.stderr.buffer.write(b'failure\\xff' + b'x'*50000 + b'end-marker'); sys.exit(7)"
        )
        report = run_checks([self.case()], self.adapter(code))
        detail = report["cases"][0]["detail"]
        self.assertEqual(report["errors"], 1)
        self.assertIn("exited with code 7", detail)
        self.assertIn("failure", detail)
        self.assertNotIn("end-marker", detail)
        self.assertLess(len(detail), 500)

    def test_timeout_and_invalid_timeout(self):
        case = self.case()
        argv = self.adapter("import time; time.sleep(5)")
        report = run_checks([case], argv, timeout=0.05)
        self.assertEqual(report["errors"], 1)
        self.assertIn("Adapter timeout", report["cases"][0]["detail"])
        for timeout in (0, -1, float("nan"), float("inf"), "1", True, None, 10**1000):
            with self.subTest(timeout=timeout):
                report = run_checks([case], argv, timeout=timeout)
                self.assertEqual(report["errors"], 1)
                self.assertIn("finite positive", report["cases"][0]["detail"])

    def test_command_resolution_and_configuration_error(self):
        case = self.case()
        exe = Path(sys.executable)
        argv = [exe.name if os.name != "nt" else exe.stem,
                *self.adapter("print('{\"status\":\"matched\",\"detail\":\"\"}')")[1:]]
        with patch.dict(os.environ, {"PATH": str(exe.parent)}):
            self.assertEqual(run_checks([case], argv)["passed"], 1)
            for adapter, detail in [
                (["missing-did-it-search-adapter"], "not found on PATH"),
                ([str(self.root / "missing.exe")], "Adapter launch"),
                (["./relative-script"], "absolute executable"),
                ("python", "argv list"), ([], "argv list"), (["python", "bad\0arg"], "argv list"),
            ]:
                with self.subTest(adapter=adapter):
                    report = run_checks([case], adapter)
                    self.assertEqual(report["errors"], 1)
                    self.assertIn(detail, report["cases"][0]["detail"])

    def test_empty_cases(self):
        self.assertEqual(run_checks([]), dict(version=1, passed=0, total=0, errors=0, cases=[]))
