// Real Python control plane + mocked Pi UI/model. No model calls or live workers.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, statSync, writeFileSync, rmSync, openSync, closeSync, existsSync, cpSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, resolve, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const sourceRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const tmp = mkdtempSync(join(tmpdir(), 'mate-extension-'));
const root = join(tmp, 'mate');
// Run the real extension/control plane from a disposable installation, never personal config.
for (const path of ['.pi/extensions', 'bin', 'SUPERVISOR.md', 'WORKER.md']) {
  cpSync(join(sourceRoot, path), join(root, path), { recursive: true });
}
cpSync(join(sourceRoot, 'mate.config.example.json'), join(root, 'mate.config.json'));
const installed = join(execFileSync('npm', ['root', '-g'], { encoding: 'utf8' }).trim(), '@earendil-works/pi-coding-agent');
const { createJiti } = await import(pathToFileURL(join(installed, 'node_modules/jiti/lib/jiti.mjs')).href);
const jiti = createJiti(import.meta.url, { alias: {
  typebox: join(installed, 'node_modules/typebox/build/index.mjs'),
  '@earendil-works/pi-ai': join(installed, 'node_modules/@earendil-works/pi-ai/dist/index.js'),
  '@earendil-works/pi-tui': join(installed, 'node_modules/@earendil-works/pi-tui/dist/index.js'),
} });
const { default: factory, workerProfile, dispatchProfile } = await jiti.import(join(root, '.pi/extensions/mate-supervisor.ts'));
process.env.MATE_HOME = join(tmp, 'home');
const repo = join(tmp, 'repo'); mkdirSync(repo);
const git = (...args) => execFileSync('git', ['-C', repo, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
git('init', '-b', 'main'); git('-c', 'user.name=Mate Test', '-c', 'user.email=mate@test.invalid', 'commit', '--allow-empty', '-m', 'base');
const handlers = {}, tools = {}, commands = {}, renderers = {}, messages = [], notices = [], statuses = {};
let active = ['read', 'write', 'bash', 'external_tool'], approval = false, closeApproval = false, expanded = false;
const models = [
  { provider: 'openai-codex', id: 'main-model', reasoning: true },
  { provider: 'openai-codex', id: 'worker-model', reasoning: true, thinkingLevelMap: { xhigh: 'xhigh' } },
  { provider: 'other', id: 'vendor/model', reasoning: false },
];
let supervisorIdle = true, supervisorQueued = false;
const ctx = { mode: 'tui', hasUI: true, model: models[0], thinkingLevel: 'high',
  isIdle: () => supervisorIdle, hasPendingMessages: () => supervisorQueued,
  modelRegistry: { find: (provider, id) => models.find(m => m.provider === provider && m.id === id) },
  ui: { notify: (...args) => notices.push(args), setStatus(key, value) { statuses[key] = value; }, confirm: async title => title === 'Close worker Herdr tab too?' ? closeApproval : approval,
    getToolsExpanded: () => expanded, setToolsExpanded: value => { expanded = value; } } };
const pi = {
  on(name, fn) { handlers[name] = fn; },
  registerTool(tool) { tools[tool.name] = tool; },
  registerCommand(name, command) { commands[name] = command; },
  registerMessageRenderer(name, renderer) { renderers[name] = renderer; },
  setActiveTools(names) { active = names; },
  sendMessage(message, options) { messages.push({ message, options }); },
};
const previousMode = process.env.MATE_MODE;
process.env.MATE_MODE = 'dev';
factory(new Proxy({}, { get(_target, key) { throw new Error(`Dev mode touched Pi API: ${String(key)}`); } }));
assert.equal(existsSync(process.env.MATE_HOME), false, 'dev mode creates no runtime state');
assert.deepEqual(active, ['read', 'write', 'bash', 'external_tool'], 'dev mode leaves tools untouched');
delete process.env.MATE_MODE;
factory(pi);
assert.equal(handlers.before_agent_start({ systemPrompt: 'base' }).systemPrompt,
  'base\n\n' + readFileSync(join(root, 'SUPERVISOR.md'), 'utf8'), 'runtime injects only supervisor policy');
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
async function wait(check) {
  let last;
  for (let i = 0; i < 60; i++) {
    try { const value = await check(); if (value) return value; } catch (e) { last = e; }
    await sleep(100);
  }
  throw last ?? new Error('Timed out');
}
const call = async (name, params = {}) => JSON.parse((await tools[name].execute('test', params)).content[0].text);
try {
  const { default: bridge } = await jiti.import(join(root, 'bin/worker-events.ts'));
  const bridgeHandlers = {}, bridgeFile = join(tmp, 'bridge.jsonl');
  const fd = openSync(bridgeFile, 'w', 0o600);
  const priorDescriptor = process.env.MATE_EVENT_FD;
  let shutdowns = 0, aborted = false, idle = false, queued = false;
  const bridgeCtx = { ui: { notify() {} }, isIdle: () => idle, hasPendingMessages: () => queued,
    shutdown: () => { shutdowns++; }, abort: () => { aborted = true; } };
  try {
    process.env.MATE_EVENT_FD = String(fd);
    bridge({ on: (name, handler) => { bridgeHandlers[name] = handler; } });
    assert.equal(bridgeHandlers.agent_end, undefined, 'do not terminate retries at low-level agent_end');
    bridgeHandlers.agent_settled({ type: 'agent_settled' }, bridgeCtx);
    assert.equal(shutdowns, 0, 'ignore unsettled agent');
    idle = true; queued = true;
    bridgeHandlers.agent_settled({ type: 'agent_settled' }, bridgeCtx);
    assert.equal(shutdowns, 0, 'queued messages must run first');
    queued = false;
    bridgeHandlers.message_end({ type: 'message_end', message: { role: 'assistant', stopReason: 'stop' } }, bridgeCtx);
    bridgeHandlers.agent_settled({ type: 'agent_settled' }, bridgeCtx);
    assert.equal(shutdowns, 1);
    assert.equal(JSON.parse(readFileSync(bridgeFile, 'utf8').trim().split('\n').at(-1)).type, 'agent_settled');
    bridgeHandlers.message_update({ text: 'x'.repeat(4 * 1024 * 1024) }, bridgeCtx);
    assert.equal(aborted, true, 'bridge failure must abort instead of silently losing reports');
    assert.equal(process.exitCode, 1);
    process.exitCode = 0;
  } finally {
    closeSync(fd);
    if (priorDescriptor === undefined) delete process.env.MATE_EVENT_FD;
    else process.env.MATE_EVENT_FD = priorDescriptor;
  }
  assert.deepEqual(workerProfile(ctx, {}), { provider: 'openai-codex', model: 'main-model', effort: 'high' });
  assert.deepEqual(workerProfile(ctx, { model: 'worker-model', effort: 'xhigh' }), { provider: 'openai-codex', model: 'worker-model', effort: 'xhigh' });
  assert.deepEqual(workerProfile(ctx, { effort: 'low' }), { provider: 'openai-codex', model: 'main-model', effort: 'low' });
  assert.deepEqual(workerProfile(ctx, { model: 'other/vendor/model' }), { provider: 'other', model: 'vendor/model', effort: 'off' });
  assert.deepEqual(workerProfile(ctx, {}, { provider: 'openai-codex', model: 'worker-model', effort: 'low' }), { provider: 'openai-codex', model: 'worker-model', effort: 'low' });
  assert.throws(() => workerProfile(ctx, { model: 'unknown' }), /Unknown model/);
  assert.throws(() => workerProfile(ctx, { effort: 'max' }), /unsupported/);
  assert.throws(() => workerProfile(ctx, { model: 'other/vendor/model', effort: 'high' }), /unsupported/);
  const configPath = join(tmp, 'mate.config.json');
  const configure = data => writeFileSync(configPath, JSON.stringify(data));
  configure({ worker: { model: 'openai-codex/worker-model', effort: 'xhigh' } });
  assert.deepEqual(dispatchProfile(ctx, {}, configPath), { provider: 'openai-codex', model: 'worker-model', effort: 'xhigh' });
  assert.deepEqual(dispatchProfile(ctx, { effort: 'low' }, configPath), { provider: 'openai-codex', model: 'worker-model', effort: 'low' });
  assert.deepEqual(dispatchProfile(ctx, { model: 'main-model', effort: 'high' }, configPath), { provider: 'openai-codex', model: 'main-model', effort: 'high' });
  assert.throws(() => dispatchProfile(ctx, { model: 'other/vendor/model' }, configPath), /unsupported/, 'configured effort must not silently downgrade');
  configure({ worker: { effort: 'low' } });
  assert.deepEqual(dispatchProfile(ctx, {}, configPath), { provider: 'openai-codex', model: 'main-model', effort: 'low' }, 'config reread without reload');
  configure({ projects: { fixture: { repo: '/fixture', base_branch: 'main', startup: { command: ['true'] } } } });
  assert.deepEqual(dispatchProfile(ctx, {}, configPath), workerProfile(ctx, {}), 'projects do not alter worker defaults');
  configure({});
  assert.deepEqual(dispatchProfile(ctx, {}, configPath), workerProfile(ctx, {}));
  for (const invalid of [null, [], { workers: {} }, { worker: null }, { worker: [] }, { projects: null }, { projects: [] },
    { worker: { model: 1 } }, { worker: { model: ' ' } }, { worker: { effort: 'ultra' } }, { worker: { typo: true } }]) {
    configure(invalid);
    assert.throws(() => dispatchProfile(ctx, {}, configPath), /Invalid/);
  }
  configure({ worker: { model: 'unknown' } });
  assert.throws(() => dispatchProfile(ctx, {}, configPath), /Unknown model/);
  writeFileSync(configPath, '{');
  assert.throws(() => dispatchProfile(ctx, {}, configPath), /Cannot read/);
  rmSync(configPath);
  assert.throws(() => dispatchProfile(ctx, {}, configPath), /Cannot read/);
  // Check the copied example config with a mock catalog; no personal config is read.
  const luna = { provider: 'openai-codex', id: 'gpt-5.6-luna', reasoning: true, thinkingLevelMap: { xhigh: 'xhigh' } };
  assert.deepEqual(dispatchProfile({ ...ctx, modelRegistry: { find: (provider, id) =>
    provider === luna.provider && id === luna.id ? luna : undefined } }, {}),
    { provider: 'openai-codex', model: 'gpt-5.6-luna', effort: 'xhigh' });
  cpSync(join(sourceRoot, 'mate.config.example.json'), join(root, 'mate.config.example.json'));
  rmSync(join(root, 'mate.config.json'));
  assert.throws(() => dispatchProfile(ctx, {}), /Cannot read/, 'missing local config must not fall back to example');
  cpSync(join(root, 'mate.config.example.json'), join(root, 'mate.config.json'));
  await handlers.session_start({}, ctx);
  await wait(() => call('mate_status'));

  // Calm is presentation only; existing components redraw without changing payloads.
  const theme = { fg: (_color, text) => text };
  const output = data => ({ content: [{ type: 'text', text: JSON.stringify(data) }] });
  const row = (name, result, isError = false, isPartial = false) => {
    const context = { state: {}, isError };
    const call = tools[name].renderCall({ id: 'inspect' }, theme, context);
    assert.ok(call.render(100).length, 'pending calls stay visible');
    const rendered = tools[name].renderResult(result, { expanded: true, isPartial }, theme, context);
    return () => [...call.render(100), ...rendered.render(100)];
  };
  const ack = output({ acknowledged: [1] }), unchanged = structuredClone(ack);
  const ackRow = row('mate_ack', ack);
  assert.ok(ackRow().length, 'default off');
  await commands.calm.handler('on', ctx);
  assert.equal(readFileSync(join(process.env.MATE_HOME, 'calm'), 'utf8'), 'on\n');
  assert.equal(statSync(join(process.env.MATE_HOME, 'calm')).mode & 0o777, 0o600);
  assert.equal(expanded, false, 'toggle preserves expansion preference');
  assert.deepEqual(ackRow(), []);
  assert.deepEqual(ack, unchanged, 'model payload is unchanged');
  assert.ok(row('mate_ack', ack, true)().length, 'errors cannot disappear');
  assert.ok(row('mate_ack', ack, false, true)().length, 'partial results cannot disappear');
  for (const [name, data] of [
    ['mate_propose', { state: 'awaiting-base' }],
    ['mate_dispatch', { state: 'attention', error: 'uncertain launch' }],
    ['mate_continue', { state: 'failed' }],
    ['mate_status', { tasks: [], events: [], report: { text: 'Need your answer?' } }],
    ...['blocked', 'failed', 'stalled', 'report'].map(kind => ['mate_status', { tasks: [], events: [{ kind }] }]),
    ['mate_status', { tasks: [{ state: 'review' }], events: [] }],
    ['mate_ack', { unexpected: true }],
  ]) assert.ok(row(name, output(data))().length, JSON.stringify(data));
  assert.ok(row('mate_ack', { content: [{ type: 'text', text: 'malformed' }] })().length);
  assert.ok(row('mate_ack', { content: [{ type: 'image', data: 'fixture' }] })().length);
  assert.deepEqual(row('mate_status', output({ tasks: [{ state: 'running' }], events: [] }))(), []);
  assert.deepEqual(row('mate_dispatch', output({ state: 'launching' }))(), []);
  assert.deepEqual(row('mate_continue', output({ state: 'running' }))(), []);
  const approved = { content: 'Approved inspect', details: {} };
  const approvedRow = renderers['mate-approved'](approved, {}, theme);
  assert.deepEqual(approvedRow.render(100), []);
  const wake = { content: 'Full report wake instructions', details: { events: [{ id: 1, task: 'inspect', kind: 'report' }] } };
  const originalWake = structuredClone(wake);
  const wakeRow = renderers['mate-wake'](wake, {}, theme);
  assert.match(wakeRow.render(100).join('\n'), /report ready \(not verified\)/);
  assert.deepEqual(wake, originalWake);
  for (const details of [undefined, { events: [{ kind: 'blocked' }] },
    { events: [...wake.details.events, { kind: 'failed' }] }, { events: [{ kind: 'unknown' }] }, { events: { length: 1 } }]) {
    assert.match(renderers['mate-wake']({ ...wake, details }, {}, theme).render(100).join('\n'), /Full report wake instructions/);
  }
  await commands.calm.handler('off', ctx);
  assert.ok(ackRow().length, 'existing hidden rows restore');
  assert.match(approvedRow.render(100).join('\n'), /Approved inspect/);
  assert.match(wakeRow.render(100).join('\n'), /Full report wake instructions/);
  rmSync(join(process.env.MATE_HOME, 'calm'));
  mkdirSync(join(process.env.MATE_HOME, 'calm')); // Force rename failure, not permission-dependent.
  await commands.calm.handler('on', ctx);
  assert.equal(notices.at(-1)[1], 'error');
  assert.ok(ackRow().length, 'failed save leaves live preference unchanged');
  rmSync(join(process.env.MATE_HOME, 'calm'), { recursive: true });
  await commands.calm.handler('invalid', ctx);
  assert.equal(notices.at(-1)[1], 'warning');
  await commands.calm.handler('', ctx);
  assert.deepEqual(ackRow(), [], 'empty command toggles');
  // A session reload re-reads the persistent home preference.
  writeFileSync(join(process.env.MATE_HOME, 'calm'), 'off\n');
  await handlers.session_shutdown();
  await handlers.session_start({}, ctx);
  assert.ok(ackRow().length, 'persisted off restored');
  await commands.calm.handler('on', ctx);
  await wait(() => call('mate_status'));
  assert.deepEqual(active.sort(), ['mate_ack', 'mate_continue', 'mate_dispatch', 'mate_extend', 'mate_propose', 'mate_status']);
  assert.equal(handlers.tool_call({ toolName: 'bash' }).block, true);
  assert.equal(handlers.tool_call({ toolName: 'read' }).block, true);
  assert.equal(handlers.tool_call({ toolName: 'external_tool' }).block, true);
  assert.match(tools.mate_continue.description, /unstarted continuation/);
  assert.match(tools.mate_continue.description, /No worker lock after 60s/);
  assert.match(tools.mate_continue.description, /Never force/);
  await call('mate_propose', { id: 'inspect', repo, base: 'main', brief: 'Read-only fixture investigation.' });
  await commands['mate-approve'].handler('inspect', ctx);
  assert.equal((await call('mate_status')).tasks[0].state, 'awaiting-base');
  approval = true;
  await commands['mate-approve'].handler('inspect', ctx);
  assert.equal((await call('mate_status')).tasks[0].state, 'approved');
  assert.equal(messages.filter(m => m.message.customType === 'mate-approved').length, 1);
  execFileSync('python3', ['-c', `import sqlite3,os\nc=sqlite3.connect(os.path.join(os.environ['MATE_HOME'],'mate.sqlite3'))\nc.execute("INSERT INTO events(task,attempt,kind,note) VALUES ('inspect',0,'test','fixture outcome')")\nc.commit()`]);
  await wait(() => messages.find(m => m.message.customType === 'mate-wake'));
  assert.equal(messages.find(m => m.message.customType === 'mate-wake').options.deliverAs, 'followUp');
  const count = messages.length;
  await sleep(2300);
  assert.equal(messages.length, count, 'no duplicate notification in one generation');
  await handlers.session_shutdown();
  await handlers.session_start({}, ctx);
  await wait(() => messages.length > count);
  assert.equal(messages.at(-1).message.customType, 'mate-wake', 'unacked event replays after restart');
  assert.deepEqual(ackRow(), [], 'persisted calm on survives restart');
  assert.equal(messages.at(-1).message.details.events[0].kind, 'test');
  await call('mate_ack', { events: [1], note: 'Relayed fixture outcome' });
  assert.equal((await call('mate_status')).events.length, 0);
  assert.equal(tools.mate_close_tab, undefined, 'tab closure is not a model tool');
  assert.equal(handlers.tool_call({ toolName: 'mate_close_tab' }).block, true);
  assert.equal(tools.mate_complete, undefined, 'completion is not a model tool');
  assert.equal(handlers.tool_call({ toolName: 'mate_complete' }).block, true);
  assert.equal(tools.mate_cancel, undefined, 'cancellation is not a model tool');
  assert.equal(tools.mate_inspect_cancel, undefined, 'cancellation preflight is not a model tool');
  assert.equal(handlers.tool_call({ toolName: 'mate_cancel' }).block, true);
  assert.equal(typeof commands['mate-cancel'].handler, 'function');
  await commands['mate-complete'].handler('inspect', ctx);
  assert.equal((await call('mate_status')).tasks[0].state, 'approved', 'cannot complete before review');
  execFileSync('python3', ['-c', `import sqlite3,os,json\np=os.environ['MATE_HOME']\nc=sqlite3.connect(os.path.join(p,'mate.sqlite3'))\nt=json.loads(c.execute("SELECT data FROM tasks WHERE id='inspect'").fetchone()[0])\nt.update(state='review',attempt=1)\nc.execute("UPDATE tasks SET data=? WHERE id='inspect'",(json.dumps(t),))\nc.commit()\nos.makedirs(os.path.join(p,'inspect'),exist_ok=True)`]);
  assert.equal(tools.mate_review_scope, undefined, 'scope approval is not a model tool');
  assert.equal(handlers.tool_call({ toolName: 'mate_review_scope' }).block, true);
  assert.equal(handlers.tool_call({ toolName: 'mate_extend' }), undefined);
  const initialScope = (await call('mate_status', { id: 'inspect' })).tasks[0].brief;
  const scopeParams = { id: 'inspect', brief: 'Also check accessibility.' };
  await call('mate_extend', scopeParams);
  await commands['mate-approve'].handler('inspect', { ...ctx, mode: 'rpc' });
  assert.ok((await call('mate_status', { id: 'inspect' })).tasks[0].pending_scope, 'TUI only');
  approval = false;
  await commands['mate-approve'].handler('inspect', ctx);
  let scoped = (await call('mate_status', { id: 'inspect' })).tasks[0];
  assert.equal(scoped.pending_scope, undefined, 'decline discards pending addition');
  assert.equal(scoped.brief, initialScope);
  await call('mate_extend', scopeParams);
  // Replace the proposal while its confirmation is open; accepting old text must fail.
  await commands['mate-approve'].handler('inspect', { ...ctx, ui: { ...ctx.ui, confirm: async () => {
    await call('mate_extend', { ...scopeParams, brief: 'Revised accessibility checks.' });
    return true;
  } } });
  assert.equal(notices.at(-1)[1], 'error');
  assert.equal((await call('mate_status', { id: 'inspect' })).tasks[0].brief, initialScope);
  let dialog;
  await commands['mate-approve'].handler('inspect', { ...ctx, ui: { ...ctx.ui, confirm: async (title, body) => {
    dialog = title + '\n' + body; return true;
  } } });
  scoped = (await call('mate_status', { id: 'inspect' })).tasks[0];
  assert.match(dialog, /Approve additional task scope/);
  assert.ok(dialog.includes(initialScope) && dialog.includes(scoped.sha));
  assert.match(dialog, /Revised accessibility checks/);
  assert.equal(scoped.state, 'review', 'approval does not dispatch or launch');
  assert.equal(scoped.scope_history.length, 1);
  assert.equal(scoped.original_brief, initialScope);
  const approvalEvent = (await call('mate_status')).events.find(e => e.kind.startsWith('scope-approved-'));
  assert.ok(approvalEvent);
  await wait(() => messages.some(m => m.message.details?.events.some(e => e.id === approvalEvent.id)));
  assert.match(messages.at(-1).message.content, /call mate_continue in this turn/);
  // Reproduce the incident: two report wakes arrive, model repeats its old launch reply.
  execFileSync('python3', ['-c', `import sqlite3,os\nc=sqlite3.connect(os.path.join(os.environ['MATE_HOME'],'mate.sqlite3'))\nc.executemany("INSERT INTO events(task,attempt,kind,note) VALUES (?,1,'report','fixture report ready')", [('inspect',),('second',)])\nc.commit()`]);
  await wait(() => messages.some(m => m.message.details?.events.some(e => e.task === 'second')));
  assert.match(messages.at(-1).message.content, /A report supersedes an old launching update/);
  const beforeCorrection = messages.length;
  assert.equal(handlers.agent_end, undefined, 'no correction during low-level retry/compaction');
  supervisorIdle = false;
  handlers.agent_settled({}, ctx);
  supervisorIdle = true; supervisorQueued = true;
  handlers.agent_settled({}, ctx);
  await sleep(2300);
  assert.equal(messages.length, beforeCorrection, 'busy/queued runs are not corrected');
  supervisorQueued = false;
  handlers.agent_settled({}, ctx);
  await wait(() => messages.length > beforeCorrection);
  assert.match(messages.at(-1).message.content, /CORRECTION:/);
  assert.equal(messages.at(-1).options.deliverAs, 'followUp');
  assert.equal(messages.at(-1).message.details.events.length, 3, 'both reports and approval retained');
  let correctedCount = messages.length;
  handlers.agent_settled({}, ctx);
  await sleep(2300);
  assert.equal(messages.length, correctedCount, 'only one corrective turn per event');
  assert.match(statuses['mate-unhandled'], /UNHANDLED:.*inspect.*second/);
  assert.match(messages.at(-1).message.content, /do not launch again/);
  assert.match(messages.at(-1).message.content, /do not retry without resolving/);
  await handlers.session_shutdown();
  handlers.agent_settled({}, ctx); // A stale callback must not re-arm the old generation.
  await sleep(100);
  assert.equal(messages.length, correctedCount);
  await handlers.session_start({}, ctx);
  await wait(() => messages.length > correctedCount);
  assert.doesNotMatch(messages.at(-1).message.content, /CORRECTION:/, 'restart first replays normally');
  correctedCount = messages.length;
  handlers.agent_settled({}, ctx);
  await wait(() => messages.length > correctedCount);
  assert.match(messages.at(-1).message.content, /CORRECTION:/, 'new generation has one reminder budget');
  correctedCount = messages.length;
  await call('mate_ack', { events: (await call('mate_status')).events.map(e => e.id), note: 'Relayed disposable fixture outcomes and scope blocker; no launch.' });
  handlers.agent_settled({}, ctx);
  await wait(() => statuses['mate-unhandled'] === undefined);
  assert.equal(messages.length, correctedCount, 'ack suppresses correction');
  await commands['mate-complete'].handler('inspect', ctx);
  assert.equal(notices.at(-1)[1], 'error', 'cannot accept an unexecuted scope addition');
  // Simulate a finished continuation in this disposable fixture, without Herdr or a model.
  execFileSync('python3', ['-c', `import sqlite3,os,json\nc=sqlite3.connect(os.path.join(os.environ['MATE_HOME'],'mate.sqlite3'))\nt=json.loads(c.execute("SELECT data FROM tasks WHERE id='inspect'").fetchone()[0])\nt['attempt']=2\nc.execute("UPDATE tasks SET data=? WHERE id='inspect'",(json.dumps(t),))\nc.commit()`]);
  await commands['mate-complete'].handler('inspect', { ...ctx, mode: 'rpc' });
  assert.equal((await call('mate_status')).tasks[0].state, 'review', 'TUI only');
  for (const args of ['', '--force', 'inspect --oops', 'inspect --force extra']) {
    await commands['mate-complete'].handler(args, ctx);
    assert.match(notices.at(-1)[0], /Usage:/);
  }
  execFileSync('python3', ['-c', `import sqlite3,os,json\nc=sqlite3.connect(os.path.join(os.environ['MATE_HOME'],'mate.sqlite3'))\nt=json.loads(c.execute("SELECT data FROM tasks WHERE id='inspect'").fetchone()[0])\nt.update(state='failed',error='Pi exit=1, settled=False')\nc.execute("UPDATE tasks SET data=? WHERE id='inspect'",(json.dumps(t),))\nc.commit()`]);
  await commands['mate-complete'].handler('inspect', ctx);
  assert.match(notices.at(-1)[0], /stopped failed tasks with --force/);
  await commands['mate-complete'].handler('inspect --force', { ...ctx, mode: 'rpc' });
  assert.match(notices.at(-1)[0], /Human TUI confirmation/);
  const forceCtx = { ...ctx, ui: { ...ctx.ui, confirm: async (title, body) => {
    assert.equal(title, 'Force accept task as complete?');
    assert.match(body, /FORCE ACCEPTANCE from failed/);
    assert.match(body, /Pi exit=1, settled=False/);
    return approval;
  } } };
  approval = false;
  await commands['mate-complete'].handler('inspect --force', forceCtx);
  assert.match(notices.at(-1)[0], /task remains failed/);
  approval = true;
  await commands['mate-complete'].handler('inspect --force', forceCtx);
  assert.match(notices.at(-1)[0], /'pane'/, 'force reaches backend but refuses this fixture without an endpoint');
  assert.equal((await call('mate_status')).tasks[0].state, 'failed');
  execFileSync('python3', ['-c', `import sqlite3,os,json\nc=sqlite3.connect(os.path.join(os.environ['MATE_HOME'],'mate.sqlite3'))\nt=json.loads(c.execute("SELECT data FROM tasks WHERE id='inspect'").fetchone()[0])\nt['state']='review'\nc.execute("UPDATE tasks SET data=? WHERE id='inspect'",(json.dumps(t),))\nc.commit()`]);
  approval = false;
  await commands['mate-complete'].handler('inspect', ctx);
  assert.equal((await call('mate_status')).tasks[0].state, 'review', 'decline preserves review');
  await wait(() => statuses.mate?.startsWith('1 open tasks ·'));
  approval = true;
  await commands['mate-complete'].handler('inspect', ctx);
  await wait(() => statuses.mate?.startsWith('0 open tasks ·'));
  assert.equal((await call('mate_status')).total_tasks, 1, 'completed history retained');
  const completed = (await call('mate_status')).tasks[0];
  assert.equal(completed.state, 'complete');
  await assert.rejects(() => call('mate_extend', scopeParams), /review\/failed/);
  assert.ok(completed.completed_by);
  const completionMessage = messages.find(m => m.message.customType === 'mate-completed');
  assert.equal(completionMessage.message.display, true);
  assert.equal(completionMessage.options.triggerTurn, false);
  assert.equal(renderers['mate-completed'], undefined, 'Calm must not hide human acceptance');
  await commands['mate-complete'].handler('inspect', ctx);
  assert.equal(messages.filter(m => m.message.customType === 'mate-completed').length, 1);
  assert.equal((await call('mate_status')).tasks[0].tab_close_state, undefined, 'declined closure retains tab');
  // This fixture has no actual Herdr endpoint: accept closure and verify its failure
  // is visible without undoing the already-recorded completion.
  closeApproval = true;
  await commands['mate-complete'].handler('inspect', ctx);
  assert.equal(notices.at(-1)[1], 'error');
  assert.equal((await call('mate_status')).tasks[0].state, 'complete');
  assert.equal(messages.filter(m => m.message.customType === 'mate-tab-closed').length, 0);

  // Cancellation is a human command only: decline is a no-op, confirmation routes
  // through the read-only preflight and mutation RPC, and repeats retain its audit.
  await call('mate_propose', { id: 'cancel-me', repo, base: 'main', brief: 'Disposable cancellation fixture.' });
  approval = false;
  await commands['mate-cancel'].handler('cancel-me', ctx);
  assert.equal((await call('mate_status', { id: 'cancel-me' })).tasks[0].state, 'awaiting-base');
  approval = true;
  await commands['mate-approve'].handler('cancel-me', ctx);
  assert.equal((await call('mate_status', { id: 'cancel-me' })).tasks[0].state, 'approved');
  approval = false;
  await commands['mate-cancel'].handler('cancel-me', ctx);
  assert.equal((await call('mate_status', { id: 'cancel-me' })).tasks[0].state, 'approved', 'decline must not mutate');
  approval = true;
  await commands['mate-cancel'].handler('cancel-me', ctx);
  let cancelled = (await call('mate_status', { id: 'cancel-me' })).tasks[0];
  assert.equal(cancelled.state, 'cancelled');
  assert.equal(cancelled.completed_at, undefined);
  assert.equal(cancelled.cancelled_via, 'mate-cancel');
  assert.equal((await call('mate_status')).open_tasks, 0, 'cancelled history is not open capacity');
  assert.equal(messages.filter(m => m.message.customType === 'mate-cancelled').length, 1);
  await commands['mate-cancel'].handler('cancel-me', ctx);
  cancelled = (await call('mate_status', { id: 'cancel-me' })).tasks[0];
  assert.equal(cancelled.cancellation_history.length, 1, 'repeat preserves original cancellation audit');

  execFileSync('python3', ['-c', `import sqlite3,os,json\nc=sqlite3.connect(os.path.join(os.environ['MATE_HOME'],'mate.sqlite3'))\nt=json.loads(c.execute("SELECT data FROM tasks WHERE id='inspect'").fetchone()[0])\nt['same_tab_as']='supervisor'\nc.execute("UPDATE tasks SET data=? WHERE id='inspect'",(json.dumps(t),))\nc.commit()`]);
  await commands['mate-complete'].handler('inspect', { ...ctx, ui: { ...ctx.ui, confirm: async () => { throw new Error('Must not offer shared-tab closure'); } } });
  assert.match(notices.at(-1)[0], /Shared tab and worker pane retained/);
  assert.equal(tools.mate_dispatch.parameters.properties.same_tab_as.type, 'string');
  assert.match(tools.mate_dispatch.description, /same_tab_as/);
  await handlers.session_shutdown();
  // Echo dispatch RPC in the disposable installation only, to test TS argument forwarding.
  const backend = join(root, 'bin/mate.py');
  writeFileSync(backend, readFileSync(backend, 'utf8').replace('def dispatch(db, p):', 'def dispatch(db, p):\n    return p'));
  await handlers.session_start({}, ctx);
  await wait(() => call('mate_status'));
  writeFileSync(join(root, 'mate.config.json'), '{}');
  for (const same_tab_as of ['supervisor', 'inspect', undefined]) {
    const response = await tools.mate_dispatch.execute('placement', { id: 'next', same_tab_as }, undefined, undefined, ctx);
    const sent = JSON.parse(response.content[0].text);
    assert.equal(sent.same_tab_as, same_tab_as);
    assert.equal(sent.model, 'main-model');
    if (same_tab_as === undefined) assert.equal('same_tab_as' in sent, false);
  }
  await handlers.session_shutdown();
  const stopped = messages.length;
  await sleep(2100);
  assert.equal(messages.length, stopped, 'shutdown does not re-arm');
  console.log('PASS: dev mode no-op / supervisor policy separation, worker config validation/precedence/reload/catalog, Calm persistence/toggle/rendering/payload preservation, model/effort resolution and validation, extension load, tool guard, human-only approval/cancellation, follow-up wake, dedup, restart replay, ack, shutdown');
} finally {
  await handlers.session_shutdown();
  rmSync(tmp, { recursive: true, force: true });
  if (previousMode === undefined) delete process.env.MATE_MODE;
  else process.env.MATE_MODE = previousMode;
}
