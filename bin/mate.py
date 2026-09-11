#!/usr/bin/env python3
"""Mate's local control plane. stdlib only; macOS/Linux. No Firstmate runtime imports."""
from contextlib import closing
import fcntl
import json
import math
import os
from pathlib import Path
import re
import pwd
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
CONFIG = ROOT / "mate.config.json"


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
    return matches[0]


def project_config(repo):
    """Trusted Mate config only; never discover executable config in a worker repo."""
    config = json.loads(CONFIG.read_text())
    if not isinstance(config, dict) or set(config) - {"worker", "projects"}:
        raise ValueError("Invalid Mate config")
    projects = config.get("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("Invalid projects config")
    selected, seen = {}, set()
    for name, project in projects.items():
        text(name, "project name", 256)
        if not isinstance(project, dict) or set(project) - {"repo", "base_branch", "startup"}:
            raise ValueError("Invalid project config")
        path = Path(text(project.get("repo"), "project repo", 4096)).expanduser()
        if not path.is_absolute():
            raise ValueError("Project repo must be an absolute path (or ~/path)")
        path = str(path.resolve())
        if path in seen:
            raise ValueError("Duplicate project repo")
        seen.add(path)
        if "base_branch" in project:
            branch = text(project["base_branch"], "base_branch", 256)
            if branch.startswith(("-", "refs/")) or any(c.isspace() for c in branch):
                raise ValueError("base_branch must be a short local or remote-tracking branch name")
            git(ROOT, "check-ref-format", "refs/heads/" + branch)
        if "startup" in project:
            startup = project["startup"]
            if not isinstance(startup, dict) or set(startup) - {"command", "timeout_seconds"}:
                raise ValueError("Invalid startup config")
            command = startup.get("command")
            if not isinstance(command, list) or not command or len(command) > 128:
                raise ValueError("Startup command must be a nonempty argv list")
            for arg in command:
                text(arg, "startup argument", 4096)
            timeout = startup.get("timeout_seconds", 30)
            if type(timeout) is not int or not 1 <= timeout <= 120:
                raise ValueError("Startup timeout_seconds must be an integer from 1 to 120")
        if path == repo:
            selected = dict(project, repo=path, name=name)
    return selected


def branch_ref(repo, branch):
    refs = git(repo, "for-each-ref", "--format=%(refname)\t%(symref)", "refs/heads", "refs/remotes")
    matches = [line.split("\t")[0] for line in refs.splitlines()
               if line.split("\t")[0] in ("refs/heads/" + branch, "refs/remotes/" + branch)
               and not line.partition("\t")[2]]
    if len(matches) != 1:
        raise ValueError("Configured base_branch must identify exactly one existing branch, not a tag/SHA/symbolic ref")
    return matches[0]


def startup_worktree(db, task):
    startup = task.get("startup")
    if not startup:
        return
    task.update(startup_state="running", startup_started_at=time.time())
    with db:
        save(db, task)  # Crash here is uncertain, never permission to run it again.
    child = None
    try:
        log_path = HOME / task["id"] / "startup.log"
        with os.fdopen(os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as log:
            child = subprocess.Popen(startup["command"], cwd=task["worktree"],
                                     env=dict(os.environ, MATE_REPO=task["repo"],
                                              MATE_WORKTREE=task["worktree"], MATE_TASK_ID=task["id"]),
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            task["startup_pid"] = child.pid
            with db:
                save(db, task)
            code = child.wait(timeout=startup.get("timeout_seconds", 30))
            task["startup_exit_code"] = code
            if code:
                raise RuntimeError("Startup exited unsuccessfully")
            try:
                os.killpg(child.pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError("Startup left background processes")
        task.update(startup_state="succeeded", startup_finished_at=time.time())
        with db:
            save(db, task)
    except BaseException as exc:
        # Kill only our new process group. Detached descendants cannot be proven gone:
        # keep attention and forbid continuation/retry even after successful cleanup.
        if child:
            try:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        task.update(startup_state="uncertain" if not isinstance(exc, Exception) else "failed",
                    startup_finished_at=time.time())
        with db:
            save(db, task)
        if not isinstance(exc, Exception):
            raise
        raise RuntimeError("Startup failed or timed out; inspect private startup.log and processes. No automatic retry.") from None


def propose(db, p):
    ident = task_id(p["id"])
    repo = str(Path(text(p["repo"], "repo", 4096)).expanduser().resolve())
    brief = text(p["brief"], "brief")
    if git(repo, "rev-parse", "--show-toplevel") != repo:
        raise ValueError("Use the repository root")
    old = db.execute("SELECT data FROM tasks WHERE id=?", (ident,)).fetchone()
    if old:
        existing = json.loads(old[0])
        if (existing["repo"], existing["base"], existing["brief"]) != (repo, p.get("base", existing["base"]), brief):
            raise ValueError("Task ID already exists with different scope; choose a new ID")
        return existing  # Retrying a tool call does not change its approved SHA.
    project = project_config(repo)
    configured = project.get("base_branch")
    base = text(p.get("base", configured), "base (required without project base_branch)", 256)
    if base.startswith("-") or any(c.isspace() for c in base):
        raise ValueError("Base must be an explicit Git ref, not options")
    if configured and base != configured:
        raise ValueError(f"Project requires base_branch {configured}")
    ref = branch_ref(repo, configured) if configured else base
    sha = git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}")
    task = dict(id=ident, repo=repo, base=base, base_branch=configured, base_ref=ref, sha=sha, brief=brief,
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


def propose_scope(db, p):
    task = load(db, p["id"])
    if task["state"] not in ("review", "failed"):
        raise ValueError("Only a stopped review/failed task can extend scope")
    with lock(HOME / task["id"] / "run.lock"):
        addition = text(p["brief"], "additional scope")
        text(task["brief"] + "\n\nAdditional approved scope:\n" + addition, "combined scope")
        if task.get("pending_scope", {}).get("brief") == addition:
            return task
        task["pending_scope"] = dict(token=uuid.uuid4().hex, brief=addition,
                                     attempt=task["attempt"], proposed_at=time.time())
        with db:
            save(db, task)
    return task  # Proposal never changes approval, resources or worker profile.


def review_scope(db, p):
    """Human command only: accept or discard the exact displayed proposal."""
    task = load(db, p["id"])
    pending = task.get("pending_scope")
    if (task["state"] not in ("review", "failed") or not pending or
        p.get("token") != pending["token"] or type(p.get("attempt")) is not int or
        p["attempt"] != task["attempt"] or pending["attempt"] != task["attempt"] or
        p.get("sha") != task["sha"] or type(p.get("approve")) is not bool):
        raise ValueError("Scope confirmation no longer matches the pending task")
    with lock(HOME / task["id"] / "run.lock"):
        if p["approve"]:
            brief = text(task["brief"] + "\n\nAdditional approved scope:\n" + pending["brief"], "combined scope")
            task.setdefault("original_brief", task["brief"])
            task["brief"] = brief
            task.setdefault("scope_history", []).append(dict(pending, approved_at=time.time(),
                approved_by=pwd.getpwuid(os.getuid()).pw_name, approved_via="mate-approve",
                first_attempt=task["attempt"] + 1))
        del task["pending_scope"]
        with db:
            save(db, task)
            if p["approve"]:
                event(db, task, "scope-approved-" + pending["token"],
                      "Human approved additional scope. Inspect current brief; use mate_continue, not dispatch. No worker started.")
    return task


def check_capacity(db, inspected=None):
    fleet = tasks(db)
    if any(t["state"] == "attention" and t["id"] != inspected for t in fleet):
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
    profile = worker_profile({}, task) if task.get("recoveries") else worker_profile(p)
    project = project_config(task["repo"])
    configured = project.get("base_branch")
    if configured != task.get("base_branch") or (configured and branch_ref(task["repo"], configured) != task.get("base_ref")):
        raise ValueError("Project base_branch changed; propose a new task and approve its base")
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
    if not task.get("recoveries"):
        task.update(project=project.get("name"), startup=project.get("startup"))
    (HOME / task["id"]).mkdir(mode=0o700, exist_ok=bool(task.get("recoveries")))
    task.update(pi_binary=pi_binary, **profile, state="acquiring",
                attempt=task["attempt"] + 1, holder=f"mate:{uuid.uuid4().hex}")
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
        startup_worktree(db, task)
        if task.get("startup"):
            check_lease(task)
            if (git(wt, "rev-parse", "--show-toplevel") != wt or
                git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir") != git(task["repo"], "rev-parse", "--path-format=absolute", "--git-common-dir") or
                git(wt, "rev-parse", "HEAD") != task["sha"] or
                git(wt, "symbolic-ref", "HEAD") != "refs/heads/" + task["branch"]):
                raise ValueError("Startup changed worktree identity or approved HEAD/branch")
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


def recover_acquire(db, task):
    """Offline human recovery only, before any saved lease/checkout/startup/endpoint."""
    if (task["state"] != "attention" or not task.get("approved_at") or
        not task.get("holder") or task["attempt"] < 1 or
        any(key in task for key in ("lease", "worktree", "startup_state", "endpoint_receipt",
                                    "pane", "tab", "usage", "followup"))):
        raise ValueError("Recovery only supports acquisition failures before a saved lease; inspect later phases separately")
    folder = HOME / task["id"]
    if any(path.name != "run.lock" for path in folder.iterdir()):
        raise ValueError("Task has execution artifacts; refusing acquisition recovery")
    project = project_config(task["repo"])
    configured = project.get("base_branch")
    if configured != task.get("base_branch") or (configured and branch_ref(task["repo"], configured) != task.get("base_ref")):
        raise ValueError("Project base_branch changed; new proposal/approval required")
    if git(task["repo"], "rev-parse", "--verify", task["sha"] + "^{commit}") != task["sha"]:
        raise ValueError("Approved commit is unavailable")
    if git(task["repo"], "for-each-ref", "--format=%(refname)", "refs/heads/" + task["branch"]):
        raise ValueError("Task branch already exists; refusing to reset or reuse it")
    rows = json.loads(run(["treehouse", "status", "--json"], cwd=task["repo"]))
    if not isinstance(rows, list) or any(not isinstance(row, dict) or
            not isinstance(row.get("path"), str) or not Path(row["path"]).is_absolute() or
            not isinstance(row.get("status"), str) or not row["status"] or
            (row["status"] == "leased" and any(not isinstance(row.get(key), str) or not row[key]
                for key in ("lease_id", "lease_holder"))) for row in rows):
        raise ValueError("Uncertain Treehouse status; recovery refused")
    if any(row.get("lease_holder") == task["holder"] for row in rows):
        raise ValueError("Treehouse still records this holder; inspect/release manually")
    if load(db, task["id"]) != task:
        raise ValueError("Task changed during confirmation")
    previous = {key: value for key, value in task.items() if key != "recoveries"}
    task.setdefault("recoveries", []).append(dict(task=previous, at=time.time(),
        by=pwd.getpwuid(os.getuid()).pw_name, via="recover-acquire"))
    task.update(state="approved")
    task.pop("error", None)
    with db:
        save(db, task)
        event(db, task, "acquire-recovered", "Human confirmed external cleanup; original approval retained. Explicit dispatch may retry acquisition.")
    return task


def recover_acquire_cli(ident):
    # Not an RPC/model tool. Hold both locks across inspection, confirmation and commit.
    if not sys.stdin.isatty():
        raise ValueError("Recovery requires a human at an interactive terminal")
    with lock(HOME / "supervisor.lock"), closing(connect()) as db:
        task = load(db, ident)
        with lock(HOME / ident / "run.lock"):
            print(json.dumps(task, indent=2))
            print("Confirm you inspected Treehouse setup/worktrees, leases, Herdr and processes; no orphan work remains.")
            print("No cleanup or worker launch will occur. Saved profile/startup and approved base are retained.")
            expected = f"recover {ident} {task['attempt']} {task['sha']}"
            if input(f"Type exactly: {expected}\n> ") != expected:
                print("Cancelled; task unchanged")
                return
            recover_acquire(db, task)
            print("Recovered to approved. Restart the same Mate home; explicitly request dispatch when ready.")


def ready_pane(task):
    pane = check_endpoint(task, task["pane"])
    original = task.get("endpoint_receipt", {}).get("root_pane", {}).get("terminal_id")
    if not original or pane.get("terminal_id") != original:
        raise ValueError("Worker terminal identity changed; inspect the original pane")
    process = herdr(task, "pane", "process-info", "--pane", task["pane"]).get("process_info", {})
    foreground = process.get("foreground_processes", [])
    shell = process.get("shell_pid")
    if (process.get("pane_id") != task["pane"] or type(shell) is not int or shell <= 0 or
        len(foreground) != 1 or foreground[0].get("pid") != shell):
        raise ValueError("Worker pane is not an idle shell; return it to its shell, then request mate_continue. No keys sent.")
    # ponytail: OS snapshots cannot detect busy shell builtins or reserve the prompt;
    # keep the pane untouched during launch; use a shell handshake if Herdr adds one.
    # A shared TTY is not ownership: detached prompt helpers can retain it.
    # Shell children and its process group still block, including stopped jobs.
    # Recovery separately checks detached task/session and worktree processes.
    rows = {}
    for line in run(["ps", "-axo", "pid=,ppid=,pgid=,tty=,comm=,args="]).splitlines():
        pid, parent, group, tty, command, args = line.split(None, 5)
        rows[int(pid)] = dict(parent=int(parent), group=int(group), tty=tty, command=command, args=args)
    current = rows.get(shell)
    if (not current or Path(current["command"]).name.lstrip("-") not in ("sh", "bash", "zsh", "fish", "dash", "ksh") or
        current["tty"] in ("?", "??") or process.get("foreground_process_group_id") != current["group"]):
        raise ValueError("Cannot confirm the pane's shell process identity")
    if any(pid != shell and (row["parent"] == shell or row["group"] == current["group"])
           for pid, row in rows.items()):
        raise ValueError("Worker pane has background/stopped processes; inspect before continuing")
    return shell, rows


def inspect_missing_launch(db, task):
    """Only an unstarted continuation, never a crashed Pi or uncertain acquisition."""
    attempt = task["attempt"]
    if (task.get("error") != "No worker lock after 60s. Resources retained; no automatic relaunch." or
        task.get("missing_from", "launching") != "launching" or not task.get("approved_at") or
        task.get("startup_state") not in (None, "succeeded") or not task.get("followup") or
        not task.get("usage") or str(attempt) in task["usage"] or
        not db.execute("SELECT 1 FROM events WHERE task=? AND attempt=? AND kind='worker-missing'",
                       (task["id"], attempt)).fetchone()):
        raise ValueError("Only an unstarted continuation can recover; uncertain launches require inspection")
    folder = HOME / task["id"]
    if any((folder / name).exists() or (folder / name).is_symlink() for name in
           (f"events-{attempt}.jsonl", f"stderr-{attempt}.log", f"report-{attempt}.txt")):
        raise ValueError("Attempt has execution artifacts; possible orphan worker, recovery refused")
    shell, processes = ready_pane(task)
    lease = check_lease(task)
    wt = task["worktree"]
    if (git(wt, "rev-parse", "--show-toplevel") != wt or wt == task["repo"] or
        git(wt, "rev-parse", "--path-format=absolute", "--git-common-dir") !=
        git(task["repo"], "rev-parse", "--path-format=absolute", "--git-common-dir") or
        git(wt, "symbolic-ref", "HEAD") != "refs/heads/" + task["branch"]):
        raise ValueError("Worktree/repository/branch identity changed")
    git(wt, "merge-base", "--is-ancestor", task["sha"], "HEAD")
    session = folder / "session.jsonl"
    if session.is_symlink():
        raise ValueError("Saved Pi session path changed")
    with session.open() as stream:
        header = json.loads(stream.readline())
        if (not isinstance(header, dict) or header.get("type") != "session" or header.get("version") != 3 or
            header.get("cwd") != wt or not isinstance(header.get("id"), str)):
            raise ValueError("Saved Pi session identity is invalid")
        uuid.UUID(header["id"])
        for line in stream:
            entry = json.loads(line)
            if not isinstance(entry, dict) or not isinstance(entry.get("type"), str) or entry["type"] == "session":
                raise ValueError("Saved Pi session is malformed")
    # Legacy launches have no session snapshot. A prior published report must postdate it.
    reports = [p for p in folder.glob("report-*.txt") if p.stem[7:].isdigit() and int(p.stem[7:]) < attempt]
    if not reports or session.stat().st_mtime_ns > max(p.stat().st_mtime_ns for p in reports):
        raise ValueError("Session changed since the last reported run; inspect before recovery")
    inventory = lease.get("processes")
    if (not isinstance(inventory, list) or len(inventory) != 1 or
        not isinstance(inventory[0], dict) or inventory[0].get("pid") != shell):
        raise ValueError("Treehouse process inventory is uncertain or contains other worktree processes")
    markers = (str(folder), header["id"], f"{ROOT / 'bin/mate.py'} worker {task['id']} ")
    if any(any(marker in row["args"] for marker in markers) for row in processes.values()):
        raise ValueError("Possible task/session process remains; recovery refused")


def launch_worker(task):
    with lock(HOME / task["id"] / "run.lock"):
        ready_pane(task)  # Shared by dispatch and continuation; never type into Vite/Pi.
    # The receiving wrapper needs this lock, so release it before submitting to Herdr.
    command = shlex.join(["env", f"MATE_HOME={HOME}", sys.executable, str(ROOT / "bin/mate.py"),
                          "worker", task["id"], str(task["attempt"])])
    command = "cd -- " + shlex.quote(task["worktree"]) + " && " + command
    herdr(task, "pane", "run", task["pane"], command)


def resume(db, p):
    task = load(db, p["id"])
    if task["state"] not in ("review", "failed", "attention"):
        raise ValueError("Only a stopped review/failed task or inspected missing launch can continue")
    if task.get("pending_scope"):
        raise ValueError("Pending additional scope requires human /mate-approve before continuing")
    profile = worker_profile(p, task)
    message = text(p["message"], "message")
    recovering = task["state"] == "attention"
    # Keep late old wrappers out until checks and the next-attempt journal commit finish.
    with lock(HOME / task["id"] / "run.lock"):
        if recovering:
            inspect_missing_launch(db, task)
        else:
            ready_pane(task)
            check_lease(task)
        check_capacity(db, inspected=task["id"] if recovering else None)
        if load(db, task["id"]) != task:
            raise ValueError("Task changed during continuation checks")
        with db:
            if recovering:
                previous = {k: v for k, v in task.items() if k != "launch_recoveries"}
                task.setdefault("launch_recoveries", []).append(dict(task=previous, at=time.time(),
                    via="mate_continue", outcome="launch-failed"))
                event(db, task, "launch-recovered", "Inspected unstarted launch; continuing in the same worktree/session with a new attempt. Prior evidence and approval retained.")
            task.update(attempt=task["attempt"] + 1, state="launching", followup=message, **profile)
            task.pop("error", None)
            task.pop("missing_from", None)
            save(db, task)
    try:
        launch_worker(task)
    except Exception as exc:
        with db:
            event(db, task, "launch-uncertain", str(exc))
        raise
    return load(db, task["id"])


def complete(db, p):
    task = load(db, p["id"])
    if task["state"] == "complete":
        return task  # Repeated confirmation does not rewrite the acceptance record.
    if task["state"] != "review" or type(p.get("attempt")) is not int or p["attempt"] != task["attempt"]:
        raise ValueError("Only the reviewed attempt shown in the confirmation can be completed")
    history = task.get("scope_history", [])
    if (task.get("pending_scope") or p.get("scope_revision", 0) != len(history) or
        (history and history[-1]["first_attempt"] > task["attempt"])):
        raise ValueError("Scope changed or awaits approval/execution; review the new result before completion")
    try:
        guard = lock(HOME / task["id"] / "run.lock")
    except BlockingIOError:
        raise ValueError("Worker is still active; wait before completing the task") from None
    try:
        with db:
            task.update(state="complete", completed_at=time.time(),
                        completed_by=pwd.getpwuid(os.getuid()).pw_name,
                        completed_via="mate-complete")
            save(db, task)
    finally:
        guard.close()
    return task  # No acknowledgement, resource cleanup, or Git operations.


def close_tab(db, p):
    task = load(db, p['id'])
    if task['state'] != 'complete' or p.get('attempt') != task['attempt'] or p.get('tab') != task.get('tab'):
        raise ValueError('Tab closure requires the exact completed task/attempt/tab')
    if task.get('tab_close_state') == 'closed':
        return task
    if task.get('tab_close_state') in ('closing', 'uncertain'):
        raise ValueError('Previous tab closure is uncertain; inspect manually, do not retry blindly')
    try:
        guard = lock(HOME / task['id'] / 'run.lock')
    except BlockingIOError:
        raise ValueError('Worker is still active; cannot close its tab') from None
    try:
        pane = check_endpoint(task, task['pane'])
        original = task.get('endpoint_receipt', {}).get('root_pane', {}).get('terminal_id')
        if not original or pane.get('terminal_id') != original:
            raise ValueError('Worker terminal identity changed; refusing to close a reused pane')
        tab = herdr(task, 'tab', 'get', task['tab']).get('tab', {})
        if tab.get('tab_id') != task['tab'] or tab.get('workspace_id') != task['workspace'] or tab.get('pane_count') != 1:
            raise ValueError('Tab identity changed or contains additional panes; close manually')
        # ponytail: foreground-shell check cannot detect background jobs/busy builtins;
        # confirmation warns about job loss; use a job inventory if Herdr adds one.
        process = herdr(task, 'pane', 'process-info', '--pane', task['pane']).get('process_info', {})
        foreground = process.get('foreground_processes', [])
        if process.get('pane_id') != task['pane'] or not process.get('shell_pid') or len(foreground) != 1 or foreground[0].get('pid') != process['shell_pid']:
            raise ValueError('Worker pane is not an idle shell; refusing to interrupt another process')
        task['tab_close_state'] = 'closing'
        with db:
            save(db, task)  # Journal before an external operation with an ambiguous failure mode.
        try:
            herdr(task, 'tab', 'close', task['tab'])
        except Exception as exc:
            task.update(tab_close_state='uncertain', tab_close_error=str(exc))
            with db:
                save(db, task)
            raise
        task.update(tab_close_state='closed', tab_closed_at=time.time(),
                    tab_closed_by=pwd.getpwuid(os.getuid()).pw_name)
        with db:
            save(db, task)
        return task
    finally:
        guard.close()


def empty_usage():
    return dict(messages=0, token_reported_messages=0, cost_reported_messages=0,
                input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0,
                estimated_cost_usd=None)


def record_usage(db, ident, attempt, message):
    """Count finalized assistant messages only, never streaming snapshots or replayed sessions."""
    task = load(db, ident)
    if task['attempt'] != attempt:
        raise ValueError('Usage belongs to a stale worker attempt')
    bucket = task.setdefault('usage', {}).setdefault(str(attempt), empty_usage())
    bucket['messages'] += 1
    usage = message.get('usage')
    if isinstance(usage, dict):
        fields = dict(input='input_tokens', output='output_tokens', cacheRead='cache_read_tokens', cacheWrite='cache_write_tokens')
        if all(type(usage.get(key)) is int and usage[key] >= 0 for key in fields):
            for key, target in fields.items():
                bucket[target] += usage[key]
            bucket['token_reported_messages'] += 1
        cost = usage.get('cost')
        amount = cost.get('total') if isinstance(cost, dict) else None
        if type(amount) in (int, float) and math.isfinite(amount) and amount >= 0:
            bucket['estimated_cost_usd'] = (bucket['estimated_cost_usd'] or 0) + amount
            bucket['cost_reported_messages'] += 1
    with db:
        save(db, task)  # Retain usage even if the worker crashes before its report.


def usage_total(task):
    attempts = task.get('usage', {})
    total = empty_usage()
    for bucket in attempts.values():
        for key in total:
            if key == 'estimated_cost_usd':
                if bucket[key] is not None:
                    total[key] = (total[key] or 0) + bucket[key]
            else:
                total[key] += bucket[key]
    total['untracked_attempts'] = [n for n in range(1, task['attempt'] + 1) if str(n) not in attempts]
    return total


def snapshot(db, p):
    all_tasks = tasks(db)
    start = int(p.get("task_offset", 0))
    if start < 0:
        raise ValueError("Invalid task offset")
    result = {"total_tasks": len(all_tasks), "open_tasks": sum(t["state"] != "complete" for t in all_tasks), "tasks": sorted(all_tasks, key=lambda t: t["updated"], reverse=True)[start:start + 50], "events": [dict(zip(("id", "task", "attempt", "kind", "note"), row))
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
        result["tasks"] = [{k: t[k] for k in ("id", "state", "base", "base_branch", "project", "startup_state", "sha", "attempt", "provider", "model", "effort", "worktree", "pane", "error", "completed_at", "completed_by", "completed_via", "tab_close_state", "tab_closed_at", "tab_closed_by", "tab_close_error") if k in t} | {"usage_total": usage_total(t), "scope_pending": bool(t.get("pending_scope"))} for t in result["tasks"]]
    else:
        task = load(db, p["id"])
        result["tasks"] = [dict(task, usage_total=usage_total(task))]
        result["attempt_usage"] = task.get("usage", {}).get(str(attempt))
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
                task.update(missing_from=task["state"], state="attention",
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
    methods = dict(propose=propose, approve=approve, propose_scope=propose_scope, review_scope=review_scope,
                   dispatch=dispatch, resume=resume,
                   status=snapshot, ack=acknowledge, complete=complete, close_tab=close_tab)
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
        task.setdefault("usage", {}).setdefault(str(attempt), empty_usage())
        save(db, task)
    folder = HOME / ident
    session = folder / "session.jsonl"
    prompt = task.get("followup", task["brief"])
    if task.get("scope_history"):
        prompt = "Current human-approved scope (including additions):\n" + task["brief"] + "\n\nContinuation instructions (within this scope only):\n" + prompt
    policy = (ROOT / "WORKER.md").read_text()
    args = [task["pi_binary"], "--tui-mode", "regular", "--no-prompt-templates",
            "-e", str(ROOT / "bin/worker-events.ts"),
            "--approve", "--provider", task["provider"], "--model", task["model"],
            "--session", str(session), "--append-system-prompt", policy]
    if "effort" in task:  # Legacy in-flight tasks keep their existing CLI/session defaults.
        args += ["--thinking", worker_profile(task)["effort"]]
    args += ["--", prompt]
    child = None
    last = {}
    error = ""
    settled = False
    uncertain = False
    read_fd, write_fd = os.pipe()
    try:
        # Keep stdin/stdout and the foreground process group attached to Herdr's TTY.
        # The private pipe carries events only; never parse or suppress Pi's terminal UI.
        with os.fdopen(read_fd) as stream, (folder / f"events-{attempt}.jsonl").open("w") as log, (folder / f"stderr-{attempt}.log").open("w") as err:
            try:
                child = subprocess.Popen(args, cwd=task["worktree"], stderr=err,
                                         # Reuse Mate's no-supervisor mode; other extensions/skills still load.
                                         env=dict(os.environ, MATE_MODE="dev", MATE_EVENT_FD=str(write_fd)), pass_fds=(write_fd,))
            finally:
                os.close(write_fd)
            def stop(_sig, _frame):
                if child.poll() is None:
                    child.terminate()
                raise InterruptedError("Worker interrupted")
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            for line in iter(lambda: stream.readline(4 * 1024 * 1024 + 1), ""):
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
                    record_usage(db, ident, attempt, last)
                if item.get("type") == "agent_settled":
                    settled = True
                elif item.get("type") == "agent_start":
                    settled = False
            code = child.wait()
            uncertain = code < 0  # A signal-killed Pi may have left tool subprocesses behind.
            if code or not settled or last.get("stopReason") != "stop":
                error = f"Pi exit={code}, settled={settled}, stopReason={last.get('stopReason')}: {last.get('errorMessage', '')}. See stderr-{attempt}.log"
    except Exception as exc:
        error = str(exc)
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
                uncertain = True
        if uncertain:
            error += " Pi was forcibly terminated; inspect pane/processes before any continuation."
        report = "\n".join(part.get("text", "") for part in last.get("content", []) if part.get("type") == "text")
        if not report.strip() and not error:
            error = "Pi exited without a final text report"
        (folder / f"report-{attempt}.txt").write_text((error + "\n\n" if error else "") + report)
        with db:
            current = load(db, ident)
            if current["attempt"] == attempt:
                current.update(state="attention" if uncertain else "failed" if error else "review", error=error)
                save(db, current)
                event(db, current, "worker-missing" if uncertain else "failed" if error else "report", error or "Worker report available; not verified completion.")
        guard.close()
        db.close()
    if error:
        print(f"\n[Mate: {error}]", flush=True)
    print("\n[Mate: report saved; supervisor will wake automatically.]", flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        if sys.argv[1:] == ["serve"]:
            serve()
        elif len(sys.argv) == 4 and sys.argv[1] == "worker":
            worker(task_id(sys.argv[2]), int(sys.argv[3]))
        elif len(sys.argv) == 3 and sys.argv[1] == "recover-acquire":
            recover_acquire_cli(task_id(sys.argv[2]))
        elif sys.argv[1:] == ["status"]:
            with connect() as db:
                print(json.dumps(snapshot(db, {}), indent=2))
        else:
            raise ValueError("Usage: mate.py serve | worker ID ATTEMPT | status | recover-acquire ID")
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
