import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { chmod, mkdir, mkdtemp, readFile, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { test } from 'node:test';
import { setTimeout as delay } from 'node:timers/promises';
import { diagnose } from '../pi/diagnose.mjs';

const rg = process.platform === 'win32' ? 'rg.exe' : 'rg';
const input = { pattern: 'legacy_export', literal: true };
const native = !spawnSync(rg, ['--version']).error;
const nativeTest = (name, fn) => test(name, { skip: !native && 'native ripgrep is required' }, fn);

async function fixture(t, files) {
  const cwd = await mkdtemp(join(tmpdir(), 'didit-pi-'));
  t.after(() => rm(cwd, { force: true, recursive: true, maxRetries: 10, retryDelay: 25 }));
  for (const [name, contents] of Object.entries(files)) {
    const path = join(cwd, name);
    await mkdir(dirname(path), { recursive: true });
    await writeFile(path, contents);
  }
  return cwd;
}

function nativeMatches(cwd, args = {}) {
  const { pattern, path = '.', glob } = { ...input, ...args };
  const options = ['--no-config', '--hidden', '--json', '--line-number', '--color=never', '--fixed-strings'];
  if (glob) options.push('--glob', glob.replace(/^(!?)(?:\.\/)+/u, '$1'));
  const result = spawnSync(rg, [...options, '--', pattern, '.'], { cwd: join(cwd, path), encoding: 'utf8' });
  assert.ifError(result.error);
  assert.ok(result.status === 0 || result.status === 1, result.stderr);
  return result.stdout.split('\n').filter(Boolean).map(JSON.parse).filter((event) => event.type === 'match');
}

nativeTest('identifies the ignored caller and the exact native ignore rule', async (t) => {
  const cwd = await fixture(t, {
    '.git/info/exclude': '',
    '.gitignore': 'generated/\r\nnode_modules/\r\n',
    'generated/client.ts': 'legacy_export("private source text");\n',
    'node_modules/pkg/lib.js': 'unrelated dependency',
    'src/main.ts': 'repository source',
  });
  assert.equal(nativeMatches(cwd).length, 0);
  const diagnostic = await diagnose({ cwd, input });
  assert.match(diagnostic, /Skipped "generated\/client\.ts" by "generated\/" at \.gitignore:1/u);
  assert.doesNotMatch(diagnostic, /legacy_export|private source text|node_modules/u);
  const scoped = { ...input, glob: './generated/client.ts' };
  assert.equal(nativeMatches(cwd, scoped).length, 0);
  assert.match(await diagnose({ cwd, input: scoped }), /Skipped "generated\/client\.ts" by "generated\/" at \.gitignore:1/u);
});

nativeTest('routine ignored dependencies and caches alone produce no note', async (t) => {
  const cwd = await fixture(t, {
    '.ignore': 'node_modules/\n.venv/\n',
    'src/main.ts': 'ordinary source',
    'node_modules/pkg/index.js': 'package contents',
    '.venv/lib/module.py': 'environment contents',
  });
  assert.equal(await diagnose({ cwd, input }), null);
});

nativeTest('reports a grouped ignored directory from the search root without walking its descendants', async (t) => {
  const cwd = await fixture(t, {
    '.rgignore': '# generated artifacts\noutput/\n',
    'src/output/first.txt': 'legacy_export',
    'src/output/second.txt': 'legacy_export',
    'src/ordinary.txt': 'ordinary',
  });
  const scoped = { ...input, path: 'src' };
  assert.equal(nativeMatches(cwd, scoped).length, 0);
  assert.match(await diagnose({ cwd, input: scoped }), /Skipped "src\/output\/" by "output\/" at \.rgignore:2/u);
});

nativeTest('only reports the ignored file when a local rule location is ambiguous', async (t) => {
  const cwd = await fixture(t, {
    '.ignore': 'local.txt\nlocal.txt\n',
    'local.txt': input.pattern,
  });
  assert.equal(nativeMatches(cwd).length, 0);
  assert.match(await diagnose({ cwd, input }), /Skipped "local\.txt" by an ignore rule\./u);
});

nativeTest('honors ripgrep’s repository requirement and ignore precedence', async (t) => {
  const cwd = await fixture(t, {
    '.gitignore': 'local.txt\n',
    'local.txt': input.pattern,
    'ordinary.txt': 'ordinary',
  });
  assert.equal(nativeMatches(cwd).length, 1);
  assert.equal(await diagnose({ cwd, input: { ...input, pattern: 'absent' } }), null);
  await mkdir(join(cwd, '.git'));
  await writeFile(join(cwd, '.ignore'), '!local.txt\n');
  assert.equal(nativeMatches(cwd).length, 1);
  assert.equal(await diagnose({ cwd, input: { ...input, pattern: 'absent' } }), null);
  await writeFile(join(cwd, '.rgignore'), '# highest priority\nlocal.txt\n');
  assert.equal(nativeMatches(cwd).length, 0);
  assert.match(await diagnose({ cwd, input }), /Skipped "local\.txt" by "local\.txt" at \.rgignore:2/u);
});

nativeTest('keeps hidden paths and binary candidates eligible in the filename inventory', async (t) => {
  const cwd = await fixture(t, {
    '.hidden/ordinary.ts': 'ordinary',
    '.hidden/present.txt': input.pattern,
    'source.bin': '\0' + input.pattern,
  });
  assert.equal(nativeMatches(cwd, { glob: '*.txt' }).length, 1);
  assert.equal(nativeMatches(cwd, { glob: '*.ts' }).length, 0);
  assert.equal(await diagnose({ cwd, input: { ...input, glob: '*.ts' } }), null);
  assert.equal(await diagnose({ cwd, input: { ...input, glob: '*.bin' } }), null);
  assert.equal(await diagnose({ cwd, input: { ...input, pattern: 'ordinary miss' } }), null);
});

nativeTest('uses Pi’s directory-relative glob and distinguishes a glob miss from a pattern miss', async (t) => {
  const cwd = await fixture(t, {
    'repo/migrations/main/a.js': 'ordinary',
    'repo/nested/migrations/main/b.js': input.pattern,
  });
  const scoped = { ...input, path: './repo', glob: '././migrations/main/*.js' };
  assert.equal(nativeMatches(cwd, scoped).length, 0);
  assert.equal(await diagnose({ cwd, input: scoped }), null);
  const empty = { ...scoped, glob: './migrations/main/*.rs' };
  assert.equal(nativeMatches(cwd, empty).length, 0);
  assert.match(await diagnose({ cwd, input: empty }), /glob selected no files/u);
  assert.match(await diagnose({ cwd, input: { ...scoped, glob: '!././*.js' } }), /glob selected no files/u);
});

nativeTest('positive globs can override an ignore rule for files and directories', async (t) => {
  const cwd = await fixture(t, {
    '.ignore': 'private.ts\nhidden-output/\n',
    'private.ts': 'ordinary',
    'hidden-output/hidden-source.ts': 'ordinary',
  });
  for (const glob of ['private.ts', 'hidden*']) {
    assert.equal(nativeMatches(cwd, { glob, pattern: 'ordinary' }).length, 1, glob);
    assert.equal(nativeMatches(cwd, { glob }).length, 0);
    assert.equal(await diagnose({ cwd, input: { ...input, glob } }), null);
  }
});

nativeTest('ignores ambient ripgrep configuration and handles invalid globs quietly', async (t) => {
  const cwd = await fixture(t, {
    'rg-config': '--glob\n!*.ts\n',
    'main.ts': 'ordinary',
  });
  const previous = process.env.RIPGREP_CONFIG_PATH;
  process.env.RIPGREP_CONFIG_PATH = join(cwd, 'rg-config');
  t.after(() => {
    if (previous === undefined) delete process.env.RIPGREP_CONFIG_PATH;
    else process.env.RIPGREP_CONFIG_PATH = previous;
  });
  assert.equal(await diagnose({ cwd, input: { ...input, glob: '*.ts' } }), null);
  assert.equal(await diagnose({ cwd, input: { ...input, glob: '[' } }), null);
});

nativeTest('uses an absolute executable supplied by the hook when PATH cannot resolve ripgrep', async (t) => {
  const cwd = await fixture(t, { 'a.txt': 'ordinary' });
  const locator = process.platform === 'win32' ? ['where.exe', [rg]] : ['which', [rg]];
  const result = spawnSync(...locator, { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const rgPath = result.stdout.trim().split(/\r?\n/u)[0];
  const previous = process.env.PATH;
  process.env.PATH = cwd;
  t.after(() => { process.env.PATH = previous; });
  const empty = { ...input, glob: '*.rs' };
  assert.equal(await diagnose({ cwd, input: empty }), null);
  assert.equal(await diagnose({ cwd, input: empty, rgPath: rg }), null);
  assert.match(await diagnose({ cwd, input: empty, rgPath }), /glob selected no files/u);
});

test('declines unsupported input and a cancelled diagnostic', async (t) => {
  const cwd = await fixture(t, { 'a.txt': 'ordinary' });
  const supported = { ...input, glob: '*.rs' };
  for (const changes of [
    { literal: false }, { literal: undefined }, { ignoreCase: true }, { ignoreCase: 1 },
    { pattern: '' }, { pattern: 'x'.repeat(4097) }, { pattern: 'a\nb' },
    { path: 'a.txt' }, { path: '..' }, { path: '@.' }, { path: 'file:///tmp' },
    { path: '//host/share' }, { path: '\\\\host\\share' }, { path: 'C:relative' },
    { path: 'https://example.test/src' }, { glob: 'a\0b' }, { glob: 'x'.repeat(1025) },
  ]) {
    assert.equal(await diagnose({ cwd, input: { ...supported, ...changes } }), null, JSON.stringify(changes));
  }
  const abort = new AbortController();
  abort.abort();
  assert.equal(await diagnose({ cwd, input: supported, signal: abort.signal }), null);
  const pending = new AbortController();
  const diagnosis = diagnose({ cwd, input: supported, signal: pending.signal });
  pending.abort();
  assert.equal(await diagnosis, null);
});

nativeTest('stays quiet for symlinked directory roots and never lists link targets as skipped', async (t) => {
  const cwd = await fixture(t, {
    '.ignore': 'alias/\nignored-link\n',
    'actual/a.txt': 'ordinary',
  });
  try {
    await symlink(join(cwd, 'actual'), join(cwd, 'alias'), process.platform === 'win32' ? 'junction' : 'dir');
    await symlink(join(cwd, 'actual/a.txt'), join(cwd, 'ignored-link'), 'file');
  } catch (error) {
    if (error.code !== 'EPERM') throw error;
    t.skip('creating symlinks requires Windows Developer Mode');
    return;
  }
  assert.equal(await diagnose({ cwd, input: { ...input, path: 'alias', glob: '*.rs' } }), null);
  assert.equal(await diagnose({ cwd, input }), null);
});

nativeTest('stays quiet when ripgrep’s trace contains multiline filenames', async (t) => {
  if (process.platform === 'win32') return t.skip('Windows filenames cannot contain newlines');
  const cwd = await fixture(t, { '.ignore': 'bad*\n', 'bad\nname.txt': input.pattern });
  assert.equal(nativeMatches(cwd).length, 0);
  assert.equal(await diagnose({ cwd, input }), null);
});

async function instrumentedRg(t, emit) {
  const cwd = await fixture(t, {
    'rg.cjs': [
      '#!/usr/bin/env node',
      "const fs = require('node:fs');",
      "fs.appendFileSync('started', JSON.stringify({ pid: process.pid, args: process.argv.slice(2) }) + '\\n');",
      emit,
      'setInterval(() => {}, 1000);',
    ].join('\n'),
  });
  const rgPath = join(cwd, 'rg.cjs');
  await chmod(rgPath, 0o700);
  return { cwd, rgPath };
}

async function stopped(pid) {
  for (let i = 0; i < 30; i++) {
    try {
      process.kill(pid, 0);
    } catch (error) {
      if (error.code === 'ESRCH') return;
      throw error;
    }
    await delay(20);
  }
  assert.fail('The inventory process was still running');
}

test('bounds output and kills the one filename-only inventory', { skip: process.platform === 'win32' }, async (t) => {
  const options = await instrumentedRg(t, 'process.stdout.write(Buffer.alloc(512 * 1024, 120));');
  assert.equal(await diagnose({ ...options, input }), null);
  const [{ pid, args }] = (await readFile(join(options.cwd, 'started'), 'utf8')).trimEnd().split('\n').map(JSON.parse);
  assert.deepEqual(args, ['--no-config', '--hidden', '--files', '--null', '--debug', '--', '.']);
  await stopped(pid);
});

test('cancels an active inventory and times out a stalled one', { skip: process.platform === 'win32' }, async (t) => {
  const options = await instrumentedRg(t, '');
  const controller = new AbortController();
  const pending = diagnose({ ...options, input, signal: controller.signal });
  const log = join(options.cwd, 'started');
  let first;
  for (let i = 0; i < 50 && !first; i++) {
    try {
      first = JSON.parse((await readFile(log, 'utf8')).trimEnd());
    } catch (error) {
      if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error;
      await delay(20);
    }
  }
  controller.abort();
  assert.ok(first);
  assert.equal(await pending, null);
  await stopped(first.pid);

  assert.equal(await diagnose({ ...options, input }), null);
  const runs = (await readFile(log, 'utf8')).trimEnd().split('\n').map(JSON.parse);
  assert.equal(runs.length, 2);
  await stopped(runs[1].pid);
});
