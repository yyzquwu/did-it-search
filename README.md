# Did It Search?

A conformance kit for the answers repository search tools give coding agents. When ripgrep is given an explicit search root such as `.`, a normal miss, a glob selecting zero files, and an ignored caller can all produce empty stdout. Give your search adapter six tiny Git repositories and check whether it tells these situations apart.

## See the ignored caller

From this checkout, with Python 3.11+, Git and ripgrep (`rg`) on your PATH:

```sh
python -m pip install .
did-it-search demo
```

The demo runs a real search for `legacy_export`. Ripgrep exits 1 with empty stdout. The reference adapter reports incomplete coverage and one ignored file. The fixture's answer key locates the caller at `generated/client.ts`. The adapter learns the exclusion by comparing file lists and leaves the ignored file's contents unread.

```sh
did-it-search check
did-it-search check --json --adapter examples/plain_rg.py
did-it-search fixtures search-fixtures
```

The reference passes all six cases. The example wraps ordinary ripgrep output and handles a missing path. It passes three cases and exits 1. Try `did-it-search check --adapter examples/reference_adapter.py` for a passing external adapter. The last command above writes six repositories and `cases.json` to a new directory so you can open a case in your own agent. Run the installed CLI as `python -m diditsearch` when preferred.

| Fixture | Search truth | Expected status | Exclusions |
| --- | --- | --- | --- |
| hit | A text file contains the literal | `matched` | none |
| miss | A text file was searched and the literal is absent | `no_match` | none |
| missing-path | The requested path does not exist | `missing_path` | none |
| empty-glob | The project contains Python and the request selects Rust files | `no_files` | none |
| ignored-caller | The only call is in a generated file excluded by Git ignore rules | `partial` | `ignored` |
| binary | The only occurrence is in a file containing NUL bytes | `partial` | `binary` |

## Check your search adapter in CI

Implement the [adapter protocol](#adapter-protocol) around your tool's actual search output. Run this step after installing the kit in your CI environment:

```yaml
- name: Check search diagnostics
  run: did-it-search check --json --adapter ci/search_adapter.py
```

Exit 0 means every case agreed with its answer key. Exit 1 means at least one answer disagreed. Exit 2 means the kit or adapter could not run a check, including malformed responses and timeouts. The runner continues after an adapter error and includes the other results. The fixture names and answer key are public, so this is a regression suite for maintainers.

### Adapter protocol

The runner starts a new process in each fixture's root and sends one UTF-8 JSON object followed by a newline on stdin.

```json
{"pattern":"legacy_export","path":".","glob":null}
```

Return one UTF-8 JSON object without a byte-order mark on stdout (up to 64 KiB) and exit 0 when your tool can describe the outcome. Diagnostic logs may go to stderr. If your adapter keeps a fixture open and cleanup fails, the runner preserves the report and its exit status. Stderr identifies the temporary directory for later removal.

```json
{"status":"partial","detail":"An ignored file was outside the text search.","skipped":["ignored"]}
```

`pattern` is a case-sensitive literal. `path` is relative to the fixture root. `glob` is an optional ripgrep-style file glob. The kit grades the status and the set of exclusion reasons. Omit `skipped` for an empty set. The human-readable `detail` string is included in the report. An adapter that cannot determine a cause should report its own result honestly so a mismatch remains visible.

Put `--adapter` last: everything after it is the adapter command. A first argument ending in `.py` is resolved relative to the calling directory and run with the kit's Python. Other executables may be given by path or name on PATH. Additional arguments are passed unchanged, so use absolute paths for any other files your command needs. Use `--timeout 5` before `--adapter` to set a per-process timeout in seconds.

## What happened with real agents?

In a three-project pilot, each of three agents got the same initial result for three different situations. All three investigated and answered correctly. The [pilot](study/README.md) includes the prompts, first search outputs, follow-ups and final answers. You can recreate its project directories locally.

## Reference scope

The reference searches small, stable fixtures. It includes dotfiles and skips Git metadata and links. It applies the glob and in-tree ignore rules even to explicitly named files. Native ripgrep can search a named file that these rules exclude. The reference enumerates filenames under ignored directories to account for exclusions. It reads up to 1 MiB plus one byte per eligible file to identify binary or oversized inputs. A UTF-16 file containing NUL bytes is classified as binary, even though native ripgrep can decode a UTF-16 BOM. A confirmed hit takes precedence and still reports exclusions.

Run the tests from an installed checkout with `python -m unittest discover -s tests`.
