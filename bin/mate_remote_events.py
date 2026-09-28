"""Append-only remote notifications and atomic primary mirror/cursor/ack storage."""
import hashlib
import json
import uuid

import mate_remote as inbox


def digest(previous, source, payload):
    return hashlib.sha256(inbox.encoded(dict(previous=previous, source=source, payload=payload)).encode()).hexdigest()


def initialize(db):
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS remote_state (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
        db.execute("""CREATE TABLE IF NOT EXISTS notifications (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT UNIQUE NOT NULL,
            payload TEXT NOT NULL, digest TEXT NOT NULL)""")
        db.execute("INSERT OR IGNORE INTO remote_state VALUES ('stream',?)", (str(uuid.uuid4()),))


def get(db, key, default=None):
    row = db.execute("SELECT data FROM remote_state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def put(db, key, value):
    db.execute("INSERT OR REPLACE INTO remote_state VALUES (?,?)", (key, inbox.encoded(value)))


def genesis(stream):
    inbox.identifier(stream)
    return hashlib.sha256(("mate-events:" + stream).encode()).hexdigest()


def stream_id(db):
    # Stream UUID is raw text; other metadata is encoded JSON.
    return db.execute("SELECT data FROM remote_state WHERE key='stream'").fetchone()[0]


def append(db, source, payload):
    encoded = inbox.encoded(payload)
    if not isinstance(payload, dict) or "id" in payload:
        raise ValueError("Notification payload cannot override its sequence")
    if not isinstance(source, str) or not source or len(source) > 160 or len(encoded) > 8192:
        raise ValueError("Invalid remote notification")
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT payload FROM notifications WHERE source=?", (source,)).fetchone()
        if old:
            if old[0] != encoded:
                raise ValueError("Remote event source changed; inspect continuity")
            return
        last = db.execute("SELECT digest FROM notifications ORDER BY sequence DESC LIMIT 1").fetchone()
        previous = last[0] if last else genesis(stream_id(db))
        db.execute("INSERT INTO notifications(source,payload,digest) VALUES (?,?,?)",
                   (source, encoded, digest(previous, source, payload)))


def changes(db, after, anchor):
    if type(after) is not int or after < 0 or not isinstance(anchor, str):
        raise ValueError("Invalid notification cursor")
    stream = stream_id(db)
    row = db.execute("SELECT digest FROM notifications WHERE sequence=?", (after,)).fetchone() if after else (genesis(stream),)
    if not row or (anchor != row[0] and not (after == 0 and anchor == "")):
        raise ValueError("Remote notification continuity changed; refusing cursor reset")
    rows = db.execute("SELECT sequence,source,payload,digest FROM notifications WHERE sequence>? ORDER BY sequence LIMIT 20", (after,)).fetchall()
    records = [dict(sequence=seq, source=source, payload=json.loads(payload), digest=hash_) for seq, source, payload, hash_ in rows]
    cursor = rows[-1][0] if rows else after
    return dict(home=db.execute("SELECT home FROM identity").fetchone()[0], stream=stream,
                anchor=row[0], records=records, next_cursor=cursor,
                next_anchor=rows[-1][3] if rows else row[0],
                more=bool(db.execute("SELECT 1 FROM notifications WHERE sequence>? LIMIT 1", (cursor,)).fetchone()))


def initialize_primary(db):
    with db:
        db.execute("CREATE TABLE IF NOT EXISTS mirror_cursor (id INTEGER PRIMARY KEY CHECK(id=1), stream TEXT, cursor INTEGER, anchor TEXT)")
        db.execute("INSERT OR IGNORE INTO mirror_cursor VALUES (1,NULL,0,'')")
        db.execute("CREATE TABLE IF NOT EXISTS mirrored_events (id INTEGER PRIMARY KEY, payload TEXT NOT NULL, ack TEXT)")


def mirror(db, send):
    before = db.execute("SELECT stream,cursor,anchor FROM mirror_cursor WHERE id=1").fetchone()
    stream, cursor, anchor = before
    reply = send("changes", dict(after=cursor, anchor=anchor))
    if reply.get("ok") is not True:
        raise ValueError("Remote notification read failed; local cursor retained")
    page = reply["result"]
    route = json.loads(db.execute("SELECT binding FROM route").fetchone()[0])
    if page.get("home") != route["home"] or (stream is not None and page.get("stream") != stream):
        raise ValueError("Remote notification stream identity changed")
    stream = inbox.identifier(page.get("stream"))
    previous = anchor or genesis(stream)
    if page.get("anchor") != previous or not isinstance(page.get("records"), list) or len(page["records"]) > 20:
        raise ValueError("Invalid notification page anchor")
    records = []
    for item in page["records"]:
        if (not isinstance(item, dict) or type(item.get("sequence")) is not int or item["sequence"] != cursor + 1 or
                not isinstance(item.get("source"), str) or not isinstance(item.get("payload"), dict) or "id" in item["payload"] or
                item.get("digest") != digest(previous, item["source"], item["payload"])):
            raise ValueError("Remote notification chain mismatch")
        payload = inbox.encoded(item["payload"])
        if len(payload) > 8192:
            raise ValueError("Remote notification exceeded budget")
        cursor, previous = item["sequence"], item["digest"]
        records.append((cursor, payload))
    if page.get("next_cursor") != cursor or page.get("next_anchor") != previous or type(page.get("more")) is not bool:
        raise ValueError("Invalid notification continuation")
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT stream,cursor,anchor FROM mirror_cursor WHERE id=1").fetchone() != before:
            raise ValueError("Mirror advanced concurrently; read again")
        db.executemany("INSERT INTO mirrored_events(id,payload) VALUES (?,?)", records)
        db.execute("UPDATE mirror_cursor SET stream=?,cursor=?,anchor=? WHERE id=1", (stream, cursor, previous))
    return pending(db) | dict(more=page["more"])


def pending(db):
    rows = db.execute("SELECT id,payload FROM mirrored_events WHERE ack IS NULL ORDER BY id LIMIT 50").fetchall()
    return dict(events=[dict(id=ident, **json.loads(payload)) for ident, payload in rows])


def acknowledge(db, ids, note):
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 50 or any(type(i) is not int or i < 1 for i in ids) or
            not isinstance(note, str) or not note.strip() or len(note) > 2000):
        raise ValueError("Invalid remote event acknowledgement")
    with db:
        db.execute("BEGIN IMMEDIATE")
        for ident in ids:
            if not db.execute("SELECT 1 FROM mirrored_events WHERE id=?", (ident,)).fetchone():
                raise ValueError("Unknown mirrored event")
            db.execute("UPDATE mirrored_events SET ack=? WHERE id=? AND ack IS NULL", (note, ident))
    return dict(acknowledged=ids)
