import shutil
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from diditsearch.fixtures import make_fixtures
from diditsearch.reference import inspect_search


@unittest.skipUnless(shutil.which("rg"), "ripgrep is unavailable")
class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))

    def put(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def inspect(self, pattern="needle", path=".", glob=None):
        return inspect_search(self.root, {"path": path, "pattern": pattern, "glob": glob})

    @unittest.skipUnless(shutil.which("git"), "Git is unavailable")
    def test_conformance_fixtures(self):
        for case in make_fixtures(self.root / "fixtures"):
            with self.subTest(case=case.id):
                result = inspect_search(case.root, case.request)
                self.assertEqual(result["status"], case.expected)
                self.assertTrue(result["detail"])
                self.assertEqual(tuple(result.get("skipped", [])), case.skipped)

    def test_literal_and_root_relative_scope(self):
        name = "-caf\u00e9 data/r\u00e9sum\u00e9.txt"
        pattern = "-d\u00e9clenche[.]\nsuite"
        self.put(name, pattern.encode("utf-8"))
        self.put("other.txt", b"outside-only")
        self.assertEqual(self.inspect(pattern, name, "*.txt")["status"], "matched")
        self.assertEqual(self.inspect(pattern, name, "!*.txt")["status"], "no_files")
        self.assertEqual(self.inspect(pattern, name, name)["status"], "matched")
        self.assertEqual(self.inspect(pattern, name, f"/{Path(name).name}")["status"], "no_files")
        self.assertEqual(self.inspect("outside-only", "-caf\u00e9 data")["status"], "no_match")
        self.assertEqual(self.inspect(path=f"{name}/child")["status"], "missing_path")

    def test_glob_anchors_to_each_directory_and_keeps_ancestor_ignores(self):
        self.put(".gitignore", b"/repo/migrations/main/a.js\n")
        self.put("repo/.gitignore", b"/nested/migrations/main/b.js\n")
        self.put("migrations/main/outside.js", b"needle")
        self.put("repo/migrations/main/a.js", b"needle")
        self.put("repo/nested/migrations/main/b.js", b"needle")
        self.put("repo/nested/migrations/main/c.js", b"different")
        self.put("repo/nested/deeper/migrations/main/d.js", b"needle")

        for path in ("repo", "repo/nested"):
            for glob in ("migrations/main/*.js", "/migrations/main/*.js"):
                with self.subTest(path=path, glob=glob):
                    result = self.inspect(path=path, glob=glob)
                    self.assertEqual(result["status"], "partial")
                    self.assertEqual(result["skipped"], ["ignored"])
                    self.assertIn("1 ignored", result["detail"])
        result = self.inspect(path="repo", glob="**/migrations/main/*.js")
        self.assertEqual(result["status"], "matched")
        self.assertIn("2 ignored", result["detail"])

    def test_glob_does_not_override_ignored_directory_or_read_it(self):
        self.put(".gitignore", b"generated/\n")
        self.put("app.py", b"different")
        ignored = self.put("generated/client.ts", b"needle")
        open_path = Path.open

        def guard(path, *args, **kwargs):
            self.assertNotEqual(path, ignored)
            return open_path(path, *args, **kwargs)

        with patch.object(Path, "open", guard):
            for request in ({"glob": "*.ts"}, {"path": "generated"},
                            {"path": "generated/client.ts"}):
                with self.subTest(request=request):
                    result = self.inspect(**request)
                    self.assertEqual(result["status"], "partial")
                    self.assertIn("1 ignored", result["detail"])
            self.assertEqual(self.inspect(glob="*.py")["status"], "no_match")

    def test_binary_and_large_file_exclusions(self):
        self.put("record.txt", b"\0needle")
        self.put("large.txt", b"x" * (1024 * 1024 + 1) + b"\0")
        self.put("plain.txt", b"different")
        result = self.inspect(glob="*.txt")
        self.assertEqual(result["status"], "partial")
        self.assertIn("1 with NUL bytes", result["detail"])
        self.assertIn("1 over 1 MiB", result["detail"])

    def test_proven_hit_wins_and_reports_exclusions(self):
        self.put(".gitignore", b"unused.txt\n")
        self.put("unused.txt", b"different")
        self.put("binary.txt", b"\0needle")
        self.put("hit.txt", b"needle")
        result = self.inspect(glob="*.txt")
        self.assertEqual(result["status"], "matched")
        self.assertIn("1 ignored", result["detail"])
        self.assertIn("1 with NUL bytes", result["detail"])

    def test_git_metadata_exclusion_and_dotfile_scope(self):
        self.put(".git/metadata.txt", b"needle")
        self.put("nested/.git/metadata.txt", b"needle")
        self.put("nested/.git/deep/metadata.txt", b"needle")
        self.put(".hidden.txt", b"different")
        self.assertEqual(self.inspect(glob="*.txt")["status"], "no_match")
        self.assertEqual(self.inspect(glob="**/metadata.txt")["status"], "no_files")
        for path in (".git", "nested", "nested/.git", "nested/.git/deep"):
            with self.subTest(path=path):
                self.assertEqual(self.inspect(path=path, glob="*.txt")["status"], "no_files")
        self.assertEqual(self.inspect("different")["status"], "matched")

    def test_linked_directory_is_skipped_and_cannot_be_explicit(self):
        self.put("actual/entry.txt", b"needle")
        try:
            (self.root / "linked").symlink_to(self.root / "actual", target_is_directory=True)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"Cannot create a directory symlink: {exc}")
        self.assertEqual(self.inspect(glob="linked/**")["status"], "no_files")
        with self.assertRaisesRegex(ValueError, "links"):
            self.inspect(path="linked/entry.txt")

    def test_invalid_inputs_and_tool_errors(self):
        self.put("app.txt", b"needle")
        for request in ({"path": "../escape"}, {"pattern": "a\0b"}, {"glob": 42}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.inspect(**request)
        err = subprocess.CompletedProcess([], 2, b"", b"failed to enumerate")
        with patch("diditsearch.reference.subprocess.run", return_value=err):
            with self.assertRaisesRegex(OSError, "failed to enumerate"):
                self.inspect()
        with patch("diditsearch.reference.subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaisesRegex(OSError, "ripgrep.*PATH"):
                self.inspect()
