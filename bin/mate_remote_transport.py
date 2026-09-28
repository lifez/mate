#!/usr/bin/env python3
"""Bounded SSH inbox transport. No worker launch or approval authority.

receive CONFIG: one SSH request, using an operator-owned fixed home binding.
transport ROUTE: one long-lived stdio process per remote home; never auto-retries.
SSH authentication/host verification belong to OpenSSH. On the remote account,
use an authorized_keys forced command with `restrict` pointing to receive CONFIG.
IDs bind routing; knowing an ID is not authentication. Same-account processes
remain trusted, as in the existing Mate runtime.
"""
from contextlib import closing, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time
import uuid

import mate_remote as inbox
import mate_remote_primary as primary
import mate_remote_events as events

WIRE_LIMIT = inbox.MAX_BYTES + 4096


class Uncertain(RuntimeError):
    """Transport did not prove a response; remote completion may have occurred."""


def parse(raw):
    if len(raw) > WIRE_LIMIT:
        raise ValueError("Remote message exceeds wire budget")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def invalid_number(_):
        raise ValueError("Invalid JSON number")
    value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_number)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def settings(path, receiver=False):
    """No config or journal path is accepted from a wire request."""
    with open(path, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Transport config must be account-owned and private (0600)")
        config = parse(stream.read(WIRE_LIMIT + 1))
    expected = {"home", "primary", "journal"} if receiver else {"home", "primary", "host", "outbox"}
    if (set(config) - ({"supervisor"} if receiver else set())) != expected:
        raise ValueError("Unexpected transport config fields")
    if receiver and "supervisor" in config:
        profile = config["supervisor"]
        if (not isinstance(profile, dict) or not {"provider", "model", "effort"} <= set(profile) or
                set(profile) - {"harness", "provider", "model", "effort"}):
            raise ValueError("supervisor requires provider, model and effort (optional harness)")
    inbox.identifier(config["home"])
    inbox.identifier(config["primary"])
    if receiver:
        if not isinstance(config["journal"], str) or not Path(config["journal"]).is_absolute():
            raise ValueError("Journal must be an absolute path")
    elif not isinstance(config["host"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", config["host"]):
        raise ValueError("Host must be a safe configured SSH alias")
    if not receiver and (not isinstance(config["outbox"], str) or not Path(config["outbox"]).is_absolute()):
        raise ValueError("Outbox must be an absolute path")
    return config


def validate(config, request):
    if (set(request) != {"version", "home", "primary", "call", "method", "params"} or
            type(request["version"]) is not int or request["version"] != 1 or
            request["home"] != config["home"] or request["primary"] != config["primary"]):
        raise ValueError("Remote protocol/home/primary mismatch")
    inbox.identifier(request["call"])
    params = request["params"]
    fields = {"hello": set(), "doctor": set(), "accept": {"request"}, "status": {"id"},
              "outcomes": {"after", "limit"}, "changes": {"after", "anchor"}}
    method = request["method"]
    if not isinstance(method, str) or method not in fields or not isinstance(params, dict) or set(params) != fields[method]:
        raise ValueError("Unsupported remote operation/parameters")


def receive(config, request, config_path=None):
    validate(config, request)
    response = {key: request[key] for key in ("version", "home", "primary", "call")}
    # Unknown receiver exceptions (including storage failures) do not claim refusal:
    # connection failure leaves the sender uncertain about commit/delivery.
    with closing(inbox.connect(config["journal"], config["home"], config["primary"])) as db:
        try:
            method, params = request["method"], request["params"]
            if method == "hello":
                result = dict(protocol=1, operations=["hello", "doctor", "accept", "status", "outcomes", "changes"])
            elif method == "doctor":
                import mate_remote_bootstrap as bootstrap
                if config_path is None:
                    raise ValueError("Receiver config path required")
                result = bootstrap.inspect(db, config, config_path)
            elif method == "accept":
                result = inbox.accept(db, params["request"])
                if params["request"]["body"].get("method") in ("secondmate_start", "secondmate_recover"):
                    import mate_remote_bootstrap as bootstrap
                    bootstrap.consume(db, config, config_path, params["request"])
            elif method == "status":
                result = inbox.status(db, params["id"])
            elif method == "changes":
                result = events.changes(db, **params)
            else:
                result = inbox.outcomes(db, **params)
            return dict(response, ok=True, result=result)
        except ValueError as exc:
            return dict(response, ok=False, error=str(exc))


def exchange(command, payload, timeout):
    """Bound stdout/stderr in memory and elapsed time; kill only our SSH child."""
    deadline = time.monotonic() + timeout
    # A temp input file avoids deadlock while writing a large request to a child
    # that is itself blocked writing output. No shell, credentials or payload argv.
    import tempfile
    with tempfile.TemporaryFile() as source:
        source.write(payload)
        source.seek(0)
        child = subprocess.Popen(command, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ, "stdout")
                selector.register(child.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise Uncertain("SSH timeout; inspect the same request, do not redispatch")
                    for key, _ in selector.select(min(remaining, .1)):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        buffers[key.data].extend(chunk)
                        if len(buffers[key.data]) > WIRE_LIMIT:
                            raise Uncertain("SSH output exceeded wire budget")
                code = child.wait(timeout=max(.001, deadline - time.monotonic()))
                if code != 0:
                    raise Uncertain(f"SSH exit {code}; remote completion unknown")
                return bytes(buffers["stdout"])
        except subprocess.TimeoutExpired as exc:
            raise Uncertain("SSH timeout; remote completion unknown") from exc
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdout.close()
            child.stderr.close()


def send(config, method, params, timeout=30, ssh="ssh"):
    request = dict(version=1, home=config["home"], primary=config["primary"],
                   call=str(uuid.uuid4()), method=method, params=params)
    validate(config, request)
    payload = json.dumps(request, ensure_ascii=True, allow_nan=False).encode() + b"\n"
    if len(payload) > WIRE_LIMIT:
        raise ValueError("Remote request exceeds wire budget")
    command = [ssh, "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
               "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", "-o", "SendEnv=-*",
               "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
               "--", config["host"], "mate-remote-v1"]
    try:
        reply = parse(exchange(command, payload, timeout))
        base = {"version", "home", "primary", "call", "ok"}
        if (type(reply.get("ok")) is not bool or
                type(reply.get("version")) is not int or
                any(reply.get(key) != request[key] for key in ("version", "home", "primary", "call")) or
                set(reply) != base | ({"result"} if reply["ok"] else {"error"})):
            raise ValueError("Remote reply identity/schema mismatch")
        if reply["ok"] and not isinstance(reply["result"], dict):
            raise ValueError("Invalid remote result")
        if not reply["ok"] and not isinstance(reply["error"], str):
            raise ValueError("Invalid remote error")
        if reply["ok"] and method == "accept":
            original = params["request"]
            receipt = reply["result"]
            fingerprint = hashlib.sha256(inbox.encoded(original).encode()).hexdigest()
            if (receipt.get("accepted") is not True or type(receipt.get("version")) is not int or receipt.get("version") != 1 or
                    receipt.get("home") != config["home"] or receipt.get("id") != original["id"] or
                    receipt.get("fingerprint") != fingerprint or type(receipt.get("sequence")) is not int or
                    receipt["sequence"] < 1):
                raise ValueError("Remote acceptance receipt mismatch")
        return reply
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise Uncertain("Unverified SSH response; reconcile the same request") from exc


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("receive", "transport"):
        raise ValueError("Usage: mate_remote_transport.py receive CONFIG | transport ROUTE")
    receiver = sys.argv[1] == "receive"
    config = settings(sys.argv[2], receiver)
    if receiver and os.environ.get("SSH_ORIGINAL_COMMAND", "mate-remote-v1") != "mate-remote-v1":
        raise ValueError("Unexpected SSH original command")
    with closing(primary.connect(config)) if not receiver else nullcontext() as db:
        serve_frames(config, receiver, db, sys.argv[2])


def serve_frames(config, receiver, db, config_path=None):
    while True:
        raw = sys.stdin.buffer.readline(WIRE_LIMIT + 1)
        if not raw:
            return
        if len(raw) > WIRE_LIMIT or not raw.endswith(b"\n"):
            raise ValueError("Oversized or incomplete transport frame")
        request = parse(raw)
        if receiver:
            response = receive(config, request, config_path)
        else:
            if set(request) != {"method", "params"}:
                raise ValueError("Expected method and params")
            method, params = request["method"], request["params"]
            if method in ("mirror", "events", "ack_events"):
                if method == "ack_events":
                    if not isinstance(params, dict) or set(params) != {"events", "note"}:
                        raise ValueError("ack_events requires events and note")
                    result = events.acknowledge(db, params["events"], params["note"])
                else:
                    if params != {}:
                        raise ValueError("Event reads take no parameters")
                    result = events.mirror(db, lambda m, p: send(config, m, p)) if method == "mirror" else events.pending(db)
                response = dict(home=config["home"], result=result)
            elif method == "pending":
                if params != {}:
                    raise ValueError("pending takes no parameters")
                response = dict(home=config["home"], pending=primary.unresolved(db))
            elif method == "result":
                if not isinstance(params, dict) or set(params) != {"id"}:
                    raise ValueError("result requires a request id")
                response = dict(home=config["home"], execution=primary.execution(
                    db, params["id"], lambda method, params: send(config, method, params)))
            elif method in ("accept", "delivery", "reconcile"):
                expected = {"request"} if method == "accept" else {"id"}
                if not isinstance(params, dict) or set(params) != expected:
                    raise ValueError("Invalid journal operation parameters")
                sender = lambda method, params: send(config, method, params, timeout=90 if
                    method == 'accept' and params['request']['body'].get('method') in ('secondmate_start', 'secondmate_recover') else 30)
                if method == "accept":
                    delivery = primary.stage(db, params["request"])
                    delivery = primary.deliver(db, delivery["id"], sender)
                elif method == "reconcile":
                    delivery = primary.reconcile(db, params["id"], sender)
                else:
                    delivery = primary.inspect(db, params["id"])
                response = dict(home=config["home"], delivery=delivery)
            else:
                try:
                    response = send(config, method, params)
                except Uncertain as exc:
                    response = dict(ok=False, uncertain=True, error=str(exc))
        print(json.dumps(response, ensure_ascii=True, allow_nan=False), flush=True)
        if receiver:
            return


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
