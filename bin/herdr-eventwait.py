#!/usr/bin/env python3
"""Herdr event transport adapted from Firstmate; see UPSTREAM.md R01 and
third_party/firstmate/LICENSE. Emits @subscribed, then TSV pane/workspace/status/agent.
Exit 0: bounded wait ended; 2: arguments/connect; 3: handshake; 4: stream failure.
Transport only: idle/done are NOT proof of task completion.
"""
import json
import math
import socket
import sys
import time

MAX_FRAME = 1024 * 1024


def _read_line(sock, buf, deadline):
    while b"\n" not in buf:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, buf, "timeout"
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            return None, buf, "timeout"
        except OSError:
            return None, buf, "error"
        if not chunk:
            return None, buf, "closed"
        buf += chunk
        if len(buf) > MAX_FRAME:
            return None, b"", "error"
    line, buf = buf.split(b"\n", 1)
    return line, buf, "line"


def _clean(value):
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def main(argv):
    if len(argv) < 4:
        return 2
    try:
        timeout = float(argv[2])
    except ValueError:
        return 2
    if not math.isfinite(timeout) or timeout <= 0:
        return 2
    panes = set(argv[3:])
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        try:
            sock.settimeout(5)
            sock.connect(argv[1])
            request = {"id": "mate-eventwait", "method": "events.subscribe", "params": {
                "subscriptions": [{"type": "pane.agent_status_changed", "pane_id": p} for p in panes]}}
            sock.sendall((json.dumps(request) + "\n").encode())
        except OSError:
            return 2
        deadline = time.monotonic() + timeout
        line, buf, _ = _read_line(sock, b"", min(deadline, time.monotonic() + 5))
        try:
            ack = json.loads(line) if line else None
        except ValueError:
            return 3
        if not isinstance(ack, dict) or not isinstance(ack.get("result"), dict) or ack["result"].get("type") != "subscription_started":
            return 3
        print("@subscribed", flush=True)
        while True:
            line, buf, outcome = _read_line(sock, buf, deadline)
            if line is None:
                return 0 if outcome == "timeout" else 4
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict) or message.get("event") != "pane.agent_status_changed":
                continue
            data = message.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("pane_id"), str) or data["pane_id"] not in panes:
                continue
            print("\t".join(_clean(data.get(k) or "") for k in ("pane_id", "workspace_id", "agent_status", "agent")), flush=True)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except (BrokenPipeError, KeyboardInterrupt):
        sys.exit(0)
