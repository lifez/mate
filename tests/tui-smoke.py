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
    def log_message(self, *_):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
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
    (mate.HOME / 'fixture').mkdir()
    lease = dict(path=str(folder.resolve()), lease_id='fixture-lease', lease_holder='fixture-holder', status='leased')
    with db:
        mate.save(db, dict(id='fixture', attempt=1, state='launching', pane='fixture-pane', socket='fixture-socket',
            repo=str(folder.resolve()), worktree=str(folder.resolve()), holder='fixture-holder', lease=lease,
            pi_binary=shutil.which('pi'), provider='mate-fixture', model='fixture', effort='off',
            brief='Read fixture.txt then report.'))
    fakebin = folder / 'bin'; fakebin.mkdir()
    checker = fakebin / 'treehouse'
    checker.write_text('#!' + sys.executable + '\nprint(' + repr(json.dumps([lease])) + ')\n')
    checker.chmod(0o755)
    pid, terminal = pty.fork()
    if pid == 0:
        os.chdir(folder)
        env = dict(os.environ, PI_CODING_AGENT_DIR=str(agent), MATE_HOME=str(mate.HOME), TERM='xterm-256color',
                   HERDR_PANE_ID='fixture-pane', HERDR_SOCKET_PATH='fixture-socket',
                   PATH=str(fakebin) + os.pathsep + os.environ['PATH'])
        os.execve(sys.executable, [sys.executable, str(ROOT / 'bin/mate.py'), 'worker', 'fixture', '1'], env)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    output = bytearray()
    streams = {terminal: output}
    status = None
    try:
        deadline = time.monotonic() + 30
        while streams and time.monotonic() < deadline:
            for fd in select.select(list(streams), [], [], 0.1)[0]:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b''
                if not data:
                    del streams[fd]
                else:
                    streams[fd].extend(data)
        ended, status = os.waitpid(pid, os.WNOHANG)
        while ended == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
            ended, status = os.waitpid(pid, os.WNOHANG)
        assert ended == pid, 'Pi did not shut down after settled: ' + output.decode(errors='replace')[-5000:]
        assert os.waitstatus_to_exitcode(status) == 0, output.decode(errors='replace')[-4000:]
        events = (mate.HOME / 'fixture/events-1.jsonl').read_bytes()
        rows = [json.loads(line) for line in events.splitlines()]
        assert mate.load(db, 'fixture')['state'] == 'review', mate.snapshot(db, {'id': 'fixture'})
        snapshot = mate.snapshot(db, {'id': 'fixture'})
        assert snapshot['events'][0]['kind'] == 'report'
        assert 'MATE_TUI_REPORT' in snapshot['report']['text']
        assert any(e['type'] == 'tool_execution_start' and e['toolName'] == 'read' for e in rows), rows
        assert any(e['type'] == 'tool_execution_end' and not e['isError'] for e in rows), rows
        assert rows[-1]['type'] == 'agent_settled', rows[-3:]
        assert b'MATE_TUI_REPORT' in output, output.decode(errors='replace')[-4000:]
        assert b'fixture.txt' in output, 'native tool row missing from TTY'
        assert b'MATE_TUI_TOOL_RESULT' in events, 'tool result missing from bridge'
        print('PASS: real Pi native TUI renders tool/report, bridge carries tool result, settled exits; localhost fake model only')
    finally:
        if status is None or ended == 0:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        os.close(terminal)
        db.close()
        server.shutdown(); server.server_close(); thread.join(timeout=5)
