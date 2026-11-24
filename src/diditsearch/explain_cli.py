"""Present a local search diagnosis and an optional follow-up."""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from .explain import _explain, _path, _rg_path


_EXIT = {"matched": 0, "no_match": 1, "no_files": 1, "missing_path": 2, "partial": 2, "error": 2}


def _win_arg(arg):
    native = subprocess.list2cmdline([arg])
    if native.startswith('"'):
        return native
    # Quote even whitespace-free arguments so native wildcard expansion stays off.
    trailing = len(native) - len(native.rstrip("\\"))
    return '"' + native + "\\" * trailing + '"'


def _ps_quote(arg):
    return "'" + re.sub("['\u2018-\u201b]", lambda m: m[0] * 2, arg) + "'"


def _shell_command(argv):
    if os.name != "nt":
        return shlex.join(argv)
    if all(re.fullmatch(r"[\w./:\\-]+", arg) for arg in argv):
        return " ".join(argv)
    # PowerShell 5's native argument passing can drop empty values and literal quotes.
    native = " ".join(map(_win_arg, argv[1:]))
    return (f"$LASTEXITCODE = (Start-Process -FilePath {_ps_quote(argv[0])} -NoNewWindow -Wait -PassThru "
            f"-ArgumentList {_ps_quote(native)}).ExitCode")


def _in_git(path):
    return any((part.rstrip(" .").casefold() if os.name == "nt" else part) == ".git"
               for part in path.parts)


def _skipped_file(path, entries):
    if not entries:
        return None
    req = Path(path)
    try:
        target = _path(req)
    except (OSError, ValueError):
        return None
    if _in_git(target):
        return None
    directory = target.is_dir()
    for entry in entries:
        rel = Path(entry["path"])
        if (entry["kind"] not in {"ignored", "hidden"} or entry["is_dir"] or rel.anchor
                or not rel.parts or ".." in rel.parts or rel.name in {".gitignore", ".ignore", ".rgignore"}
                or (not directory and rel != Path(target.name))):
            continue
        candidate = (req if directory else req.parent) / rel
        if _in_git(candidate.absolute()):
            continue
        try:
            actual = _path(candidate)
            if not _in_git(actual) and actual.is_file():
                return candidate.as_posix()
        except (OSError, ValueError):
            continue
    return None


def _diagnose(pattern, path, glob=None, *, timeout=5.0, executable):
    report = _explain(pattern, path, glob, timeout=timeout, executable=executable)
    status = report["status"]
    follow_up = None
    flags = ["-n", "-F", *(["--multiline"] if "\n" in pattern else [])]
    if executable and status in {"partial", "no_match", "no_files"}:
        if status == "no_files" and glob is not None and not _in_git(Path(path).absolute()):
            follow_up = [executable, "--no-config", *flags, "--", pattern, "./-" if path == "-" else path]
        elif target := _skipped_file(path, report.get("skipped_paths", ())):
            follow_up = [executable, "--no-config", "--no-follow", "--max-depth", "0",
                         "-H", *flags, "--", pattern, "./-" if target == "-" else target]
    return {**report, "follow_up_argv": follow_up}


def _safe(value):
    return "".join(c if c.isprintable() else ascii(c)[1:-1] for c in value)


def _render(report, stream, *, without_glob=False, display_exe=None):
    print(f"{report['status']}: {_safe(report['detail'])}", file=stream)
    count = report["files_searched"]
    if count is not None:
        print(f"{count} {'file' if count == 1 else 'files'} searched.", file=stream)
    for entry in report.get("skipped_paths", ()):
        suffix = "/" if entry["is_dir"] else ""
        kind = "excluded by the glob" if entry["kind"] == "excluded" else entry["kind"]
        print(f"Skipped: {_safe(entry['path'])}{suffix} ({_safe(kind)})", file=stream)
        if rule := entry["rule"]:
            print(f"  Rule: {_safe(rule['source'])}:{rule['line']}  {_safe(rule['pattern'])}", file=stream)
    if argv := report["follow_up_argv"]:
        if all(arg.isprintable() for arg in argv):
            print("To try ripgrep without the glob, run" if without_glob
                  else "To check only that file, run", file=stream)
            print(f"  {_shell_command([display_exe or argv[0], *argv[1:]])}", file=stream)
        else:
            print("Use --json for the exact arguments to try the same search without the glob." if without_glob
                  else "Use --json for the exact arguments to check only that file.", file=stream)


def explain_cmd(args):
    report = _diagnose(args.pattern, args.path, args.glob, timeout=args.timeout, executable=_rg_path())
    code = _EXIT[report["status"]]
    if args.json:
        print(json.dumps(report, allow_nan=False))
    else:
        _render(report, sys.stderr if code == 2 else sys.stdout,
                without_glob=args.glob is not None and report["status"] == "no_files")
    return code
