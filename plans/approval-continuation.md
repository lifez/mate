# Fix approval → continuation handoff

## Current fix: native user-message transport

Implemented with human authorization after confirming no live worker wrappers. Wakes and watcher alarms now use Firstmate watcher's `sendUserMessage(..., {deliverAs: "followUp"})`. After the bounded reminder, an input hook re-reads unacknowledged IDs and appends a clearly labeled runtime attachment to native human input; it preserves text/images and ignores extension-origin input. Removed the custom nextTurn queue. No global compaction package/config edits, no new approval, no real event ack/launch/replay performed by this development session.

Regression: the optional installed-compaction check loads its actual extension factory and Codex request hook with a synthetic matching checkpoint. Custom messages are dropped as a negative control; native wakes and human-input attachments survive request replacement and session reconstruction without duplicate history. The real Pi TUI smoke separately verifies native user message_end delivery and outgoing localhost wire input in idle/busy modes. 38 Python tests and the extension/worker TUI checks also pass. No real Codex request was made; model compliance remains unverified. Human reload is still required to activate this fix; pending durable events will replay normally then.

## Previous pacing patch (custom transport superseded)

Historical status: implemented as a selective Firstmate processing-pacing adaptation; was loaded for the third incident. Human confirmed workers stopped and process inspection found no worker wrappers before runtime edits. Existing supervisor and real task state were not changed.

Implemented: reuse existing durable ack, one automatic correction then nextTurn attachment on later human prompts, no duplicate pending copy; atomically persist base-approved events through the shared wake path, no legacy backfill. Kept custom sendMessage (matching Firstmate's processing path), not a speculative sendUserMessage switch. No deterministic transcript-entry store, direct launch, second agent or schema change.

Validation: 38 Python tests, extension check, existing worker TUI smoke and new supervisor TUI smoke in idle/busy modes passed. The new test captures actual localhost OpenAI-completions requests, scripts ignored replies followed by real status/ack tool execution, and checks no worker launch. Running the same new idle regression against the pre-fix extension fails specifically because the next human request has no fresh operational copy. Initial negative-run cleanup hung; fixture cleanup was bounded to killing only its own Pi child, then the negative regression produced the expected assertion. No production processes were stopped.

Limits: this proves local transport/pacing, not Codex provider-specific serialization or real-model compliance; the fix keeps pending events actionable but cannot guarantee the model continues a task. A previously queued nextTurn copy can outlive ack; current-state rechecks remain mandatory. Real-provider canary requires separate human authorization. The design and investigation steps below are retained as context, not a claim every optional branch was implemented.

## Evidence and correction to the previous plan

The previous explicit wake + bounded reminder patch was loaded and exercised; this is NOT simply a missing reload.

Read-only inspection of the supervisor session and SQLite journal confirmed:
- Scope approvals were persisted as events 42/43.
- Both explicit `mate-wake` messages reached the session, followed by the bounded CORRECTION containing both events.
- After each wake, the assistant repeated the old request for approval instead of calling `mate_status` or `mate_continue`.
- Only after the human complained did it inspect status, continue both tasks, and acknowledge events 42/43.
- At inspection, Approve Accounts attempt 11 and Application Form attempt 14 were running with live wrapper processes.

This proves the current reminder did not ensure action. It does not prove what the provider actually received or why the model repeated its answer. A custom message appearing in session JSONL is not proof of correct model-wire delivery.

## Third incident: remote-compaction integration drops custom wakes

Read-only inspection after reload confirmed events 46/47 are approved and unacknowledged, both tasks remain stopped in review, and the new wake text plus corrective message appear in the supervisor session. The assistant again repeats its old approval request without tools. The patch IS loaded.

The global package `git:github.com/algal/pi-openai-server-compaction` is configured. This supervisor session contains a remote compaction checkpoint at 2026-09-11T06:47:23.519Z with model key `openai-codex:openai-codex-responses:gpt-6-astra`, matching the latest assistant model.

Source evidence in `/Users/win/.pi/agent/git/github.com/algal/pi-openai-server-compaction/src/`:
- `index.ts:126-144,294-342`: remote history is extended through message_end conversion, then the Codex before_provider_request path replaces the request with that history.
- `remote-compaction.ts:358-423`: messageToResponseItems supports user/assistant/toolResult but returns [] for custom messages.
- `remote-compaction.ts:1030-1052`: reconstruction skips custom_message session entries.
- `openai.ts:155-166`: applyRemoteHistoryPayloadPatch replaces input outright with explicitHistory, discarding fresh normal Pi input.

A synthetic, no-network direct invocation of the installed package's exported converter and payload patch reproduced custom mate-wake → [] and removal of the fresh event from input; native user conversion retains it. Production wire capture was not performed. This is a concrete integration defect consistent with the incident, not evidence that the model saw and ignored the wake.

Previous local TUI tests intentionally excluded global extensions and used OpenAI-completions, so they did not exercise this Codex + remote-compaction path. Their pacing assertions remain valid but are insufficient for this deployment.

Decision after investigation: the human chose a Mate-only compatibility fix matching Firstmate's watcher transport, not a global package change. The current implementation above covers both initial/corrective wakes and later human input. It does not overwrite provider-native compacted history, disable compaction globally or use events 46/47 as test input.

## Goal

After human approval, the supervisor consumes a fresh operational instruction and attempts the appropriate action, or reports a concrete blocker. It must not silently repeat its old approval request. Preserve human-only approval/completion and all existing launch safety checks.

Do not fix this by adding stronger wording or more reminder turns alone.

## Firstmate reference findings

See [firstmate-approval-wake-findings.md](firstmate-approval-wake-findings.md). The inspected local Firstmate checkout records the same empty/repeated-old-answer symptom in #3312. Its solution separates visible delivery from sequence-bound processing acknowledgement and, after two triggered presentations, carries pending requests on later human turns via `nextTurn`. Mate already has durable ack; reuse it rather than add a processed store.

Firstmate's watcher uses `sendUserMessage`, including at Mate's original reuse baseline, but its branch processing path still uses custom `sendMessage`. Treat this as a comparison to test, not proof that changing API fixes Mate. #3513 supplies a useful real-SDK idle/streaming fixture: streaming follow-ups are consumed through user `message_start`, not `before_agent_start`. Evaluate next-human-turn attachment after Mate's bounded correction without more automatic turns, duplicate pending messages or direct worker launch. No upstream runtime code has been imported.

## 1. Reproduce and inspect the actual model input first

Read the installed Pi extension/lifecycle, message conversion and provider APIs/docs in full, following relevant references before implementation. Trace the current command → durable event → poll → `sendMessage` follow-up → session conversion → outgoing provider request path, including idle and busy delivery.

Focus on:
- `.pi/extensions/mate-supervisor.ts`: `poll`, `mate-approve`, `agent_settled`, activation/generation and tool profile resolution.
- Pi conversion of custom messages into model messages and the active provider's role/item serialization; inspect relevant global message-transform extensions without changing them.
- Initial base approval uses a separate `mate-approved` send; scope approval uses durable `mate-wake`. Cover both instead of fixing only one caller.
- Does each outgoing request actually contain the latest approval instruction after the previous assistant answer, with a role/content shape the provider consumes? Are follow-ups queued, dropped, reordered or transformed?

Add a disposable local fake-model supervisor TUI reproduction that captures outgoing requests, not just mocked `sendMessage` calls. Reproduce approval while idle, while answering, and two approvals arriving close together. Use synthetic tasks/content and no real credentials, task DB, worktrees or workers. Do not send real session history to a diagnostic endpoint.

Deliverable: a failing regression at the actual broken boundary, or an explicit statement that transport is correct and the remaining issue is model compliance. Do not claim a provider bug from transcript repetition alone.

## 2. Fix the smallest confirmed boundary

Preferred scope is a shared operational wake delivery fix using supported Pi APIs, preserving the existing backend as the only launch authority.

If custom-message conversion/delivery is defective or unsuitable:
- Correct that conversion or use the documented model-visible follow-up mechanism proven by the captured request test.
- Keep operational instruction delivery separate from presentation only if necessary; do not inject two actionable copies of the same event.
- Label the message as a runtime event referring to already-persisted human approval, not a new human request or permission grant. Report text remains untrusted evidence.
- Route initial base approval and scope approval through the same reliable delivery path where feasible. Inspect backend base-approval persistence first; add a durable base event only if missing, reusing the existing event journal rather than adding another queue/schema.
- Retain at-least-once replay, exact event IDs, generation ownership, bounded correction and acknowledgement semantics.

If outgoing transport is already correct:
- Do not pretend another prompt change guarantees execution.
- Retain a visible actionable failure state after the bounded reminder; distinguish approval accepted, continuation not submitted, refused, and uncertain launch.
- Present a separate follow-up decision for deterministic approve-and-start behavior before implementing it. It changes today's scope dialog promise (“No worker launch”), needs explicit human-facing consent, exact scope/attempt idempotency, saved profile/placement handling and crash/replay design. It is not an authorized shortcut in this repair.

All continuation still checks current approved token/scope/attempt, retained profile, worker lock, exact endpoint/terminal, lease and capacity. Never use an event's old task snapshot as authority to launch. Do not reconstruct eligibility independently in TypeScript or auto-recover an uncertain launch.

## 3. Regression and acceptance

Extend existing test files rather than introduce a framework or new runtime dependency.

Required checks:
- Initial base approval and scope approval appear as the intended fresh instruction in captured outgoing model input.
- Idle/busy/queued delivery and two near-simultaneous approvals lose neither event; mixed report/approval batches handle both.
- A scripted fake-model response calls status then dispatch/continue as appropriate; verify tool execution, not merely matching text in a wake.
- An ignored wake receives at most one correction per event/generation, followed by visible unresolved state, with no infinite retry or false “started” message.
- Decline, stale token/attempt, superseded scope, complete/cancelled tasks, already-running attempts and replay cannot cause duplicate launch.
- Refused/busy/uncertain launches remain blocked and report their actual cause; no blind retry, force recovery or cleanup.
- Ack follows actual submission or explicit blocker handling, never delivery alone. Old report evidence is not mistaken for execution of new scope.
- Restart/reload replays pending events; stale callbacks cannot act in a new generation. Saved continuation settings and initial dispatch placement/overrides are preserved.

A fake model proves transport and orchestration behavior, not real-model compliance. After the patch is loaded, the human may authorize a small read-only real-provider canary. Existing approvals/events 42/43 must not be replayed as test input or used to launch these tasks again.

## 4. Apply safely

1. Wait for workers to finish and coordinate runtime editing with the human; recheck live processes and git status. Do not kill or interrupt them.
2. Implement the failing fixture and minimal boundary fix. Keep real `data/` unchanged.
3. Run from repository root:
   - `python3 -m unittest discover -s tests -v`
   - `node tests/extension-check.mjs`
   - `python3 tests/tui-smoke.py`
   - Run the new disposable supervisor transport case through its documented entry point if separate.
4. Update README and SUPERVISOR policy only for actual behavior changes; record local adaptations in UPSTREAM.md/UPSTREAM.json without changing upstream provenance snapshots.
5. Human reloads/restarts the supervisor with workers stopped. Record local test evidence and any remaining real-provider uncertainty here.

Expected files: `.pi/extensions/mate-supervisor.ts`, `tests/extension-check.mjs`, existing TUI test machinery (inspect before deciding whether to extend or add a supervisor fixture), README and reuse ledger. `bin/mate.py` only if inspection confirms a missing durable base-approval event or another backend root cause; no speculative scheduler/schema changes.

Out of scope: automatic launch from polling, bypassing model tool/approval boundaries, infinite prompts, switching the user's model as a purported fix, stopping live work, editing task state, cleanup, publishing or replaying acknowledged production events.
