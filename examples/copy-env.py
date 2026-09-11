#!/usr/bin/env python3
"""Trusted startup example: python3 /path/to/copy-env.py /path/to/source.env.

Mate sets cwd to the worktree. Refuse existing destinations, including symlinks.
Add .env.local to the repository's .gitignore before approving the task base.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: copy-env.py /absolute/path/to/source.env")
    source = Path(sys.argv[1]).expanduser()
    if not source.is_absolute() or not source.is_file():
        raise SystemExit("Source must be an existing absolute file path")
    destination = ".env.local"
    if subprocess.run(["git", "check-ignore", "-q", "--", destination]).returncode:
        raise SystemExit("Destination must be untracked and Git-ignored")
    os.umask(0o077)
    with source.open("rb") as src, open(destination, "xb") as dst:
        shutil.copyfileobj(src, dst)


if __name__ == "__main__":
    main()
