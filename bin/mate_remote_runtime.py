"""Secondmate control-plane adapter: remote inbox -> existing Mate methods.

Only the owning `mate.py serve` consumes requests, under its supervisor lock.
The local agent may propose/dispatch approved work, but cannot approve, accept or
clean up tasks. Those methods require a parent inbox request and the exact task
revision shown to the human. SSH key/forced-command setup owns authentication;
JSON IDs/confirmation hashes are bindings, not credentials or signatures.
"""
import hashlib
import json
import os
import time
from pathlib import Path

import mate_remote as inbox
import mate_remote_events as notifications
from mate_remote_transport import settings

HUMAN = frozenset({"approve", "review_scope", "complete", "cancel", "close_tab", "return_lease"})
PARENT_METHODS = HUMAN | {"propose", "extend_scope", "propose_scope", "status", "dispatch", "resume",
                          "inspect_cancel", "inspect_return_lease", "ack"}


def revision(config, task):
    # Full stored task, including its last mutation timestamp, not a lossy UI view.
    value = dict(home=config["home"], primary=config["primary"], task=task)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def open_runtime(mate, db, methods):
    path = mate.HOME / "remote.json"
    latched = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='remote_authority'").fetchone()
    if not path.exists() and not path.is_symlink():
        if latched:
            raise ValueError("Remote home binding missing; refusing to become a local supervisor")
        return None
    config = settings(path, receiver=True)
    if Path(config["journal"]).resolve() == (mate.HOME / "mate.sqlite3").resolve():
        raise ValueError("Remote inbox must be separate from the task database")
    queue = inbox.connect(config["journal"], config["home"], config["primary"])
    try:
        endpoint = notifications.get(queue, "secondmate")
        if endpoint:
            if (not endpoint.get("pane") or os.environ.get("HERDR_PANE_ID") != endpoint.get("pane") or
                    os.environ.get("HERDR_SOCKET_PATH") != endpoint.get("socket") or
                    os.environ.get("HERDR_SESSION", "default") != endpoint.get("session")):
                raise ValueError("Secondmate started outside its saved endpoint")
            if mate.check_endpoint(endpoint, endpoint["pane"]).get("terminal_id") != endpoint.get("terminal_id"):
                raise ValueError("Secondmate terminal identity changed")
        pinned = json.dumps(config, sort_keys=True)
        with db:
            db.execute("BEGIN IMMEDIATE")
            if not latched:
                if mate.tasks(db):
                    raise ValueError("Bind a fresh remote home; existing local tasks cannot be adopted")
                db.execute("CREATE TABLE remote_authority (binding TEXT NOT NULL)")
                db.execute("INSERT INTO remote_authority VALUES (?)", (pinned,))
            elif db.execute("SELECT binding FROM remote_authority").fetchall() != [(pinned,)]:
                raise ValueError("Remote home/primary/journal binding changed")
        return RemoteRuntime(mate, db, methods, config, queue, path)
    except BaseException:
        queue.close()
        raise


class RemoteRuntime:
    def __init__(self, mate, db, methods, config, queue, path):
        self.mate, self.db, self.methods = mate, db, methods
        self.config, self.queue, self.path = config, queue, path

    def check_binding(self):
        if settings(self.path, receiver=True) != self.config:
            raise ValueError("Remote binding changed; stop and inspect, never retarget live work")

    def project(self, method, result):
        if method == "status" and isinstance(result, dict):
            result = dict(result, remote_home=self.config["home"], approval_via="primary-only")
            rows = []
            for row in result.get("tasks", []):
                task = self.mate.load(self.db, row["id"])
                rows.append(dict(row, remote_home=self.config["home"],
                                 confirmation=revision(self.config, task)))
            if "tasks" in result:
                result["tasks"] = rows
        elif isinstance(result, dict) and "id" in result and "state" in result:
            # Never attach a newer DB revision to older displayed contents.
            result = dict(result, remote_home=self.config["home"], confirmation=revision(self.config, result))
        return result

    def invoke(self, method, params):
        if method == "status":
            # Displayed fields and their confirmation come from one SQLite snapshot,
            # even if a resident worker changes task state during this read.
            with self.db:
                self.db.execute("BEGIN")
                return self.project(method, self.methods[method](self.db, params))
        return self.project(method, self.methods[method](self.db, params))

    def local(self, method, params):
        self.check_binding()
        if method in HUMAN:
            raise ValueError("Remote secondmate cannot approve/complete/clean up locally; use the primary human dialog")
        return self.invoke(method, params)

    def preflight(self, request):
        body = request["body"]
        method = body.get("method")
        if not isinstance(method, str) or method not in PARENT_METHODS:
            raise ValueError("Unsupported parent operation")
        expected = {"method", "params", "confirmation"} if method in HUMAN else {"method", "params"}
        if set(body) != expected or not isinstance(body["params"], dict):
            raise ValueError("Invalid parent operation envelope")
        params = dict(body["params"])
        # Reserved audit material is generated here, never trusted from a payload.
        if any(key.startswith("_") for key in params):
            raise ValueError("Reserved runtime parameter")
        if method in HUMAN:
            task = self.mate.load(self.db, params.get("id"))
            if body["confirmation"] != revision(self.config, task):
                raise ValueError("Stale parent confirmation; inspect the current remote task and ask the human again")
            params["_parent_authority"] = dict(home=self.config["home"], primary=self.config["primary"],
                                                request=request["id"], confirmation=body["confirmation"])
        return method, params

    def publish(self):
        cursor = notifications.get(self.queue, "event_cursor", 0)
        for ident, task, attempt, kind, note in self.db.execute(
                "SELECT id,task,attempt,kind,note FROM events WHERE id>? ORDER BY id LIMIT 50", (cursor,)):
            notifications.append(self.queue, "event:" + str(ident),
                                 dict(task=task, attempt=attempt, kind=kind, note=note[:2000]))
            with self.queue:
                notifications.put(self.queue, "event_cursor", ident)
        for task in self.mate.tasks(self.db):
            if task["state"] == "awaiting-base" or task.get("pending_scope"):
                notifications.append(self.queue, "proposal:" + revision(self.config, task),
                                     dict(task=task["id"], attempt=task["attempt"], kind="approval-needed",
                                          note="Remote scope needs primary human review; inspect the current task before approving."))
        with self.queue:
            notifications.put(self.queue, "heartbeat", dict(at=time.time(), pid=os.getpid(),
                pane=os.environ.get("HERDR_PANE_ID"), socket=os.environ.get("HERDR_SOCKET_PATH"),
                session=os.environ.get("HERDR_SESSION", "default")))

    def poll(self):
        """One operation per turn. A claimed request is never picked up on restart."""
        self.check_binding()
        self.publish()
        row = self.queue.execute("""SELECT id FROM inbox WHERE state='queued'
            AND COALESCE(json_extract(payload,'$.body.method'),'') NOT IN ('secondmate_start','secondmate_recover')
            ORDER BY sequence LIMIT 1""").fetchone()
        if not row:
            return
        owned = inbox.claim(self.queue, row[0])
        request = owned["request"]
        # Revalidate binding even for records inserted outside the SSH receiver.
        try:
            if any(request[key] != self.config[key] for key in ("home", "primary")):
                raise ValueError("Inbox route identity mismatch")
            method, params = self.preflight(request)
        except (ValueError, KeyError, TypeError) as exc:
            outcome = dict(ok=False, error=str(exc), refused=True)
        else:
            try:
                outcome = dict(ok=True, result=self.invoke(method, params))
                # Bound before publishing; oversize becomes an uncertain outcome,
                # never a false claim of rollback of a completed action.
                if len(inbox.encoded(outcome).encode()) > inbox.MAX_BYTES - 1024:
                    raise ValueError("Runtime result exceeds transport budget; inspect a bounded status page")
            except Exception as exc:
                outcome = dict(ok=False, uncertain=True, error=str(exc)[:4000])
        inbox.finish(self.queue, request["id"], owned["token"], outcome)
        self.publish()

    def close(self):
        self.queue.close()
