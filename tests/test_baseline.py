import json
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from diditsearch.baseline import compare, record
from diditsearch.fixtures import Case
from diditsearch.runner import run_checks


class BaselineTests(unittest.TestCase):
    tool = {"name": "test-search", "version": "1.0"}
    next_tool = {"name": "test-search", "version": "2.0"}

    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))

    def case(self, id_, base=None):
        root = (base or self.root) / id_
        root.mkdir(parents=True)
        (root / "app.py").write_bytes(b"target()\n")
        return Case(id_, root, {"pattern": "target", "path": ".", "glob": None},
                    "matched", "The literal occurs in app.py.")

    def run_report(self, cases, *results):
        if not results:
            results = [self.response()] * len(cases)
        with patch("diditsearch.reference.inspect_search", side_effect=results):
            return run_checks(cases)

    def response(self, status="matched", *, skipped=(), text="native response"):
        return {"status": status, "detail": text, "skipped": list(skipped),
                "raw": {"content": [{"text": text, "type": "text"}]}}

    def test_classifies_every_case_without_offsetting_a_regression(self):
        cases = [self.case(id_) for id_ in ("changed", "gain", "known", "loss", "pass")]
        hit, miss = self.response(), self.response("no_match", text="Nothing found")
        before = self.run_report(cases, miss, miss, miss, hit, hit)
        after = self.run_report(cases, self.response("no_match", skipped=("ignored",)), hit,
                                self.response("no_match", text="New wording"), miss, hit)
        self.assertEqual(before["passed"], after["passed"])
        baseline = record(iter(cases), before, self.tool)
        result = compare(json.loads(json.dumps(baseline)), iter(cases), after, self.next_tool)
        self.assertEqual((result["status"], result["exit_code"]), ("regression", 1))
        self.assertEqual((result["baseline_tool"], result["tool"]), (self.tool, self.next_tool))
        self.assertEqual({row["id"]: row["classification"] for row in result["cases"]},
                         {"changed": "changed_failure", "gain": "improvement", "known": "known_failure",
                          "loss": "regression", "pass": "unchanged_pass"})
        loss = next(row for row in result["cases"] if row["id"] == "loss")
        self.assertEqual((loss["request"], loss["expected"]), (cases[3].request, "matched"))
        self.assertEqual((loss["before"]["raw"], loss["after"]["raw"]), (hit["raw"], miss["raw"]))
        self.assertEqual((loss["before"]["actual"], loss["after"]["actual"]), ("matched", "no_match"))

    def test_changed_failure_alone_fails_and_improvement_passes(self):
        cases = [self.case("one")]
        miss = self.run_report(cases, self.response("no_match"))
        baseline = record(cases, miss, self.tool)
        for status, classification, code in [
            ("no_match", "known_failure", 0),
            ("matched", "improvement", 0),
            ("partial", "changed_failure", 1),
        ]:
            with self.subTest(status=status):
                result = compare(baseline, cases, self.run_report(cases, self.response(status)), self.next_tool)
                self.assertEqual(result["exit_code"], code)
                self.assertEqual(result["cases"][0]["classification"], classification)

    def test_report_errors_prevent_recording_and_override_regressions(self):
        cases = [self.case("broken"), self.case("regressed")]
        baseline = record(cases, self.run_report(cases), self.tool)
        report = self.run_report(cases, OSError("search unavailable"), self.response("no_match"))
        with self.assertRaisesRegex(ValueError, "Cannot record.*search unavailable"):
            record(cases, report, self.tool)
        result = compare(baseline, cases, report, self.next_tool)
        self.assertEqual((result["status"], result["exit_code"]), ("infrastructure_error", 2))
        self.assertEqual([row["classification"] for row in result["cases"]], ["infrastructure_error", "regression"])
        self.assertIn("search unavailable", result["cases"][0]["after"]["detail"])

    def test_fingerprint_is_stable_and_covers_fixture_content_and_contract(self):
        one, two = cases = [self.case("one"), self.case("two")]
        (one.root / ".git").mkdir()
        (one.root / ".git" / "config").write_bytes(b"local metadata")
        moved = [self.case(id_, self.root / "moved") for id_ in ("two", "one")]
        expected = record(cases, self.run_report(cases), self.tool)
        actual = record(moved, self.run_report(moved), self.tool)
        self.assertEqual(json.dumps(expected), json.dumps(actual))
        for altered in [
            replace(one, request=dict(one.request, glob="*.py")),
            replace(one, expected="partial"), replace(one, reason="new reason"),
            replace(one, skipped=("ignored",)),
        ]:
            with self.subTest(case=altered):
                self.assertEqual(compare(expected, [altered, two], self.run_report([altered, two]), self.tool)["exit_code"], 2)
        (one.root / "empty").mkdir()
        self.assertEqual(compare(expected, cases, self.run_report(cases), self.tool)["exit_code"], 2)
        (one.root / "empty").rmdir()
        (one.root / "app.py").write_bytes(b"target()\n# added\n")
        self.assertEqual(compare(expected, cases, self.run_report(cases), self.tool)["exit_code"], 2)

    def test_rejects_invalid_duplicate_and_incompatible_baselines(self):
        cases = [self.case("one")]
        report = self.run_report(cases, self.response())
        baseline = record(cases, report, self.tool)
        mutations = [
            lambda b: b.update(version=True), lambda b: b.update(version=99), lambda b: b.update(extra=1),
            lambda b: b.update(cases=[]), lambda b: b.update(fixture_fingerprint=""),
            lambda b: b["cases"].append(deepcopy(b["cases"][0])),
            lambda b: b["cases"][0].update(id="wrong"), lambda b: b["cases"][0].update(passed=1),
            lambda b: b["cases"][0].update(actual=None, passed=False), lambda b: b["cases"][0].update(actual=[]),
            lambda b: b["cases"][0].update(detail=10), lambda b: b["cases"][0].update(skipped=[[]]),
            lambda b: b["cases"][0].update(raw={"x": float("nan")}),
            lambda b: b["cases"][0].update(raw={1: "not a JSON key"}),
            lambda b: b["tool"].update(name="another-tool"), lambda b: b["tool"].update(version=""),
        ]
        for mutate in mutations:
            invalid = deepcopy(baseline)
            mutate(invalid)
            with self.subTest(baseline=invalid):
                result = compare(invalid, cases, report, self.next_tool)
                self.assertEqual(result["exit_code"], 2)
                self.assertTrue(result["error"])
        self.assertEqual(compare(None, cases, report, self.next_tool)["exit_code"], 2)
        self.assertEqual(compare(baseline, cases * 2, report, self.next_tool)["exit_code"], 2)

    def test_rejects_inconsistent_runner_reports(self):
        cases = [self.case("one")]
        report = self.run_report(cases)
        baseline = record(cases, report, self.tool)
        for invalid in (dict(report, errors=1), dict(report, cases=report["cases"] * 2)):
            with self.subTest(report=invalid):
                with self.assertRaises(ValueError):
                    record(cases, invalid, self.tool)
                self.assertEqual(compare(baseline, cases, invalid, self.tool)["exit_code"], 2)

    def test_record_and_compare_do_not_share_mutable_evidence_with_inputs(self):
        cases = [self.case("one")]
        report = self.run_report(cases, self.response("partial", skipped=("ignored",)))
        baseline = record(cases, report, self.tool)
        result = compare(baseline, cases, report, self.next_tool)
        report["cases"][0]["skipped"].clear()
        report["cases"][0]["raw"]["content"].clear()
        self.assertEqual(baseline["cases"][0]["skipped"], ["ignored"])
        self.assertTrue(baseline["cases"][0]["raw"]["content"])
        result["cases"][0]["before"]["raw"]["content"].clear()
        self.assertTrue(baseline["cases"][0]["raw"]["content"])
