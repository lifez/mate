"""Primary-owned delivery journal, separate from remote execution authority.

    staged -> sending (commit before SSH) -> accepted / rejected / uncertain
                   |                                  |
                 crash                                |
                   +---------- read-only reconcile <--+

Neither `accepted` nor remote inbox `done` means a human completed a Mate task.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import mate_remote as inbox


def binding(route):
    inbox.identifier(route["home"])
    inbox.identifier(route["primary"])
    return inbox.encoded({key: route[key] for key in ("host", "home", "primary")})


def create(route):
    """Explicit provisioning, never replace a journal or retarget existing requests."""
    pinned = binding(route)
    fd = os.open(route["outbox"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    db = sqlite3.connect(route["outbox"])
    try:
        db.executescript("""
            CREATE TABLE route (binding TEXT NOT NULL);
            CREATE TABLE results (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE deliveries (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
                state TEXT NOT NULL, evidence TEXT
            );
        """)
        with db:
            db.execute("INSERT INTO route VALUES (?)", (pinned,))
    finally:
        db.close()


def connect(route):
    db = sqlite3.connect(Path(route["outbox"]).resolve().as_uri() + "?mode=rw", uri=True, timeout=10)
    try:
        db.execute("PRAGMA synchronous=FULL")
        if db.execute("SELECT binding FROM route").fetchall() != [(binding(route),)]:
            raise ValueError("Outbox route changed; never retarget a saved request")
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS results (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS resolutions (id TEXT PRIMARY KEY, by_request TEXT NOT NULL)")
        import mate_remote_events
        mate_remote_events.initialize_primary(db)
        return db
    except BaseException:
        db.close()
        raise


def stage(db, request):
    if (not isinstance(request, dict) or
            set(request) != {"version", "home", "primary", "id", "body"} or
            type(request["version"]) is not int or request["version"] != 1 or
            not isinstance(request["body"], dict)):
        raise ValueError("Invalid outbound envelope")
    inbox.identifier(request["id"])
    route = json.loads(db.execute("SELECT binding FROM route").fetchone()[0])
    if any(request[key] != route[key] for key in ("home", "primary")):
        raise ValueError("Outbound home/primary mismatch")
    payload = inbox.encoded(request)
    fingerprint = hashlib.sha256(payload.encode()).hexdigest()
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT payload FROM deliveries WHERE id=?", (request["id"],)).fetchone()
        if old and old[0] != payload:
            raise ValueError("Conflicting outbound request identity")
        if not old:
            if request["body"].get("method") not in ("status", "inspect_cancel", "inspect_return_lease"):
                blocked = unresolved(db)
                recovering_boot = (request["body"].get("method") == "secondmate_recover" and
                                   all(item["method"] in ("secondmate_start", "secondmate_recover") for item in blocked))
                if blocked and not recovering_boot:
                    raise ValueError("Unresolved remote operation; inspect before another mutation: " + blocked[0]["id"])
            db.execute("INSERT INTO deliveries VALUES (?,?,?,'staged',NULL)",
                       (request["id"], payload, fingerprint))
    return inspect(db, request["id"])


def inspect(db, ident):
    inbox.identifier(ident)
    row = db.execute("SELECT state, fingerprint, evidence FROM deliveries WHERE id=?", (ident,)).fetchone()
    if not row:
        raise ValueError("Unknown outbound request")
    return dict(id=ident, state=row[0], fingerprint=row[1],
                evidence=json.loads(row[2]) if row[2] else None)


def store(db, ident, state, evidence):
    payload = inbox.encoded(evidence)
    with db:
        # Another reader may already have proved remote acceptance. An older
        # timeout/refusal must never overwrite that evidence.
        db.execute("UPDATE deliveries SET state=?, evidence=? WHERE id=? AND state IN ('sending','uncertain')",
                   (state, payload, ident))
    return inspect(db, ident)


def deliver(db, ident, send):
    """send(method, params) must return the transport's verified response.

    Only a staged request crosses SSH. Existing sending/uncertain requests return
    their journal entry, never an implicit replay, even after a process restart.
    """
    inbox.identifier(ident)
    with db:
        db.execute("BEGIN IMMEDIATE")
        updated = db.execute("UPDATE deliveries SET state='sending' WHERE id=? AND state='staged'", (ident,))
        row = db.execute("SELECT payload FROM deliveries WHERE id=?", (ident,)).fetchone()
        if not row:
            raise ValueError("Unknown outbound request")
    if not updated.rowcount:
        return inspect(db, ident)
    request = json.loads(row[0])
    try:
        reply = send("accept", {"request": request})
        if reply.get("ok") is not True:
            return store(db, ident, "rejected", {"error": reply["error"]})
        return store(db, ident, "accepted", reply["result"])
    except Exception:
        # Sending was already durable, so even a second failure saving this note
        # cannot make a restart believe the request was never sent.
        return store(db, ident, "uncertain", {"error": "Delivery unconfirmed; reconcile this request, do not redispatch"})


def reconcile(db, ident, send):
    """Read-only remote inspection, never enqueue/reclaim/relaunch work."""
    current = inspect(db, ident)
    if current["state"] not in ("sending", "uncertain"):
        return current
    try:
        reply = send("status", {"id": ident})
        result = reply.get("result", {})
        if (reply.get("ok") is not True or result.get("id") != ident or
                result.get("fingerprint") != current["fingerprint"] or
                result.get("state") not in ("queued", "claimed", "done")):
            raise ValueError("Remote did not prove the exact request")
        return store(db, ident, "accepted", {key: result[key] for key in ("id", "fingerprint", "state")})
    except Exception:
        # Even 'unknown request' is insufficient permission to redispatch after
        # remote restore, reconfiguration, or concurrent delivery.
        return store(db, ident, "uncertain", {"error": "Remote acceptance still unconfirmed; no request resent"})


def unresolved(db):
    """Bounded inventory, not permission to retry or discard an operation."""
    rows = db.execute("""SELECT d.id, d.state, d.payload FROM deliveries d
        LEFT JOIN results r ON r.id=d.id
        LEFT JOIN resolutions z ON z.id=d.id
        WHERE z.id IS NULL AND d.state != 'rejected' AND
          (r.id IS NULL OR json_extract(r.payload, '$.uncertain') = 1)
        ORDER BY d.rowid""")
    result = []
    for ident, state, payload in rows:
        method = json.loads(payload)["body"].get("method")
        if method not in ("status", "inspect_cancel", "inspect_return_lease"):
            result.append(dict(id=ident, state=state, method=method))
            if len(result) == 50:
                break
    return result


def execution(db, ident, send):
    """Pull exact-request outcome and commit it locally before returning to the UI."""
    current = inspect(db, ident)
    cached = db.execute("SELECT payload FROM results WHERE id=?", (ident,)).fetchone()
    if cached:
        return dict(id=ident, state="done", outcome=json.loads(cached[0]))
    if current["state"] == "rejected":
        return dict(id=ident, state="rejected", outcome=current["evidence"])
    reply = send("status", {"id": ident})
    result = reply.get("result", {})
    if (reply.get("ok") is not True or result.get("id") != ident or
            result.get("fingerprint") != current["fingerprint"] or
            result.get("state") not in ("queued", "claimed", "done")):
        raise ValueError("Remote did not prove the exact request; do not resend")
    if result["state"] != "done":
        return dict(id=ident, state=result["state"])
    outcome = result.get("outcome")
    if not isinstance(outcome, dict) or type(outcome.get("ok")) is not bool:
        raise ValueError("Invalid remote runtime outcome")
    if outcome["ok"]:
        valid = set(outcome) == {"ok", "result"} and isinstance(outcome["result"], dict)
    else:
        disposition = "refused" if "refused" in outcome else "uncertain"
        valid = (set(outcome) == {"ok", "error", disposition} and
                 outcome.get(disposition) is True and isinstance(outcome.get("error"), str))
    if not valid:
        raise ValueError("Invalid remote runtime outcome disposition")
    payload = inbox.encoded(outcome)
    with db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT payload FROM results WHERE id=?", (ident,)).fetchone()
        if existing and existing[0] != payload:
            raise ValueError("Remote outcome changed; inspect continuity")
        db.execute("INSERT OR IGNORE INTO results VALUES (?,?)", (ident, payload))
        db.execute("UPDATE deliveries SET state='accepted' WHERE id=?", (ident,))
        request = json.loads(db.execute("SELECT payload FROM deliveries WHERE id=?", (ident,)).fetchone()[0])
        if request['body'].get('method') == 'secondmate_recover' and outcome['ok']:
            result = outcome['result']
            resolved = result.get('resolved_request')
            old = db.execute("SELECT payload FROM deliveries WHERE id=?", (resolved,)).fetchone() if isinstance(resolved, str) else None
            if result.get('state') == 'running' and old and json.loads(old[0])['body'].get('method') in ('secondmate_start', 'secondmate_recover'):
                db.execute("INSERT OR IGNORE INTO resolutions VALUES (?,?)", (resolved, ident))
    return dict(id=ident, state="done", outcome=outcome)
