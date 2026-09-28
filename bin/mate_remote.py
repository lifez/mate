"""Remote secondmate's durable inbox (stdlib only).

Internal storage API, not an authenticated RPC server or a worker launcher.
The eventual receiver must authenticate the primary before calling accept().
A claimed request is never reclaimed automatically after process/SSH failure.

    accept -> queued -> claim (commit) -> external operation -> finish
                            |                                    |
                          crash                            durable outcome
                            v
                  reconcile same request; never blindly re-execute
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

MAX_BYTES = 256 * 1024


def identifier(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Expected canonical UUID")
    return value


def encoded(value):
    result = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                        allow_nan=False)
    if len(result.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Remote record exceeds size limit")
    return result


def create(path, home, primary):
    """Explicit provisioning only. Existing journals are never adopted/rebound."""
    identifier(home)
    identifier(primary)
    path = Path(path)
    # Exclusive creation also prevents silently replacing an existing identity.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    db = sqlite3.connect(path, timeout=10)
    try:
        db.executescript("""
            CREATE TABLE identity (home TEXT NOT NULL, primary_id TEXT NOT NULL);
            CREATE TABLE inbox (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
                fingerprint TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
                claim TEXT, outcome TEXT
            );
            CREATE TABLE outcomes (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                request TEXT NOT NULL UNIQUE REFERENCES inbox(id),
                payload TEXT NOT NULL
            );
        """)
        with db:
            db.execute("INSERT INTO identity VALUES (?, ?)", (home, primary))
    finally:
        db.close()
    # Failed provisioning deliberately leaves a file for inspection, not replacement.


def connect(path, home, primary):
    """Open an existing pinned journal; absence never creates a new remote home."""
    identifier(home)
    identifier(primary)
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=10)
    try:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        if db.execute("SELECT home, primary_id FROM identity").fetchall() != [(home, primary)]:
            raise ValueError("Remote home/primary identity mismatch")
        import mate_remote_events
        mate_remote_events.initialize(db)
        return db
    except BaseException:
        db.close()
        raise


def accept(db, request):
    """Persist a bounded opaque envelope; acceptance is NOT execution/approval.

    Semantic scope/SHA/approval validation belongs to the runtime consumer.
    Matching replays return the original receipt even after execution finishes.
    """
    if (not isinstance(request, dict) or
            set(request) != {"version", "home", "primary", "id", "body"} or
            type(request["version"]) is not int or request["version"] != 1 or
            not isinstance(request["body"], dict)):
        raise ValueError("Invalid remote request envelope")
    ident = identifier(request["id"])
    identity = db.execute("SELECT home, primary_id FROM identity").fetchall()
    if identity != [(request["home"], request["primary"])]:
        raise ValueError("Remote home/primary identity mismatch")
    payload = encoded(request)
    fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT sequence, payload FROM inbox WHERE id=?", (ident,)).fetchone()
        if row:
            if row[1] != payload:
                raise ValueError("Conflicting replay of remote request")
            sequence = row[0]
        else:
            sequence = db.execute("INSERT INTO inbox(id,payload,fingerprint) VALUES (?,?,?)",
                                  (ident, payload, fingerprint)).lastrowid
    return dict(version=1, home=request["home"], id=ident, sequence=sequence,
                fingerprint=fingerprint, accepted=True)


def status(db, ident):
    identifier(ident)
    row = db.execute("SELECT state, outcome, fingerprint FROM inbox WHERE id=?", (ident,)).fetchone()
    if row is None:
        raise ValueError("Unknown remote request")
    return dict(id=ident, state=row[0], outcome=json.loads(row[1]) if row[1] else None,
                fingerprint=row[2])


def claim(db, ident):
    """Commit before any external effect; a crash retains claimed/unknown state."""
    identifier(ident)
    token = str(uuid.uuid4())
    with db:
        db.execute("BEGIN IMMEDIATE")
        updated = db.execute("UPDATE inbox SET state='claimed', claim=? WHERE id=? AND state='queued'",
                             (token, ident))
        if updated.rowcount != 1:
            raise ValueError("Request unavailable; reconcile rather than execute again")
        payload = db.execute("SELECT payload FROM inbox WHERE id=?", (ident,)).fetchone()[0]
    return dict(token=token, request=json.loads(payload))


def finish(db, ident, token, outcome):
    """Atomically publish a terminal receipt and outcome; identical replay is safe.

    Outcome is a transport-operation result, never automatic task completion.
    Unknown external completion must remain claimed until reconciled.
    """
    identifier(ident)
    identifier(token)
    if not isinstance(outcome, dict):
        raise ValueError("Outcome must be an object")
    payload = encoded(outcome)
    if len(payload.encode("utf-8")) > MAX_BYTES - 1024:
        raise ValueError("Outcome exceeds page budget; split large reports")
    with db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT state, claim, outcome FROM inbox WHERE id=?", (ident,)).fetchone()
        if not row or row[1] != token or row[0] not in ("claimed", "done"):
            raise ValueError("Remote claim identity mismatch")
        if row[0] == "done":
            if row[2] != payload:
                raise ValueError("Conflicting remote outcome")
            return
        db.execute("INSERT INTO outcomes(request,payload) VALUES (?,?)", (ident, payload))
        db.execute("UPDATE inbox SET state='done', outcome=? WHERE id=?", (payload, ident))


def outcomes(db, after=0, limit=50):
    """Non-destructive bounded delta. Caller commits locally before advancing."""
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("Invalid outcome cursor/limit")
    latest = db.execute("SELECT COALESCE(MAX(sequence),0) FROM outcomes").fetchone()[0]
    if after > latest:
        raise ValueError("Remote outcome log is behind cursor; inspect continuity")
    rows = db.execute("SELECT sequence, request, payload FROM outcomes WHERE sequence>? ORDER BY sequence LIMIT ?",
                      (after, limit)).fetchall()
    # ponytail: one record per inbox operation; chunk report text at the consumer
    # boundary rather than increasing the fixed transport page budget.
    page, size = [], 0
    for sequence, request, payload in rows:
        record = dict(sequence=sequence, request=request, outcome=json.loads(payload))
        record_size = len(json.dumps(record, ensure_ascii=True).encode("utf-8"))
        if page and size + record_size > MAX_BYTES - 512:
            break
        page.append(record)
        size += record_size
    cursor = page[-1]["sequence"] if page else after
    return dict(home=db.execute("SELECT home FROM identity").fetchone()[0],
                records=page, next_cursor=cursor, more=cursor < latest)
