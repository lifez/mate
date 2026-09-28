"""Bootstrap ownership/journal tests; no real servers or agents are started."""
from contextlib import closing
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import mate_remote as inbox
import mate_remote_events as events
import mate_remote_bootstrap as boot
sys.path.pop(0)


class RemoteBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mate-boot-test-')
        self.home = Path(self.tmp.name)
        self.config = dict(home=str(uuid.uuid4()), primary=str(uuid.uuid4()), journal=str(self.home/'inbox'))
        inbox.create(self.config['journal'], self.config['home'], self.config['primary'])
        self.db = inbox.connect(self.config['journal'], self.config['home'], self.config['primary'])
        self.profile = dict(harness='pi', provider='fixture', model='fixture', effort='off')
        self.context = patch.object(boot, 'context', return_value=(self.home,self.home,self.profile,dict(pi='/fixture/pi',herdr='/fixture/herdr'),{}))
        self.context.start()
        self.session = 'mate-remote-' + self.config['home'].replace('-','')
        self.record = dict(phase='submitted', session=self.session, socket='/fixture/socket',
                           pane='w1:p1', tab='w1:t1', workspace='w1', terminal_id='terminal',
                           request=str(uuid.uuid4()), submitted_at=time.time()-100)

    def tearDown(self):
        self.context.stop(); self.db.close(); self.tmp.cleanup()

    def save(self, heartbeat=False):
        with self.db:
            events.put(self.db,'secondmate',self.record)
            if heartbeat:
                events.put(self.db,'heartbeat',dict(pane=self.record['pane'],socket=self.record['socket'],session=self.session,at=time.time()-30))

    def request(self, method='secondmate_recover', confirmation=None):
        request=dict(version=1,home=self.config['home'],primary=self.config['primary'],id=str(uuid.uuid4()),
                     body=dict(method=method,params={},confirmation=confirmation or boot.confirmation(self.config,events.get(self.db,'secondmate'))))
        inbox.accept(self.db,request)
        return request

    def test_stale_confirmation_has_no_external_effect(self):
        request=self.request('secondmate_start', 'stale')
        with patch.object(boot,'command',side_effect=AssertionError('No command')):
            boot.consume(self.db,self.config,self.home/'remote.json',request)
        self.assertTrue(inbox.status(self.db,request['id'])['outcome']['refused'])
        self.assertIsNone(events.get(self.db,'secondmate'))

    def test_live_secondmate_is_observed_not_relaunched(self):
        self.save(True)
        with self.db: events.put(self.db,'heartbeat',dict(pane=self.record['pane'],socket=self.record['socket'],session=self.session,at=time.time()))
        request=self.request()
        with boot.mate.lock(self.home/'supervisor.lock'), patch.object(boot.mate,'check_endpoint',return_value={'terminal_id':'terminal'}), patch.object(boot,'command',side_effect=AssertionError('No new command')):
            result=boot.launch(self.db,self.config,self.home/'remote.json',request)
        self.assertEqual(result['state'],'running')
        self.assertEqual(result['resolved_request'],self.record['request'])

    def test_unobserved_submission_refuses_recovery_even_if_shell_looks_idle(self):
        self.save()
        request=self.request()
        status=dict(server=dict(session=self.session,socket=self.record['socket'],running=True,compatible=True))
        with patch.object(boot,'command',return_value=status), patch.object(boot.mate,'ready_pane',side_effect=AssertionError('No adoption')):
            boot.consume(self.db,self.config,self.home/'remote.json',request)
        outcome=inbox.status(self.db,request['id'])['outcome']
        self.assertTrue(outcome['refused']);self.assertIn('never observed',outcome['error'])
        self.assertEqual(events.get(self.db,'secondmate'),self.record)

    def test_observed_stopped_supervisor_recovery_journals_before_one_launch(self):
        self.save(True)
        request=self.request()
        status=dict(server=dict(session=self.session,socket=self.record['socket'],running=True,compatible=True))
        calls=[]
        def herdr(record,*args):
            saved=events.get(self.db,'secondmate')
            self.assertEqual(saved['request'],request['id']);self.assertEqual(saved['phase'],'submitted')
            calls.append(args)
        with patch.object(boot,'command',return_value=status), patch.object(boot,'live',side_effect=[False,True]), \
             patch.object(boot.mate,'ready_pane'), patch.object(boot.mate,'check_endpoint',return_value={'cwd':str(self.home)}), \
             patch.object(boot.mate,'run',return_value=''), patch.object(boot.mate,'herdr',side_effect=herdr):
            boot.consume(self.db,self.config,self.home/'remote.json',request)
            boot.consume(self.db,self.config,self.home/'remote.json',request)
        self.assertEqual(len(calls),1)
        outcome=inbox.status(self.db,request['id'])['outcome']
        self.assertTrue(outcome['ok']);self.assertEqual(outcome['result']['resolved_request'],self.record['request'])

    def test_claude_secondmate_trusts_root_and_keeps_one_session(self):
        self.profile.update(harness='claude', provider='anthropic', model='claude-test', effort='high')
        self.save(True)
        status=dict(server=dict(session=self.session,socket=self.record['socket'],running=True,compatible=True))
        commands=[]
        store=self.home/'claude-config'
        for resumed in (False, True):
            request=self.request()
            with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(store)}), patch.object(boot,'command',return_value=status), \
                 patch.object(boot,'live',side_effect=[False,True]), patch.object(boot.mate,'ready_pane'), \
                 patch.object(boot.mate,'check_endpoint',return_value={'cwd':str(self.home)}), patch.object(boot.mate,'run',return_value=''), \
                 patch.object(boot.mate,'claude_trust') as trust, \
                 patch.object(boot.mate,'herdr',side_effect=lambda record,*args: commands.append(args[-1])):
                boot.consume(self.db,self.config,self.home/'remote.json',request)
            self.assertTrue(inbox.status(self.db,request['id'])['outcome']['ok'])
            trust.assert_called_once_with(str(self.home), str(self.home))
            session=boot.claude_session(self.config)
            self.assertIn(('--resume ' if resumed else '--session-id ') + session, commands[-1])
            self.assertIn('bin/mate_claude.py --model claude-test --effort high', commands[-1])
            with patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(store)}):
                transcript=boot.claude_transcript(self.home, session)
            self.assertTrue(str(transcript).startswith(str(store)))
            transcript.parent.mkdir(parents=True, exist_ok=True); transcript.write_text('{}\n')
            self.record=dict(events.get(self.db,'secondmate'),submitted_at=time.time()-100); self.save(True)

    def test_failed_submission_remains_uncertain_and_not_retried(self):
        self.record['phase']='preflight';self.save()
        request=self.request()
        status=dict(server=dict(session=self.session,socket=self.record['socket'],running=True,compatible=True))
        with patch.object(boot,'command',return_value=status), patch.object(boot.mate,'ready_pane'), \
             patch.object(boot.mate,'check_endpoint',return_value={'cwd':str(self.home)}), patch.object(boot.mate,'run',return_value=''), \
             patch.object(boot.mate,'herdr',side_effect=RuntimeError('Lost submission reply')) as launch:
            boot.consume(self.db,self.config,self.home/'remote.json',request)
            boot.consume(self.db,self.config,self.home/'remote.json',request)
        self.assertEqual(launch.call_count,1)
        self.assertTrue(inbox.status(self.db,request['id'])['outcome']['uncertain'])
        self.assertEqual(events.get(self.db,'secondmate')['phase'],'submitted')


if __name__=='__main__': unittest.main()
