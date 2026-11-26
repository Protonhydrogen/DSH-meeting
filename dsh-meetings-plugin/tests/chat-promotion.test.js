// Native composer turns → meeting note.
//
// Regression: a 纪要 requested in the embedded DSH conversation is not an
// analysed run, so its answer never reached the 纪要 tab. The host now watches
// the owned session's own turns and promotes the final Markdown.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { createSdkRoot } from './fixtures/canonical-sdk.js';

let implementation;
try { implementation = await import('../lib/agent.js'); } catch {}
const appRoot = createSdkRoot();
const evidence = { source_signatures: { a: '1234567890abcdef' }, source_revision: 1, provisional: false, summary_through_ms: 3000, notebook_revision: 0 };
const note = '# 这场报告讲什么\n\n' + '企业界的职业发展路径与选择标准。'.repeat(20) + '\n\n## 要点\n\n- 第一条\n- 第二条\n';
const tick = () => new Promise(r => setTimeout(r, 20));
const tempBase = resolve(process.env.DSH_MEETINGS_TEST_TMP || 'output/dsh_meetings_agent_tests');
mkdirSync(tempBase, { recursive: true });

function setup(t, options = {}) {
  assert.equal(typeof implementation?.createAgentController, 'function');
  const root = mkdtempSync(join(tempBase, 'chat-'));
  const guards = new Set(), listeners = new Set(), agents = new Map(), saved = [], snapshot = [];
  const ctx = {
    tools: { guard(fn) { guards.add(fn); return () => guards.delete(fn); } },
    on(name, fn) { assert.ok(['session/event', 'system-prompt/assemble'].includes(name)); if (name !== 'session/event') return () => {}; listeners.add(fn); return () => listeners.delete(fn); },
    get(name) { return services[name]; }, logger: { warn() {}, error() {} },
  };
  const emit = (id, type, data = {}, seq = 1) => { for (const fn of [...listeners]) fn({ id }, { type, data, seq }); };
  const services = {
    sessionController: {
      async create(args) {
        if (!agents.has(args.sessionId)) agents.set(args.sessionId, { session: { id: args.sessionId, seq: 0, snapshotEvents: () => snapshot }, status: 'idle', followup() { this.status = 'running'; }, cancel() { this.status = 'idle'; } });
        return { sessionId: args.sessionId };
      },
      async resolveAgent(id) { return agents.get(id); },
    },
    permissionPresets: { set() {} },
    attachments: { async admitPromptContent(parts) { return parts; } },
  };
  const opts = { stateDir: root, appRoot, contextProvider: async () => ({ prompt: 'Transcript', review_epoch: 3, title: 'Career talk', images: [], evidence }), saveResult: async (...args) => { saved.push(args); return { ok: true, note_id: 'note_chat' }; }, saveProfile: async () => ({ ok: true }), ...options };
  const controller = implementation.createAgentController(ctx, opts);
  t.after(async () => { await controller.dispose(); const p = resolve(root); if (!p.startsWith(tempBase)) throw Error('unsafe cleanup'); rmSync(p, { recursive: true, force: true }); });
  return { controller, emit, agents, saved, root, guards, listeners, snapshot };
}

async function chatTurn(f, id, { turn = 7, user = '', text = note, reason = { kind: 'completed' } } = {}) {
  f.emit(id, 'turn/start', { turn });
  if (user) f.emit(id, 'user/message', { content: [{ type: 'text', text: user }], source: { kind: 'user' } }, 2);
  f.emit(id, 'assistant/chunk', { chunk: { type: 'text-delta', text: text.slice(0, 40) }, turn }, 3);
  f.emit(id, 'assistant/message', { message: { content: [{ type: 'reasoning', text: 'hidden' }, { type: 'text', text }] }, turn }, 4);
  f.emit(id, 'turn/end', { turn, reason }, 5);
  for (let i = 0; i < 12 && !f.saved.length; i++) await tick();
  await tick();
}

test('a 纪要 asked for in the chat is saved into the meeting automatically', async t => {
  const f = setup(t);
  const bound = await f.controller.bind('meeting-a');
  await chatTurn(f, bound.agent_session_id, { user: '重新生成会议纪要，不要按照学术模板。此人主要讲企业界。' });
  assert.equal(f.saved.length, 1);
  assert.equal(f.saved[0][1].markdown, note.trim());
  assert.match(f.saved[0][1].run_id, /^chat-/);
  assert.equal(f.saved[0][1].review_epoch, 3);
  const status = f.controller.status('meeting-a');
  assert.equal(status.run.kind, 'summary');
  assert.equal(status.run.origin, 'chat');
  assert.equal(status.run.state, 'completed');
  assert.equal(status.run.saved, true);
  assert.equal(status.run.note_id, 'note_chat');
  assert.equal(status.chat.state, 'saved');
  assert.equal(status.analyses.summary.id, status.run.id);
});

test('an ordinary chat answer stays a draft until the user saves it', async t => {
  const f = setup(t);
  const bound = await f.controller.bind('meeting-a');
  await chatTurn(f, bound.agent_session_id, { user: '这段转折是什么意思？' });
  assert.equal(f.saved.length, 0);
  let status = f.controller.status('meeting-a');
  assert.equal(status.chat.state, 'pending');
  assert.equal(status.chat.chars, note.trim().length);
  assert.equal(status.run, null);
  status = await f.controller.saveChat('meeting-a', status.chat.turn);
  assert.equal(f.saved.length, 1);
  assert.equal(status.chat.state, 'saved');
  assert.equal(status.run.state, 'completed');
});

test('a short chat answer is not treated as a meeting note', async t => {
  const f = setup(t);
  const bound = await f.controller.bind('meeting-a');
  await chatTurn(f, bound.agent_session_id, { user: '生成会议纪要', text: '好的，我先读转录。' });
  assert.equal(f.saved.length, 0);
  assert.equal(f.controller.status('meeting-a').chat, null);
});

test('a failed save keeps the draft so it can be retried', async t => {
  const f = setup(t, { saveResult: async () => ({ ok: false, error: 'epoch changed' }) });
  const bound = await f.controller.bind('meeting-a');
  await chatTurn(f, bound.agent_session_id, { user: '帮我写会议纪要' });
  const status = f.controller.status('meeting-a');
  assert.equal(status.chat.state, 'pending');
  assert.match(status.chat.error, /epoch/);
  assert.equal(status.run.state, 'failed');
  await assert.rejects(f.controller.saveChat('meeting-a', status.chat.turn), /epoch/);
});

test('asking about the note does not replace it with a prose answer', async t => {
  const f = setup(t);
  const bound = await f.controller.bind('meeting-a');
  await chatTurn(f, bound.agent_session_id, { user: '纪要里提到哪些公司？', text: '纪要提到三家公司和两位猎头。' + '内容还在展开。'.repeat(60) });
  assert.equal(f.saved.length, 0);
  assert.equal(f.controller.status('meeting-a').chat.state, 'pending');
});

test('a note generated before a restart is recovered when the meeting is bound', async t => {
  const f = setup(t);
  f.snapshot.push(
    { type: 'turn/start', seq: 1, data: { turn: 11 } },
    { type: 'user/message', seq: 2, data: { content: [{ type: 'text', text: '重新生成会议纪要' }], source: { kind: 'user' } } },
    { type: 'user/message', seq: 3, data: { content: [{ type: 'text', text: 'Time sampled…' }], source: { kind: 'time-context' } } },
    { type: 'assistant/message', seq: 4, data: { message: { content: [{ type: 'reasoning', text: 'private' }, { type: 'text', text: note }] }, turn: 11 } },
    { type: 'turn/end', seq: 5, data: { turn: 11, reason: { kind: 'completed' } } },
  );
  const bound = await f.controller.bind('meeting-a');
  for (let i = 0; i < 12 && !f.saved.length; i++) await tick();
  await tick(); await tick();
  assert.equal(f.saved.length, 1);
  assert.equal(f.saved[0][1].markdown, note.trim());
  const status = f.controller.status('meeting-a');
  assert.equal(status.chat.state, 'saved');
  assert.equal(status.run.kind, 'summary');
  assert.equal(status.run.origin, 'chat');
  assert.equal(status.run.state, 'completed');
  assert.equal(bound.agent_session_id.startsWith('meetings-'), true);
});

test('a long profile answer is not recovered as a meeting note', async t => {
  const f = setup(t);
  f.snapshot.push(
    { type: 'turn/start', seq: 1, data: { turn: 12 } },
    { type: 'user/message', seq: 2, data: { content: [{ type: 'text', text: '根据人工提供的信息检索公开简介' }], source: { kind: 'user' } } },
    { type: 'assistant/message', seq: 3, data: { message: { content: [{ type: 'text', text: note }] }, turn: 12 } },
    { type: 'turn/end', seq: 4, data: { turn: 12, reason: { kind: 'completed' } } },
  );
  await f.controller.bind('meeting-a');
  await tick(); await tick();
  assert.equal(f.saved.length, 0);
  assert.equal(f.controller.status('meeting-a').chat, null);
});

test('a stale turn id refuses to save a superseded draft', async t => {
  const f = setup(t);
  const bound = await f.controller.bind('meeting-a');
  await chatTurn(f, bound.agent_session_id, { turn: 7, user: '这段讲了什么？' });
  await assert.rejects(f.controller.saveChat('meeting-a', 6), /更新/);
  assert.equal(f.saved.length, 0);
});
