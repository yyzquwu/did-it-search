| Project | Answer | First text-search invocation, run from the project root | Exact output | Exit status |
|---|---|---|---|---|
| A | **Yes.** `generated/client.ts` calls `legacy_export()` on line 1. | `rg -n --no-heading --color never -F -- legacy_export .` | Empty (0 bytes) | `1` |
| B | **No.** The only source file, `app.py`, contains `start_server()`. | `rg -n --no-heading --color never -F -- dispatch_event .` | Empty (0 bytes) | `1` |
| C | **No.** The project contains no `*.rs` files. | `rg -n --no-heading --color never -g '*.rs' -F -- dispatch_event .` | Empty (0 bytes) | `1` |

**Follow-up checks**

- **A.** `rg --files --hidden --no-ignore .` listed `.\generated\client.ts` (exit `0`). `rg -n --hidden --no-ignore --no-heading --color never -F -- legacy_export .` returned `.\generated\client.ts:1:legacy_export();` (exit `0`). `Get-Content -LiteralPath .gitignore` returned `generated/` (exit `0`), which explains why the first search missed the call.
- **B.** `rg --files --hidden --no-ignore .` listed `.\app.py`, `.\.git\HEAD`, and `.\.git\config` (exit `0`). `rg -n --hidden --no-ignore --no-heading --color never -F -- dispatch_event .` produced empty output (exit `1`). `Get-Content -LiteralPath app.py` returned `start_server()` (exit `0`).
- **C.** `rg --files --hidden --no-ignore -g '*.rs' .` produced empty output (exit `1`). `rg --files --hidden --no-ignore .` listed `.\app.py`, `.\.git\HEAD`, and `.\.git\config` (exit `0`).
