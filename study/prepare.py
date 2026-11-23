"""Create the three neutral project directories used by the search pilot."""
import argparse
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from diditsearch.fixtures import make_fixtures


def prepare(dest: Path) -> None:
    dest.mkdir(exist_ok=False)
    with TemporaryDirectory() as tmp:
        cases = {c.id: c.root for c in make_fixtures(Path(tmp))}
        for label, case in [("A", "ignored-caller"), ("B", "miss"), ("C", "empty-glob")]:
            shutil.copytree(cases[case], dest / label)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    dest = parser.parse_args().destination
    try:
        prepare(dest)
    except FileExistsError:
        parser.error(f"Choose a new directory: {dest}")
    except OSError as exc:
        parser.error(str(exc))
