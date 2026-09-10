"""Run: python3 -m unittest discover -s tests -v (no network/model/real Herdr)."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mate", ROOT / "bin/mate.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class MateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mate-test-")
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.env = patch.dict(os.environ, {"MATE_HOME": str(self.home), "HERDR_ENV": "1", "HERDR_SESSION": "mate-test",
                                          "HERDR_SOCKET_PATH": str(self.root / "herdr.sock"), "HERDR_PANE_ID": "w1:p1", "HERDR_WORKSPACE_ID": "w1"})
        self.env.start()
        self.old_home = m.HOME
        m.HOME = self.home
        self.db = m.connect()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        m.git(self.repo, "init", "-b", "main")
        m.git(self.repo, "-c", "user.name=Mate Test", "-c", "user.email=mate@test.invalid", "commit", "--allow-empty", "-m", "base")
        self.sha = m.git(self.repo, "rev-parse", "HEAD")
        self.acquires = 0
        self.launches = 0
        self.real_run = m.run
        self.leases = []

    def tearDown(self):
        self.db.close()
        m.HOME = self.old_home
        self.env.stop()
        self.tmp.cleanup()

    def propose(self, ident="fix"):
        return m.propose(self.db, dict(id=ident, repo=str(self.repo), base="main", brief="Investigate locally; report evidence. Do not push."))

    def fake_run(self, args, cwd=None, timeout=30):
        if args[:2] == ["treehouse", "get"]:
            self.acquires += 1
            wt = self.root / f"worktree-{self.acquires}"
            m.git(self.repo, "worktree", "add", "-b", f"pool-{self.acquires}", str(wt), "HEAD")
            lease = dict(path=str(wt), lease_id=f"lease-{self.acquires}", lease_holder=args[-1])
            self.leases.append(dict(lease, status="leased"))
            return json.dumps(lease)
        if args[:2] == ["treehouse", "status"]:
            return json.dumps(self.leases)
        return self.real_run(args, cwd, timeout)

    def fake_herdr(self, task, *args):
        if args[:2] == ("pane", "get"):
            return {"pane": {"pane_id": args[2], "workspace_id": "w1", "tab_id": task.get("tab", "w1:t1")}}
        if args[:2] == ("tab", "create"):
            return {"tab": {"tab_id": "w1:t2"}, "root_pane": {"pane_id": "w1:p2"}}
        if args[:2] == ("pane", "run"):
            self.launches += 1
            return {}
        raise AssertionError(args)

    def dispatch(self, **overrides):
        params = dict(id="fix", provider="openai-codex", model="test-model")
        params.update(overrides)
        return m.dispatch(self.db, params)

    def test_profile_overrides_persist_and_continue_without_reacquire(self):
        self.propose()
        m.approve(self.db, dict(id="fix", sha=self.sha))
        with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", self.fake_herdr):
            with self.assertRaisesRegex(ValueError, "effort"):
                self.dispatch(effort="ultra")
            self.assertEqual(self.acquires, 0)
            task = self.dispatch(model="worker-model", effort="high")
            self.assertEqual(self.dispatch(model="ignored", effort="low")["effort"], "high")
            self.db.close()
            self.db = m.connect()
            self.assertEqual(m.snapshot(self.db, {})["tasks"][0]["model"], "worker-model")
            task["state"] = "review"
            with self.db:
                m.save(self.db, task)
            continued = m.resume(self.db, dict(id="fix", message="Same scope"))
            self.assertEqual((continued["model"], continued["effort"]), ("worker-model", "high"))
            continued["state"] = "review"
            with self.db:
                m.save(self.db, continued)
            changed = m.resume(self.db, dict(id="fix", message="Same scope", model="other-model", effort="low"))
            self.assertEqual((changed["provider"], changed["model"], changed["effort"]), ("openai-codex", "other-model", "low"))
            self.assertEqual((changed["worktree"], changed["sha"]), (task["worktree"], self.sha))
            self.assertEqual(self.acquires, 1)

    def test_approval_pin_isolation_idempotency_and_lease_identity(self):
        task = self.propose()
        with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", self.fake_herdr):
            self.assertEqual(self.dispatch()["state"], "awaiting-base")
            self.assertEqual(self.acquires, 0)
            with self.assertRaises(ValueError):
                m.approve(self.db, dict(id="fix", sha="wrong"))
            m.approve(self.db, dict(id="fix", sha=self.sha))
            m.git(self.repo, "-c", "user.name=Mate Test", "-c", "user.email=mate@test.invalid", "commit", "--allow-empty", "-m", "branch moved")
            new_sha = m.git(self.repo, "rev-parse", "HEAD")
            task = self.dispatch()
            self.assertEqual(m.git(task["worktree"], "rev-parse", "HEAD"), self.sha)
            self.assertEqual(m.git(self.repo, "rev-parse", "HEAD"), new_sha)
            self.assertEqual(m.git(self.repo, "rev-parse", "pool-1"), new_sha)
            self.dispatch()
            self.assertEqual((self.acquires, self.launches), (1, 1))
            self.leases[0]["lease_holder"] = "foreign"
            with self.assertRaises(ValueError):
                m.check_lease(task)

    def test_uncertain_acquire_is_preserved_not_retried(self):
        self.propose()
        m.approve(self.db, dict(id="fix", sha=self.sha))
        with patch.object(m, "run", side_effect=RuntimeError("ambiguous acquire")), patch.object(m, "herdr", self.fake_herdr):
            with self.assertRaises(RuntimeError):
                self.dispatch()
        self.assertEqual(m.load(self.db, "fix")["state"], "attention")
        self.assertEqual(self.dispatch()["state"], "attention")
        with self.assertRaises(ValueError):
            m.resume(self.db, dict(id="fix", message="Retry"))
        self.assertEqual(len(m.snapshot(self.db, {})["events"]), 1)

    def test_durable_events_ack_is_atomic_and_not_completion(self):
        task = self.propose()
        with self.db:
            m.event(self.db, task, "blocked", "Question")
            m.event(self.db, task, "blocked", "duplicate")
        self.db.close()
        self.db = m.connect()
        events = m.snapshot(self.db, {})["events"]
        self.assertEqual(len(events), 1)
        ident = events[0]["id"]
        with self.assertRaises(ValueError):
            m.acknowledge(self.db, dict(events=[ident, 99999], note="handled"))
        self.assertEqual(len(m.snapshot(self.db, {})["events"]), 1)
        m.acknowledge(self.db, dict(events=[ident], note="Asked user the question"))
        m.acknowledge(self.db, dict(events=[ident], note="replayed ack"))
        self.assertFalse(m.snapshot(self.db, {})["events"])
        self.assertEqual(m.load(self.db, "fix")["state"], "awaiting-base")

    def test_complete_requires_review_stopped_worker_and_exact_attempt(self):
        task = self.propose()
        (self.home / 'fix').mkdir()
        for state in ('awaiting-base', 'approved', 'acquiring', 'launching', 'running', 'failed', 'attention'):
            task.update(state=state, attempt=1)
            with self.db:
                m.save(self.db, task)
            with self.assertRaises(ValueError):
                m.complete(self.db, dict(id='fix', attempt=1))
        task.update(state='review')
        with self.db:
            m.save(self.db, task)
            m.event(self.db, task, 'report', 'Unacknowledged report')
        with self.assertRaises(ValueError):
            m.complete(self.db, dict(id='fix', attempt=2))
        guard = m.lock(self.home / 'fix/run.lock')
        try:
            with self.assertRaisesRegex(ValueError, 'still active'):
                m.complete(self.db, dict(id='fix', attempt=1))
        finally:
            guard.close()
        with patch.object(m, 'run', side_effect=AssertionError('No Git/Treehouse operations')), patch.object(m, 'herdr', side_effect=AssertionError('No pane operations')):
            completed = m.complete(self.db, dict(id='fix', attempt=1))
            self.assertEqual(completed['state'], 'complete')
            self.assertTrue(completed['completed_by'])
            self.assertEqual(completed['completed_via'], 'mate-complete')
            self.assertGreater(completed['completed_at'], 0)
            self.assertEqual(m.complete(self.db, dict(id='fix', attempt=1)), completed)
            self.assertEqual(self.dispatch()['state'], 'complete')
            with self.assertRaises(ValueError):
                m.resume(self.db, dict(id='fix', message='Cannot reopen'))
        self.db.close(); self.db = m.connect()
        snapshot = m.snapshot(self.db, {})
        self.assertEqual(snapshot['tasks'][0]['completed_at'], completed['completed_at'])
        self.assertEqual(len(snapshot['events']), 1, 'completion must not swallow pending reports')

    def test_crashed_worker_requires_inspection_not_duplicate_resume(self):
        task = self.propose()
        (self.home / "fix").mkdir()
        task.update(state="running", attempt=1)
        with self.db:
            m.save(self.db, task)
        with patch.object(m.time, "time", return_value=time.time() + 120):
            m.reconcile(self.db)
        self.assertEqual(m.load(self.db, "fix")["state"], "attention")
        with self.assertRaises(ValueError):
            m.resume(self.db, dict(id="fix", message="retry"))
        with self.assertRaisesRegex(ValueError, "uncertain task"):
            m.check_capacity(self.db)

    def test_stalled_live_worker_alerts_once_without_stopping(self):
        task = self.propose()
        (self.home / "fix").mkdir()
        task.update(state="running", attempt=1)
        with self.db:
            m.save(self.db, task)
        handle = m.lock(self.home / "fix" / "run.lock")
        try:
            with patch.object(m.time, "time", return_value=time.time() + 1000):
                m.reconcile(self.db)
                m.reconcile(self.db)
            self.assertEqual(m.load(self.db, "fix")["state"], "running")
            self.assertEqual([e["kind"] for e in m.snapshot(self.db, {})["events"]], ["stalled"])
        finally:
            handle.close()

    def test_task_input_and_concurrency_gate(self):
        with self.assertRaises(ValueError):
            self.propose("../bad")
        with self.assertRaises(ValueError):
            m.propose(self.db, dict(id="bad", repo=str(self.repo), base="--help", brief="test"))
        self.propose()
        m.approve(self.db, dict(id="fix", sha=self.sha))
        for ident in ("one", "two"):
            task = self.propose(ident)
            task["state"] = "running"
            with self.db:
                m.save(self.db, task)
        with self.assertRaisesRegex(ValueError, "Two workers"):
            self.dispatch()

    def test_supervisor_lock_and_restart_snapshot(self):
        self.propose()
        owner = m.lock(self.home / "supervisor.lock")
        with self.assertRaises(BlockingIOError):
            m.lock(self.home / "supervisor.lock")
        owner.close()
        child = subprocess.Popen([sys.executable, str(ROOT / "bin/mate.py"), "serve"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertTrue(json.loads(child.stdout.readline())["ready"])
            child.stdin.write(json.dumps(dict(id=1, method="status")) + "\n")
            child.stdin.flush()
            self.assertEqual(json.loads(child.stdout.readline())["result"]["tasks"][0]["id"], "fix")
            child.stdin.close()
            child.wait(timeout=5)
            self.assertEqual(child.returncode, 0, child.stderr.read())
        finally:
            if child.poll() is None:
                child.kill(); child.wait()
            child.stdout.close(); child.stderr.close()

    def test_worker_report_and_zero_exit_provider_error(self):
        self.propose()
        m.approve(self.db, dict(id="fix", sha=self.sha))
        with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", self.fake_herdr):
            task = self.dispatch(model="selected-model", effort="high")
        fakebin = self.root / "bin"
        fakebin.mkdir()
        for name, content in {
            "treehouse": "#!/usr/bin/env python3\nimport json\nprint(" + repr(json.dumps(self.leases)) + ")\n",
            "pi": "#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\nPath(os.environ['MATE_HOME'], 'argv.json').write_text(json.dumps(sys.argv))\nerror=os.environ.get('TEST_PROVIDER_ERROR')\nprint('Native Pi terminal output')\nf=os.fdopen(int(os.environ['MATE_EVENT_FD']), 'w')\nprint(json.dumps({'type':'message_end','message':{'role':'assistant','stopReason':'error' if error else 'stop','errorMessage':'quota' if error else '', 'content':[{'type':'text','text':'Evidence: checked fixture.'}]}}), file=f)\nif not os.environ.get('TEST_NO_SETTLED'): print(json.dumps({'type':'agent_settled'}), file=f)\nf.close()\nif os.environ.get('TEST_KILL_PI'): os.kill(os.getpid(), 9)\n"
        }.items():
            path = fakebin / name
            path.write_text(content); path.chmod(0o755)
        task["pi_binary"] = str(fakebin / "pi")
        with self.db:
            m.save(self.db, task)
        env = dict(os.environ, PATH=str(fakebin) + os.pathsep + os.environ["PATH"], HERDR_PANE_ID=task["pane"])
        command = [sys.executable, str(ROOT / "bin/mate.py"), "worker", "fix", "1"]
        output = subprocess.run(command, env=env, cwd=task["worktree"], capture_output=True, text=True, check=True, timeout=10)
        self.assertIn('Native Pi terminal output', output.stdout)
        self.assertEqual(m.load(self.db, "fix")["state"], "review")
        argv = json.loads((self.home / "argv.json").read_text())
        self.assertEqual(argv[argv.index("--model") + 1], "selected-model")
        self.assertEqual(argv[argv.index("--thinking") + 1], "high")
        self.assertNotIn('-p', argv)
        self.assertNotIn('--mode', argv)
        self.assertEqual(argv[argv.index('--tui-mode') + 1], 'regular')
        self.assertEqual(argv[argv.index('-e') + 1], str(ROOT / 'bin/worker-events.ts'))
        self.assertNotIn('Native Pi terminal output', (self.home / 'fix/events-1.jsonl').read_text())
        self.assertIn("Evidence", m.snapshot(self.db, {"id": "fix"})["report"]["text"])
        duplicate = subprocess.run(command, env=env, cwd=task["worktree"], capture_output=True, timeout=10)
        self.assertNotEqual(duplicate.returncode, 0)
        with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", self.fake_herdr):
            m.resume(self.db, dict(id="fix", message="Recheck the same scope"))
        command[-1] = "2"
        env["TEST_PROVIDER_ERROR"] = "1"
        subprocess.run(command, env=env, cwd=task["worktree"], capture_output=True, check=True, timeout=10)
        self.assertEqual(m.load(self.db, "fix")["state"], "failed")
        argv = json.loads((self.home / "argv.json").read_text())
        self.assertEqual(argv[argv.index("--model") + 1], "selected-model")
        self.assertEqual(argv[argv.index("--thinking") + 1], "high")
        self.assertIn("quota", m.snapshot(self.db, {"id": "fix"})["report"]["text"])
        with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", self.fake_herdr):
            m.resume(self.db, dict(id='fix', message='Check missing bridge completion'))
        command[-1] = '3'
        env.pop('TEST_PROVIDER_ERROR')
        env['TEST_NO_SETTLED'] = '1'
        subprocess.run(command, env=env, cwd=task['worktree'], capture_output=True, check=True, timeout=10)
        self.assertEqual(m.load(self.db, 'fix')['state'], 'failed')
        self.assertIn('settled=False', m.snapshot(self.db, {'id': 'fix'})['report']['text'])
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', self.fake_herdr):
            m.resume(self.db, dict(id='fix', message='Check killed Pi'))
        command[-1] = '4'
        env.pop('TEST_NO_SETTLED')
        env['TEST_KILL_PI'] = '1'
        subprocess.run(command, env=env, cwd=task['worktree'], capture_output=True, check=True, timeout=10)
        self.assertEqual(m.load(self.db, 'fix')['state'], 'attention')
        with self.assertRaisesRegex(ValueError, 'uncertain launches'):
            m.resume(self.db, dict(id='fix', message='Must not restart possible orphan tools'))

    def test_event_transport_filters_and_reports_disconnect(self):
        sockpath = str(self.root / "events.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(sockpath); server.listen(1)
        def send():
            conn, _ = server.accept()
            with conn:
                request = b""
                while b"\n" not in request:
                    request += conn.recv(4096)
                conn.sendall((json.dumps({"result": {"type": "subscription_started"}}) + "\n").encode())
                for payload in [[], {"event": "pane.agent_status_changed", "data": []},
                                {"event": "pane.agent_status_changed", "data": {"pane_id": "foreign", "agent_status": "blocked"}},
                                {"event": "pane.agent_status_changed", "data": {"pane_id": "w1:p2", "workspace_id": "w1", "agent_status": "blocked"}}]:
                    conn.sendall((json.dumps(payload) + "\n").encode())
        thread = threading.Thread(target=send, daemon=True); thread.start()
        try:
            out = subprocess.run([sys.executable, str(ROOT / "bin/herdr-eventwait.py"), sockpath, "2", "w1:p2"], capture_output=True, text=True, timeout=5)
            self.assertEqual(out.returncode, 4)
            self.assertEqual(out.stdout.splitlines(), ["@subscribed", "w1:p2\tw1\tblocked\t"])
        finally:
            thread.join(timeout=5); server.close()
        out = subprocess.run([sys.executable, str(ROOT / "bin/herdr-eventwait.py"), sockpath, "nan", "w1:p2"], timeout=5)
        self.assertEqual(out.returncode, 2)


if __name__ == "__main__":
    unittest.main()
