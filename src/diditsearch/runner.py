"""Run the reference search or an adapter against each case in order."""

import json
import math
import os
import signal
import subprocess
from pathlib import Path
from tempfile import TemporaryFile

_STATUSES = {"matched", "no_match", "missing_path", "no_files", "partial"}
_DIAGNOSTIC_LIMIT = 400


class _AdapterError(Exception):
    pass


def _command(argv):
    if not isinstance(argv, list) or not argv or any(
        not isinstance(arg, str) or "\0" in arg for arg in argv
    ) or not argv[0]:
        raise _AdapterError("Adapter configuration error: expected an executable argv list")
    exe = Path(argv[0])
    if not exe.is_absolute():
        if exe.name != argv[0]:
            raise _AdapterError("Adapter configuration error: use an absolute executable path or a name on PATH")
        names = [exe.name]
        if os.name == "nt":
            names += [exe.name + ext for ext in (".com", ".exe")]
        exe = next((p.absolute() for d in os.get_exec_path() for name in names
                    if (p := Path(d) / name).is_file() and os.access(p, os.X_OK)), None)
        if exe is None:
            raise _AdapterError(f"Adapter launch error: executable {argv[0]!r} was not found on PATH")
    if os.name == "nt" and exe.name.rstrip(" .").lower().endswith((".bat", ".cmd")):
        raise _AdapterError("Adapter configuration error: Windows batch files require a shell")
    return [str(exe), *argv[1:]]


def _diagnostic(stderr):
    stderr.seek(0)
    raw = stderr.read(_DIAGNOSTIC_LIMIT + 1)
    if not raw:
        return ""
    snippet = ascii(raw.decode("utf-8", errors="replace"))
    suffix = "..." if len(snippet) > _DIAGNOSTIC_LIMIT else ""
    return f". stderr: {snippet[:_DIAGNOSTIC_LIMIT]}{suffix}"


def _invalid_constant(value):
    raise ValueError(f"Invalid JSON constant {value}")


def _fields(result, error):
    if not isinstance(result, dict):
        raise error("expected a JSON object")
    status, detail = result.get("status"), result.get("detail")
    if not isinstance(status, str) or status not in _STATUSES:
        raise error("expected a recognized status string")
    if not isinstance(detail, str):
        raise error("expected a detail string")
    skipped = result.get("skipped", [])
    if not isinstance(skipped, list) or not all(isinstance(s, str) and s for s in skipped):
        raise error("expected skipped to be a list of nonempty reason strings")
    return status, detail, sorted(set(skipped))


def _adapter_result(case, argv, timeout):
    request = {key: case.request[key] for key in ("pattern", "path", "glob") if key in case.request}
    try:
        data = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _AdapterError(f"Adapter request error: {exc}") from exc
    # File-backed streams avoid buffering stderr and let timeout return even
    # when a descendant inherits the adapter's standard streams.
    try:
        with TemporaryFile() as stdin, TemporaryFile() as stdout, TemporaryFile() as stderr:
            stdin.write(data)
            stdin.seek(0)
            with subprocess.Popen(argv, cwd=case.root, stdin=stdin, stdout=stdout, stderr=stderr,
                                  start_new_session=os.name != "nt") as process:
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    # A Windows venv launcher starts a second Python process. Terminate
                    # its tree before removing the per-case working directory.
                    if os.name == "nt":
                        kill = Path(os.environ["SystemRoot"], "System32", "taskkill.exe")
                        subprocess.run([str(kill), "/PID", str(process.pid), "/T", "/F"],
                                       capture_output=True)
                    else:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    process.kill()
                    process.wait()
                    raise _AdapterError(f"Adapter timeout after {timeout:g}s{_diagnostic(stderr)}") from exc
            if process.returncode:
                raise _AdapterError(f"Adapter exited with code {process.returncode}{_diagnostic(stderr)}")
            stdout.seek(0)
            raw = stdout.read(65537)
            if len(raw) > 65536:
                raise _AdapterError("Adapter protocol error: stdout exceeds 64 KiB")
    except (OSError, OverflowError) as exc:
        raise _AdapterError(f"Adapter launch or I/O error: {exc}") from exc

    def protocol_error(message):
        return _AdapterError(f"Adapter protocol error: {message}")

    try:
        result = json.loads(raw.decode("utf-8"), parse_constant=_invalid_constant)
    except UnicodeDecodeError as exc:
        raise protocol_error("stdout is not valid UTF-8") from exc
    except (ValueError, RecursionError) as exc:
        raise protocol_error(f"stdout must contain exactly one JSON object ({exc})") from exc
    return _fields(result, protocol_error)


def run_checks(cases, adapter=None, timeout=10.0) -> dict:
    """Return case outcomes and separate execution errors from status mismatches.

    Set adapter to a list starting with an absolute executable path or a name
    on PATH. Arguments are passed verbatim. With an interpreter, supply an
    absolute script path because the adapter runs from each case directory.
    The finite, positive timeout applies separately to each external process.
    """
    config_error = None
    try:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except (ValueError, OverflowError):
        config_error = "Runner configuration error: timeout must be a finite positive number"

    argv = None
    if adapter is not None and config_error is None:
        try:
            argv = _command(adapter)
        except (_AdapterError, OSError) as exc:
            config_error = str(exc)

    results = []
    for case in cases:
        actual, detail, skipped = None, config_error, []
        if detail is None:
            try:
                if argv is None:
                    from .reference import inspect_search

                    actual, detail, skipped = _fields(inspect_search(case.root, case.request), ValueError)
                else:
                    actual, detail, skipped = _adapter_result(case, argv, timeout)
            except _AdapterError as exc:
                detail = str(exc)
            except Exception as exc:
                source = "Reference search" if argv is None else "Adapter"
                detail = f"{source} error: {type(exc).__name__}: {exc}"
        results.append(dict(id=case.id, expected=case.expected, actual=actual,
                            detail=detail[:_DIAGNOSTIC_LIMIT * 2] if actual is None else detail,
                            expected_skipped=list(case.skipped), skipped=skipped,
                            passed=actual == case.expected and skipped == sorted(case.skipped), reason=case.reason))
    return dict(version=1, passed=sum(result["passed"] for result in results), total=len(results),
                errors=sum(result["actual"] is None for result in results), cases=results)
