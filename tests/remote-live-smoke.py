#!/usr/bin/env python3
"""Opt-in real Herdr + real Pi + real Treehouse; localhost model only.
Run --local or --host SSH_ALIAS. Owns a disposable named server/home/repo.
No personal credentials/settings or default Herdr sessions are modified.
"""
import argparse
from contextlib import closing
import io
import json
import os
from pathlib import Path
import re
import select
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import mate_remote as inbox
import mate_remote_primary as primary
import mate_remote_events as events
import mate_remote_transport as transport


class Model(BaseHTTPRequestHandler):
    count = 0
    def log_message(self, *_): pass
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        Model.count += 1
        supervisor = any(t.get('function', {}).get('name') == 'mate_status' for t in request.get('tools', []))
        messages = request['messages']
        last_user = max((i for i, m in enumerate(messages) if m['role'] == 'user'), default=-1)
        tools = [m for m in messages[last_user+1:] if m['role'] == 'tool']
        name, args = None, None
        if supervisor:
            result = None
            if tools:
                content = tools[-1].get('content', '')
                if isinstance(content, list): content = '\n'.join(p.get('text', '') for p in content)
                try: result = json.loads(content)
                except (ValueError, TypeError): pass
            if not tools or not isinstance(result, dict): name, args = 'mate_status', {}
            elif 'tasks' in result:
                approved = next((t for t in result['tasks'] if t['state'] == 'approved'), None)
                if approved: name, args = 'mate_dispatch', dict(id=approved['id'], model='mate-fixture/fixture', effort='off')
                elif result.get('events'): name, args = 'mate_ack', dict(events=[e['id'] for e in result['events']], note='Fixture supervisor handled wake; primary independently reads reports.')
            elif 'state' in result: name, args = 'mate_status', {}
        elif not tools:
            name, args = 'read', {'path': 'fixture.txt'}
        delta = {'content': 'MATE_REMOTE_REPORT: fixture verified.'}
        reason = 'stop'
        if name:
            delta = {'tool_calls': [{'index': 0, 'id': 'fixture-' + str(Model.count), 'type': 'function',
                                    'function': {'name': name, 'arguments': json.dumps(args)}}]}
            reason = 'tool_calls'
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
        for change, finish in [(dict(role='assistant', **delta), None), ({}, reason)]:
            chunk = dict(id='fixture', object='chat.completion.chunk', created=1, model='fixture',
                         choices=[dict(index=0, delta=change, finish_reason=finish)])
            self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode()); self.wfile.flush()
        self.wfile.write(b'data: [DONE]\n\n')


def service(folder):
    folder = Path(folder).resolve()
    install = ROOT
    home = folder/'home'; home.mkdir(mode=0o700)
    agent = folder/'agent'; agent.mkdir()
    repo = folder/'repo'; repo.mkdir()
    fakebin = folder/'fixture-bin'; fakebin.mkdir()
    (fakebin/'quota-axi').write_text('#!/bin/sh\nprintf \'{"providers":[]}\\n\'\n'); (fakebin/'quota-axi').chmod(0o700)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    (agent/'models.json').write_text(json.dumps({'providers': {'mate-fixture': {
        'baseUrl': f'http://127.0.0.1:{server.server_port}/v1', 'api': 'openai-completions',
        'apiKey': 'local-fixture-only', 'models': [{'id': 'fixture', 'contextWindow': 64000, 'maxTokens': 1000}]}}}))
    (agent/'extensions').mkdir()
    (agent/'extensions/fixture.ts').write_text('export default function(pi) { pi.registerCommand("fixture-quit", {handler: async (_,ctx) => ctx.shutdown()}); }\n')
    config = dict(home=str(uuid.uuid4()), primary=str(uuid.uuid4()), journal=str(home/'inbox.sqlite3'),
                  supervisor=dict(provider='mate-fixture', model='fixture', effort='off'))
    inbox.create(config['journal'], config['home'], config['primary'])
    role = home/'remote.json'; role.write_text(json.dumps(config)); role.chmod(0o600)
    (install/'mate.config.json').write_text(json.dumps({'worker': {'model': 'mate-fixture/fixture', 'effort': 'off', 'workspace_per_task': True}}))
    herdr_config = folder/'herdr.toml'
    herdr_config.write_text('[terminal]\ndefault_shell = "/bin/sh"\nshell_mode = "non_login"\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith(('MATE_', 'HERDR_', 'PI_'))}
    env.update(PI_CODING_AGENT_DIR=str(agent), HERDR_CONFIG_PATH=str(herdr_config), TERM='xterm-256color', PATH=str(fakebin)+os.pathsep+env['PATH'])
    def run(args, **kwargs):
        return subprocess.run(args, env=env, text=True, capture_output=True, check=True, timeout=30, **kwargs).stdout.strip()
    def git(*args): return run(['git','-C',str(repo),*args])
    git('init','-b','main')
    (repo/'fixture.txt').write_text('REMOTE_FIXTURE_CONTENT\n')
    (repo/'treehouse.toml').write_text('max_trees = 1\nroot = "'+str(folder/'pool')+'"\n')
    git('add','fixture.txt','treehouse.toml')
    git('-c','user.name=Mate Test','-c','user.email=mate@test.invalid','commit','-m','fixture')
    session = 'mate-remote-' + config['home'].replace('-','')
    logfile = (folder/'herdr.log').open('w')
    child = subprocess.Popen(['herdr','--session',session,'server'], env=env, stdin=subprocess.DEVNULL, stdout=logfile, stderr=logfile, start_new_session=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        for _ in range(100):
            if child.poll() is not None: raise RuntimeError('Fixture Herdr exited')
            try:
                status = json.loads(run(['herdr','--session',session,'status','--json']))
                if status['server']['running']: break
            except Exception: pass
            time.sleep(.1)
        else: raise RuntimeError('Fixture Herdr not ready')
        remote_env = {key: env[key] for key in ('PI_CODING_AGENT_DIR','HERDR_CONFIG_PATH','TERM','PATH')}
        print(json.dumps(dict(config=config, config_path=str(role), root=str(install), repo=str(repo), env=remote_env)), flush=True)
        for line in sys.stdin:
            action = json.loads(line)['action']
            if action == 'close': break
            if action == 'quit-supervisor':
                with closing(inbox.connect(config['journal'], config['home'], config['primary'])) as queue:
                    endpoint = events.get(queue, 'secondmate')
                # Test-only simulated human command to our exact fixture Pi.
                run(['herdr','--session',session,'pane','run',endpoint['pane'],'/fixture-quit'])
            elif action != 'stats': raise ValueError('Unknown fixture action')
            print(json.dumps(dict(model_requests=Model.count)), flush=True)
    finally:
        subprocess.run(['herdr','--session',session,'server','stop'], env=env, capture_output=True, timeout=15)
        try: child.wait(timeout=10)
        except subprocess.TimeoutExpired: child.kill(); child.wait()
        logfile.close(); server.shutdown(); server.server_close(); thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--host'); group.add_argument('--local', action='store_true'); group.add_argument('--service')
    args = parser.parse_args()
    if args.service: service(args.service); return
    if args.host and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,252}', args.host): raise ValueError('Use a configured SSH alias')
    ssh = [shutil.which('ssh'), '-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ForwardAgent=no','-o','ConnectTimeout=10','--',args.host] if args.host else None
    if ssh:
        preflight = subprocess.run(ssh+['true'], capture_output=True, text=True, timeout=15)
        if preflight.returncode:
            raise SystemExit('SSH preflight failed; no fixture created: '+preflight.stderr.strip())
    local = Path(tempfile.mkdtemp(prefix='mate-remote-driver-'))
    remote = local/'fixture'
    if ssh:
        remote = Path(subprocess.run(ssh+['mktemp -d /tmp/mate-remote-e2e.XXXXXX'], capture_output=True, text=True, check=True, timeout=15).stdout.strip())
        if not re.fullmatch(r'/tmp/mate-remote-e2e\.[A-Za-z0-9]+', str(remote)): raise ValueError('Unexpected fixture path')
    else: remote.mkdir()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        for relative in ('bin', '.pi/extensions', 'SUPERVISOR.md', 'WORKER.md', 'tests/remote-live-smoke.py'):
            tar.add(ROOT/relative, arcname='install/'+relative, filter=lambda item: None if '__pycache__' in item.name else item)
    if ssh:
        subprocess.run(ssh+['tar -xf - -C '+shlex.quote(str(remote))], input=archive.getvalue(), check=True, timeout=30)
    else:
        with tarfile.open(fileobj=io.BytesIO(archive.getvalue())) as tar: tar.extractall(remote, filter='data')
    start = ['python3', str(remote/'install/tests/remote-live-smoke.py'), '--service', str(remote)]
    with (local/'service.log').open('w') as service_log:
        proc = subprocess.Popen(ssh+[shlex.join(start)] if ssh else start, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=service_log, text=True)
    def line():
        if not select.select([proc.stdout], [], [], 60)[0]: raise RuntimeError('Fixture service timeout; inspect '+str(local))
        text = proc.stdout.readline()
        if not text: raise RuntimeError('Fixture service exited; inspect '+str(local))
        return json.loads(text)
    db = None; success = False
    try:
        info = line(); config = info['config']
        route = dict(host=args.host or 'local-fixture', home=config['home'], primary=config['primary'], outbox=str(local/'outbox'))
        primary.create(route); db = primary.connect(route)
        wrapper = local/'ssh-fixture'
        receiver = ['env', *[f'{k}={v}' for k,v in info['env'].items()], 'python3', str(remote/'install/bin/mate_remote_transport.py'), 'receive', info['config_path']]
        if ssh:
            code = 'import os,sys\nos.execv('+repr(ssh[0])+', ['+repr(ssh[0])+']+sys.argv[1:-1]+['+repr(shlex.join(receiver))+'])\n'
        else:
            code = 'import os\nos.execv("/usr/bin/env", '+repr(receiver)+')\n'
        wrapper.write_text('#!'+sys.executable+'\n'+code); wrapper.chmod(0o700)
        def send(method, params): return transport.send(route, method, params, timeout=90, ssh=str(wrapper))
        def call(method, params, confirmation=None):
            request = dict(version=1,home=route['home'],primary=route['primary'],id=str(uuid.uuid4()),body=dict(method=method,params=params))
            if confirmation is not None: request['body']['confirmation']=confirmation
            primary.stage(db,request)
            delivered = primary.deliver(db,request['id'],send)
            assert delivered['state']=='accepted', delivered
            deadline=time.monotonic()+90
            while time.monotonic()<deadline:
                result=primary.execution(db,request['id'],send)
                if result['state']=='done':
                    assert result['outcome']['ok'], result
                    return result['outcome']['result']
                time.sleep(.3)
            raise RuntimeError('Remote operation did not settle: '+request['id'])
        doctor=send('doctor',{})['result']; assert doctor['ready'],doctor
        boot=call('secondmate_start',{},doctor['confirmation']); assert boot['state']=='running',boot
        again=send('doctor',{})['result']
        assert call('secondmate_start',{},again['confirmation'])['endpoint']['pane']==boot['endpoint']['pane']
        proposed=call('propose',dict(id='fixture',repo=info['repo'],base='main',brief='Read fixture.txt then report. Fixture only; no writes, network, push or merge.'))
        assert proposed['state']=='awaiting-base' and proposed['attempt']==0
        assert any(e['kind']=='approval-needed' for e in events.mirror(db,send)['events'])
        call('approve',dict(id='fixture',sha=proposed['sha'],brief=proposed['brief']),proposed['confirmation'])  # Explicit synthetic fixture approval, never real task authority.
        def task(): return call('status',dict(id='fixture',history=True))
        def reviewed(attempt):
            deadline=time.monotonic()+90
            while time.monotonic()<deadline:
                snapshot=task(); row=snapshot['tasks'][0]
                if row['state']=='review' and row['attempt']==attempt: return snapshot
                if row['state'] in ('attention','failed'): raise AssertionError(row)
                time.sleep(.4)
            raise RuntimeError('Worker did not reach review')
        first=reviewed(1); row=first['tasks'][0]
        assert 'MATE_REMOTE_REPORT' in first['report']['text'] and row.get('worker_control'), first
        lease=row['lease']; pane=row['pane']
        assert any(e['kind']=='report' for e in events.mirror(db,send)['events'])
        call('resume',dict(id='fixture',message='Read fixture.txt once more within the same approved scope.'))
        second=reviewed(2); row=second['tasks'][0]
        assert row['pane']==pane and row['lease']==lease and 'MATE_REMOTE_REPORT' in second['report']['text']
        call('complete',dict(id='fixture',attempt=2,scope_revision=0),row['confirmation'])
        row=task()['tasks'][0]; assert not row.get('worker_control'),row
        call('close_tab',dict(id='fixture',attempt=2,tab=row['tab']),row['confirmation'])
        row=task()['tasks'][0]
        params=dict(id='fixture',attempt=2,worktree=row['worktree'],lease_id=lease['lease_id'],lease_holder=lease['lease_holder'])
        assert call('inspect_return_lease',params)['changes']==[]
        call('return_lease',params,row['confirmation'])
        assert task()['tasks'][0]['lease_return_state']=='returned'
        mirrored=events.mirror(db,send)
        events.acknowledge(db,[e['id'] for e in mirrored['events']],'Fixture evidence inspected')
        assert events.mirror(db,send)['events']==[]
        # Graceful fixture-only quit followed by exact-shell recovery of secondmate.
        time.sleep(3)
        proc.stdin.write(json.dumps({'action':'quit-supervisor'})+'\n');proc.stdin.flush();line()
        time.sleep(3)
        doctor=send('doctor',{})['result']
        recovered=call('secondmate_recover',{},doctor['confirmation'])
        assert recovered['endpoint']['pane']==boot['endpoint']['pane']
        assert task()['tasks'][0]['state']=='complete'
        success=True
        print('PASS: '+('real SSH to '+args.host if ssh else 'local receiver boundary')+'; real Herdr/Pi/Treehouse, secondmate auto-dispatch, durable report/mirror/ack, resident continuation, human-fixture completion/exact cleanup and stopped-supervisor recovery; localhost model only')
    finally:
        if db: db.close()
        try: proc.stdin.write('{"action":"close"}\n');proc.stdin.flush()
        except (BrokenPipeError,OSError): pass
        try: proc.wait(timeout=30)
        except subprocess.TimeoutExpired: proc.kill();proc.wait()
        proc.stdin.close();proc.stdout.close()
        if success:
            if ssh: subprocess.run(ssh+['rm -r -- '+shlex.quote(str(remote))],check=True,timeout=30)
            shutil.rmtree(local)
        else: print('Fixture diagnostics retained:',local,'remote:',remote,file=sys.stderr)


if __name__=='__main__':
    os.umask(0o077)
    main()
