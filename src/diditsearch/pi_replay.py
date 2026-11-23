"""Replay Pi's grep-glob regression using the pinned upstream source trees."""

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from urllib.request import urlopen

from .fixtures import make_fixtures
from .runner import run_checks

UPSTREAM = "https://github.com/earendil-works/pi/pull/11895"
REPO = "https://github.com/earendil-works/pi.git"
BEFORE = "1f7c685b851441745d1b1edb99cafeecb2ed3ff5"
AFTER = "6bf2ab9bbb64fd14793e506a5404078441219b95"
SRC = "packages/coding-agent/src"
GREP = f"{SRC}/core/tools/grep.ts"
RESOLVER = f"{SRC}/experimental/source-resolver.ts"
CASES = ("glob-root-hit", "glob-root-decoy")

# Registry packages resolved by the two revisions' grep module graph.
# Pi's own source resolver loads all Pi workspace packages from the checkout.
PKGS = (
    "chalk", "cross-spawn", "get-east-asian-width", "highlight.js", "isexe", "marked",
    "marked-cjk-friendly", "partial-json", "path-key", "shebang-command", "shebang-regex",
    "typebox", "which",
)


def _run(cmd, cwd, *, env=None, timeout=90):
    proc = subprocess.run(cmd, cwd=cwd, env=env, timeout=timeout, capture_output=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode:
        raise OSError(f"{Path(cmd[0]).name} {cmd[1]} exited {proc.returncode}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _sources(git, src):
    src.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")

    def cmd(*args):
        return _run([git, "-c", "core.autocrlf=false", "-c", "core.longpaths=true", *args], src, env=env)

    cmd("init", "-q", "--template=")
    cmd("fetch", "-q", "--depth=2", "--no-tags", REPO, AFTER)
    if cmd("rev-parse", "FETCH_HEAD", "FETCH_HEAD^").splitlines() != [AFTER, BEFORE]:
        raise ValueError("Fetched Pi commit or direct parent does not match the replay pins")
    changes = cmd("diff", "--name-only", BEFORE, AFTER).splitlines()
    if GREP not in changes or "package-lock.json" in changes:
        raise ValueError("Pi grep and lockfile do not match the expected fix")

    def checkout(commit):
        cmd("checkout", "-q", "--detach", commit)
        if cmd("rev-parse", "HEAD") != commit:
            raise ValueError("Checked-out Pi commit does not match the replay pin")

    checkout(BEFORE)
    return checkout


def _install(dst, name, entry):
    url = entry["resolved"]
    if not url.startswith(f"https://registry.npmjs.org/{name}/-/"):
        raise ValueError(f"Unexpected registry URL for {name}")
    with urlopen(url, timeout=30) as response:
        data = response.read()
    digest = base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    if entry["integrity"] != f"sha512-{digest}":
        raise ValueError(f"Registry integrity check failed for {name}")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            if (path.parts[:1] != ("package",) or ".." in path.parts or
                    any("\\" in p or ":" in p for p in path.parts)):
                raise ValueError(f"Invalid registry archive path for {name}")
            out = dst.joinpath(*path.parts[1:])
            if member.isdir():
                out.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                out.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as reader, out.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
            else:
                raise ValueError(f"Unsupported registry archive entry for {name}")


def _deps(src):
    packages = json.loads((src / "package-lock.json").read_bytes())["packages"]

    def install(name):
        entry = packages[f"node_modules/{name}"]
        _install(src / "node_modules" / name, name, entry)

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(install, PKGS))


def _verdict(before, after):
    if any(report["errors"] or any("raw" not in case for case in report["cases"])
           for report in (before, after)):
        return "infrastructure_error"
    old = {case["id"]: case["actual"] for case in before["cases"]}
    new = {case["id"]: case["actual"] for case in after["cases"]}
    if old == new:
        return "unexpected_parity"
    if (old == dict.fromkeys(CASES, "no_match") and new == dict(zip(CASES, ("matched", "no_match")))
            and (before["passed"], after["passed"]) == (1, 2)):
        return "reproduced"
    return "unexpected_result"


def replay() -> dict:
    """Run both exact revisions and report the observed regression or failure."""
    before, after = {"commit": BEFORE}, {"commit": AFTER}
    result = dict(version=1, upstream=UPSTREAM, before=before, after=after)
    try:
        git, node, rg = (shutil.which(name) for name in ("git", "node", "rg"))
        if not all((git, node, rg)):
            raise ValueError("Pi replay requires Git, Node.js 22.19+ and ripgrep (rg) on PATH")
        with TemporaryDirectory(prefix="didit-pi-") as tmp:
            root = Path(tmp).resolve()
            node_ver = _run([node, "--version"], root)
            if tuple(map(int, node_ver.lstrip("v").split(".")[:2])) < (22, 19):
                raise ValueError(f"Pi replay requires Node.js 22.19+ (found {node_ver})")
            rg_ver = _run([rg, "--version"], root).splitlines()[0]
            result["runtime"] = {"node": node_ver, "ripgrep": rg_ver}
            src = root / "pi"
            checkout = _sources(git, src)
            _deps(src)
            cases = {c.id: c for c in make_fixtures(root / "fixtures")}
            selected = [cases[id_] for id_ in CASES]
            env = {k: v for k, v in os.environ.items()
                   if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "TEMP", "TMP", "TMPDIR"}}
            env.update(DIDITSEARCH_PI_MODULE=str(src / GREP), PI_OFFLINE="1",
                       PI_CODING_AGENT_DIR=str(root / "agent"), GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_CONFIG_NOSYSTEM="1", XDG_CONFIG_HOME=str(root / "config"))
            adapter = [node, "--import", (src / RESOLVER).as_uri(),
                       str(Path(__file__).resolve().with_name("pi_adapter.mjs"))]
            before.update(run_checks(selected, adapter, timeout=60, env=env))
            checkout(AFTER)
            after.update(run_checks(selected, adapter, timeout=60, env=env))
        status = _verdict(before, after)
    except (OSError, HTTPException, ValueError, KeyError, tarfile.TarError, subprocess.SubprocessError) as exc:
        status = "infrastructure_error"
        result["error"] = str(exc)
    return dict(result, status=status, passed=status == "reproduced")
