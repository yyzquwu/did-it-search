"""Command-line entry points for the search conformance kit."""
import argparse
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from .fixtures import make_fixtures
from .reference import inspect_search
from .runner import run_checks


@contextmanager
def _workspace():
    tmp = TemporaryDirectory(prefix="did-it-search-")
    try:
        yield Path(tmp.name)
    finally:
        try:
            tmp.cleanup()
        except OSError as exc:
            print(f"did-it-search: could not remove temporary fixtures at {tmp.name}: {exc}", file=sys.stderr)


def _export(dest: Path) -> None:
    if os.path.lexists(dest):
        raise FileExistsError(f"Choose a new directory: {dest}")
    if not dest.parent.is_dir():
        raise NotADirectoryError(f"Fixture destination parent must exist: {dest.parent}")
    with TemporaryDirectory(dir=dest.parent) as tmp:
        stage = Path(tmp)
        cases = make_fixtures(stage)
        manifest = [
            dict(id=c.id, directory=c.id, request=c.request, expected=c.expected,
                 expected_skipped=list(c.skipped), reason=c.reason)
            for c in cases
        ]
        (stage / "cases.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        dest.mkdir()
        for item in stage.iterdir():
            item.rename(dest / item.name)
    print(f"Wrote {len(cases)} search fixtures to {dest}")


def _demo(cases) -> None:
    case = next(c for c in cases if c.id == "ignored-caller")
    cmd = ["rg", "--no-config", "-F", "--", case.request["pattern"], "."]
    try:
        raw = subprocess.run(cmd, cwd=case.root, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise OSError("ripgrep (rg) must be available on PATH") from exc
    if raw.returncode not in (0, 1):
        raise ValueError(raw.stderr.decode("utf-8", "replace").strip() or "ripgrep could not run")
    answer = inspect_search(case.root, case.request)
    print("$", " ".join(cmd))
    print(f"exit {raw.returncode} | stdout: {raw.stdout.decode('utf-8', 'replace').strip() or '(empty)'}")
    print(f"Scope: {answer['status']} | {answer['detail']}")
    print(f"Fixture answer: {case.reason}")


def _replay_pi(as_json: bool) -> int:
    from .pi_replay import replay

    report = replay()
    if as_json:
        print(json.dumps(report, indent=2, allow_nan=False))
    else:
        for label in ("before", "after"):
            rev = report[label]
            print(f"{label} {rev['commit'][:12]}")
            for case in rev.get("cases", []):
                mark = "ERROR" if case["actual"] is None else "PASS" if case["passed"] else "FAIL"
                print(f"  {mark:5} {case['id']:16} {case['detail']}")
        print(f"Pi grep replay: {report['status']}")
    if report.get("error"):
        print(f"did-it-search: {report['error']}", file=sys.stderr)
    return 2 if report["status"] == "infrastructure_error" else int(not report["passed"])


def _pi_adapter():
    env = dict(os.environ, DIDITSEARCH_PI_ROOT=str(Path.cwd()))
    env.pop("DIDITSEARCH_PI_MODULE", None)
    return ["node", str(Path(__file__).with_name("pi_adapter.mjs"))], env


def _save_baseline(path, value, replace):
    data = json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False) + "\n"
    with NamedTemporaryFile(dir=path.parent, prefix=".didit-", delete=False) as tmp:
        name = Path(tmp.name)
    try:
        name.write_bytes(data.encode("utf-8"))
        if replace:
            os.replace(name, path)
        else:
            os.link(name, path)
    finally:
        name.unlink(missing_ok=True)


def _baseline_cmd(args):
    from .baseline import compare, record
    from .summary import format_summary

    creating = args.cmd == "baseline"
    path = args.output if creating else args.baseline
    if creating and not args.replace and os.path.lexists(path):
        raise ValueError(f"Baseline already exists: {path}. Use --replace for an intentional update.")
    if not creating:
        baseline = json.loads(path.read_bytes(), object_pairs_hook=_unique_keys)
    adapter, env = _pi_adapter()
    identity = subprocess.run([*adapter, "--identify"], env=env, capture_output=True, timeout=60)
    if identity.returncode:
        raise ValueError(identity.stderr.decode("utf-8", "replace").strip() or "Pi identification failed")
    tool = json.loads(identity.stdout)
    with _workspace() as tmp:
        cases = make_fixtures(tmp)
        report = run_checks(cases, adapter=adapter, timeout=60, env=env)
        if creating:
            value = record(cases, report, tool)
            _save_baseline(path, value, args.replace)
            print(f"Recorded {len(cases)} search cases for {tool['name']} {tool['version']} in {path}")
            return 0
        result = compare(baseline, cases, report, tool)
    if args.json:
        print(json.dumps(result, indent=2, allow_nan=False))
    else:
        print(format_summary(result, markdown=args.markdown))
    return result["exit_code"]


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate baseline key: {key}")
        result[key] = value
    return result


def main(argv=None) -> int:
    if argv is None:
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(prog="did-it-search", description="Explain a local search or check a search tool for regressions.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('did-it-search')}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="see a zero-match search with a real caller")
    explain = sub.add_parser("explain", help="explain the scope of a local literal search", allow_abbrev=False,
                             epilog="Place -- before PATTERN to pass a dash-prefixed literal or path.")
    explain.add_argument("pattern", metavar="PATTERN")
    explain.add_argument("path", nargs="?", default=".", metavar="PATH")
    explain.add_argument("--glob", metavar="GLOB", help="restrict the files searched")
    explain.add_argument("--timeout", type=float, default=5.0, metavar="SECONDS",
                         help="total search time limit (default: 5)")
    explain.add_argument("--json", action="store_true", help="print a machine-readable report")
    fixtures = sub.add_parser("fixtures", help="write the search repos and their answer key")
    fixtures.add_argument("directory", type=Path)
    check = sub.add_parser("check", help="grade the reference or your own search adapter")
    check.add_argument("--json", action="store_true", help="print a machine-readable report")
    check.add_argument("--timeout", type=float, metavar="SECONDS",
                       help="per invocation (60 seconds for Pi, 10 for other adapters)")
    check.add_argument("--case", action="append", dest="case_ids", metavar="ID",
                       help="run a specific fixture (repeat to select several)")
    mode = check.add_mutually_exclusive_group()
    mode.add_argument("--tool", choices=["pi"], help="grade the actual installed search tool")
    mode.add_argument("--adapter", nargs=argparse.REMAINDER, metavar="ARGV",
                       help="external command and arguments, or a Python script; put this option last")
    replay = sub.add_parser("replay", help="reproduce a pinned upstream regression")
    replay.add_argument("tool", choices=["pi"])
    replay.add_argument("--json", action="store_true")
    baseline = sub.add_parser("baseline", help="record a reviewable Pi search baseline")
    baseline.add_argument("--tool", choices=["pi"], default="pi")
    baseline.add_argument("--output", type=Path, default=Path(".did-it-search.json"))
    baseline.add_argument("--replace", action="store_true", help="replace an existing baseline intentionally")
    compare = sub.add_parser("compare", help="check for regressions against a reviewed baseline")
    compare.add_argument("--tool", choices=["pi"], default="pi")
    compare.add_argument("--baseline", type=Path, default=Path(".did-it-search.json"))
    fmt = compare.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true")
    fmt.add_argument("--markdown", action="store_true", help="format the report for a GitHub step summary")
    args = parser.parse_args(argv)
    if args.cmd == "compare" and args.markdown and argv is None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if args.cmd == "check" and args.adapter == []:
        parser.error("--adapter needs a command or Python script")
    if args.cmd == "explain":
        from .explain_cli import explain_cmd

        if argv is None:
            for stream in (sys.stdout, sys.stderr):
                if hasattr(stream, "reconfigure"):
                    stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        return explain_cmd(args)
    try:
        if args.cmd in {"baseline", "compare"}:
            return _baseline_cmd(args)
        if args.cmd == "replay":
            return _replay_pi(args.json)
        if args.cmd == "fixtures":
            _export(args.directory)
            return 0
        with _workspace() as tmp:
            cases = make_fixtures(tmp)
            if args.cmd == "demo":
                _demo(cases)
                return 0
            adapter = args.adapter
            env = None
            if args.case_ids:
                unknown = set(args.case_ids) - {c.id for c in cases}
                if unknown:
                    raise ValueError(f"Unknown fixture: {', '.join(sorted(unknown))}")
                cases = [c for c in cases if c.id in args.case_ids]
            if args.tool == "pi":
                adapter, env = _pi_adapter()
            elif adapter:
                first = Path(adapter[0])
                if first.suffix.lower() == ".py":
                    adapter = [sys.executable, str(first.absolute()), *adapter[1:]]
                elif first.parent != Path(".") or "/" in adapter[0] or "\\" in adapter[0]:
                    adapter = [str(first.absolute()), *adapter[1:]]
            timeout = args.timeout if args.timeout is not None else (60 if args.tool else 10)
            report = run_checks(cases, adapter=adapter, timeout=timeout, env=env)
        if args.json:
            print(json.dumps(report, indent=2, allow_nan=False))
        else:
            for case in report["cases"]:
                mark = "PASS" if case["passed"] else "ERROR" if case["actual"] is None else "FAIL"
                print(f"{mark:5}  {case['id']:15} expected {case['expected']}, got {case['actual'] or 'error'}")
                if case["actual"] is not None and case["skipped"] != case["expected_skipped"]:
                    print(f"       expected exclusions {case['expected_skipped']}, got {case['skipped']}")
                if not case["passed"]:
                    print(f"       {case['detail']}")
            print(f"{report['passed']}/{report['total']} passed | {report['errors']} adapter errors")
        return 2 if report["errors"] else int(report["passed"] != report["total"])
    except (OSError, ValueError, RecursionError, subprocess.SubprocessError) as exc:
        if args.cmd == "compare":
            from .summary import format_summary
            error = dict(version=1, exit_code=2, status="infrastructure_error", cases=[], error=str(exc))
            if args.json:
                print(json.dumps(error))
            elif args.markdown:
                print(format_summary(error, markdown=True))
        print(f"did-it-search: {exc}", file=sys.stderr)
        return 2
