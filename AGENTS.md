- Do not over engineer

# Developing Mate

Mate is an independent personal supervisor built on pi, Herdr and Treehouse.
This file describes how to develop the repository, not a request to start workers.

## Runtime roles

- `MATE_MODE=dev pi` starts a normal coding session here. The Mate extension does
  not register tools, commands, hooks or a watcher. Use normal coding tools directly.
- Plain `pi` loads the trusted project's supervisor extension. When Mate's
  supervisor tools are active, its injected `SUPERVISOR.md` policy takes precedence:
  delegate project work and do not bypass its tool allowlist.
- `WORKER.md` is policy for explicitly launched delegated workers only. Merely
  reading it or opening this repository does not turn you into a worker.
- Do not bootstrap Mate or launch its control plane/workers just to edit code.

## Development

Read `README.md` for behavior and checks, and `UPSTREAM.md`/`UPSTREAM.json` for the
selective Firstmate reuse ledger. Reuse existing code and keep changes minimal.
Prefer Python/Node stdlib and installed pi APIs; no launcher wrapper is needed.
Use `gh-axi` for GitHub operations. Do not push or publish without authorization.

Preserve human-only base approval/completion, exact endpoint and lease checks,
durable report/event/ack handling, per-task settings, and fail-closed uncertain
launch/closure behavior. Tests must use disposable fixtures, not real task state.
Do not stop existing supervisors/workers or modify `data/` to test a code change.
Do not change runtime code while workers are live without coordinating with the user.

Run relevant checks from the repository root:

```sh
python3 -m unittest discover -s tests -v
node tests/extension-check.mjs
python3 tests/tui-smoke.py
# Optional; requires Herdr, owns a disposable named server and Treehouse pool:
python3 tests/live-smoke.py
```

The TUI test uses a localhost fake model; it does not consume subscription quota.
Never commit credentials, `data/`, reports or runtime logs. Keep Firstmate MIT
notices and initial provenance snapshots; record local adaptations/renames in the
reuse ledger rather than claiming an upstream update that was not reviewed.
