The answers are A: yes, B: no, C: no. Each first search returned "No matches found", but for A that was misleading: the only match is in a folder ripgrep skips by default. The Grep tool doesn't report exit status. All paths below are relative to `<root>`.

## A: Is `legacy_export` called anywhere?

**Answer: yes, once.** It is called at `A/generated/client.ts:1`.

- **First search:** Grep, pattern `legacy_export`, path `<root>/A`, content mode.
  - Output: `No matches found`
  - Exit status: not exposed.
- **Follow-up:** I listed every file and found that `A/.gitignore` contains `generated/`. Ripgrep skips ignored folders by default, so I searched again with ignore rules off: `rg -n -uu --glob '!.git' legacy_export A`
  - Output: `A\generated\client.ts:1:legacy_export();`
  - Exit status: 0
- **Check:** I read both source files. `A/app.ts` contains only `startServer();`, and `A/generated/client.ts` contains `legacy_export();`.

## B: Is `dispatch_event` called anywhere?

**Answer: no.**

- **First search:** Grep, pattern `dispatch_event`, path `<root>/B`, content mode.
  - Output: `No matches found`
  - Exit status: not exposed.
- **Follow-up:** B has no ignore file and only one source file, `B/app.py`. I searched with ignore rules off: `rg -n -uu dispatch_event B --glob '!.git'`
  - Output: none
  - Exit status: 1
- **Check:** I read `B/app.py` directly. It is plain ASCII and contains only `start_server()`, so no encoding issue is hiding a match.
- **Positive control:** the same command run on C did find `dispatch_event`, so the search itself works.

## C: Is `dispatch_event` called in any `*.rs` files?

**Answer: no, because C has no `.rs` files at all.** The empty result says nothing about Rust call sites.

- **First search:** Grep, pattern `dispatch_event`, path `<root>/C`, glob `*.rs`, content mode.
  - Output: `No matches found`
  - Exit status: not exposed.
- **Follow-up:** I listed every file in C. Apart from `.git`, the only file is `C/app.py`. I then searched C without the glob: `rg -n -uu dispatch_event C --glob '!.git'`
  - Output: `C\app.py:1:dispatch_event()`
  - Exit status: 0
- **Note:** `dispatch_event` is called in C, but only in the Python file `C/app.py`.

All three were checked read-only: no files created, nothing posted, no subagents.
