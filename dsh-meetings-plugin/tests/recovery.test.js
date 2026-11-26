import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { createSdkRoot } from './fixtures/canonical-sdk.js';
let implementation;
try { implementation = await import('../lib/agent.js'); } catch {}
// Native turns read ``@deepseek-ai/dsh-llm`` from the app root; a packaged DSH
// build is not importable from plain Node, so publish a minimal SDK root.
const appRoot = createSdkRoot();
const evidence = { source_signatures: { a: '1234567890abcdef' }, source_revision: 1, provisional: false, summary_through_ms: 3000, notebook_revision: 0 };
const tempBase = resolve(process.env.DSH_MEETINGS_TEST_TMP || 'output/dsh_meetings_agent_tests');
mkdirSync(tempBase, { recursive: true });

function setup(t, options = {}) {
  assert.equal(typeof implementation?.createAgentController, 'function');
  const root = mkdtempSync(join(tempBase, 'recovery-'));
  const guards = new Set(), listeners = new Set(), agents = new Map(), created = [], saved = [], profileSaves = [];
  const ctx = {
    tools: { guard(fn) { guards.add(fn); return () => guards.delete(fn); } },
    on(name, fn) { assert.ok(['session/event', 'system-prompt/assemble'].includes(name)); if (name === 'session/event') listeners.add(fn); return () => listeners.delete(fn); },
    get(name) { return services[name]; }, logger: { warn() {}, error() {} },
  };
  const emit = (id, type, data = {}, seq = 1) => { for (const fn of [...listeners]) fn({ id }, { type, data, seq }); };
  const services = {
    sessionController: {
      async create(args) {
        created.push(args);
        if (!agents.has(args.sessionId)) agents.set(args.sessionId, {
          session: { id: args.sessionId }, status: 'idle', messages: [],
          followup(message) { this.status = 'running'; this.messages.push(message); },
          cancel() { this.cancelled = true; this.status = 'idle'; },
        });
        return { sessionId: args.sessionId };
      },
      async resolveAgent(id) { return agents.get(id); },
      selectModel() { throw Error('must never change default model'); },
    },
    permissionPresets: { set() {} },
    attachments: { async admitPromptContent(parts) { return parts; } },
  };
  const opts = { stateDir: root, appRoot, contextProvider: async () => ({ prompt: 'Transcript', review_epoch: 3, title: 'Science', images: [], evidence }), saveResult: async (...args) => { saved.push(args); return { ok: true, note_id: 'note_recovery' }; }, saveProfile: async (...args) => { profileSaves.push(args); return { ok: true }; }, ...options };
  const controller = implementation.createAgentController(ctx, opts);
  t.after(async () => { await controller.dispose(); const p = resolve(root); if (!p.startsWith(tempBase + '\\') && !p.startsWith(tempBase + '/')) throw Error('unsafe cleanup'); rmSync(p, { recursive: true, force: true }); });
  return { controller, ctx, opts, root, guards, listeners, agents, created, saved, profileSaves, emit };
}

// The host reports turns through a best-effort observer stream; when the closing
// events of a turn never arrive the plugin must still promote the finished message,
// otherwise the run stays "running" and every later request is refused as busy.
function withDurableLog(f, text) {
  const create = f.ctx.get('sessionController').create;
  f.ctx.get('sessionController').create = async args => {
    const result = await create(args);
    const agent = f.agents.get(args.sessionId);
    agent.log = [];
    agent.idle = new Promise(done => { agent.settle = done; });
    agent.whenIdle = function () { return this.idle; };
    agent.session = { id: args.sessionId, seq: 0, snapshotEvents: () => agent.log };
    return result;
  };
  return async id => {
    const agent = f.agents.get(id);
    agent.log.push({ seq: 1, type: 'turn/start', data: { turn: 1 } });
    agent.log.push({ seq: 2, type: 'assistant/message', data: { turn: 1, message: { content: [{ type: 'text', text }] } } });
    agent.log.push({ seq: 3, type: 'turn/end', data: { turn: 1, reason: { kind: 'completed' } } });
    agent.status = 'idle';
    agent.settle();
  };
}

test('a dropped turn/end still promotes the finished summary from the durable session log', async t => {
  const f = setup(t);
  const complete = withDurableLog(f, 'Recovered summary body');
  const started = await f.controller.start({ session_id: 'dropped_summary', kind: 'summary' });
  await complete(started.agent_session_id);
  for (let i = 0; i < 40 && !f.saved.length; i++) await new Promise(r => setTimeout(r, 20));
  assert.equal(f.saved.length, 1, 'the finished turn was never promoted');
  assert.match(f.saved[0][1].markdown, /Recovered summary body/);
  for (let i = 0; i < 40 && f.controller.status('dropped_summary').run.state === 'saving'; i++) await new Promise(r => setTimeout(r, 20));
  const run = f.controller.status('dropped_summary').run;
  assert.equal(run.state, 'completed');
  assert.equal(run.saved, true);
});

test('a live run whose owner provably stopped is retired instead of blocking the meeting', async t => {
  const f = setup(t);
  const started = await f.controller.start({ session_id: 'stale_run', kind: 'summary' });
  const id = started.agent_session_id;
  f.emit(id, 'turn/start', { turn: 1 });
  f.agents.get(id).status = 'idle';   // the turn is over, but turn/end never arrived
  const reopened = implementation.createAgentController(f.ctx, f.opts);
  try {
    assert.equal(reopened.status('stale_run').run.state, 'running');
    await new Promise(r => setTimeout(r, 60));
    assert.equal(reopened.status('stale_run').run.state, 'interrupted');
    const next = await reopened.start({ session_id: 'stale_run', kind: 'summary' });
    assert.equal(next.run.state, 'running');
    assert.equal(next.agent_session_id, id);
  } finally { await reopened.dispose(); }
});
