#!/usr/bin/env python3
"""Real Pi in a PTY + localhost fake model. No credentials, subscription or Herdr needed."""
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


class Model(BaseHTTPRequestHandler):
    requests = []
    def log_message(self, *_):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append(request)
        has_result = any(m['role'] == 'tool' for m in request['messages'])
        delta = {'content': 'MATE_TUI_REPORT: fixture read complete.'} if has_result else {
            'tool_calls': [{'index': 0, 'id': 'fixture-read', 'type': 'function',
                            'function': {'name': 'read', 'arguments': json.dumps({'path': 'fixture.txt'})}}]}
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        for change, finish in [(dict(role='assistant', **delta), None), ({}, 'stop' if has_result else 'tool_calls')]:
            chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture',
                     'choices': [{'index': 0, 'delta': change, 'finish_reason': finish}]}
            self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
            self.wfile.flush()
        self.wfile.write(b'data: [DONE]\n\n')


with tempfile.TemporaryDirectory(prefix='mate-tui-') as temporary:
    folder = Path(temporary)
    agent = folder / 'agent'; agent.mkdir()
    (folder / 'fixture.txt').write_text('MATE_TUI_TOOL_RESULT\n')
    for scope, resources in [('global', agent), ('project', folder / '.pi')]:
        extensions = resources / 'extensions'; extensions.mkdir(parents=True)
        (extensions / 'fixture.ts').write_text(
            'import { writeFileSync } from "node:fs";\n'
            'export default function () {\n'
            'if (process.env.MATE_MODE !== "dev") throw new Error("Supervisor not disabled");\n'
            f'writeFileSync({json.dumps(str(folder / (scope + "-loaded")))}, "loaded");\n'
            '}\n')
        skill = resources / 'skills' / (scope + '-fixture'); skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text(
            f'---\nname: {scope}-fixture\ndescription: MATE_{scope.upper()}_SKILL\n---\nFixture only.\n')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    (agent / 'models.json').write_text(json.dumps({'providers': {'mate-fixture': {
        'baseUrl': f'http://127.0.0.1:{server.server_port}/v1', 'api': 'openai-completions',
        'apiKey': 'local-fixture-only', 'models': [{'id': 'fixture', 'contextWindow': 32000, 'maxTokens': 1000}]
    }}}))
    # Run the actual Python worker too, using a fixture-only lease checker.
    spec = importlib.util.spec_from_file_location('mate_tui_fixture', ROOT / 'bin/mate.py')
    mate = importlib.util.module_from_spec(spec); spec.loader.exec_module(mate)
    mate.HOME = folder / 'home'
    db = mate.connect()
    installation = folder / 'installation'
    shutil.copytree(ROOT / 'bin', installation / 'bin')
    shutil.copyfile(ROOT / 'WORKER.md', installation / 'WORKER.md')
    mate.CONFIG = installation / 'mate.config.json'
    mate.CONFIG.write_text(json.dumps({'worker': {'max_active': 2}}))
    (mate.HOME / 'fixture').mkdir()
    lease = dict(path=str(folder.resolve()), lease_id='fixture-lease', lease_holder='fixture-holder', status='leased')
    with db:
        mate.save(db, dict(id='fixture', attempt=1, state='launching', pane='fixture-pane', socket='fixture-socket',
            repo=str(folder.resolve()), worktree=str(folder.resolve()), holder='fixture-holder', lease=lease,
            pi_binary=shutil.which('pi'), provider='mate-fixture', model='fixture', effort='off',
            brief='Read fixture.txt then report.', session='fixture', workspace='fixture-workspace',
            tab='fixture-tab', terminal_id='fixture-terminal', sha='fixture-sha'))
    fakebin = folder / 'bin'; fakebin.mkdir()
    checker = fakebin / 'treehouse'
    checker.write_text('#!' + sys.executable + '\nprint(' + repr(json.dumps([lease])) + ')\n')
    checker.chmod(0o755)
    herdr = fakebin / 'herdr'
    herdr.write_text('#!' + sys.executable + '\nprint(' + repr(json.dumps({'result': {'pane': {
        'pane_id': 'fixture-pane', 'workspace_id': 'fixture-workspace', 'tab_id': 'fixture-tab', 'terminal_id': 'fixture-terminal'}}})) + ')\n')
    herdr.chmod(0o755)
    prior_path = os.environ['PATH']
    os.environ['PATH'] = str(fakebin) + os.pathsep + prior_path
    pid, terminal = pty.fork()
    if pid == 0:
        os.chdir(folder)
        env = dict(os.environ, PI_CODING_AGENT_DIR=str(agent), MATE_HOME=str(mate.HOME), TERM='xterm-256color',
                   HERDR_PANE_ID='fixture-pane', HERDR_SOCKET_PATH='fixture-socket',
                   PATH=str(fakebin) + os.pathsep + os.environ['PATH'])
        os.execve(sys.executable, [sys.executable, str(installation / 'bin/mate.py'), 'worker', 'fixture', '1'], env)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    output = bytearray()
    streams = {terminal: output}
    status = None
    ended = 0
    def wait(check, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for fd in select.select(list(streams), [], [], 0.05)[0]:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b''
                if not data:
                    del streams[fd]
                else:
                    streams[fd].extend(data)
            if check(): return
        raise AssertionError('Timed out: ' + output.decode(errors='replace')[-5000:] + '\n' + str(mate.load(db, 'fixture')))
    def reviewed(attempt):
        task = mate.load(db, 'fixture')
        return task['state'] == 'review' and task['attempt'] == attempt
    try:
        wait(lambda: reviewed(1) and b'Mate report saved' in output)
        assert os.waitpid(pid, os.WNOHANG)[0] == 0, 'Pi must stay open after settled'
        assert mate.resident_alive(mate.load(db, 'fixture'))
        assert not mate.worker_alive(mate.load(db, 'fixture')), 'idle Pi releases only the round lock'
        events = (mate.HOME / 'fixture/events-1.jsonl').read_bytes()
        rows = [json.loads(line) for line in events.splitlines()]
        assert mate.load(db, 'fixture')['state'] == 'review', mate.snapshot(db, {'id': 'fixture'})
        snapshot = mate.snapshot(db, {'id': 'fixture'})
        assert snapshot['events'][0]['kind'] == 'report'
        assert 'MATE_TUI_REPORT' in snapshot['report']['text']
        assert snapshot['tasks'][0]['usage_total']['messages'] == 2, 'count finals, not streaming updates'
        assert snapshot['attempt_usage']['cost_reported_messages'] == 2
        assert snapshot['attempt_usage']['estimated_cost_usd'] == 0, 'localhost fixture has zero catalog pricing'
        assert any(e['type'] == 'tool_execution_start' and e['toolName'] == 'read' for e in rows), rows
        assert any(e['type'] == 'tool_execution_end' and not e['isError'] for e in rows), rows
        assert rows[-1]['type'] == 'agent_settled', rows[-3:]
        for scope in ('global', 'project'):
            assert (folder / (scope + '-loaded')).is_file(), scope + ' extension missing'
            assert 'MATE_' + scope.upper() + '_SKILL' in json.dumps(Model.requests), scope + ' skill missing'
        assert b'MATE_TUI_REPORT' in output, output.decode(errors='replace')[-4000:]
        assert b'fixture.txt' in output, 'native tool row missing from TTY'
        assert b'MATE_TUI_TOOL_RESULT' in events, 'tool result missing from bridge'
        first_report = snapshot['report']['text']
        # Exact generation/attempt fences: stale commands never inject a prompt.
        task = mate.load(db, 'fixture')
        for wrong in [dict(task, attempt=99), dict(task, worker_control=dict(task['worker_control'], generation='stale'))]:
            try:
                mate.worker_control(wrong, 'shutdown')
                raise AssertionError('stale control accepted')
            except ValueError: pass
        assert reviewed(1)
        os.write(terminal, b'/reload\r')
        wait(lambda: b'Reloaded keybindings' in output)
        assert reviewed(1), 'reload must not create another attempt'
        # Native pane follow-up: another attempt in the SAME live Pi session.
        os.write(terminal, b'Explain the fixture once more.\r')
        wait(lambda: reviewed(2))
        assert os.waitpid(pid, os.WNOHANG)[0] == 0
        assert mate.snapshot(db, {'id': 'fixture', 'attempt': 1})['report']['text'] == first_report
        assert mate.snapshot(db, {'id': 'fixture', 'attempt': 2})['attempt_usage']['messages'] == 1
        assert len([e for e in mate.snapshot(db, {})['events'] if e['kind'] == 'report']) == 2
        # A stale completion dialog cannot accept a later native round.
        try:
            mate.complete(db, dict(id='fixture', attempt=1))
            raise AssertionError('stale confirmation accepted')
        except ValueError: pass
        pending = mate.propose_scope(db, dict(id='fixture', brief='Also explain the marker.'))['pending_scope']
        requests = len(Model.requests)
        os.write(terminal, b'Do the pending addition now.\r')
        wait(lambda: b'Additional scope awaits' in output)
        assert len(Model.requests) == requests and reviewed(2), 'pending scope gates direct input too'
        mate.review_scope(db, dict(id='fixture', attempt=2, sha='fixture-sha', token=pending['token'], approve=True))
        mate.resume(db, dict(id='fixture', message='Explain the newly approved marker.', effort='off'))
        wait(lambda: reviewed(3))
        assert os.waitpid(pid, os.WNOHANG)[0] == 0
        assert 'Also explain the marker.' in json.dumps(Model.requests[-1]), 'updated scope reaches the resident Pi'
        assert mate.snapshot(db, {'id': 'fixture'})['tasks'][0]['usage_total']['messages'] == 4
        # Human acceptance stops Pi gracefully, without closing its tab or returning a lease.
        # Keep draining the PTY while completion waits for graceful Pi shutdown.
        completion = []
        def accept():
            connection = mate.connect()
            try: completion.append(mate.complete(connection, dict(id='fixture', attempt=3, scope_revision=1)))
            finally: connection.close()
        accepting = threading.Thread(target=accept, daemon=True); accepting.start()
        wait(lambda: bool(completion))
        accepting.join(timeout=1)
        completed = completion[0]
        assert completed['state'] == 'complete' and not completed.get('worker_stop_error'), completed
        wait(lambda: not streams)
        ended, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0, output.decode(errors='replace')[-4000:]
        assert mate.load(db, 'fixture')['state'] == 'complete'
        assert not mate.resident_alive(mate.load(db, 'fixture'))
        assert len([e for e in mate.snapshot(db, {})['events'] if e['kind'] == 'report']) == 3
        print('PASS: persistent real Pi, native + supervisor continuation, scope/stale gates, per-round reports/usage, human completion exits; localhost model only')
    except BaseException:
        import traceback
        traceback.print_exc()
        print(output.decode(errors='replace')[-5000:], file=sys.stderr, flush=True)
        raise
    finally:
        if status is None or ended == 0:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        os.close(terminal)
        os.environ['PATH'] = prior_path
        db.close()
        server.shutdown(); server.server_close(); thread.join(timeout=5)
