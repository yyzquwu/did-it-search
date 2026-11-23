"""Example adapter that exposes the limitations of a conventional empty result."""
import json
import subprocess
import sys
from pathlib import Path

req = json.loads(sys.stdin.buffer.read())
args = ["rg", "--no-config", "--fixed-strings", "--color=never"]
if req["glob"] is not None:
    args += ["-g", req["glob"]]
run = subprocess.run([*args, "--", req["pattern"], req["path"]], capture_output=True)
if run.returncode == 2 and not Path(req["path"]).exists():
    status, detail = "missing_path", "The requested path does not exist."
elif run.returncode == 0:
    status, detail = "matched", "ripgrep reported a hit."
elif run.returncode == 1:
    status, detail = "no_match", "ripgrep returned exit 1."
else:
    print(run.stderr.decode("utf-8", "replace").strip(), file=sys.stderr)
    sys.exit(2)
print(json.dumps({"status": status, "detail": detail}))
