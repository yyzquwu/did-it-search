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
                      patch("diditsearch.explain_cli._explain", return_value=report) as engine,
                      redirect_stdout(out), redirect_stderr(err)):
                    self.assertEqual(main(args), code)
                    engine.assert_called_once_with("-literal", "-file", None, timeout=0.25, executable=_rg_path())
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
        self.assertEqual(follow_up, [_rg_path(),
                                     "--no-config", "-n", "-F", "--", *args[:2]])
        rerun = subprocess.run(follow_up, cwd=ROOT, capture_output=True, timeout=20)
        self.assertEqual((rerun.returncode, rerun.stderr), (0, b""))
        self.assertIn(b"pi_adapter.mjs", rerun.stdout)
        hit = self.run_cli(*args[:2], "--json")
        self.assertEqual((hit.returncode, hit.stderr), (0, b""))
        self.assertEqual(json.loads(hit.stdout)["status"], "matched")

    def test_json_follow_up_keeps_the_executable_used_for_the_search(self):
        exe = _rg_path()
        self.assertIsNotNone(exe)
        with (TemporaryDirectory() as tmp,
              patch("diditsearch.explain_cli._rg_path", side_effect=[exe]) as lookup,
              patch("diditsearch.explain._rg_path", lookup),
              redirect_stdout(StringIO()) as out):
            Path(tmp, "app.py").write_text("needle\n")
            self.assertEqual(main(["explain", "needle", tmp, "--glob", "*.rs", "--json"]), 1)
            self.assertEqual(json.loads(out.getvalue())["follow_up_argv"][0], exe)
            lookup.assert_called_once_with()

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
        with patch("diditsearch.explain_cli._explain", return_value=report), redirect_stdout(out):
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
        with patch("diditsearch.explain_cli._explain", return_value=report):
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

    def test_ignored_file_follow_up_is_opt_in_and_respects_the_requested_path(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", "--template="], cwd=root, check=True)
            (root / ".gitignore").write_bytes(b"/source/-\n")
            (root / "source").mkdir()
            (root / "source" / "-").write_bytes(b"-needle\nsecond\n")
            (root / "source" / "visible").write_bytes(b"other\n")
            for cwd, path, expected in ((root, "source", "source/-"), (root, "source/-", "source/-"),
                                        (root / "source", "-", "./-")):
                with self.subTest(path=path):
                    args = ("--", "-needle\nsecond", path)
                    run = self.run_cli("--json", *args, cwd=cwd)
                    self.assertEqual((run.returncode, run.stderr), (2, b""))
                    report = json.loads(run.stdout)
                    self.assertTrue(report["coverage_complete"])
                    self.assertEqual(report["skipped_paths"], [{
                        "path": "-", "kind": "ignored", "is_dir": False,
                        "rule": {"source": ".gitignore", "line": 1, "pattern": "/source/-"},
                    }])
                    argv = report["follow_up_argv"]
                    self.assertEqual(argv[0], _rg_path())
                    self.assertEqual(argv[-4:], ["--multiline", "--", "-needle\nsecond", expected])
                    hit = subprocess.run(argv, cwd=cwd, capture_output=True, timeout=20)
                    self.assertEqual((hit.returncode, hit.stderr), (0, b""))
                    self.assertIn(b":1:-needle", hit.stdout)
                    human = self.run_cli(*args, cwd=cwd)
                    self.assertEqual((human.returncode, human.stdout), (2, b""))
                    self.assertIn(b".gitignore:1  /source/-", human.stderr)
                    self.assertIn(b"Use --json", human.stderr)
                    self.assertNotIn(b":1:-needle", human.stderr)

    def test_hidden_file_follow_up_round_trips_in_the_shell(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            name = ".- dir '$(echo injected) & echo bad; %TEMP% [x]"
            pattern = '-text " & echo unexpected; \\' + "'$(echo injected) " + chr(96)
            (root / name).write_text(pattern + "\n", encoding="utf-8")
            run = self.run_cli("--", pattern, ".", cwd=root)
            self.assertEqual((run.returncode, run.stderr), (1, b""))
            self.assertIn(b" (hidden)", run.stdout)
            self.assertNotIn(b":1:", run.stdout)
            command = run.stdout.decode().splitlines()[-1].strip()
            hit = self.run_shell(command, root)
            self.assertEqual((hit.returncode, hit.stderr), (0, b""))
            self.assertEqual(hit.stdout.splitlines(), [f"{name}:1:{pattern}".encode()])

    def test_no_follow_up_for_grouped_or_binary_skips_or_a_confirmed_hit(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".ignore").write_bytes(b"generated/\nignored.txt\n")
            (root / "generated").mkdir()
            for name in ("generated/a.txt", "generated/b.txt", "ignored.txt"):
                (root / name).write_bytes(b"needle\n")
            (root / "binary.txt").write_bytes(b"\x00needle")
            (root / "visible.txt").write_bytes(b"needle\n")
            hit = json.loads(self.run_cli("needle", "--json", cwd=root).stdout)
            self.assertEqual(hit["status"], "matched")
            self.assertIsNone(hit["follow_up_argv"])
            (root / "visible.txt").unlink()
            (root / "ignored.txt").unlink()
            miss = json.loads(self.run_cli("needle", "--json", cwd=root).stdout)
            self.assertEqual(miss["status"], "partial")
            self.assertEqual({(s["kind"], s["is_dir"]) for s in miss["skipped_paths"]},
                             {("ignored", True), ("binary", False)})
            self.assertIsNone(miss["follow_up_argv"])

    def test_follow_up_rechecks_the_candidate_is_an_ordinary_file_in_scope(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp, "scope")
            root.mkdir()
            (root / "directory").mkdir()
            (root / ".git").mkdir()
            outside = Path(tmp, "outside")
            outside.write_bytes(b"needle")
            (root / ".git" / "metadata").write_bytes(b"needle")
            try:
                (root / "link").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"Cannot create a symbolic link: {exc}")
            report = dict(status="partial", detail="Some files were skipped.", files_searched=0)
            for path in ("directory", ".git/metadata", "../outside", "link"):
                entry = dict(path=path, kind="ignored", is_dir=False, rule=None)
                with (self.subTest(path=path), redirect_stdout(StringIO()) as out,
                      patch("diditsearch.explain_cli._explain", return_value=dict(report, skipped_paths=[entry]))):
                    self.assertEqual(main(["explain", "--json", "needle", str(root)]), 2)
                    self.assertIsNone(json.loads(out.getvalue())["follow_up_argv"])

    def test_skip_diagnostics_escape_control_characters(self):
        report = dict(status="partial", detail="Search incomplete.", files_searched=1,
                      coverage_complete=True, skipped_paths=[{
                          "path": "first\nsecond\x1b[0m", "kind": "ignored", "is_dir": False,
                          "rule": {"source": "\u202e.gitignore", "line": 2, "pattern": "pat\ttern"},
                      }])
        with patch("diditsearch.explain_cli._explain", return_value=report), redirect_stderr(StringIO()) as err:
            self.assertEqual(main(["explain", "needle"]), 2)
        self.assertIn(r"Skipped: first\nsecond\x1b[0m (ignored)", err.getvalue())
        self.assertIn(r"Rule: \u202e.gitignore:2  pat\ttern", err.getvalue())
        self.assertEqual(len(err.getvalue().splitlines()), 4)

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
