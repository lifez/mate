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
        self.config = self.root / "mate.config.json"
        self.config.write_text('{}')
        self.config_patch = patch.object(m, "CONFIG", self.config)
        self.config_patch.start()
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
        self.config_patch.stop()
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

    def configure_project(self, **settings):
        self.config.write_text(json.dumps(dict(projects={"fixture": dict(repo=str(self.repo), **settings)})))

    def test_project_branch_policy_pin_and_config_changes(self):
        self.configure_project(base_branch="main")
        params = dict(id="fix", repo=str(self.repo), brief="Fixture")
        with self.assertRaisesRegex(ValueError, "requires base_branch"):
            m.propose(self.db, dict(params, base="other"))
        task = m.propose(self.db, params)
        self.assertEqual((task['base'], task['base_ref']), ('main', 'refs/heads/main'))
        m.approve(self.db, dict(id="fix", sha=self.sha))
        m.git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@test.invalid", "commit", "--allow-empty", "-m", "moved")
        self.assertEqual(m.propose(self.db, params)['sha'], self.sha)
        with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", self.fake_herdr):
            self.configure_project(base_branch="other")
            with self.assertRaisesRegex(ValueError, "changed"):
                self.dispatch()
            self.config.write_text('{}')
            with self.assertRaisesRegex(ValueError, "changed"):
                self.dispatch()
            self.assertEqual(self.acquires, 0)
            self.configure_project(base_branch="main")
            task = self.dispatch()
            self.assertEqual(m.git(task['worktree'], 'rev-parse', 'HEAD'), self.sha)

    def test_project_config_validation_and_branch_kinds(self):
        for projects in ([], {'x': None}, {'x': {'repo': 'relative'}},
                         {'x': {'repo': str(self.repo), 'unknown': True}},
                         {'a': {'repo': str(self.repo)}, 'b': {'repo': str(self.repo / '..' / 'repo')}}):
            self.config.write_text(json.dumps(dict(projects=projects)))
            with self.assertRaises(ValueError):
                self.propose()
        for startup in (None, {}, {'command': 'echo hi'}, {'command': []},
                        {'command': ['echo', None]}, {'command': ['echo'], 'typo': 1},
                        {'command': ['echo'], 'timeout_seconds': True},
                        {'command': ['echo'], 'timeout_seconds': 121}):
            self.configure_project(startup=startup)
            with self.assertRaises(ValueError):
                self.propose()
        m.git(self.repo, 'tag', 'tag-only')
        for base in ('tag-only', self.sha, 'missing', 'main~1'):
            self.configure_project(base_branch=base)
            with self.assertRaises((ValueError, RuntimeError)):
                self.propose()
        m.git(self.repo, 'update-ref', 'refs/remotes/origin/migration', self.sha)
        self.configure_project(base_branch='origin/migration')
        params = dict(id='remote', repo=str(self.repo), brief='Fixture')
        self.assertEqual(m.propose(self.db, params)['base_ref'], 'refs/remotes/origin/migration')
        m.git(self.repo, 'branch', 'origin/migration')
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            m.propose(self.db, dict(params, id='ambiguous'))
        alias = self.root / 'alias'
        alias.symlink_to(self.repo, target_is_directory=True)
        self.config.write_text(json.dumps(dict(projects={'alias': dict(repo=str(alias), base_branch='main')})))
        self.assertEqual(m.project_config(str(self.repo))['base_branch'], 'main')
        with patch.dict(os.environ, {'HOME': str(self.root)}):
            self.config.write_text(json.dumps(dict(projects={'home': dict(repo='~/repo')})))
            self.assertEqual(m.project_config(str(self.repo))['repo'], str(self.repo))
        self.config.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'base'):
            m.propose(self.db, dict(params, id='no-base'))
        self.assertEqual(self.acquires, 0)

    def test_startup_copies_before_launch_and_never_repeats(self):
        source = self.root / 'fixture.env'
        source.write_text('FIXTURE=not-a-secret\n')
        script = self.root / 'setup.py'
        script.write_text('''import os, pathlib, shutil
assert pathlib.Path.cwd() == pathlib.Path(os.environ['MATE_WORKTREE'])
assert os.environ['MATE_TASK_ID'] == 'fix'
assert pathlib.Path(os.environ['MATE_REPO']).name == 'repo'
shutil.copyfile(pathlib.Path(os.environ['MATE_REPO']).parent / 'fixture.env', '.env')
pathlib.Path('count').write_text('once')
print('fixture-private-output')
''')
        self.configure_project(base_branch='main', startup=dict(command=[sys.executable, str(script)]))
        self.propose()
        m.approve(self.db, dict(id='fix', sha=self.sha))
        def endpoint(task, *args):
            if args[:2] == ('tab', 'create'):
                self.assertEqual((Path(task['worktree']) / '.env').read_text(), source.read_text())
                self.assertEqual(m.load(self.db, 'fix')['startup_state'], 'succeeded')
            return self.fake_herdr(task, *args)
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', endpoint):
            task = self.dispatch()
            self.assertFalse((self.repo / '.env').exists())
            self.assertEqual((self.home / 'fix/startup.log').stat().st_mode & 0o777, 0o600)
            self.assertNotIn('fixture-private-output', json.dumps(m.snapshot(self.db, dict(id='fix'))))
            script.write_text('raise RuntimeError("must not run again")')
            self.dispatch()
            task['state'] = 'review'
            with self.db: m.save(self.db, task)
            self.configure_project(base_branch='changed', startup=dict(command=['false']))
            m.resume(self.db, dict(id='fix', message='Same scope'))
            self.assertEqual(self.acquires, 1)
            self.assertEqual((Path(task['worktree']) / 'count').read_text(), 'once')

    def test_startup_failure_timeout_and_head_change_block_launch(self):
        for index, command in enumerate((
            [sys.executable, '-c', 'print("private-output"); raise SystemExit(1)'],
            [sys.executable, '-c', 'import time; time.sleep(30)'],
            ['git', 'switch', '-c', 'unexpected'],
            ['/bin/sh', '-c', 'sleep 30 &'],
        )):
            ident = f'failure-{index}'
            self.configure_project(startup=dict(command=command, timeout_seconds=1))
            self.propose(ident)
            m.approve(self.db, dict(id=ident, sha=self.sha))
            with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', self.fake_herdr):
                with self.assertRaises((RuntimeError, ValueError)) as error:
                    self.dispatch(id=ident)
                self.assertNotIn('private-output', str(error.exception))
                task = m.load(self.db, ident)
                self.assertEqual(task['state'], 'attention')
                self.assertEqual(self.launches, 0)
                self.assertEqual(self.dispatch(id=ident)['state'], 'attention')
                with self.assertRaises(ValueError):
                    m.resume(self.db, dict(id=ident, message='Retry'))
            self.assertTrue(m.snapshot(self.db, {})['events'])
            # Remove only the fixture task so the next subcase can acquire (attention blocks fleet).
            with self.db: self.db.execute('DELETE FROM tasks WHERE id=?', (ident,))

    def test_invalid_dispatch_config_stops_before_acquire(self):
        self.propose()
        m.approve(self.db, dict(id='fix', sha=self.sha))
        for content in ('{', '[]', '{"projects": null}', '{"projects": {"x": {"repo": "relative"}}}'):
            self.config.write_text(content)
            with patch.object(m, 'herdr', side_effect=AssertionError('No endpoint calls')):
                with self.assertRaises(ValueError): self.dispatch()
            self.assertEqual(m.load(self.db, 'fix')['state'], 'approved')
        self.config.unlink()
        with self.assertRaises(FileNotFoundError): self.dispatch()
        self.assertEqual(self.acquires, 0)

    def test_copy_env_example_permissions_ignore_and_symlink_refusal(self):
        source = self.root / 'source.env'
        source.write_text('FIXTURE=yes')
        command = [sys.executable, str(ROOT / 'examples/copy-env.py'), str(source)]
        def copy():
            return subprocess.run(command, cwd=self.repo, capture_output=True).returncode
        self.assertNotEqual(copy(), 0)  # Not ignored.
        (self.repo / '.gitignore').write_text('.env.local\n')
        destination = self.repo / '.env.local'
        destination.symlink_to(source)
        self.assertNotEqual(copy(), 0)
        self.assertEqual(source.read_text(), 'FIXTURE=yes')
        destination.unlink()
        self.assertEqual(copy(), 0)
        self.assertEqual(destination.read_text(), source.read_text())
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(copy(), 0)  # No silent overwrite.
        destination.unlink(); source.unlink()
        self.assertNotEqual(copy(), 0)
        self.assertFalse(destination.exists())

    def test_startup_crash_recovers_to_attention_without_retry(self):
        self.configure_project(startup=dict(command=['true']))
        self.propose()
        m.approve(self.db, dict(id='fix', sha=self.sha))
        def crash(db, task):
            task.update(startup_state='running')
            with db: m.save(db, task)
            raise SystemExit
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', self.fake_herdr), patch.object(m, 'startup_worktree', crash):
            with self.assertRaises(SystemExit): self.dispatch()
        self.db.close(); self.db = m.connect()
        with patch.object(m.time, 'time', return_value=time.time() + 61): m.reconcile(self.db)
        self.assertEqual(self.dispatch()['state'], 'attention')
        self.assertEqual((self.acquires, self.launches), (1, 0))

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

    def test_optional_tab_close_checks_identity_activity_and_preserves_resources(self):
        task = self.propose()
        (self.home / 'fix').mkdir()
        task.update(state='complete', attempt=1, pane='w1:p2', tab='w1:t2', workspace='w1',
                    endpoint_receipt={'root_pane': {'terminal_id': 'original-terminal'}})
        with self.db:
            m.save(self.db, task)
        params = dict(id='fix', attempt=1, tab='w1:t2')
        task['state'] = 'review'
        with self.db: m.save(self.db, task)
        with self.assertRaisesRegex(ValueError, 'completed task'): m.close_tab(self.db, params)
        task['state'] = 'complete'
        with self.db: m.save(self.db, task)
        pane = dict(pane_id='w1:p2', tab_id='w1:t2', workspace_id='w1', terminal_id='original-terminal')
        tab = dict(tab_id='w1:t2', workspace_id='w1', pane_count=1)
        process = dict(pane_id='w1:p2', shell_pid=10, foreground_processes=[dict(pid=10)])
        closed = []
        def endpoint(_task, *args):
            if args[:2] == ('pane', 'get'): return dict(pane=pane)
            if args[:2] == ('tab', 'get'): return dict(tab=tab)
            if args[:2] == ('pane', 'process-info'): return dict(process_info=process)
            if args[:2] == ('tab', 'close'): closed.append(args); return {}
            raise AssertionError(args)
        with patch.object(m, 'herdr', endpoint), patch.object(m, 'run', side_effect=AssertionError('No Git/lease cleanup')):
            with self.assertRaises(ValueError): m.close_tab(self.db, dict(params, attempt=2))
            with self.assertRaises(ValueError): m.close_tab(self.db, dict(params, tab='foreign'))
            guard = m.lock(self.home / 'fix/run.lock')
            try:
                with self.assertRaisesRegex(ValueError, 'still active'): m.close_tab(self.db, params)
            finally: guard.close()
            pane['terminal_id'] = 'reused-terminal'
            with self.assertRaisesRegex(ValueError, 'identity'): m.close_tab(self.db, params)
            pane['terminal_id'] = 'original-terminal'
            tab['pane_count'] = 2
            with self.assertRaisesRegex(ValueError, 'additional panes'): m.close_tab(self.db, params)
            tab['pane_count'] = 1
            process['foreground_processes'] = [dict(pid=20)]
            with self.assertRaisesRegex(ValueError, 'idle shell'): m.close_tab(self.db, params)
            process['foreground_processes'] = [dict(pid=10)]
            self.assertEqual(closed, [])
            result = m.close_tab(self.db, params)
            self.assertEqual(result['state'], 'complete')
            self.assertEqual(result['tab_close_state'], 'closed')
            self.assertTrue(result['tab_closed_by'])
            self.assertEqual(result['sha'], task['sha'])
            self.assertEqual(m.close_tab(self.db, params), result)
            self.assertEqual(closed, [('tab', 'close', 'w1:t2')])
        # Simulate an ambiguous close receipt; never automatically try again.
        with self.db: m.save(self.db, task)
        def ambiguous(t, *args):
            if args[:2] == ('tab', 'close'): raise RuntimeError('lost receipt')
            return endpoint(t, *args)
        with patch.object(m, 'herdr', ambiguous):
            with self.assertRaisesRegex(RuntimeError, 'lost receipt'): m.close_tab(self.db, params)
        self.assertEqual(m.load(self.db, 'fix')['tab_close_state'], 'uncertain')
        with patch.object(m, 'herdr', side_effect=AssertionError('No retry')):
            with self.assertRaisesRegex(ValueError, 'uncertain'): m.close_tab(self.db, params)
        self.assertEqual(m.load(self.db, 'fix')['state'], 'complete')

    def test_usage_totals_preserve_unknowns_and_sum_attempts(self):
        task = self.propose()
        task.update(state='running', attempt=1)
        with self.db:
            m.save(self.db, task)
        self.assertIsNone(m.snapshot(self.db, {})['tasks'][0]['usage_total']['estimated_cost_usd'])
        self.assertEqual(m.usage_total(task)['untracked_attempts'], [1])
        message = dict(usage=dict(input=100, output=20, cacheRead=30, cacheWrite=10, cost=dict(total=0.125)))
        m.record_usage(self.db, 'fix', 1, message)
        m.record_usage(self.db, 'fix', 1, {})  # Missing usage is not a free request.
        m.record_usage(self.db, 'fix', 1, dict(usage=dict(input=-1, output=2, cacheRead=0, cacheWrite=0, cost=dict(total=float('nan')))))
        self.db.close(); self.db = m.connect()
        first = m.snapshot(self.db, dict(id='fix', attempt=1))['attempt_usage']
        self.assertEqual(first['messages'], 3)
        self.assertEqual(first['token_reported_messages'], 1)
        self.assertEqual(first['cost_reported_messages'], 1)
        self.assertEqual(first['estimated_cost_usd'], 0.125)
        self.assertEqual(first['input_tokens'], 100)
        task = m.load(self.db, 'fix'); task['attempt'] = 2
        with self.db:
            m.save(self.db, task)
        with self.assertRaisesRegex(ValueError, 'stale'):
            m.record_usage(self.db, 'fix', 1, message)
        m.record_usage(self.db, 'fix', 2, message)
        total = m.snapshot(self.db, {})['tasks'][0]['usage_total']
        self.assertEqual(total['estimated_cost_usd'], 0.25)
        self.assertEqual(total['output_tokens'], 40)
        self.assertEqual(total['cache_read_tokens'], 60)
        self.assertEqual(total['cache_write_tokens'], 20)
        self.assertEqual(total['untracked_attempts'], [])
        self.assertEqual(m.snapshot(self.db, dict(id='fix', attempt=1))['attempt_usage'], first)
        task = m.load(self.db, 'fix'); del task['usage']['1']
        self.assertEqual(m.usage_total(task)['untracked_attempts'], [1])
        self.assertEqual(m.usage_total(task)['estimated_cost_usd'], 0.125)

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
            "pi": "#!/usr/bin/env python3\nimport json,os,sys\nfrom pathlib import Path\nPath(os.environ['MATE_HOME'], 'argv.json').write_text(json.dumps(sys.argv))\nerror=os.environ.get('TEST_PROVIDER_ERROR')\nprint('Native Pi terminal output')\nf=os.fdopen(int(os.environ['MATE_EVENT_FD']), 'w')\nprint(json.dumps({'type':'message_end','message':{'role':'assistant','stopReason':'error' if error else 'stop','errorMessage':'quota' if error else '', 'usage':{'input':100,'output':20,'cacheRead':30,'cacheWrite':10,'cost':{'total':0.125}}, 'content':[{'type':'text','text':'Evidence: checked fixture.'}]}}), file=f)\nif not os.environ.get('TEST_NO_SETTLED'): print(json.dumps({'type':'agent_settled'}), file=f)\nf.close()\nif os.environ.get('TEST_KILL_PI'): os.kill(os.getpid(), 9)\n"
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
        self.assertEqual(m.snapshot(self.db, {})['tasks'][0]['usage_total']['estimated_cost_usd'], 0.125)
        argv = json.loads((self.home / "argv.json").read_text())
        self.assertEqual(argv[argv.index("--model") + 1], "selected-model")
        self.assertEqual(argv[argv.index("--thinking") + 1], "high")
        self.assertNotIn('-p', argv)
        self.assertNotIn('--mode', argv)
        self.assertNotIn('--no-extensions', argv)
        self.assertNotIn('--no-skills', argv)
        self.assertNotIn('--no-approve', argv)
        self.assertIn('--approve', argv)
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
        self.assertEqual(m.snapshot(self.db, {})['tasks'][0]['usage_total']['estimated_cost_usd'], 0.25)
        self.assertEqual(m.snapshot(self.db, {'id': 'fix', 'attempt': 1})['attempt_usage']['messages'], 1)
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
