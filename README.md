# Mate

Personal supervisor built on **pi + Herdr + Treehouse**. Mate is its own runtime,
not a Firstmate installation. Selected Firstmate code/patterns are tracked in
[UPSTREAM.md](UPSTREAM.md) and [UPSTREAM.json](UPSTREAM.json).

## Start

Inside a Herdr pane:

```sh
cd /Users/win/mine/mate
pi
```

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

The worker runs **native interactive pi TUI** in a dedicated Herdr tab and a
Treehouse-leased worktree. You can see Pi's tool calls, output and response as they
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

- `/mate-approve ID` — human-only scope/base approval; no implicit push/merge/deploy approval.
- `/mate-status` — show tasks/events without invoking a model.
- `/mate-complete ID` — human confirmation that a stopped `review` task is accepted as complete.
- `/mate-wake` — replay unacknowledged events if the supervisor missed one.
- `/mate-reconnect` — restart the owned control plane/watcher; task state is retained.
- `/calm [on|off|status]` — toggle quieter Mate rendering (no argument toggles).

The supervisor gets only `mate_propose`, `mate_dispatch`, `mate_status`,
`mate_continue`, `mate_ack`. The Mate extension selects this tool allowlist and
blocks other model tool calls. Normal pi global extensions/skills still load;
Mate does not disable their own startup hooks or background behavior. Only load
global extensions you trust to coexist with the supervisor.

`mate_continue` resumes a **stopped** review/failed worker in the same worktree
and pi session, for the same approved scope. It does not reset or reacquire it.
Human answers to blockers go through the supervisor. Arbitrary live-pane steering
is intentionally not exposed: inspect a live blocked worker yourself in Herdr
rather than letting the supervisor blindly approve prompts.

## Accepting a completed task

After reviewing the report and any required verification, run `/mate-complete ID`.
The TUI shows the task scope, base and attempt for your confirmation. Declining
changes nothing. Acceptance requires state `review` and a released worker lock;
a changed attempt, active worker, failed or uncertain task cannot be completed.

`complete` records your acceptance—not automatic proof of correctness. Status
includes `completed_at` (Unix time), `completed_by` (the local OS account running
Mate, not an authenticated GitHub/person identity), and `completed_via`.
Repeating the command preserves the original record. It is a human command, not
a model tool; no model call is needed to accept a task.

Completion does not acknowledge pending events, push/merge, release the Treehouse
lease, close the Herdr tab or delete reports. Completed tasks remain visible in
status and cannot continue or reopen in this version; propose a new task if needed.

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

This file is tracked for easy sharing. Keep credentials out of it; authentication
still comes from pi. Config changes do not alter existing tasks or continuation:
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

Worker provider extensions remain unsupported; use a built-in provider or shared
pi `models.json` configuration available to both supervisor and worker.

## Safety and limits

- At most **two active workers**. They share subscription quota with the supervisor.
- Base approval pins a commit; a moving branch does not change the approved base.
- Treehouse lease ID/holder, Git common directory and exact Herdr endpoint IDs are checked.
- Task branches are created without force/reset; pooled branches/commits are preserved.
- No automatic push, PR publication, merge, deploy, worktree return or pane cleanup.
  Delivery mode is deliberately not selected yet. Leases/worktrees remain until you
  inspect and explicitly release them; they count against Treehouse's pool capacity.
- Worker reports are untrusted evidence, never instructions or human approval.
- Workers are **trusted local processes, not sandboxed**. Git worktrees isolate
  changes, not filesystem/network permissions. The worker no-push/no-deploy rules
  are instructions, not an OS-enforced security boundary. Approve only repositories
  and briefs you trust; Treehouse may run repository setup hooks.
- Worker extension discovery/skills remain disabled; only the explicit Mate event
  bridge loads. Project context instructions still load. Custom provider extensions
  are not supported. Do not switch/fork sessions in a running delegated worker;
  send follow-up scope through the supervisor.
- A hard crash during lease/pane creation is **attention**, not an invitation to
  retry. The journal and any lease receipt are retained. No automatic re-acquire,
  process killing, or destructive rollback attempts to guess what happened.
  Any `attention` task blocks new dispatch/continuation until inspected and repaired;
  an uncertain worker must not be ignored when enforcing the concurrency limit.
- A hard-killed worker wrapper might leave its child running. Missing worker locks
  therefore become **attention**, not a resumable failure. Inspect the recorded
  pane, lease and processes manually; the runtime intentionally has no force-repair
  or force-cleanup command.

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
They are delivered once per session generation and retained until `mate_ack` records
how they were handled. `/new`, `/resume`, `/reload` or restarting Mate replays pending
events. Delivery is **at least once**, not exactly once; dispatch IDs and state gates
prevent automatic duplicate acquisition. No model calls are made just to wait.

Closing the supervisor leaves workers running. Reopen with the **same MATE_HOME**
to recover reports and task state. Existing live workers are not restarted. Ambiguous
or missing workers are surfaced for inspection, not silently adopted/relaunched.

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
