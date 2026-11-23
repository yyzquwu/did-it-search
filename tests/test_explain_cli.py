import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from diditsearch.cli import main
from diditsearch.explain import _rg_path


ROOT = Path(__file__).resolve().parents[1]


class ExplainCliTests(unittest.TestCase):
    def run_cli(self, *args, cwd=ROOT):
        return subprocess.run([sys.executable, "-I", "-m", "diditsearch", "explain", *args],
                              cwd=cwd, capture_output=True, timeout=20)

    def run_shell(self, cmd, root):
        if os.name == "nt":
            script = Path(root, "follow-up.ps1")
            script.write_text(f"{cmd}\nexit $LASTEXITCODE\n", encoding="utf-8-sig")
            argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(script)]
        else:
            argv = ["sh", "-c", cmd]
        return subprocess.run(argv, cwd=root, capture_output=True, timeout=20)

    def test_status_exit_codes_and_streams(self):
        for status, code, count in (("matched", 0, 1), ("no_match", 1, 2), ("no_files", 1, 0),
                                    ("missing_path", 2, None), ("partial", 2, None), ("error", 2, None)):
            report = dict(status=status, detail="Search diagnosis", files_searched=count)
            for as_json in (False, True):
                out, err = StringIO(), StringIO()
                args = ["explain", "--timeout", "0.25", *(["--json"] if as_json else []), "--", "-literal", "-file"]
                with (self.subTest(status=status, as_json=as_json),
                      patch("diditsearch.explain_cli.explain", return_value=report) as engine,
                      redirect_stdout(out), redirect_stderr(err)):
                    self.assertEqual(main(args), code)
                    engine.assert_called_once_with("-literal", "-file", None, timeout=0.25)
                    if as_json:
                        self.assertEqual(err.getvalue(), "")
                        self.assertEqual(json.loads(out.getvalue()), dict(report, follow_up_argv=None))
                    else:
                        used, empty = (err, out) if code == 2 else (out, err)
                        self.assertIn("Search diagnosis", used.getvalue())
                        self.assertEqual(empty.getvalue(), "")

    def test_readme_search_can_be_broadened_explicitly(self):
        args = ("createGrepTool", "src/diditsearch", "--glob", "*.rs")
        run = self.run_cli(*args, "--json")
        self.assertEqual((run.returncode, run.stderr), (1, b""))
        report = json.loads(run.stdout)
        self.assertEqual((report["status"], report["files_searched"]), ("no_files", 0))
        follow_up = report["follow_up_argv"]
        self.assertEqual(follow_up, [_rg_path() if os.name == "nt" else "rg",
                                     "--no-config", "-n", "-F", "--", *args[:2]])
        rerun = subprocess.run(follow_up, cwd=ROOT, capture_output=True, timeout=20)
        self.assertEqual((rerun.returncode, rerun.stderr), (0, b""))
        self.assertIn(b"pi_adapter.mjs", rerun.stdout)
        hit = self.run_cli(*args[:2], "--json")
        self.assertEqual((hit.returncode, hit.stderr), (0, b""))
        self.assertEqual(json.loads(hit.stdout)["status"], "matched")

    def test_printed_follow_up_round_trips_hostile_arguments(self):
        name = "- dir '$(echo injected) & echo bad; %TEMP% [x]\u2018\u2019\u201a\u201b"
        pattern = '-text " & echo unexpected; \\' + "'$(echo injected) " + chr(96) + " $HOME %TEMP%"
        with TemporaryDirectory() as tmp:
            target = Path(tmp, name)
            target.mkdir()
            (target / "match.txt").write_text(pattern + "\n", encoding="utf-8")
            args = ("--glob=*.rs", "--", pattern, name)
            run = self.run_cli(*args, cwd=tmp)
            self.assertEqual((run.returncode, run.stderr), (1, b""))
            self.assertNotIn(b"match.txt", run.stdout)
            command = run.stdout.decode().splitlines()[-1].strip()
            follow_up = self.run_shell(command, tmp)
            self.assertEqual((follow_up.returncode, follow_up.stderr), (0, b""))
            self.assertIn(b"match.txt:1:" + pattern.encode(), follow_up.stdout)
            structured = self.run_cli("--json", *args, cwd=tmp)
            argv = json.loads(structured.stdout)["follow_up_argv"]
            self.assertEqual(argv[-3:], ["--", pattern, name])
            direct = subprocess.run(argv, cwd=tmp, capture_output=True, timeout=20)
            self.assertEqual((direct.returncode, direct.stdout, direct.stderr),
                             (0, follow_up.stdout, b""))

    def test_follow_up_treats_lone_dash_as_a_filename(self):
        out = StringIO()
        report = dict(status="no_files", detail="Glob selected no files.", files_searched=0)
        with patch("diditsearch.explain_cli.explain", return_value=report), redirect_stdout(out):
            self.assertEqual(main(["explain", "--json", "--glob=*.rs", "--", "-needle", "-"]), 1)
        argv = json.loads(out.getvalue())["follow_up_argv"]
        self.assertEqual(argv[-3:], ["--", "-needle", "./-"])
        with TemporaryDirectory() as tmp:
            Path(tmp, "-").write_text("-needle\n", encoding="utf-8")
            run = subprocess.run(argv, input=b"", cwd=tmp, capture_output=True, timeout=20)
            self.assertEqual((run.returncode, run.stdout.splitlines()), (0, [b"1:-needle"]))

    def test_nonprinting_input_cannot_add_terminal_commands(self):
        detail = "Glob selected no files.\x1b[0m\ninjected"
        report = dict(status="no_files", detail=detail, files_searched=0)
        args = ["explain", "--glob=*.rs", "first\nsecond"]
        with patch("diditsearch.explain_cli.explain", return_value=report):
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(args), 1)
            self.assertNotIn("\x1b", out.getvalue())
            self.assertNotIn("\ninjected", out.getvalue())
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(main([*args, "--json"]), 1)
            self.assertEqual(json.loads(out.getvalue())["follow_up_argv"][-4:],
                             ["--multiline", "--", "first\nsecond", "."])

    def test_git_metadata_has_no_follow_up(self):
        with TemporaryDirectory() as tmp:
            Path(tmp, ".git").mkdir()
            run = self.run_cli("needle", ".git", "--glob", "*.rs", "--json", cwd=tmp)
        self.assertEqual((run.returncode, run.stderr), (1, b""))
        self.assertIsNone(json.loads(run.stdout)["follow_up_argv"])

    def test_invalid_timeout_has_a_json_diagnostic(self):
        for value in ("-1", "nan", "inf"):
            with self.subTest(value=value):
                run = self.run_cli("needle", f"--timeout={value}", "--json")
                self.assertEqual((run.returncode, run.stderr), (2, b""))
                report = json.loads(run.stdout)
                self.assertEqual((report["status"], report["files_searched"]), ("error", None))

    def test_zero_timeout_is_partial(self):
        run = self.run_cli("needle", "--timeout", "0", "--json")
        self.assertEqual((run.returncode, run.stderr), (2, b""))
        report = json.loads(run.stdout)
        self.assertEqual((report["status"], report["files_searched"]), ("partial", None))
