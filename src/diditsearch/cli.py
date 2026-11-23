"""Command-line entry points for the search conformance kit."""
import argparse
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

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


def main(argv=None) -> int:
    if argv is None:
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(prog="did-it-search", description="Check how a search adapter reports its scope.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('did-it-search')}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo", help="see a zero-match search with a real caller")
    fixtures = sub.add_parser("fixtures", help="write the six independent search repos and their answer key")
    fixtures.add_argument("directory", type=Path)
    check = sub.add_parser("check", help="grade the reference or your own search adapter")
    check.add_argument("--json", action="store_true", help="print a machine-readable report")
    check.add_argument("--timeout", type=float, default=10, metavar="SECONDS", help="per external adapter invocation")
    check.add_argument("--adapter", nargs=argparse.REMAINDER, metavar="ARGV",
                       help="external command and arguments, or a Python script; put this option last")
    args = parser.parse_args(argv)
    if args.cmd == "check" and args.adapter == []:
        parser.error("--adapter needs a command or Python script")
    try:
        if args.cmd == "fixtures":
            _export(args.directory)
            return 0
        with _workspace() as tmp:
            cases = make_fixtures(tmp)
            if args.cmd == "demo":
                _demo(cases)
                return 0
            adapter = args.adapter
            if adapter:
                first = Path(adapter[0])
                if first.suffix.lower() == ".py":
                    adapter = [sys.executable, str(first.absolute()), *adapter[1:]]
                elif first.parent != Path(".") or "/" in adapter[0] or "\\" in adapter[0]:
                    adapter = [str(first.absolute()), *adapter[1:]]
            report = run_checks(cases, adapter=adapter, timeout=args.timeout)
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
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"did-it-search: {exc}", file=sys.stderr)
        return 2
