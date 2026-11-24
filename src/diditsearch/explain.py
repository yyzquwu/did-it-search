"""Bounded, local ripgrep diagnostics for a case-sensitive literal search."""

import math
import os
import stat
import subprocess
import time
from pathlib import Path
from threading import Thread

_OUTPUT_LIMIT = 4 * 1024 * 1024
_PROCESS_LIMIT = 64
_ARG_LIMIT = 24_000
_METADATA = ("--iglob" if os.name == "nt" else "--glob", "!**/.git")
_IGNORE_MARKERS = (".rgignore", ".ignore", ".gitignore", ".git")
_IS_RESERVED = getattr(os.path, "isreserved", lambda name: Path(name).is_reserved())
_LIST = ("--files", "--null")
_SCAN = ("--no-mmap", "--null")


class _Incomplete(Exception):
    pass


def _rg_path():
    name = "rg.exe" if os.name == "nt" else "rg"
    for directory in map(Path, os.get_exec_path()):
        path = directory / name
        if directory.is_absolute() and path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


class _Ripgrep:
    def __init__(self, deadline, executable):
        self.deadline = deadline
        self.left = _OUTPUT_LIMIT
        self.runs = _PROCESS_LIMIT
        if not executable:
            raise ValueError("ripgrep (rg) must be available on PATH.")
        self.command = (os.path.abspath(executable), "--no-config", "--no-follow", "--threads=1")

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise _Incomplete("Search exceeded its time limit.")
        return remaining

    def run(self, cwd, *args):
        self.remaining()
        if not self.runs:
            raise _Incomplete("Search exceeded its ripgrep process limit.")
        if _arg_size((*self.command, *args)) > _ARG_LIMIT:
            raise _Incomplete("Search scope exceeded the command-line size limit.")
        self.runs -= 1
        out, err = bytearray(), bytearray()
        with subprocess.Popen((*self.command, *args), cwd=cwd, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as proc:
            def read(pipe, dst, limit):
                dst.extend(pipe.read(limit + 1))
                if len(dst) > limit:
                    proc.kill()

            readers = (Thread(target=read, args=(proc.stdout, out, self.left)),
                       Thread(target=read, args=(proc.stderr, err, min(8192, self.left))))
            try:
                for reader in readers:
                    reader.start()
                proc.wait(timeout=self.remaining())
            except subprocess.TimeoutExpired as exc:
                raise _Incomplete("Search exceeded its time limit.") from exc
            finally:
                if proc.poll() is None:
                    proc.kill()
                for reader in readers:
                    if reader.ident is not None:
                        reader.join()
            self.left -= len(out) + len(err)
            if self.left < 0 or len(err) > 8192:
                raise _Incomplete("Search exceeded its ripgrep output limit.")
            return proc.returncode, out, err


def _arg_size(args):
    return len(os.fsencode(subprocess.list2cmdline(args))) + 2 * len(args)


def _path(path):
    path = Path(path).absolute()
    current = Path(path.anchor)
    for part in path.parts[1:]:
        if os.name == "nt" and (":" in part or _IS_RESERVED(part)):
            raise ValueError("Search paths must refer to regular filesystem entries.")
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError("Search paths cannot traverse symbolic links or reparse points.")
    return path.resolve(strict=True)


def _walk_root(directory):
    # Start above applicable ignore rules so an explicit path cannot bypass an ignored parent.
    root = directory
    for parent in (directory, *directory.parents):
        if (parent / ".git").exists():
            return parent
        if any((parent / name).exists() for name in _IGNORE_MARKERS):
            root = parent
    return root


def _literal_glob(name):
    return "".join(f"[{char}]" if char in "[]{}*?," else r"\\" if char == "\\" else char
                   for char in name)


def _prune(parts):
    # Only exclusions here. A positive glob would override ripgrep's ignore rules.
    prefix = "/"
    for part in parts:
        alt = []
        for i, char in enumerate(part):
            head = _literal_glob(part[:i])
            alt.append(head + "[!" + (r"\\" if char == "\\" else char) + "]*")
            if head:
                alt.append(head)
        literal = _literal_glob(part)
        yield "--glob"
        yield "!" + prefix + "{" + ",".join((*alt, literal + "?*")) + "}"
        prefix += literal + "/"


def _names(output):
    return {Path(name) for name in os.fsdecode(bytes(output)).split("\0") if name}


def _files(rg, cwd, *args):
    code, out, err = rg.run(cwd, *_LIST, *args, *_METADATA, "--", ".")
    if err.startswith((b"error parsing glob", b"rg: error parsing glob")):
        raise ValueError("Invalid ripgrep glob.")
    if code not in (0, 1) or err:
        raise _Incomplete("Ripgrep could not enumerate the scope or read its ignore rules.")
    return {cwd / name for name in _names(out)}


def _selected(rg, target, directory, glob):
    glob_args = ("--glob", glob) if glob is not None else ()
    if directory:
        return _files(rg, target, "--no-ignore", *glob_args)
    if glob is None:
        return {target}
    cwd = Path.cwd().resolve()
    base = cwd if target.is_relative_to(cwd) else target.parent
    return _files(rg, base, "--no-ignore", "--hidden", *glob_args,
                  *_prune(target.relative_to(base).parts)) & {target}


def _outer_allowed(rg, outer, target, eligible):
    opts = list(_prune(target.relative_to(outer).parts))
    if target.is_dir():
        # Keep the paths to eligible files. Prune all sibling directories before
        # ripgrep loads their ignore files, even when VCS filtering is disabled.
        parents = set()
        for path in eligible:
            parent = path.parent
            while parent not in parents and parent.is_relative_to(target):
                parents.add(parent)
                parent = parent.parent
        size = _arg_size((*rg.command, *_LIST, "--hidden", "--no-ignore-vcs", *opts, *_METADATA, "--", "."))
        for parent in parents:
            with os.scandir(parent) as entries:
                for entry in entries:
                    rg.remaining()
                    path = parent / entry.name
                    if path in parents or entry.name == ".git" or not entry.is_dir(follow_symlinks=False):
                        continue
                    rule = "!/" + "/".join(map(_literal_glob, path.relative_to(outer).parts))
                    size += _arg_size(("--glob", rule)) + 2
                    if size > _ARG_LIMIT:
                        # One reachable file can have thousands of ignored siblings.
                        # Probe eligible paths directly using the same shared limits.
                        return {file for file in sorted(eligible)
                                if file in _files(rg, outer, "--hidden", "--no-ignore-vcs",
                                                  *_prune(file.relative_to(outer).parts))}
                    opts.extend(("--glob", rule))
    return eligible & _files(rg, outer, "--hidden", "--no-ignore-vcs", *opts)


def _batches(files, base, limit):
    batch, used = [], 0
    for path in sorted(files):
        # Prefix even a path named '-' so ripgrep always opens a file instead of stdin.
        name = "." + os.sep + os.fspath(path.relative_to(base))
        size = _arg_size((name,)) + 1
        if size > limit:
            raise _Incomplete("A search path exceeded the command-line size limit.")
        if used + size > limit:
            yield batch
            batch, used = [], 0
        batch.append(name)
        used += size
    if batch:
        yield batch


def _result(status, detail, files_searched=None):
    return {"status": status, "detail": detail, "files_searched": files_searched}


def explain(pattern, path=".", glob=None, *, timeout=5.0):
    """Explain a local search without reading the contents of ignored or linked files.

    Directories use directory-relative globs and ripgrep's default hidden-file rules
    unless a positive glob selects hidden entries.
    A file glob is relative to the current directory if it contains the file, otherwise
    to the file's parent. Ignore rules also apply to explicitly named files. Git
    metadata is excluded, and .gitignore applies even without a Git repository.

    files_searched counts eligible text files searched, including empty files. NUL
    bytes after ripgrep's default decoding indicate binary data. An excluded input
    makes a miss partial. A confirmed text match takes precedence over exclusions
    and incomplete coverage. The count is unknown if the eligible search is interrupted.

    One deadline covers all ripgrep phases, with a shared 4 MiB output budget and
    at most 64 serial invocations. Coverage describes the files observed during
    the search, which can change while the search is running.
    """
    return _explain(pattern, path, glob, timeout=timeout, executable=_rg_path())


def _explain(pattern, path, glob, *, timeout, executable):
    started = time.monotonic()
    matched, searched, excluded, binary = False, 0, 0, 0
    target = None
    count = None
    try:
        path = os.fspath(path)
        if (not isinstance(pattern, str) or not isinstance(path, str) or not path
                or glob is not None and not isinstance(glob, str)
                or any("\0" in value for value in (pattern, path, glob or ""))):
            raise ValueError("Expected a literal pattern, nonempty path and optional glob without NUL bytes.")
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("Timeout must be a finite nonnegative number of seconds.")
        target = _path(path)
        directory = target.is_dir()
        if not directory and not target.is_file():
            raise ValueError("Search path must be a regular file or directory.")
        if Path(".git") in map(Path, target.parts):
            return _result("no_files", "Git metadata is outside the search scope.", 0)
        rg = _Ripgrep(started + timeout, executable)
        base = target if directory else target.parent
        root = _walk_root(base)
        # Let ripgrep resolve hidden entries and explicit ignore negations together.
        # Running from the requested directory also honors an explicitly named dot-directory.
        glob_args = ("--glob", glob) if glob is not None else ()
        scope = (_files(rg, target, "--no-require-git", *glob_args) if directory
                 else _selected(rg, target, False, glob))
        eligible = scope
        if scope and (not directory or root != target or (glob and not glob.startswith("!"))):
            # A positive glob or an explicit target can override an ignore. Require
            # membership in an independent ignore-aware walk before reading file contents.
            allowed = _files(rg, root, "--no-require-git", "--hidden",
                             *_prune(target.relative_to(root).parts))
            eligible = scope & allowed
        # Repository boundaries reset Git rules. Ripgrep's .ignore and .rgignore
        # can still exclude the entire repository from a parent directory.
        if eligible and (root / ".git").exists():
            outer = next((p for p in reversed(root.parents)
                          if (p / ".ignore").is_file() or (p / ".rgignore").is_file()), None)
            if outer is not None:
                eligible = _outer_allowed(rg, outer, target, eligible)
        multiline = ("--multiline",) if "\n" in pattern else ()
        literal = ("--fixed-strings", "--case-sensitive", *multiline, "-e", pattern)
        limit = _ARG_LIMIT - _arg_size((*rg.command, *_SCAN, "--files-with-matches", *literal, "--")) - 128
        incomplete = False
        for batch in _batches(eligible, base, limit):
            code, out, err = rg.run(base, *_SCAN, "--text", "--files-without-match", "-e", r"\x00", "--", *batch)
            text = _names(out)
            if code in (0, 1) and not err:
                binary += len(batch) - len(text)
            else:
                incomplete = True
            paths = [name for name in batch if Path(name) in text]
            if paths:
                code, out, err = rg.run(base, *_SCAN, "--files-with-matches", *literal, "--", *paths)
                matched |= bool(_names(out))
                searched += len(paths)
                incomplete |= code not in (0, 1) or bool(err)
        if incomplete:
            raise _Incomplete("Ripgrep could not read or search all eligible files.")
        count = searched
        # Count excluded names after searching so a large ignored tree cannot hide a verified hit.
        selected = scope | _selected(rg, target, directory, glob) if directory else scope
        if not selected:
            return _result("no_files", "No files were selected by the requested scope.", 0)
        excluded = len(selected - eligible)
    except (FileNotFoundError, NotADirectoryError):
        if target is None:
            return _result("missing_path", "The requested path does not exist.", 0)
        return _result("matched" if matched else "partial", "The search scope changed during the search.", count)
    except _Incomplete as exc:
        detail = f"Found a literal match. {exc}" if matched else str(exc)
        return _result("matched" if matched else "partial", detail, count)
    except (OSError, ValueError, TypeError) as exc:
        return _result("error", str(exc))

    notes = []
    if excluded:
        notes.append(f"{excluded} ignored or hidden")
    if binary:
        notes.append(f"{binary} binary")
    detail = "Found no literal match in the searched text files." if searched else "No eligible text files were searched."
    if matched:
        detail = "Found a literal match."
    if notes:
        detail += " Files skipped (" + ", ".join(notes) + ")."
    return _result("matched" if matched else "partial" if notes else "no_match", detail, searched)
