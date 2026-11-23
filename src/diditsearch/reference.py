"""Ripgrep-backed literal search for small, stable conformance fixtures."""

import os
import stat
import subprocess
from pathlib import Path

_MAX_FILE_BYTES = 1024 * 1024
_is_reserved = getattr(os.path, "isreserved", lambda name: Path(name).is_reserved())


def _rg(root: Path, *args: str, data: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        run = subprocess.run(["rg", "--no-config", *args], cwd=root, input=data,
                             capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise OSError("ripgrep (rg) must be available on PATH") from exc
    if run.returncode not in (0, 1) or run.stderr:
        detail = run.stderr.decode("utf-8", errors="replace").strip()
        raise OSError(f"ripgrep failed: {detail or f'exit code {run.returncode}'}")
    return run


def _files(root: Path, target: Path, *opts: str, base: Path = Path()) -> set[Path]:
    metadata_glob = "--iglob" if os.name == "nt" else "--glob"
    run = _rg(root / base, "--files", "--null", "--hidden", *opts, metadata_glob, "!.git", "--", ".")
    paths = (base / os.fsdecode(name) for name in run.stdout.split(b"\0") if name)
    return {path for path in paths if path.is_relative_to(target)}


def _mode(path: Path) -> int:
    info = path.lstat()
    attrs = getattr(info, "st_file_attributes", 0)
    if stat.S_ISLNK(info.st_mode) or attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ValueError(f"Search paths cannot traverse links: {path}")
    return info.st_mode


def _target_mode(root: Path, target: Path) -> int:
    mode = stat.S_IFDIR
    for part in target.parts:
        if not stat.S_ISDIR(mode):
            raise NotADirectoryError(root)
        root /= part
        mode = _mode(root)
    return mode


def inspect_search(root: Path, request: dict) -> dict:
    """Search regular files under a root-relative path with one optional rg glob.

    Globs select scope independently of repository ignore rules. A directory
    anchors its glob at that directory. For a named file, the glob is applied to
    the file's fixture-root-relative path. Dotfiles are eligible, `.git` entries
    and linked entries are outside scope, and explicit linked paths are rejected.
    Ignore rules above the fixture root, global Git rules, and .git/info/exclude
    are disabled. rg must be on PATH.

    A file containing NUL or exceeding 1 MiB is excluded. The literal is
    case-sensitive and searched as UTF-8 bytes, with multiline support. A
    confirmed hit takes precedence over exclusions, which are still reported.
    """
    if not isinstance(request, dict):
        raise ValueError("Search request must be an object")
    path, pattern, glob = (request.get(key) for key in ("path", "pattern", "glob"))
    if not isinstance(path, str) or not path or not isinstance(pattern, str):
        raise ValueError("Search requires a nonempty path and a literal string pattern")
    if glob is not None and not isinstance(glob, str):
        raise ValueError("Search glob must be a string or null")
    if any("\0" in value for value in (path, pattern, glob or "")):
        raise ValueError("Search inputs cannot contain NUL bytes")
    target = Path(path)
    if target.anchor or ".." in target.parts or (os.name == "nt" and any(
        _is_reserved(part) or ":" in part or part.endswith((".", " "))
        for part in target.parts
    )):
        raise ValueError("Search path must stay relative to the fixture root")

    root = root.absolute()
    if not stat.S_ISDIR(_mode(root)):
        raise NotADirectoryError(root)
    try:
        mode = _target_mode(root, target)
    except (FileNotFoundError, NotADirectoryError):
        return {"status": "missing_path", "detail": "The requested path does not exist."}
    if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
        raise ValueError("Search path must be a regular file or directory")

    glob_opts = ("--glob", glob) if glob is not None else ()
    base = Path()
    # A walk starting inside .git would bypass the metadata exclusion.
    if glob is not None and stat.S_ISDIR(mode) and Path(".git") not in map(Path, target.parts):
        base = target
    candidates = _files(root, target, "--no-ignore", *glob_opts, base=base)
    if not candidates:
        return {"status": "no_files", "detail": "The requested scope selects no regular files."}
    # Positive rg globs can override ignores, so intersect two independent walks.
    eligible = candidates & _files(root, target, "--no-require-git", "--no-ignore-parent",
                                   "--no-ignore-global", "--no-ignore-exclude")

    hit, binary, oversized = False, 0, 0
    for path in eligible:
        if not stat.S_ISREG(_target_mode(root, path)):
            raise OSError(f"Search input is no longer a regular file: {path}")
        with (root / path).open("rb") as stream:
            data = stream.read(_MAX_FILE_BYTES + 1)
        if b"\0" in data:
            binary += 1
        elif len(data) > _MAX_FILE_BYTES:
            oversized += 1
        elif not hit:
            hit = _rg(root, "--quiet", "--fixed-strings", "--multiline", "--encoding", "none",
                      "-e", pattern, "--", "-", data=data).returncode == 0

    exclusions = [f"{count} {reason}" for count, reason in (
        (len(candidates - eligible), "ignored"), (binary, "with NUL bytes"),
        (oversized, "over 1 MiB"),
    ) if count]
    if hit:
        status, detail = "matched", "Found a literal match."
    elif exclusions:
        status, detail = "partial", "Found no literal match in the searched files."
    else:
        status, detail = "no_match", "Found no literal match."
    if exclusions:
        detail += " Files skipped (" + ", ".join(exclusions) + ")."
    skipped = [tag for tag, count in (("ignored", len(candidates - eligible)),
                                      ("binary", binary), ("oversized", oversized)) if count]
    return {"status": status, "detail": detail, "skipped": skipped}
