# Grace period for a newly created Herdr worker pane

## Status

Implemented after the human confirmed workers had stopped and process inspection found no live worker wrapper. Runtime reload remains a human action and must be done while workers stay stopped.

## Problem

On initial dispatch, `bin/mate.py:dispatch()` creates a Herdr pane/tab/workspace and immediately calls `launch_worker()`. `launch_worker()` calls `ready_pane()` once before submitting `pane run`.

A newly created terminal can still be starting its shell or running short-lived shell initialization children at that instant. Mate then records the task as `attention` with `launch_stage: preflight-refused`, even though the pane becomes an idle shell moments later. No worker command was sent, so the human must currently ask Mate to recover through `mate_continue`.

This is a pane-readiness race after dispatch, not a failure of `/mate-approve` or the durable approval wake.

## Desired behavior

- Give only a newly created worker pane a short bounded window to reach the existing `ready_pane()` safety condition.
- Submit the worker command exactly once after readiness is proven.
- If readiness is not proven by the deadline, preserve today's fail-closed `attention` state and initial-preflight recovery path.
- Do not weaken terminal identity, lease, worktree, foreground-process, shell-child, or process-group checks.
- Do not add automatic launch retries after `pane run` has been submitted.

## Implementation

1. Add a small helper in `bin/mate.py` that retries `ready_pane(task)` until a fixed monotonic deadline (proposed: 5 seconds, sleeping 100 ms between checks).
2. Retry only the two existing transient readiness refusals:
   - `Worker pane is not an idle shell...`
   - `Worker pane has background/stopped processes...`
3. Propagate terminal/endpoint identity errors and all unknown errors immediately. They are not shell-startup evidence.
4. Use the helper only on the initial dispatch path after the new endpoint receipt and terminal identity have been saved. Ordinary continuation and recovery keep the current single preflight check because their panes are expected to have been returned to an idle shell by the human.
5. Keep `LaunchPreflightRefused` as the final exception when the deadline expires, so the existing journal, event, and `mate_continue` recovery semantics remain unchanged.

No configuration option is needed unless real measurements show one fixed bounded grace period is insufficient.

## Tests

Extend `tests/test_mate.py` with the smallest regression cases:

1. **Transient shell startup:** mocked `pane process-info` reports a startup child for the first checks, then an idle shell. Assert one lease acquisition, one worker submission, and no `attention`/`launch-uncertain` result.
2. **Persistent non-idle pane:** readiness never settles before the mocked deadline. Assert no `pane run`, state remains `attention`, `launch_stage` remains `preflight-refused`, and the existing recovery remains eligible.
3. **Identity mismatch:** assert it is not retried or converted into a benign readiness wait.
4. Keep the existing initial-preflight recovery test passing to prove backward compatibility.

Run from the repository root:

```sh
python3 -m unittest discover -s tests -v
node tests/extension-check.mjs
python3 tests/tui-smoke.py
```

The Python unit test is the direct regression. The Node and TUI checks guard extension/worker integration; no real task state or subscription-backed model is used.

Implemented validation:

- `python3 -m unittest discover -s tests -v` — 47 passed
- `node tests/extension-check.mjs` — passed
- `python3 tests/tui-smoke.py` — passed

## Expected files

- `bin/mate.py`
- `tests/test_mate.py`
- `README.md` only if the user-facing recovery/readiness description needs one short clarification

No schema, config, extension wake logic, SUPERVISOR policy, or dependency change. No UPSTREAM ledger change unless upstream code is actually consulted or reused during implementation.

## Safety and rollout

1. Wait for all `bin/mate.py worker ...` processes to stop; do not interrupt them.
2. Recheck `git status` and preserve the unrelated in-progress workspace/presentation changes.
3. Implement and run disposable tests without touching `data/`.
4. With workers still stopped, have the human `/reload` or restart the supervisor to load the change.
5. Use a small normal task as the first live canary. Success means a transiently starting shell launches once; a persistently busy/uncertain pane still refuses without receiving keys.

## Out of scope

- Dispatching directly from `/mate-approve`
- Retrying approval wakes or changing model prompts
- Retrying an uncertain worker submission
- Sending Ctrl-C or cleaning up a busy pane automatically
- Increasing RPC timeouts or adding a configurable readiness subsystem
