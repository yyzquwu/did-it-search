import { createGrepToolDefinition, getAgentDir, VERSION, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { diagnose } from "./diagnose.mjs";
import { createHook } from "./hook.mjs";

export default function (pi: ExtensionAPI) {
  if (VERSION !== "1.5.1" && VERSION !== "1.6.0") return;
  const native = createGrepToolDefinition(process.cwd());
  const hook = createHook({ pi, diagnose, getAgentDir, parameters: native.parameters });
  // Pi's SDK baseToolsOverride keeps the label and schema, but drops the
  // definition's prompt snippet. Check Pi's own per-turn tool metadata.
  let nativeGrep = false;
  pi.on("before_agent_start", (event) => {
    nativeGrep = !!native.promptSnippet && event.systemPromptOptions?.toolSnippets?.grep === native.promptSnippet;
  });
  pi.on("agent_end", () => { nativeGrep = false; });
  pi.on("tool_result", (event, ctx) => nativeGrep ? hook(event, ctx) : undefined);
}
