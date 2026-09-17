// Real Python control plane + mocked Pi UI/model. No model calls or live workers.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, mkdirSync, readFileSync, statSync, writeFileSync, rmSync, openSync, closeSync, existsSync, cpSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, resolve, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const sourceRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const workerPolicy = readFileSync(join(sourceRoot, 'WORKER.md'), 'utf8');
assert.match(workerPolicy, /scope explicitly\nrequires a PR, you may push only the assigned task branch/);
assert.match(workerPolicy, /Never merge, deploy/);
const ahoy = readFileSync(join(sourceRoot, '.pi/skills/ahoy/SKILL.md'), 'utf8');
assert.match(ahoy, /^---\nname: ahoy\ndescription: .+\n---/);
assert.match(ahoy, /Otherwise, use only visible session history\. Do not call tools/);
assert.match(ahoy, /call `mate_status` once/);
assert.match(ahoy, /present only the one you judge most impactful/);
assert.doesNotMatch(ahoy, /fm-session-start|FIRSTMATE_OP/);
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
  '@earendil-works/pi-ai/compat': join(installed, 'node_modules/@earendil-works/pi-ai/dist/compat.js'),
  '@earendil-works/pi-ai': join(installed, 'node_modules/@earendil-works/pi-ai/dist/index.js'),
  '@earendil-works/pi-coding-agent': join(installed, 'dist/index.js'),
  '@earendil-works/pi-tui': join(installed, 'node_modules/@earendil-works/pi-tui/dist/index.js'),
} });
const { default: factory, workerProfile, dispatchProfile, dispatchInstructions } = await jiti.import(join(root, '.pi/extensions/mate-supervisor.ts'));
const { statusPreview } = await jiti.import(join(root, '.pi/extensions/lib/calm.ts'));
const { renderBearingsBoard } = await jiti.import(join(root, '.pi/extensions/lib/bearings.ts'));
process.env.MATE_HOME = join(tmp, 'home');
const originalPath = process.env.PATH;
const fakebin = join(tmp, 'fakebin'); mkdirSync(fakebin);
writeFileSync(join(fakebin, 'lavish-axi'), '#!/bin/sh\nprintf "session:\\n  url: http://127.0.0.1:4321/session/fixture\\n  status: opened\\n"\n', { mode: 0o755 });
process.env.PATH = `${fakebin}:${originalPath}`;
const repo = join(tmp, 'repo'); mkdirSync(repo);
const git = (...args) => execFileSync('git', ['-C', repo, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
git('init', '-b', 'main'); git('-c', 'user.name=Mate Test', '-c', 'user.email=mate@test.invalid', 'commit', '--allow-empty', '-m', 'base');
const handlers = {}, tools = {}, commands = {}, renderers = {}, messages = [], notices = [], statuses = {};
let active = ['read', 'write', 'bash', 'external_tool'], approval = false, closeApproval = false, releaseApproval = false, expanded = false;
const models = [
  { provider: 'openai-codex', id: 'main-model', reasoning: true },
  { provider: 'openai-codex', id: 'worker-model', reasoning: true, thinkingLevelMap: { xhigh: 'xhigh' } },
  { provider: 'other', id: 'vendor/model', reasoning: false },
];
let supervisorIdle = true, supervisorQueued = false;
const ctx = { mode: 'tui', hasUI: true, model: models[0], thinkingLevel: 'high',
  isIdle: () => supervisorIdle, hasPendingMessages: () => supervisorQueued,
  modelRegistry: { find: (provider, id) => models.find(m => m.provider === provider && m.id === id) },
  ui: { notify: (...args) => notices.push(args), setStatus(key, value) { statuses[key] = value; }, confirm: async title => title === 'Close worker Herdr tab too?' ? closeApproval : title === 'Return Treehouse worktree too?' ? releaseApproval : approval,
    getToolsExpanded: () => expanded, setToolsExpanded: value => { expanded = value; } } };
const pi = {
  on(name, fn) { handlers[name] = fn; },
  registerTool(tool) { tools[tool.name] = tool; },
  registerCommand(name, command) { commands[name] = command; },
  registerMessageRenderer(name, renderer) { renderers[name] = renderer; },
  setActiveTools(names) { active = names; },
  sendMessage(message, options) {
    assert.ok(!['mate-wake', 'mate-watch-error'].includes(message.customType), 'operational wakes must not use custom messages');
    messages.push({ message, options });
  },
  sendUserMessage(content, options) {
    // Test metadata only; the actual Pi API receives plain native user text.
    const events = content.startsWith('MATE EVENT') ? JSON.parse(content.split('\n')[0].split(': ').slice(1).join(': ')) : [];
    messages.push({ message: { role: 'user', customType: 'mate-wake', content, details: { events } }, options });
  },
};
const previousMode = process.env.MATE_MODE;
process.env.MATE_MODE = 'dev';
factory(new Proxy({}, { get(_target, key) { throw new Error(`Dev mode touched Pi API: ${String(key)}`); } }));
assert.equal(existsSync(process.env.MATE_HOME), false, 'dev mode creates no runtime state');
assert.deepEqual(active, ['read', 'write', 'bash', 'external_tool'], 'dev mode leaves tools untouched');
delete process.env.MATE_MODE;
factory(pi);
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
  const board = renderBearingsBoard({ total_tasks: 5, tasks: [
    { id: 'needs-approval', state: 'awaiting-base', project: 'mate', attempt: 0, sha: '1234567890abcdef', usage_total: {} },
    { id: 'shipping', state: 'running', project: 'mate', attempt: 1, error: '</script><script>bad()</script>', usage_total: {} },
    { id: 'done', state: 'complete', project: 'mate', attempt: 1, usage_total: {} },
    { id: 'next', state: 'approved', project: 'mate', attempt: 0, usage_total: {} },
  ], events: [{ task: 'shipping', kind: 'report', note: 'Ready' }] });
  for (const heading of ['Captain’s Call', 'Recently Landed', 'Underway', 'Charted Next']) assert.match(board, new RegExp(heading));
  assert.match(board, /Copy \/mate-approve needs-approval/);
  assert.doesNotMatch(board, /<script>bad\(\)<\/script>/, 'task content cannot escape into markup');
  assert.match(board, /Read-only board/);

  const { default: bridge } = await jiti.import(join(root, 'bin/worker-events.ts'));
  const bridgeHandlers = {}, bridgeFile = join(tmp, 'bridge.jsonl');
  const fd = openSync(bridgeFile, 'w', 0o600);
  const bridgeEnv = Object.fromEntries(['MATE_EVENT_FD', 'MATE_REPLY_FD', 'MATE_WORKER_CONTROL', 'MATE_SESSION_FILE', 'MATE_ATTEMPT'].map(key => [key, process.env[key]]));
  const repliesFile = join(tmp, 'bridge-replies');
  writeFileSync(repliesFile, [JSON.stringify({ ok: true, attempt: 1, brief: 'Fixture scope' }), JSON.stringify({ ok: true, attempt: 1 })].map(line => line.padEnd(4095) + '\n').join(''));
  const replyFd = openSync(repliesFile, 'r');
  let shutdowns = 0, aborted = false, idle = false, queued = false;
  const bridgeCtx = { ui: { notify() {} }, isIdle: () => idle, hasPendingMessages: () => queued,
    sessionManager: { getSessionFile: () => join(tmp, 'fixture.jsonl') }, model: { provider: 'fixture', id: 'fixture' },
    shutdown: () => { shutdowns++; }, abort: () => { aborted = true; } };
  try {
    process.env.MATE_EVENT_FD = String(fd);
    process.env.MATE_REPLY_FD = String(replyFd);
    process.env.MATE_SESSION_FILE = join(tmp, 'fixture.jsonl');
    process.env.MATE_WORKER_CONTROL = JSON.stringify({ socket: join(tmp, 'unused.sock'), generation: 'fixture' });
    process.env.MATE_ATTEMPT = '1';
    bridge({ on: (name, handler) => { bridgeHandlers[name] = handler; }, getThinkingLevel: () => 'off' });
    bridgeHandlers.agent_start({ type: 'agent_start' }, bridgeCtx);
    assert.equal(bridgeHandlers.agent_end, undefined, 'do not terminate retries at low-level agent_end');
    bridgeHandlers.agent_settled({ type: 'agent_settled' }, bridgeCtx);
    assert.equal(shutdowns, 0, 'ignore unsettled agent');
    idle = true; queued = true;
    bridgeHandlers.agent_settled({ type: 'agent_settled' }, bridgeCtx);
    assert.equal(shutdowns, 0, 'queued messages must run first');
    queued = false;
    bridgeHandlers.message_end({ type: 'message_end', message: { role: 'assistant', stopReason: 'stop' } }, bridgeCtx);
    bridgeHandlers.agent_settled({ type: 'agent_settled' }, bridgeCtx);
    assert.equal(shutdowns, 0, 'settled publishes a report without exiting Pi');
    assert.deepEqual(bridgeHandlers.session_before_switch({}, bridgeCtx), { cancel: true });
    assert.deepEqual(bridgeHandlers.session_before_fork({}, bridgeCtx), { cancel: true });
    assert.deepEqual(bridgeHandlers.session_before_tree({}, bridgeCtx), { cancel: true });
    assert.equal(bridgeHandlers.user_bash().result.exitCode, 1, 'no untracked shell mutation');
    assert.equal(JSON.parse(readFileSync(bridgeFile, 'utf8').trim().split('\n').at(-1)).type, 'agent_settled');
    bridgeHandlers.message_update({ text: 'x'.repeat(4 * 1024 * 1024) }, bridgeCtx);
    assert.equal(aborted, true, 'bridge failure must abort instead of silently losing reports');
    assert.equal(process.exitCode, 1);
    process.exitCode = 0;
  } finally {
    closeSync(fd); closeSync(replyFd);
    for (const [key, value] of Object.entries(bridgeEnv)) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
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
  configure({ worker: { model: 'openai-codex/worker-model', effort: 'xhigh', max_active: 5, workspace_per_task: true } });
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
  const dispatch = { rules: [{ when: 'The task is broad or risky.',
    use: { model: 'openai-codex/worker-model', effort: 'xhigh' }, why: 'Use strong reasoning.' }] };
  configure({ worker: { model: 'openai-codex/main-model', effort: 'high' }, dispatch });
  assert.match(dispatchInstructions(configPath), /The task is broad or risky/);
  assert.match(dispatchInstructions(configPath), /"default":\{"model":"openai-codex\/main-model","effort":"high"\}/);
  assert.throws(() => dispatchProfile(ctx, {}, configPath), /Dispatch rules are active/);
  assert.deepEqual(dispatchProfile(ctx, { model: 'openai-codex/worker-model', effort: 'xhigh' }, configPath),
    { provider: 'openai-codex', model: 'worker-model', effort: 'xhigh' });
  configure({ worker: { model: 'openai-codex/main-model', effort: 'high' }, dispatch: { rules: [
    { when: 'x', use: { model: 'missing', effort: 'low' } }] } });
  assert.throws(() => dispatchProfile(ctx, { model: 'main-model', effort: 'high' }, configPath), /Unknown model/,
    'an invalid unused rule must fail closed before dispatch');
  for (const invalid of [null, [], { workers: {} }, { worker: null }, { worker: [] }, { projects: null }, { projects: [] },
    { worker: { model: 1 } }, { worker: { model: ' ' } }, { worker: { effort: 'ultra' } },
    { worker: { max_active: 0 } }, { worker: { max_active: 1.5 } },
    { worker: { workspace_per_task: 'yes' } }, { worker: { typo: true } },
    { dispatch: {} }, { worker: { model: 'main-model', effort: 'high' }, dispatch: { rules: [] } },
    { worker: { model: 'main-model', effort: 'high' }, dispatch: { rules: [{ when: '', use: { model: 'worker-model', effort: 'low' } }] } },
    { worker: { model: 'main-model', effort: 'high' }, dispatch: { rules: [{ when: 'x', use: [{ model: 'worker-model', effort: 'low' }] }] } },
    { worker: { model: 'main-model', effort: 'high' }, dispatch: { rules: [{ when: 'x', use: { model: 'worker-model' } }] } }]) {
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
  const lunaCtx = { ...ctx, modelRegistry: { find: (provider, id) =>
    provider === luna.provider && id === luna.id ? luna : undefined } };
  assert.throws(() => dispatchProfile(lunaCtx, {}), /Dispatch rules are active/);
  assert.deepEqual(dispatchProfile(lunaCtx, { model: 'openai-codex/gpt-5.6-luna', effort: 'xhigh' }),
    { provider: 'openai-codex', model: 'gpt-5.6-luna', effort: 'xhigh' });
  cpSync(join(sourceRoot, 'mate.config.example.json'), join(root, 'mate.config.example.json'));
  rmSync(join(root, 'mate.config.json'));
  assert.throws(() => dispatchProfile(ctx, {}), /Cannot read/, 'missing local config must not fall back to example');
  cpSync(join(root, 'mate.config.example.json'), join(root, 'mate.config.json'));
  await handlers.session_start({}, ctx);
  await wait(() => call('mate_status'));
  await commands.bearings.handler('', ctx);
  assert.match(notices.at(-1)[0], /Usage: \/bearings lavish/);
  await commands.bearings.handler('lavish', ctx);
  const boardPath = join(process.env.MATE_HOME, '.lavish', 'bearings-board.html');
  assert.equal(existsSync(boardPath), true);
  assert.equal(statSync(boardPath).mode & 0o777, 0o600);
  assert.match(readFileSync(boardPath, 'utf8'), /Nothing needs your action right now/);
  assert.match(notices.at(-1)[0], /http:\/\/127\.0\.0\.1:4321\/session\/fixture/);
  assert.match(notices.at(-1)[0], /Read-only/);
  const basePrompt = 'base\n\n' + readFileSync(join(root, 'SUPERVISOR.md'), 'utf8') + '\n\n' + dispatchInstructions();
  assert.equal((await handlers.before_agent_start({ systemPrompt: 'base' }, ctx)).systemPrompt, basePrompt);
  assert.equal(handlers.tool_call({ toolName: 'mate_memory' }), undefined);
  const beforeStow = await call('mate_status');
  const emptyMemory = await call('mate_memory');
  assert.equal(emptyMemory.revision, 0);
  const beforeStowMessages = messages.length;
  supervisorIdle = false;
  await commands.stow.handler('', ctx);
  supervisorIdle = true;
  await commands.stow.handler('reset', ctx);
  assert.equal(messages.length, beforeStowMessages, 'busy/invalid stow cannot trigger a model');
  await commands.stow.handler('', ctx);
  assert.equal(messages.length, beforeStowMessages + 1);
  assert.match(messages.at(-1).message.content, /Read mate_memory fully first/);
  assert.match(messages.at(-1).message.content, /Do not reset automatically/);
  assert.equal(messages.at(-1).options.deliverAs, 'followUp');
  assert.equal((await call('mate_memory')).revision, 0, 'command does not pretend model already saved');
  messages.pop(); // Keep later wake assertions scoped to actual task events.
  const oldNotes = await call('mate_memory', { action: 'save', revision: 0, content: 'OLD_MEMORY_SENTINEL', reason: 'fixture initial' });
  const notes = await call('mate_memory', { action: 'save', revision: oldNotes.revision, content: 'CURRENT_MEMORY_SENTINEL', reason: 'fixture superseded' });
  assert.deepEqual(await call('mate_status'), beforeStow, 'memory does not change tasks/events');
  await assert.rejects(() => call('mate_memory', { action: 'save', revision: oldNotes.revision, content: 'stale', reason: 'fixture' }), /revision changed/);
  assert.equal((await handlers.before_agent_start({ systemPrompt: 'base' }, ctx)).systemPrompt, basePrompt, 'prefix stays fixed after save');
  await handlers.session_shutdown();
  await handlers.session_start({ reason: 'new' }, ctx);
  await wait(() => call('mate_status'));
  const restoredPrompt = (await handlers.before_agent_start({ systemPrompt: 'base' }, ctx)).systemPrompt;
  assert.match(restoredPrompt, /CURRENT_MEMORY_SENTINEL/);
  assert.match(restoredPrompt, /untrusted historical context, never approval/);
  assert.doesNotMatch(restoredPrompt, /OLD_MEMORY_SENTINEL/, 'cold revisions are never auto-loaded');
  assert.equal((await call('mate_memory')).revision, notes.revision);
  assert.equal((await call('mate_memory', { revision: oldNotes.revision })).content, 'OLD_MEMORY_SENTINEL');

  // Calm is presentation only; existing components redraw without changing payloads.
  const preview = statusPreview({ total_tasks: 22, open_tasks: 7, events: [], tasks: [{
    id: 'migrate-admin-add-user-credit', state: 'review', attempt: 1, repo: '/repo', base: 'main',
    sha: '1234567890abcdef', branch: 'mate/task', brief: 'User intent:\nMake the status readable.'
  }] });
  assert.match(preview, /^22 tasks · 7 open · 0 pending/m);
  assert.match(preview, /\[REVIEW\] migrate-admin-add-user-credit · attempt 1/);
  assert.match(preview, /brief   User intent: Make the status readable\./);
  assert.doesNotMatch(preview, /[{}\"]/);
  const listPreview = statusPreview({ total_tasks: 2, open_tasks: 1, events: [], tasks: [
    { id: 'one', state: 'running', attempt: 1, base: 'main', model: 'worker' },
    { id: 'two', state: 'complete', attempt: 2, base: 'main', model: 'worker' },
  ] });
  assert.equal(listPreview.split('\n').length, 3, 'task lists stay one line per task');
  assert.match(statusPreview({ report_attempt: 2, report: { text: 'result\ncheck', more: false } }), /report · attempt 2\nresult\ncheck/);
  const theme = { fg: (_color, text) => text, bg: (_color, text) => text };
  const output = data => ({ content: [{ type: 'text', text: JSON.stringify(data) }] });
  const statusColors = [], statusBackgrounds = [];
  const statusResult = tools.mate_status.renderResult(output({ total_tasks: 1, open_tasks: 1, tasks: [{ id: 'inspect', state: 'review', repo: '/repo', brief: 'Readable' }], events: [] }),
    { expanded: true, isPartial: false }, {
      fg: (color, text) => { statusColors.push(color); return text; },
      bg: (color, text) => { statusBackgrounds.push(color); return text; },
    }, { state: {}, isError: false });
  const renderedStatus = statusResult.render(100).join('\n');
  assert.match(renderedStatus, /\[REVIEW\] inspect/);
  assert.doesNotMatch(renderedStatus, /"tasks"/, 'expanded status stays human-readable');
  assert.deepEqual(statusColors, ['text'], 'status uses readable body contrast');
  assert.ok(statusBackgrounds.every(color => color === 'toolSuccessBg') && statusBackgrounds.length, 'settled output supplies a contrasting background');
  const continueResult = tools.mate_continue.renderResult(output({ id: 'inspect', state: 'launching', repo: '/repo', brief: 'Readable' }),
    { expanded: true, isPartial: false }, theme, { state: {}, isError: false });
  assert.match(continueResult.render(100).join('\n'), /\[LAUNCHING\] inspect/);
  assert.doesNotMatch(continueResult.render(100).join('\n'), /"brief"/, 'task mutations use the readable task summary too');
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
  assert.deepEqual(active.sort(), ['mate_ack', 'mate_continue', 'mate_dispatch', 'mate_extend', 'mate_memory', 'mate_propose', 'mate_status']);
  assert.equal(handlers.tool_call({ toolName: 'bash' }).block, true);
  assert.equal(handlers.tool_call({ toolName: 'read' }).block, true);
  assert.equal(handlers.tool_call({ toolName: 'external_tool' }).block, true);
  assert.match(tools.mate_continue.description, /unstarted continuation/);
  assert.match(tools.mate_continue.description, /No worker lock after 60s/);
  assert.match(tools.mate_continue.description, /Never force/);
  const brief = '## User intent\nตรวจ fixture แบบ read-only\n## Mate spec\nReport file references.\n## Exclusions\nNo edits or tests.\n## Acceptance evidence\nReferences, tests NOT RUN.\n## Stop conditions\nAsk if fixture is missing.';
  assert.match(tools.mate_propose.parameters.properties.brief.description, /User intent/);
  assert.match(tools.mate_extend.description, /five brief sections/);
  assert.equal(tools.mate_status.parameters.properties.history.type, 'boolean');
  await call('mate_propose', { id: 'inspect', repo, base: 'main', brief });
  await commands['mate-approve'].handler('inspect', ctx);
  assert.equal((await call('mate_status')).tasks[0].state, 'awaiting-base');
  approval = true;
  const revisedBrief = brief.replace('Report file references.', 'Report revised file references.');
  await commands['mate-approve'].handler('inspect', { ...ctx, ui: { ...ctx.ui, confirm: async () => {
    await call('mate_propose', { id: 'inspect', repo, base: 'main', brief: revisedBrief });
    return true;
  } } });
  assert.equal((await call('mate_status')).tasks[0].state, 'awaiting-base', 'stale displayed scope is not approved');
  assert.equal(notices.at(-1)[1], 'error');
  await commands['mate-approve'].handler('inspect', { ...ctx, ui: { ...ctx.ui, confirm: async (_title, body) => {
    assert.ok(body.includes(revisedBrief), 'human sees the exact revised five-section brief'); return true;
  } } });
  assert.equal((await call('mate_status')).tasks[0].state, 'approved');
  const baseEvent = (await call('mate_status')).events.find(e => e.kind === 'base-approved');
  assert.ok(baseEvent, 'base approval is durable, not a separate ephemeral send');
  await wait(() => messages.some(m => m.message.details?.events.some(e => e.id === baseEvent.id)));
  assert.match(messages.at(-1).message.content, /For base-approved events/);
  await call('mate_ack', { events: [baseEvent.id], note: 'Fixture base approval inspected; dispatch blocked in fixture.' });
  execFileSync('python3', ['-c', `import sqlite3,os\nc=sqlite3.connect(os.path.join(os.environ['MATE_HOME'],'mate.sqlite3'))\nc.execute("INSERT INTO events(task,attempt,kind,note) VALUES ('inspect',0,'test','fixture outcome')")\nc.commit()`]);
  await wait(() => messages.some(m => m.message.details?.events.some(e => e.kind === 'test')));
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
  await call('mate_ack', { events: (await call('mate_status')).events.map(e => e.id), note: 'Relayed fixture outcome' });
  assert.equal((await call('mate_status')).events.length, 0);
  assert.equal(tools.mate_close_tab, undefined, 'tab closure is not a model tool');
  assert.equal(handlers.tool_call({ toolName: 'mate_close_tab' }).block, true);
  assert.equal(tools.mate_return_lease, undefined, 'lease return is not a model tool');
  assert.equal(handlers.tool_call({ toolName: 'mate_return_lease' }).block, true);
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
  assert.equal(scoped.scope_revision, 1);
  assert.equal(scoped.latest_scope.first_attempt, 2);
  assert.equal(scoped.scope_history, undefined, 'cold history is not in ordinary model status');
  const historical = (await call('mate_status', { id: 'inspect', history: true })).tasks[0];
  assert.equal(historical.scope_history.length, 1);
  assert.equal(historical.original_brief, initialScope);
  assert.equal(historical.scope_history[0].token, scoped.latest_scope.token);
  writeFileSync(join(process.env.MATE_HOME, 'inspect/report-1.txt'), 'ก'.repeat(12000) + 'remaining evidence');
  const firstPage = await call('mate_status', { id: 'inspect', attempt: 1 });
  const nextPage = await call('mate_status', { id: 'inspect', attempt: firstPage.report_attempt, offset: firstPage.report.next_offset });
  assert.equal(nextPage.report.text, 'remaining evidence');
  assert.equal(nextPage.tasks, undefined);
  assert.equal(nextPage.events, undefined);
  assert.equal(nextPage.report.more, false);
  assert.ok(row('mate_status', output(nextPage))().length, 'Calm preserves report-only pages');
  await assert.rejects(() => call('mate_status', { id: 'inspect', offset: 1 }), /explicit attempt/);
  await commands['mate-complete'].handler('inspect', ctx);
  assert.match(notices.at(-1)[0], /awaits approval\/execution/, 'human completion still checks full scope history');
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
  assert.equal(messages.length, correctedCount, 'no third automatic turn or custom nextTurn queue');
  const humanInput = { source: 'interactive', text: 'Unrelated human question', images: [{ type: 'image' }] };
  assert.equal(await handlers.input({ ...humanInput, source: 'extension' }, ctx), undefined, 'no recursive attachment to runtime wakes');
  const attached = await handlers.input(humanInput, ctx);
  assert.equal(attached.action, 'transform');
  assert.ok(attached.text.startsWith(humanInput.text + '\n\n'));
  assert.match(attached.text, /not part of the human's request/);
  assert.match(attached.text, /scope-approved-/);
  assert.equal(attached.images, undefined, 'Pi preserves original images when omitted by the transform');
  assert.equal(messages.length, correctedCount, 'attachment itself starts no turn');
  supervisorIdle = false;
  assert.equal((await handlers.input(humanInput, ctx)).text, attached.text, 'queued human input also retains the wake');
  supervisorIdle = true;
  assert.equal((await handlers.input(humanInput, ctx)).text, attached.text, 'later human inputs retain unacked events');
  // Optional real installed compaction extension: synthetic checkpoint, actual
  // message_end and before_provider_request hooks, never provider/auth calls.
  if (process.env.MATE_COMPACTION_EXTENSION) {
    const { default: compaction } = await jiti.import(resolve(process.env.MATE_COMPACTION_EXTENSION, 'src/index.ts'));
    const hooks = {};
    compaction({ on: (name, fn) => { hooks[name] = fn; }, registerProvider() {} });
    const model = { provider: 'openai-codex', api: 'openai-codex-responses', id: 'fixture', reasoning: false };
    const branch = [{ type: 'compaction', id: 'synthetic-checkpoint', details: { remoteCompaction: {
      version: 1, provider: 'openai-responses-compact', implementation: 'responses_compact_v1',
      modelKey: 'openai-codex:openai-codex-responses:fixture',
      replacementHistory: [{ type: 'message', role: 'user', content: [{ type: 'input_text', text: 'SYNTHETIC_CHECKPOINT' }] }],
    } } }];
    const cctx = { ...ctx, cwd: root, model, sessionManager: { getSessionId: () => 'mate-compaction-fixture', getBranch: () => branch } };
    writeFileSync(join(root, '.pi/openai-server-compaction.json'), JSON.stringify({ enabled: true, notify: false }));
    const payload = () => hooks.before_provider_request({ payload: { model: 'fixture', input: [{ role: 'user', content: 'RAW_INPUT_SENTINEL' }] } }, cctx);
    const receive = message => {
      branch.push({ type: 'message', id: `entry-${branch.length}`, message });
      hooks.message_end({ message }, cctx);
    };
    try {
      hooks.session_start({}, cctx);
      hooks.message_end({ message: { role: 'custom', customType: 'mate-wake', content: 'LOST_CUSTOM_SENTINEL' } }, cctx);
      assert.ok(payload(), 'real Codex remote-history hook must be active');
      assert.doesNotMatch(JSON.stringify(payload()), /LOST_CUSTOM_SENTINEL|RAW_INPUT_SENTINEL/, 'negative control reproduces dropped custom wake');
      for (const content of [messages.at(-1).message.content, attached.text]) {
        receive({ role: 'user', content, timestamp: 1 });
        const wire = JSON.stringify(payload());
        assert.ok(wire.includes(JSON.stringify(content).slice(1, -1)), 'actual remote-history request retains native wake/attachment');
        receive({ role: 'assistant', ...model, model: model.id, content: [{ type: 'text', text: 'ignored' }], timestamp: 2 });
      }
      const beforeReload = payload();
      hooks.session_start({}, cctx);
      assert.deepEqual(payload(), beforeReload, 'compaction reconstruction preserves native wake history without duplicates');
      console.log('PASS: installed compaction Codex hook retains native wakes and human attachments after synthetic checkpoint/reload; custom negative control drops');
    } finally { hooks.session_shutdown({}, cctx); }
  }
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
  assert.equal(await handlers.input({ source: 'interactive', text: 'Next question' }, ctx), undefined, 'ack removes pending input attachment');
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
  await commands['mate-status'].handler('', ctx);
  const commandStatus = JSON.parse(notices.at(-1)[0]);
  assert.equal(commandStatus.total_tasks, 2, 'status keeps the retained-history count');
  assert.deepEqual(commandStatus.tasks, [], '/mate-status shows open tasks only');
  assert.equal(messages.filter(m => m.message.customType === 'mate-cancelled').length, 1);
  await commands['mate-cancel'].handler('cancel-me', ctx);
  assert.equal(cancelled.cancellation_history, undefined, 'cancellation audit stays cold by default');
  cancelled = (await call('mate_status', { id: 'cancel-me', history: true })).tasks[0];
  assert.equal(cancelled.cancellation_history.length, 1, 'repeat preserves original cancellation audit');

  execFileSync('python3', ['-c', `import sqlite3,os,json\nc=sqlite3.connect(os.path.join(os.environ['MATE_HOME'],'mate.sqlite3'))\nt=json.loads(c.execute("SELECT data FROM tasks WHERE id='inspect'").fetchone()[0])\nt.update(same_tab_as='supervisor',worktree=${JSON.stringify(repo)},lease={'lease_id':'lease-test','lease_holder':'holder-test'})\nc.execute("UPDATE tasks SET data=? WHERE id='inspect'",(json.dumps(t),))\nc.commit()`]);
  writeFileSync(join(repo, 'unfinished.txt'), 'keep me');
  let returnBody = '';
  await commands['mate-complete'].handler('inspect', { ...ctx, ui: { ...ctx.ui, confirm: async (title, body) => {
    if (title === 'Return Treehouse worktree too?') { returnBody = body; return false; }
    throw new Error('Must not offer shared-tab closure');
  } } });
  assert.match(notices.at(-2)[0], /Shared tab retained/);
  assert.match(returnBody, /\?\? unfinished\.txt/);
  assert.match(notices.at(-1)[0], /lease retained/);
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
  await commands.stow.handler('', ctx);
  assert.match(notices.at(-1)[0], /control plane unavailable/);
  assert.equal(messages.length, stopped, 'stow refuses after ownership shutdown');
  await sleep(2100);
  assert.equal(messages.length, stopped, 'shutdown does not re-arm');
  console.log('PASS: Bearings read-only board/command, stow command/refusals, memory save/history/conflicts/session reload/stable prefix, dev mode no-op / supervisor policy separation, worker config validation/precedence/reload/catalog, Calm persistence/toggle/rendering/payload preservation, model/effort resolution and validation, extension load, tool guard, human-only approval/cancellation, follow-up wake, dedup, restart replay, ack, shutdown');
} finally {
  await handlers.session_shutdown();
  rmSync(tmp, { recursive: true, force: true });
  if (previousMode === undefined) delete process.env.MATE_MODE;
  else process.env.MATE_MODE = previousMode;
  process.env.PATH = originalPath;
}
