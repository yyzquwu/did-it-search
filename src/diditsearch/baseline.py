"""Record and compare versioned, JSON-ready search baselines."""

import json
import os
import re
import stat
from hashlib import file_digest, sha256

from .runner import _STATUSES

_OUTCOME = {"actual", "detail", "passed", "skipped"}
_NUM = r"(?:0|[1-9][0-9]*)"
_PRE = rf"(?:{_NUM}|[0-9]*[A-Za-z-][A-Za-z0-9-]*)"
_PI_VERSION = re.compile(rf"{_NUM}\.{_NUM}\.{_NUM}(?:-{_PRE}(?:\.{_PRE})*)?(?:\+[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)?")


def _encode(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _tool(tool):
    if (not isinstance(tool, dict) or tool.keys() != {"name", "version"}
            or not all(isinstance(value, str) and value.strip() for value in tool.values())):
        raise ValueError("Tool identity must contain a nonempty name and version")
    if tool["name"] == "@earendil-works/pi-coding-agent" and not _PI_VERSION.fullmatch(tool["version"]):
        raise ValueError("Invalid Pi SDK version")
    return {"name": tool["name"], "version": tool["version"]}


def _case_index(cases):
    index = {}
    for case in cases:
        if not isinstance(case.id, str) or not case.id or case.id in index:
            raise ValueError(f"Invalid or duplicate fixture ID: {case.id!r}")
        index[case.id] = case
    return dict(sorted(index.items()))


def _contents(path, root):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"Unsupported link in fixture: {path.relative_to(root)}")
    relative = path.relative_to(root).as_posix()
    if stat.S_ISDIR(info.st_mode):
        yield [relative, None]
        metadata = path / ".git"
        for child in sorted(path.iterdir(), key=lambda p: p.name):
            if child != metadata:
                yield from _contents(child, root)
    elif stat.S_ISREG(info.st_mode) and path != root:
        with path.open("rb") as stream:
            yield [relative, file_digest(stream, "sha256").hexdigest()]
    else:
        raise ValueError(f"Unsupported fixture entry: {relative}")


def _fingerprint(cases):
    contract = [[c.id, c.request, c.expected, c.reason, list(c.skipped), list(_contents(c.root, c.root))]
                for c in cases.values()]
    return sha256(_encode(contract).encode("ascii")).hexdigest()


def _index(rows, cases, label):
    if not isinstance(rows, list):
        raise ValueError(f"{label} cases must be an array")
    index = {}
    for row in rows:
        id_ = row.get("id") if isinstance(row, dict) else None
        if not isinstance(id_, str) or not id_ or id_ in index:
            raise ValueError(f"{label} has an invalid or duplicate case ID: {id_!r}")
        index[id_] = row
    if index.keys() != cases.keys():
        raise ValueError(f"{label} case IDs do not match the fixtures")
    return index


def _outcome(row, case, *, baseline=False):
    keys = _OUTCOME | ({"raw"} if "raw" in row else set())
    if not _OUTCOME <= row.keys() or (baseline and row.keys() != keys | {"id"}):
        raise ValueError(f"Invalid outcome fields for {case.id!r}")
    actual, skipped = row["actual"], row["skipped"]
    if actual is None:
        if baseline or skipped != [] or "raw" in row:
            raise ValueError(f"Invalid runner error outcome for {case.id!r}")
    elif not isinstance(actual, str) or actual not in _STATUSES:
        raise ValueError(f"Invalid actual status for {case.id!r}")
    if (not isinstance(skipped, list) or not all(isinstance(s, str) and s for s in skipped)
            or skipped != sorted(set(skipped)) or not isinstance(row["detail"], str)
            or row["passed"] is not (actual == case.expected and skipped == sorted(case.skipped))):
        raise ValueError(f"Invalid outcome for {case.id!r}")
    outcome = {key: row[key] for key in sorted(keys)}
    outcome["skipped"] = skipped.copy()
    if "raw" in outcome:
        raw = outcome["raw"]
        outcome["raw"] = json.loads(_encode(raw))
        if outcome["raw"] != raw:
            raise ValueError(f"Raw outcome is not a JSON value for {case.id!r}")
    return outcome


def _portable_evidence(value, roots):
    if isinstance(value, str):
        return roots.sub(lambda m: "<fixture>/" if m.group(1) else "<fixture>", value)
    if isinstance(value, list):
        return [_portable_evidence(item, roots) for item in value]
    if isinstance(value, dict):
        normalized = {_portable_evidence(k, roots): _portable_evidence(v, roots) for k, v in value.items()}
        if len(normalized) != len(value):
            raise ValueError("Fixture path normalization would merge distinct evidence keys")
        return normalized
    return value


def _report(report, cases):
    if not isinstance(report, dict) or type(report.get("version")) is not int or report["version"] != 1:
        raise ValueError("Invalid runner report version")
    rows = _index(report.get("cases"), cases, "Runner report")
    outcomes = {}
    for id_, case in cases.items():
        row = rows[id_]
        if (row.get("expected") != case.expected or row.get("reason") != case.reason
                or row.get("expected_skipped") != list(case.skipped)):
            raise ValueError(f"Runner report does not match fixture {id_!r}")
        outcome = _outcome(row, case)
        paths = {s for root in (case.root.absolute(), case.root.resolve())
                 for s in (str(root), root.as_posix())}
        roots = re.compile("(?:" + "|".join(re.escape(s) for s in sorted(paths, key=len, reverse=True))
                           + r""")([/\\]|(?=[.,;:!?]*(?:$|[\s"'()`<>\[\]{}])))""",
                           re.IGNORECASE if os.name == "nt" else 0)
        outcomes[id_] = _portable_evidence(outcome, roots)
    counts = {"passed": sum(row["passed"] for row in outcomes.values()), "total": len(outcomes),
              "errors": sum(row["actual"] is None for row in outcomes.values())}
    if any(type(report.get(key)) is not int or report[key] != count for key, count in counts.items()):
        raise ValueError("Runner report counts do not match case outcomes")
    return outcomes


def record(cases, report, tool: dict) -> dict:
    """Return a JSON-ready baseline from fixtures.Case objects and a run_checks report.

    Tool identity is an object with name and version strings. Raises ValueError
    for invalid inputs or runner errors, and OSError for unreadable fixtures.
    """
    cases = _case_index(cases)
    tool = _tool(tool)
    outcomes = _report(report, cases)
    for id_, row in outcomes.items():
        if row["actual"] is None:
            raise ValueError(f"Cannot record a baseline with runner errors ({id_}): {row['detail']}")
    return {"version": 1, "tool": tool, "fixture_fingerprint": _fingerprint(cases),
            "cases": [dict(id=id_, **row) for id_, row in outcomes.items()]}


def _classification(before, after):
    if after["actual"] is None:
        return "infrastructure_error"
    if before["passed"] != after["passed"]:
        return "improvement" if after["passed"] else "regression"
    if after["passed"]:
        return "unchanged_pass"
    return ("known_failure" if (before["actual"], before["skipped"]) == (after["actual"], after["skipped"])
            else "changed_failure")


def compare(baseline, cases, report, tool: dict) -> dict:
    """Compare a decoded baseline with a run_checks report over the same fixtures.

    Returns version, tool, baseline_tool, status, exit_code, and cases. Each
    comparable case has id, classification, request, expected, expected_skipped,
    and before/after outcomes (including raw native responses when supplied).
    Failure identity uses normalized status and exclusions. Tool versions may
    differ. Exit codes are 0 for unchanged/improved results, 1 for regressions
    or changed failures, and 2 for invalid/incompatible inputs or runner errors.
    Code 2 includes an error message.
    """
    result = {"version": 1, "status": "infrastructure_error", "exit_code": 2,
              "tool": None, "baseline_tool": None, "cases": []}
    try:
        result["tool"] = _tool(tool)
        if (not isinstance(baseline, dict) or baseline.keys() != {"version", "tool", "fixture_fingerprint", "cases"}
                or type(baseline["version"]) is not int or baseline["version"] != 1):
            raise ValueError("Invalid baseline schema or unsupported version")
        result["baseline_tool"] = _tool(baseline["tool"])
        if result["tool"]["name"] != result["baseline_tool"]["name"]:
            raise ValueError("Baseline tool name does not match the current tool")
        cases = _case_index(cases)
        previous = _index(baseline["cases"], cases, "Baseline")
        if baseline["fixture_fingerprint"] != _fingerprint(cases):
            raise ValueError("Baseline fixture fingerprint does not match the current fixtures")
        current = _report(report, cases)
        rows = []
        for id_, case in cases.items():
            before, after = _outcome(previous[id_], case, baseline=True), current[id_]
            rows.append(dict(id=id_, classification=_classification(before, after),
                             request=json.loads(_encode(case.request)), expected=case.expected,
                             expected_skipped=list(case.skipped), reason=case.reason, before=before, after=after))
        result["cases"] = rows
        kinds = {row["classification"] for row in rows}
        if "infrastructure_error" in kinds:
            errors = [row["id"] for row in rows if row["classification"] == "infrastructure_error"]
            raise ValueError(f"Runner errors in: {', '.join(errors)}")
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        return dict(result, error=str(exc))
    regression = bool(kinds & {"regression", "changed_failure"})
    return dict(result, exit_code=int(regression), status="regression" if regression else "passed")
