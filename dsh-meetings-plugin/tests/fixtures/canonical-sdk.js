// Throw-away stand-in for the canonical ``@deepseek-ai/dsh-llm`` package.
//
// Native Meetings turns only need ``createUserMessage``, and the real package
// ships inside a DSH build (``<resources>/app.asar/dsh/node_modules``) which a
// plain Node test process cannot import. Every test that starts a run therefore
// publishes a minimal package root instead of depending on a machine-specific
// DSH installation.
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const PACKAGE = {
  name: '@deepseek-ai/dsh-llm',
  version: '0.0.0-test',
  type: 'module',
  main: 'lib/index.js',
  exports: { '.': './lib/index.js', './package.json': './package.json' },
};

const SOURCE = `let seq = 0;
export function createMessage(input) {
  seq += 1;
  return Object.freeze({ ...input, id: 'msg_test_' + seq });
}
export function createUserMessage(input) {
  return createMessage({ ...input, role: 'user' });
}
`;

/** Write ``node_modules/@deepseek-ai/dsh-llm`` below an existing root. */
export function writeSdkPackage(root) {
  const pkg = join(root, 'node_modules', '@deepseek-ai', 'dsh-llm');
  mkdirSync(join(pkg, 'lib'), { recursive: true });
  writeFileSync(join(pkg, 'package.json'), JSON.stringify(PACKAGE, null, 2));
  writeFileSync(join(pkg, 'lib', 'index.js'), SOURCE);
  return root;
}

/** Fresh temporary root that already contains the stand-in package. */
export function createSdkRoot() {
  return writeSdkPackage(mkdtempSync(join(tmpdir(), 'dsh-meetings-sdk-')));
}

/** Minimal scoped native tool SDK for the controller's permission tests. */
export function writeNativeToolSdk(root) {
  const modules = {
    'dsh-scope': 'export const scopeOf = ctx => ctx.agent;',
    'dsh-tool-executor-native': 'export class NativeToolExecutor { constructor(ctx) { this.ctx = ctx; } }',
  };
  for (const [name, source] of Object.entries(modules)) {
    const pkg = join(root, 'node_modules', '@deepseek-ai', name);
    mkdirSync(pkg, { recursive: true });
    writeFileSync(join(pkg, 'package.json'), JSON.stringify({ type:'module', main:'index.js' }));
    writeFileSync(join(pkg, 'index.js'), source);
  }
  return root;
}
