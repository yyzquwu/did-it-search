import shutil
import subprocess
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory

from diditsearch.fixtures import make_fixtures


class FixtureTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(TemporaryDirectory())
        self.root = Path(tmp) / "fixtures"

    def test_case_contract(self):
        self.root.mkdir()
        cases = make_fixtures(self.root)
        self.assertEqual([(case.id, case.expected) for case in cases], [
            ("hit", "matched"), ("miss", "no_match"),
            ("missing-path", "missing_path"), ("empty-glob", "no_files"),
            ("ignored-caller", "partial"), ("binary", "partial"),
        ])
        self.assertEqual(set(self.root.iterdir()), {case.root for case in cases})
        for case in cases:
            self.assertEqual(case.root, self.root / case.id)
            self.assertTrue((case.root / ".git" / "HEAD").is_file())
            self.assertEqual(set(case.request), {"pattern", "path", "glob"})
            self.assertFalse(Path(case.request["path"]).is_absolute())
            self.assertTrue(case.reason)
        with self.assertRaises(FrozenInstanceError):
            cases[0].expected = "no_match"

    def test_nonempty_root_is_preserved(self):
        self.root.mkdir()
        original = self.root / "hit"
        original.write_bytes(b"user data")
        with self.assertRaises(ValueError):
            make_fixtures(self.root)
        self.assertEqual(list(self.root.iterdir()), [original])
        self.assertEqual(original.read_bytes(), b"user data")

    @unittest.skipUnless(shutil.which("rg"), "ripgrep is unavailable")
    def test_search_evidence(self):
        cases = {case.id: case for case in make_fixtures(self.root)}

        def rg(case, *opts):
            request = case.request
            glob = ["--glob", request["glob"]] if request["glob"] is not None else []
            args = ["--"] if "--files" in opts else ["--", request["pattern"]]
            return subprocess.run(
                ["rg", "--no-config", "--fixed-strings", "--color=never", *opts,
                 *glob, *args, request["path"]],
                cwd=case.root, capture_output=True,
            )

        for id_, code in [("hit", 0), ("miss", 1), ("missing-path", 2),
                          ("empty-glob", 1), ("ignored-caller", 1), ("binary", 1)]:
            with self.subTest(id=id_):
                self.assertEqual(rg(cases[id_]).returncode, code)

        for id_ in ("miss", "ignored-caller", "binary"):
            self.assertIn(b"app.", rg(cases[id_], "--files").stdout)
        self.assertEqual(rg(cases["empty-glob"], "--files").stdout, b"")
        self.assertEqual(rg(cases["ignored-caller"], "--no-ignore", "--count").returncode, 0)
        self.assertEqual(rg(cases["binary"], "--text", "--count").returncode, 0)
