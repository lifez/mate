# Firstmate reference: ignored operational wakes

Inspected local `/Users/win/mine/firstmate` at `869ae905779c4c366a45759be8676406a1aae85c`. No fetch, execution, runtime loading or modification of that checkout; its existing untracked `.pi/pi-openai-fast-mode/` was left untouched. This is a selective source investigation, not a full upstream review or evidence that the newest remote version was inspected. No background-agent facility was available; inspection was performed directly.

## Closest recorded incident: same symptom, different workflow

Commit `5466394` (`fix(pi): deliver captain outcomes as deterministic transcript entries (#3312)`) records a 2026-08-31 incident: seven delivered decision outcomes received empty assistant replies and two received unrelated previous replies. The old cursor advanced when enqueued and accepted whatever assistant reply followed. This is directly analogous to Mate's repeated old approval answer, but it concerns supervision outcomes, not Mate's `/mate-approve` command.

Source: `git -C /Users/win/mine/firstmate show -s --format=full 5466394`; implementation at `.pi/extensions/fm-branch-supervision.ts:152-180,1005-1057`.

## What Firstmate changed

1. **Display is deterministic, processing remains explicit.** Persist the exact outcome as a sequence-keyed visible transcript entry. Track a separate processed marker; only `fm_branch_processed` for a presented sequence advances it. Empty/unrelated replies do not count. This guarantees visibility and tracks acknowledgement, not semantic correctness or guaranteed worker launch.
2. **Bound automatic turns, retain the request on later human turns.** Send a typed `fm-branch-process` custom message with `triggerTurn: true, deliverAs: "followUp"`. After two triggered presentations of the same sequence set, use `deliverAs: "nextTurn"` without opening another turn; keep doing that on subsequent human turns until acknowledged. Session replacement resets the triggered budget. A pending request is not widened/duplicated into its own running turn.
3. **Separate transport acceptance from consumption.** The watcher uses `await pi.sendUserMessage(content, { deliverAs: "followUp" })`, with an explicit operational envelope. Acceptance lets its successor-watcher pipeline continue. Consumption is tracked separately to decide replay across session replacement: `before_agent_start` for idle sends, user `message_start` with exact text for streaming sends.

Sources in the inspected checkout:
- `.pi/extensions/fm-branch-supervision.ts:152-180,1005-1057` — typed processing request, bounded triggered attempts and nextTurn delivery.
- `docs/pi-supervision-branch.md`, “Two-stage noise filter” — visible entry, separate read/processed markers, exact-sequence acknowledgement and replay contract.
- `.pi/extensions/fm-primary-pi-watch.ts:14-23,515-553,731-802,1095-1101` — delivery/consumption distinction, user-message send and consumption hooks.
- `.pi/extensions/lib/fm-operational-input.ts:5-18,80-95` — shared operational encoder; do not import its shell machinery merely to copy an envelope concept.

## Relevant upstream bug fix and tests

`77ee3c8` (`fix(pi): settle watcher delivery on Pi accepting the follow-up (#3513)`) fixed a stalled successor chain: a follow-up queued during streaming does not raise `before_agent_start`. Waiting for that event blocked later actionable closes. Acceptance now releases delivery; consumption only controls replacement replay. This is NOT proof of Mate's root cause: Mate does not currently wait for before_agent_start in its polling delivery.

Evidence inspected, not tests run in this investigation:
- `tests/fm-pi-watch-extension.test.sh:2281+` — replacement replay for streaming follow-ups.
- `tests/fm-pi-branch-live-e2e.test.sh:795-993` — credential-free real installed SDK probe, holding a local fake provider stream open; asserts no before_agent_start for streaming follow-up, actual user message_start, successor continuity and idle consumption. Useful fixture design, not real-model compliance evidence.
- `tests/fm-pi-branch-extension.test.sh:1240-1342` — real store scripts + mocked lifecycle: empty response, repeated unrelated response, two triggered turns then nextTurn, no duplicate pending copy and session replay.

## Important comparison with Mate

- Mate already keeps durable events until explicit `mate_ack` and sends one corrective follow-up, then an UNHANDLED footer. Do not add Firstmate's second processed store: Mate's ack already fills that role.
- Firstmate's continued `nextTurn` attachment after the triggered budget is a concrete behavior Mate lacks; useful to evaluate, but still not guaranteed execution.
- Watcher `sendUserMessage` is a real difference from Mate's `sendMessage(customType="mate-wake")`. However Firstmate's branch processing path ALSO uses custom `sendMessage` follow-ups. Therefore “custom messages are broken; switch APIs and it is fixed” is unsupported.
- The pinned original reuse baseline `4930d2caaba8a14b13b754cefc4bd22d77d993d0` already used watcher `sendUserMessage` (line 251 in that version). This is an original adaptation difference, not a new upstream API migration.
- None of the inspected paths guarantees that approval directly starts a worker without the model. They improve delivery, visibility, explicit processing acknowledgement and recovery.

## Selective recommendation for the Mate plan

1. Keep actual outgoing-request capture as the first check. Compare current custom wake and documented user follow-up using disposable identical idle/busy fixtures; verify ordering, model-visible content and relevant extension transforms rather than guessing from session JSONL.
2. Adapt the real-SDK streaming fixture idea; do not copy the arm-child/branch/replacement coordinator stack. Mate already has durable SQLite replay and generation ownership.
3. If needed, attach still-unacknowledged events to the next human turn after the bounded correction, using supported Pi APIs, no extra automatic model turns and no duplicate pending request. Recheck current task scope/attempt before any action. This supplements the footer; it does not replace backend safety or ack.
4. Preserve visible factual approval/action state independently from model prose. Do not claim a launch from an acknowledgement or display entry alone.
5. Read installed Pi API/conversion docs and implement a failing regression before adopting a delivery API change. No direct approve-and-start behavior without its separate human-facing contract decision.

No runtime files, real task state or reuse baseline changed. Record a selective port in UPSTREAM.md/UPSTREAM.json only when a reviewed change is actually adopted; these observations do not move the global baseline.
