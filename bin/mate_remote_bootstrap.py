"""Human-requested secondmate start/recovery. Never stops a server or live agent."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import mate as mate
import mate_remote as inbox
import mate_remote_events as events

METHODS = {"secondmate_start", "secondmate_recover"}


def context(config, config_path):
    path = Path(config_path).resolve()
    if path.name != "remote.json":
        raise ValueError("Bootstrap requires the bound home's remote.json")
    home = path.parent
    profile = mate.worker_profile(config.get("supervisor", {}))
    tools = {name: shutil.which(name) for name in ("pi", "git", "treehouse", "herdr")}
    if not all(tools.values()):
        raise ValueError("Missing remote tools: " + ", ".join(k for k, v in tools.items() if not v))
    mate.mate_config()  # Validate trusted installed config before external creation.
    root = Path(__file__).resolve().parents[1]
    if not (root / ".pi/extensions/mate-supervisor.ts").is_file():
        raise ValueError("Remote Mate supervisor extension is not installed")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MATE_", "HERDR_"))}
    # A fixture or operator may select a Herdr config, but never a caller's pane.
    if os.environ.get("HERDR_CONFIG_PATH"):
        env["HERDR_CONFIG_PATH"] = os.environ["HERDR_CONFIG_PATH"]
    return home, root, profile, tools, env


def confirmation(config, record):
    return hashlib.sha256(inbox.encoded(dict(config=config, endpoint=record)).encode()).hexdigest()


def inspect(db, config, path):
    record = events.get(db, "secondmate")
    try:
        home, root, profile, tools, _ = context(config, path)
        readiness = dict(ready=True, home_path=str(home), code_root=str(root), profile=profile, tools=tools,
                         credentials="not probed; use remote Pi authentication")
    except Exception as exc:
        readiness = dict(ready=False, error=str(exc))
    return dict(**readiness, endpoint=record, heartbeat=events.get(db, "heartbeat"),
                confirmation=confirmation(config, record))


def command(args, env, timeout=20):
    result = subprocess.run(args, capture_output=True, text=True, env=env, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Remote bootstrap command failed: {(result.stderr or result.stdout)[-2000:]}")
    return json.loads(result.stdout)


def live(db, record, home):
    if not record or not record.get("pane"):
        return False
    heartbeat = events.get(db, "heartbeat", {})
    if (heartbeat.get("pane") != record["pane"] or heartbeat.get("socket") != record["socket"] or
            heartbeat.get("session") != record["session"] or heartbeat.get("at", 0) < record.get("submitted_at", 0) or time.time() - heartbeat.get("at", 0) > 15):
        return False
    try:
        guard = mate.lock(home / "supervisor.lock")
    except BlockingIOError:
        pane = mate.check_endpoint(record, record["pane"])
        return pane.get("terminal_id") == record["terminal_id"]
    guard.close()
    return False


def launch(db, config, path, request):
    body = request["body"]
    if (set(body) != {"method", "params", "confirmation"} or body["method"] not in METHODS or
            body["params"] != {}):
        raise ValueError("Invalid secondmate launch request")
    record = events.get(db, "secondmate")
    if body["confirmation"] != confirmation(config, record):
        raise ValueError("Secondmate endpoint/config changed; confirm again")
    home, root, profile, tools, env = context(config, path)
    if record and live(db, record, home):
        return dict(state="running", endpoint=record, resolved_request=record.get("request"))  # Observe, never type into live Pi.
    # Missing/stale heartbeat is not proof of death. Kernel ownership and shell
    # inspection below must both agree before any new command can be submitted.
    guard = mate.lock(home / "supervisor.lock")
    try:
        if record and body["method"] != "secondmate_recover":
            raise ValueError("Saved secondmate endpoint exists; inspect and use explicit recovery")
        if not record and body["method"] == "secondmate_recover":
            raise ValueError("No saved secondmate endpoint to recover")
        session = "mate-remote-" + config["home"].replace("-", "")
        status = command([tools["herdr"], "--session", session, "status", "--json"], env)["server"]
        if status.get("session") != session or not status.get("socket"):
            raise ValueError("Herdr session identity unavailable")
        if not status.get("running"):
            if status.get("status") != "not_running":
                raise ValueError("Herdr server state is uncertain")
            if sys.platform == "darwin":
                raise ValueError("Start this named Herdr server in the Mac's GUI login session first; SSH cannot grant keychain access")
            if record:
                raise ValueError("Saved server is unavailable; restore it explicitly before endpoint recovery")
            with db:
                events.put(db, "secondmate", dict(phase="starting-server", session=session, request=request["id"]))
            with (home / "herdr-server.log").open("ab") as log:
                subprocess.Popen([tools["herdr"], "--session", session, "server"], env=env,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = command([tools["herdr"], "--session", session, "status", "--json"], env)["server"]
                if status.get("running"):
                    break
                time.sleep(.1)
        if (not status.get("running") or status.get("compatible") is False or
                status.get("session") != session or status.get("endpoint_compatible") is False):
            raise ValueError("Named Herdr server is not ready/compatible; never replace it automatically")
        if record:
            if record.get("session") != session or record.get("socket") != status["socket"] or not record.get("pane"):
                raise ValueError("Saved endpoint identity incomplete; inspect uncertain creation manually")
            heartbeat = events.get(db, "heartbeat", {})
            if record.get("phase") != "preflight" and (
                    heartbeat.get("pane") != record["pane"] or heartbeat.get("socket") != record["socket"] or
                    heartbeat.get("session") != record["session"] or heartbeat.get("at", 0) < record.get("submitted_at", float('inf'))):
                raise ValueError("Previous launch was never observed running; uncertain submissions cannot be replayed")
            mate.ready_pane(record)
            pane = mate.check_endpoint(record, record["pane"])
            if pane.get("cwd") != str(root):
                raise ValueError("Secondmate shell cwd changed; restore the installed root before recovery")
            # Refuse detached agents still referencing the private session file.
            session_file = str(home / "secondmate.jsonl")
            if session_file in mate.run(["ps", "-axo", "args="]):
                raise ValueError("Possible orphan secondmate session remains")
        else:
            with db:
                events.put(db, "secondmate", dict(phase="creating-pane", session=session, socket=status["socket"], request=request["id"]))
            created = command([tools["herdr"], "--session", session, "workspace", "create", "--cwd", str(root),
                               "--label", "Mate secondmate", "--no-focus"], env)["result"]
            pane = created["root_pane"]
            record = dict(session=session, socket=status["socket"], workspace=created["workspace"]["workspace_id"],
                          tab=created["tab"]["tab_id"], pane=pane["pane_id"], terminal_id=pane["terminal_id"],
                          endpoint_receipt=created, request=request["id"])
            with db:
                events.put(db, "secondmate", dict(record, phase="preflight"))
            mate.wait_ready_pane(record, 5)
        argv = ["env", "MATE_MODE=supervisor", f"MATE_HOME={home}", tools["pi"], "--approve", "--tui-mode", "regular",
                "--provider", profile["provider"], "--model", profile["model"], "--thinking", profile["effort"],
                "--session", str(home / "secondmate.jsonl"), "--no-prompt-templates"]
        previous_request = record.get("request")
        record = dict(record, phase="submitted", request=request["id"], profile=profile, submitted_at=time.time())
        with db:
            events.put(db, "secondmate", record)
    finally:
        guard.close()  # The extension's receiving control plane needs this lock.
    mate.herdr(record, "pane", "run", record["pane"], "cd -- " + shlex.quote(str(root)) + " && " + shlex.join(argv))
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if live(db, record, home):
            return dict(state="running", endpoint=record, resolved_request=previous_request)
        time.sleep(.2)
    raise RuntimeError("Secondmate launch unconfirmed; inspect endpoint/heartbeat, never blindly resubmit")


def consume(db, config, path, request):
    if inbox.status(db, request["id"])["state"] != "queued":
        return
    owned = inbox.claim(db, request["id"])
    before = events.get(db, "secondmate")
    try:
        result = launch(db, config, path, request)
        outcome = dict(ok=True, result=result)
    except Exception as exc:
        # Every external mutation is preceded by a changed journal record.
        disposition = "refused" if events.get(db, "secondmate") == before else "uncertain"
        outcome = dict(ok=False, error=str(exc)[:4000], **{disposition: True})
    inbox.finish(db, request["id"], owned["token"], outcome)
