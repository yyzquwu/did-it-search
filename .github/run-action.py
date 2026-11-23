"""Run the installed CLI against a consumer checkout and write its step summary."""

import os
import re
import subprocess
import sys
import venv
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory


def relative_path(value, name):
    path = Path(value)
    if path.anchor or ".." in path.parts:
        raise ValueError(f"{name} must be relative to the checkout: {value!r}")
    return path


def checkout_path(root, path):
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"Path points outside the checkout: {path}")
    return resolved


def baseline_path(root, path, tmp):
    if os.environ["GITHUB_EVENT_NAME"] != "pull_request":
        return checkout_path(root, path)
    sha = os.environ.get("DIDITSEARCH_PR_BASE_SHA", "")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise ValueError("The pull request base SHA must be a full Git commit ID.")

    git = ["git", "--no-replace-objects", "-C", str(root), "cat-file"]
    if subprocess.run([*git, "-e", f"{sha}^{{commit}}"], capture_output=True).returncode:
        raise ValueError(f"Pull request base commit {sha} is unavailable. Set checkout fetch-depth to 0.")
    result = subprocess.run([*git, "blob", f"{sha}:{path.as_posix()}"], capture_output=True)
    if result.returncode:
        raise ValueError(f"Could not read baseline {path} from pull request base commit {sha}.")
    baseline = tmp / "baseline.json"
    baseline.write_bytes(result.stdout)
    return baseline


def install_cli(action, env_dir):
    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    try:
        venv.create(env_dir, with_pip=True)
        subprocess.run([str(python), "-I", "-m", "pip", "--disable-pip-version-check", "install",
                        "--no-deps", str(action)], cwd=action, check=True)
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"Could not install did-it-search in the action's Python environment (exit {exc.returncode}).") from exc
    return python


def main():
    summary = Path(os.environ["GITHUB_STEP_SUMMARY"])
    try:
        root = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
        project = checkout_path(root, relative_path(os.environ["DIDITSEARCH_PROJECT_DIR"], "project-dir"))
        if not project.is_dir():
            raise ValueError(f"Project directory does not exist: {project.relative_to(root)}")
        path = relative_path(os.environ["DIDITSEARCH_BASELINE"], "baseline")
        with TemporaryDirectory(prefix="didit-action-", dir=os.environ.get("RUNNER_TEMP"),
                                ignore_cleanup_errors=True) as name:
            tmp = Path(name)
            baseline = baseline_path(root, path, tmp)
            python = install_cli(Path(os.environ["GITHUB_ACTION_PATH"]).resolve(), tmp / "venv")
            result = subprocess.run(
                [str(python), "-I", "-X", "utf8", "-m", "diditsearch", "compare", "--baseline",
                 str(baseline), "--tool", "pi", "--markdown"],
                cwd=project, env={**os.environ, "PI_OFFLINE": "1"}, stdout=subprocess.PIPE,
            )
        if result.returncode not in (0, 1, 2) or not result.stdout.strip():
            raise ValueError(f"Compare failed to produce a Markdown report (exit {result.returncode}).")
        with summary.open("ab") as out:
            out.write(result.stdout)
        return result.returncode
    except (OSError, ValueError) as exc:
        message = " ".join(str(exc).splitlines())
        print(f"did-it-search: {message}", file=sys.stderr)
        with summary.open("a", encoding="utf-8") as out:
            out.write(f"## Search regression check — ERROR\n\n<pre>{escape(message)}</pre>\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
