"""Disposable inbox tests: no SSH, live homes, workers or model calls."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

SOURCE = Path(__file__).resolve().parents[1] / "bin/mate_remote.py"
sys.path.insert(0, str(SOURCE.parent))
import mate_remote as r
import mate_remote_events
sys.path.pop(0)


class RemoteInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mate-remote-test-")
        self.path = Path(self.tmp.name) / "inbox.sqlite3"
        self.home, self.primary = str(uuid.uuid4()), str(uuid.uuid4())
        r.create(self.path, self.home, self.primary)
        self.db = r.connect(self.path, self.home, self.primary)
        self.request = dict(version=1, home=self.home, primary=self.primary,
                            id=str(uuid.uuid4()), body=dict(brief="ตรวจงาน", sha="a" * 40))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_lost_receipt_reopen_and_conflicting_replay(self):
        receipt = r.accept(self.db, self.request)
        self.db.close()
        self.db = r.connect(self.path, self.home, self.primary)
        self.assertEqual(r.accept(self.db, self.request), receipt)
        with self.assertRaisesRegex(ValueError, "Conflicting replay"):
            r.accept(self.db, dict(self.request, body=dict(brief="different")))
        claim = r.claim(self.db, self.request["id"])
        r.finish(self.db, self.request["id"], claim["token"], {"state": "review"})
        self.assertEqual(r.accept(self.db, self.request), receipt)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 1)

    def test_wrong_identity_missing_home_and_reprovision_refused(self):
        with self.assertRaises(ValueError):
            r.connect(self.path, str(uuid.uuid4()), self.primary)
        with self.assertRaises(ValueError):
            r.connect(self.path, self.home, str(uuid.uuid4()))
        with self.assertRaises(FileExistsError):
            r.create(self.path, str(uuid.uuid4()), self.primary)
        missing = self.path.with_name("missing.sqlite3")
        with self.assertRaises(r.sqlite3.OperationalError):
            r.connect(missing, self.home, self.primary)
        self.assertFalse(missing.exists())
        for key in ("home", "primary"):
            with self.assertRaises(ValueError):
                r.accept(self.db, dict(self.request, **{key: str(uuid.uuid4())}))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_concurrent_delivery_and_claim_have_single_owner(self):
        def deliver(_):
            db = r.connect(self.path, self.home, self.primary)
            try:
                receipt = r.accept(db, self.request)
                try:
                    claim = r.claim(db, self.request["id"])
                except ValueError:
                    claim = None
                return receipt, claim
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(deliver, range(4)))
        self.assertEqual(len({json.dumps(item[0], sort_keys=True) for item in results}), 1)
        self.assertEqual(sum(item[1] is not None for item in results), 1)

    def test_process_crash_after_claim_never_requeues(self):
        r.accept(self.db, self.request)
        code = """
import sys, os
sys.path.insert(0, sys.argv[1])
import mate_remote as r
db = r.connect(sys.argv[2], sys.argv[3], sys.argv[4])
r.claim(db, sys.argv[5])
os._exit(23)
"""
        result = subprocess.run([sys.executable, "-c", code, str(SOURCE.parent), str(self.path),
                                 self.home, self.primary, self.request["id"]], timeout=10)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(r.status(self.db, self.request["id"])["state"], "claimed")
        r.accept(self.db, self.request)
        with self.assertRaises(ValueError):
            r.claim(self.db, self.request["id"])
        self.assertEqual(r.outcomes(self.db)["records"], [])

    def test_atomic_outcome_and_replay_without_duplicate_events(self):
        r.accept(self.db, self.request)
        claim = r.claim(self.db, self.request["id"])
        with self.assertRaises(ValueError):
            r.finish(self.db, self.request["id"], str(uuid.uuid4()), {"report": "wrong"})
        # Simulate event insertion failure: request must remain claimed, not done.
        self.db.execute("CREATE TRIGGER fail_event BEFORE INSERT ON outcomes BEGIN SELECT RAISE(ABORT, 'failure'); END")
        with self.assertRaises(r.sqlite3.IntegrityError):
            r.finish(self.db, self.request["id"], claim["token"], {"report": "result"})
        self.assertEqual(r.status(self.db, self.request["id"])["state"], "claimed")
        self.db.execute("DROP TRIGGER fail_event")
        for _ in range(2):
            r.finish(self.db, self.request["id"], claim["token"], {"report": "result"})
        with self.assertRaises(ValueError):
            r.finish(self.db, self.request["id"], claim["token"], {"report": "changed"})
        first = r.outcomes(self.db)
        self.assertEqual(len(first["records"]), 1)
        self.assertEqual(r.outcomes(self.db), first)  # Read never consumes evidence.
        self.assertEqual(r.outcomes(self.db, first["next_cursor"])["records"], [])
        with self.assertRaises(ValueError):
            r.outcomes(self.db, first["next_cursor"] + 1)

    def test_envelope_limits_and_bounded_pages(self):
        bad = [dict(self.request, version=True), dict(self.request, version=2),
               dict(self.request, id="../task"), dict(self.request, extra="field"),
               dict(self.request, body=[]), dict(self.request, body={"n": float("nan")}),
               dict(self.request, body={"text": "x" * r.MAX_BYTES})]
        for request in bad:
            with self.subTest(request_type=str(request)[:80]), self.assertRaises(ValueError):
                r.accept(self.db, request)
        for _ in range(3):
            request = dict(self.request, id=str(uuid.uuid4()))
            r.accept(self.db, request)
            claim = r.claim(self.db, request["id"])
            r.finish(self.db, request["id"], claim["token"], {"report": "x" * 150000})
        page = r.outcomes(self.db)
        self.assertEqual(len(page["records"]), 1)
        self.assertTrue(page["more"])
        self.assertLess(len(json.dumps(page).encode()), r.MAX_BYTES)
        next_page = r.outcomes(self.db, page["next_cursor"])
        self.assertGreater(next_page["next_cursor"], page["next_cursor"])
        for cursor, limit in ((True, 1), (-1, 1), (0, 0), (0, 51)):
            with self.assertRaises(ValueError):
                r.outcomes(self.db, cursor, limit)


if __name__ == "__main__":
    unittest.main()
