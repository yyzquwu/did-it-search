import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { mkdir, mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
import { findPackageJSON, registerHooks } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

// npm install --prefix "$PI_ROOT" @earendil-works/pi-coding-agent@1.6.0
// node tests/pi-integration.mjs --pi-root "$PI_ROOT" [--demo] [--bench|--audit-reads]
// --samples <number> adjusts benchmark repetitions (default 24).
// Requires Node 22.19+, git and real ripgrep on PATH. Linux --audit-reads also uses strace.
const piPackage = '@earendil-works/pi-coding-agent';
const piAi = '@earendil-works/pi-ai';
const root = fileURLToPath(new URL('../', import.meta.url));
const args = process.argv.slice(2);
const option = (name) => args.includes(name) ? args[args.indexOf(name) + 1] : undefined;
const piRoot = option('--pi-root');
const extensionRoot = await realpath(option('--extension-root') ?? root);
assert(piRoot && !piRoot.startsWith('--'), 'Pass --pi-root <scratch npm install directory>');

const privateMarker = 'IGNORED-CALLER-CONTENT-782f530a';
const ignoredFile = 'ignored-caller.ts';
const dependencyFile = 'ignored-dependency.js';
const visibleFile = 'visible.ts';
const piCaller = pathToFileURL(resolve(piRoot, 'package.json')).href;
const packageJSON = async (path) => JSON.parse(await readFile(path, 'utf8'));
const rgVersion = execFileSync('rg', ['--version'], { encoding: 'utf8' }).split(/\r?\n/, 1)[0];

async function importPi() {
  let sdkUrl;
  const hook = registerHooks({
    resolve(specifier, context, next) {
      if (context.parentURL !== import.meta.url) return next(specifier, context);
      if (specifier === piAi || specifier === '@earendil-works/pi-agent-core') return next(specifier, { ...context, parentURL: sdkUrl });
      if (specifier !== piPackage) return next(specifier, context);
      const result = next(specifier, { ...context, parentURL: piCaller });
      sdkUrl = result.url;
      return result;
    },
  });
  try {
    const sdk = await import(piPackage);
    const ai = await import(piAi);
    const { Agent } = await import('@earendil-works/pi-agent-core');
    const manifestPath = findPackageJSON(piPackage, piCaller);
    assert.equal(await realpath(manifestPath), await realpath(resolve(piRoot, 'node_modules', piPackage, 'package.json')),
      'Pi must be installed directly in the supplied scratch directory');
    const manifest = await packageJSON(manifestPath);
    assert.equal(manifest.name, piPackage);
    assert(['1.6.0', '1.5.1'].includes(manifest.version), `Unexpected Pi version: ${manifest.version}`);
    return { sdk, ai, Agent, version: manifest.version };
  } finally {
    hook.deregister();
  }
}

async function withScratch(run) {
  const scratch = await mkdtemp(join(tmpdir(), 'did-it-search-pi-'));
  try {
    return await run(scratch);
  } finally {
    await rm(scratch, { recursive: true, force: true, maxRetries: 3 });
  }
}

async function fixture(scratch, name, dependencyCount = 0) {
  const cwd = join(scratch, name);
  await mkdir(join(cwd, 'src'), { recursive: true });
  execFileSync('git', ['init', '-q', cwd]);
  await Promise.all([
    writeFile(join(cwd, '.gitignore'), 'generated/\nnode_modules/\n'),
    writeFile(join(cwd, 'src', visibleFile), `export const marker = 'visibleNeedle';\n${'x'.repeat(1000)} longNeedle\n`),
    writeFile(join(cwd, '.hidden.ts'), 'export const marker = "hiddenNeedle";\n'),
  ]);
  if (!dependencyCount) {
    await mkdir(join(cwd, 'generated'));
    await writeFile(join(cwd, 'generated', ignoredFile), `refreshReviewedInput(); // ${privateMarker}\n`);
  }
  // 100 files per package, with nested directories typical of dependencies.
  const body = `export const marker = '${privateMarker}';\n${' '.repeat(4040)}\n`;
  for (let start = 0; start < dependencyCount; start += 100) {
    const dir = join(cwd, 'node_modules', `package-${start / 100}`, 'dist');
    await mkdir(dir, { recursive: true });
    await Promise.all(Array.from({ length: 100 }, (_, i) =>
      writeFile(join(dir, `${i}-${dependencyFile}`), body)));
  }
  return cwd;
}

const scenarios = {
  hit: { pattern: 'visibleNeedle' },
  miss: { pattern: 'absentNeedle', path: 'src' },
  ignored: { pattern: 'refreshReviewedInput' },
  glob: { pattern: 'visibleNeedle', glob: '*.no-such-extension' },
  hidden: { pattern: 'hiddenNeedle' },
  truncated: { pattern: 'longNeedle' },
};
const text = ({ content }) => content.filter(({ type }) => type === 'text').map((block) => block.text).join('\n');

async function createHarness({ sdk, ai, Agent }, scratch, cwd, enabled, manifest, grepRoot) {
  const agentDir = join(scratch, 'agent');
  const settingsManager = sdk.SettingsManager.inMemory({ packages: enabled ? [extensionRoot] : [] });
  const loader = new sdk.DefaultResourceLoader({
    cwd, agentDir, settingsManager, noSkills: true, noPromptTemplates: true, noContextFiles: true,
  });
  await loader.reload();
  const { extensions, errors } = loader.getExtensions();
  assert.deepEqual(errors, [], 'Pi could not load an extension');
  const discovered = extensions.find((ext) => manifest.pi.extensions.some((entry) =>
    resolve(ext.path) === resolve(extensionRoot, entry)));
  assert.equal(!!discovered, enabled, 'Pi package discovery disagrees with the settings package list');
  if (discovered) {
    assert.equal(discovered.sourceInfo.origin, 'package');
    assert.equal(discovered.handlers.has('tool_result'), true, 'Discovered extension must handle actual results');
  }

  // Only model input is scripted. Pi executes its native grep and dispatches extension hooks.
  const faux = ai.fauxProvider();
  const modelRuntime = await sdk.ModelRuntime.create({
    authPath: join(agentDir, 'auth.json'), modelsPath: null, modelsStorePath: join(agentDir, 'models-cache.json'),
    allowModelNetwork: false, refreshOnCreate: false,
  });
  modelRuntime.registerNativeProvider(faux.provider);
  const options = {
    cwd, agentDir, modelRuntime, settingsManager, resourceLoader: loader,
    sessionManager: sdk.SessionManager.inMemory(), model: faux.getModel(), tools: ['grep'],
  };
  let session;
  if (grepRoot) {
    const agent = new Agent({
      initialState: { systemPrompt: '', model: faux.getModel(), thinkingLevel: 'off', tools: [], messages: [] },
      convertToLlm: sdk.convertToLlm, streamFn: (model, ctx, opts) => modelRuntime.streamSimple(model, ctx, opts),
    });
    session = new sdk.AgentSession({ ...options, agent, baseToolsOverride: { grep: sdk.createGrepTool(grepRoot) } });
  } else {
    ({ session } = await sdk.createAgentSession(options));
  }
  const extensionErrors = [];
  await session.bindExtensions({ onError: (error) => extensionErrors.push(error) });
  assert.deepEqual(session.getActiveToolNames(), ['grep']);
  assert.equal(session.getAllTools().find((tool) => tool.name === 'grep')?.sourceInfo.path, 'builtin:grep');
  let sequence = 0;
  async function run(input) {
    const call = `grep-${++sequence}`;
    const events = [];
    let providerResult;
    let start;
    let elapsed;
    const unsubscribe = session.subscribe((event) => {
      if (event.toolCallId !== call) return;
      if (event.type === 'tool_execution_start') start = performance.now();
      if (event.type === 'tool_execution_end') {
        elapsed = performance.now() - start;
        events.push(event);
      }
    });
    faux.setResponses([
      ai.fauxAssistantMessage(ai.fauxToolCall('grep', { literal: true, ...input }, { id: call }), { stopReason: 'toolUse' }),
      (context) => {
        providerResult = context.messages.find((msg) => msg.role === 'toolResult' && msg.toolCallId === call);
        return ai.fauxAssistantMessage('Grep result received.');
      },
    ]);
    try {
      await session.prompt('Run the scripted grep.');
      assert.deepEqual(extensionErrors, [], 'Pi reported an extension error');
      assert.equal(faux.getPendingResponseCount(), 0, 'The scripted turns did not finish');
      assert.equal(events.length, 1, 'Expected one Pi tool_execution_end event');
      assert.equal(events[0].toolName, 'grep');
      assert.equal(events[0].isError, false, text(events[0].result));
      assert(Number.isFinite(elapsed), 'Missing Pi tool timing');
      assert(providerResult, 'Result never reached the scripted provider through Pi');
      const result = events[0].result;
      assert.deepEqual(providerResult.content, result.content, 'Provider received different grep content');
      assert(!JSON.stringify({ result, providerResult }).includes(privateMarker), 'Ignored content leaked');
      return { result, elapsed };
    } finally {
      unsubscribe();
    }
  }
  return { run, dispose: () => session.dispose() };
}

function assertNativePrefix(native, extended) {
  assert.deepEqual(extended.content.slice(0, native.content.length), native.content, 'Native grep content changed');
  assert.deepEqual(extended.details, native.details, 'Native grep details changed');
  assert.deepEqual(extended.structuredContent, native.structuredContent, 'Native grep structured content changed');
  assert.deepEqual(extended.usage, native.usage, 'Native grep usage changed');
}

async function withPair(pi, scratch, cwd, manifest, compare) {
  const off = await createHarness(pi, scratch, cwd, false, manifest);
  try {
    const on = await createHarness(pi, scratch, cwd, true, manifest);
    try {
      await compare(off, on);
    } finally {
      on.dispose();
    }
  } finally {
    off.dispose();
  }
}

async function verify(pi, scratch, manifest) {
  const cwd = await fixture(scratch, 'verification');
  const run = () => verifyResults(pi, scratch, cwd, manifest);
  if (process.platform === 'win32' && args.includes('--audit-reads')) {
    await withWindowsReadDenied(join(cwd, 'generated', ignoredFile), run);
  } else {
    await run();
  }
  const other = join(scratch, 'other-project');
  await mkdir(other);
  await writeFile(join(other, 'report.md'), 'other-project-marker\n');
  const embedded = await createHarness(pi, scratch, cwd, true, manifest, other);
  try {
    assert.match(text((await embedded.run({ pattern: 'other-project-marker', glob: '*.md' })).result), /report\.md:1/);
    assert.equal(text((await embedded.run({ pattern: 'absent', glob: '*.md' })).result), 'No matches found');
    console.log('SDK grep bound to another project: PASS');
  } finally {
    embedded.dispose();
  }
}

async function verifyResults(pi, scratch, cwd, manifest) {
  await withPair(pi, scratch, cwd, manifest, async (off, on) => {
    for (const [name, input] of Object.entries(scenarios)) {
      const { result: native } = await off.run(input);
      const { result: extended } = await on.run(input);
      assertNativePrefix(native, extended);
      const found = text(extended);
      switch (name) {
        case 'hit':
          assert.match(text(native), /src\/visible\.ts:1:.*visibleNeedle/);
          assert.deepEqual(extended, native);
          break;
        case 'hidden':
          assert.match(text(native), /\.hidden\.ts:1:.*hiddenNeedle/);
          assert.deepEqual(extended, native);
          break;
        case 'truncated':
          assert.equal(native.details?.linesTruncated, true, 'Fixture must exercise native grep details');
          assert.deepEqual(extended, native);
          break;
        case 'miss':
          assert.equal(text(native), 'No matches found');
          assert.deepEqual(extended, native);
          break;
        case 'ignored':
          assert.equal(text(native), 'No matches found');
          assert.equal(extended.content.length, native.content.length + 1);
          assert.match(extended.content.at(-1).text, /^\[Did It Search\] Skipped "generated\/ignored-caller\.ts" by "generated\/" at \.gitignore:1\./);
          break;
        case 'glob':
          assert.equal(text(native), 'No matches found');
          assert.equal(extended.content.length, native.content.length + 1);
          assert.match(extended.content.at(-1).text, /^\[Did It Search\].*\bglob\b.*\bselected no files\b/i);
          break;
      }
      console.log(`${name}: PASS`);
      if (args.includes('--demo') && name !== 'truncated') {
        console.log(`  grep ${JSON.stringify({ literal: true, ...input })}\n  Pi only: ${text(native)}\n  With extension: ${found}`);
      }
    }
  });
}

const percentile = (times, ratio) => times.toSorted((a, b) => a - b)[Math.ceil(times.length * ratio) - 1];

async function benchmark(pi, scratch, manifest) {
  const iterations = Number(option('--samples') ?? 24);
  assert(Number.isInteger(iterations) && iterations >= 4 && iterations <= 200, '--samples must be between 4 and 200');
  const requests = [scenarios.hit, { ...scenarios.miss, path: '.' }];
  console.log('Tool start through result hook and completion in the same warm SDK process (ms).');
  console.log(`3 warmups per variant, ${iterations} measured runs, alternating enabled/disabled order.`);
  for (const [name, count] of [['small', 100], ['large', 10_000]]) {
    const cwd = await fixture(scratch, name, count);
    await withPair(pi, scratch, cwd, manifest, async (off, on) => {
      for (const input of requests) {
        const times = new Map([[off, []], [on, []]]);
        const isHit = input === requests[0];
        for (let i = -3; i < iterations; i++) {
          for (const target of i % 2 === 0 ? [off, on] : [on, off]) {
            const { result, elapsed } = await target.run(input);
            if (isHit) assert.match(text(result), /visibleNeedle/);
            else assert.equal(text(result), 'No matches found', 'Routine dependency exclusions should stay quiet');
            assert.doesNotMatch(text(result), /\[Did It Search\]/);
            if (i >= 0) times.get(target).push(elapsed);
          }
        }
        const percentiles = (target) => [0.5, 0.95].map((p) => percentile(times.get(target), p).toFixed(2)).join('/');
        console.log(`${name} (${count} ignored dependency files) ${isHit ? 'hit' : 'miss'} p50/p95: off ${percentiles(off)}, on ${percentiles(on)}`);
      }
    });
  }
}

async function withWindowsReadDenied(path, run) {
  const system = join(process.env.SystemRoot, 'System32');
  const identity = execFileSync(join(system, 'whoami.exe'), ['/user', '/fo', 'csv', '/nh'], { encoding: 'utf8' });
  const sid = identity.match(/\bS-1-[\d-]+\b/i)?.[0];
  assert.ok(sid, 'Could not identify the Windows user for the scratch fixture');
  const acl = (...args) => execFileSync(join(system, 'icacls.exe'), [path, ...args], { windowsHide: true });
  // RD denies file data reads, while allowing metadata inspection and cleanup.
  acl('/deny', `*${sid}:(RD)`);
  const check = () => assert.rejects(readFile(path), e => ['EACCES', 'EPERM'].includes(e.code));
  try {
    await check();
    await run();
    await check();
    console.log('Windows: read access to the ignored fixture was denied throughout verification');
  } finally {
    acl('/remove:d', `*${sid}`);
  }
}

async function traceReads(scratch) {
  const log = join(scratch, 'open.log');
  const child = spawnSync('strace', [
    '-f', '-qq', '-e', 'trace=open,openat,openat2', '-o', log, process.execPath, fileURLToPath(import.meta.url),
    ...args.filter((arg) => arg !== '--audit-reads'),
  ], { env: { ...process.env, UV_USE_IO_URING: '0' }, stdio: 'inherit', timeout: 120_000 });
  if (child.error) throw child.error;
  assert.equal(child.status, 0, `Traced Pi verification exited ${child.status ?? child.signal}`);
  const readOpens = (await readFile(log, 'utf8')).split('\n')
    .filter((line) => /\bO_(?:RDONLY|RDWR)\b/.test(line) && !line.includes('O_DIRECTORY'));
  assert(readOpens.some((line) => line.includes(visibleFile)), 'Trace did not capture the visible file positive control');
  assert(!readOpens.some((line) => line.includes(ignoredFile)), 'An ignored file was opened for reading');
  console.log('strace: visible file opened, ignored file never opened for reading by Node or child processes');
}

await withScratch(async (scratch) => {
  if (args.includes('--audit-reads')) {
    assert(!args.includes('--bench'), 'Run the file audit and benchmark separately');
    if (process.platform === 'linux') return traceReads(scratch);
    assert.equal(process.platform, 'win32', '--audit-reads requires Linux strace or Windows file permissions');
  }
  Object.assign(process.env, {
    PI_OFFLINE: '1', PI_CODING_AGENT_DIR: join(scratch, 'agent'),
    GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: join(scratch, 'gitconfig'), GIT_CONFIG_COUNT: '0',
    XDG_CONFIG_HOME: join(scratch, 'config'),
  });
  const manifest = await packageJSON(join(extensionRoot, 'package.json'));
  assert.match(manifest.version, /^0\.6\./);
  assert(manifest.pi?.extensions?.length, 'No extension paths in the Did It Search package manifest');
  const pi = await importPi();
  console.log(`Pi ${pi.version}, Did It Search ${manifest.version}, Node ${process.versions.node}, ${rgVersion}`);
  await verify(pi, scratch, manifest);
  if (args.includes('--bench')) await benchmark(pi, scratch, manifest);
});
