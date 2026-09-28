import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/mate_claude.py"
sys.path.insert(0, str(ROOT / "bin"))
import mate_claude as mc  # noqa: E402


class MateClaudeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mate-claude-test-")
        self.root = Path(self.tmp.name).resolve()
        self.config = self.root / "mate.config.json"
        self.config.write_text("{}")
        self.patches = [patch.object(mc.mate, "CONFIG", self.config)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.tmp.cleanup()

    def configure(self, data):
        self.config.write_text(json.dumps(data))

    def test_tool_contract_is_shared_with_the_pi_extension(self):
        texts = mc.descriptions()
        self.assertEqual(set(texts), set(mc.TOOLS))
        self.assertIn("/mate-approve", texts["mate_propose"])
        self.assertEqual(set(mc.RPC), set(mc.TOOLS))
        self.assertEqual(set(mc.SCHEMAS), set(mc.TOOLS))

    def test_dispatch_profile_matches_pi_rules_without_a_model_registry(self):
        self.configure({})
        with self.assertRaisesRegex(ValueError, "Pass a model"):
            mc.dispatch_profile({})
        with self.assertRaisesRegex(ValueError, "provider/model-id"):
            mc.dispatch_profile({"model": "gpt"})
        self.assertEqual(mc.dispatch_profile({"model": "openai-codex/gpt", "effort": "high"}),
                         dict(harness="pi", provider="openai-codex", model="gpt", effort="high"))
        self.assertEqual(mc.dispatch_profile({"harness": "claude", "model": "anthropic/claude-test"}),
                         dict(harness="claude", provider="anthropic", model="claude-test", effort="high"))
        self.configure({"worker": {"model": "openai-codex/gpt", "effort": "xhigh"}})
        self.assertEqual(mc.dispatch_profile({})["model"], "gpt")
        with self.assertRaisesRegex(ValueError, "Pass a model for the claude"):
            mc.dispatch_profile({"harness": "claude"}), "Pi defaults never feed a Claude worker"
        rules = [{"when": "UI", "use": {"harness": "claude", "model": "claude-test", "effort": "high"}}]
        self.configure({"worker": {"model": "openai-codex/gpt", "effort": "xhigh"}, "dispatch": {"rules": rules}})
        with self.assertRaisesRegex(ValueError, "Dispatch rules are active"):
            mc.dispatch_profile({})
        with self.assertRaisesRegex(ValueError, "pass the selected harness"):
            mc.dispatch_profile({"model": "claude-test", "effort": "high"})
        self.assertEqual(mc.dispatch_profile({"harness": "claude", "model": "claude-test", "effort": "high"})["harness"], "claude")
        routing = mc.dispatch_instructions(mc.mate.mate_config())
        self.assertIn('"default":{"model":"openai-codex/gpt","effort":"xhigh"}', routing)
        self.assertEqual(mc.continue_profile({"model": "gpt-2"}, {"provider": "openai-codex"}),
                         {"provider": "openai-codex", "model": "gpt-2"})
        self.assertEqual(mc.continue_profile({"model": "anthropic/c2"}, {"harness": "claude", "provider": "anthropic"}),
                         {"provider": "anthropic", "model": "c2"})

    def test_stow_command_reuses_pi_request_with_claude_reset(self):
        text = mc.stow_prompt()
        self.assertIn("Stow this Mate conversation now.", text)
        self.assertIn("mate_memory action=save", text)
        self.assertIn("run `python3 bin/mate_claude.py` again", text)
        self.assertNotIn("/new loads", text)
        home = self.root / "home"
        with patch.object(mc.mate, "HOME", home), patch.dict(os.environ, {"HERDR_ENV": "1"}), \
                patch.object(mc, "policy", return_value="policy"), patch.object(mc.os, "chdir"), \
                patch.object(mc.os, "execvpe") as launched:
            mc.launch(["--model", "opus"])
        argv = launched.call_args.args[1]
        folder = Path(argv[argv.index("--plugin-dir") + 1])
        self.assertEqual(folder, home / "claude-plugin")
        self.assertEqual(json.loads((folder / ".claude-plugin/plugin.json").read_text())["name"], "mate")
        self.assertIn(text, (folder / "commands/stow.md").read_text())
        self.assertEqual(argv[-2:], ["--model", "opus"])

    def test_launch_requires_herdr(self):
        with patch.dict(os.environ, {"HERDR_ENV": ""}):
            with self.assertRaisesRegex(ValueError, "inside Herdr"):
                mc.launch([])

    def test_remote_fleet_tool_wake_and_human_cli(self):
        home = self.root / "home"
        (home / "remotes").mkdir(parents=True)
        fake = self.root / "transport.py"
        fake.write_text("""import json, sys
config = json.load(open(sys.argv[-1]))
bodies = {}
for line in sys.stdin:
    frame = json.loads(line); method, params = frame['method'], frame['params']
    reply = dict(home=config['home'])
    if method == 'mirror': reply['result'] = dict(events=[dict(id=7, task='fix', kind='report')])
    elif method == 'accept':
        bodies[params['request']['id']] = params['request']['body']
        reply['delivery'] = dict(id=params['request']['id'], state='accepted')
    elif method == 'result':
        reply['execution'] = dict(id=params['id'], state='done', outcome=dict(ok=True, result=dict(state='done', body=bodies[params['id']])))
    elif method == 'ack_events': reply['result'] = dict(acknowledged=params['events'])
    print(json.dumps(reply), flush=True)
""")
        route = dict(host="fixture", home=str(mc.uuid.uuid4()), primary=str(mc.uuid.uuid4()), outbox=str(self.root / "outbox"))
        good = home / "remotes/lab.json"
        good.write_text(json.dumps(route)); good.chmod(0o600)
        loose = home / "remotes/loose.json"
        loose.write_text(json.dumps(route)); loose.chmod(0o644)
        with patch.object(mc.mate, "HOME", home), patch.object(mc.RemoteFleet, "transport", [sys.executable, str(fake)]):
            fleet = mc.RemoteFleet()
            try:
                self.assertEqual(fleet.list(), ["lab", "loose"])
                with self.assertRaisesRegex(ValueError, "private"):
                    fleet.route("loose")
                result = fleet.call("lab", "dispatch", {"id": "fix", "harness": "claude"})
                self.assertEqual(result["body"], {"method": "dispatch", "params": {"id": "fix", "harness": "claude"}})
                with self.assertRaisesRegex(ValueError, "exact displayed confirmation"):
                    fleet.call("lab", "approve", {"id": "fix"})
                serve = type("S", (), {"role": "primary"})()
                with self.assertRaisesRegex(ValueError, "not model tools"):
                    mc.remote_tool(serve, fleet, {"remote": "lab", "operation": "approve", "params": {}})
                self.assertEqual(mc.remote_tool(serve, fleet, {"remote": "lab", "operation": "ack_events",
                                                               "params": {"events": [7], "note": "handled"}}), {"acknowledged": [7]})
                serve.role = "secondmate"
                with self.assertRaisesRegex(ValueError, "primary Mate"):
                    mc.remote_tool(serve, fleet, {"remote": "lab", "operation": "status", "params": {}})
                found = fleet.poll()
                self.assertEqual(found, {"lab": [{"id": 7, "task": "fix", "kind": "report"}]}, "a broken route never blocks another")
                self.assertIn("loose", fleet.retries)
                self.assertEqual(fleet.poll(), found, "the broken route waits for its backoff")
            finally:
                fleet.close()

            state = {"input": json.dumps({"session_id": "s2"})}
            with patch.object(mc, "pending_events", return_value=[]), patch.object(mc, "remote_events", return_value=found), \
                    patch.object(mc.sys, "stdin", type("I", (), {"read": lambda self: state["input"]})()), \
                    patch.object(mc.sys, "stderr", new_callable=__import__("io").StringIO) as err:
                self.assertEqual(mc.wake(), 2)
                self.assertIn('MATE REMOTE EVENT', err.getvalue())
                self.assertIn('remote lab approve TASK', err.getvalue())
                with patch.object(mc.time, "sleep", side_effect=RuntimeError("parked")):
                    with self.assertRaisesRegex(RuntimeError, "parked"):
                        mc.wake()  # Already delivered: no second wake for the same mirror event.

        task = dict(id="fix", remote_home=route["home"], confirmation="a" * 64, state="awaiting-base", attempt=0, repo="/r",
                    base="main", sha="s" * 40, branch="b", brief="Remote scope.")
        calls = []
        def human_call(method, params=None):
            calls.append((method, params))
            if method == "remote_route":
                return route
            if params["method"] == "status":
                return dict(remote_home=route["home"], tasks=[task])
            return dict(state="approved")
        with patch.object(mc, "human_call", side_effect=human_call), patch("builtins.print") as shown:
            mc.human(["remote", "lab", "approve", "fix"])
            text = shown.call_args_list[0].args[0]
            self.assertIn("Remote scope.", text)
            code = re.search(r"--yes ([0-9a-f]{12})", text).group(1)
            self.assertFalse(any(p and p.get("method") == "approve" for _, p in calls), "first run never mutates")
            mc.human(["remote", "lab", "approve", "fix", "--yes", code])
        self.assertEqual(calls[-1][1]["method"], "approve")
        self.assertEqual(calls[-1][1]["confirmation"], "a" * 64, "the displayed remote revision is sent unchanged")

    def test_mcp_human_approval_and_wake_hooks(self):
        home = self.root / "home"
        env = dict(os.environ, MATE_HOME=str(home))
        repo = self.root / "repo"
        repo.mkdir()
        mc.mate.git(repo, "init", "-b", "main")
        mc.mate.git(repo, "-c", "user.name=T", "-c", "user.email=t@test.invalid", "commit", "--allow-empty", "-m", "base")
        with patch.object(mc.mate, "HOME", home):
            with mc.closing(mc.mate.connect()) as db:
                mc.mate.propose(db, dict(id="fix", repo=str(repo), base="main", brief="Inspect fixture."))
        server = subprocess.Popen([sys.executable, str(SCRIPT), "mcp"], env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        try:
            def request(rid, method, params=None):
                server.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}) + "\n")
                server.stdin.flush()
                return json.loads(server.stdout.readline())["result"]
            request(1, "initialize", {"protocolVersion": "2025-06-18"})
            status = json.loads(request(2, "tools/call", {"name": "mate_status", "arguments": {"id": "fix"}})["content"][0]["text"])
            self.assertEqual(status["tasks"][0]["state"], "awaiting-base")
            refused = request(3, "tools/call", {"name": "Bash", "arguments": {"command": "true"}})
            self.assertTrue(refused["isError"])

            def human(*args):
                return subprocess.run([sys.executable, str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30)
            shown = human("approve", "fix")
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertIn("Inspect fixture.", shown.stdout)
            code = re.search(r"--yes ([0-9a-f]{12})", shown.stdout).group(1)
            self.assertIn("token does not match", human("approve", "fix", "--yes", "0" * 12).stderr)
            done = human("approve", "fix", "--yes", code)
            self.assertIn("Approved fix", done.stdout, done.stderr)
            self.assertIn("fix\tapproved", human("list").stdout)

            def wake(session="s1", **kwargs):
                return subprocess.Popen([sys.executable, str(SCRIPT), "wake"], env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)
            first = wake()
            _, err = first.communicate(json.dumps({"session_id": "s1"}), timeout=20)
            self.assertEqual(first.returncode, 2)
            self.assertIn("base-approved", err)
            self.assertNotIn("CORRECTION", err)
            again = wake()
            _, err = again.communicate(json.dumps({"session_id": "s1"}), timeout=20)
            self.assertEqual(again.returncode, 2, "an unhandled delivered event gets one correction")
            self.assertIn("CORRECTION", err)
            attach = subprocess.run([sys.executable, str(SCRIPT), "attach"], env=env, input=json.dumps({"session_id": "s1"}),
                                    capture_output=True, text=True, timeout=20)
            self.assertIn("base-approved", json.loads(attach.stdout)["hookSpecificOutput"]["additionalContext"])
            parked = wake()
            parked.stdin.write(json.dumps({"session_id": "s1"}))
            parked.stdin.close()
            time.sleep(3)
            self.assertIsNone(parked.poll(), "no fresh event and no second correction: the hook parks")
            newer = wake()
            newer.stdin.write(json.dumps({"session_id": "s1"}))
            newer.stdin.close()
            self.assertEqual(parked.wait(timeout=10), 0, "a newer Stop claim supersedes the parked watch")
            pending = json.loads(request(5, "tools/call", {"name": "mate_status", "arguments": {}})["content"][0]["text"])["events"]
            events = [e["id"] for e in pending]
            self.assertTrue(events)
            request(4, "tools/call", {"name": "mate_ack", "arguments": {"events": events, "note": "Dispatch deferred in test"}})
            newer.kill()
            newer.wait()
            for proc in (first, again, parked, newer):
                proc.stdout.close()
                proc.stderr.close()
        finally:
            server.stdin.close()
            server.wait(timeout=10)
            server.stdout.close()


if __name__ == "__main__":
    unittest.main()
