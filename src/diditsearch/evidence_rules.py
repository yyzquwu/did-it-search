"""Optional provenance for already-observed search exclusions."""

import os
import stat
import subprocess
from functools import cache
from itertools import islice
from pathlib import Path
from tempfile import TemporaryFile

_MAX_SAMPLES = 16
_MAX_DEPTH = 48
_MAX_PATH = 4096
_MAX_RULE_FILE = 64 * 1024
_MAX_OUTPUT = _MAX_SAMPLES * (_MAX_RULE_FILE + 2 * _MAX_PATH + 32)


def _git_path():
    name = "git.exe" if os.name == "nt" else "git"
    for directory in map(Path, os.get_exec_path()):
        exe = directory / name
        if directory.is_absolute() and exe.is_file() and os.access(exe, os.X_OK):
            return exe
    return None


def _stat(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _regular(info, kind):
    return (info is not None and kind(info.st_mode)
            and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _small(info):
    return info is None or _regular(info, stat.S_ISREG) and info.st_size <= _MAX_RULE_FILE


def _read_small(path, info):
    if not _small(info):
        return None
    if info is None:
        return b""
    with path.open("rb") as file:
        data = file.read(_MAX_RULE_FILE + 1)
    return data if len(data) <= _MAX_RULE_FILE else None


def _config_safe(path, keyword):
    data = _read_small(path, _stat(path))
    return data is not None and keyword not in data.lower()


def _ignore_safe(path, info):
    data = _read_small(path, info)
    if data is None or b"\0" in data:
        return False
    try:
        lines = data.decode("utf-8-sig").split("\n")
    except UnicodeError:
        return False
    for line in lines:
        if line.startswith("#"):
            continue
        # Git and ripgrep can interpret alternation, classes, escapes and
        # trailing whitespace differently. Leave those competing rules alone.
        line = line.removesuffix("\r").rstrip(" ").removeprefix(r"\!").removeprefix(r"\#")
        if line != line.rstrip() or any(char in line for char in "{}[]\\"):
            return False
    return True


def _global_rules_possible(root, remaining):
    # ripgrep reads these configs independently of Git. An ambient global rule
    # could prune a parent before Git would see a rule in a child directory.
    if os.environ.get("GIT_CONFIG_GLOBAL") or os.environ.get("GIT_CONFIG_SYSTEM"):
        return True
    home = Path(os.path.expanduser("~"))
    xdg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    if not home.is_absolute() or not xdg.is_absolute():
        return True
    for config in (home / ".gitconfig", xdg / "git/config", Path(root.anchor) / "etc/gitconfig"):
        remaining()
        if not _config_safe(config, b"excludesfile"):
            return True
    info = _stat(xdg / "git/ignore")
    return info is not None and (not _small(info) or info.st_size != 0)


def _probe(paths, root, remaining):
    if not root.is_absolute() or ".." in root.parts or len(root.parts) > _MAX_DEPTH:
        return {}
    for directory in reversed((root, *root.parents)):
        remaining()
        if not _regular(_stat(directory), stat.S_ISDIR):
            return {}
        markers = (".ignore", ".rgignore") if directory == root else (".ignore", ".rgignore", ".gitignore", ".git")
        if any(_stat(directory / marker) is not None for marker in markers):
            return {}
    git = root / ".git"
    if (not _regular(_stat(git), stat.S_ISDIR)
            or not _regular(_stat(git / "HEAD"), stat.S_ISREG)
            or any(_stat(git / name) is not None for name in ("commondir", "config.worktree"))
            or not _config_safe(git / "config", b"include")):
        return {}
    info = _stat(git / "info")
    exclude = git / "info/exclude"
    if info is not None and (not _regular(info, stat.S_ISDIR) or not _ignore_safe(exclude, _stat(exclude))):
        return {}

    @cache
    def inspect(path):
        remaining()
        info = _stat(path)
        if (path.name.startswith(".") or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_HIDDEN):
            return False, False
        if not _regular(info, stat.S_ISDIR):
            return _regular(info, stat.S_ISREG), False
        clean = all(_stat(path / name) is None for name in (".git", ".ignore", ".rgignore"))
        return clean, True

    @cache
    def rule(directory):
        remaining()
        file = directory / ".gitignore"
        info = _stat(file)
        if info is None:
            return True, None
        if not _ignore_safe(file, info) or (os.name == "nt" and file.resolve(strict=True).name != file.name):
            return False, None
        return True, os.fsencode(file.relative_to(root).as_posix())

    probes = {}
    for path in paths:
        remaining()
        if path == root or not path.is_relative_to(root):
            continue
        rel = path.relative_to(root)
        name = b"./" + os.fsencode(rel.as_posix())
        if ".." in rel.parts or len(rel.parts) > _MAX_DEPTH or len(name) > _MAX_PATH:
            continue
        dirs = tuple(reversed(path.parents[:len(rel.parts)]))
        if not all(inspect(directory) == (True, True) for directory in dirs[1:]) or not inspect(path)[0]:
            continue
        rules = [rule(directory) for directory in dirs]
        sources = {src for _, src in rules if src is not None}
        if sources and all(safe for safe, _ in rules):
            probes[name] = path, sources
    return probes


def _check_ignore(git, names, root, remaining):
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    cmd = [git, "--no-pager", "--git-dir=.git", "--work-tree=.", "-c", "core.ignoreCase=false",
           "-c", "core.excludesFile=", "check-ignore", "--stdin", "-z", "-v", "--no-index"]
    # File-backed streams keep Windows pipe I/O from blocking past the deadline.
    with TemporaryFile() as stdin, TemporaryFile() as stdout, TemporaryFile() as stderr:
        stdin.write(b"\0".join(names) + b"\0")
        stdin.seek(0)
        result = subprocess.run(cmd, cwd=root, env=env, stdin=stdin, stdout=stdout, stderr=stderr,
                                timeout=remaining())
        stderr.seek(0)
        if result.returncode != 0 or stderr.read(1):
            return b""
        stdout.seek(0)
        return stdout.read(_MAX_OUTPUT + 1)


def git_rules(paths, root, remaining):
    """Return verified rules for up to 16 observed absolute file or directory Paths.

    Root is the absolute Path to the worktree root. Each mapped Path gets a
    repository-relative POSIX source, a one-based line number and Git's pattern.
    Missing Git and ambiguous matches leave paths out of the mapping. The
    deadline callback supplies the probe timeout and its exception propagates.
    """
    paths = tuple(islice(paths, _MAX_SAMPLES))
    if not paths:
        return {}
    try:
        remaining()
        git = _git_path()
        if git is None:
            return {}
        probes = _probe(paths, root, remaining)
        if not probes or _global_rules_possible(root, remaining):
            return {}
        output = _check_ignore(git, probes, root, remaining)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if len(output) > _MAX_OUTPUT:
        return {}
    fields = output.split(b"\0")
    if fields[-1] or len(fields) % 4 != 1:
        return {}
    out = {}
    for start in range(0, len(fields) - 1, 4):
        source, line, pattern, name = fields[start:start + 4]
        if name not in probes or not pattern or pattern.startswith(b"!"):
            continue
        path, sources = probes[name]
        if source not in sources:
            continue
        try:
            number = int(line)
            if number > 0:
                out[path] = {"source": source.decode(), "line": number, "pattern": pattern.decode()}
        except (UnicodeError, ValueError):
            continue
    return out
