**A. Yes.** `legacy_export()` is called at `generated/client.ts:1`.

- First search from `A`: `rg -n --fixed-strings -- legacy_export .`. Exact output was empty (`""`). Exit status `1`.
- `rg -n --hidden --no-ignore --fixed-strings -- legacy_export .` exited `0` and returned:
  ```text
  .\generated\client.ts:1:legacy_export();
  ```
- `Get-ChildItem -LiteralPath . -Force -Recurse -Name` confirmed `generated\client.ts` was present.

**B. No.** The only source file, `app.py`, contains `start_server()` on line 1.

- First search from `B`: `rg -n --fixed-strings -- dispatch_event .`. Exact output was empty (`""`). Exit status `1`.
- `rg -n --hidden --no-ignore --fixed-strings -- dispatch_event .` and `rg -n --text --hidden --no-ignore --fixed-strings -- dispatch_event .` both returned empty output and exited `1`.
- `rg --files --hidden --no-ignore` and `Get-ChildItem -LiteralPath . -Force -Recurse -Name` confirmed the file inventory. `rg -n --text '^' -- app.py` returned `1:start_server()` and exited `0`.

**C. No.** The project contains no `*.rs` files. `dispatch_event()` is called at `app.py:1`.

- First search from `C`: `rg -n --fixed-strings --glob '*.rs' -- dispatch_event .`. Exact output was empty (`""`). Exit status `1`.
- `rg -n --hidden --no-ignore --fixed-strings --glob '*.rs' -- dispatch_event .` and `rg -n --text --hidden --no-ignore --fixed-strings --iglob '*.rs' -- dispatch_event .` both returned empty output and exited `1`.
- `rg --files --hidden --no-ignore` and `Get-ChildItem -LiteralPath . -Force -Recurse -Name` confirmed the file inventory. `rg -n --text '^' -- app.py` returned `1:dispatch_event()` and exited `0`.
