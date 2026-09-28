"""Real subprocess transport/receiver with a disposable deterministic SSH boundary."""
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import mate_remote as inbox
import mate_remote_primary as primary
import mate_remote_transport as transport
sys.path.pop(0)


class RemoteTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mate-ssh-test-")
        self.root = Path(self.tmp.name)
        self.route = dict(host="test-host", home=str(uuid.uuid4()), primary=str(uuid.uuid4()),
                          outbox=str(self.root / "outbox.sqlite3"))
        primary.create(self.route)
        self.journal = self.root / "inbox.sqlite3"
        inbox.create(self.journal, self.route["home"], self.route["primary"])
        self.config = self.root / "receiver.json"
        self.config.write_text(json.dumps(dict(home=self.route["home"], primary=self.route["primary"],
                                               journal=str(self.journal))))
        self.config.chmod(0o600)
        self.ssh = self.root / "fake-ssh"
        self.ssh.write_text(f'''#!{sys.executable}
import os, sys
assert sys.argv[-3:] == ["--", "test-host", "mate-remote-v1"]
assert "BatchMode=yes" in sys.argv and "StrictHostKeyChecking=yes" in sys.argv
assert "ForwardAgent=no" in sys.argv and "SendEnv=-*" in sys.argv
os.environ["SSH_ORIGINAL_COMMAND"] = "mate-remote-v1"
os.execv(sys.executable, [sys.executable, {str(BIN / 'mate_remote_transport.py')!r}, "receive", {str(self.config)!r}])
''')
        self.ssh.chmod(0o700)
        self.request = dict(version=1, home=self.route["home"], primary=self.route["primary"],
                            id=str(uuid.uuid4()), body={"brief": "สวัสดี; $(touch should-not-exist)"})

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, method, params):
        return transport.send(self.route, method, params, ssh=str(self.ssh), timeout=5)

    def test_real_receiver_replay_status_and_outcomes(self):
        self.assertTrue(self.send("hello", {})["ok"])
        first = self.send("accept", {"request": self.request})
        self.assertTrue(first["ok"])
        self.assertEqual(first["result"], self.send("accept", {"request": self.request})["result"])
        self.assertEqual(self.send("status", {"id": self.request["id"]})["result"]["state"], "queued")
        changed = dict(self.request, body={"brief": "changed"})
        self.assertFalse(self.send("accept", {"request": changed})["ok"])
        with closing(inbox.connect(self.journal, self.route["home"], self.route["primary"])) as db:
            claim = inbox.claim(db, self.request["id"])
            inbox.finish(db, self.request["id"], claim["token"], {"note": "report"})
        page = self.send("outcomes", {"after": 0, "limit": 50})["result"]
        self.assertEqual(page["records"][0]["outcome"], {"note": "report"})

    def test_lost_reply_remains_uncertain_and_same_request_can_be_inspected(self):
        real_exchange = transport.exchange
        def drop_reply(command, payload, timeout):
            real_exchange(command, payload, timeout)
            raise transport.Uncertain("reply lost after remote commit")
        with patch.object(transport, "exchange", side_effect=drop_reply) as exchange:
            with self.assertRaises(transport.Uncertain):
                self.send("accept", {"request": self.request})
            self.assertEqual(exchange.call_count, 1)  # No automatic retry.
        self.assertEqual(self.send("status", {"id": self.request["id"]})["result"]["state"], "queued")
        self.assertEqual(self.send("accept", {"request": self.request})["result"]["sequence"], 1)

    def test_wrong_home_and_unsupported_operations_never_execute(self):
        with self.assertRaises(transport.Uncertain):
            transport.send(dict(self.route, home=str(uuid.uuid4())), "hello", {}, ssh=str(self.ssh))
        for method in ("dispatch", "approve", "claim", "finish", "shell"):
            with self.assertRaises(ValueError):
                self.send(method, {})
        with self.assertRaises(ValueError):
            self.send("hello", {"journal": "/tmp/other"})

    def test_timeout_excess_output_nonzero_and_forged_reply_are_uncertain(self):
        cases = ["import time; time.sleep(5)",
                 f"import sys; sys.stdout.write('x' * {transport.WIRE_LIMIT + 100})",
                 "import sys; sys.exit(255)"]
        for code in cases:
            with self.subTest(code=code), self.assertRaises(transport.Uncertain):
                transport.exchange([sys.executable, "-c", code], b"{}\n", .2)
        for raw in (b"{}", b'{"ok":true,"ok":false}', b"not json"):
            with patch.object(transport, "exchange", return_value=raw), self.assertRaises(transport.Uncertain):
                self.send("hello", {})
        def forged(command, payload, timeout):
            request = json.loads(payload)
            return json.dumps({k: request[k] for k in ("version", "home", "primary", "call")} |
                              {"ok": True, "result": {"accepted": True}}).encode()
        with patch.object(transport, "exchange", side_effect=forged), self.assertRaises(transport.Uncertain):
            self.send("accept", {"request": self.request})

    def test_long_lived_transport_process_reuses_only_its_pinned_route(self):
        (self.root / "ssh").symlink_to(self.ssh)
        route = self.root / "route.json"
        route.write_text(json.dumps(self.route))
        route.chmod(0o600)
        frames = [dict(method="accept", params={"request": self.request}),
                  dict(method="status", params={"id": self.request["id"]})]
        process = subprocess.run(
            [sys.executable, str(BIN / "mate_remote_transport.py"), "transport", str(route)],
            input="".join(json.dumps(frame) + "\n" for frame in frames), text=True,
            capture_output=True, timeout=10,
            env=dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ.get("PATH", "")))
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(replies), 2)
        self.assertTrue(all(reply["home"] == self.route["home"] for reply in replies))
        self.assertEqual(replies[0]["delivery"]["state"], "accepted")
        self.assertTrue(replies[1]["ok"])
        self.assertEqual(replies[1]["result"]["state"], "queued")

    def test_primary_outbox_commits_before_send_and_rejects_retargeting(self):
        with closing(primary.connect(self.route)) as db:
            primary.stage(db, self.request)
            def send(method, params):
                with closing(primary.connect(self.route)) as reader:
                    self.assertEqual(primary.inspect(reader, self.request["id"])["state"], "sending")
                return self.send(method, params)
            delivered = primary.deliver(db, self.request["id"], send)
            self.assertEqual(delivered["state"], "accepted")
            with patch.object(transport, "send", side_effect=AssertionError("No resubmission")):
                self.assertEqual(primary.deliver(db, self.request["id"], transport.send), delivered)
            with self.assertRaises(ValueError):
                primary.stage(db, dict(self.request, body={"brief": "changed"}))
        for key in ("host", "home", "primary"):
            with self.assertRaises(ValueError):
                primary.connect(dict(self.route, **{key: str(uuid.uuid4())}))
        self.assertEqual(Path(self.route["outbox"]).stat().st_mode & 0o777, 0o600)

    def test_primary_refusal_is_durable_and_missing_outbox_is_not_created(self):
        missing = dict(self.route, outbox=str(self.root / "missing.sqlite3"))
        with self.assertRaises(primary.sqlite3.OperationalError):
            primary.connect(missing)
        self.assertFalse(Path(missing["outbox"]).exists())
        with self.assertRaises(FileExistsError):
            primary.create(self.route)
        self.send("accept", {"request": dict(self.request, body={"brief": "original remote payload"})})
        with closing(primary.connect(self.route)) as db:
            for request in (dict(self.request, version=True), dict(self.request, body=[]),
                            dict(self.request, home=str(uuid.uuid4()))):
                with self.assertRaises(ValueError):
                    primary.stage(db, request)
            primary.stage(db, self.request)
            self.assertEqual(primary.deliver(db, self.request["id"], self.send)["state"], "rejected")
        with closing(primary.connect(self.route)) as db:
            primary.stage(db, self.request)
            with patch.object(transport, "send", side_effect=AssertionError("No resend after refusal")):
                self.assertEqual(primary.deliver(db, self.request["id"], transport.send)["state"], "rejected")

    def test_primary_crash_after_remote_commit_reconciles_without_resend(self):
        with closing(primary.connect(self.route)) as db:
            primary.stage(db, self.request)
        code = '''
import sys, json, os
sys.path.insert(0, sys.argv[1])
import mate_remote_primary as p
import mate_remote_transport as t
route = json.loads(sys.argv[2])
db = p.connect(route)
def send(method, params):
    reply = t.send(route, method, params, ssh=sys.argv[4])
    assert reply['ok']
    os._exit(29)
p.deliver(db, sys.argv[3], send)
'''
        process = subprocess.run([sys.executable, "-c", code, str(BIN), json.dumps(self.route),
                                  self.request["id"], str(self.ssh)], timeout=10)
        self.assertEqual(process.returncode, 29)
        with closing(primary.connect(self.route)) as db:
            def no_send(*_):
                raise AssertionError("Must not resubmit after crash")
            self.assertEqual(primary.deliver(db, self.request["id"], no_send)["state"], "sending")
            calls = []
            def inspect_only(method, params):
                calls.append(method)
                return self.send(method, params)
            result = primary.reconcile(db, self.request["id"], inspect_only)
            self.assertEqual(result["state"], "accepted")
            self.assertEqual(calls, ["status"])
        self.assertEqual(self.send("accept", {"request": self.request})["result"]["sequence"], 1)

    def test_primary_unreachable_missing_or_mismatched_request_never_requeues(self):
        with closing(primary.connect(self.route)) as db:
            primary.stage(db, self.request)
            def disconnected(*_):
                raise transport.Uncertain("offline")
            self.assertEqual(primary.deliver(db, self.request["id"], disconnected)["state"], "uncertain")
            self.assertEqual(primary.reconcile(db, self.request["id"], self.send)["state"], "uncertain")
            wrong = dict(ok=True, result=dict(id=self.request["id"], fingerprint="wrong", state="queued"))
            self.assertEqual(primary.reconcile(db, self.request["id"], lambda *_: wrong)["state"], "uncertain")
            with patch.object(transport, "send", side_effect=AssertionError("No resend")):
                self.assertEqual(primary.deliver(db, self.request["id"], transport.send)["state"], "uncertain")

    def test_primary_concurrent_senders_only_one_delivery(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        with closing(primary.connect(self.route)) as db:
            primary.stage(db, self.request)
        calls, lock = [], threading.Lock()
        def send(method, params):
            with lock:
                calls.append(method)
            return self.send(method, params)
        def deliver(_):
            with closing(primary.connect(self.route)) as db:
                return primary.deliver(db, self.request["id"], send)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(deliver, range(4)))
        self.assertEqual(calls, ["accept"])
        with closing(primary.connect(self.route)) as db:
            self.assertEqual(primary.inspect(db, self.request["id"])["state"], "accepted")

    def test_runtime_result_is_committed_before_unblocking_next_mutation(self):
        with closing(primary.connect(self.route)) as db:
            primary.stage(db, self.request)
            primary.deliver(db, self.request["id"], self.send)
            self.assertEqual(primary.execution(db, self.request["id"], self.send)["state"], "queued")
            with self.assertRaisesRegex(ValueError, "Unresolved"):
                primary.stage(db, dict(self.request, id=str(uuid.uuid4())))
            with closing(inbox.connect(self.journal, self.route["home"], self.route["primary"])) as remote:
                claim = inbox.claim(remote, self.request["id"])
                inbox.finish(remote, self.request["id"], claim["token"], {"ok": True, "result": {"state": "approved"}})
            result = primary.execution(db, self.request["id"], self.send)
            self.assertEqual(result["outcome"]["result"]["state"], "approved")
        with closing(primary.connect(self.route)) as db:
            with patch.object(transport, "send", side_effect=AssertionError("Cached result should not need network")):
                self.assertEqual(primary.execution(db, self.request["id"], transport.send), result)
            self.assertEqual(primary.unresolved(db), [])
            primary.stage(db, dict(self.request, id=str(uuid.uuid4())))

    def test_uncertain_runtime_result_blocks_mutations_but_allows_status(self):
        with closing(primary.connect(self.route)) as db:
            primary.stage(db, self.request)
            primary.deliver(db, self.request["id"], self.send)
            with closing(inbox.connect(self.journal, self.route["home"], self.route["primary"])) as remote:
                claim = inbox.claim(remote, self.request["id"])
                inbox.finish(remote, self.request["id"], claim["token"], {"ok": False, "uncertain": True, "error": "Uncertain launch"})
            primary.execution(db, self.request["id"], self.send)
            self.assertEqual(primary.unresolved(db)[0]["id"], self.request["id"])
            with self.assertRaisesRegex(ValueError, "Unresolved"):
                primary.stage(db, dict(self.request, id=str(uuid.uuid4())))
            primary.stage(db, dict(self.request, id=str(uuid.uuid4()), body={"method": "status", "params": {}}))

    def test_bootstrap_recovery_resolves_only_bootstrap_uncertainty(self):
        start = dict(self.request, body={"method": "secondmate_start", "params": {}})
        recover = dict(start, id=str(uuid.uuid4()), body={"method": "secondmate_recover", "params": {}})
        with closing(primary.connect(self.route)) as db:
            staged = primary.stage(db, start)
            failed = dict(ok=True, result=dict(id=start['id'], fingerprint=staged['fingerprint'],
                          state='done', outcome=dict(ok=False, uncertain=True, error='Lost launch reply')))
            primary.execution(db, start['id'], lambda *_: failed)
            staged = primary.stage(db, recover)
            success = dict(ok=True, result=dict(id=recover['id'], fingerprint=staged['fingerprint'],
                           state='done', outcome=dict(ok=True, result=dict(state='running', resolved_request=start['id']))))
            primary.execution(db, recover['id'], lambda *_: success)
            self.assertEqual(primary.unresolved(db), [])
            self.assertTrue(primary.execution(db, start['id'], lambda *_: self.fail('Must use evidence'))['outcome']['uncertain'])
            primary.stage(db, dict(self.request, id=str(uuid.uuid4()), body={'method': 'dispatch', 'params': {}}))
            with self.assertRaisesRegex(ValueError, 'Unresolved'):
                primary.stage(db, dict(recover, id=str(uuid.uuid4())))

    def test_invalid_outcome_cannot_clear_primary_uncertainty(self):
        with closing(primary.connect(self.route)) as db:
            staged = primary.stage(db, self.request)
            for outcome in ({"ok": True}, {"ok": False, "uncertain": "yes", "error": "bad"}):
                reply = dict(ok=True, result=dict(id=self.request["id"], fingerprint=staged["fingerprint"], state="done", outcome=outcome))
                with self.assertRaises(ValueError):
                    primary.execution(db, self.request["id"], lambda *_: reply)
            self.assertEqual(primary.unresolved(db)[0]["id"], self.request["id"])

    def test_private_fixed_config_and_original_command(self):
        self.config.chmod(0o644)
        with self.assertRaises(ValueError):
            transport.settings(self.config, True)
        self.config.chmod(0o600)
        command = [sys.executable, str(BIN / "mate_remote_transport.py"), "receive", str(self.config)]
        process = subprocess.run(command, input=b"{}\n", capture_output=True,
                                 env=dict(os.environ, SSH_ORIGINAL_COMMAND="rm -rf anything"), timeout=5)
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(process.stdout, b"")
        route = self.root / "route.json"
        for host in ("-option", "host; evil", "user@host", "host\n"):
            route.write_text(json.dumps(dict(self.route, host=host)))
            route.chmod(0o600)
            with self.assertRaises(ValueError):
                transport.settings(route)
        with self.assertRaises(ValueError):
            transport.parse(b'{"x":NaN}')


if __name__ == "__main__":
    unittest.main()
