"""Expose the reference implementation through the same protocol as a user adapter."""
import json
import sys
from pathlib import Path

from diditsearch.reference import inspect_search

request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
print(json.dumps(inspect_search(Path.cwd(), request)))
