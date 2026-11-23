"""Render baseline comparisons for terminals and GitHub step summaries."""
import json
from collections import Counter
from html import escape


def format_summary(report, *, markdown=False):
    counts = Counter(c["classification"] for c in report["cases"])
    status = {0: "PASS", 1: "FAIL", 2: "ERROR"}[report["exit_code"]]
    title = f"Search regression check — {status}"
    lines = []
    for key, label in (("baseline_tool", "Baseline"), ("tool", "Current")):
        tool = report.get(key)
        if tool:
            lines.append(f"{label}: {tool['name']} {tool['version']}")
    lines.append(" | ".join(f"{label}: {counts[key]}" for key, label in (
        ("regression", "Regressions"), ("improvement", "Improvements"),
        ("changed_failure", "Changed failures"), ("known_failure", "Unchanged known failures"))))
    if report.get("error"):
        lines.append(str(report["error"]))
    for c in report["cases"]:
        if c["classification"] in ("known_failure", "unchanged_pass"):
            continue
        lines += ["", f"{c['classification'].upper()}  {c['id']}",
                  "Request: " + json.dumps(c["request"], ensure_ascii=False),
                  f"Expected: {c['expected']} (skipped: {c['expected_skipped']})"]
        for side in ("before", "after"):
            outcome = c.get(side)
            if outcome:
                lines.append(f"{side.capitalize()}: {outcome['actual']} (skipped: {outcome['skipped']})")
                if "raw" in outcome:
                    lines.append(json.dumps(outcome["raw"], ensure_ascii=False))
                elif outcome.get("detail"):
                    lines.append(outcome["detail"])
        if c.get("reason"):
            lines.append(c["reason"])
    body = "\n".join(lines)
    if markdown:
        return f"## {title}\n\n<pre>{escape(body)}</pre>"
    return f"{title}\n{body}"
