"""Exercise the real Mate task methods with disposable home/repo and inbox."""
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import mate_remote as inbox
import mate_remote_runtime as runtime
sys.path.pop(0)
spec = importlib.util.spec_from_file_location("mate_runtime_fixture", BIN / "mate.py")
mate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mate)


class RemoteRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mate-runtime-test-")
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.config = self.root / "mate.config.json"
        self.config.write_text("{}")
        self.patch_home = patch.object(mate, "HOME", self.home)
        self.patch_config = patch.object(mate, "CONFIG", self.config)
        self.patch_home.start(); self.patch_config.start()
        self.db = mate.connect()
        self.binding = dict(home=str(uuid.uuid4()), primary=str(uuid.uuid4()), journal=str(self.root / "inbox.sqlite3"))
        inbox.create(self.binding["journal"], self.binding["home"], self.binding["primary"])
        self.role = self.home / "remote.json"
        self.role.write_text(json.dumps(self.binding)); self.role.chmod(0o600)
        self.methods = {name: getattr(mate, name) for name in
                        ("propose", "approve", "propose_scope", "review_scope", "dispatch", "complete",
                         "cancel", "close_tab", "return_lease")}
        self.methods["status"] = mate.snapshot
        self.remote = runtime.open_runtime(mate, self.db, self.methods)
        self.repo = self.root / "repo"; self.repo.mkdir()
        mate.git(self.repo, "init", "-b", "main")
        mate.git(self.repo, "-c", "user.name=Mate Test", "-c", "user.email=mate@test.invalid",
                 "commit", "--allow-empty", "-m", "base")
        self.params = dict(id="test", repo=str(self.repo), base="main", brief="Read-only fixture")

    def tearDown(self):
        self.remote.close(); self.db.close()
        self.patch_home.stop(); self.patch_config.stop(); self.tmp.cleanup()

    def enqueue(self, method, params, confirmation=None):
        body = dict(method=method, params=params)
        if confirmation is not None:
            body["confirmation"] = confirmation
        request = dict(version=1, home=self.binding["home"], primary=self.binding["primary"],
                       id=str(uuid.uuid4()), body=body)
        inbox.accept(self.remote.queue, request)
        return request

    def call(self, method, params, confirmation=None):
        request = self.enqueue(method, params, confirmation)
        self.remote.poll()
        return inbox.status(self.remote.queue, request["id"])["outcome"]

    def test_proposal_parent_approval_and_local_human_gate(self):
        proposed = self.remote.local("propose", self.params)
        for method in runtime.HUMAN:
            with self.subTest(method=method), self.assertRaisesRegex(ValueError, "primary human"):
                self.remote.local(method, dict(id="test"))
        # The existing dispatch guard still prevents unapproved acquisition.
        self.assertEqual(self.call("dispatch", {"id": "test"})["result"]["state"], "awaiting-base")
        result = self.call("approve", dict(id="test", sha=proposed["sha"], brief=proposed["brief"]),
                           proposed["confirmation"])
        self.assertTrue(result["ok"], result)
        task = mate.load(self.db, "test")
        self.assertEqual(task["state"], "approved")
        self.assertEqual(task["parent_approval"]["primary"], self.binding["primary"])
        self.assertEqual(task["parent_approval"]["confirmation"], proposed["confirmation"])
        self.assertEqual(task["attempt"], 0)
        self.assertNotIn("lease", task)
        self.assertEqual(self.db.execute("SELECT kind FROM events").fetchall(), [("base-approved",)])

    def test_stale_scope_and_missing_confirmation_are_refused(self):
        old = self.remote.local("propose", self.params)
        new = self.remote.local("propose", dict(self.params, brief="Revised scope"))
        self.assertNotEqual(old["confirmation"], new["confirmation"])
        p = dict(id="test", sha=old["sha"], brief=old["brief"])
        for confirmation in (None, old["confirmation"], "forged"):
            result = self.call("approve", p, confirmation)
            self.assertTrue(result["refused"], result)
        self.assertEqual(mate.load(self.db, "test")["state"], "awaiting-base")
        p.update(brief=new["brief"])
        self.assertTrue(self.call("approve", p, new["confirmation"])["ok"])

    def test_snapshot_confirmation_covers_exact_stored_identity(self):
        self.remote.local("propose", self.params)
        result = self.call("status", dict(id="test", history=True))["result"]
        self.assertEqual(result["approval_via"], "primary-only")
        row = result["tasks"][0]
        stored = mate.load(self.db, "test")
        self.assertEqual(row["confirmation"], runtime.revision(self.binding, stored))
        for field in ("repo", "sha", "branch", "brief"):
            self.assertNotEqual(row["confirmation"], runtime.revision(self.binding, dict(stored, **{field: "changed"})))
        self.assertNotEqual(row["confirmation"], runtime.revision(dict(self.binding, home=str(uuid.uuid4())), stored))

    def test_snapshot_never_pairs_old_scope_with_a_new_confirmation(self):
        self.remote.local("propose", self.params)
        original = mate.load(self.db, "test")
        def racing_status(db, params):
            shown = mate.snapshot(db, params)
            other = mate.connect()
            try:
                task = mate.load(other, "test")
                task["brief"] = "changed concurrently"
                with other: mate.save(other, task)
            finally:
                other.close()
            return shown
        self.methods["status"] = racing_status
        shown = self.remote.local("status", {"id": "test"})["tasks"][0]
        self.assertEqual(shown["brief"], original["brief"])
        self.assertEqual(shown["confirmation"], runtime.revision(self.binding, original))
        result = self.call("approve", dict(id="test", sha=shown["sha"], brief=shown["brief"]), shown["confirmation"])
        self.assertTrue(result["refused"], result)

    def test_scope_addition_requires_current_parent_confirmation(self):
        proposed = self.remote.local("propose", self.params)
        task = mate.load(self.db, "test")
        task.update(state="review", attempt=1)
        with self.db: mate.save(self.db, task)
        (self.home / "test").mkdir()
        pending = self.remote.local("propose_scope", dict(id="test", brief="Additional read-only check"))
        p = dict(id="test", token=pending["pending_scope"]["token"], attempt=1, sha=pending["sha"], approve=True)
        self.assertTrue(self.call("review_scope", p, proposed["confirmation"])["refused"])
        accepted = self.call("review_scope", p, pending["confirmation"])
        self.assertTrue(accepted["ok"], accepted)
        history = mate.load(self.db, "test")["scope_history"]
        self.assertEqual(history[-1]["parent_approval"]["primary"], self.binding["primary"])
        self.assertEqual(history[-1]["first_attempt"], 2)

    def test_binding_loss_and_retargeting_fail_closed(self):
        self.role.unlink()
        with self.assertRaises(ValueError): runtime.open_runtime(mate, self.db, self.methods)
        with self.assertRaises(FileNotFoundError): self.remote.local("status", {})
        self.role.write_text(json.dumps(dict(self.binding, primary=str(uuid.uuid4())))); self.role.chmod(0o600)
        with self.assertRaises(ValueError): runtime.open_runtime(mate, self.db, self.methods)
        with self.assertRaises(ValueError): self.remote.poll()

    def test_crash_after_approval_does_not_repeat_or_lose_evidence(self):
        proposed = self.remote.local("propose", self.params)
        request = self.enqueue("approve", dict(id="test", sha=proposed["sha"], brief=proposed["brief"]),
                               proposed["confirmation"])
        with patch.object(inbox, "finish", side_effect=SystemExit("fixture crash before receipt")):
            with self.assertRaises(SystemExit): self.remote.poll()
        self.assertEqual(mate.load(self.db, "test")["state"], "approved")
        self.assertEqual(inbox.status(self.remote.queue, request["id"])["state"], "claimed")
        self.remote.close()
        self.remote = runtime.open_runtime(mate, self.db, self.methods)
        self.remote.poll()
        self.assertEqual(inbox.status(self.remote.queue, request["id"])["state"], "claimed")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM events WHERE kind='base-approved'").fetchone()[0], 1)

    def test_unknown_operations_and_reserved_authority_refused(self):
        for method, params in (("shell", {}), ("memory", {}), ("propose", {"_parent_authority": {}})):
            result = self.call(method, params)
            self.assertTrue(result["refused"])
        self.assertEqual(mate.tasks(self.db), [])

    def test_real_serve_process_consumes_inbox_and_denies_local_approval(self):
        install = self.root / "installation"
        (install / "bin").mkdir(parents=True)
        for name in ("mate.py", "mate_remote.py", "mate_remote_primary.py", "mate_remote_transport.py", "mate_remote_runtime.py", "mate_remote_events.py"):
            shutil.copyfile(BIN / name, install / "bin" / name)
        (install / "mate.config.json").write_text("{}")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("HERDR_", "MATE_"))}
        env["MATE_HOME"] = str(self.home)
        child = subprocess.Popen([sys.executable, str(install / "bin/mate.py"), "serve"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, env=env)
        def line():
            self.assertTrue(select.select([child.stdout], [], [], 10)[0], "Control-plane reply timed out")
            return json.loads(child.stdout.readline())
        try:
            self.assertEqual(line()["role"], "secondmate")
            child.stdin.write(json.dumps(dict(id=1, method="propose", params=self.params)) + "\n"); child.stdin.flush()
            proposed = line()["result"]
            child.stdin.write(json.dumps(dict(id=2, method="approve", params=dict(id="test", sha=proposed["sha"]))) + "\n"); child.stdin.flush()
            self.assertIn("primary human", line()["error"])
            request = self.enqueue("approve", dict(id="test", sha=proposed["sha"], brief=proposed["brief"]),
                                   proposed["confirmation"])
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                result = inbox.status(self.remote.queue, request["id"])
                if result["state"] == "done": break
                time.sleep(.1)
            self.assertEqual(result["state"], "done", result)
            self.assertTrue(result["outcome"]["ok"], result)
            self.assertEqual(mate.load(self.db, "test")["state"], "approved")
        finally:
            child.stdin.close()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()
            errors = child.stderr.read()
            child.stdout.close(); child.stderr.close()
        self.assertEqual(child.returncode, 0, errors)


if __name__ == "__main__":
    unittest.main()
