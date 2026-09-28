// Primary Pi command/tool boundary + real Python transport/remote control plane.
// Fake SSH boundary, disposable state, no Herdr, credentials or model calls.
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { mkdtempSync, mkdirSync, cpSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createInterface } from 'node:readline';

const source = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const tmp = mkdtempSync(join(tmpdir(), 'mate-remote-ui-'));
const root = join(tmp, 'install'), local = join(tmp, 'primary'), home = join(tmp, 'remote');
const fakebin = join(tmp, 'bin'), repo = join(tmp, 'repo');
for (const path of [root, local, home, fakebin, repo, join(local, 'remotes')]) mkdirSync(path, { recursive: true });
cpSync(join(source, 'bin'), join(root, 'bin'), { recursive: true });
writeFileSync(join(root, 'mate.config.json'), '{}');
const route = { host: 'fixture', home: randomUUID(), primary: randomUUID(), outbox: join(local, 'outbox.sqlite3') };
const binding = { home: route.home, primary: route.primary, journal: join(home, 'inbox.sqlite3') };
writeFileSync(join(local, 'remotes/fixture.json'), JSON.stringify(route), { mode: 0o600 });
writeFileSync(join(home, 'remote.json'), JSON.stringify(binding), { mode: 0o600 });
const setup = `import sys,json;sys.path.insert(0,sys.argv[1]);import mate_remote as i;import mate_remote_primary as p;r=json.loads(sys.argv[2]);b=json.loads(sys.argv[3]);i.create(b['journal'],b['home'],b['primary']);p.create(r)`;
execFileSync('python3', ['-c', setup, join(root, 'bin'), JSON.stringify(route), JSON.stringify(binding)]);
const git = (...args) => execFileSync('git', ['-C', repo, ...args], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
git('init', '-b', 'main');
git('-c', 'user.name=Mate Test', '-c', 'user.email=mate@test.invalid', 'commit', '--allow-empty', '-m', 'fixture');
writeFileSync(join(fakebin, 'ssh'), `#!/usr/bin/env python3
import os,sys,time
assert sys.argv[-1]=='mate-remote-v1'
if sys.argv[-2]=='slow':
 time.sleep(3)
 sys.exit(255)
assert sys.argv[-2]=='fixture'
os.environ['SSH_ORIGINAL_COMMAND']='mate-remote-v1'
os.execv(sys.executable,[sys.executable,${JSON.stringify(join(root, 'bin/mate_remote_transport.py'))},'receive',${JSON.stringify(join(home, 'remote.json'))}])
`, { mode: 0o755 });
const oldPath = process.env.PATH;
process.env.PATH = `${fakebin}:${oldPath}`;
const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('MATE_') && !key.startsWith('HERDR_')));
env.MATE_HOME = home;
const child = spawn('python3', [join(root, 'bin/mate.py'), 'serve'], { env, stdio: 'pipe' });
let errors = '';
child.stderr.on('data', data => { errors += data.toString(); });
const lines = createInterface({ input: child.stdout });
const started = new Promise((resolveReady, reject) => {
  const timer = setTimeout(() => reject(new Error('Remote startup timed out: ' + errors)), 10000);
  lines.once('line', line => { clearTimeout(timer); try { assert.equal(JSON.parse(line).role, 'secondmate'); resolveReady(); } catch (error) { reject(error); } });
  child.once('error', reject);
});
const installed = join(execFileSync('npm', ['root', '-g'], { encoding: 'utf8' }).trim(), '@earendil-works/pi-coding-agent');
const { createJiti } = await import(pathToFileURL(join(installed, 'node_modules/jiti/lib/jiti.mjs')).href);
const jiti = createJiti(import.meta.url, { alias: { typebox: join(installed, 'node_modules/typebox/build/index.mjs') } });
const { createRemoteFleet, registerRemoteUI } = await jiti.import(join(source, '.pi/extensions/lib/remote.ts'));
const fleet = createRemoteFleet(root, local);
const tools = {}, commands = {}, notices = [], confirmations = [];
let approve = false, primary = true;
const ctx = { mode: 'tui', ui: { notify: (...args) => notices.push(args), confirm: async (title, body) => { confirmations.push({ title, body }); return approve; } } };
registerRemoteUI({ registerCommand: (name, value) => { commands[name] = value; } }, tool => { tools[tool.name] = tool; }, fleet, () => primary);
const tool = async (operation, params) => JSON.parse((await tools.mate_remote.execute('test', { remote: 'fixture', operation, params })).content[0].text);
const command = (args, context = ctx) => commands['mate-remote'].handler(args, context);
try {
  await started;
  await command('');
  assert.equal(notices.at(-1)[0], 'fixture');
  await assert.rejects(tool('approve', {}), /Human operations/);
  primary = false;
  await assert.rejects(tool('status', {}), /ready primary/);
  primary = true;
  const params = { id: 'check', repo, base: 'main', brief: 'Only read the disposable fixture; no remote push.' };
  await tool('propose', params);
  const proposed = (await tool('status', { id: 'check', history: true })).tasks[0];
  await command('fixture approve check', { ...ctx, mode: 'rpc' });
  assert.match(notices.at(-1)[0], /human TUI/);
  assert.equal(confirmations.length, 0);
  await command('fixture approve check');
  assert.match(notices.at(-1)[0], /Declined/);
  assert.equal((await tool('status', { id: 'check' })).tasks[0].state, 'awaiting-base');
  const shown = confirmations.at(-1).body;
  for (const value of [route.host, route.home, repo, proposed.sha, params.brief]) assert.ok(shown.includes(value), `approval omitted ${value}`);
  // A proposal changes while the real human dialog is open: no silent refresh.
  await command('fixture approve check', { ...ctx, ui: { ...ctx.ui, confirm: async () => {
    await tool('propose', { ...params, brief: 'Revised scope while dialog was open' });
    return true;
  } } });
  assert.match(notices.at(-1)[0], /Stale parent confirmation/);
  assert.equal((await tool('status', { id: 'check' })).tasks[0].state, 'awaiting-base');
  approve = true;
  await command('fixture approve check');
  assert.equal(notices.at(-1)[1], 'info', notices.at(-1)[0]);
  const accepted = (await tool('status', { id: 'check', history: true })).tasks[0];
  assert.equal(accepted.state, 'approved');
  assert.equal(accepted.attempt, 0);
  assert.equal(accepted.parent_approval.primary, route.primary);
  assert.equal(accepted.lease, undefined);
  assert.deepEqual((await fleet.inspect('fixture')).pending, []);
  await command('fixture complete check');
  assert.match(notices.at(-1)[0], /Only review/);
  await command('../unsafe status');
  assert.match(notices.at(-1)[0], /Invalid remote route/);
  // One unavailable home does not hold the healthy home's stdio queue.
  const slowRoute = { ...route, host: 'slow', outbox: join(local, 'slow.sqlite3') };
  writeFileSync(join(local, 'remotes/slow.json'), JSON.stringify(slowRoute), { mode: 0o600 });
  execFileSync('python3', ['-c', `import sys,json;sys.path.insert(0,sys.argv[1]);import mate_remote_primary as p;p.create(json.loads(sys.argv[2]))`, join(root, 'bin'), JSON.stringify(slowRoute)]);
  const slow = fleet.call('slow', 'status', {}).then(() => 'unexpected', () => 'slow-failed');
  const healthy = tool('status', { id: 'check' }).then(() => 'healthy');
  assert.equal(await Promise.race([slow, healthy]), 'healthy');
  assert.equal(await slow, 'slow-failed');
  await command('fixture reconnect');
  assert.equal((await tool('status', { id: 'check' })).tasks[0].state, 'approved');
  await tool('propose', { ...params, id: 'session-check' });
  await command('fixture approve session-check', { ...ctx, ui: { ...ctx.ui, confirm: async () => { fleet.close(); return true; } } });
  assert.match(notices.at(-1)[0], /session changed/);
  assert.equal((await tool('status', { id: 'session-check' })).tasks[0].state, 'awaiting-base');
  // Cleanup UI contract only; actual lease/endpoint safety stays in Python tests.
  const confirmation = 'a'.repeat(64), cleanupCalls = [], cleanupCommands = {};
  let cleanupTask = { ...accepted, state: 'complete', attempt: 1, confirmation,
    worktree: '/fixture/worktree', tab: 'w1:t2', pane: 'w1:p2', session: 'fixture', workspace: 'w1',
    lease: { lease_id: 'lease-1', lease_holder: 'holder-1' } };
  const cleanupFleet = { epoch: () => 0, route: () => route, call: async (_route, method, params, revision) => {
    cleanupCalls.push({ method, params, revision });
    if (method === 'status') return { remote_home: route.home, tasks: [cleanupTask] };
    if (method === 'inspect_return_lease') return { changes: ['?? generated.txt'] };
    if (method === 'inspect_cancel') return { confirmation: 'cancel-token', requires_external_attestation: true, checks: ['Fixture orphan inspection'] };
    return cleanupTask;
  } };
  registerRemoteUI({ registerCommand: (name, value) => { cleanupCommands[name] = value; } }, () => {}, cleanupFleet, () => true);
  const cleanup = async action => {
    cleanupCalls.length = 0;
    await cleanupCommands['mate-remote'].handler(`fixture ${action} check`, ctx);
    return cleanupCalls.at(-1);
  };
  approve = false;
  await cleanup('return');
  assert.ok(!cleanupCalls.some(call => call.method === 'return_lease'));
  assert.match(confirmations.at(-1).body, /generated.txt/);
  approve = true;
  let effect = await cleanup('return');
  assert.equal(effect.method, 'return_lease');
  assert.equal(effect.revision, confirmation);
  assert.deepEqual(effect.params.changes, ['?? generated.txt']);
  assert.equal(effect.params.lease_id, 'lease-1');
  effect = await cleanup('close');
  assert.equal(effect.method, 'close_tab');
  assert.equal(effect.params.tab, 'w1:t2');
  cleanupTask = { ...cleanupTask, state: 'review', scope_history: [] };
  effect = await cleanup('complete');
  assert.equal(effect.method, 'complete');
  assert.equal(effect.revision, confirmation);
  assert.equal(effect.params.scope_revision, 0);
  cleanupTask = { ...cleanupTask, state: 'awaiting-base', attempt: 0 };
  effect = await cleanup('cancel');
  assert.equal(effect.method, 'cancel');
  assert.equal(effect.params.confirmation, 'cancel-token');
  assert.equal(effect.params.attest_external, true);
  assert.match(confirmations.at(-1).body, /personally inspected/);
  cleanupTask = { ...cleanupTask, state: 'review', attempt: 1, pending_scope: { token: 'scope-token', brief: 'Additional scope' } };
  approve = false;
  effect = await cleanup('approve');
  assert.equal(effect.method, 'review_scope');
  assert.equal(effect.params.approve, false);
  assert.equal(effect.revision, confirmation);
  console.log('PASS: primary tools/dialogs + real remote control plane, model approval refusal, decline/accept, stale-scope/session refusal, durable results, independent homes, reconnect, separate cleanup confirmation payloads; fake SSH only');
} finally {
  fleet.close();
  child.stdin.end();
  await new Promise(resolveDone => { if (child.exitCode !== null) return resolveDone(); const timer = setTimeout(() => { child.kill(); resolveDone(); }, 5000); child.once('close', () => { clearTimeout(timer); resolveDone(); }); });
  lines.close();
  process.env.PATH = oldPath;
  rmSync(tmp, { recursive: true, force: true });
}
