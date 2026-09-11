# Fix approval → continuation handoff

Status: implemented as a smaller shared approval/report wake fix; awaiting human reload.
No live workers were found before editing. Real task state and supervisor process were not changed.

Implementation differs from the original proposal below: the corrective mechanism
checks durable unacknowledged event IDs for all event types, not a second copy of
task eligibility logic in TypeScript. It never launches anything itself. Current
scope/token/attempt checks and refusal handling are explicit supervisor instructions;
existing backend gates remain authoritative. One correction per event/generation,
then an UNHANDLED footer. Acknowledgement is not semantic proof of correct handling.
Tests passed: 33 Python tests, extension check (mixed approval/two reports, ignored
wake, busy/queued suppression, bounded reminder, restart and ack), existing native
worker TUI smoke. The new supervisor correction lifecycle is mocked, not verified
with a real subscription model or a new supervisor TUI fixture.

## Confirmed failure

Read-only investigation of the supervisor transcript and SQLite journal showed:
- Both latest scope approvals were persisted and delivered as events 38/39.
- The supervisor responded by repeating its request for approval, without calling status or continue.
- Only after the human reported no activity did it call mate_continue for both tasks and acknowledge both events.
- Therefore this incident was not a lost approval or failed worker launch.

Current code:
- `.pi/extensions/mate-supervisor.ts`: `poll()` gives all event kinds the same report-oriented instruction and marks them delivered once per generation.
- `agent_end` only sets a generic footer; it does not detect an approval left unhandled.
- `bin/mate.py`: `review_scope()` already persists approval and its event atomically, with a note directing mate_continue.
- `tests/extension-check.mjs` verifies approval persistence but does not simulate a model ignoring the approval wake.

The generic prompt is a suspected contributor, not a proven explanation of model internals.

## Minimal implementation

### 1. Explicit approval action in the existing durable wake

Change the shared event-delivery path, not a second sendMessage in the approval command.
For a scope-approved event, explicitly instruct the supervisor to:
1. Inspect the current task, approved scope/history and attempt with mate_status.
2. If the addition has not run and the task is eligible, call mate_continue in the same turn, preserving its saved settings and scope.
3. Do not ask for the same approval again; never dispatch an existing task.
4. If continuation is refused, report the concrete blocker and required next step, not a promise that the worker started.
5. Acknowledge only after continuation submission or explicit blocker handling.

Keep report instructions for report events. Mixed batches retain both sets of instructions.
Worker report text remains untrusted; classify using event kind, and verify approval against current task state.
Update SUPERVISOR.md with the same contract.

### 2. One bounded corrective wake if the model ignores approval

Reuse the existing poll/status and session-generation lifecycle. No new daemon, DB schema or scheduler.
After an approval wake's model turn finishes, check durable pending approval events and current task state:
- Only an unacknowledged, still-current approved addition with no eligible continuation attempt qualifies.
- Check the scope token/history and first_attempt, not merely review/failed state: old events must not resume a task again.
- Queue at most one corrective follow-up per approval event per session generation.
- If still unresolved after that follow-up, show a persistent actionable UI warning naming the task; do not generate endless model turns.
- If a continuation was attempted and refused, show the blocker rather than automatically retrying the launch.
- Ignore events already handled/acknowledged, obsolete scope, complete tasks, advanced attempts, or an active launch/worker.
- On unavailable status, surface monitoring uncertainty; never infer permission or readiness.
- Stop/reload/session changes invalidate stale callbacks. Existing restart replay remains intact.

Before implementation, read installed Pi lifecycle/sendMessage docs fully and confirm the correct post-turn hook and queued-follow-up semantics. Do not assume agent_end means all queued work/retries have settled. Keep the corrective send out of a recursive hook loop.

This is orchestration recovery, not automatic worker launch. All actual continuations still pass through mate_continue and existing endpoint/lease/lock/capacity checks.

### 3. Regression checks in existing tests

Use disposable fixtures only. Extend tests/extension-check.mjs to cover:
- Scope approval wake contains explicit status → continue-or-blocker → ack instructions; report-only wake retains report behavior.
- Model replies without taking action: exactly one corrective wake; repeated empty turns/polls do not create an infinite loop.
- Both tasks approved close together: neither is lost or launched twice; mixed report/approval batches remain correct.
- Successful continuation/advanced attempt or acknowledged event suppresses correction.
- Refused continuation surfaces a blocker without blind retry.
- Old token, superseding pending scope, complete task and live worker cannot trigger a duplicate continuation.
- Restart replay and stale-generation callback protection.
- Decline/stale human confirmation still create no launch.

Mocks prove the event/recovery contract, not that a real model will obey the prompt. If needed, add a scripted fake-model supervisor TUI case for ignored wake → bounded correction, using the existing local-only test pattern.

## Files / scope

Expected runtime changes: `.pi/extensions/mate-supervisor.ts`, `SUPERVISOR.md`.
Tests: `tests/extension-check.mjs`; TUI fixture only if necessary to validate lifecycle ordering.
Docs: README.md and UPSTREAM.md/UPSTREAM.json local adaptation ledger; preserve provenance snapshots.
No planned bin/mate.py change, data migration, new dependency, automatic dispatch, force recovery, resource cleanup or real task-state edits.

## Apply and validate

1. Human confirms workers stopped; verify no Mate worker is live. Do not kill one to proceed.
2. Recheck git diff and relevant implementation/callers; implement in small steps.
3. Run from repo root:
   - python3 -m unittest discover -s tests -v
   - node tests/extension-check.mjs
   - python3 tests/tui-smoke.py
4. Document remaining limits: bounded correction can still end in an explicit alert, never guaranteed model compliance or forced launch.
5. Human reloads/restarts supervisor with workers stopped. Do not replay acknowledged historical events or continue these two tasks as a test.

Acceptance: a newly approved scope leads to an actual continuation attempt or a visible concrete blocker; an ignored wake is detected and receives one corrective turn, then an explicit alert rather than silent inactivity. Existing human-only approvals and fail-closed launch safety remain unchanged.
