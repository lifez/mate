#!/usr/bin/env python3
"""Explicit user-local setup; no credential copying, package installation or updates."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sys
import uuid

import mate
import mate_remote as inbox
import mate_remote_primary as primary


def exclusive(path, content, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    remote = sub.add_parser("init-home")
    remote.add_argument("path")
    remote.add_argument("--primary", required=True)
    remote.add_argument("--harness", choices=sorted(mate.HARNESSES), default="pi")
    remote.add_argument("--provider", help="Pi provider; Claude uses anthropic")
    remote.add_argument("--model", required=True)
    remote.add_argument("--effort", default="off")
    remote.add_argument("--install-entrypoint", action="store_true")
    local = sub.add_parser("init-route")
    local.add_argument("name")
    local.add_argument("--host", required=True)
    local.add_argument("--home", required=True)
    local.add_argument("--primary", required=True)
    args = parser.parse_args()
    inbox.identifier(args.primary)
    if args.action == "init-home":
        if args.harness == "pi" and not args.provider:
            raise ValueError("--provider is required for a Pi secondmate")
        profile = mate.worker_profile(dict(harness=args.harness, provider=args.provider or "anthropic", model=args.model,
                                           effort=args.effort if args.effort != "off" or args.harness == "pi" else "high"))
        home = Path(args.path).expanduser()
        if not home.is_absolute():
            raise ValueError("Remote home must be absolute")
        home.mkdir(parents=True, mode=0o700, exist_ok=False)
        config = dict(home=str(uuid.uuid4()), primary=args.primary, journal=str(home / "inbox.sqlite3"), supervisor=profile)
        inbox.create(config["journal"], config["home"], config["primary"])
        exclusive(home / "remote.json", json.dumps(config, indent=2) + "\n")
        command = shlex.join([sys.executable, str(Path(__file__).resolve().with_name("mate_remote_transport.py")),
                              "receive", str(home / "remote.json")])
        if args.install_entrypoint:
            entrypoint = Path.home() / ".local/bin/mate-remote-v1"
            entrypoint.parent.mkdir(parents=True, exist_ok=True)
            exclusive(entrypoint, "#!/bin/sh\nexec " + command + "\n", 0o700)
        print(json.dumps(dict(config=config, receiver_command=command,
                              next="Configure installed mate.config.json and remote Pi authentication; add the primary route, then use /mate-remote NAME doctor and start."), indent=2))
    else:
        inbox.identifier(args.home)
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", args.name) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", args.host):
            raise ValueError("Invalid route name/SSH alias")
        folder = mate.HOME / "remotes"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / (args.name + ".json")
        if path.exists() or path.is_symlink():
            raise ValueError("Route already exists; no overwrite or retargeting")
        config = dict(host=args.host, home=args.home, primary=args.primary,
                      outbox=str(folder / (args.name + ".sqlite3")))
        primary.create(config)
        exclusive(path, json.dumps(config, indent=2) + "\n")
        print(json.dumps(dict(route=str(path), next=f"/mate-remote {args.name} doctor")))


if __name__ == "__main__":
    os.umask(0o077)
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
