# Mate

Personal supervisor built on **pi + Herdr + Treehouse**. Mate is its own runtime,
not a Firstmate installation. Selected Firstmate code/patterns are tracked in
[UPSTREAM.md](UPSTREAM.md) and [UPSTREAM.json](UPSTREAM.json).

## Choose a mode

To **develop Mate itself**, start a fresh coding session:

```sh
cd /Users/win/mine/mate
MATE_MODE=dev pi
```

In development mode, Mate registers no supervisor tools/commands, no tool guard,
no supervisor policy injection, no Calm renderer and no watcher/control-plane child.
Normal pi coding tools remain available; you can read/edit/test the project directly.
It does not launch workers, acquire leases or touch Mate task state merely by starting.
Global pi extensions/skills still load normally; this is not a sandbox or a global
extension-disable switch.

Use a **fresh session**, not `--continue`/`--session` pointing at an old supervisor
conversation (which still contains its previous operational messages). To switch
modes, exit that pi process and launch again with the desired environment. `/reload`
keeps the process's existing mode; it does not change the inherited environment.
Use the one-command assignment above rather than exporting `MATE_MODE` globally.
Only the exact value `dev` disables Mate; unset/other values keep supervisor behavior.
If your shell already exports it, `env -u MATE_MODE pi` restores the default.

Role files:
- `AGENTS.md` — repository development instructions, loaded normally by pi.
- `SUPERVISOR.md` — runtime coordinator policy, injected only by the active Mate supervisor extension.
- `WORKER.md` — appended only when Mate explicitly launches a delegated worker.
  Opening pi or reading this file does not itself start worker mode.

Development mode does not stop an already-running supervisor/worker. Coordinate
runtime edits with the user and wait for workers to stop; tests use disposable state.
Authentication and `mate.config.json` worker defaults are unchanged. No launcher
wrapper or new dependency is required.

## Start (use Mate as supervisor)

Inside a Herdr pane:

```sh
cd /Users/win/mine/mate
test -e mate.config.json || cp mate.config.example.json mate.config.json
chmod 600 mate.config.json
# Edit mate.config.json for your local models and projects before starting.
pi
```

See [CONFIGURATION.md](CONFIGURATION.md) for all config fields, examples, validation,
startup behavior and upgrading from versions that tracked the real config.

Approve pi's project trust prompt on first launch so
`.pi/extensions/mate-supervisor.ts` loads automatically. Without project trust,
Mate's tools and auto-wake are not active.

Use `/login` → OpenAI Codex if needed, then `/model` to select your subscription
model. Existing pi authentication is reused; Mate does not store/copy credentials.
Workers use the defaults in `mate.config.json`, unless you request per-task
overrides (see below). The supervisor's model is independent.

Example request:

> Investigate the flaky login test in /absolute/path/to/repo. Propose the task
> using origin/main, but wait for my base confirmation before starting.

Mate proposes an ID and resolves the **local** ref to a SHA. It does not fetch or
assume that your local remote-tracking ref is fresh. If you want the newest remote
base, fetch it yourself before proposing the task.

Run `/mate-approve TASK_ID` and review the scope, repository, branch and full SHA
in the confirmation dialog. Declining creates no lease/pane/worker. Accepting
wakes the supervisor so it can dispatch the approved task.

The worker runs **native interactive pi TUI** in a dedicated Herdr pane and a
Treehouse-leased worktree. By default Mate creates a new tab; optionally share an
existing tab as described below. You can see Pi's tool calls, output and response as they
happen; use Pi's normal expansion/thinking visibility controls. Mate Calm affects
only the supervisor, not the worker UI.

A small explicit `bin/worker-events.ts` extension sends lifecycle events through a
separate pipe, leaving stdin/stdout attached to the terminal. After Pi fully settles
(including automatic retries, compaction and queued follow-ups), the worker requests
a graceful Pi exit. Its report is stored on disk and the supervisor wakes automatically.
The Herdr tab and regular-TUI transcript remain; `mate_continue` reopens the same
saved Pi session. This is one delegated run per attempt, not a permanently idle Pi.
Quitting before the settled event is a failure, not inferred completion.
A successful process exit becomes **review**, never automatic task completion.
Code verification/research/review must themselves be delegated.

## Commands

- `/mate-approve ID` — human-only scope/base approval, or accept/decline a pending scope addition; no implicit push/merge/deploy approval.
- `/mate-status` — show tasks/events without invoking a model.
- `/mate-complete ID [--force]` — accept a stopped `review` task (`--force` also permits `failed`), then optionally confirm closing its worker tab.
- `/mate-cancel ID` — human-only cancellation of an eligible unstarted task after read-only safety checks.
- `/mate-wake` — replay unacknowledged events if the supervisor missed one.
- `/mate-reconnect` — restart the owned control plane/watcher; task state is retained.
- `/calm [on|off|status]` — toggle quieter Mate rendering (no argument toggles).

The supervisor gets only `mate_propose`, `mate_dispatch`, `mate_status`,
`mate_continue`, `mate_extend`, `mate_ack`. The Mate extension selects this tool allowlist and
blocks other model tool calls. Normal pi global extensions/skills still load;
Mate does not disable their own startup hooks or background behavior. Only load
global extensions you trust to coexist with the supervisor.

`mate_continue` resumes a **stopped** review/failed worker in the same worktree
and pi session, for the same approved scope. It also supports the narrowly inspected
unstarted-continuation recovery described below. It does not reset or reacquire it.
Human answers to blockers go through the supervisor. Arbitrary live-pane steering
is intentionally not exposed: inspect a live blocked worker yourself in Herdr
rather than letting the supervisor blindly approve prompts.

## New worker pane in an existing tab

Ask “Open BE in a new pane in FE's tab.” After normal base approval, Mate can use:

```json
{"id":"be-task","same_tab_as":"fe-task"}
```

Pass this to `mate_dispatch`; use `"same_tab_as":"supervisor"` for Mate's own tab.
Omit the field to create a new tab as before. `supervisor` is a reserved selector,
not a task lookup. Splits open to the right without changing focus. Each new task
still gets its own Treehouse lease, worktree, branch and Pi session; sharing a tab
is only terminal layout, not shared code state or permission to broaden scope.

Mate pins the target's session/socket/workspace/tab/pane/original terminal before
acquiring resources and checks it again after startup. A task target must have an
existing original pane in the supervisor's session/socket/workspace. Missing,
moved, reused or uncertain targets are refused, never silently replaced with a new
tab. Do not move/close target panes during dispatch. Ambiguous creation remains
`attention` with its journal/receipt retained; no automatic retry or cleanup.

`mate_status` includes `tab`, `pane` and the saved `same_tab_as` selector. Duplicate
dispatch does not change placement; `mate_continue` uses the saved worker pane,
worktree and session without splitting again. Human acquisition recovery retains
and rechecks the saved target, ignoring new placement overrides.

After `/mate-complete`, tasks created this way **never offer or permit whole-tab
closure**, even if only their pane remains. Close the stopped pane manually if
needed. For tasks that originally created a tab, existing extra-pane checks still
prevent closing a tab containing related workers. No automatic pane cleanup.

Reload the supervisor with workers stopped to load the updated tools/control plane.
No task-state migration is required; old single-tab tasks retain their behavior.

## Additional scope in the same worktree

For a **stopped `review`/`failed` task**, ask Mate to add work to the existing task.
Mate records `mate_extend {id, brief}` with only the proposed addition; the approved
brief and worker settings stay unchanged. Then run `/mate-approve ID` to review the
current scope, addition, repository, pinned base, branch and existing worktree.

- Accepting appends the addition to the approved brief and saves its approval time,
  local OS account, proposal token and first eligible attempt in `scope_history`.
  `original_brief` and the original base approval remain intact. No worker starts.
- Declining discards only that pending addition. While pending, continuation and
  completion are blocked. Repeating the same pending brief is a no-op; a different
  brief replaces it. Stale tokens/attempts/base confirmations and live worker locks
  are refused. Combined approved scope is limited to 20,000 characters.
- After approval, Mate uses **`mate_continue`**, never a new dispatch. The existing
  endpoint/lease and capacity checks still apply. Worktree, dirty files, commits,
  lease, branch, base, Pi session, reports, events and saved model/effort are retained;
  there is no reset, rebase, reacquisition or startup rerun. Explicit model/effort
  overrides still work. Every subsequent worker prompt includes the updated scope.
- Approval creates a durable wake event, replayed after restart until acknowledged.
  Inspect the current task with `mate_status`; old reports remain evidence for their
  original attempts. Completion requires a new reviewed run covering the addition,
  not merely acceptance of the old report; stale scope confirmations are refused.
- `complete`, active and `attention` tasks cannot be extended. This does not authorize
  push/merge/deploy or reopen completed tasks. Scope compliance remains an instruction
  to trusted workers, not semantic enforcement of arbitrary continuation messages.

After installing the change, reload/restart the supervisor **with workers stopped**
to load both the new tool and control plane. Existing tasks need no database migration;
back up stopped state before use and do not downgrade while additions are pending.

## Cancelling an unstarted task

`/mate-cancel ID` is a human-only command; cancellation is not a model tool or an
arbitrary shell operation. It accepts only `awaiting-base`/`approved` tasks at
attempt 0 with no saved execution evidence, or the initial pre-receipt `attention`
shape. The latter is inspected under the supervisor and task locks: saved holder,
branch, artifact directory, wrapper lock, Treehouse `status --json`, Herdr task
workspace and relevant process evidence must all be readable and show no match.
A missing lease receipt is not proof of absence, so the human must attest to the
external orphan inspection in the confirmation dialog. Saved leases, worktrees,
startup/endpoint receipts, usage, follow-ups, branches, artifacts, matching
holders/tabs/processes, malformed or unavailable inspection, busy locks, and later
phase states refuse without changing the task. A saved `same_tab_as` target is only
placement intent and is retained; any created-pane receipt (`pane` for a split or
`root_pane` for a new tab), or uncertain split after acquisition, blocks cancellation.

Cancellation records `state: cancelled`, local human/time/via audit and a durable
`cancelled` event. It preserves the approved SHA/profile/scope, receipts, attempts,
reports, usage, events and acknowledgements; it does not acknowledge events, fake a
report or completion, launch/retry, clean up resources, or return a lease. Repeating
cancellation is idempotent and preserves the original audit. Cancelled tasks are
excluded from open counts and cannot dispatch, continue, extend, complete or close.

## Accepting a completed task

After reviewing the report and any required verification, run `/mate-complete ID`.
The TUI shows the task scope, base and attempt for your confirmation. Declining
changes nothing. Acceptance requires state `review` and a released worker lock;
a changed attempt, active worker or uncertain task cannot be completed.

To accept existing work despite a failed worker run, use `/mate-complete ID --force`.
The human dialog warns that the result may be incomplete and shows the saved error.
This only adds `failed` to the allowed states, not `attention` or active tasks. Under
the worker lock, Mate checks the original endpoint/terminal, shell/process readiness
and exact Treehouse lease. Missing, busy or changed resources cause refusal; no
worker is launched or interrupted. Pending/unexecuted scope additions still block.
The error, reports, usage and events remain; `completed_via: "mate-complete --force"`
and `completed_from` record the override alongside the usual time/local account.
Tab closure still requires its own separate confirmation. Reload the supervisor
with workers stopped (`/reload`) before using the new flag; no state migration is needed.

`complete` records your acceptance—not automatic proof of correctness. Status
includes `completed_at` (Unix time), `completed_by` (the local OS account running
Mate, not an authenticated GitHub/person identity), and `completed_via`.
Repeating the command preserves the original record. It is a human command, not
a model tool; no model call is needed to accept a task.

Completion itself does not acknowledge pending events, push/merge, release the
Treehouse lease or delete reports. For tasks that created their own tab, a **separate confirmation** then offers to close
the task's worker Herdr tab. Declining keeps the tab; running `/mate-complete ID`
again on an already-complete task offers closure again without rewriting acceptance.

Closure checks the worker lock, exact session/socket/workspace/tab/pane and original
terminal identity, a single-pane tab, and shell-only foreground process information.
Reused terminals, extra panes or another foreground process cause refusal. Shell-only
foreground checks cannot prove there are no background jobs or busy shell builtins;
closing loses terminal scrollback and may end shell jobs. Do not reuse or alter the
worker tab while confirming closure. Worktree/lease, reports, Pi session and cost
records remain. Missing tabs or validation errors do not undo task completion.

Closure is journaled before the external operation. A lost/ambiguous response leaves
`tab_close_state` as `closing`/`uncertain` and requires manual inspection, not automatic
retry. Successful closure records `tab_closed_at`/`tab_closed_by`; repeats are no-ops.
The footer counts only open (not `complete` or `cancelled`) tasks, across all task pages.
Completed and cancelled tasks remain visible in `/mate-status` and cannot continue or reopen in this version;
propose a new task if needed.

## Calm mode

Run `/calm on` to hide completed routine dispatch/continue, acknowledgement and
uneventful status rows, plus redundant approval notifications. Report-only wake
messages become a short **report ready (not verified)** notice. Report contents,
blockers, failures, pending calls and human approval dialogs remain visible.
Unknown results stay visible rather than being silently classified as routine.

Default is **off**. The preference is saved atomically to `$MATE_HOME/calm`
(default `data/calm`) and restored on restart. `/calm off` restores existing rows;
it does not restart workers or the watcher. Model context, raw session messages,
auto-wake delivery and acknowledgement behavior are unchanged.

This is scoped to Mate's own tool/message rendering—not Firstmate's full UI:
no thinking suppression, global tool patches or boat animation. Hidden custom
messages may leave a blank spacer. Use `/calm off` before `/export` or `/share`
if you want full rendered orchestration history; raw session content is retained.

## Worker defaults and per-task overrides

Edit `mate.config.json` in the Mate repository (not the worker's repository):

```json
{
  "worker": {
    "model": "openai-codex/gpt-5.6-luna",
    "effort": "xhigh"
  }
}
```

New dispatches read this file each time; no restart is needed after config edits.
Precedence per field is **task override → worker config → supervisor setting**.
Use `provider/model-id` to keep the default independent of the supervisor's provider.
A bare ID uses the supervisor's provider. Use `{}` to inherit both supervisor settings.
Missing/unreadable/invalid config fails dispatch instead of silently falling back.
Unknown models or unsupported configured effort also fail before worker launch;
when overriding to a model without `xhigh`, explicitly override effort too.

This file is local-only and Git-ignored; share `mate.config.example.json` instead.
Keep credentials out of both files; authentication still comes from pi. Config changes do not alter existing tasks or continuation:
those retain their saved profiles unless explicitly overridden.

Ask naturally, for example: “Delegate this with model `<model-id>` and effort `high`.”
The supervisor passes optional `model` and `effort` to `mate_dispatch`:

```json
{"id":"login-fix","model":"openai-codex/<model-id>","effort":"high"}
```

Replace `<model-id>` with an exact ID from pi's `/model` picker. A bare ID uses the
supervisor's provider; `provider/model-id` explicitly selects another provider.
Selecting another provider may require separate credentials/billing—it does not
make that provider part of your OpenAI subscription.

- Omitted values use `mate.config.json` worker defaults first, then the supervisor's
  current settings on initial dispatch.
- Effort maps to pi's `--thinking`: `off`, `minimal`, `low`, `medium`, `high`,
  `xhigh`, `max`. Explicit unsupported levels or unknown models fail before launch.
- Configured or per-task effort is validated strictly. Only effort inherited from
  the supervisor is clamped using pi's supported-level rules; `mate_status` shows
  the resolved, saved profile.
- `mate_continue` accepts the same overrides. Otherwise it keeps the task's saved
  model/effort, even if you change the supervisor's settings or restart Mate. A bare
  model ID on continuation uses the task's provider. It reuses the worktree/session.
- Overrides do not change the supervisor's model or the task's approved base/scope.
  Repeating dispatch on an already-launched ID does not change its saved profile.
- Legacy tasks with no saved effort retain their old CLI behavior while running;
  their next continuation starts from `off`, clamped to the selected model's support.

Custom providers must be available to both the supervisor's model registry (for
profile validation) and the worker; worker-only provider registration is insufficient.

## Per-project base branch and startup

Add `projects` beside `worker` in your local `mate.config.json` (the example ships
with `projects: {}`; no project commands run until you explicitly configure them):

```json
{
  "projects": {
    "migrate-admin": {
      "repo": "/Users/win/mine/migrate-admin",
      "base_branch": "origin/migration",
      "startup": {
        "command": ["python3", "/absolute/path/to/mate/examples/copy-env.py", "/absolute/path/to/source.env"],
        "timeout_seconds": 30
      }
    }
  }
}
```

Merge this into the existing file; keep your `worker` defaults. The project name
is a label. `repo` must be an absolute local Git repository root (including `~/...`);
symlinks are resolved before exact matching. Relative paths, Git URLs, glob matching
and duplicate resolved repo mappings are not supported. No implicit project discovery.

`base_branch` and `startup` are independently optional:

- With `base_branch`, omit `base` from `mate_propose`; a conflicting explicit base
  is refused. Use a short local branch (`migration`) or remote-tracking branch
  (`origin/migration`), not `refs/...`, a tag, SHA or symbolic ref such as `origin/HEAD`.
  Missing or ambiguous branches fail. Mate never fetches: fetch yourself for fresh refs.
- Proposal resolves the branch to a SHA. Human approval is still mandatory and pins
  that SHA even if the branch subsequently moves. Workers write to a separate
  `mate/...` task branch, never automatically onto the configured base branch.
- Changing/removing/adding the project's required base policy before dispatch
  requires a new proposal and approval. No automatic rebase, merge or repinning.
  Without `base_branch`, the task must still supply an explicit base as before.
- Startup is read/validated on initial dispatch, saved with the task, and run after
  lease/identity checks and switching to the approved task branch, before creating
  a Herdr tab or starting Pi. This includes pooled worktrees reused by Treehouse.
  Invalid project config fails before acquiring resources.
- Command is a nonempty argv array, not implicit shell syntax. Use
  `["/bin/sh", "/absolute/path/to/setup.sh"]` for a shell script. Arguments do not
  expand `~`, `$VAR` or globs; scripts may read the provided environment themselves.
  `cwd` is the task worktree. `MATE_REPO`, `MATE_WORKTREE` and `MATE_TASK_ID` are set;
  other environment is inherited. stdin is closed (no interactive prompts).
- Timeout defaults to 30 seconds; allowed range is **1–120 integer seconds**.
  Setup must exit, not start background services. Dispatch RPC allows up to 15 minutes
  for acquisition, bounded setup and identity checks (other RPCs retain 3 minutes).
  The serial control plane pauses polling while setup runs; durable outcomes remain.
- Startup must preserve the approved HEAD, task branch and repository identity.
  Setup may create files; ensure env files are ignored by Git before approval.
- Nonzero exit, timeout or uncertain interruption prevents worker launch. The task
  becomes `attention` (after reconciliation for a hard crash); lease/worktree and
  journal remain. No automatic rerun, rollback, reset or resource return.
  Timeout/error kills only the startup's owned process group. Detached descendants
  or a hard-killed supervisor can leave processes behind: inspect manually.
- Duplicate dispatch and `mate_continue` **never rerun startup**. Continuation keeps
  the same worktree/session/base even after config changes. Status with an `id`
  includes saved startup config, state, timestamps, PID and exit code when available.
  stdout/stderr go to private `data/<id>/startup.log`, not tool results/model context.

Example: [`examples/copy-env.py`](examples/copy-env.py) copies the supplied source
into `.env.local` in the worktree with permission `0600`. It checks Git ignore rules
and refuses an existing destination (including symlinks), rather than overwriting
pooled env files blindly. Inspect any existing file before replacing it yourself.
The source path and destination filename can be adapted in your own trusted script.

Config and scripts are **trusted local code, not sandboxed**. Keep secrets out of
command arguments/config and never print them. Mate saves command paths/arguments,
not a snapshot/hash of external script contents; edits to those scripts affect future
initial dispatches. Treehouse's own setup hooks may also run before Mate's startup.
After installing this runtime change, reload/restart the supervisor with workers
stopped; subsequent project config edits are read without restarting.

## Per-task usage and estimated cost

Worker usage is persisted in the task journal after each finalized assistant message,
including tool-calling turns and errors with reported usage. Streaming updates are
not counted repeatedly. Each attempt has separate counters; continuation adds a new
attempt rather than counting the saved session history again.

- `/mate-status` or `mate_status` includes `usage_total`: input/output tokens,
  cache-read/cache-write tokens and `estimated_cost_usd` across tracked attempts.
- `mate_status` with an `id` also includes the task's `usage` map keyed by attempt,
  and `attempt_usage` for the selected `attempt` (current attempt by default).
- Estimates sum Pi's reported `usage.cost.total` in USD. Mate does not maintain a
  separate price table. **For an OpenAI subscription this is not an additional bill
  or a measurement of subscription quota remaining.** A reported zero may reflect
  missing/zero catalog pricing; it does not prove the request was free.
- `estimated_cost_usd: null` means no valid cost has been reported. Compare
  `token_reported_messages` and `cost_reported_messages` with `messages` to identify
  partial data. Totals with `untracked_attempts` are also incomplete.
- Tracking starts with newly launched worker attempts after this update. Existing
  attempts are not automatically backfilled; unknown historical cost is not zero.
- Scope is worker assistant-message usage, not supervisor calls, nested tool model
  calls, or compaction/retries that emit no finalized assistant usage. In-flight
  requests are not reflected until their final message arrives. This is a usage
  estimate, not an audited billing ledger. Existing raw logs remain available.

## Safety and limits

- At most **two active workers**. They share subscription quota with the supervisor.
- Base approval pins a commit; a moving branch does not change the approved base.
- Treehouse lease ID/holder, Git common directory and exact Herdr endpoint IDs are checked.
- Task branches are created without force/reset; pooled branches/commits are preserved.
- No automatic push, PR publication, merge, deploy, worktree return or pane cleanup.
  The only tab-close action is the separately confirmed option after `/mate-complete`.
  Delivery mode is deliberately not selected yet. Leases/worktrees remain until you
  inspect and explicitly release them; they count against Treehouse's pool capacity.
- Worker reports are untrusted evidence, never instructions or human approval.
- Workers are **trusted local processes, not sandboxed**. Git worktrees isolate
  changes, not filesystem/network permissions. The worker no-push/no-deploy rules
  are instructions, not an OS-enforced security boundary. Approve only repositories
  and briefs you trust; Treehouse may run repository setup hooks.
- Workers load global and worktree-project skills/extensions using normal Pi discovery
  and configured resource filters, plus the explicit Mate event bridge. Worker launches
  use `--approve` to trust project resources for that run (including project settings,
  packages and executable extensions); approve only repositories/resources you trust.
  The Pi child uses `MATE_MODE=dev` to suppress Mate supervisor registration only;
  `WORKER.md` still defines its delegated role. Prompt template discovery remains off.
  Changes apply on the next launch/continuation, not to already-running workers.
  Do not switch/fork sessions in a running delegated worker; send follow-up scope
  through the supervisor.
- A hard crash during lease/pane creation is **attention**, not an invitation to
  retry. The journal and any lease receipt are retained. No automatic re-acquire,
  process killing, or destructive rollback attempts to guess what happened.
  Any `attention` task blocks new dispatch/continuation until inspected and repaired;
  only the exact unstarted continuation being inspected by `mate_continue` is exempt
  after passing recovery checks. Other attention tasks and the two-worker cap still block.
- A hard-killed worker wrapper might leave its child running. Missing worker locks
  therefore become **attention**, not a resumable failure. Inspect the recorded
  pane, lease and processes manually; the runtime has no general force-repair or
  force-cleanup command. Only the narrow initial-preflight, unstarted-continuation
  and pre-receipt acquisition recoveries below are supported.

## Auto-wake and recovery

The pi extension owns a Python control-plane child and its shutdown lifecycle.
The child holds a kernel `flock`; a second supervisor for the same home refuses
ownership. SQLite stores tasks and events transactionally; workers publish reports
before enqueueing an outcome.

Herdr native status subscriptions provide blocked-state hints. Durable worker
outcomes are checked every **2 seconds** without model calls. Native stream failure
falls back to this polling, with an explicit alert after five bounded retries.
If the control plane dies, the extension retries three times then displays a
watcher-down alarm. `/mate-reconnect` resets that retry budget.

A worker that has produced no new pi output for **15 minutes** triggers one stalled
alert per attempt, without being interrupted or declared failed. Set
`MATE_STALE_SECONDS` (minimum 30) to tune this for long local checks.

Events are delivered as follow-ups, so they do not interrupt an active conversation.
Approval wakes explicitly require current-state inspection and continuation or a
concrete blocker; report wakes require reading and summarizing the report instead
of repeating an old launching update. Events remain until `mate_ack` records handling.
After Pi fully settles (including retries, compaction and queued messages), delivered
but unacknowledged events receive **one corrective follow-up per session generation**.
If still unhandled, a separate `UNHANDLED` footer names them until acknowledgement;
there is no infinite model retry loop. This checks acknowledgement, not the semantic
correctness of a model's summary or handling note. Corrections never launch workers
directly: the supervisor must recheck current scope/attempt and obey launch refusals. `/new`, `/resume`, `/reload` or restarting Mate replays pending
events. Delivery is **at least once**, not exactly once; dispatch IDs and state gates
prevent automatic duplicate acquisition. No model calls are made just to wait.

Closing the supervisor leaves workers running. Reopen with the **same MATE_HOME**
to recover reports and task state. Existing live workers are not restarted. Ambiguous
or missing workers are surfaced for inspection, not silently adopted/relaunched.

### Recover an initial launch refused before submission

After the human returns the original pane to its idle shell, `mate_continue` also
accepts the narrow initial attempt-1 `attention` case with the exact saved
"Worker pane is not an idle shell ... No keys sent." refusal and matching durable
`launch-uncertain` event. New records distinguish `launch_stage: preflight-refused`
from uncertain submission; the exact legacy refusal is supported without migration.
A timeout, missing worker, crashed Pi or ambiguous pane command is **not** eligible.

Under the worker lock it checks the original endpoint/terminal, exact lease/holder,
isolated worktree/common directory, branch and unchanged approved HEAD; startup
must have succeeded if configured. No session, usage, reports, worker events,
unknown artifacts or possible task/worktree processes may remain. Only the idle
shell may appear in Treehouse's process inventory. Pending scope, other attention
tasks and worker capacity still block. Dirty startup files are preserved.

Success retains the entire prior record in `launch_recoveries`, emits
`launch-recovered` on the old attempt and starts one new attempt with the approved
brief plus the recovery instructions. Approval, settings, lease, worktree and pane
are retained; no startup rerun, acquisition, reset or cleanup. This is the first Pi
session, not a resume of a nonexistent one.

The call observes for up to 10 seconds for a durable `worker-started` receipt,
written only after Pi process creation succeeds. `launch_confirmation: started`
proves process creation, **not** successful work or continued liveness; inspect the
returned current state and later report. `unconfirmed` means no start was proven:
no resubmission or fabricated outcome occurs, and normal reconciliation remains
responsible for a missing wrapper. Further uncertain attempts remain fail-closed.
Reload the supervisor with workers stopped before using this path. No manual
`data/` edits or Firstmate commands are required.

### Continue after repairing a busy worker pane

Keep the worker pane dedicated to Mate. Before every dispatch/continuation launch,
Mate checks the original terminal identity, foreground process, shell children
and shell process group (including background/stopped jobs). A shared TTY alone
is not ownership: detached prompt helpers can retain it and are not rejected on
that basis. Recovery still checks detached task/session and worktree processes. If Vite/Pi/another job is using it, no command or Ctrl-C is
sent. An ordinary continuation rejected at preflight leaves its attempt/state intact.
Launch commands explicitly enter the saved worktree root, even if the shell was
left in a subdirectory. No worktree contents are changed by that `cd`.

For an existing `attention: No worker lock after 60s` **continuation**, return the
original pane to its shell yourself, then tell the supervisor it is fixed and ask
to continue the same task. No supervisor shutdown, manual database edit or new base
approval is required. `mate_continue` performs fail-closed checks under the worker
lock before journaling the next attempt:

- Exact socket/session/workspace/tab/pane/original terminal and Treehouse lease/holder.
- Same isolated worktree, repository common directory, task branch and approved
  commit ancestry; dirty edits and later task commits are retained, not reset.
- Existing readable Pi JSONL session with the worktree identity, unchanged since
  a prior report; no usage entry, event log, stderr or report for the missing attempt.
  A recorded running/startup/acquisition crash is not eligible.
- Shell-only foreground, no shell children or other shell-group processes, Treehouse's worktree process
  inventory contains only that shell, and no OS command references the task/session.
  Missing/malformed/unavailable inspection data is a refusal, not evidence of absence.
- No pending scope addition, other attention task or exhausted worker capacity.

Success retains the failed launch in `launch_recoveries` and emits a durable
`launch-recovered` event on the old attempt, then starts a **new attempt** using the
same session, worktree, lease, approval/scope and saved settings (unless explicitly
overridden). Reports, usage, events and acknowledgements remain; no fake worker
report is created. No startup rerun, acquisition, release, cleanup or auto-completion.
A `launching` response is submission, not proof the new worker acquired its lock.
The supervisor must check subsequent status/outcomes normally.

This is explicit continuation after human pane repair, not a polling auto-relaunch
or a generic orphan repair. Legacy missing continuations can qualify using their
saved usage/artifacts; absent current usage is meaningful because the wrapper
journals it **before** starting Pi. Any current-attempt execution evidence refuses
recovery even if the process appears gone. OS snapshots cannot prove a shell is
waiting at a prompt (a shell builtin may be busy), reserve it against human input,
or identify arbitrary detached processes that changed identity/cwd. Keep the pane
untouched during launch; ambiguity or a crashed worker still needs manual inspection.
Run dev servers separately; recovery conservatively refuses other worktree processes.

After installing this change with workers stopped, use `/reload` in the supervisor
to load both the updated tool policy and control plane. `/mate-reconnect` alone does
not reload tool descriptions. Existing task data needs no manual migration.

### Recover a failed Treehouse acquisition (human-only)

After manually inspecting/clearing Treehouse setup processes, worktrees and leases,
Herdr panes and orphan workers, stop the supervisor normally and ensure workers are
stopped. Keep the same `MATE_HOME`. Back up the stopped state before recovery.
In your own terminal, from the Mate repository:

```sh
python3 bin/mate.py recover-acquire migrate-admin-application-form
```

This is an interactive local command, **not a supervisor/model tool or RPC**. It
holds the supervisor and task worker locks, displays the saved task and requires
an exact confirmation containing ID, attempt and full approved SHA. Declining
leaves the task unchanged. Do not pipe confirmation or run it through an agent.

The command only accepts `attention` **before any lease receipt was saved**. It
refuses saved checkout/startup/endpoint/worker evidence, execution artifacts, an
existing task branch, changed base policy, unavailable approved commit, malformed
or unavailable Treehouse status, or any row still recording the task's holder.
A missing receipt can hide external side effects: the human inspection is required;
Treehouse status and an unlocked wrapper alone cannot prove no orphan exists.
Later-phase failures remain `attention`; this is not a generic recovery bypass.

Success returns the same task to `approved`, keeping its scope, repo, pinned SHA,
branch and original approval timestamp. The complete prior record is retained in
`recoveries`, with local user/time; pending events are not acknowledged or deleted.
No cleanup, Git reset, lease acquisition, startup or worker launch occurs here.
Restart the supervisor with the same home and request `mate_dispatch` for this ID
when ready. The next dispatch uses a new attempt/holder, reuses the task directory
without deleting evidence, and retains the saved model/effort and not-yet-run
startup (dispatch profile overrides are ignored for recovered tasks). Base policy
is checked again; moving the base branch never repins the approved SHA. Another
uncertain acquisition requires another human recovery, never an automatic retry.

## State

Default: `mate/data/` (gitignored, private permissions). Override with an absolute
`MATE_HOME` when launching; keep it stable between supervisor sessions.

```text
data/mate.sqlite3                  task journal + events + handling notes
data/supervisor.lock               kernel-held ownership lock
data/calm                          persistent Calm presentation preference
data/<id>/session.jsonl            worker pi session (reused on continuation)
data/<id>/events-<attempt>.jsonl    selected Pi lifecycle/message/tool events
data/<id>/stderr-<attempt>.log      provider/CLI errors
data/<id>/report-<attempt>.txt      retained worker outcome
data/<id>/run.lock                 live wrapper ownership
data/<id>/startup.log              private project setup output (never auto-delivered)
```

`mate_status` returns 50 tasks per page (`task_offset`), 50 pending events per batch,
and report text in 12k-character pages (`offset`/`next_offset`). Full logs remain on
disk for manual inspection. Never commit `data/` or publish its reports/logs blindly.
Do not update code or change the home path while workers are live. Back up state
with workers stopped before changing schema; there is no schema migration system yet.

## Checks

Requires macOS/Linux, Python 3.10+, Git, pi, Herdr and Treehouse supporting
`get --lease --json` / `status --json`. Tested tool versions: pi 0.85.1, Herdr 0.8.0,
Treehouse 2.1.1. No extra runtime package installation is needed.

```sh
python3 -m unittest discover -s tests -v
node tests/extension-check.mjs
# Real Pi TUI in a PTY; localhost fake model, no subscription/API credentials.
python3 tests/tui-smoke.py
# Opt-in: run inside Herdr; owns a unique test server and disposable repo/pool.
python3 tests/live-smoke.py
```

The Node check uses the globally installed pi package's jiti/typebox loader and a
mock Pi UI, including Calm toggling, persistence, fail-open visibility and unchanged
message payloads. It is not a visual terminal test. The live smoke uses real Herdr/Treehouse but **fake pi**, so it consumes
no subscription quota. It deletes only its own successful fixture and stops only
its own named test server; failed fixtures are retained for inspection.

The TUI smoke runs the real Python worker and real Pi in a pseudo-terminal against
a localhost fake model. It checks native tool/report rendering, separate event
transport, settled shutdown, durable report and pending wake event. It uses a fixture
lease checker; the Herdr/Treehouse integration is covered by the separate live smoke.

These checks do **not** prove a real OpenAI model follows the workflow. The first
real task should be a small read-only investigation with explicit base approval.
A real subscription-backed end-to-end run has not yet been performed.
