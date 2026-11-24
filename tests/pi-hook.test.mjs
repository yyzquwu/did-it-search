import assert from "node:assert/strict";
import { mkdir, mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";
import test from "node:test";
import { createHook, resolveRg } from "../pi/hook.mjs";

const builtin = { source: "builtin", path: "builtin:grep" };
const rgName = process.platform === "win32" ? "rg.exe" : "rg";
const evidence = "The glob selected no files in the searched directory. Review the glob.";
const native = { type: "text", text: "No matches found" };
const event = (input = {}, result = {}) => ({
  type: "tool_result",
  toolName: "grep",
  toolCallId: "call1",
  input: { pattern: "legacy_export", literal: true, ...input },
  content: [native],
  isError: false,
  ...result,
});

async function setup(t, options = {}) {
  const root = await mkdtemp(join(tmpdir(), "did-it-search-hook-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const cwd = join(root, "repo");
  const agentDir = join(root, "agent");
  const bin = join(agentDir, "bin");
  const cached = join(bin, rgName);
  await mkdir(cwd);
  await mkdir(bin, { recursive: true });
  await writeFile(cached, "", { mode: 0o755 });
  const calls = [];
  const parameters = {};
  let lookups = 0;
  const hook = createHook({
    pi: {
      getAllTools() {
        lookups++;
        return [{ name: "grep", sourceInfo: options.source ?? builtin, parameters: options.parameters ?? parameters }];
      },
    },
    getAgentDir: () => agentDir,
    parameters,
    deadlineMs: options.deadlineMs,
    diagnose: async (request) => {
      calls.push(request);
      return options.diagnose ? options.diagnose(request) : evidence;
    },
  });
  return { root, cwd, agentDir, cached, ctx: { cwd }, hook, calls, lookups: () => lookups };
}

test("appends one note and preserves native content, details, usage and structured content", async (t) => {
  const { cwd, cached, ctx, hook, calls } = await setup(t);
  const original = event({ ignoreCase: false, context: 0, limit: 100, glob: "*.ts" }, {
    details: { sentinel: 7 },
    usage: { input: 8 },
    structuredContent: { values: [null, "sentinel"] },
  });
  const result = await hook(original, ctx);
  assert.deepEqual(result.content, [native, { type: "text", text: "[Did It Search] " + evidence }]);
  assert.equal(result.content[0], original.content[0]);
  assert.equal(result.details, original.details);
  assert.equal(result.usage, original.usage);
  assert.equal(result.structuredContent, original.structuredContent);
  assert.equal(result.isError, false);
  assert.deepEqual(original.content, [native]);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].input, original.input);
  assert.equal(calls[0].cwd, cwd);
  assert.equal(calls[0].rgPath, cached);
  assert.equal(await hook({ ...original, ...result }, ctx), undefined);
  assert.equal(calls.length, 1);
});

test("does no work for hits, unusual results, unsupported input, and nested calls", async (t) => {
  const { ctx, hook, calls, lookups } = await setup(t);
  for (const result of [
    event({}, { content: [{ ...native, text: "x.ts:1: legacy_export" }] }),
    event({}, { isError: true }),
    event({}, { details: { matchLimitReached: 100 } }),
    event({}, { content: [{ ...native, text: "No matches found\n" }] }),
    event({}, { content: [native, { type: "image", data: "AA==" }] }),
    event({}, { parentToolCallId: "outer" }),
    event({}, { toolName: "bash" }),
    event({ literal: false }),
    event({ ignoreCase: true }),
    event({ context: 1 }),
    event({ limit: 1 }),
    event({ flags: "--no-ignore" }),
    event({ glob: null }),
    event({ glob: "./" }),
    event({ glob: "!./" }),
    event({ pattern: "" }),
    event({ pattern: "a\nb" }),
    event({ path: "../outside" }),
    event({ path: ".git/refs" }),
    event({ path: "https://example.com/project" }),
    event({ path: "C:/fake" }),
    event({ path: "\\\\host\\share" }),
    event({ path: "@/project" }),
  ]) {
    assert.equal(await hook(result, ctx), undefined, JSON.stringify(result));
  }
  assert.equal(calls.length, 0);
  assert.equal(lookups(), 0);
});

test("requires local directory scope and native registry provenance", async (t) => {
  const { root, cwd, ctx, hook, calls } = await setup(t);
  await writeFile(join(cwd, "file.ts"), "");
  await mkdir(join(cwd, "src"));
  await mkdir(join(root, "elsewhere"));
  await symlink(join(root, "elsewhere"), join(cwd, "link"), "junction");
  for (const path of ["file.ts", "missing", "link", root]) {
    assert.equal(await hook(event({ path }), ctx), undefined);
  }
  assert.equal(await hook(event(), { cwd: "//127.0.0.1/share" }), undefined);
  assert.equal(calls.length, 0);
  const annotated = await hook(event({ path: "src" }), ctx);
  assert.equal(annotated.content.length, 2);
  assert.equal(Object.hasOwn(annotated, "structuredContent"), false);
  const custom = await setup(t, { source: { source: "sdk", path: "<sdk:grep>" } });
  assert.equal(await custom.hook(event(), custom.ctx), undefined);
  assert.equal(custom.calls.length, 0);
  const override = await setup(t, { parameters: {} });
  assert.equal(await override.hook(event(), override.ctx), undefined);
  assert.equal(override.calls.length, 0);
});

test("passes the exact cached ripgrep location before a PATH fallback", async (t) => {
  const { agentDir, root, cwd, cached } = await setup(t);
  const bin = join(root, "path-bin");
  const fallback = join(bin, rgName);
  await mkdir(bin);
  await writeFile(fallback, "", { mode: 0o755 });
  const signal = new AbortController().signal;
  assert.equal(await resolveRg(agentDir, cwd, signal, bin), cached);
  await rm(cached);
  assert.equal(await resolveRg(agentDir, cwd, signal, bin), fallback);
  assert.equal(await resolveRg(agentDir, cwd, signal, ["relative", bin].join(delimiter)), undefined);
  if (process.platform === "win32") {
    await writeFile(join(cwd, "rg.com"), "");
    assert.equal(await resolveRg(agentDir, cwd, signal, bin), undefined);
  }
});

test("keeps missing, malformed, oversized, and failed diagnoses quiet", async (t) => {
  const { ctx, hook } = await setup(t, {
    diagnose: ({ input }) => {
      if (input.pattern === "throws") throw new Error("worker failed");
      return { absent: undefined, empty: " ", object: {}, long: "a".repeat(361), unsafe: "line\nbreak" }[input.pattern];
    },
  });
  for (const pattern of ["absent", "empty", "object", "long", "unsafe", "throws"]) {
    assert.equal(await hook(event({ pattern }), ctx), undefined);
  }
});

test("cancels in-flight diagnosis and stops waiting at the deadline", async (t) => {
  const controller = new AbortController();
  let start;
  const started = new Promise((accept) => { start = accept; });
  const pending = await setup(t, {
    diagnose: ({ signal }) => { start(signal); return new Promise(() => {}); },
  });
  const result = pending.hook(event(), { ...pending.ctx, signal: controller.signal });
  const signal = await started;
  controller.abort();
  assert.equal(await result, undefined);
  assert.equal(signal.aborted, true);
  assert.equal(await pending.hook(event(), { ...pending.ctx, signal: controller.signal }), undefined);
  assert.equal(pending.calls.length, 1);

  const timed = await setup(t, { deadlineMs: 75, diagnose: () => new Promise(() => {}) });
  assert.equal(await timed.hook(event(), timed.ctx), undefined);
  assert.equal(timed.calls[0].signal.aborted, true);
});
