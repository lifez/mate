#!/usr/bin/env python3
"""Mate's local control plane. stdlib only; macOS/Linux. No Firstmate runtime imports."""
from contextlib import closing
import fcntl
import hashlib
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
NEW_PANE_READY_TIMEOUT = 5


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
    db.execute("""CREATE TABLE IF NOT EXISTS memories (
        revision INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL,
        reason TEXT NOT NULL, saved_at TEXT NOT NULL)""")
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


def endpoint_pane(task):
    receipt = task.get("endpoint_receipt", {})
    return receipt.get("pane", {}) if task.get("same_tab_as") else receipt.get("root_pane", {})


def check_split_target(task):
    target = task["split_target"]
    if any(not isinstance(target.get(k), str) or not target[k] or target[k].startswith("-")
           for k in ("session", "socket", "workspace", "tab", "pane", "terminal_id")):
        raise ValueError("Missing exact split target identity")
    if any(target.get(k) != task[k] for k in ("session", "socket", "workspace")):
        raise ValueError("Split target belongs to a different Herdr endpoint")
    pane = check_endpoint(target, target["pane"])
    if not target.get("tab") or not target.get("terminal_id") or pane.get("terminal_id") != target["terminal_id"]:
        raise ValueError("Split target terminal identity changed or is missing")
    return target


def check_lease(task):
    lease = task["lease"]
    if not isinstance(lease, dict) or lease.get("lease_holder") != task["holder"] or not lease.get("lease_id"):
        raise ValueError("Missing/mismatched Treehouse lease receipt")
    rows = json.loads(run(["treehouse", "status", "--json"], cwd=task["repo"]))
    matches = [r for r in rows if isinstance(r, dict) and str(Path(r.get("path", "")).resolve()) == task["worktree"]]
    if len(matches) != 1 or any(matches[0].get(k) != lease.get(k) for k in ("lease_id", "lease_holder")) or matches[0].get("status") != "leased":
        raise ValueError("Treehouse no longer confirms this task's exact lease")
    return matches[0]


def mate_config():
    config = json.loads(CONFIG.read_text())
    if not isinstance(config, dict) or set(config) - {"worker", "dispatch", "projects"}:
        raise ValueError("Invalid Mate config")
    worker = config.get("worker", {})
    efforts = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
    if not isinstance(worker, dict) or set(worker) - {"model", "effort", "max_active", "workspace_per_task"}:
        raise ValueError("Invalid worker config")
    if "model" in worker and (not isinstance(worker["model"], str) or not worker["model"].strip() or worker["model"] != worker["model"].strip()):
        raise ValueError("Invalid worker.model")
    if "effort" in worker and (not isinstance(worker["effort"], str) or worker["effort"] not in efforts):
        raise ValueError("Invalid worker.effort")
    limit = worker.get("max_active", 2)
    if type(limit) is not int or limit < 1:
        raise ValueError("worker.max_active must be a positive integer")
    if type(worker.get("workspace_per_task", False)) is not bool:
        raise ValueError("worker.workspace_per_task must be a boolean")
    dispatch = config.get("dispatch")
    if "dispatch" in config:
        if (not isinstance(dispatch, dict) or set(dispatch) - {"rules"}
                or not isinstance(dispatch.get("rules"), list) or not dispatch["rules"]
                or "model" not in worker or "effort" not in worker):
            raise ValueError("Invalid dispatch config")
        for rule in dispatch["rules"]:
            if not isinstance(rule, dict) or set(rule) - {"when", "use", "why"}:
                raise ValueError("Invalid dispatch rule")
            text(rule.get("when"), "dispatch rule when", 4000)
            if rule["when"] != rule["when"].strip():
                raise ValueError("Invalid dispatch rule when")
            if "why" in rule:
                text(rule["why"], "dispatch rule why", 4000)
                if rule["why"] != rule["why"].strip():
                    raise ValueError("Invalid dispatch rule why")
            use = rule.get("use")
            if not isinstance(use, dict) or set(use) != {"model", "effort"}:
                raise ValueError("Invalid dispatch profile")
            text(use["model"], "dispatch profile model", 1000)
            if (use["model"] != use["model"].strip() or not isinstance(use["effort"], str)
                    or use["effort"] not in efforts):
                raise ValueError("Invalid dispatch profile")
    return config


def project_config(repo):
    """Trusted Mate config only; never discover executable config in a worker repo."""
    projects = mate_config().get("projects", {})
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
        if (existing["repo"], existing["base"]) != (repo, p.get("base", existing["base"])):
            raise ValueError("Task ID already exists with a different repository or base; choose a new ID")
        if existing["brief"] != brief:
            if existing["state"] != "awaiting-base":
                raise ValueError("Task ID already exists with different scope; choose a new ID")
            existing["brief"] = brief
            with db:
                save(db, existing)
        return existing  # Retrying or revising unapproved scope keeps the pinned SHA and branch.
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
    if (task["state"] != "awaiting-base" or p["sha"] != task["sha"] or
            p.get("brief", task["brief"]) != task["brief"]):
        raise ValueError("Approval no longer matches the pending task")
    task.update(state="approved", approved_at=time.time())
    with db:
        save(db, task)
        event(db, task, "base-approved", "Human approved scope and pinned base. Inspect current task; use mate_dispatch. No worker started.")
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
    limit = mate_config().get("worker", {}).get("max_active", 2)
    if sum(t["state"] in ("acquiring", "launching", "running") for t in fleet) >= limit:
        raise ValueError(f"{limit} workers are already active")


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
    if task["state"] == "cancelled":
        raise ValueError("Cancelled task cannot be dispatched")
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
    caller = check_endpoint(task, parent)  # Before acquiring anything.
    if not task.get("recoveries"):
        task.update(launcher_workspace=workspace,
                    workspace_per_task=(mate_config().get("worker", {}).get("workspace_per_task", False)
                                        and "same_tab_as" not in p))
    if not task.get("recoveries") and "same_tab_as" in p:
        reference = task_id(p["same_tab_as"])
        if reference != "supervisor" and reference == task["id"]:
            raise ValueError("Cannot split relative to the task being dispatched")
        source = task if reference == "supervisor" else load(db, reference)
        if reference != "supervisor" and source.get("tab_close_state"):
            raise ValueError("Target tab is closed or closure is uncertain")
        pane = caller if reference == "supervisor" else endpoint_pane(source)
        task.update(same_tab_as=reference, split_target=dict(
            session=source.get("session"), socket=source.get("socket"), workspace=source.get("workspace"),
            tab=caller.get("tab_id") if reference == "supervisor" else source.get("tab"),
            pane=parent if reference == "supervisor" else source.get("pane"), terminal_id=pane.get("terminal_id")))
    if task.get("same_tab_as"):
        check_split_target(task)
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
        if task.get("same_tab_as"):
            target = check_split_target(task)  # Recheck after acquisition/startup; never fall back.
            created = herdr(task, "pane", "split", target["pane"], "--direction", "right", "--cwd", wt, "--no-focus")
        elif task.get("workspace_per_task"):
            created = herdr(task, "workspace", "create", "--cwd", wt,
                            "--label", "└ " + task["id"], "--no-focus")
            task["workspace"] = created["workspace"]["workspace_id"]
        else:
            created = herdr(task, "tab", "create", "--workspace", workspace, "--cwd", wt,
                            "--label", "mate-" + task["id"], "--no-focus")
        task["endpoint_receipt"] = created
        with db:
            save(db, task)
        pane = endpoint_pane(task)
        task["pane"] = pane["pane_id"]
        task["tab"] = target["tab"] if task.get("same_tab_as") else created["tab"]["tab_id"]
        if task.get("same_tab_as") and (pane.get("tab_id") != task["tab"] or
                pane.get("workspace_id") != workspace or pane["pane_id"] == target["pane"] or
                pane.get("terminal_id") == target["terminal_id"]):
            raise ValueError("Split receipt does not identify a new pane in the target tab")
        actual = check_endpoint(task, task["pane"])
        if not pane.get("terminal_id") or actual.get("terminal_id") != pane["terminal_id"]:
            raise ValueError("Created terminal identity mismatch")
        with db:
            save(db, task)
        launch_worker(task, NEW_PANE_READY_TIMEOUT)
    except Exception as exc:
        # Do not roll back external resources or auto-retry an uncertain launch.
        with db:
            latest = load(db, task["id"])
            if latest["state"] in ("acquiring", "launching"):
                task.update(state="attention", error=str(exc))
                if task.get("pane"):
                    task["launch_stage"] = "preflight-refused" if isinstance(exc, LaunchPreflightRefused) else "uncertain"
                save(db, task)
                event(db, task, "launch-uncertain", str(exc))
        raise
    return load(db, task["id"])


CANCELLATION_EXECUTION_FIELDS = (
    "lease", "worktree", "startup_state", "startup_started_at", "startup_finished_at",
    "startup_pid", "startup_exit_code", "endpoint_receipt", "pane", "tab", "session",
    "socket", "workspace", "pi_binary", "holder", "usage", "followup", "error",
    "missing_from", "recoveries", "launch_recoveries", "launch_stage")


def cancellation_fingerprint(task):
    return hashlib.sha256(json.dumps(task, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def cancellation_folder(task, required):
    folder = HOME / task["id"]
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ValueError("Task artifact directory is malformed; cancellation refused")
    if not folder.exists():
        if required:
            raise ValueError("Task artifact directory is missing; cancellation refused")
        return folder
    children = list(folder.iterdir())
    for child in children:
        if child.name != "run.lock" or child.is_symlink() or not child.is_file():
            raise ValueError("Task has execution artifacts; cancellation refused")
    return folder


def cancellation_git_checks(task):
    repo = Path(task.get("repo", ""))
    branch = task.get("branch")
    sha = task.get("sha")
    if not repo.is_absolute() or not repo.is_dir() or str(repo.resolve()) != task.get("repo"):
        raise ValueError("Task repository identity is malformed; cancellation refused")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Approved SHA is malformed; cancellation refused")
    if not isinstance(branch, str) or not branch or branch.startswith("-") or any(c.isspace() for c in branch):
        raise ValueError("Task branch is malformed; cancellation refused")
    try:
        git(repo, "check-ref-format", "refs/heads/" + branch)
        if git(repo, "rev-parse", "--show-toplevel") != str(repo):
            raise ValueError("Task repository identity is uncertain; cancellation refused")
    except (RuntimeError, ValueError):
        raise ValueError("Task repository or branch inspection failed; cancellation refused") from None
    if git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/" + branch):
        raise ValueError("Task branch already exists; cancellation refused")


def cancellation_treehouse_checks(task):
    try:
        rows = json.loads(run(["treehouse", "status", "--json"], cwd=task["repo"]))
    except (RuntimeError, json.JSONDecodeError, TypeError):
        raise ValueError("Treehouse status is unavailable or malformed; cancellation refused") from None
    if not isinstance(rows, list):
        raise ValueError("Treehouse status is unavailable or malformed; cancellation refused")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or not Path(row["path"]).is_absolute():
            raise ValueError("Treehouse status is unavailable or malformed; cancellation refused")
        if not isinstance(row.get("status"), str) or not row["status"]:
            raise ValueError("Treehouse status is unavailable or malformed; cancellation refused")
        if row["status"] == "leased" and any(not isinstance(row.get(key), str) or not row[key]
                                              for key in ("lease_id", "lease_holder")):
            raise ValueError("Treehouse status is unavailable or malformed; cancellation refused")
        if "lease_holder" in row and row["lease_holder"] is not None and not isinstance(row["lease_holder"], str):
            raise ValueError("Treehouse status is unavailable or malformed; cancellation refused")
        if row.get("lease_holder") == task["holder"]:
            raise ValueError("Treehouse still records the saved task holder; cancellation refused")
    return len(rows)


def cancellation_process_checks(task):
    try:
        output = run(["ps", "-axo", "pid=,ppid=,pgid=,tty=,comm=,args="])
        rows = output.splitlines()
        for line in rows:
            fields = line.split(None, 5)
            if len(fields) != 6 or any(not field for field in fields[:5]):
                raise ValueError("Process inspection is malformed; cancellation refused")
    except (RuntimeError, ValueError):
        raise ValueError("Process inspection is unavailable or malformed; cancellation refused") from None
    markers = [str(HOME / task["id"]), task["id"], task["branch"], task["holder"],
               f"worker {task['id']} ", f"MATE_TASK_ID={task['id']}"]
    startup = task.get("startup")
    if isinstance(startup, dict):
        markers.extend(arg for arg in startup.get("command", [])
                       if isinstance(arg, str) and arg.startswith("/"))
    for line in rows:
        args = line.split(None, 5)[5]
        if any(marker in args for marker in markers):
            raise ValueError("Possible task/setup/session process remains; cancellation refused")
    return len(rows)


def cancellation_herdr_checks(task):
    try:
        result = herdr(task, "tab", "list", "--workspace", task["workspace"])
    except Exception:
        raise ValueError("Herdr task evidence is unavailable; cancellation refused") from None
    tabs = result.get("tabs") if isinstance(result, dict) else None
    if not isinstance(tabs, list):
        raise ValueError("Herdr task evidence is malformed; cancellation refused")
    for tab in tabs:
        if not isinstance(tab, dict) or not isinstance(tab.get("tab_id"), str) or tab.get("workspace_id") != task["workspace"]:
            raise ValueError("Herdr task evidence is malformed; cancellation refused")
        if tab.get("label") == "mate-" + task["id"]:
            raise ValueError("Herdr has a matching Mate task tab; cancellation refused")
    return len(tabs)


def inspect_cancel_locked(db, task):
    state = task.get("state")
    for key in ("id", "repo", "base", "brief"):
        if not isinstance(task.get(key), str) or not task[key]:
            raise ValueError("Task record is malformed; cancellation refused")
    if task["id"] != task_id(task["id"]):
        raise ValueError("Task ID is malformed; cancellation refused")
    startup = task.get("startup")
    if startup is not None and (not isinstance(startup, dict) or not isinstance(startup.get("command"), list) or
                                not startup["command"] or
                                any(not isinstance(arg, str) or not arg for arg in startup["command"])):
        raise ValueError("Saved startup config is malformed; cancellation refused")
    if state not in ("awaiting-base", "approved", "attention"):
        raise ValueError("Only an unstarted awaiting-base/approved task or inspected pre-receipt attention task can be cancelled")
    if type(task.get("attempt")) is not int or task["attempt"] < 0:
        raise ValueError("Task attempt is malformed; cancellation refused")
    attention = state == "attention"
    if attention:
        if task["attempt"] != 1 or type(task.get("approved_at")) not in (int, float) or task["approved_at"] <= 0:
            raise ValueError("Only the initial pre-receipt attention attempt can be cancelled")
        if task.get("missing_from") not in (None, "acquiring"):
            raise ValueError("Attention is from a later phase; cancellation refused")
        for key in ("holder", "session", "socket", "workspace", "error"):
            if not isinstance(task.get(key), str) or not task[key]:
                raise ValueError("Saved acquisition identity is malformed; cancellation refused")
        if not task["holder"].startswith("mate:"):
            raise ValueError("Saved acquisition holder is malformed; cancellation refused")
    elif task["attempt"] != 0:
        raise ValueError("Unstarted cancellation requires attempt 0")
    allowed_attention_identity = {"holder", "session", "socket", "workspace", "pi_binary", "error", "missing_from"}
    if any(key in task and (not attention or key not in allowed_attention_identity)
           for key in CANCELLATION_EXECUTION_FIELDS):
        raise ValueError("Task has saved execution evidence; cancellation refused")
    if state == "approved" and type(task.get("approved_at")) not in (int, float):
        raise ValueError("Approved task record is malformed; cancellation refused")
    if state == "awaiting-base" and "approved_at" in task:
        raise ValueError("Awaiting-base task record is malformed; cancellation refused")
    cancellation_git_checks(task)
    folder = cancellation_folder(task, required=attention)
    if attention:
        cancellation_treehouse_checks(task)
        cancellation_process_checks(task)
        cancellation_herdr_checks(task)
    checks = [f"state {state}, attempt {task['attempt']}", "approved SHA and task branch inspected", "task artifact directory contains no execution evidence"]
    if attention:
        checks.extend(["Treehouse status has no row for the exact saved holder", "process snapshot has no task/setup/session match", "Herdr task workspace has no matching Mate tab"])
    return dict(task=task, confirmation=cancellation_fingerprint(task), already_cancelled=False,
                requires_external_attestation=attention, checks=checks,
                artifact_directory=str(folder), treehouse_checked=attention,
                process_checked=attention, herdr_checked=attention)


def inspect_cancel(db, p):
    task = load(db, p["id"])
    if task.get("state") == "cancelled":
        return dict(task=task, already_cancelled=True, confirmation=cancellation_fingerprint(task), checks=[])
    if (task.get("state") not in ("awaiting-base", "approved", "attention") or
        (task.get("state") == "attention" and task.get("missing_from") not in (None, "acquiring"))):
        return inspect_cancel_locked(db, task)
    folder = cancellation_folder(task, required=task.get("state") == "attention")
    if folder.exists():
        try:
            guard = lock(folder / "run.lock")
        except BlockingIOError:
            raise ValueError("Task wrapper lock is busy; cancellation refused") from None
        try:
            current = load(db, task["id"])
            if current != task:
                raise ValueError("Cancellation inspection became stale; retry the human command")
            return inspect_cancel_locked(db, current)
        finally:
            guard.close()
    return inspect_cancel_locked(db, task)


def cancel(db, p):
    task = load(db, p["id"])
    if task.get("state") == "cancelled":
        return task  # Repeated cancellation preserves the original audit/history.
    if p.get("confirmed") is not True:
        raise ValueError("Cancellation requires the human confirmation dialog")
    expected = p.get("confirmation")
    if not isinstance(expected, str) or not expected:
        raise ValueError("Cancellation confirmation is missing")
    if (cancellation_fingerprint(task) != expected or p.get("state") != task.get("state") or
        p.get("attempt") != task.get("attempt") or p.get("sha") != task.get("sha")):
        raise ValueError("Cancellation confirmation is stale; task unchanged")
    if task["state"] not in ("awaiting-base", "approved", "attention"):
        raise ValueError("Task is not eligible for cancellation")
    folder = HOME / task["id"]
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ValueError("Task artifact directory is malformed; cancellation refused")
    if task["state"] == "attention" and not folder.exists():
        raise ValueError("Task artifact directory is missing; cancellation refused")
    folder.mkdir(mode=0o700, exist_ok=True)
    try:
        guard = lock(folder / "run.lock")
    except BlockingIOError:
        raise ValueError("Task wrapper lock is busy; cancellation refused") from None
    try:
        current = load(db, task["id"])
        if current.get("state") == "cancelled":
            return current
        if cancellation_fingerprint(current) != expected or p.get("state") != current.get("state") or \
           p.get("attempt") != current.get("attempt") or p.get("sha") != current.get("sha"):
            raise ValueError("Cancellation confirmation is stale; task unchanged")
        inspection = inspect_cancel_locked(db, current)
        if inspection["requires_external_attestation"] and p.get("attest_external") is not True:
            raise ValueError("Human external-orphan inspection attestation is required")
        now = time.time()
        audit = dict(at=now, by=pwd.getpwuid(os.getuid()).pw_name, via="mate-cancel",
                     state_before=current["state"], attempt=current["attempt"], sha=current["sha"],
                     external_inspection_attested=inspection["requires_external_attestation"],
                     checks=inspection["checks"])
        current.setdefault("cancellation_history", []).append(audit)
        current.update(state="cancelled", cancelled_at=now, cancelled_by=audit["by"], cancelled_via="mate-cancel")
        with db:
            save(db, current)
            event(db, current, "cancelled", "Human confirmed cancellation; task history and any existing evidence were retained. No cleanup or completion was performed.")
        return current
    finally:
        guard.close()


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
    original = endpoint_pane(task).get("terminal_id")
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
    """Prove an initial preflight refusal or an unstarted continuation, never a crash."""
    attempt = task["attempt"]
    initial = attempt == 1 and not task.get("followup") and not task.get("usage")
    if initial:
        # Legacy dispatch emitted only the idle-shell error before pane run. New records
        # distinguish either safe readiness refusal from uncertain submission.
        legacy_error = "Worker pane is not an idle shell; return it to its shell, then request mate_continue. No keys sent."
        error = task.get("error")
        stage = task.get("launch_stage")
        if (error not in (legacy_error, "Worker pane has background/stopped processes; inspect before continuing") or
            stage not in (None, "preflight-refused") or (stage is None and error != legacy_error) or
            task.get("missing_from") is not None or not task.get("approved_at") or
            task.get("startup_state") not in (None, "succeeded") or
            (task.get("startup") and task.get("startup_state") != "succeeded") or
            task.get("recoveries") or task.get("launch_recoveries") or task.get("scope_history") or
            not db.execute("SELECT 1 FROM events WHERE task=? AND attempt=1 AND kind='launch-uncertain' AND note=?",
                           (task["id"], error)).fetchone() or
            db.execute("SELECT 1 FROM events WHERE task=? AND kind NOT IN ('base-approved', 'launch-uncertain')",
                       (task["id"],)).fetchone()):
            raise ValueError("Only a proven initial preflight refusal can recover; uncertain launches require inspection")
        folder = HOME / task["id"]
        if folder.is_symlink() or any(p.name not in ("run.lock", "startup.log") or
                                     p.is_symlink() or not p.is_file() for p in folder.iterdir()):
            raise ValueError("Initial attempt has execution artifacts or uncertain paths; recovery refused")
    elif (task.get("error") != "No worker lock after 60s. Resources retained; no automatic relaunch." or
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
    if initial:
        if git(wt, "rev-parse", "HEAD") != task["sha"]:
            raise ValueError("Initial worktree HEAD changed; recovery refused")
        inventory = lease.get("processes")
        if (not isinstance(inventory, list) or len(inventory) != 1 or
            not isinstance(inventory[0], dict) or inventory[0].get("pid") != shell):
            raise ValueError("Treehouse process inventory is uncertain or contains other worktree processes")
        if any(str(folder) in row["args"] or wt in row["args"] or
               f"{ROOT / 'bin/mate.py'} worker {task['id']} " in row["args"]
               for pid, row in processes.items() if pid != shell):
            raise ValueError("Possible task/worktree process remains; recovery refused")
        return True
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


class LaunchPreflightRefused(ValueError):
    pass


def wait_ready_pane(task, timeout):
    deadline = time.monotonic() + timeout
    while True:
        try:
            return ready_pane(task)
        except ValueError as exc:
            if str(exc) not in (
                    "Worker pane is not an idle shell; return it to its shell, then request mate_continue. No keys sent.",
                    "Worker pane has background/stopped processes; inspect before continuing") or time.monotonic() >= deadline:
                raise
            time.sleep(.1)


def launch_worker(task, readiness_timeout=0):
    with lock(HOME / task["id"] / "run.lock"):
        try:
            if readiness_timeout:
                wait_ready_pane(task, readiness_timeout)
            else:
                ready_pane(task)  # Existing panes must already be idle; never type into Vite/Pi.
        except ValueError as exc:
            raise LaunchPreflightRefused(str(exc)) from exc
    # The receiving wrapper needs this lock, so release it before submitting to Herdr.
    command = shlex.join(["env", f"MATE_HOME={HOME}", sys.executable, str(ROOT / "bin/mate.py"),
                          "worker", task["id"], str(task["attempt"])])
    command = "cd -- " + shlex.quote(task["worktree"]) + " && " + command
    herdr(task, "pane", "run", task["pane"], command)


def confirm_recovered_worker(db, task):
    # Observe once, never resubmit. The durable receipt also covers a fast exit;
    # it proves Popen succeeded, not successful work or a still-live Pi.
    deadline = time.monotonic() + 10
    while True:
        started = db.execute("SELECT 1 FROM events WHERE task=? AND attempt=? AND kind='worker-started'",
                             (task["id"], task["attempt"])).fetchone()
        current = load(db, task["id"])
        if started or current["state"] not in ("launching", "running") or time.monotonic() >= deadline:
            return dict(current, launch_confirmation="started" if started else "unconfirmed")
        time.sleep(0.1)


def resume(db, p):
    task = load(db, p["id"])
    if task["state"] not in ("review", "failed", "attention"):
        raise ValueError("Only a stopped review/failed task or inspected missing launch can continue")
    if task.get("pending_scope"):
        raise ValueError("Pending additional scope requires human /mate-approve before continuing")
    profile = worker_profile(p, task)
    message = text(p["message"], "message")
    recovering = task["state"] == "attention"
    initial = False
    # Keep late old wrappers out until checks and the next-attempt journal commit finish.
    with lock(HOME / task["id"] / "run.lock"):
        if recovering:
            initial = inspect_missing_launch(db, task)
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
                event(db, task, "launch-recovered",
                      "Inspected initial preflight refusal; starting the first Pi session in the same worktree with a new attempt. Prior evidence and approval retained."
                      if initial else "Inspected unstarted launch; continuing in the same worktree/session with a new attempt. Prior evidence and approval retained.")
            if initial:
                message = task["brief"] + "\n\nRecovery instructions (within approved scope only):\n" + message
            task.update(attempt=task["attempt"] + 1, state="launching", followup=message, **profile)
            task.pop("launch_stage", None)
            task.pop("error", None)
            task.pop("missing_from", None)
            save(db, task)
    try:
        launch_worker(task)
    except Exception as exc:
        with db:
            event(db, task, "launch-uncertain", str(exc))
        raise
    return confirm_recovered_worker(db, task) if initial else load(db, task["id"])


def complete(db, p):
    task = load(db, p["id"])
    if task["state"] == "complete":
        return task  # Repeated confirmation does not rewrite the acceptance record.
    force = p.get("force", False)
    if type(force) is not bool:
        raise ValueError("force must be a boolean")
    if (task["state"] not in (("review", "failed") if force else ("review",)) or
        type(p.get("attempt")) is not int or p["attempt"] != task["attempt"]):
        raise ValueError("Only the reviewed attempt (or stopped failed attempt with --force) shown in the confirmation can be completed")
    history = task.get("scope_history", [])
    if (task.get("pending_scope") or p.get("scope_revision", 0) != len(history) or
        (history and history[-1]["first_attempt"] > task["attempt"])):
        raise ValueError("Scope changed or awaits approval/execution; review the new result before completion")
    try:
        guard = lock(HOME / task["id"] / "run.lock")
    except BlockingIOError:
        raise ValueError("Worker is still active; wait before completing the task") from None
    try:
        if force:
            ready_pane(task)
            check_lease(task)
        if load(db, task["id"]) != task:
            raise ValueError("Task changed during completion checks; confirm again")
        with db:
            if force:
                task["completed_from"] = task["state"]
            task.update(state="complete", completed_at=time.time(),
                        completed_by=pwd.getpwuid(os.getuid()).pw_name,
                        completed_via="mate-complete --force" if force else "mate-complete")
            save(db, task)
    finally:
        guard.close()
    return task  # No acknowledgement, resource cleanup, or Git operations.


def return_lease(db, p):
    task = load(db, p['id'])
    lease = task.get('lease', {})
    if (task['state'] != 'complete' or p.get('attempt') != task['attempt'] or
        p.get('worktree') != task.get('worktree') or p.get('lease_id') != lease.get('lease_id') or
        p.get('lease_holder') != lease.get('lease_holder')):
        raise ValueError('Lease return requires the exact completed task/attempt/worktree/lease')
    if task.get('lease_return_state') == 'returned':
        return task
    if task.get('lease_return_state') in ('returning', 'uncertain'):
        raise ValueError('Previous Treehouse return is uncertain; inspect manually, do not retry blindly')
    try:
        guard = lock(HOME / task['id'] / 'run.lock')
    except BlockingIOError:
        raise ValueError('Worker is still active; cannot return its lease') from None
    try:
        current_lease = check_lease(task)
        if task.get('tab_close_state') == 'closed':
            if current_lease.get('processes') != []:
                raise ValueError('Treehouse still reports worktree processes after tab closure')
        else:
            shell, _ = ready_pane(task)
            processes = current_lease.get('processes')
            if (not isinstance(processes, list) or len(processes) != 1 or
                not isinstance(processes[0], dict) or processes[0].get('pid') != shell):
                raise ValueError('Treehouse process inventory is uncertain or contains other worktree processes')
        if git(task['worktree'], '-c', 'status.showUntrackedFiles=all', 'status', '--porcelain'):
            raise ValueError('Worktree has uncommitted changes; commit or remove them before returning the lease')
        if load(db, task['id']) != task:
            raise ValueError('Task changed during lease return checks; confirm again')
        task['lease_return_state'] = 'returning'
        with db:
            save(db, task)
        try:
            run(['treehouse', 'return', task['worktree'], '--if-lease-id', lease['lease_id'],
                 '--if-lease-holder', lease['lease_holder']], cwd=task['repo'], timeout=120)
            rows = json.loads(run(['treehouse', 'status', '--json'], cwd=task['repo']))
            matches = [row for row in rows if isinstance(row, dict) and isinstance(row.get('path'), str)
                       and str(Path(row['path']).resolve()) == task['worktree']] if isinstance(rows, list) else []
            if (len(matches) != 1 or matches[0].get('status') != 'available' or
                matches[0].get('lease_id') not in (None, '') or matches[0].get('lease_holder') not in (None, '')):
                raise RuntimeError('Treehouse did not prove that the exact lease was returned')
        except Exception as exc:
            task.update(lease_return_state='uncertain', lease_return_error=str(exc))
            with db:
                save(db, task)
            raise
        task.update(lease_return_state='returned', lease_returned_at=time.time(),
                    lease_returned_by=pwd.getpwuid(os.getuid()).pw_name)
        with db:
            save(db, task)
        return task
    finally:
        guard.close()


def close_tab(db, p):
    task = load(db, p['id'])
    if task['state'] != 'complete' or p.get('attempt') != task['attempt'] or p.get('tab') != task.get('tab'):
        raise ValueError('Tab closure requires the exact completed task/attempt/tab')
    if task.get('same_tab_as'):
        raise ValueError('Shared tab cannot be closed by Mate; retain the pane or close it manually')
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
        original = endpoint_pane(task).get('terminal_id')
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
    history = p.get("history", False)
    if type(history) is not bool:
        raise ValueError("history must be a boolean")
    offset = p.get("offset", 0)
    if type(offset) is not int or offset < 0:
        raise ValueError("Invalid offset")
    if (history or "attempt" in p or "offset" in p) and not p.get("id"):
        raise ValueError("history, attempt and offset require a task id")
    if offset and ("attempt" not in p or history):
        raise ValueError("Report continuation requires an explicit attempt and no history")
    all_tasks = tasks(db)
    start = int(p.get("task_offset", 0))
    if start < 0:
        raise ValueError("Invalid task offset")
    result = {"total_tasks": len(all_tasks), "open_tasks": sum(t["state"] not in ("complete", "cancelled") for t in all_tasks), "tasks": sorted(all_tasks, key=lambda t: t["updated"], reverse=True)[start:start + 50], "events": [dict(zip(("id", "task", "attempt", "kind", "note"), row))
              for row in db.execute("SELECT id,task,attempt,kind,note FROM events WHERE ack IS NULL ORDER BY id LIMIT 50")]}
    if p.get("id"):
        task = load(db, p["id"])
        attempt = p.get("attempt", task["attempt"])
        if type(attempt) is not int or attempt < 0 or attempt > task["attempt"] or ("attempt" in p and attempt == 0):
            raise ValueError("Invalid attempt")
        report = HOME / task["id"] / f"report-{attempt}.txt"
        result["report_attempt"] = attempt
        if offset:
            # Page the pinned report, not the task's growing brief/audit history.
            result = dict(id=task["id"], report_attempt=attempt, current_attempt=task["attempt"],
                          state=task["state"], updated=task["updated"])
        if report.exists():
            with report.open() as f:
                f.seek(offset)
                content = f.read(12000)
                result["report"] = dict(path=str(report), text=content, next_offset=f.tell(), more=bool(f.read(1)))
        elif offset:
            raise ValueError("Report is unavailable for the requested attempt")
        if offset:
            return result
    # Do not send every brief/receipt repeatedly into model context.
    if not p.get("id"):
        result["tasks"] = [{k: t[k] for k in ("id", "state", "base", "base_branch", "project", "startup_state", "sha", "attempt", "provider", "model", "effort", "worktree", "launcher_workspace", "workspace", "workspace_per_task", "pane", "tab", "same_tab_as", "error", "completed_at", "completed_by", "completed_via", "completed_from", "cancelled_at", "cancelled_by", "cancelled_via", "tab_close_state", "tab_closed_at", "tab_closed_by", "tab_close_error") if k in t} | {"usage_total": usage_total(t), "scope_pending": bool(t.get("pending_scope"))} for t in result["tasks"]]
    else:
        fields = ("id", "state", "updated", "repo", "base", "base_branch", "sha", "branch", "brief",
                  "attempt", "approved_at", "provider", "model", "effort", "worktree",
                  "launcher_workspace", "workspace", "workspace_per_task", "pane", "tab",
                  "same_tab_as", "error", "missing_from", "launch_stage", "pending_scope",
                  "startup", "startup_state", "startup_started_at", "startup_finished_at", "startup_pid", "startup_exit_code",
                  "completed_at", "completed_by", "completed_via", "completed_from",
                  "cancelled_at", "cancelled_by", "cancelled_via", "tab_close_state", "tab_close_error",
                  "tab_closed_at", "tab_closed_by")
        current = dict(task) if history else {k: task[k] for k in fields if k in task}
        scopes = task.get("scope_history", [])
        current.update(usage_total=usage_total(task), scope_revision=len(scopes))
        if scopes:
            current["latest_scope"] = {k: scopes[-1][k] for k in ("token", "first_attempt", "approved_at") if k in scopes[-1]}
        result["tasks"] = [current]
        result["attempt_usage"] = task.get("usage", {}).get(str(attempt))
    return result


def memory(db, p):
    """Bounded current notes, append-only cold history; never task/event authority."""
    action = p.get("action", "read")
    revision = p.get("revision")
    if action not in ("read", "save") or (revision is not None and (type(revision) is not int or revision < 0)):
        raise ValueError("Invalid memory action/revision")
    with db:
        # Same transaction covers comparison + append, including non-server callers.
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT revision,content,reason,saved_at FROM memories ORDER BY revision DESC LIMIT 1").fetchone()
        if action == "save":
            content = text(p.get("content"), "memory content", 12000)
            if len(content.encode("utf-8")) > 12000:
                raise ValueError("Memory exceeds 12000 UTF-8 bytes; curate before saving")
            reason = text(p.get("reason"), "memory change reason", 1000)
            if revision != (current[0] if current else 0):
                raise ValueError("Memory revision changed; read current memory before saving")
            if not current or content != current[1]:
                db.execute("INSERT INTO memories(content,reason,saved_at) VALUES (?,?,?)",
                           (content, reason, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
            current = db.execute("SELECT revision,content,reason,saved_at FROM memories ORDER BY revision DESC LIMIT 1").fetchone()
        elif revision is not None:
            current = db.execute("SELECT revision,content,reason,saved_at FROM memories WHERE revision=?", (revision,)).fetchone()
            if not current:
                raise ValueError("Unknown memory revision")
        record = dict(zip(("revision", "content", "reason", "saved_at"), current)) if current else dict(revision=0, content="", reason="", saved_at=None)
    return dict(record, bytes=len(record["content"].encode("utf-8")), budget_bytes=12000,
                storage=str(HOME / "mate.sqlite3") + "#memories")


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
                   dispatch=dispatch, resume=resume, inspect_cancel=inspect_cancel, cancel=cancel,
                   status=snapshot, memory=memory, ack=acknowledge, complete=complete,
        return_lease=return_lease, close_tab=close_tab)
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
                if task.get("launch_recoveries"):
                    with db:
                        event(db, task, "worker-started", "Pi process started in the saved endpoint/worktree; not verified completion.")
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
