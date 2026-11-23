import hashlib
import io
import os
import tarfile
import unittest
from base64 import b64encode
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from diditsearch import pi_replay as pi


def _report(hit="matched", decoy="no_match", errors=0):
    cases = [
        {"id": id_, "actual": actual, "raw": {"content": [{"type": "text", "text": actual}]}}
        for id_, actual in zip(pi.CASES, (hit, decoy))
    ]
    return dict(cases=cases, passed=int(hit == "matched") + int(decoy == "no_match"), errors=errors)


class PiReplayTests(unittest.TestCase):
    def test_requires_the_specific_old_mismatch_and_fixed_outcome(self):
        old, fixed = _report(hit="no_match"), _report()
        self.assertEqual(pi._verdict(old, fixed), "reproduced")
        self.assertEqual(pi._verdict(fixed, fixed), "unexpected_parity")
        self.assertEqual(pi._verdict(old, old), "unexpected_parity")
        self.assertEqual(pi._verdict(old, _report(decoy="matched")), "unexpected_result")
        self.assertEqual(pi._verdict(old, _report(errors=1)), "infrastructure_error")
        del fixed["cases"][0]["raw"]
        self.assertEqual(pi._verdict(old, fixed), "infrastructure_error")

    def test_setup_failure_retains_pins_and_has_no_conformance_verdict(self):
        with patch.object(pi.shutil, "which", return_value=None):
            report = pi.replay()
        self.assertEqual(report["status"], "infrastructure_error")
        self.assertFalse(report["passed"])
        self.assertEqual(report["before"], {"commit": pi.BEFORE})
        self.assertEqual(report["after"], {"commit": pi.AFTER})
        self.assertIn("Node.js", report["error"])

    def test_registry_integrity_and_archive_scope(self):
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            file = tarfile.TarInfo("package/../../escaped")
            tar.addfile(file)
        data = archive.getvalue()
        entry = dict(resolved="https://registry.npmjs.org/demo/-/demo-1.0.0.tgz", integrity="sha512-bad")
        with TemporaryDirectory() as tmp:
            dst = Path(tmp) / "demo"
            with patch.object(pi, "urlopen", return_value=io.BytesIO(data)):
                with self.assertRaisesRegex(ValueError, "integrity"):
                    pi._install(dst, "demo", entry)
            entry["integrity"] = "sha512-" + b64encode(hashlib.sha512(data).digest()).decode("ascii")
            with patch.object(pi, "urlopen", return_value=io.BytesIO(data)):
                with self.assertRaisesRegex(ValueError, "archive path"):
                    pi._install(dst, "demo", entry)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    @unittest.skipUnless(os.environ.get("DIDITSEARCH_TEST_PI_REPLAY") == "1", "requires GitHub and npm registry")
    def test_actual_upstream_red_green(self):
        report = pi.replay()
        self.assertEqual(report["status"], "reproduced", report)
        self.assertTrue(report["passed"])
        for key, sha, outcomes in (
            ("before", pi.BEFORE, [("matched", "no_match", False), ("no_match", "no_match", True)]),
            ("after", pi.AFTER, [("matched", "matched", True), ("no_match", "no_match", True)]),
        ):
            rev = report[key]
            self.assertEqual(rev["commit"], sha)
            self.assertEqual(rev["errors"], 0)
            self.assertEqual([c["id"] for c in rev["cases"]], list(pi.CASES))
            self.assertEqual([(c["expected"], c["actual"], c["passed"]) for c in rev["cases"]], outcomes)
            self.assertEqual(rev["cases"][1]["raw"]["content"], [{"type": "text", "text": "No matches found"}])
        self.assertEqual(report["before"]["cases"][0]["raw"]["content"][0]["text"], "No matches found")
        self.assertEqual(report["after"]["cases"][0]["raw"]["content"][0]["text"],
                         'migrations/main/a.js:1: console.log("hello");')
