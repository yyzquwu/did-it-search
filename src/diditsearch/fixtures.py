"""Small repositories with independently specified search outcomes."""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Case:
    id: str
    root: Path
    request: dict
    expected: str
    reason: str
    skipped: tuple[str, ...] = ()


def make_fixtures(root: Path) -> list[Case]:
    """Populate a new or empty directory with eight independent Git repositories."""
    root.mkdir(exist_ok=True)
    if any(root.iterdir()):
        raise ValueError(f"Fixture root must be empty: {root}")
    git = shutil.which("git")
    if not git:
        raise OSError("Git (git) must be available on PATH to create the fixtures")

    specs = [
        ("hit", "dispatch_event", ".", None, "matched",
         "The literal occurs in the eligible text file app.py.",
         {"app.py": b"dispatch_event()\n"}),
        ("miss", "dispatch_event", ".", None, "no_match",
         "The eligible text file app.py has no occurrence of the literal.",
         {"app.py": b"start_server()\n"}),
        ("missing-path", "dispatch_event", "absent", None, "missing_path",
         "The requested path absent does not exist, and app.py is outside that path.",
         {"app.py": b"dispatch_event()\n"}),
        ("empty-glob", "dispatch_event", ".", "*.rs", "no_files",
         "The only source file is app.py, so the Rust glob selects zero files.",
         {"app.py": b"dispatch_event()\n"}),
        ("ignored-caller", "legacy_export", ".", None, "partial",
         "The only occurrence is in generated/client.ts, excluded by .gitignore.",
         {".gitignore": b"generated/\n", "app.ts": b"startServer();\n",
          "generated/client.ts": b"legacy_export();\n"}),
        ("binary", "binary_needle", ".", None, "partial",
         "The literal occurs only in a NUL-marked file that the text search skips.",
         {"record.bin": b"\x00binary_needle\x00", "app.py": b"start_server()\n"}),
        ("glob-root-hit", "hello", "repo", "migrations/main/*.js", "matched",
         "The glob relative to repo selects migrations/main/a.js, which contains hello.",
         {"repo/migrations/main/a.js": b'console.log("hello");\n'}),
        ("glob-root-decoy", "hello", "repo", "migrations/main/*.js", "no_match",
         "The selected migrations/main/a.js has no hello. The glob leaves out nested/migrations/main/b.js.",
         {"repo/migrations/main/a.js": b'console.log("goodbye");\n',
          "repo/nested/migrations/main/b.js": b'console.log("hello");\n'}),
    ]
    cases = []
    for id_, pattern, path, glob, expected, reason, files in specs:
        case_root = root / id_
        case_root.mkdir()
        subprocess.run(
            [git, "-c", "init.defaultBranch=main", "init", "-q", "--template="],
            cwd=case_root, check=True,
            env={k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")},
        )
        for name, data in files.items():
            file = case_root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            with file.open("xb") as stream:
                stream.write(data)
        skipped = {"ignored-caller": ("ignored",), "binary": ("binary",)}.get(id_, ())
        cases.append(Case(id_, case_root, {"pattern": pattern, "path": path, "glob": glob},
                          expected, reason, skipped))
    return cases
