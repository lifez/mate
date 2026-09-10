#!/usr/bin/env python3
"""Opt-in real Herdr + Treehouse test, fake Pi (no model calls).
Creates/owns a unique named Herdr server and a temporary repo/pool/home.
Run from Herdr: python3 tests/live-smoke.py
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if os.environ.get("HERDR_ENV") != "1":
    raise SystemExit("Run this smoke test inside Herdr; it only controls its own named test server")
for command in ("herdr", "treehouse", "git"):
    if not shutil.which(command):
        raise SystemExit(f"Missing {command}")
folder = Path(tempfile.mkdtemp(prefix="mate-live-")).resolve()
name = "mate-test-" + uuid.uuid4().hex[:10]
env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_") and not k.startswith("MATE_")}
config = folder / "herdr/config.toml"
config.parent.mkdir(); config.write_text("")
env.update(HERDR_CONFIG_PATH=str(config), MATE_HOME=str(folder / "home"))
repo = folder / "repo"; repo.mkdir()

def command(args, **kwargs):
    return subprocess.run(args, env=env, text=True, capture_output=True, timeout=30, check=True, **kwargs).stdout.strip()

def git(*args):
    return command(["git", "-C", str(repo), *args])

def herdr(*args):
    return json.loads(command(["herdr", "--session", name, *args]))

git("init", "-b", "main")
(repo / "treehouse.toml").write_text('max_trees = 2\nroot = "./"\n')
git("add", "treehouse.toml")
git("-c", "user.name=Mate Test", "-c", "user.email=mate@test.invalid", "commit", "-m", "base")
fakebin = folder / "bin"; fakebin.mkdir()
fakepi = fakebin / "pi"
fakepi.write_text("#!/usr/bin/env python3\nimport json,time\ntime.sleep(1)\nprint(json.dumps({'type':'message_end','message':{'role':'assistant','stopReason':'stop','content':[{'type':'text','text':'Fixture worker report: local test only.'}]}}))\n")
fakepi.chmod(0o755)
env["PATH"] = str(fakebin) + os.pathsep + env["PATH"]
log = (folder / "server.log").open("w")
server = subprocess.Popen(["herdr", "--session", name, "server"], env=env, stdout=log, stderr=log)
db = None
ok = False
try:
    status = None
    for _ in range(100):
        if server.poll() is not None:
            raise RuntimeError("Test server exited; inspect " + str(folder / "server.log"))
        try:
            status = herdr("status", "--json")
            if status.get("server", {}).get("running"):
                break
        except (subprocess.SubprocessError, ValueError):
            pass
        time.sleep(.1)
    else:
        raise RuntimeError("Test Herdr server did not start")
    created = herdr("workspace", "create", "--cwd", str(repo), "--label", "mate-test-owner", "--no-focus")["result"]
    env.update(HERDR_ENV="1", HERDR_SESSION=name, HERDR_SOCKET_PATH=status["server"]["socket"],
               HERDR_WORKSPACE_ID=created["workspace"]["workspace_id"], HERDR_PANE_ID=created["root_pane"]["pane_id"])
    subscribed = subprocess.run([sys.executable, str(ROOT / "bin/herdr-eventwait.py"),
                                 env["HERDR_SOCKET_PATH"], "0.5", env["HERDR_PANE_ID"]],
                                capture_output=True, text=True, timeout=10)
    assert subscribed.returncode == 0 and "@subscribed" in subscribed.stdout, (subscribed.returncode, subscribed.stdout, subscribed.stderr)
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location("mate", ROOT / "bin/mate.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    db = m.connect()
    task = m.propose(db, dict(id="smoke", repo=str(repo), base="main", brief="Read-only fixture test; no network or code changes."))
    m.approve(db, dict(id="smoke", sha=task["sha"]))  # Test-only synthetic approval.
    git("-c", "user.name=Mate Test", "-c", "user.email=mate@test.invalid", "commit", "--allow-empty", "-m", "base moved")
    original = git("rev-parse", "HEAD")
    task = m.dispatch(db, dict(id="smoke", provider="openai-codex", model="fake-no-model-call"))
    for _ in range(150):
        task = m.load(db, "smoke")
        if task["state"] in ("review", "failed", "attention"):
            break
        time.sleep(.2)
    assert task["state"] == "review", task
    assert m.git(task["worktree"], "rev-parse", "HEAD") == task["sha"]
    assert git("rev-parse", "HEAD") == original
    assert "Fixture worker report" in m.snapshot(db, {"id": "smoke"})["report"]["text"]
    assert m.snapshot(db, {})["events"]
    assert m.dispatch(db, dict(id="smoke", provider="openai-codex", model="fake"))["pane"] == task["pane"]
    ok = True
    print("PASS: real Herdr subscription/pane + real Treehouse lease + pinned base + fake Pi worker + durable report + duplicate dispatch guard")
finally:
    if db:
        db.close()
    try:
        command(["herdr", "--session", name, "server", "stop"])  # This test server only.
    except Exception:
        server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill(); server.wait()
    log.close()
    if ok:
        shutil.rmtree(folder)  # Disposable fixture/pool only; never production cleanup.
    else:
        print("Retained failing fixture for inspection:", folder, file=sys.stderr)
