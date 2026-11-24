import json
import os
import shutil
import subprocess
import unittest
from contextlib import chdir
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from diditsearch.explain import _rg_path, explain


@unittest.skipUnless(shutil.which("rg"), "ripgrep is unavailable")
class ExplainTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))

    def put(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def check(self, status, count, pattern="needle", path=".", glob=None, **kwargs):
        return self.report(status, count, pattern, path, glob, **kwargs)["detail"]

    def report(self, status, count, pattern="needle", path=".", glob=None, **kwargs):
        kwargs.setdefault("timeout", 20.0)
        result = explain(pattern, self.root / path, glob, **kwargs)
        self.assertEqual(set(result), {"status", "detail", "files_searched", "skipped_paths", "coverage_complete"})
        self.assertEqual((result["status"], result["files_searched"]), (status, count), result)
        self.assertTrue(result["detail"])
        self.assertIsInstance(result["coverage_complete"], bool)
        self.assertEqual(json.loads(json.dumps(result)), result)
        return result

    @staticmethod
    def skip(path, kind, is_dir=False, rule=None):
        return dict(path=path, kind=kind, is_dir=is_dir, rule=rule)

    def test_literal_case_and_exact_count(self):
        self.put("plain.txt", b"-needle[.]\nsecond\n")
        self.put("empty.txt", b"")
        self.put("case.txt", b"-NEEDLE[.]\nsecond\n")
        self.put("-", b"different")
        self.check("matched", 4, "-needle[.]\nsecond")
        self.check("no_match", 4, "-needle.")
        self.check("no_match", 4, "-Needle[.]")
        self.check("matched", 1, "different", "-")
        self.check("no_match", 1, path="empty.txt")

    @unittest.skipUnless(os.name == "nt", "Windows executable lookup")
    def test_executable_resolution_uses_absolute_path_entries(self):
        installed = _rg_path()
        self.assertIsNotNone(installed)
        self.put("rg.exe", b"")
        with chdir(self.root), patch.dict(os.environ, {"PATH": os.pathsep.join((".", str(Path(installed).parent)))}):
            self.assertEqual(_rg_path(), installed)
            with patch.dict(os.environ, {"PATH": "."}):
                self.assertIsNone(_rg_path())

    def test_missing_empty_and_glob_scope(self):
        self.put("other/migrations/a.js", b"needle")
        self.put("repo/migrations/a.js", b"different")
        self.put("repo/deep/migrations/a.js", b"needle")
        self.check("missing_path", 0, path="missing")
        self.check("missing_path", 0, path="repo/migrations/a.js/child")
        self.check("no_files", 0, path="repo", glob="*.rs")
        self.check("no_match", 1, path="repo", glob="/migrations/*.js")
        self.check("matched", 2, path="repo", glob="**/migrations/*.js")
        self.check("no_files", 0, path="repo/deep/migrations/a.js", glob="!*.js")
        self.check("error", None, path="repo", glob="[")
        self.check("error", None, pattern="bad\0pattern")

    def test_glob_and_explicit_paths_cannot_override_ignores(self):
        self.put(".gitignore", b"/repo/generated/\n/repo/ignored.ts\n")
        self.put("repo/generated/a.ts", b"needle")
        self.put("repo/ignored.ts", b"needle")
        self.put("repo/kept.ts", b"different")
        self.put("outside.ts", b"needle")
        for path, count in (("repo", 1), ("repo/generated", 0), ("repo/ignored.ts", 0),
                            ("repo/generated/a.ts", 0)):
            with self.subTest(path=path):
                detail = self.check("partial", count, path=path, glob="*.ts")
                self.assertIn("ignored", detail)
        self.check("matched", 1, "different", "repo", "*.ts")
        self.check("no_files", 0, path="repo", glob="*.rs")

    def test_explicit_pruning_handles_glob_metacharacters_and_unicode(self):
        self.put(".ignore", b"unrelated/\n")
        folder = "caf\u00e9[!,^-]"
        self.put(f"{folder}/target.txt", b"different")
        self.put(f"{folder}/target.txt.extra", b"needle")
        self.put(f"{folder}/target.tx", b"needle")
        self.put(f"{folder}more/target.txt", b"needle")
        self.check("no_match", 1, path=f"{folder}/target.txt", glob="*.txt")

    def test_binary_requires_full_classification_and_utf16_is_decoded(self):
        self.put("early.txt", b"\x00needle")
        self.put("late.txt", b"needle\n" + b"x\n" * 40_000 + b"\x00")
        self.put("plain.txt", b"different")
        detail = self.check("partial", 1)
        self.assertIn("2 binary", detail)
        self.put("utf16.txt", "needle".encode("utf-16"))
        detail = self.check("matched", 2)
        self.assertIn("2 binary", detail)
        self.check("partial", 0, path="late.txt")

    def test_hidden_git_metadata_and_configuration(self):
        self.put(".git/metadata.txt", b"needle")
        self.put(".hidden.txt", b"needle")
        self.put("scope/visible.txt", b"NEEDLE")
        config = self.put("other/config", b"--ignore-case\n--hidden\n--no-ignore\n")
        with patch.dict(os.environ, {"RIPGREP_CONFIG_PATH": str(config)}):
            self.check("no_match", 1, path="scope")
            self.check("no_match", 2)
        self.check("matched", 2, glob="*.txt")
        self.put(".ignore", b"!.hidden.txt\n")
        self.check("matched", 3)
        self.check("matched", 2, glob="!visible.txt")
        self.check("no_files", 0, path=".git")
        self.check("no_files", 0, glob="**/metadata.txt")

    def test_nested_repository_uses_its_own_gitignore(self):
        (self.root / ".git").mkdir()
        self.put(".gitignore", b"/child/\n")
        self.put("child/main.py", b"needle\n")
        (self.root / "child/.git").mkdir()
        self.check("matched", 1, path="child")
        self.check("matched", 1, path="child/main.py")

    def test_explicit_and_reincluded_hidden_directories(self):
        self.put(".github/workflows/ci.yml", b"needle\n")
        self.check("matched", 1, path=".github/workflows")
        self.put(".ignore", b"!.github/\n")
        self.check("matched", 1, glob="*.yml")
        self.put("outside.yml", b"other\n")
        self.check("matched", 2, glob="*.yml")
        self.put(".ignore", b".github/\n")
        self.assertIn("ignored", self.check("partial", 0, path=".github/workflows"))

    def test_parent_ripgrep_ignore_can_exclude_a_nested_repository(self):
        self.put("inner/main.py", b"needle\n")
        (self.root / "inner/.git").mkdir()
        for name in (".ignore", ".rgignore"):
            with self.subTest(name=name):
                rule = self.put(name, b"/inner/\n")
                self.check("partial", 0, path="inner/main.py")
                self.check("partial", 0, path="inner")
                rule.unlink()
                self.check("matched", 1, path="inner/main.py")

    def test_outer_rule_preserves_selection_and_skips_ignored_subtrees(self):
        (self.root / "inner/.git").mkdir(parents=True)
        self.put("inner/.ignore", b"!.visible\n")
        self.put("inner/.visible", b"needle\n")
        self.put(".ignore", b"/inner/\n")
        self.assertIn("ignored", self.check("partial", 0, path="inner"))
        self.put(".ignore", b"/unrelated/\n")
        self.put("inner/.gitignore", b"generated/\n")
        self.put("inner/generated/.ignore", b"[z-a]\n")
        self.put("inner/generated/nested/file", b"ignored\n")
        self.check("matched", 1, path="inner")
        for i in range(70):
            self.put(f"inner/pkgs/pkg{i}/main.py", b"other\n")
        self.check("matched", 71, path="inner", timeout=30)

    def test_many_ignored_siblings_do_not_hide_one_eligible_file(self):
        (self.root / "inner/.git").mkdir(parents=True)
        self.put(".ignore", b"/unrelated/\n")
        self.put("inner/.gitignore", b"cache-*/\n")
        self.put("inner/main.txt", b"needle\n")
        # A smaller argv budget triggers the same fallback with fewer directories.
        for i in range(30):
            (self.root / "inner" / f"cache-generated-exports-{i:03}").mkdir()
        with patch("diditsearch.explain._ARG_LIMIT", 1024):
            self.check("matched", 1, path="inner")
            self.put(".ignore", b"/inner/\n")
            self.check("partial", 0, path="inner")

    def test_linked_paths_and_descendants(self):
        self.put("outside/file.txt", b"needle")
        self.put("scope/file.txt", b"different")
        try:
            (self.root / "scope" / "link").symlink_to(self.root / "outside", target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"Cannot create a directory symlink: {exc}")
        self.assertEqual(self.report("no_match", 1, path="scope")["skipped_paths"], [])
        self.check("error", None, path="scope/link/file.txt")

    def test_unreadable_file_keeps_count_unknown_and_preserves_a_confirmed_hit(self):
        locked = self.put("blocked.txt", b"different")
        self.put("available.txt", b"needle")
        if os.name == "nt":
            import msvcrt

            file = self.enterContext(locked.open("r+b"))
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            locked.chmod(0)
            self.addCleanup(locked.chmod, 0o600)
            if os.access(locked, os.R_OK):
                self.skipTest("This user can read files with no read permissions")
        self.assertIn("could not read", self.check("partial", None, "absent"))
        self.check("matched", None)
        self.put(".ignore", b"blocked.txt\n")
        self.assertIn("ignored", self.check("partial", 1, "absent", glob="*.txt"))

    def test_deadline_output_and_process_limits(self):
        self.put("directory-with-a-long-name/file.txt", b"needle")
        self.assertIn("time limit", self.check("partial", None, timeout=0))
        with patch("diditsearch.explain._OUTPUT_LIMIT", 12):
            self.assertIn("output limit", self.check("partial", None))
        with patch("diditsearch.explain._PROCESS_LIMIT", 2):
            self.assertIn("process limit", self.check("partial", None))
        with patch("diditsearch.explain._PROCESS_LIMIT", 3):
            report = self.report("matched", 1)
            self.assertIn("process limit", report["detail"])
            self.assertFalse(report["coverage_complete"])

    def test_accounting_flags_and_observed_evidence_on_interruption(self):
        self.put(".gitignore", b"generated/\n")
        self.put("generated/client.ts", b"needle")
        self.put("a.txt", b"needle")
        self.put("b.txt", b"\0needle")
        self.assertFalse(self.report("missing_path", 0, path="missing")["coverage_complete"])
        self.assertFalse(self.report("partial", None, timeout=0)["coverage_complete"])
        self.assertTrue(self.report("no_files", 0, glob="*.rs")["coverage_complete"])
        with patch("diditsearch.explain._PROCESS_LIMIT", 4):
            report = self.report("matched", 1)
        self.assertFalse(report["coverage_complete"])
        self.assertEqual(report["skipped_paths"], [self.skip("generated/client.ts", "ignored"),
                                                   self.skip("b.txt", "binary")])

    def test_hidden_directory_and_binary_are_reported_separately(self):
        self.put(".gitignore", b"other/\n")
        self.put(".git/metadata.txt", b"needle")
        self.put("nested/.github/workflows/ci.yml", b"needle")
        self.put("visible/.a.txt", b"needle")
        self.put("visible/.b.txt", b"needle")
        self.put("z.bin", b"\x00needle")
        self.put("a.txt", b"other")
        report = self.report("partial", 1)
        self.assertTrue(report["coverage_complete"])
        self.assertEqual(report["skipped_paths"], [self.skip("nested/.github", "hidden", True),
                                                   self.skip("z.bin", "binary"),
                                                   self.skip("visible/.a.txt", "hidden")])
        report = self.report("no_files", 0, path="nested")
        self.assertTrue(report["coverage_complete"])
        self.assertEqual(report["skipped_paths"], [self.skip(".github", "hidden", True)])
        report = self.report("matched", 1, path="nested/.github")
        self.assertEqual(report["skipped_paths"], [])

    def test_grouping_is_stable_and_keeps_single_ignored_file(self):
        self.put(".gitignore", b"generated/\ncache-*/\n")
        self.put("generated/client.ts", b"needle")
        self.put("cache-b/sub/b.ts", b"needle")
        self.put("cache-b/a.ts", b"needle")
        self.put("cache-a/a.ts", b"needle")
        self.put("cache-a/b.ts", b"needle")
        self.put("visible.txt", b"other")
        expected = [self.skip("cache-a", "ignored", True), self.skip("cache-b", "ignored", True),
                    self.skip("generated/client.ts", "ignored")]
        for _ in range(2):
            report = self.report("partial", 1)
            self.assertTrue(report["coverage_complete"])
            self.assertEqual(report["skipped_paths"], expected)

    def test_a_subtree_with_searched_files_keeps_specific_ignored_examples(self):
        self.put(".gitignore", b"*.generated.ts\n")
        self.put("src/a.generated.ts", b"needle")
        self.put("src/b.generated.ts", b"needle")
        self.put("src/app.ts", b"other")
        self.assertEqual(self.report("partial", 1)["skipped_paths"], [
            self.skip("src/a.generated.ts", "ignored"), self.skip("src/b.generated.ts", "ignored"),
        ])

    def test_explicit_file_paths_are_relative_to_parent(self):
        self.put(".gitignore", b"generated/\n")
        self.put("generated/client.ts", b"needle")
        report = self.report("partial", 0, path="generated/client.ts")
        self.assertEqual(report["skipped_paths"], [self.skip("client.ts", "ignored")])
        report = self.report("no_files", 0, path="generated/client.ts", glob="*.rs")
        self.assertEqual(report["skipped_paths"], [self.skip("client.ts", "excluded")])

    @unittest.skipUnless(shutil.which("git"), "Git is unavailable")
    def test_ignored_caller_gets_verified_rule_in_git_repository(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True, timeout=10)
        self.put(".gitignore", b"# generated SDK\ngenerated/\n")
        self.put("generated/client.ts", b"legacy_export();\n")
        self.put("app.ts", b"startServer();\n")
        report = self.report("partial", 1, "legacy_export")
        self.assertTrue(report["coverage_complete"])
        self.assertEqual(report["skipped_paths"], [self.skip("generated/client.ts", "ignored", rule={
            "source": ".gitignore", "line": 2, "pattern": "generated/",
        })])
