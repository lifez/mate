"""Durable notification mirror, cursor continuity, and primary acknowledgement."""
from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
import mate_remote as inbox
import mate_remote_primary as primary
import mate_remote_events as events
sys.path.pop(0)


class RemoteEventsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mate-events-test-')
        root = Path(self.tmp.name)
        self.route = dict(host='fixture', home=str(uuid.uuid4()), primary=str(uuid.uuid4()), outbox=str(root/'outbox'))
        inbox.create(root/'inbox', self.route['home'], self.route['primary'])
        primary.create(self.route)
        self.remote = inbox.connect(root/'inbox', self.route['home'], self.route['primary'])
        self.local = primary.connect(self.route)

    def tearDown(self):
        self.local.close(); self.remote.close(); self.tmp.cleanup()

    def send(self, method, params):
        self.assertEqual(method, 'changes')
        return dict(ok=True, result=events.changes(self.remote, **params))

    def test_replay_ack_and_restart_preserve_exactly_one_local_event(self):
        payload = dict(task='fixture', attempt=1, kind='report', note='untrusted report ready')
        events.append(self.remote, 'event:1', payload)
        events.append(self.remote, 'event:1', payload)
        page = events.mirror(self.local, self.send)
        self.assertEqual(page['events'], [dict(id=1, **payload)])
        self.assertEqual(events.mirror(self.local, self.send)['events'], page['events'])
        self.local.close(); self.local = primary.connect(self.route)
        self.assertEqual(events.pending(self.local)['events'], page['events'])
        with self.assertRaises(ValueError): events.acknowledge(self.local, [1, 999], 'not all valid')
        self.assertEqual(len(events.pending(self.local)['events']), 1)
        events.acknowledge(self.local, [1], 'Reported result to human')
        self.assertEqual(events.mirror(self.local, self.send)['events'], [])
        self.assertEqual(self.remote.execute('SELECT COUNT(*) FROM notifications').fetchone()[0], 1)

    def test_cursor_and_local_evidence_commit_atomically(self):
        events.append(self.remote, 'event:1', dict(task='fixture', kind='report'))
        self.local.execute("CREATE TRIGGER crash BEFORE UPDATE ON mirror_cursor BEGIN SELECT RAISE(ABORT,'fixture crash'); END")
        with self.assertRaises(inbox.sqlite3.IntegrityError): events.mirror(self.local, self.send)
        self.assertEqual(events.pending(self.local)['events'], [])
        self.assertEqual(self.local.execute('SELECT cursor FROM mirror_cursor').fetchone()[0], 0)
        self.local.execute('DROP TRIGGER crash')
        self.assertEqual(len(events.mirror(self.local, self.send)['events']), 1)

    def test_changed_source_stream_and_truncated_prefix_refuse_reset(self):
        events.append(self.remote, 'event:1', {'kind': 'report'})
        with self.assertRaises(ValueError): events.append(self.remote, 'event:1', {'kind': 'changed'})
        events.mirror(self.local, self.send)
        with self.remote:
            self.remote.execute('DELETE FROM notifications')
        with self.assertRaises(ValueError): events.mirror(self.local, self.send)
        self.assertEqual(self.local.execute('SELECT cursor FROM mirror_cursor').fetchone()[0], 1)
        with self.remote:
            self.remote.execute("UPDATE remote_state SET data=? WHERE key='stream'", (str(uuid.uuid4()),))
        with self.assertRaises(ValueError): events.mirror(self.local, lambda *_: dict(ok=True, result=events.changes(self.remote, 0, '')))
        self.assertEqual(len(events.pending(self.local)['events']), 1)

    def test_tampered_payload_and_bounded_pages(self):
        for i in range(25): events.append(self.remote, f'event:{i}', dict(task='fixture', kind='report', note='x'*2000))
        def tamper(method, params):
            reply = self.send(method, params)
            reply['result']['records'][0]['payload']['note'] = 'changed in transit'
            return reply
        with self.assertRaises(ValueError): events.mirror(self.local, tamper)
        self.assertEqual(events.pending(self.local)['events'], [])
        first = events.mirror(self.local, self.send)
        self.assertTrue(first['more']); self.assertEqual(len(first['events']), 20)
        second = events.mirror(self.local, self.send)
        self.assertFalse(second['more']); self.assertEqual(len(second['events']), 25)
        with self.assertRaises(ValueError): events.append(self.remote, 'bad', {'id': 3})
        with self.assertRaises(ValueError): events.append(self.remote, 'big', {'note': 'x'*9000})


if __name__ == '__main__': unittest.main()
