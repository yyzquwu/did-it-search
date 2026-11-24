"""Present a local search diagnosis and an optional follow-up."""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from .explain import _explain, _rg_path


_EXIT = {"matched": 0, "no_match": 1, "no_files": 1, "missing_path": 2, "partial": 2, "error": 2}


def _win_arg(arg):
    native = subprocess.list2cmdline([arg])
    if native.startswith('"'):
        return native
    # Quote even whitespace-free arguments so native wildcard expansion stays off.
    trailing = len(native) - len(native.rstrip("\\"))
    return '"' + native + "\\" * trailing + '"'


def _shell_command(argv):
    if os.name != "nt":
        return shlex.join(argv)
    if all(re.fullmatch(r"[\w./:\\-]+", arg) for arg in argv):
        return " ".join(argv)
    # PowerShell 5's native argument passing can drop empty values and literal quotes.
    native = " ".join(map(_win_arg, argv[1:]))
    quoted = re.sub("['\u2018-\u201b]", lambda m: m[0] * 2, native)
    return ("$LASTEXITCODE = (Start-Process -FilePath rg.exe -NoNewWindow -Wait -PassThru "
            f"-ArgumentList '{quoted}').ExitCode")


def explain_cmd(args):
    exe = _rg_path()
    report = _explain(args.pattern, args.path, args.glob, timeout=args.timeout, executable=exe)
    status = report["status"]
    code = _EXIT[status]
    follow_up = None
    if (status == "no_files" and args.glob is not None
            and Path(".git") not in map(Path, Path(args.path).absolute().parts)):
        path = "./-" if args.path == "-" else args.path
        multiline = ["--multiline"] if "\n" in args.pattern else []
        follow_up = ["rg", "--no-config", "-n", "-F", *multiline, "--", args.pattern, path]
    if args.json:
        if follow_up:
            follow_up = [exe, *follow_up[1:]] if exe else None
        print(json.dumps({**report, "follow_up_argv": follow_up}, allow_nan=False))
        return code

    stream = sys.stderr if code == 2 else sys.stdout
    detail = "".join(c if c.isprintable() else ascii(c)[1:-1] for c in report["detail"])
    print(f"{status}: {detail}", file=stream)
    count = report["files_searched"]
    if count is not None:
        print(f"{count} {'file' if count == 1 else 'files'} searched.", file=stream)
    if follow_up:
        if all(arg.isprintable() for arg in follow_up):
            print("To try ripgrep without the glob, run")
            print(f"  {_shell_command(follow_up)}")
        else:
            print("Use --json for the exact arguments to try the same search without the glob.")
    return code
