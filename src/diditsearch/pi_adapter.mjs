import { readFileSync, realpathSync, statSync } from 'node:fs';
import { findPackageJSON, registerHooks } from 'node:module';
import { dirname, isAbsolute, join, relative, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const PACKAGE = '@earendil-works/pi-coding-agent';
const MAX_STDOUT = 64 * 1024;
const VERSION = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(0|[1-9]\d*|\d*[a-zA-Z-][\da-zA-Z-]*)(?:\.(0|[1-9]\d*|\d*[a-zA-Z-][\da-zA-Z-]*))*)?(?:\+[\da-zA-Z-]+(?:\.[\da-zA-Z-]+)*)?$/;

function callerURL(root, hint = '') {
  if (!root || !isAbsolute(root)) {
    throw new Error(`Set DIDITSEARCH_PI_ROOT to the absolute caller project directory${hint}`);
  }
  return pathToFileURL(join(root, 'package.json')).href;
}

// The published SDK exposes an import-only entry, so anchor Node's ESM resolution at the caller.
function callerHook(parentURL) {
  return registerHooks({
    resolve(specifier, context, next) {
      return next(specifier, specifier === PACKAGE && context.parentURL === import.meta.url
        ? { ...context, parentURL } : context);
    },
  });
}

async function loadPi() {
  const { DIDITSEARCH_PI_MODULE: entry, DIDITSEARCH_PI_ROOT: root } = process.env;
  if (entry) {
    if (!isAbsolute(entry)) throw new Error('DIDITSEARCH_PI_MODULE must be an absolute JS module path');
    return import(pathToFileURL(entry).href);
  }
  const hook = callerHook(callerURL(root, ' or set DIDITSEARCH_PI_MODULE'));
  try {
    return await import(PACKAGE);
  } catch (error) {
    throw new Error(`Could not import ${PACKAGE} from ${root}: ${error.message}`);
  } finally {
    hook.deregister();
  }
}

function identifyPi() {
  const root = process.env.DIDITSEARCH_PI_ROOT;
  const parentURL = callerURL(root);
  if (!statSync(root).isDirectory()) throw new Error('DIDITSEARCH_PI_ROOT must be a caller project directory');
  const hook = callerHook(parentURL);
  let entryURL;
  try {
    entryURL = import.meta.resolve(PACKAGE);
  } finally {
    hook.deregister();
  }

  // Use the bare specifier to locate the package root even if the entry has a nested manifest.
  const manifest = findPackageJSON(PACKAGE, parentURL);
  if (!manifest) throw new Error(`Missing package.json for ${PACKAGE}`);
  const packageDir = realpathSync(dirname(manifest));
  const entry = realpathSync(fileURLToPath(entryURL));
  const entryPath = relative(packageDir, entry);
  if (isAbsolute(entryPath) || entryPath.split(sep).some((part) => part === '..' || part.toLowerCase() === 'node_modules')
      || relative(packageDir, dirname(realpathSync(manifest))) || !statSync(entry).isFile()) {
    throw new Error(`Package manifest and resolved entry do not identify the same ${PACKAGE} installation`);
  }

  const { name, version } = JSON.parse(readFileSync(manifest, 'utf8')) ?? {};
  if (name !== PACKAGE || typeof version !== 'string' || !VERSION.test(version)) {
    throw new Error(`Expected the ${PACKAGE} package name and a valid version in package.json`);
  }
  return { name, version };
}

function grepArgs(request, tool) {
  if (!request || typeof request.pattern !== 'string') throw new Error('Expected a string pattern');
  const properties = tool.parameters?.properties ?? {};
  if (!Object.hasOwn(properties, 'literal')) throw new Error('Pi grep does not expose literal search');
  const args = { pattern: request.pattern, literal: true };
  for (const key of ['path', 'glob']) {
    const value = request[key];
    if (value === undefined || (key === 'glob' && value === null)) continue;
    if (typeof value !== 'string') throw new Error(`Expected a string ${key}`);
    if (!Object.hasOwn(properties, key)) throw new Error(`Pi grep does not expose ${key}`);
    args[key] = value;
  }
  return args;
}

function normalize(raw) {
  const text = raw?.content?.filter(({ type }) => type === 'text').map(({ text }) => text);
  if (!text?.length || text.some((block) => typeof block !== 'string')) {
    throw new Error('Pi grep returned no text content');
  }
  const detail = text.join('\n');
  if (raw.isError) {
    if (detail.startsWith('Path not found: ')) return { status: 'missing_path', detail, raw };
    throw new Error(detail);
  }
  if (detail === 'No matches found') return { status: 'no_match', detail, raw };
  if (/^.+:[1-9]\d*: /m.test(detail)) return { status: 'matched', detail, raw };
  throw new Error(`Unrecognized Pi grep output: ${detail}`);
}

async function search(request) {
  // Pi can download ripgrep when absent. Use its local cache or PATH only.
  process.env.PI_OFFLINE = '1';
  const { createGrepTool, createGrepToolDefinition } = await loadPi();
  const factory = createGrepTool ?? createGrepToolDefinition;
  if (typeof factory !== 'function') throw new Error('Pi module does not export a grep-tool factory');
  const tool = factory(process.cwd());
  const args = grepArgs(request, tool);
  let raw;
  try {
    raw = await tool.execute('did-it-search', args);
  } catch (error) {
    if (!error?.message?.startsWith('Path not found: ')) throw error;
    const { name, message } = error;
    return { status: 'missing_path', detail: message, raw: { name, message } };
  }
  return normalize(raw);
}

const identify = process.argv[2] === '--identify';
try {
  if (identify) {
    process.stdout.write(JSON.stringify(identifyPi()) + '\n');
  } else {
    const result = await search(JSON.parse(readFileSync(0, 'utf8')));
    let output = JSON.stringify(result) + '\n';
    if (Buffer.byteLength(output) > MAX_STDOUT) {
      const { status, detail } = result;
      output = JSON.stringify({ status, detail }) + '\n';
    }
    if (Buffer.byteLength(output) > MAX_STDOUT) throw new Error('Pi grep text exceeds the 64 KiB runner limit');
    process.stdout.write(output);
  }
} catch (error) {
  process.stderr.write(`Pi adapter: ${identify ? 'SDK provenance: ' : ''}${error?.message ?? error}\n`);
  process.exitCode = 2;
}
