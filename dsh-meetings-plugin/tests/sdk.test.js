// Resolution of the canonical ``@deepseek-ai/dsh-llm`` package.
//
// Regression: the official DeepSeek Harness nests its runtime under
// ``<resources>/app.asar/dsh``, while a profile may still carry ``appRoot`` for
// a Desktop build that no longer exists. Resolving only
// ``<resources>/app.asar`` made every 纪要/简介 request fail with
// ``Canonical dsh-llm createUserMessage is unavailable``.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { writeSdkPackage } from './fixtures/canonical-sdk.js';

const { sdkRoots, loadCanonicalSdk } = await import('../lib/agent.js');
const LEGACY = 'C:/example/old-harness/resources/app';
delete process.env.DSH_MEETINGS_SDK_ROOT;

function withGlobals({ resourcesPath, argv }, run) {
  const previous = { resourcesPath: process.resourcesPath, argv: process.argv };
  if (resourcesPath === undefined) delete process.resourcesPath; else process.resourcesPath = resourcesPath;
  if (argv) process.argv = argv;
  return Promise.resolve().then(run).finally(() => {
    if (previous.resourcesPath === undefined) delete process.resourcesPath; else process.resourcesPath = previous.resourcesPath;
    process.argv = previous.argv;
  });
}

test('probes the nested runtime directory of the running app first', async () => {
  const resources = 'C:/example/test-harness/resources';
  await withGlobals({ resourcesPath: resources }, () => {
    const roots = sdkRoots(LEGACY);
    for (const expected of [join(resources, 'app.asar'), join(resources, 'app.asar', 'dsh'), join(resources, 'app', 'dsh'), LEGACY, join(LEGACY, 'dsh')]) {
      assert.ok(roots.includes(resolve(expected)), `${expected} should be probed`);
    }
    assert.ok(roots.indexOf(resolve(resources, 'app.asar', 'dsh')) < roots.indexOf(resolve(LEGACY)), 'the running app comes before the configured root');
    assert.equal(new Set(roots).size, roots.length, 'no duplicate roots');
  });
});

test('loads createUserMessage from the running app while appRoot is a removed build', async () => {
  const resources = mkdtempSync(join(tmpdir(), 'dsh-meetings-resources-'));
  writeSdkPackage(join(resources, 'app.asar', 'dsh'));
  await withGlobals({ resourcesPath: resources }, async () => {
    const sdk = await loadCanonicalSdk(LEGACY);
    assert.equal(typeof sdk?.createUserMessage, 'function');
    const message = sdk.createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: '纪要' }] });
    assert.equal(message.role, 'user');
    assert.deepEqual(message.content, [{ type: 'text', text: '纪要' }]);
  });
});

test('derives the runtime root from the host command line without resourcesPath', async () => {
  const root = mkdtempSync(join(tmpdir(), 'dsh-meetings-host-'));
  const runtime = join(root, 'app.asar', 'dsh');
  writeSdkPackage(runtime);
  const argv = ['harness.exe', '--expose-internals', join(runtime, 'node_modules', 'host', 'lib', 'index.js'), runtime, 'profile'];
  await withGlobals({ resourcesPath: undefined, argv }, async () => {
    assert.ok(sdkRoots(LEGACY).includes(resolve(runtime)));
    assert.equal(typeof (await loadCanonicalSdk(LEGACY))?.createUserMessage, 'function');
  });
});

test('reports no SDK instead of guessing when nothing exports createUserMessage', async () => {
  await withGlobals({ resourcesPath: undefined, argv: ['harness.exe'] }, async () => {
    assert.deepEqual(sdkRoots('C:/nowhere/app').includes(resolve('C:/nowhere/app')), true);
    assert.equal(await loadCanonicalSdk('C:/nowhere/app'), null);
  });
});

test('honours an explicit DSH_MEETINGS_SDK_ROOT override', async () => {
  const root = mkdtempSync(join(tmpdir(), 'dsh-meetings-override-'));
  writeSdkPackage(root);
  process.env.DSH_MEETINGS_SDK_ROOT = root;
  try {
    await withGlobals({ resourcesPath: undefined, argv: ['harness.exe'] }, async () => {
      assert.equal(resolve(sdkRoots(LEGACY)[0]), resolve(root));
      assert.equal(typeof (await loadCanonicalSdk(LEGACY))?.createUserMessage, 'function');
    });
  } finally { delete process.env.DSH_MEETINGS_SDK_ROOT; }
});
