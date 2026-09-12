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

    def test_status_current_history_and_pinned_report_pages(self):
        task = self.propose()
        folder = self.home / task['id']
        folder.mkdir()
        report = 'หลักฐาน\n' * 4000
        (folder / 'report-1.txt').write_text(report)
        task.update(state='review', attempt=2, provider='test', model='fixture', effort='high',
                    original_brief='original', scope_history=[dict(token='approved-token', first_attempt=3,
                    approved_at=1, brief='cold scope ' * 2000)],
                    pending_scope=dict(token='pending-token', attempt=2, brief='Not approved'),
                    launch_recoveries=[dict(task=dict(brief='cold recovery ' * 3000))],
                    lease=dict(lease_id='lease-fixture'), error='Still blocked',
                    startup_state='succeeded', usage={'1': m.empty_usage()})
        with self.db:
            m.save(self.db, task)
            m.event(self.db, task, 'report', 'Fixture report available')
        before = m.load(self.db, 'fix')
        first = m.snapshot(self.db, dict(id='fix', attempt=1))
        current = first['tasks'][0]
        for key in ('id', 'brief', 'sha', 'repo', 'branch', 'provider', 'model', 'effort',
                    'pending_scope', 'error', 'startup_state'):
            self.assertEqual(current[key], before[key])
        for key in ('scope_history', 'original_brief', 'launch_recoveries', 'lease', 'usage'):
            self.assertNotIn(key, current)
        self.assertEqual(current['scope_revision'], 1)
        self.assertEqual(current['latest_scope'], dict(token='approved-token', first_attempt=3, approved_at=1))
        self.assertEqual(current['usage_total']['untracked_attempts'], [2])
        self.assertIsNone(current['usage_total']['estimated_cost_usd'])
        self.assertEqual(first['report_attempt'], 1)
        self.assertEqual(first['attempt_usage'], before['usage']['1'])
        full = m.snapshot(self.db, dict(id='fix', history=True))['tasks'][0]
        for key, value in before.items():
            self.assertEqual(full[key], value)
        self.assertLess(len(json.dumps(current)), len(json.dumps(full)) // 5)
        # A newer attempt must not redirect pagination to a different report.
        task.update(attempt=3, state='running')
        with self.db:
            m.save(self.db, task)
        (folder / 'report-3.txt').write_text('Different attempt')
        collected = first['report']['text']
        page = first
        while page['report']['more']:
            page = m.snapshot(self.db, dict(id='fix', attempt=first['report_attempt'], offset=page['report']['next_offset']))
            self.assertEqual(set(page), {'id', 'report_attempt', 'current_attempt', 'state', 'updated', 'report'})
            self.assertEqual((page['id'], page['report_attempt'], page['current_attempt'], page['state']), ('fix', 1, 3, 'running'))
            collected += page['report']['text']
        self.assertEqual(collected, report)
        self.assertNotIn('report', m.snapshot(self.db, dict(id='fix', attempt=2)))
        self.assertEqual(len(m.snapshot(self.db, {})['events']), 1, 'reads never acknowledge events')
        self.assertEqual(m.load(self.db, 'fix'), task, 'reads never rewrite the journal')
        for invalid in [dict(history=True), dict(attempt=1), dict(offset=1),
                        dict(id='fix', history='true'), dict(id='fix', offset=-1),
                        dict(id='fix', offset=True), dict(id='fix', offset=1.5),
                        dict(id='fix', offset=1), dict(id='fix', offset=1, attempt=1, history=True),
                        dict(id='fix', offset=1, attempt=2), dict(id='fix', attempt=0),
                        dict(id='fix', attempt=True), dict(id='fix', attempt=1.5), dict(id='fix', attempt=4)]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                m.snapshot(self.db, invalid)

    def test_stow_memory_budget_history_conflicts_and_task_independence(self):
        self.propose()
        before = m.snapshot(self.db, {})
        empty = m.memory(self.db, {})
        self.assertEqual(empty['revision'], 0)
        first = m.memory(self.db, dict(action='save', revision=0, content='## Preferences\nตอบภาษาไทย', reason='User preference'))
        self.assertEqual(first['bytes'], len(first['content'].encode('utf-8')))
        self.assertEqual(m.memory(self.db, dict(action='save', revision=first['revision'], content=first['content'], reason='unchanged')), first)
        for invalid in [dict(revision=0, content='stale', reason='stale'),
                        dict(revision=True, content='bad', reason='invalid'),
                        dict(revision=first['revision'], content='ก' * 4001, reason='too large'),
                        dict(revision=first['revision'], content=' ', reason='empty'),
                        dict(revision=first['revision'], content='new', reason=''),
                        dict(content='new', reason='missing revision')]:
            with self.assertRaises(ValueError):
                m.memory(self.db, dict(action='save', **invalid))
            self.assertEqual(m.memory(self.db, {}), first)
        second = m.memory(self.db, dict(action='save', revision=first['revision'], content='x' * 12000, reason='Consolidated fixture'))
        self.assertEqual(second['bytes'], second['budget_bytes'])
        self.assertEqual(m.memory(self.db, dict(revision=first['revision'])), first)
        with self.assertRaises(ValueError):
            m.memory(self.db, dict(revision=999))
        with self.assertRaises(ValueError):
            m.memory(self.db, dict(action='delete'))
        self.assertEqual(m.snapshot(self.db, {}), before)
        self.db.close()
        self.db = m.connect()
        self.assertEqual(m.memory(self.db, {}), second)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM memories').fetchone()[0], 2)
        # A failed insert must preserve both active and cold memory.
        self.db.execute("CREATE TRIGGER refuse_memory BEFORE INSERT ON memories BEGIN SELECT RAISE(ABORT, 'fixture disk failure'); END")
        self.db.commit()
        with self.assertRaisesRegex(Exception, 'fixture disk failure'):
            m.memory(self.db, dict(action='save', revision=second['revision'], content='lost', reason='failure'))
        self.assertEqual(m.memory(self.db, {}), second)
        self.assertEqual(m.memory(self.db, dict(revision=first['revision'])), first)

    def propose(self, ident="fix"):
        return m.propose(self.db, dict(id=ident, repo=str(self.repo), base="main", brief="Investigate locally; report evidence. Do not push."))

    def fake_run(self, args, cwd=None, timeout=30):
        if args[:2] == ["ps", "-axo"]:
            return '100 1 100 ttys100 zsh -zsh'
        if args[:2] == ["treehouse", "get"]:
            self.acquires += 1
            wt = self.root / f"worktree-{self.acquires}"
            m.git(self.repo, "worktree", "add", "-b", f"pool-{self.acquires}", str(wt), "HEAD")
            lease = dict(path=str(wt), lease_id=f"lease-{self.acquires}", lease_holder=args[-1])
            self.leases.append(dict(lease, status="leased", processes=[dict(pid=100, name='zsh')]))
            return json.dumps(lease)
        if args[:2] == ["treehouse", "status"]:
            return json.dumps(self.leases)
        return self.real_run(args, cwd, timeout)

    def fake_herdr(self, task, *args):
        if args[:2] == ("pane", "get"):
            return {"pane": {"pane_id": args[2], "workspace_id": "w1", "tab_id": task.get("tab", "w1:t1"), "terminal_id": "original-terminal"}}
        if args[:2] == ("pane", "process-info"):
            return dict(process_info=dict(pane_id=task['pane'], shell_pid=100,
                foreground_process_group_id=100, foreground_processes=[dict(pid=100)]))
        if args[:2] == ("tab", "create"):
            return {"tab": {"tab_id": "w1:t2"}, "root_pane": {"pane_id": "w1:p2", "terminal_id": "original-terminal"}}
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

    def test_same_tab_dispatch_continuation_and_closure(self):
        calls = []
        def herdr(task, *args):
            calls.append(args)
            if args[:2] == ('pane', 'split'):
                return {'pane': dict(pane_id='w1:p3', tab_id='w1:t1', workspace_id='w1', terminal_id='split-terminal')}
            result = self.fake_herdr(task, *args)
            if args[:2] == ('pane', 'get') and args[2] == 'w1:p3':
                result['pane']['terminal_id'] = 'split-terminal'
            return result
        self.propose()
        m.approve(self.db, dict(id='fix', sha=self.sha))
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', herdr):
            task = self.dispatch(same_tab_as='supervisor')
            self.assertEqual((task['tab'], task['pane']), ('w1:t1', 'w1:p3'))
            self.assertEqual(task['split_target']['pane'], 'w1:p1')
            self.assertEqual(m.snapshot(self.db, {})['tasks'][0]['same_tab_as'], 'supervisor')
            self.assertEqual(self.dispatch(same_tab_as='missing'), task)
            task['state'] = 'review'
            with self.db:
                m.save(self.db, task)
            resumed = m.resume(self.db, dict(id='fix', message='Check again'))
            for key in ('pane', 'tab', 'worktree', 'lease', 'same_tab_as', 'endpoint_receipt'):
                self.assertEqual(resumed[key], task[key])
            self.assertEqual((self.acquires, self.launches), (1, 2))
            self.assertEqual(sum(c[:2] == ('pane', 'split') for c in calls), 1)
            self.assertFalse(any(c[:2] == ('tab', 'create') for c in calls))
            self.assertTrue(all(c[2] == 'w1:p3' for c in calls if c[:2] == ('pane', 'run')))
            resumed['state'] = 'failed'
            with self.db:
                m.save(self.db, resumed)
            completed = m.complete(self.db, dict(id='fix', attempt=2, force=True))
            self.assertEqual(completed['completed_from'], 'failed')
            self.assertEqual(completed['completed_via'], 'mate-complete --force')
            with self.assertRaisesRegex(ValueError, 'Shared tab'):
                m.close_tab(self.db, dict(id='fix', attempt=2, tab='w1:t1'))

    def test_same_tab_target_validation_and_uncertain_split(self):
        self.propose()
        m.approve(self.db, dict(id='fix', sha=self.sha))
        source = self.propose('related')
        source.update(session=os.environ['HERDR_SESSION'], socket=os.environ['HERDR_SOCKET_PATH'],
            workspace='w1', tab='w1:t2', pane='w1:p2',
            endpoint_receipt={'root_pane': {'terminal_id': 'original-terminal'}})
        with self.db:
            m.save(self.db, source)
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', self.fake_herdr):
            for reference in ('missing', 'fix', '', None, 1, '--current'):
                with self.subTest(reference=reference), self.assertRaises(ValueError):
                    self.dispatch(same_tab_as=reference)
            for key, bad in (('session', 'other'), ('socket', '/other'), ('workspace', 'w2'),
                             ('tab', None), ('pane', None), ('tab_close_state', 'uncertain'),
                             ('endpoint_receipt', {'root_pane': {'terminal_id': 'reused'}})):
                with self.subTest(key=key):
                    with self.db:
                        m.save(self.db, dict(source, **{key: bad}))
                    with self.assertRaises(ValueError):
                        self.dispatch(same_tab_as='related')
            self.assertEqual(self.acquires, 0)
            self.assertEqual(m.load(self.db, 'fix')['state'], 'approved')
            with self.db:
                m.save(self.db, source)
        def broken(task, *args):
            if args[:2] == ('pane', 'split'):
                saved = m.load(self.db, 'fix')
                self.assertEqual(saved['split_target']['tab'], 'w1:t2')
                raise RuntimeError('Lost split response')
            return self.fake_herdr(task, *args)
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', broken):
            with self.assertRaisesRegex(RuntimeError, 'Lost split'):
                self.dispatch(same_tab_as='related')
            self.assertEqual(self.dispatch()['state'], 'attention')
            with self.assertRaisesRegex(ValueError, 'execution evidence'):
                m.inspect_cancel(self.db, dict(id='fix'))
            self.assertEqual((self.acquires, self.launches), (1, 0))

    def test_same_tab_acquisition_recovery_keeps_pinned_target(self):
        self.propose()
        m.approve(self.db, dict(id='fix', sha=self.sha))
        def fail_acquire(args, cwd=None, timeout=30):
            if args[:2] == ['treehouse', 'get']:
                raise RuntimeError('Fixture acquisition failure')
            return self.fake_run(args, cwd, timeout)
        with patch.object(m, 'run', fail_acquire), patch.object(m, 'herdr', self.fake_herdr):
            with self.assertRaisesRegex(RuntimeError, 'Fixture acquisition'):
                self.dispatch(same_tab_as='supervisor')
            failed = m.load(self.db, 'fix')
            recovered = m.recover_acquire(self.db, failed)  # Test-only synthetic human recovery.
            self.assertEqual(recovered['split_target'], failed['split_target'])
        def herdr(task, *args):
            if args[:2] == ('pane', 'split'):
                self.assertEqual(args[2], 'w1:p1')
                return {'pane': dict(pane_id='w1:p3', tab_id='w1:t1', workspace_id='w1', terminal_id='new')}
            result = self.fake_herdr(task, *args)
            if args[:2] == ('pane', 'get') and args[2] == 'w1:p3':
                result['pane']['terminal_id'] = 'new'
            return result
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', herdr):
            task = self.dispatch(same_tab_as='missing')
            self.assertEqual((task['attempt'], task['same_tab_as']), (2, 'supervisor'))
            self.assertEqual(task['split_target'], failed['split_target'])

    def test_same_tab_recheck_and_bad_receipt_never_launch(self):
        for case in ('target-moved', 'wrong-tab', 'same-pane', 'same-terminal', 'missing-terminal'):
            with self.subTest(case=case):
                ident = case
                self.propose(ident)
                m.approve(self.db, dict(id=ident, sha=self.sha))
                def herdr(task, *args):
                    if args[:2] == ('pane', 'split'):
                        pane = dict(pane_id='w1:p3', tab_id='w1:t1', workspace_id='w1', terminal_id='new')
                        if case == 'wrong-tab': pane['tab_id'] = 'w1:t99'
                        if case == 'same-pane': pane['pane_id'] = 'w1:p1'
                        if case == 'same-terminal': pane['terminal_id'] = 'original-terminal'
                        if case == 'missing-terminal': pane.pop('terminal_id')
                        return {'pane': pane}
                    result = self.fake_herdr(task, *args)
                    if case == 'target-moved' and args[:2] == ('pane', 'get') and task.get('tab') and self.acquires:
                        result['pane']['tab_id'] = 'w1:t99'
                    return result
                with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', herdr):
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.dispatch(id=ident, same_tab_as='supervisor')
                    self.assertEqual(m.load(self.db, ident)['state'], 'attention')
                    self.assertEqual(self.launches, 0)
                # Remove only this disposable task's journal row to exercise the next case.
                with self.db:
                    self.db.execute('DELETE FROM tasks WHERE id=?', (ident,))

    def test_snapshot_open_tasks_excludes_complete_before_pagination(self):
        states = ['complete'] * 51 + ['awaiting-base', 'approved', 'running', 'review', 'failed', 'attention']
        records = [dict(id=str(i), state=state, updated=-i, attempt=0) for i, state in enumerate(states)]
        with patch.object(m, 'tasks', return_value=records):
            for offset in (0, 50, 100):
                snapshot = m.snapshot(self.db, dict(task_offset=offset))
                self.assertEqual(snapshot['total_tasks'], 57)
                self.assertEqual(snapshot['open_tasks'], 6)
            self.assertTrue(all(t['state'] == 'complete' for t in m.snapshot(self.db, {})['tasks']))
        with patch.object(m, 'tasks', return_value=records[:51]):
            self.assertEqual(m.snapshot(self.db, {})['open_tasks'], 0)
        with patch.object(m, 'tasks', return_value=[]):
            self.assertEqual(m.snapshot(self.db, {})['open_tasks'], 0)

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
            pending = m.propose_scope(self.db, dict(id='fix', brief='Also check accessibility.'))['pending_scope']
            m.review_scope(self.db, dict(id='fix', token=pending['token'], attempt=1, sha=self.sha, approve=True))
            m.resume(self.db, dict(id='fix', message='Run the approved scope'))
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

    def test_scope_addition_approval_continuation_and_preserved_evidence(self):
        original = self.propose()
        approved = m.approve(self.db, dict(id='fix', sha=self.sha))
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', self.fake_herdr):
            task = self.dispatch(effort='high')
            task['state'] = 'review'
            with self.db:
                m.save(self.db, task)
                m.event(self.db, task, 'report', 'Original report')
            folder = self.home / 'fix'
            (folder / 'report-1.txt').write_text('Original evidence')
            (folder / 'session.jsonl').write_text('Saved session')
            dirty = Path(task['worktree']) / 'keep.txt'
            dirty.write_text('Uncommitted work')
            pending = m.propose_scope(self.db, dict(id='fix', brief='Also check accessibility.'))
            self.assertEqual(m.propose_scope(self.db, dict(id='fix', brief='Also check accessibility.')), pending)
            self.assertEqual(pending['brief'], original['brief'])
            self.assertTrue(m.snapshot(self.db, {})['tasks'][0]['scope_pending'])
            self.assertNotIn('pending_scope', m.snapshot(self.db, {})['tasks'][0])
            with self.assertRaisesRegex(ValueError, 'Pending'):
                m.resume(self.db, dict(id='fix', message='Must wait'))
            with self.assertRaisesRegex(ValueError, 'Scope changed'):
                m.complete(self.db, dict(id='fix', attempt=1))
            self.db.close(); self.db = m.connect()
            params = dict(id='fix', token=pending['pending_scope']['token'], attempt=1, sha=self.sha, approve=True)
            extended = m.review_scope(self.db, params)
            self.assertEqual(extended['original_brief'], original['brief'])
            self.assertIn('Also check accessibility.', extended['brief'])
            self.assertTrue(extended['scope_history'][0]['approved_by'])
            for key in ('repo', 'base', 'sha', 'branch', 'lease', 'holder', 'worktree', 'pane', 'tab', 'session', 'socket', 'model', 'effort'):
                self.assertEqual(extended[key], task[key])
            self.assertEqual(extended['approved_at'], approved['approved_at'])
            with self.assertRaises(ValueError): m.review_scope(self.db, params)
            with self.assertRaisesRegex(ValueError, 'Scope changed'):
                m.complete(self.db, dict(id='fix', attempt=1, scope_revision=1))
            self.assertEqual(self.dispatch()['state'], 'review')  # Not a fresh dispatch approval.
            self.assertEqual((self.acquires, self.launches), (1, 1))
            with patch.object(m, 'check_endpoint', side_effect=ValueError('endpoint changed')):
                with self.assertRaisesRegex(ValueError, 'endpoint changed'):
                    m.resume(self.db, dict(id='fix', message='Run approved addition'))
            self.leases[0]['lease_holder'] = 'foreign'
            with self.assertRaisesRegex(ValueError, 'exact lease'):
                m.resume(self.db, dict(id='fix', message='Run approved addition'))
            self.leases[0]['lease_holder'] = task['holder']
            continued = m.resume(self.db, dict(id='fix', message='Run approved addition'))
            self.assertEqual((self.acquires, self.launches, continued['attempt']), (1, 2, 2))
            self.assertEqual(dirty.read_text(), 'Uncommitted work')
            self.assertEqual((folder / 'session.jsonl').read_text(), 'Saved session')
            self.assertEqual(m.snapshot(self.db, dict(id='fix', attempt=1))['report']['text'], 'Original evidence')
            self.assertEqual(len(m.snapshot(self.db, {})['events']), 2)
            continued['state'] = 'review'
            with self.db: m.save(self.db, continued)
            with self.assertRaisesRegex(ValueError, 'Scope changed'):
                m.complete(self.db, dict(id='fix', attempt=2, scope_revision=0))
            self.assertEqual(m.complete(self.db, dict(id='fix', attempt=2, scope_revision=1))['state'], 'complete')

    def test_scope_addition_rejects_live_stale_invalid_and_terminal_tasks(self):
        task = self.propose()
        (self.home / 'fix').mkdir()
        for state in ('awaiting-base', 'approved', 'acquiring', 'launching', 'running', 'attention', 'complete'):
            task.update(state=state, attempt=1)
            with self.db: m.save(self.db, task)
            with self.assertRaises(ValueError): m.propose_scope(self.db, dict(id='fix', brief='Extra'))
        task['state'] = 'failed'
        with self.db: m.save(self.db, task)
        with m.lock(self.home / 'fix/run.lock'):
            with self.assertRaises(BlockingIOError): m.propose_scope(self.db, dict(id='fix', brief='Extra'))
        for brief in ('', ' ', '\0', 1, 'x' * 20000):
            with self.assertRaises(ValueError): m.propose_scope(self.db, dict(id='fix', brief=brief))
        pending = m.propose_scope(self.db, dict(id='fix', brief='Extra'))['pending_scope']
        params = dict(id='fix', token=pending['token'], attempt=1, sha=self.sha, approve=True)
        for change in (dict(token='stale'), dict(attempt=2), dict(attempt=True), dict(sha='changed'), dict(approve='yes')):
            with self.assertRaises(ValueError): m.review_scope(self.db, dict(params, **change))
        with m.lock(self.home / 'fix/run.lock'):
            with self.assertRaises(BlockingIOError): m.review_scope(self.db, params)
        replaced = m.propose_scope(self.db, dict(id='fix', brief='Revised extra'))
        with self.assertRaises(ValueError): m.review_scope(self.db, params)
        params['token'] = replaced['pending_scope']['token']
        # A changed attempt/state must invalidate an open approval dialog.
        for change in (dict(attempt=2), dict(state='complete'), dict(state='attention')):
            with self.db: m.save(self.db, dict(replaced, **change))
            with self.assertRaises(ValueError): m.review_scope(self.db, params)
        with self.db: m.save(self.db, replaced)
        declined = m.review_scope(self.db, dict(params, approve=False))
        self.assertNotIn('pending_scope', declined)
        self.assertNotIn('scope_history', declined)
        self.assertEqual((declined['state'], declined['brief']), ('failed', task['brief']))
        self.assertEqual(m.snapshot(self.db, {})['events'], [])
        for brief in ('First approved addition', 'Second approved addition'):
            pending = m.propose_scope(self.db, dict(id='fix', brief=brief))['pending_scope']
            m.review_scope(self.db, dict(params, token=pending['token']))
        self.db.close(); self.db = m.connect()
        accepted = m.load(self.db, 'fix')
        self.assertEqual(accepted['state'], 'failed')
        self.assertEqual(len(accepted['scope_history']), 2)
        self.assertEqual(len(m.snapshot(self.db, {})['events']), 2, 'each approval has a durable event even in the same attempt')
        self.assertTrue(all(h['first_attempt'] == 2 for h in accepted['scope_history']))

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

    def test_acquire_recovery_preserves_approval_history_and_retries_once(self):
        self.configure_project(base_branch='main', startup=dict(command=['true']))
        self.propose()
        approved = m.approve(self.db, dict(id='fix', sha=self.sha))
        def fail_acquire(args, cwd=None, timeout=30):
            if args[:2] == ['treehouse', 'get']: raise RuntimeError('lost acquire receipt')
            return self.real_run(args, cwd, timeout)
        with patch.object(m, 'run', fail_acquire), patch.object(m, 'herdr', self.fake_herdr):
            with self.assertRaises(RuntimeError): self.dispatch(effort='high')
        failed = m.load(self.db, 'fix')
        m.git(self.repo, '-c', 'user.name=Test', '-c', 'user.email=test@test.invalid', 'commit', '--allow-empty', '-m', 'moved')
        self.configure_project(base_branch='main', startup=dict(command=['false']))
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', self.fake_herdr):
            confirmation = f"recover fix {failed['attempt']} {failed['sha']}"
            with patch.object(m.sys.stdin, 'isatty', return_value=True), patch('builtins.print'), patch('builtins.input', return_value=confirmation):
                m.recover_acquire_cli('fix')
            recovered = m.load(self.db, 'fix')
            self.assertEqual(recovered['recoveries'][0]['task'], failed)
            self.assertEqual((self.acquires, self.launches), (0, 0))
            for key in ('id', 'repo', 'brief', 'sha', 'base', 'branch', 'approved_at'):
                self.assertEqual(recovered[key], approved[key])
            self.db.close(); self.db = m.connect()
            with self.assertRaises(ValueError): m.recover_acquire(self.db, m.load(self.db, 'fix'))
            task = self.dispatch(model='ignored', effort='low')
            self.assertEqual(task['attempt'], 2)
            self.assertEqual(task['effort'], 'high')
            self.assertEqual(task['startup_state'], 'succeeded')
            self.assertNotEqual(task['holder'], failed['holder'])
            self.assertEqual(m.git(task['worktree'], 'rev-parse', 'HEAD'), self.sha)
            self.dispatch()
            self.assertEqual((self.acquires, self.launches), (1, 1))
        self.assertEqual([e['kind'] for e in m.snapshot(self.db, {})['events']], ['launch-uncertain', 'acquire-recovered'])

    def test_acquire_recovery_refuses_uncertainty_and_cli_requires_human(self):
        self.propose()
        m.approve(self.db, dict(id='fix', sha=self.sha))
        with patch.object(m, 'run', side_effect=RuntimeError('acquire failed')), patch.object(m, 'herdr', self.fake_herdr):
            with self.assertRaises(RuntimeError): self.dispatch()
        task = m.load(self.db, 'fix')
        with patch.object(m, 'run', self.fake_run):
            for key in ('lease', 'worktree', 'startup_state', 'endpoint_receipt', 'pane', 'tab', 'usage', 'followup'):
                with self.assertRaises(ValueError): m.recover_acquire(self.db, dict(task, **{key: None}))
            artifact = self.home / 'fix/session.jsonl'
            artifact.write_text('evidence')
            with self.assertRaisesRegex(ValueError, 'artifacts'): m.recover_acquire(self.db, task.copy())
            artifact.unlink()
            self.leases = [dict(path=str(self.root / 'wt'), status='leased', lease_id='fixture', lease_holder=task['holder'])]
            with self.assertRaisesRegex(ValueError, 'holder'): m.recover_acquire(self.db, task.copy())
            for rows in ({}, [None], [{}], [dict(path=str(self.root / 'wt'), status='leased')]):
                self.leases = rows
                with self.assertRaisesRegex(ValueError, 'status'): m.recover_acquire(self.db, task.copy())
            self.leases = []
            self.configure_project(base_branch='main')
            with self.assertRaisesRegex(ValueError, 'base_branch'): m.recover_acquire(self.db, task.copy())
            self.config.write_text('{}')
            m.git(self.repo, 'branch', task['branch'])
            with self.assertRaisesRegex(ValueError, 'branch already exists'): m.recover_acquire(self.db, task.copy())
        with patch.object(m.sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(ValueError, 'interactive'): m.recover_acquire_cli('fix')
        with patch.object(m.sys.stdin, 'isatty', return_value=True), patch('builtins.print'), patch('builtins.input', return_value='no'):
            with m.lock(self.home / 'supervisor.lock'):
                with self.assertRaises(BlockingIOError): m.recover_acquire_cli('fix')
            with m.lock(self.home / 'fix/run.lock'):
                with self.assertRaises(BlockingIOError): m.recover_acquire_cli('fix')
            m.recover_acquire_cli('fix')
        self.assertEqual(m.load(self.db, 'fix'), task)

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

    def cancellation_attention_fixture(self, ident="cancel-attention"):
        task = self.propose(ident)
        task = m.approve(self.db, dict(id=ident, sha=self.sha))
        task.update(state="attention", attempt=1, holder="mate:cancel-holder", session="mate-test",
                    socket=str(self.root / "herdr.sock"), workspace="w1", missing_from="acquiring",
                    error="Treehouse acquire outcome was uncertain", pi_binary=sys.executable,
                    provider="openai-codex", model="test-model", effort="off", project="fixture",
                    startup={"command": ["true"]})
        folder = self.home / ident
        folder.mkdir(mode=0o700)
        (folder / "run.lock").touch()
        with self.db:
            m.save(self.db, task)
        return task

    def cancel_params(self, inspection, **extra):
        task = inspection["task"]
        params = dict(id=task["id"], state=task["state"], attempt=task["attempt"], sha=task["sha"],
                      confirmation=inspection["confirmation"], confirmed=True,
                      attest_external=inspection.get("requires_external_attestation", False))
        params.update(extra)
        return params

    def test_human_cancel_unstarted_preserves_history_and_is_idempotent(self):
        task = self.propose()
        approved = m.approve(self.db, dict(id="fix", sha=self.sha))
        with self.db:
            m.event(self.db, task, "report", "Prior durable evidence")
        report_id = m.snapshot(self.db, {})["events"][0]["id"]
        m.acknowledge(self.db, dict(events=[report_id], note="Retained prior evidence"))
        with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", side_effect=AssertionError("No Herdr operation for attempt 0")):
            inspection = m.inspect_cancel(self.db, dict(id="fix"))
            self.assertEqual(inspection["requires_external_attestation"], False)
            self.assertEqual(self.acquires, 0)
            cancelled = m.cancel(self.db, self.cancel_params(inspection))
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertEqual(cancelled["sha"], approved["sha"])
        self.assertEqual(cancelled["cancellation_history"][0]["state_before"], "approved")
        self.assertFalse(cancelled["cancellation_history"][0]["external_inspection_attested"])
        self.assertEqual(cancelled["cancelled_via"], "mate-cancel")
        self.assertFalse("lease" in cancelled or "worktree" in cancelled or "endpoint_receipt" in cancelled)
        self.assertFalse((self.home / "fix" / "session.jsonl").exists())
        self.assertEqual(m.snapshot(self.db, {})["open_tasks"], 0)
        self.assertEqual(len(m.snapshot(self.db, {})["events"]), 1, "cancellation event is separate from prior acknowledged event")
        audit = cancelled["cancellation_history"]
        self.assertEqual(m.cancel(self.db, self.cancel_params(inspection))["cancellation_history"], audit)
        with patch.object(m, "run", side_effect=AssertionError("No dispatch after cancellation")), patch.object(m, "herdr", side_effect=AssertionError("No Herdr operation")):
            with self.assertRaisesRegex(ValueError, "cannot be dispatched"):
                m.dispatch(self.db, dict(id="fix"))
        for operation in (
            lambda: m.resume(self.db, dict(id="fix", message="must refuse")),
            lambda: m.propose_scope(self.db, dict(id="fix", brief="must refuse")),
            lambda: m.complete(self.db, dict(id="fix", attempt=0)),
            lambda: m.complete(self.db, dict(id="fix", attempt=0, force=True)),
            lambda: m.close_tab(self.db, dict(id="fix", attempt=0, tab="none")),
        ):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(m.load(self.db, "fix")["cancellation_history"], audit)
        pending = self.propose("pending-cancel")
        with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", side_effect=AssertionError("No Herdr operation for awaiting-base")):
            pending_inspection = m.inspect_cancel(self.db, dict(id="pending-cancel"))
            pending_cancelled = m.cancel(self.db, self.cancel_params(pending_inspection))
        self.assertEqual(pending_cancelled["state"], "cancelled")
        self.assertNotIn("approved_at", pending_cancelled)

    def test_pre_receipt_attention_cancel_requires_inspection_and_attestation(self):
        task = self.cancellation_attention_fixture()
        def herdr_cancel(current, *args):
            self.assertEqual(args, ("tab", "list", "--workspace", "w1"))
            return {"tabs": []}
        with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", herdr_cancel):
            inspection = m.inspect_cancel(self.db, dict(id=task["id"]))
            self.assertTrue(inspection["requires_external_attestation"])
            with self.assertRaisesRegex(ValueError, "attestation"):
                m.cancel(self.db, self.cancel_params(inspection, attest_external=False))
            self.assertEqual(m.load(self.db, task["id"])["state"], "attention")
            cancelled = m.cancel(self.db, self.cancel_params(inspection, attest_external=True))
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertTrue(cancelled["cancellation_history"][0]["external_inspection_attested"])
        self.assertEqual(cancelled["cancellation_history"][0]["attempt"], 1)
        self.assertEqual(self.acquires, 0)
        self.assertEqual(self.launches, 0)
        self.assertEqual(m.snapshot(self.db, {})["open_tasks"], 0)

    def test_shared_tab_pre_receipt_cancel_preserves_target_and_refuses_split_receipt(self):
        self.propose()
        m.approve(self.db, dict(id='fix', sha=self.sha))
        def fail_acquire(args, cwd=None, timeout=30):
            if args[:2] == ['treehouse', 'get']:
                raise RuntimeError('Fixture acquisition failure')
            return self.fake_run(args, cwd, timeout)
        with patch.object(m, 'run', fail_acquire), patch.object(m, 'herdr', self.fake_herdr):
            with self.assertRaisesRegex(RuntimeError, 'Fixture acquisition'):
                self.dispatch(same_tab_as='supervisor')
        failed = m.load(self.db, 'fix')
        with self.assertRaisesRegex(ValueError, 'uncertain task'):
            m.check_capacity(self.db)
        for receipt in ({'pane': {'terminal_id': 'split-terminal'}},
                        {'root_pane': {'terminal_id': 'original-terminal'}}):
            with self.subTest(receipt=receipt):
                with self.db:
                    m.save(self.db, dict(failed, endpoint_receipt=receipt))
                with self.assertRaisesRegex(ValueError, 'execution evidence'):
                    m.inspect_cancel(self.db, dict(id='fix'))
                self.assertEqual(m.load(self.db, 'fix')['endpoint_receipt'], receipt)
        with self.db:
            m.save(self.db, failed)
        def herdr_inspection(task, *args):
            self.assertEqual(args, ('tab', 'list', '--workspace', 'w1'))
            return {'tabs': [dict(tab_id='w1:t1', workspace_id='w1', label='supervisor')]}
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', herdr_inspection):
            inspection = m.inspect_cancel(self.db, dict(id='fix'))
            cancelled = m.cancel(self.db, self.cancel_params(inspection))
        self.assertEqual(cancelled['state'], 'cancelled')
        for key in ('same_tab_as', 'split_target', 'sha', 'holder', 'error'):
            self.assertEqual(cancelled[key], failed[key])
        self.assertNotIn('endpoint_receipt', cancelled)
        self.assertEqual((self.acquires, self.launches), (0, 0))
        m.check_capacity(self.db)
        self.assertEqual(m.snapshot(self.db, {})['open_tasks'], 0)

    def test_cancel_refuses_resources_orphan_evidence_and_stale_or_busy_confirmation(self):
        task = self.cancellation_attention_fixture("refuse-cancel")
        def herdr_cancel(current, *args):
            return {"tabs": []}
        for key in ("lease", "worktree", "startup_state", "endpoint_receipt", "usage", "followup"):
            changed = dict(task, **{key: {} if key != "startup_state" else "failed"})
            with self.db:
                m.save(self.db, changed)
            with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", herdr_cancel):
                with self.assertRaisesRegex(ValueError, "evidence"):
                    m.inspect_cancel(self.db, dict(id="refuse-cancel"))
            self.assertEqual(m.load(self.db, "refuse-cancel")["state"], "attention")
        with self.db:
            m.save(self.db, task)
        self.leases = [dict(path=str(self.root / "other-wt"), status="leased", lease_id="l", lease_holder=task["holder"])]
        with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", herdr_cancel):
            with self.assertRaisesRegex(ValueError, "holder"):
                m.inspect_cancel(self.db, dict(id="refuse-cancel"))
        self.leases = []
        branch = task["branch"]
        m.git(self.repo, "branch", branch)
        with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", herdr_cancel):
            with self.assertRaisesRegex(ValueError, "branch"):
                m.inspect_cancel(self.db, dict(id="refuse-cancel"))
        m.git(self.repo, "branch", "-D", branch)
        (self.home / "refuse-cancel" / "report-1.txt").write_text("saved evidence")
        with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", herdr_cancel):
            with self.assertRaisesRegex(ValueError, "artifacts"):
                m.inspect_cancel(self.db, dict(id="refuse-cancel"))
        (self.home / "refuse-cancel" / "report-1.txt").unlink()
        with m.lock(self.home / "refuse-cancel" / "run.lock"):
            with self.assertRaisesRegex(ValueError, "busy"):
                m.inspect_cancel(self.db, dict(id="refuse-cancel"))
        with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", herdr_cancel):
            inspection = m.inspect_cancel(self.db, dict(id="refuse-cancel"))
            changed = m.load(self.db, "refuse-cancel")
            changed["brief"] = "changed while dialog was open"
            with self.db:
                m.save(self.db, changed)
            with self.assertRaisesRegex(ValueError, "stale"):
                m.cancel(self.db, self.cancel_params(inspection, confirmation=inspection["confirmation"]))
        self.assertEqual(m.load(self.db, "refuse-cancel")["state"], "attention")
        with self.db:
            m.save(self.db, task)
        def matching_process(args, cwd=None, timeout=30):
            output = self.fake_run(args, cwd, timeout)
            return output + f"\n101 1 101 ttys100 pi pi --task {task['id']}" if args[0] == "ps" else output
        with patch.object(m, "run", matching_process), patch.object(m, "herdr", herdr_cancel):
            with self.assertRaisesRegex(ValueError, "process"):
                m.inspect_cancel(self.db, dict(id="refuse-cancel"))
        with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", lambda *_: {"tabs": [{"tab_id": "w1:t9", "workspace_id": "w1", "label": "mate-refuse-cancel"}]}):
            with self.assertRaisesRegex(ValueError, "matching Mate task tab"):
                m.inspect_cancel(self.db, dict(id="refuse-cancel"))
        with patch.object(m, "run", self.fake_run), patch.object(m, "herdr", side_effect=RuntimeError("unavailable")):
            with self.assertRaisesRegex(ValueError, "Herdr"):
                m.inspect_cancel(self.db, dict(id="refuse-cancel"))
        self.assertEqual(m.load(self.db, "refuse-cancel")["state"], "attention")

    def test_cancel_refuses_later_phases_and_unknown_inspection(self):
        task = self.propose("phase-cancel")
        for state in ("acquiring", "launching", "running", "review", "failed", "complete"):
            changed = dict(task, state=state, attempt=1)
            with self.db:
                m.save(self.db, changed)
            with self.assertRaises(ValueError):
                m.inspect_cancel(self.db, dict(id="phase-cancel"))
        with self.db:
            m.save(self.db, dict(task, state="attention", attempt=1, approved_at=1,
                                 holder="mate:x", session="s", socket="/missing", workspace="w",
                                 error="later phase", missing_from="launching"))
        with self.assertRaisesRegex(ValueError, "later phase"):
            m.inspect_cancel(self.db, dict(id="phase-cancel"))
        phase_folder = self.home / "phase-cancel"
        phase_folder.mkdir(mode=0o700)
        (phase_folder / "run.lock").touch()
        with self.db:
            m.save(self.db, dict(task, state="attention", attempt=1, approved_at=1,
                                 holder="mate:x", session="s", socket="/missing", workspace="w",
                                 error="corrupt", missing_from="acquiring", lease=None))
        with self.assertRaisesRegex(ValueError, "evidence"):
            m.inspect_cancel(self.db, dict(id="phase-cancel"))

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

    def test_force_complete_preserves_evidence_and_safety_gates(self):
        self.propose()
        m.approve(self.db, dict(id='fix', sha=self.sha))
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', self.fake_herdr):
            task = self.dispatch()
            task.update(state='failed', error='Pi exit=1, settled=False')
            with self.db:
                m.save(self.db, task)
                m.event(self.db, task, 'failed', task['error'])
            report = self.home / 'fix/report-1.txt'
            report.write_text('Partial worker report')
            params = dict(id='fix', attempt=1, force=True)
            for change in (dict(force='true'), dict(force=1), dict(force=False), dict(attempt=2)):
                with self.assertRaises(ValueError):
                    m.complete(self.db, params | change)
            for change in (dict(state='attention'), dict(state='running'), dict(state='launching'),
                           dict(state='approved'), dict(state='acquiring'), dict(state='awaiting-base'),
                           dict(pending_scope={'brief': 'not approved'}),
                           dict(scope_history=[dict(first_attempt=2)])):
                with self.db:
                    m.save(self.db, task | change)
                with self.assertRaises(ValueError):
                    m.complete(self.db, params)
            with self.db:
                m.save(self.db, task)
            with m.lock(self.home / 'fix/run.lock'):
                with self.assertRaisesRegex(ValueError, 'still active'):
                    m.complete(self.db, params)
            for gate in ('ready_pane', 'check_lease'):
                with patch.object(m, gate, side_effect=ValueError('Uncertain identity/processes')):
                    with self.assertRaisesRegex(ValueError, 'Uncertain'):
                        m.complete(self.db, params)
                    self.assertEqual(m.load(self.db, 'fix'), task)
            with patch.object(m, 'run', return_value='100 1 100 ttys100 zsh -zsh\n120 100 120 ttys100 node pi'):
                with self.assertRaisesRegex(ValueError, 'background/stopped'):
                    m.complete(self.db, params)
            completed = m.complete(self.db, params)
            self.assertEqual(completed['state'], 'complete')
            self.assertEqual(completed['completed_via'], 'mate-complete --force')
            self.assertEqual(completed['completed_from'], 'failed')
            self.assertTrue(completed['completed_by'])
            self.assertGreater(completed['completed_at'], 0)
            for key, value in task.items():
                if key not in ('state', 'updated'):
                    self.assertEqual(completed[key], value)
            self.assertEqual(m.complete(self.db, params), completed)
            self.assertEqual(m.complete(self.db, dict(id='fix', attempt=1)), completed)
            self.assertEqual(report.read_text(), 'Partial worker report')
            self.assertEqual((self.acquires, self.launches), (1, 1))
        self.db.close(); self.db = m.connect()
        snapshot = m.snapshot(self.db, {})
        self.assertEqual(snapshot['tasks'][0]['completed_from'], 'failed')
        self.assertEqual(snapshot['tasks'][0]['error'], task['error'])
        self.assertEqual(len(snapshot['events']), 1)
        self.assertEqual(snapshot['open_tasks'], 0)

    def test_pane_process_ownership_not_shared_tty_or_program_name(self):
        self.propose()
        m.approve(self.db, dict(id='fix', sha=self.sha))
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', self.fake_herdr):
            task = self.dispatch()
            baseline = '100 1 100 ttys100 zsh -zsh'
            # Detached prompt services share a TTY but are not shell jobs.
            helpers = baseline + '\n110 1 110 ttys100 zsh -zsh\n111 110 110 ttys100 gitstatusd gitstatusd\n112 110 110 ttys100 <defunct> <defunct>'
            with patch.object(m, 'run', return_value=helpers):
                self.assertEqual(m.ready_pane(task)[0], 100)
            for row in ('120 100 120 ttys100 node vite',  # Background/stopped job.
                        '120 100 120 ttys100 zsh -zsh',  # No name-based exemption.
                        '120 100 120 ttys100 gitstatusd gitstatusd',
                        '120 1 100 ttys100 node orphan'):  # Still in shell's group.
                with patch.object(m, 'run', return_value=helpers + '\n' + row):
                    with self.assertRaisesRegex(ValueError, 'background/stopped'):
                        m.ready_pane(task)

    def test_initial_preflight_recovery_preserves_resources_and_confirms_start(self):
        self.propose()
        m.approve(self.db, dict(id='fix', sha=self.sha))
        def busy(t, *args):
            result = self.fake_herdr(t, *args)
            if args[:2] == ('pane', 'process-info'):
                result['process_info']['foreground_processes'] = [dict(pid=101)]
            return result
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', busy):
            with self.assertRaisesRegex(m.LaunchPreflightRefused, 'idle shell'):
                self.dispatch(effort='high')
        original = m.load(self.db, 'fix')
        self.assertEqual(original['launch_stage'], 'preflight-refused')
        self.assertEqual((self.acquires, self.launches), (1, 0))
        folder = self.home / 'fix'
        dirty = Path(original['worktree']) / 'keep.txt'
        dirty.write_text('preserved startup output')
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', self.fake_herdr):
            def refused():
                before = m.load(self.db, 'fix')
                with self.assertRaises((ValueError, RuntimeError)):
                    m.resume(self.db, dict(id='fix', message='Continue'))
                self.assertEqual(m.load(self.db, 'fix'), before)
                self.assertEqual(self.launches, 0)
            for change in (dict(usage={'1': {}}), dict(launch_stage='uncertain'),
                           dict(missing_from='running'), dict(startup_state='failed'),
                           dict(branch='foreign'), dict(endpoint_receipt={}),
                           dict(pending_scope={'brief': 'not approved'})):
                with self.db: m.save(self.db, dict(original, **change))
                refused()
            with self.db: m.save(self.db, original)
            for name in ('session.jsonl', 'events-1.jsonl', 'stderr-1.log', 'report-1.txt', 'unknown'):
                artifact = folder / name
                artifact.write_text('')
                refused()
                artifact.unlink()
            self.leases[0]['processes'] = [dict(pid=100), dict(pid=101)]
            refused()
            self.leases[0]['processes'] = [dict(pid=100)]
            with m.lock(folder / 'run.lock'):
                with self.assertRaises(BlockingIOError):
                    m.resume(self.db, dict(id='fix', message='Continue'))
            # The exact old dispatch shape remains recoverable without state migration.
            legacy = dict(original)
            del legacy['launch_stage']
            with self.db: m.save(self.db, legacy)
            def started(t, *args):
                result = self.fake_herdr(t, *args)
                if args[:2] == ('pane', 'run'):
                    current = m.load(self.db, 'fix')
                    current['state'] = 'review'  # A fast run may finish before observation.
                    with self.db:
                        m.save(self.db, current)
                        m.event(self.db, current, 'worker-started', 'fixture Pi start')
                return result
            with patch.object(m, 'herdr', started):
                recovered = m.resume(self.db, dict(id='fix', message='Proceed within scope'))
            self.assertEqual(recovered['launch_confirmation'], 'started')
            self.assertEqual((recovered['attempt'], recovered['state']), (2, 'review'))
            self.assertEqual(recovered['launch_recoveries'][0]['task'], legacy)
            for key in ('lease', 'holder', 'worktree', 'pane', 'tab', 'endpoint_receipt',
                        'sha', 'branch', 'brief', 'approved_at', 'model', 'effort'):
                self.assertEqual(recovered[key], original[key])
            self.assertIn(original['brief'], recovered['followup'])
            self.assertEqual(dirty.read_text(), 'preserved startup output')
            self.assertFalse((folder / 'report-1.txt').exists())
            self.assertEqual((self.acquires, self.launches), (1, 1))
            # Timeout is observation only: no retry, report fabrication or state reset.
            with self.db:
                self.db.execute("DELETE FROM events WHERE task='fix' AND kind='worker-started'")
                current = m.load(self.db, 'fix')
                current['state'] = 'launching'
                m.save(self.db, current)
            with patch.object(m.time, 'monotonic', side_effect=[0, 11]):
                result = m.confirm_recovered_worker(self.db, current)
            self.assertEqual(result['launch_confirmation'], 'unconfirmed')
            self.assertEqual(m.load(self.db, 'fix'), current)
            self.assertEqual(self.launches, 1)
            with self.assertRaises(ValueError):
                m.resume(self.db, dict(id='fix', message='Duplicate'))

    def test_pane_launch_guard_and_missing_continuation_recovery(self):
        self.propose()
        m.approve(self.db, dict(id='fix', sha=self.sha))
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', self.fake_herdr):
            task = self.dispatch(effort='high')
            task.update(state='review', usage={'1': m.empty_usage()}, original_brief=task['brief'],
                        scope_history=[dict(brief='Approved addition', approved_by='human', first_attempt=2)])
            with self.db: m.save(self.db, task)
            folder = self.home / 'fix'
            session = folder / 'session.jsonl'
            session.write_text(json.dumps(dict(type='session', version=3,
                id='01a08e47-9d5b-7355-a8e7-bc6b9f472ef5', cwd=task['worktree'])) + '\n')
            (folder / 'report-1.txt').write_text('Retained evidence')
            dirty = Path(task['worktree']) / 'keep.txt'
            dirty.write_text('Uncommitted edits')
            # Vite in the foreground: no command, no attempt increment or journal change.
            def busy(t, *args):
                result = self.fake_herdr(t, *args)
                if args[:2] == ('pane', 'process-info'):
                    result['process_info']['foreground_processes'] = [dict(pid=101)]
                return result
            with patch.object(m, 'herdr', busy):
                with self.assertRaisesRegex(ValueError, 'idle shell'):
                    m.resume(self.db, dict(id='fix', message='Continue'))
                with self.assertRaisesRegex(ValueError, 'idle shell'): m.launch_worker(task)
            self.assertEqual(m.load(self.db, 'fix'), task)
            self.assertEqual(self.launches, 1)
            # Reproduce a pre-upgrade command swallowed by Vite, then reconciled.
            task.update(state='launching', attempt=2, followup='Continue approved scope')
            with self.db: m.save(self.db, task)
            with patch.object(m.time, 'time', return_value=time.time() + 61): m.reconcile(self.db)
            failed = m.load(self.db, 'fix')
            del failed['missing_from']  # Legacy attempt 7 has no phase marker.
            with self.db: m.save(self.db, failed)
            events = m.snapshot(self.db, {})['events']
            with m.lock(folder / 'run.lock'):
                with self.assertRaises(BlockingIOError): m.resume(self.db, dict(id='fix', message='Continue'))
            with patch.object(m, 'herdr', busy):
                with self.assertRaises(ValueError): m.resume(self.db, dict(id='fix', message='Continue'))
            self.assertEqual(m.load(self.db, 'fix'), failed)
            commands = []
            def capture(t, *args):
                if args[:2] == ('pane', 'run'): commands.append(args[3])
                return self.fake_herdr(t, *args)
            def shared_tty_helpers(args, cwd=None, timeout=30):
                output = self.fake_run(args, cwd, timeout)
                if args[0] == 'ps':
                    output += '\n110 1 110 ttys100 zsh -zsh\n111 110 110 ttys100 gitstatusd gitstatusd'
                return output
            with patch.object(m, 'herdr', capture), patch.object(m, 'run', shared_tty_helpers):
                continued = m.resume(self.db, dict(id='fix', message='Continue approved scope'))
            self.assertEqual((continued['state'], continued['attempt']), ('launching', 3))
            self.assertEqual(continued['launch_recoveries'][0]['task'], failed)
            self.assertEqual(continued['launch_recoveries'][0]['outcome'], 'launch-failed')
            for key in ('approved_at', 'brief', 'original_brief', 'scope_history', 'repo', 'sha', 'branch', 'worktree', 'lease', 'holder', 'endpoint_receipt', 'model', 'effort', 'usage'):
                self.assertEqual(continued[key], failed[key])
            self.assertTrue(commands[0].startswith('cd -- '))
            self.assertIn(task['worktree'], commands[0])
            self.assertEqual(dirty.read_text(), 'Uncommitted edits')
            self.assertEqual((folder / 'report-1.txt').read_text(), 'Retained evidence')
            self.assertEqual(json.loads(session.read_text())['id'], '01a08e47-9d5b-7355-a8e7-bc6b9f472ef5')
            self.assertFalse((folder / 'report-2.txt').exists())
            self.assertEqual((self.acquires, self.launches), (1, 2))
            self.assertEqual(m.snapshot(self.db, {})['events'][0], events[0])
            self.assertEqual(m.snapshot(self.db, {})['events'][-1]['kind'], 'launch-recovered')
            with self.assertRaises(ValueError): m.resume(self.db, dict(id='fix', message='Duplicate'))
            self.db.close(); self.db = m.connect()
            self.assertEqual(m.load(self.db, 'fix'), continued)

    def test_missing_launch_recovery_refuses_evidence_identity_and_process_uncertainty(self):
        self.propose()
        m.approve(self.db, dict(id='fix', sha=self.sha))
        with patch.object(m, 'run', self.fake_run), patch.object(m, 'herdr', self.fake_herdr):
            task = self.dispatch()
            folder = self.home / 'fix'
            session = folder / 'session.jsonl'
            session.write_text(json.dumps(dict(type='session', version=3,
                id='01a08e47-9d5b-7355-a8e7-bc6b9f472ef5', cwd=task['worktree'])) + '\n')
            (folder / 'report-1.txt').write_text('Old report')
            task.update(state='launching', attempt=2, followup='Continue', usage={'1': m.empty_usage()})
            with self.db: m.save(self.db, task)
            with patch.object(m.time, 'time', return_value=time.time() + 61): m.reconcile(self.db)
            task = m.load(self.db, 'fix')
            def refused():
                before = m.load(self.db, 'fix')
                with self.assertRaises((ValueError, RuntimeError, FileNotFoundError)):
                    m.resume(self.db, dict(id='fix', message='Continue'))
                self.assertEqual(m.load(self.db, 'fix'), before)
                self.assertEqual(self.launches, 1)
            for change in (dict(missing_from='running'), dict(startup_state='failed'),
                           dict(usage={'1': {}, '2': {}}), dict(pending_scope={'brief': 'pending'}),
                           dict(approved_at=None), dict(branch='foreign'), dict(sha='f' * 40),
                           dict(endpoint_receipt={}), dict(error='Pi forcibly terminated')):
                with self.db: m.save(self.db, dict(task, **change))
                refused()
            with self.db: m.save(self.db, task)
            for name in ('events-2.jsonl', 'stderr-2.log', 'report-2.txt'):
                artifact = folder / name
                artifact.write_text('')
                refused()
                artifact.unlink()
            original_session = session.read_bytes()
            session.unlink(); refused()
            session.write_text('{broken'); refused()
            session.write_bytes(original_session)
            future = time.time_ns() + 1_000_000_000
            os.utime(session, ns=(future, future)); refused()
            os.utime(session, ns=(1, 1))
            for inventory in (None, [], [dict(pid=999)], [dict(pid=100), dict(pid=101, name='pi')]):
                self.leases[0]['processes'] = inventory
                refused()
            self.leases[0]['processes'] = [dict(pid=100)]
            for suffix in ('\n101 100 101 ttys100 node vite',
                           f'\n101 1 101 ttys100 pi pi --session {session}',
                           f'\n101 1 101 ?? pi pi --session {session}',
                           '\n101 1 101 ?? pi pi --session 01a08e47-9d5b-7355-a8e7-bc6b9f472ef5'):
                def processes(args, cwd=None, timeout=30):
                    result = self.fake_run(args, cwd, timeout)
                    return result + suffix if args[0] == 'ps' else result
                with patch.object(m, 'run', processes): refused()
            with patch.object(m, 'run', side_effect=RuntimeError('Process inventory unavailable')): refused()
            self.leases[0]['lease_holder'] = 'foreign'; refused()
            self.leases[0]['lease_holder'] = task['holder']
            other = self.propose('other')
            for state in ('attention', 'running'):
                other['state'] = state
                with self.db: m.save(self.db, other)
                if state == 'running':
                    third = self.propose('third'); third['state'] = 'running'
                    with self.db: m.save(self.db, third)
                refused()

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
        brief = ('## User intent\nตรวจ fixture โดยไม่แก้โค้ด\n'
                 '## Mate spec\nInspect fixture; return evidence.\n'
                 '## Exclusions\nNo edits, tests or push.\n'
                 '## Acceptance evidence\nFile references; tests NOT RUN.\n'
                 '## Stop conditions\nAsk if the fixture is unavailable.')
        m.propose(self.db, dict(id='fix', repo=str(self.repo), base='main', brief=brief))
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
        task['launch_recoveries'] = [dict(outcome='launch-failed', task=dict(attempt=0))]
        with self.db:
            m.save(self.db, task)
        env = dict(os.environ, PATH=str(fakebin) + os.pathsep + os.environ["PATH"], HERDR_PANE_ID=task["pane"])
        command = [sys.executable, str(ROOT / "bin/mate.py"), "worker", "fix", "1"]
        output = subprocess.run(command, env=env, cwd=task["worktree"], capture_output=True, text=True, check=True, timeout=10)
        self.assertIn('Native Pi terminal output', output.stdout)
        self.assertEqual(m.load(self.db, "fix")["state"], "review")
        self.assertEqual(m.confirm_recovered_worker(self.db, task)['launch_confirmation'], 'started')
        self.assertEqual(m.snapshot(self.db, {})['tasks'][0]['usage_total']['estimated_cost_usd'], 0.125)
        argv = json.loads((self.home / "argv.json").read_text())
        self.assertEqual(argv[-1], brief, 'approved brief reaches worker unchanged')
        self.assertEqual(m.snapshot(self.db, dict(id='fix'))['tasks'][0]['brief'], brief)
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
            pending = m.propose_scope(self.db, dict(id='fix', brief='Also check accessibility.'))['pending_scope']
            m.review_scope(self.db, dict(id='fix', token=pending['token'], attempt=1, sha=self.sha, approve=True))
            m.resume(self.db, dict(id="fix", message="Recheck the approved scope"))
        command[-1] = "2"
        env["TEST_PROVIDER_ERROR"] = "1"
        subprocess.run(command, env=env, cwd=task["worktree"], capture_output=True, check=True, timeout=10)
        self.assertEqual(m.load(self.db, "fix")["state"], "failed")
        self.assertEqual(m.snapshot(self.db, {})['tasks'][0]['usage_total']['estimated_cost_usd'], 0.25)
        self.assertEqual(m.snapshot(self.db, {'id': 'fix', 'attempt': 1})['attempt_usage']['messages'], 1)
        argv = json.loads((self.home / "argv.json").read_text())
        self.assertEqual(argv[argv.index("--model") + 1], "selected-model")
        self.assertEqual(argv[argv.index("--thinking") + 1], "high")
        self.assertIn('Current human-approved scope', argv[-1])
        self.assertIn('Also check accessibility.', argv[-1])
        self.assertIn('Recheck the approved scope', argv[-1])
        self.assertEqual(argv[argv.index('--session') + 1], str(self.home / 'fix/session.jsonl'))
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
