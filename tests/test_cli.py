import json
import os
import shutil
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from diditsearch.cli import main


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def run_cli(self, *args, cwd=None, env=None):
        return subprocess.run([sys.executable, "-I", "-m", "diditsearch", *args],
                              cwd=cwd, env=env, capture_output=True)

    def test_installed_workflow_and_ci_exits(self):
        with TemporaryDirectory() as tmp:
            dest = Path(tmp, "fixtures with spaces")
            demo = self.run_cli("demo", cwd=tmp)
            self.assertEqual(demo.returncode, 0, demo.stderr)
            for term in (b"exit 1", b"(empty)", b"partial", b"generated/client.ts"):
                self.assertIn(term, demo.stdout)
            report = self.run_cli("check", "--json", cwd=tmp)
            self.assertEqual((report.returncode, json.loads(report.stdout)["passed"]), (0, 8))
            standalone = self.run_cli("check", "--json", "--adapter",
                                      str(ROOT / "examples" / "reference_adapter.py"), cwd=tmp)
            self.assertEqual((standalone.returncode, json.loads(standalone.stdout)["passed"]), (0, 8))
            sample = self.run_cli("check", "--json", "--adapter", "examples/plain_rg.py", cwd=ROOT)
            self.assertEqual(sample.returncode, 1, sample.stderr)
            report = json.loads(sample.stdout)
            self.assertEqual((report["passed"], report["errors"]), (4, 0))
            missing = self.run_cli("check", "--json", "--adapter", "not-a-real-adapter-1826", cwd=tmp)
            self.assertEqual((missing.returncode, json.loads(missing.stdout)["errors"]), (2, 8))
            missing_script = self.run_cli("check", "--json", "--adapter", "missing.py", cwd=tmp)
            self.assertEqual((missing_script.returncode, json.loads(missing_script.stdout)["errors"]), (2, 8))
            exported = self.run_cli("fixtures", str(dest), cwd=tmp)
            self.assertEqual(exported.returncode, 0, exported.stderr)
            records = json.loads((dest / "cases.json").read_bytes())
            self.assertEqual(len(records), 8)
            self.assertEqual([r["expected_skipped"] for r in records][4:6], [["ignored"], ["binary"]])
            self.assertTrue(all((dest / r["directory"] / ".git" / "HEAD").is_file() for r in records))
            sentinel = dest / "user.txt"
            sentinel.write_bytes(b"preserve")
            self.assertEqual(self.run_cli("fixtures", str(dest)).returncode, 2)
            self.assertEqual(sentinel.read_bytes(), b"preserve")

    def test_distinguishes_why_each_search_was_partial(self):
        with TemporaryDirectory() as tmp:
            adapter = Path(tmp, "ambiguous adapter.py")
            adapter.write_text('print(\'{"status":"partial","detail":"incomplete"}\')', encoding="utf-8")
            result = self.run_cli("check", "--json", "--adapter", adapter.name, cwd=tmp)
            report = json.loads(result.stdout)
            self.assertEqual((result.returncode, report["passed"], report["errors"]), (1, 0, 0))
            self.assertEqual(report["cases"][4]["actual"], "partial")

    def test_cleanup_error_preserves_completed_report_and_exit(self):
        cleanup = TemporaryDirectory.cleanup

        def locked(tmp):
            cleanup(tmp)
            raise PermissionError("fixture still in use")

        with patch.object(TemporaryDirectory, "cleanup", locked):
            for adapter, code, passed in (([], 0, 8), (["--adapter", str(ROOT / "examples/plain_rg.py")], 1, 4)):
                out, err = StringIO(), StringIO()
                with self.subTest(code=code), redirect_stdout(out), redirect_stderr(err):
                    self.assertEqual(main(["check", "--json", *adapter]), code)
                    self.assertEqual(json.loads(out.getvalue())["passed"], passed)
                    self.assertIn("could not remove temporary fixtures", err.getvalue())
                    self.assertIn("fixture still in use", err.getvalue())

    def test_inherited_git_dir_cannot_redirect_fixture_initialization(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp, "unrelated.git")
            result = self.run_cli("fixtures", str(Path(tmp, "export")), env={**os.environ, "GIT_DIR": str(target)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(target.exists())

    def test_case_selection_and_pi_configuration_errors(self):
        report = self.run_cli("check", "--json", "--case", "glob-root-decoy", "--case", "glob-root-hit")
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertEqual([r["id"] for r in json.loads(report.stdout)["cases"]],
                         ["glob-root-hit", "glob-root-decoy"])
        unknown = self.run_cli("check", "--json", "--case", "absent-case")
        self.assertEqual((unknown.returncode, unknown.stdout), (2, b""))
        self.assertIn(b"absent-case", unknown.stderr)
        conflict = self.run_cli("check", "--tool", "pi", "--adapter", "some-command")
        self.assertEqual(conflict.returncode, 2)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required for Pi")
    def test_local_pi_check_ignores_the_replay_module_override(self):
        with TemporaryDirectory() as tmp:
            module = Path(tmp) / "wrong.mjs"
            module.write_text("throw new Error('wrong SDK loaded')", encoding="utf-8")
            result = self.run_cli("check", "--tool", "pi", "--case", "hit", "--json", cwd=tmp,
                                  env={**os.environ, "DIDITSEARCH_PI_MODULE": str(module)})
            self.assertEqual(result.returncode, 2)
            detail = json.loads(result.stdout)["cases"][0]["detail"]
            self.assertIn("@earendil-works/pi-coding-agent", detail)
            self.assertNotIn("wrong SDK loaded", detail)

    def test_replay_json_exit_contract(self):
        for status, code in [("reproduced", 0), ("unexpected_parity", 1), ("infrastructure_error", 2)]:
            report = dict(status=status, passed=code == 0)
            with self.subTest(status=status), patch("diditsearch.pi_replay.replay", return_value=report):
                out = StringIO()
                with redirect_stdout(out):
                    self.assertEqual(main(["replay", "pi", "--json"]), code)
                self.assertEqual(json.loads(out.getvalue()), report)
