# Three empty searches

Three independent agents answered the same three questions about synthetic projects on Windows. The two Codex sessions received empty stdout and exit 1 for all three initial ripgrep searches. The Claude Code Grep tool returned `No matches found` on all three and exposed no exit code. **Each agent recovered all three answers by following up.**

| Project | Question | Ground truth |
| --- | --- | --- |
| A | Is legacy_export called anywhere in this project? | Yes, in an ignored generated client. |
| B | Is dispatch_event called anywhere in this project? | No. The only source file calls start_server. |
| C | Is dispatch_event called in any *.rs files in this project? | There are zero Rust files. A Python file calls it. |

| Configuration | First search | Agent's account of commands, raw first outputs, and final answers |
| --- | --- | --- |
| GPT-6.2 Sol, high, Codex | ripgrep | [Sol response](responses/sol.md) |
| GPT-6.2 Astra, high, Codex | ripgrep | [Astra response](responses/astra.md) |
| Claude Opus 5.5, high, Claude Code | Grep | [Opus response](responses/opus.md) |

## Repeat the pilot

Install the kit from the repository and run `python study/prepare.py pilot` from this checkout. Open a fresh agent session and use the following prompt, substituting the absolute project directory for `<root>`.

> Answer three maintainer questions using your usual repository text-search tools. The three unrelated synthetic projects are A, B and C immediately under <root>. Only inspect those three directories, read-only. A: Is legacy_export called anywhere in this project? B: Is dispatch_event called anywhere in this project? C: Is dispatch_event called in any *.rs files in this project? For each give the first text-search invocation, its exact output and exit status if exposed, any follow-up you used and your final answer. Use relative paths or <root> for absolute prefixes in your answer. Do not look up these questions, create files, post, or start subagents.

The original runs used one fresh session per configuration, three questions per session, and fixtures created with Git 2.56.1. Codex had ripgrep 15.2.0 on PATH. Claude Code used its Grep tool. A subsequent review identified ripgrep 14.1.1 bundled with Claude Code, although the pilot response recorded no binary version. Each response is that agent's record of its run. The request to document follow-ups could encourage extra checking. This pilot measures neither a population error rate nor the effect of adding diagnostics. The three configurations used two tool environments.
