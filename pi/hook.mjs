import { constants } from "node:fs";
import { access, realpath, stat } from "node:fs/promises";
import { delimiter, isAbsolute, join, resolve } from "node:path";

const NO_MATCH = "No matches found";
const NOTE_PREFIX = "[Did It Search] ";
const MAX_NOTE = 360;
const DEADLINE_MS = 1500;
const WIN = process.platform === "win32";
const INPUT_KEYS = new Set(["pattern", "path", "glob", "literal", "ignoreCase", "context", "limit"]);
const UNSAFE = /[\p{Cc}\p{Cf}\p{Cs}\u2028\u2029\ufffd]/u;

function supportedGlob(glob) {
  if (glob === undefined) return true;
  if (typeof glob !== "string" || glob.length > 256 || UNSAFE.test(glob)) return false;
  const normalized = glob.replace(/^(!?)(?:\.\/)+/, "$1");
  return normalized !== "" && normalized !== "!";
}

function eligibleInput(input) {
  if (!input || Object.keys(input).some((key) => !INPUT_KEYS.has(key))) return false;
  const { pattern, path, glob, literal, ignoreCase, context, limit } = input;
  return literal === true && (ignoreCase === undefined || ignoreCase === false)
    && (context === undefined || context === 0) && (limit === undefined || limit === 100)
    && typeof pattern === "string" && pattern.length > 0 && pattern.length <= 1024
    && !UNSAFE.test(pattern) && (path === undefined || safeRelativePath(path)) && supportedGlob(glob);
}

function safeRelativePath(path) {
  return typeof path === "string" && path.length <= 512 && !path.startsWith("/")
    && /^[a-z0-9_./ -]+$/i.test(path) && path.split("/").every((part) =>
      part !== ".." && part.toLowerCase() !== ".git"
      && (!WIN || part === "." || !/[ .]$|^(?:con|prn|aux|nul|(?:com|lpt)[0-9])(?:\.|$)/i.test(part)));
}

function localAbsolute(path) {
  return typeof path === "string" && isAbsolute(path)
    && (WIN ? /^[a-z]:[\\/]/i.test(path) : !path.startsWith("//"));
}

async function localDirectory(cwd, path) {
  if (!localAbsolute(cwd)) return;
  const realCwd = await realpath(cwd);
  if (!localAbsolute(realCwd)) return;
  const target = resolve(realCwd, path ?? ".");
  const actual = target === realCwd ? realCwd : await realpath(target);
  if (actual === target && (await stat(actual)).isDirectory()) return target;
}

async function statIfPresent(path) {
  try {
    return await stat(path);
  } catch (error) {
    if (error?.code === "ENOENT" || error?.code === "ENOTDIR") return;
    throw error;
  }
}

export async function resolveRg(agentDir, cwd, signal, path = process.env.PATH) {
  if (!localAbsolute(agentDir)) return;
  const filename = WIN ? "rg.exe" : "rg";
  const cached = join(agentDir, "bin", filename);
  const cachedStat = await statIfPresent(cached);
  if (cachedStat) return cachedStat.isFile() ? cached : undefined;

  // Pi next calls bare rg. On Windows, Node checks cwd first and prefers .com
  // over .exe. Skip ambiguous or cwd-local candidates rather than run a shim.
  if (WIN && ((await statIfPresent(join(cwd, "rg.com")))?.isFile()
    || (await statIfPresent(join(cwd, filename)))?.isFile())) return;
  if (!path || path.length > 32768) return;
  for (const dir of path.split(delimiter)) {
    if (signal.aborted || !localAbsolute(dir) || /["\u0000-\u001f\u007f]/.test(dir)) return;
    if (WIN && (await statIfPresent(join(dir, "rg.com")))?.isFile()) return;
    const candidate = join(dir, filename);
    if (!(await statIfPresent(candidate))?.isFile()) continue;
    try {
      await access(candidate, constants.X_OK);
      return candidate;
    } catch (error) {
      if (WIN || error?.code !== "EACCES") throw error;
    }
  }
}

function boundedNote(note) {
  if (typeof note !== "string" || !note.trim() || note.length > MAX_NOTE || UNSAFE.test(note)) return;
  return NOTE_PREFIX + note;
}

export function createHook({ pi, diagnose, getAgentDir, parameters, deadlineMs = DEADLINE_MS }) {
  return async (event, ctx) => {
    if (event.toolName !== "grep" || event.isError !== false || event.parentToolCallId
      || event.content?.length !== 1 || event.content[0]?.type !== "text"
      || event.content[0].text !== NO_MATCH || event.details?.truncation
      || event.details?.matchLimitReached || event.details?.linesTruncated
      || !eligibleInput(event.input)) return;

    try {
      const tool = pi.getAllTools().find(({ name }) => name === "grep");
      if (tool?.sourceInfo?.source !== "builtin" || tool.sourceInfo.path !== "builtin:grep"
        || tool.parameters !== parameters) return;
      const sessionSignal = ctx.signal;
      if (sessionSignal?.aborted) return;
      const timeout = new AbortController();
      const timer = setTimeout(() => timeout.abort(), deadlineMs);
      const signal = sessionSignal ? AbortSignal.any([sessionSignal, timeout.signal]) : timeout.signal;
      let stop;
      try {
        const aborted = new Promise((accept) => {
          stop = () => accept(undefined);
          signal.addEventListener("abort", stop, { once: true });
        });
        const inspect = async () => {
          const { cwd } = ctx;
          const { input } = event;
          const searchDir = await localDirectory(cwd, input.path);
          if (!searchDir || signal.aborted) return;
          const rgPath = await resolveRg(getAgentDir(), searchDir, signal);
          if (rgPath && !signal.aborted) return diagnose({ input, cwd, signal, rgPath });
        };
        const note = boundedNote(await Promise.race([inspect(), aborted]));
        if (!note || signal.aborted) return;
        return {
          content: [...event.content, { type: "text", text: note }],
          details: event.details,
          ...(Object.hasOwn(event, "structuredContent") && { structuredContent: event.structuredContent }),
          isError: event.isError,
          usage: event.usage,
        };
      } finally {
        clearTimeout(timer);
        signal.removeEventListener("abort", stop);
      }
    } catch {
      // Diagnostics are optional. Pi's original response stays authoritative.
    }
  };
}
