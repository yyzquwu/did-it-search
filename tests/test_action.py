import json
import os
import runpy
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ACTION = Path(__file__).resolve().parents[1]
ENTRYPOINT = ACTION / ".github" / "run-action.py"
RUN = runpy.run_path(str(ENTRYPOINT))["main"] if ENTRYPOINT.is_file() else None
NODE = shutil.which("node")
PACKAGE = "@earendil-works/pi-coding-agent"


@unittest.skipUnless(RUN and shutil.which("git"), "Action source checkout and Git are required")
class ActionTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory(prefix="didit action # "))).resolve()
        self.checkout = root / "consumer"
        self.project = self.checkout / "project with spaces $(literal)"
        self.project.mkdir(parents=True)
        self.summary, self.event = root / "summary.md", root / "event.json"
        self.git("init", "-q", "--template=")

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "user.name=Action Test", "-c", "user.email=test@example.org",
                                        "-c", "commit.gpgsign=false", *args], cwd=self.checkout).strip().decode()

    def commit(self, path):
        self.git("add", "--", path)
        self.git("commit", "-qm", "Baseline snapshot")
        return self.git("rev-parse", "HEAD")

    def run_action(self, event, path=".did-it-search.json", sha=""):
        self.summary.write_bytes(b"")
        env = dict(GITHUB_WORKSPACE=str(self.checkout), GITHUB_STEP_SUMMARY=str(self.summary),
                   GITHUB_EVENT_NAME=event, GITHUB_ACTION_PATH=str(ACTION), GITHUB_EVENT_PATH=str(self.event),
                   DIDITSEARCH_PROJECT_DIR=self.project.name, DIDITSEARCH_BASELINE=path,
                   DIDITSEARCH_PR_BASE_SHA=sha)
        with patch.dict(os.environ, env), redirect_stderr(StringIO()):
            # CI already installed this checkout into an isolated Python environment.
            with patch.dict(RUN.__globals__, {"install_cli": lambda *_: sys.executable}):
                status = RUN()
        return status, self.summary.read_text(encoding="utf-8")

    def write_pi(self, response):
        pkg = self.project / "node_modules" / PACKAGE
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "package.json").write_text(json.dumps({"name": PACKAGE, "version": "1.2.3",
                                                      "exports": "./sdk.mjs"}), encoding="utf-8")
        (pkg / "sdk.mjs").write_text(
            "if (process.env.PI_OFFLINE !== '1') throw new Error('expected offline Pi');\n"
            "export function createGrepTool() { return {\n"
            "  parameters: { properties: { pattern: {}, literal: {}, path: {}, glob: {} } },\n"
            f"  execute: () => ({{ content: [{{ type: 'text', text: {json.dumps(response)} }}] }})\n"
            "}; }\n", encoding="utf-8",
        )

    @unittest.skipUnless(NODE, "Node.js is required for action comparison")
    def test_pr_compares_project_pi_to_base_and_preserves_exit_codes(self):
        node_version = subprocess.check_output([NODE, "--version"], text=True).strip()
        if tuple(map(int, node_version.removeprefix("v").split(".")[:2])) < (22, 19):
            self.skipTest("Action comparison requires Node.js 22.19+")
        path = ".did-it-search.json"
        self.write_pi("file:1: café")
        # A Python module in the consumer project must not shadow the installed CLI.
        (self.project / "diditsearch.py").write_text("raise RuntimeError('untrusted shim')", encoding="utf-8")
        subprocess.run([sys.executable, "-I", "-m", "diditsearch", "baseline", "--tool", "pi", "--output",
                        str(self.checkout / path)], cwd=self.project, env={**os.environ, "PI_OFFLINE": "1"},
                       capture_output=True, check=True, timeout=180)
        sha = self.commit(path)
        (self.checkout / path).write_text("{broken PR baseline", encoding="utf-8")
        head = self.commit(path)
        self.event.write_text(json.dumps({"pull_request": {"base": {"sha": head}}}), encoding="utf-8")

        code, summary = self.run_action("pull_request", sha=sha)
        self.assertEqual(code, 0, summary)
        self.assertIn("— PASS", summary)
        self.assertIn(f"{PACKAGE} 1.2.3", summary)

        self.write_pi("No matches found")
        code, summary = self.run_action("pull_request", sha=sha)
        self.assertEqual(code, 1, summary)
        self.assertIn("— FAIL", summary)
        self.assertIn("café", summary)

        code, summary = self.run_action("push")
        self.assertEqual(code, 2, summary)
        self.assertIn("— ERROR", summary)

    def test_workspace_baseline_at_explicit_path_is_read_from_checkout_root(self):
        path = "custom baseline $(literal).json"
        (self.checkout / path).write_text("{\"version\":", encoding="utf-8")
        code, summary = self.run_action("workflow_dispatch", path=path)
        self.assertEqual(code, 2, summary)
        self.assertIn("Expecting value", summary)

    def test_invalid_or_unavailable_pr_baseline_fails_with_a_summary(self):
        (self.checkout / "README.md").touch()
        base = self.commit("README.md")
        (self.checkout / ".did-it-search.json").write_text("{}", encoding="utf-8")
        for sha, message in (("HEAD", "full Git commit ID"), ("0" * 40, "fetch-depth"),
                             (base, "Could not read baseline .did-it-search.json")):
            with self.subTest(sha=sha):
                code, summary = self.run_action("pull_request", sha=sha)
                self.assertEqual(code, 2, summary)
                self.assertIn(message, summary)
