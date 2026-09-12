#!/usr/bin/env python3
"""Real supervisor TUI/control plane + captured localhost requests; no workers/credentials."""
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
BUSY = '--busy' in sys.argv
AUTOMATIC = 3 if BUSY else 2
RELEASE = threading.Event()


class Model(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append(request)
        n = len(self.requests)
        # Two ignored automatic wakes; the next HUMAN prompt must carry the
        # unresolved event again. Then inspect and acknowledge a concrete blocker.
        name, args = ('mate_status', {'id': 'fixture'}) if n == AUTOMATIC + 1 else ('mate_ack', {
            'events': [1, 2, 3] if BUSY else [1, 2], 'note': 'Fixture has no execution resources; no worker started.'})
        delta = {'content': 'OLD_APPROVAL_REPLY' if n <= AUTOMATIC else 'FIXTURE_BLOCKER_HANDLED'}
        finish = 'stop'
        if n in (AUTOMATIC + 1, AUTOMATIC + 2):
            delta = {'tool_calls': [{'index': 0, 'id': f'call-{n}', 'type': 'function',
                                    'function': {'name': name, 'arguments': json.dumps(args)}}]}
            finish = 'tool_calls'
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        if BUSY and n == 1:
            self.wfile.flush()
            assert RELEASE.wait(15), 'fixture stream was not released'
        for change, reason in [(dict(role='assistant', **delta), None), ({}, finish)]:
            chunk = {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'fixture',
                     'choices': [{'index': 0, 'delta': change, 'finish_reason': reason}]}
            self.wfile.write(('data: ' + json.dumps(chunk) + '\n\n').encode())
            self.wfile.flush()
        self.wfile.write(b'data: [DONE]\n\n')


with tempfile.TemporaryDirectory(prefix='mate-supervisor-tui-') as temporary:
    folder = Path(temporary)
    root = folder / 'mate'
    for path in ('.pi/extensions', 'bin'):
        shutil.copytree(ROOT / path, root / path)
    for path in ('SUPERVISOR.md', 'WORKER.md'):
        shutil.copyfile(ROOT / path, root / path)
    shutil.copyfile(ROOT / 'mate.config.example.json', root / 'mate.config.json')
    agent = folder / 'agent'; agent.mkdir()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    (agent / 'models.json').write_text(json.dumps({'providers': {'mate-fixture': {
        'baseUrl': f'http://127.0.0.1:{server.server_port}/v1', 'api': 'openai-completions',
        'apiKey': 'local-fixture-only', 'models': [{'id': 'fixture', 'contextWindow': 32000, 'maxTokens': 1000}]
    }}}))
    spec = importlib.util.spec_from_file_location('fixture_mate', root / 'bin/mate.py')
    mate = importlib.util.module_from_spec(spec); spec.loader.exec_module(mate)
    mate.HOME = folder / 'home'
    db = mate.connect()
    task = dict(id='fixture', state='approved', attempt=0, repo=str(root), brief='Fixture only',
                sha='0' * 40, base='main', branch='mate/fixture')
    with db:
        mate.save(db, task)
        mate.event(db, task, 'base-approved', 'Fixture approval; no execution resources.')
        mate.event(db, task, 'scope-approved-fixture', 'Synthetic scope wake for transport coverage; inspect current task.')
    # Observe actual lifecycle without changing Mate's delivery implementation.
    probe = folder / 'probe.ts'
    probe.write_text('import { appendFileSync } from "node:fs";\nexport default function(pi) {\n'
                     'pi.on("agent_settled", () => appendFileSync(process.env.FIXTURE_SETTLED, "settled\\n"));\n'
                     'pi.on("message_end", e => { if (e.message.role === "user") appendFileSync(process.env.FIXTURE_USERS, JSON.stringify(e.message) + "\\n"); });\n'
                     'pi.registerCommand("fixture-quit", {handler: async (_, ctx) => ctx.shutdown()});\n}\n')
    settled = folder / 'settled'
    pid, terminal = pty.fork()
    if pid == 0:
        os.chdir(root)
        env = dict(os.environ, PI_CODING_AGENT_DIR=str(agent), MATE_HOME=str(mate.HOME),
                   TERM='xterm-256color', FIXTURE_SETTLED=str(settled), FIXTURE_USERS=str(folder / 'users.jsonl'))
        env.pop('MATE_MODE', None)
        for key in list(env):
            if key.startswith('HERDR_'):
                env.pop(key)
        pi = shutil.which('pi')
        os.execve(pi, [pi, '--approve', '--provider', 'mate-fixture', '--model', 'fixture',
                      '--thinking', 'off', '--tui-mode', 'regular', '-e', str(probe)], env)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    output = bytearray()
    ended = 0

    def pump(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if select.select([terminal], [], [], 0.05)[0]:
                try:
                    output.extend(os.read(terminal, 65536))
                except OSError:
                    break

    def wait(check):
        for _ in range(300):
            if check():
                return
            pump(0.1)
        raise AssertionError(output.decode(errors='replace')[-5000:])

    try:
        if BUSY:
            wait(lambda: len(Model.requests) == 1)
            with db:
                mate.event(db, task, 'report', 'Fixture report arriving while model is streaming')
            pump(3)  # Let the real watcher queue the follow-up into the held run.
            RELEASE.set()
        wait(lambda: settled.exists() and len(settled.read_text().splitlines()) >= 2)
        pump(3)  # More than one durable polling interval; nextTurn must stay quiet.
        assert len(Model.requests) == AUTOMATIC, 'ignored wakes must not loop automatically'
        assert len(mate.snapshot(db, {})['events']) == (3 if BUSY else 2), 'an unrelated answer is not ack'
        for request in Model.requests:
            assert 'base-approved' in json.dumps(request) and 'scope-approved-fixture' in json.dumps(request)
        assert 'CORRECTION:' in json.dumps(Model.requests[AUTOMATIC - 1])
        if BUSY:
            assert 'Fixture report arriving' in json.dumps(Model.requests[1]), 'streaming wake lost'
        os.write(terminal, b'Human asks for current status\r')
        wait(lambda: len(Model.requests) >= AUTOMATIC + 3 and not mate.snapshot(db, {})['events'])
        pump(3)
        assert len(Model.requests) == AUTOMATIC + 3, 'ack must stop all automatic reminders'
        wire = Model.requests[AUTOMATIC]['messages']
        texts = [json.dumps(m.get('content')) for m in wire]
        old_reply = max(i for i, text in enumerate(texts) if 'OLD_APPROVAL_REPLY' in text)
        fresh_wake = [m for m in wire[old_reply + 1:] if 'MATE EVENT' in json.dumps(m)]
        assert len(fresh_wake) == 1, 'next human request needs exactly one fresh operational copy'
        assert fresh_wake[0]['role'] == 'user', 'wake attachment must be native user input'
        native = [json.loads(line) for line in (folder / 'users.jsonl').read_text().splitlines()]
        assert sum('MATE EVENT' in json.dumps(m) for m in native) == AUTOMATIC + 1, 'compaction must receive every wake through native user message_end'
        assert 'Human asks for current status' in json.dumps(native[-1]) and 'MATE EVENT' in json.dumps(native[-1])
        assert 'not typed by the human' in json.dumps(fresh_wake)
        assert any(m['role'] == 'tool' and 'fixture' in json.dumps(m) for m in Model.requests[AUTOMATIC + 1]['messages'])
        assert any(m['role'] == 'tool' and 'acknowledged' in json.dumps(m) for m in Model.requests[AUTOMATIC + 2]['messages'])
        assert mate.load(db, 'fixture')['state'] == 'approved', 'test must not launch a worker'
        os.write(terminal, b'/fixture-quit\r')
        for _ in range(100):
            pump(0.1)
            ended, status = os.waitpid(pid, os.WNOHANG)
            if ended:
                break
        assert ended and os.waitstatus_to_exitcode(status) == 0
        print('PASS:', 'busy' if BUSY else 'idle', 'real supervisor TUI, native user message_end/wire input, bounded correction, human-input attachment, real status/ack')
    finally:
        RELEASE.set()
        if not ended:
            # Only this fixture's Pi child; EOF releases its disposable control plane.
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(terminal)
        db.close()
        server.shutdown(); server.server_close(); thread.join(timeout=5)
