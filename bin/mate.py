#!/usr/bin/env python3
"""Mate's local control plane. stdlib only; macOS/Linux. No Firstmate runtime imports."""
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(os.environ.get("MATE_HOME", ROOT / "data")).expanduser().resolve()


def run(args, cwd=None, timeout=30):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{args[0]}: {(result.stderr or result.stdout)[-4000:]}")
    return result.stdout.strip()


def git(repo, *args):
    return run(["git", "-C", str(repo), *args])


def text(value, name, limit=20000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        raise ValueError(f"Invalid {name}")
    return value


def task_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", value):
        raise ValueError("Task ID must match [a-z][a-z0-9-]{0,47}")
    return value


def connect():
    HOME.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(HOME / "mate.sqlite3", timeout=10)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT, attempt INTEGER,
        kind TEXT, note TEXT, ack TEXT, UNIQUE(task, attempt, kind))""")
    db.commit()
    return db


def load(db, ident):
    row = db.execute("SELECT data FROM tasks WHERE id=?", (task_id(ident),)).fetchone()
    if not row:
        raise ValueError("Unknown task")
    return json.loads(row[0])


def save(db, task):
    task["updated"] = time.time()
    db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)", (task["id"], json.dumps(task)))


def event(db, task, kind, note):
    if db.execute("SELECT 1 FROM events WHERE task=? AND attempt=? AND kind=?", (task["id"], task["attempt"], kind)).fetchone():
        return
    db.execute("INSERT OR IGNORE INTO events(task,attempt,kind,note) VALUES (?,?,?,?)",
               (task["id"], task["attempt"], kind, note))


def tasks(db):
    return [json.loads(row[0]) for row in db.execute("SELECT data FROM tasks ORDER BY id")]


def lock(path, blocking=False):
    handle = open(path, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BaseException:
        handle.close()
        raise
    return handle


def worker_alive(task):
    try:
        handle = lock(HOME / task["id"] / "run.lock")
    except BlockingIOError:
        return True
    handle.close()
    return False


def herdr(task, *args):
    # Never fall back to a focused/default session or another socket.
    env = os.environ.copy()
    env["HERDR_SOCKET_PATH"] = task["socket"]
    cmd = ["herdr", "--session", task["session"], *args]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=20)
    if result.returncode:
        raise RuntimeError(f"Herdr: {(result.stderr or result.stdout)[-3000:]}")
    if args[:2] == ("pane", "run"):
        return {"submitted": True}  # CLI returns text, not a JSON identity receipt.
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or not isinstance(data.get("result"), dict):
        raise RuntimeError("Unexpected Herdr response; refusing ambiguous operation")
    return data["result"]


def check_endpoint(task, pane):
    result = herdr(task, "pane", "get", pane)
    info = result.get("pane", {})
    if info.get("pane_id") != pane or info.get("workspace_id") != task["workspace"] or (task.get("tab") and info.get("tab_id") != task["tab"]):
        raise RuntimeError("Herdr pane/tab/workspace identity mismatch")
    return info


def check_lease(task):
    lease = task["lease"]
    if not isinstance(lease, dict) or lease.get("lease_holder") != task["holder"] or not lease.get("lease_id"):
        raise ValueError("Missing/mismatched Treehouse lease receipt")
    rows = json.loads(run(["treehouse", "status", "--json"], cwd=task["repo"]))
    matches = [r for r in rows if isinstance(r, dict) and str(Path(r.get("path", "")).resolve()) == task["worktree"]]
    if len(matches) != 1 or any(matches[0].get(k) != lease.get(k) for k in ("lease_id", "lease_holder")) or matches[0].get("status") != "leased":
        raise ValueError("Treehouse no longer confirms this task's exact lease")


def propose(db, p):
    ident = task_id(p["id"])
    repo = str(Path(text(p["repo"], "repo", 4096)).expanduser().resolve())
    base = text(p["base"], "base", 256)
    if base.startswith("-") or any(c.isspace() for c in base):
        raise ValueError("Base must be an explicit Git ref, not options")
    brief = text(p["brief"], "brief")
    if git(repo, "rev-parse", "--show-toplevel") != repo:
        raise ValueError("Use the repository root")
    old = db.execute("SELECT data FROM tasks WHERE id=?", (ident,)).fetchone()
    if old:
        existing = json.loads(old[0])
        if (existing["repo"], existing["base"], existing["brief"]) != (repo, base, brief):
            raise ValueError("Task ID already exists with different scope; choose a new ID")
        return existing  # Retrying a tool call does not change its approved SHA.
    sha = git(repo, "rev-parse", "--verify", "--end-of-options", base + "^{commit}")
    task = dict(id=ident, repo=repo, base=base, sha=sha, brief=brief,
                state="awaiting-base", attempt=0, branch=f"mate/{ident}-{uuid.uuid4().hex[:8]}")
    with db:
        save(db, task)
    return task


def approve(db, p):
    task = load(db, p["id"])
    if task["state"] != "awaiting-base" or p["sha"] != task["sha"]:
        raise ValueError("Approval no longer matches the pending task")
    task.update(state="approved", approved_at=time.time())
    with db:
        save(db, task)
    return task


def check_capacity(db):
    fleet = tasks(db)
    if any(t["state"] == "attention" for t in fleet):
        raise ValueError("An uncertain task needs inspection before starting more workers")
    if sum(t["state"] in ("acquiring", "launching", "running") for t in fleet) >= 2:
        raise ValueError("Two workers are already active")


def worker_profile(p, previous=None):
    previous = previous or {}
    profile = {key: text(p.get(key, previous.get(key)), key, 256) for key in ("provider", "model")}
    for value in profile.values():
        if value.startswith("-") or any(c.isspace() for c in value):
            raise ValueError("Provider/model must be identifiers, not CLI options")
    effort = p.get("effort", previous.get("effort", "off"))
    if effort not in ("off", "minimal", "low", "medium", "high", "xhigh", "max"):
        raise ValueError("Invalid effort")
    return dict(profile, effort=effort)


def dispatch(db, p):
    task = load(db, p["id"])
    if task["state"] != "approved":
        return task  # Never re-acquire after an interrupted/ambiguous operation.
    profile = worker_profile(p)
    check_capacity(db)
    if os.environ.get("HERDR_ENV") != "1":
        raise ValueError("Start Mate inside Herdr")
    session = os.environ.get("HERDR_SESSION") or "default"
    socket_path = os.environ.get("HERDR_SOCKET_PATH", "")
    parent = os.environ.get("HERDR_PANE_ID", "")
    workspace = os.environ.get("HERDR_WORKSPACE_ID", "")
    if not socket_path or not parent or not workspace:
        raise ValueError("Missing exact Herdr caller identity")
    task.update(session=session, socket=socket_path, workspace=workspace)
    check_endpoint(task, parent)  # Before acquiring anything.
    pi_binary = shutil.which("pi")
    if not pi_binary:
        raise ValueError("pi is not on PATH")
    task.update(pi_binary=pi_binary, **profile,
                state="acquiring", attempt=1, holder=f"mate:{uuid.uuid4().hex}")
    (HOME / task["id"]).mkdir(mode=0o700)
    with db:
        save(db, task)  # Journal before non-transactional external acquire.
    try:
        lease = json.loads(run(["treehouse", "get", "--lease", "--json", "--lease-holder", task["holder"]],
                               cwd=task["repo"], timeout=120))
        task["lease"] = lease  # Keep exact receipt even if its schema is unexpected.
        with db:
            save(db, task)
        wt = str(Path(lease["path"]).resolve())
        task["worktree"] = wt
        check_lease(task)
        if wt == task["repo"] or git(wt, "rev-parse", "--show-toplevel") != wt:
            raise ValueError("Treehouse did not yield an isolated worktree")
        if git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir") != git(task["repo"], "rev-parse", "--path-format=absolute", "--git-common-dir"):
            raise ValueError("Treehouse worktree belongs to a different repository")
        if git(wt, "status", "--porcelain"):
            raise ValueError("Leased worktree is dirty; preserved without reset")
        # Preserve the pooled branch and all its commits. Never reset --hard.
        git(wt, "-c", "core.hooksPath=/dev/null", "switch", "-c", task["branch"], task["sha"])
        if git(wt, "rev-parse", "HEAD") != task["sha"]:
            raise ValueError("Worktree HEAD differs from approved commit")
        task["state"] = "launching"
        with db:
            save(db, task)
        created = herdr(task, "tab", "create", "--workspace", workspace, "--cwd", wt,
                        "--label", "mate-" + task["id"], "--no-focus")
        task["endpoint_receipt"] = created
        with db:
            save(db, task)
        task["pane"] = created["root_pane"]["pane_id"]
        task["tab"] = created["tab"]["tab_id"]
        check_endpoint(task, task["pane"])
        with db:
            save(db, task)
        launch_worker(task)
    except Exception as exc:
        # Do not roll back external resources or auto-retry an uncertain launch.
        with db:
            latest = load(db, task["id"])
            if latest["state"] in ("acquiring", "launching"):
                task.update(state="attention", error=str(exc))
                save(db, task)
                event(db, task, "launch-uncertain", str(exc))
        raise
    return load(db, task["id"])


def launch_worker(task):
    command = shlex.join(["env", f"MATE_HOME={HOME}", sys.executable, str(ROOT / "bin/mate.py"),
                          "worker", task["id"], str(task["attempt"])])
    herdr(task, "pane", "run", task["pane"], command)


def resume(db, p):
    task = load(db, p["id"])
    if task["state"] not in ("review", "failed") or worker_alive(task):
        raise ValueError("Only a stopped review/failed task can continue; uncertain launches require inspection")
    profile = worker_profile(p, task)
    check_capacity(db)
    check_endpoint(task, task["pane"])
    check_lease(task)
    task.update(attempt=task["attempt"] + 1, state="launching", followup=text(p["message"], "message"), **profile)
    with db:
        save(db, task)
    try:
        launch_worker(task)
    except Exception as exc:
        with db:
            event(db, task, "launch-uncertain", str(exc))
        raise
    return task


def snapshot(db, p):
    all_tasks = tasks(db)
    start = int(p.get("task_offset", 0))
    if start < 0:
        raise ValueError("Invalid task offset")
    result = {"total_tasks": len(all_tasks), "tasks": sorted(all_tasks, key=lambda t: t["updated"], reverse=True)[start:start + 50], "events": [dict(zip(("id", "task", "attempt", "kind", "note"), row))
              for row in db.execute("SELECT id,task,attempt,kind,note FROM events WHERE ack IS NULL ORDER BY id LIMIT 50")]}
    if p.get("id"):
        task = load(db, p["id"])
        attempt = int(p.get("attempt", task["attempt"]))
        if attempt < 0 or attempt > task["attempt"] or ("attempt" in p and attempt == 0):
            raise ValueError("Invalid attempt")
        report = HOME / task["id"] / f"report-{attempt}.txt"
        offset = int(p.get("offset", 0))
        if offset < 0:
            raise ValueError("Invalid offset")
        if report.exists():
            with report.open() as f:
                f.seek(offset)
                content = f.read(12000)
                result["report"] = dict(path=str(report), text=content, next_offset=f.tell(), more=bool(f.read(1)))
    # Do not send every brief/receipt repeatedly into model context.
    if not p.get("id"):
        result["tasks"] = [{k: t[k] for k in ("id", "state", "base", "sha", "attempt", "provider", "model", "effort", "worktree", "pane", "error") if k in t} for t in result["tasks"]]
    else:
        result["tasks"] = [load(db, p["id"])]
    return result


def acknowledge(db, p):
    note = text(p["note"], "handling note", 2000)
    if not isinstance(p["events"], list) or not p["events"] or len(p["events"]) > 50:
        raise ValueError("Specify 1–50 event IDs")
    with db:
        for ident in p["events"]:
            if type(ident) is not int or not db.execute("SELECT 1 FROM events WHERE id=?", (ident,)).fetchone():
                raise ValueError("Unknown event ID")
            db.execute("UPDATE events SET ack=? WHERE id=? AND ack IS NULL", (note, ident))
    return {"acknowledged": p["events"]}


def reconcile(db):
    for task in tasks(db):
        if task["state"] not in ("acquiring", "launching", "running"):
            continue
        if time.time() - task["updated"] < 60:
            continue
        if worker_alive(task):
            log = HOME / task["id"] / f"events-{task['attempt']}.jsonl"
            observed = log.stat().st_mtime if log.exists() else task["updated"]
            if time.time() - observed > max(30, int(os.environ.get("MATE_STALE_SECONDS", "900"))):
                with db:
                    event(db, task, "stalled", "No new Pi output within the stale interval. Worker still owns its lock; inspect before intervening. This is not proof of failure.")
            continue
        with db:
            task = load(db, task["id"])
            if task["state"] in ("acquiring", "launching", "running"):
                task.update(state="attention",
                            error="No worker lock after 60s. Resources retained; no automatic relaunch.")
                save(db, task)
                event(db, task, "worker-missing", task["error"])


class NativeEvents:
    """Supplement durable-result polling with Herdr's push stream (R01)."""
    def __init__(self, db, selector):
        self.db, self.selector = db, selector
        self.children = {}
        self.retries = {}

    def retire(self, ident):
        child, _ = self.children.pop(ident)
        self.selector.unregister(child.stdout)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        child.stdout.close()

    def sync(self):
        active = {t["id"]: t for t in tasks(self.db) if t["state"] in ("running", "launching") and t.get("pane")}
        for ident in list(self.children):
            if ident not in active:
                self.retire(ident)
        for ident, task in active.items():
            failures, retry_at = self.retries.get(ident, (0, 0))
            if ident in self.children or failures >= 5 or time.monotonic() < retry_at:
                continue
            child = subprocess.Popen([sys.executable, str(ROOT / "bin/herdr-eventwait.py"),
                                      task["socket"], "30", task["pane"]],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.children[ident] = (child, b"")
            self.selector.register(child.stdout, selectors.EVENT_READ, ident)

    def read(self, ident):
        child, buf = self.children[ident]
        chunk = os.read(child.stdout.fileno(), 65536)
        task = load(self.db, ident)
        if not chunk:
            code = child.wait(timeout=2)
            self.retire(ident)
            failures = 0 if code == 0 else self.retries.get(ident, (0, 0))[0] + 1
            self.retries[ident] = (failures, time.monotonic() + min(8, 2 ** failures))
            if failures >= 5:
                with self.db:
                    event(self.db, task, "transport-failed", "Herdr push unavailable after 5 retries; durable-result polling remains active. Inspect Herdr socket/protocol.")
            return
        buf += chunk
        if len(buf) > 1024 * 1024:
            child.terminate()
            buf = b""
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            fields = line.decode("utf-8", "replace").split("\t")
            if len(fields) == 4 and fields[0] == task.get("pane") and fields[1] == task["workspace"] and fields[2] == "blocked":
                with self.db:
                    event(self.db, task, "blocked", "Herdr reports a blocked worker. Inspect its report/log/pane; do not assume completion or blindly send approvals.")
        self.children[ident] = (child, buf)

    def close(self):
        for ident in list(self.children):
            self.retire(ident)


def serve():
    db = connect()
    owner = lock(HOME / "supervisor.lock")  # Kernel releases it on crash; no stale PID stealing.
    methods = dict(propose=propose, approve=approve, dispatch=dispatch, resume=resume,
                   status=snapshot, ack=acknowledge)
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin, selectors.EVENT_READ, None)
    native = NativeEvents(db, selector)
    print(json.dumps({"ready": True}), flush=True)
    def terminate(_sig, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    incoming = b""
    try:
        while True:
            for key, _ in selector.select(timeout=2):
                if key.data is not None:
                    native.read(key.data)
                    continue
                chunk = os.read(sys.stdin.fileno(), 65536)
                if not chunk:
                    return  # Pi died: workers remain recoverable.
                incoming += chunk
                if len(incoming) > 1024 * 1024:
                    raise ValueError("Control-plane input exceeded 1 MiB")
                while b"\n" in incoming:
                    line, incoming = incoming.split(b"\n", 1)
                    req = {}
                    try:
                        req = json.loads(line)
                        if not isinstance(req, dict):
                            raise ValueError("Expected a request object")
                        result = methods[req["method"]](db, req.get("params", {}))
                        print(json.dumps({"id": req["id"], "result": result}), flush=True)
                    except Exception as exc:
                        print(json.dumps({"id": req.get("id") if isinstance(req, dict) else None, "error": str(exc)}), flush=True)
            reconcile(db)
            native.sync()
    finally:
        native.close()
        selector.close()
        owner.close()
        db.close()


def worker(ident, attempt):
    db = connect()
    task = load(db, ident)
    guard = lock(HOME / ident / "run.lock")
    with db:
        task = load(db, ident)
        if task["attempt"] != attempt or task["state"] != "launching":
            raise ValueError("Stale or duplicate worker launch")
        if os.environ.get("HERDR_PANE_ID") != task["pane"] or os.environ.get("HERDR_SOCKET_PATH") != task["socket"]:
            raise ValueError("Worker launched in the wrong Herdr endpoint")
        check_lease(task)
        if str(Path.cwd().resolve()) != task["worktree"]:
            raise ValueError("Worker cwd differs from its leased worktree")
        task["state"] = "running"
        save(db, task)
    folder = HOME / ident
    session = folder / "session.jsonl"
    prompt = task.get("followup", task["brief"])
    policy = (ROOT / "WORKER.md").read_text()
    args = [task["pi_binary"], "-p", "--mode", "json", "--no-extensions", "--no-skills", "--no-prompt-templates",
            "--no-approve", "--provider", task["provider"], "--model", task["model"],
            "--session", str(session), "--append-system-prompt", policy]
    if "effort" in task:  # Legacy in-flight tasks keep their existing CLI/session defaults.
        args += ["--thinking", worker_profile(task)["effort"]]
    args += ["--", prompt]
    child = None
    last = {}
    error = ""
    try:
        with (folder / f"events-{attempt}.jsonl").open("w") as log, (folder / f"stderr-{attempt}.log").open("w") as err:
            child = subprocess.Popen(args, cwd=task["worktree"], stdout=subprocess.PIPE,
                                     stderr=err, text=True, start_new_session=True)
            def stop(_sig, _frame):
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                raise InterruptedError("Worker interrupted")
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            for line in iter(lambda: child.stdout.readline(4 * 1024 * 1024 + 1), ""):
                if len(line) > 4 * 1024 * 1024:
                    raise ValueError("Pi JSON event exceeded 4 MiB; see worker log")
                log.write(line)
                log.flush()
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("type") == "message_end" and item.get("message", {}).get("role") == "assistant":
                    last = item["message"]
                delta = item.get("assistantMessageEvent", {})
                if delta.get("type") == "text_delta":
                    print(delta.get("delta", ""), end="", flush=True)
            code = child.wait()
            if code or last.get("stopReason") != "stop":
                error = f"Pi exit={code}, stopReason={last.get('stopReason')}: {last.get('errorMessage', '')}"
    except Exception as exc:
        error = str(exc)
    finally:
        if child and child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        report = "\n".join(part.get("text", "") for part in last.get("content", []) if part.get("type") == "text")
        if not report.strip() and not error:
            error = "Pi exited without a final text report"
        (folder / f"report-{attempt}.txt").write_text((error + "\n\n" if error else "") + report)
        with db:
            current = load(db, ident)
            if current["attempt"] == attempt:
                current.update(state="failed" if error else "review", error=error)
                save(db, current)
                event(db, current, "failed" if error else "report", error or "Worker report available; not verified completion.")
        guard.close()
        db.close()
    print("\n[Mate: report saved; supervisor will wake automatically.]", flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        if sys.argv[1:] == ["serve"]:
            serve()
        elif len(sys.argv) == 4 and sys.argv[1] == "worker":
            worker(task_id(sys.argv[2]), int(sys.argv[3]))
        elif sys.argv[1:] == ["status"]:
            with connect() as db:
                print(json.dumps(snapshot(db, {}), indent=2))
        else:
            raise ValueError("Usage: mate.py serve | worker ID ATTEMPT | status")
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
