import { createGrepTool, getAgentDir, VERSION, type ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { diagnose } from "./diagnose.mjs";
import { createHook } from "./hook.mjs";

export default function (pi: ExtensionAPI) {
  if (VERSION !== "1.5.1" && VERSION !== "1.6.0") return;
  // Pi also labels SDK base-tool replacements as built-in. The native tool uses
  // a shared parameter schema, which provides a second provenance check.
  const parameters = createGrepTool(process.cwd()).parameters;
  pi.on("tool_result", createHook({ pi, diagnose, getAgentDir, parameters }));
}
