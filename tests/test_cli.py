import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from tempfile import TemporaryDirectory


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
            self.assertEqual((report.returncode, json.loads(report.stdout)["passed"]), (0, 6))
            standalone = self.run_cli("check", "--json", "--adapter",
                                      str(ROOT / "examples" / "reference_adapter.py"), cwd=tmp)
            self.assertEqual((standalone.returncode, json.loads(standalone.stdout)["passed"]), (0, 6))
            sample = self.run_cli("check", "--json", "--adapter", "examples/plain_rg.py", cwd=ROOT)
            self.assertEqual(sample.returncode, 1, sample.stderr)
            report = json.loads(sample.stdout)
            self.assertEqual((report["passed"], report["errors"]), (3, 0))
            missing = self.run_cli("check", "--json", "--adapter", "not-a-real-adapter-1826", cwd=tmp)
            self.assertEqual((missing.returncode, json.loads(missing.stdout)["errors"]), (2, 6))
            missing_script = self.run_cli("check", "--json", "--adapter", "missing.py", cwd=tmp)
            self.assertEqual((missing_script.returncode, json.loads(missing_script.stdout)["errors"]), (2, 6))
            exported = self.run_cli("fixtures", str(dest), cwd=tmp)
            self.assertEqual(exported.returncode, 0, exported.stderr)
            records = json.loads((dest / "cases.json").read_bytes())
            self.assertEqual(len(records), 6)
            self.assertEqual([r["expected_skipped"] for r in records][-2:], [["ignored"], ["binary"]])
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
            self.assertEqual(report["cases"][-2]["actual"], "partial")

    def test_inherited_git_dir_cannot_redirect_fixture_initialization(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp, "unrelated.git")
            result = self.run_cli("fixtures", str(Path(tmp, "export")), env={**os.environ, "GIT_DIR": str(target)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(target.exists())
