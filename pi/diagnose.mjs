import { spawn } from 'node:child_process';
import { constants } from 'node:fs';
import { access, lstat, open, opendir, realpath, stat } from 'node:fs/promises';
import { basename, delimiter, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';

const WINDOWS = process.platform === 'win32';
const RG = WINDOWS ? 'rg.exe' : 'rg';
const MAX_MS = 1500;
const MAX_OUTPUT = 256 * 1024;
const MAX_RULE = 32 * 1024;
const MAX_EXAMPLES = 12;
const CONTROL_FILES = new Set(['.git', '.gitignore', '.ignore', '.rgignore']);
const ROUTINE_DIRS = new Set([
  'node_modules', '.venv', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache',
  '.cache', '.tox', '.nox', '.next', '.nuxt', '.parcel-cache', '.turbo',
  'build', 'dist', 'coverage', 'target',
]);
const UNSAFE = /[\p{Cc}\p{Cf}\p{Cs}\u2028\u2029\ufffd]/u;
const PI_SPACES = /[\u00a0\u2000-\u200a\u202f\u205f\u3000]/u;
const DEBUG_LINE = /^(?:rg: )?DEBUG\|([\w:]+)\|.+?:\d+: (.*)$/u;
const IGNORED = /^ignoring (.+?): (Ignore\(IgnoreMatch\(.+\)\)|does not match glob)$/u;
const RULE = /^Ignore\(IgnoreMatch\(Gitignore\(Glob \{ from: Some\(("(?:[^"\\]|\\.)*")\), original: ("(?:[^"\\]|\\.)*"), actual: "(?:[^"\\]|\\.)*", is_whitelist: false, is_only_dir: (?:true|false) \}\)\)\)$/u;

function safeString(value, limit) {
  return typeof value === 'string' && value.length <= limit && !UNSAFE.test(value);
}

function localPath(value, absolute = false) {
  if (!safeString(value, 4096) || !value || (absolute && !isAbsolute(value))) return false;
  if (!WINDOWS) return !/^\/\/|[\\:]/u.test(value);
  if (/^[\\/]/u.test(value) || /^[A-Za-z]:(?![/\\])/u.test(value)) return false;
  return value.replace(/^[a-z]:/iu, '').split(/[\\/]/u).every((part) =>
    part === '.' || part === '..' || !/[:*?"<>|]|[ .]$|^(?:con|prn|aux|nul|(?:com|lpt)[0-9¹²³])(?:\.|$)/iu.test(part));
}

function within(base, path) {
  const child = relative(base, path);
  return !isAbsolute(child) && child !== '..' && !child.startsWith('..' + sep);
}

function display(path) {
  return path.split(sep).join('/');
}

function safeLabel(value) {
  return safeString(value, 200) && !/[<>"\x60]/u.test(value);
}

function routineName(name) {
  return ROUTINE_DIRS.has(name) || name === '.env' || name.startsWith('.env.');
}

async function exists(path) {
  try {
    await lstat(path);
    return true;
  } catch (error) {
    if (['ENOENT', 'ENOTDIR'].includes(error.code)) return false;
    throw error;
  }
}

async function rgExecutable(supplied, cwd, signal) {
  if (supplied !== undefined && supplied !== 'rg') {
    if (!localPath(supplied, true) || (WINDOWS && !/\.exe$/iu.test(supplied))) return null;
    const actual = await realpath(supplied);
    return localPath(actual, true) ? actual : null;
  }

  const path = process.env.PATH ?? '';
  if (path.length > 32768) return null;
  const dirs = path.split(delimiter);
  if (dirs.length > 128) return null;
  if (WINDOWS) {
    // A cwd-local executable or an earlier .com can shadow the PATH .exe.
    for (const dir of new Set([cwd, process.cwd()])) {
      if (!localPath(dir, true)) return null;
      if (await exists(join(dir, RG)) || await exists(join(dir, 'rg.com'))) return null;
    }
  }
  for (const dir of dirs) {
    signal.throwIfAborted();
    // A relative or network PATH entry can resolve differently after Pi changes cwd.
    if (!localPath(dir, true)) return null;
    if (WINDOWS && await exists(join(dir, 'rg.com'))) return null;
    const candidate = join(dir, RG);
    try {
      if (!(await stat(candidate)).isFile()) continue;
      await access(candidate, constants.X_OK);
      const actual = await realpath(candidate);
      return localPath(actual, true) ? actual : null;
    } catch (error) {
      if (!['ENOENT', 'ENOTDIR', 'EACCES'].includes(error.code)) throw error;
    }
  }
  return null;
}

function inventory(executable, cwd, glob, signal) {
  return new Promise((done) => {
    const args = ['--no-config', '--hidden', '--files', '--null', '--debug'];
    if (glob !== undefined) args.push('--glob', glob);
    const child = spawn(executable, [...args, '--', '.'], {
      cwd, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true, signal, killSignal: 'SIGKILL',
    });
    const stdout = [];
    const stderr = [];
    let bytes = 0;
    let overflow = false;
    const collect = (chunks) => (chunk) => {
      if (overflow) return;
      bytes += chunk.length;
      if (bytes > MAX_OUTPUT) {
        overflow = true;
        child.kill('SIGKILL');
      } else {
        chunks.push(chunk);
      }
    };
    child.stdout.on('data', collect(stdout));
    child.stderr.on('data', collect(stderr));
    child.on('error', () => done(null));
    child.on('close', (code) => {
      if (overflow || signal.aborted || (code !== 0 && code !== 1)) return done(null);
      const files = Buffer.concat(stdout);
      if (code === 0 ? files.at(-1) !== 0 : files.length !== 0) return done(null);
      done({ files: files.toString('utf8'), trace: Buffer.concat(stderr).toString('utf8') });
    });
  });
}

function traceEvidence(trace, cwd, exactTarget) {
  const ignored = [];
  let exactIgnore;
  let globExcluded = false;
  let hasIgnored = false;
  for (const line of trace.trimEnd().split(/\r?\n/u)) {
    const debug = DEBUG_LINE.exec(line);
    // Errors, unknown output and multiline paths cannot establish coverage.
    if (!debug || UNSAFE.test(debug[2])) return null;
    if (debug[1] !== 'ignore::walk' || !debug[2].startsWith('ignoring ')) continue;
    const match = IGNORED.exec(debug[2]);
    if (!match || !localPath(match[1]) || isAbsolute(match[1])) return null;
    const path = resolve(cwd, match[1]);
    if (path === cwd || !within(cwd, path)) return null;
    if (match[2].startsWith('Ignore(IgnoreMatch(Override(') || match[2] === 'does not match glob') {
      globExcluded = true;
    } else if (match[2].startsWith('Ignore(IgnoreMatch(Gitignore(')) {
      const parts = relative(cwd, path).toLowerCase().split(sep);
      if (parts.some((part) => CONTROL_FILES.has(part))) continue;
      hasIgnored = true;
      const exact = exactTarget && within(path, exactTarget);
      const example = ignored.length < MAX_EXAMPLES && !parts.some(routineName);
      if (!exact && !example) continue;
      const candidate = { path, rule: RULE.exec(match[2]) };
      if (exact) exactIgnore ??= candidate;
      if (example) ignored.push(candidate);
    }
  }
  return { ignored, exactIgnore, globExcluded, hasIgnored };
}

async function ruleLocation(rule, cwd, base, skipped, signal) {
  if (!rule) return null;
  try {
    let source = JSON.parse(rule[1]);
    // Windows ripgrep uses a verbatim drive path for parent ignore files.
    if (WINDOWS) source = source.replace(/^\\\\\?\\(?=[a-z]:\\)/iu, '');
    const pattern = JSON.parse(rule[2]);
    if (!localPath(source) || !safeLabel(pattern) || !['.gitignore', '.ignore', '.rgignore'].includes(basename(source))) return null;
    const path = resolve(cwd, source);
    if (!within(base, path) || !within(dirname(path), skipped)) return null;
    const label = display(relative(base, path));
    if (!safeLabel(label)) return null;
    const before = await lstat(path);
    if (!before.isFile() || before.size > MAX_RULE || relative(path, await realpath(path))) return null;
    signal.throwIfAborted();
    const file = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0) | (constants.O_NONBLOCK ?? 0));
    try {
      const current = await file.stat();
      if (!current.isFile() || current.ino !== before.ino || current.dev !== before.dev || current.size > MAX_RULE) return null;
      signal.throwIfAborted();
      const buf = Buffer.alloc(MAX_RULE + 1);
      const { bytesRead } = await file.read(buf, 0, buf.length, 0);
      const after = await file.stat();
      if (bytesRead !== current.size || current.mtimeMs !== before.mtimeMs || after.mtimeMs !== current.mtimeMs) return null;
      const lines = new TextDecoder('utf-8', { fatal: true }).decode(buf.subarray(0, bytesRead)).split(/\r?\n/u);
      let lineNumber;
      for (const [index, line] of lines.entries()) {
        if (line.trimEnd() !== pattern) continue;
        if (lineNumber !== undefined) return null;
        lineNumber = index + 1;
      }
      return lineNumber === undefined ? null : { pattern, location: label + ':' + lineNumber };
    } finally {
      await file.close();
    }
  } catch {
    return null;
  }
}

async function directoryExample(path, signal) {
  const dir = await opendir(path, { bufferSize: MAX_EXAMPLES });
  try {
    let file;
    let hasDirectory = false;
    for (let i = 0; i < MAX_EXAMPLES; i++) {
      signal.throwIfAborted();
      const entry = await dir.read();
      if (!entry) return file ?? (hasDirectory ? path + sep : null);
      const name = entry.name.toLowerCase();
      if (CONTROL_FILES.has(name) || routineName(name) || !localPath(entry.name)) continue;
      if (entry.isDirectory()) hasDirectory = true;
      if (!entry.isFile()) continue;
      if (file) break;
      file = join(path, entry.name);
    }
    return file || hasDirectory ? path + sep : null;
  } finally {
    await dir.close();
  }
}

async function localDirectory(cwd, target, signal) {
  const parts = relative(cwd, target).split(sep).filter(Boolean);
  if (parts.length > 32 || parts.some((part) => part.toLowerCase() === '.git')) return null;
  const base = await realpath(cwd);
  if (!localPath(base, true) || !(await stat(base)).isDirectory()) return null;
  let root = base;
  for (const part of parts) {
    signal.throwIfAborted();
    root = join(root, part);
    if (!(await lstat(root)).isDirectory()) return null;
  }
  if (relative(root, await realpath(root))) return null;
  return { base, root };
}

async function ignoredMessage(example, { path, rule }, root, base, signal, requireOrigin = false) {
  const label = display(relative(base, example)) + (example.endsWith(sep) ? '/' : '');
  if (!safeLabel(label)) return null;
  const origin = await ruleLocation(rule, root, base, path, signal);
  const next = '. Ask before inspecting this path.';
  const skipped = 'Skipped "' + label + '"';
  if (origin) {
    const precise = skipped + ' by "' + origin.pattern + '" at ' + origin.location + next;
    if (precise.length <= 340) return precise;
  }
  return requireOrigin ? null : skipped + ' by an ignore rule' + next;
}

function exactGlobTarget(glob, root) {
  // A literal, directory-qualified glob can name a file behind a pruned
  // ancestor. Other globs would require exploring possibly irrelevant trees.
  if (!glob || !/^\/?(?:[a-z0-9._-]+\/)+[a-z0-9._-]+$/iu.test(glob)) return null;
  const name = glob.replace(/^\//u, '');
  if (!localPath(name) || name.split('/').some((part) => part === '.' || part === '..' ||
      CONTROL_FILES.has(part.toLowerCase()))) return null;
  return resolve(root, name);
}

async function ignoredGlobTarget(target, candidate, root, base, signal) {
  try {
    if (!(await localDirectory(root, dirname(target), signal)) || !(await lstat(target)).isFile()) return null;
    if (await realpath(target) !== target) return null;
    return await ignoredMessage(target, candidate, root, base, signal, true);
  } catch {
    return null;
  }
}

async function explain({ input, cwd, rgPath }, signal) {
  if (!input || input.literal !== true || ![undefined, false].includes(input.ignoreCase) ||
      !safeString(input.pattern, 4096) || !input.pattern || !localPath(cwd, true)) return null;
  const path = input.path === undefined ? '.' : input.path;
  if (!localPath(path) || /^[~@]/u.test(path) || PI_SPACES.test(path)) return null;
  if (input.glob !== undefined && !safeString(input.glob, 1024)) return null;
  const glob = input.glob ? input.glob.replace(/^(!?)(?:\.\/)+/u, '$1') : undefined;
  if (glob === '' || glob === '!') return null;

  const target = resolve(cwd, path);
  if (!within(cwd, target)) return null;
  const scope = await localDirectory(cwd, target, signal);
  if (!scope) return null;
  const { base, root } = scope;
  const executable = await rgExecutable(rgPath, root, signal);
  if (!executable || signal.aborted) return null;

  const result = await inventory(executable, root, glob, signal);
  if (!result || result.files.includes('\ufffd')) return null;
  const exactTarget = exactGlobTarget(glob, root);
  const evidence = traceEvidence(result.trace, root, exactTarget);
  if (!evidence) return null;
  if (glob !== undefined) {
    if (result.files) return null;
    if (evidence.exactIgnore) return ignoredGlobTarget(exactTarget, evidence.exactIgnore, root, base, signal);
    // A pruned tree can contain a glob match. Its trace proves exclusion, not
    // what the glob would have selected inside it.
    if (!evidence.globExcluded || (!exactTarget && evidence.hasIgnored)) return null;
    return 'The glob selected no files in Pi’s search scope. Check it relative to the search directory.';
  }

  const included = new Set(result.files.split('\0').filter(Boolean).map((file) => resolve(root, file)));
  for (const candidate of evidence.ignored.sort((a, b) => a.path < b.path ? -1 : Number(a.path > b.path))) {
    signal.throwIfAborted();
    const { path } = candidate;
    if (included.has(path)) continue;
    const entry = await lstat(path);
    if (!entry.isFile() && !entry.isDirectory()) continue;
    if (relative(path, await realpath(path))) continue;
    if (entry.isDirectory() && [...included].some((file) => within(path, file))) continue;
    const example = entry.isDirectory() ? await directoryExample(path, signal) : path;
    if (!example) continue;
    const diagnosis = await ignoredMessage(example, candidate, root, base, signal);
    if (diagnosis) return diagnosis;
  }
  return null;
}

/** Diagnose an already-observed empty result from Pi’s built-in local grep. */
export async function diagnose(options) {
  const { signal } = options ?? {};
  if (!options || signal?.aborted) return null;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), MAX_MS);
  try {
    const budget = signal ? AbortSignal.any([signal, controller.signal]) : controller.signal;
    const aborted = new Promise((done) => budget.addEventListener('abort', () => done(null), { once: true }));
    const result = await Promise.race([explain(options, budget), aborted]);
    return budget.aborted ? null : result;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}
