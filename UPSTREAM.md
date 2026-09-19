# Mate — selective Firstmate reuse map

## Decision

**Mate เป็นระบบของเราเอง ไม่ใช่ Firstmate ทั้งชุด + customization layer**

`mate/` เป็นเจ้าของ workflow, instructions, state และ runtime ของตัวเอง เลือก
copy/adapt เฉพาะส่วนจาก Firstmate และรับ update โดย port เป็น change set ไม่ใช่
pull Firstmate ทับ Mate ไม่มี runtime import/source จาก `../firstmate` ไม่มี submodule
และไม่แชร์ Firstmate operational state

## Current implementation

- Persistent Pi workers, reference-only to local Firstmate `b430bf50` (no fetch),
  selected `fm-spawn.sh` Pi `agent_start`/`agent_settled` busy/idle bridge only.
  Mate now publishes each settled report/event without exiting Pi. Native human
  follow-ups and `mate_continue` use the same Pi with per-round attempts/usage;
  current scope, endpoint/lease, capacity and completion gates remain enforced.
  Mate-owned round admission/reply pipe, separate resident/round locks and private
  generation/attempt-fenced control socket; no Firstmate shell implementation copied.
  Human acceptance gracefully exits idle Pi before separately confirmed cleanup.
  Missing ownership/lost continuation/shutdown replies remain fail-closed; no retry
  or forced stop. Existing stopped sessions still reopen normally. Original source
  baselines/hashes/notices are unchanged. Tests use disposable state and localhost
  fake models, not real task data or provider quota. Stop all workers before updating;
  no schema migration, but do not downgrade while resident workers are open.

- Mate-owned `/skill:ahoy`, adapted from Firstmate's Ahoy skill at local commit
  `a27646c` (read in full; no fetch). It keeps the visible-history recap, cross-boundary
  unanswered-decision inventory, one-at-a-time impact-ordered guidance and first-real-
  human-message fallback. Mate replaces Firstmate session-start/Bearings dependencies
  with one bounded `mate_status` fallback and explicit runtime-message exclusions.
  Normal recaps call no tools; neither branch mutates tasks/events or bypasses human
  approval/completion gates. No shell helper, fresh report read or fleet action added.

- Mate-owned natural-language dispatch profiles inside existing `mate.config.json`:
  `dispatch.rules[].when/use/why` selects a concrete Pi model/effort before approval,
  with human override → matching rule → worker default precedence and an explicit-field
  dispatch backstop. Selected Firstmate `crew-dispatch` schema/judgment/backstop concepts
  at local `a27646c` reference-only; no separate config file, harness switching, profile
  arrays, quota routing or shell parser copied. Python/TypeScript validate fail closed.

- Mate-owned compact status: `/mate-status` filters completed/cancelled task rows
  before pagination while retaining total/open counts and pending events;
  `/mate-list` projects the same open snapshot to ID/state lines only. The
  `mate_status` model tool keeps historical rows and ID inspection. Default task
  inspection includes current approved and pending scope, latest scope token/first-
  attempt, settings/error/startup and usage totals; full historical journal is opt-in
  with `history: true`. Pinned subsequent report pages omit repeated task/event/usage
  payloads. Human dialogs retain full reads and existing approval/completion gates.
  No journal/schema mutation.
- R06 reference-only brief authoring contract: selected `bin/fm-brief.sh` at local
  Firstmate `a27646c` (no fetch), specifically Captain's intent vs Firstmate spec and
  explicit deliverable/evidence sections. Mate uses five concise sections in its
  existing brief string, adding exclusions and stop conditions; preserves human
  restrictions, legacy briefs and existing approval. No scaffold code copied,
  delivery modes imported or component baseline advanced. Semantic compliance is
  instructional, not claimed from fake-model tests. Disposable checks cover compact
  status, full-history reads, pinned Unicode report pagination, unchanged brief
  delivery and human approval/completion gates.

- R03/R05 reference-only narrow initial-launch recovery: inspected local Firstmate
  `869ae905779c4c366a45759be8676406a1aae85c`, selected `fm-control.sh` relaunch
  checkpoint/journal/postcondition and `fm-spawn.sh` agent-free endpoint gates;
  related changes #4120 (`3e817d3`) and #4172 (`4768e98`) informed boundaries.
  Mate-owned implementation reuses `mate_continue`, exact endpoint/lease/worker
  locks and SQLite history. Only the initial attempt-1 idle-shell preflight refusal
  with no execution evidence qualifies; retain resources/startup output and verify
  a durable post-Popen receipt with a bounded observation, never resubmit on timeout.
  No shell code, lifecycle stop/interrupt verbs, metadata stack or worktree cleanup
  imported. Original component baselines/hashes/notices remain unchanged; not a
  full Firstmate audit. Disposable Python and native Pi fake-model checks only.

- Mate-owned `/bearings lavish`, referencing Firstmate's Bearings skill, bounded
  snapshot projection and board builder at local commit `a27646c` (read in full;
  no fetch). Selected concepts: one current bounded state source, four stable sections,
  a rebuilt private stable board path and explicit separation between presentation and
  authority. Mate renders its existing 50-row SQLite status snapshot directly into a
  self-contained read-only board and opens it with installed `lavish-axi`; buttons only
  copy existing human commands. No backlog/secondmate/PR discovery, hold aging, model
  composition, answer binding, polling listener, merge/dispatch action or Firstmate
  HTML/shell code imported. Existing human approval/completion/cancellation and event
  acknowledgement gates are unchanged. Tests use a disposable home and fake Lavish CLI.

- Mate-owned `/stow` and `mate_memory`, referencing Firstmate's internal and public
  stow skills at local commit `869ae905779c4c366a45759be8676406a1aae85c` (both read in
  full; no fetch). Selected concepts: inspect-before-update, whole-memory curation,
  preserve open next steps, cold recovery and honest reset-safety receipt. New Mate
  implementation uses a bounded current note and append-only SQLite `memories`
  revisions, exact-revision replacement, fixed per-session startup snapshot, and
  the existing supervisor-owned RPC/tool boundary. No task/event/approval mutation,
  automatic reset, filesystem write tool, secondmate cascade, tier/decay engine or
  external routing. Byte limit is not provider token accounting; curation and the
  completeness verdict are model judgments. Sources: `.agents/skills/stow/SKILL.md`
  blob `ed32e020fee0f183b5f3a551fd437780291312ef`, `skills/stow/SKILL.md` blob
  `b45ebe8fd9b59161e6f67fabb4d19cd6e53665d0`. Reference-only; no source code copied
  and no existing provenance baseline advanced. Additive table only; reload with
  workers stopped, retain the table on rollback. Disposable tests cover size,
  conflicts, idempotence, cold history, failed-save preservation, command refusal,
  new-session loading, stable prefix, no task/event changes and dev-mode no-op.

- R02 native wake transport follow-up: use Firstmate watcher's `sendUserMessage`
  (selected `sendWake` at local commit `869ae905779c4c366a45759be8676406a1aae85c`;
  this API choice also existed at the original pinned baseline). The configured
  pi-openai-server-compaction Codex remote-history hook drops custom messages,
  explaining why the previous custom nextTurn adaptation did not solve the live
  handoff. Mate-owned input hook now attaches still-pending reminded events to
  native human input, re-reading SQLite, preserving text/images and skipping
  extension-origin input; no custom nextTurn queue or extra automatic turns.
  Watcher-down alarms also use native user transport. New wakes stay visible in
  Calm; old custom renderers remain for history. No global package/config edits.
  Tests: real local Pi TUI idle/busy native message_end + wire capture, and optional
  actual installed compaction hooks with synthetic Codex checkpoint/reconstruction
  (custom-message negative control drops, native input survives). No real provider
  call or claim of guaranteed model compliance. Baseline/notices retained.

- R02 selective follow-up adaptation: Firstmate #3312 bounded processing requests
  → `nextTurn` after Mate's one corrective wake, then one pending batch per later
  human prompt. Reuse Mate SQLite event/ack rather than import a processed-marker
  store, branch agent or arm-child pipeline. Keep the custom-message transport
  used by Firstmate's processing path; real local fake-model supervisor TUI captures
  model-visible wake input and checks idle/streaming delivery, no third automatic
  wake, status/ack execution and preserved unlaunched task. Base approval now
  atomically emits a durable event through the same path (no legacy backfill).
  Selected source: local Firstmate `869ae905779c4c366a45759be8676406a1aae85c`,
  `.pi/extensions/fm-branch-supervision.ts` processing constants and
  `presentUnprocessedOutcomes` only, blob `682f0a087ab0f7942a0c04ce9986f1350a8ac20a`;
  related test reference `tests/fm-pi-branch-extension.test.sh` processing-turn case,
  blob `812a55eade30625c65dc45f97f59c5d17d7e1b52`.
  Decision: adapt pacing, defer deterministic transcript entries/ack binding,
  do not import the branch/store stack. No fetch or whole-component review;
  original baseline and initial hashes below remain unchanged. Existing MIT
  attribution retained. See `plans/firstmate-approval-wake-findings.md` for limits.

- Mate-owned wake handling correction: explicit approval/report actions in the
  shared durable wake, one unacknowledged-event reminder after `agent_settled` per
  session generation, then a separate persistent UNHANDLED footer. No direct
  launch/retry, schema change or upstream baseline change; acknowledgement is not
  semantic verification. Regression uses the disposable real control plane with
  mocked Pi lifecycle, including two reports plus approval and bounded correction.

- Mate-owned `/mate-complete ID --force`: human acceptance also permits stopped
  failed tasks; reuse lock, original pane/process and lease checks, preserve scope
  gates and failed-run evidence, record override/source state and keep tab closure
  separately confirmed. A manually closed pane is accepted only from structured
  `pane_not_found` plus the exact lease's empty process inventory; record that proof
  and skip redundant tab closure. No upstream import or provenance baseline change.

- Mate-owned optional `mate_dispatch same_tab_as`: split right/no-focus in an
  existing task or supervisor tab, with independent worktree/branch/lease/session.
  Pin/recheck exact target and original terminal; retain native split receipt,
  support old tab receipts, no fallback/retry/move or shared-tab closure. Saved
  placement survives continuation/acquisition recovery. Verified Herdr 0.8.0 split
  CLI/receipt using a disposable named server. No upstream import/baseline change.
  Checks: 32 Python tests, extension check, localhost fake-model TUI and real
  Herdr/Treehouse + fake Pi smoke passed (supervisor/task split, continuation and
  shared-tab closure refusal). Live fixture uses /bin/sh without personal prompt
  plugins; initial inherited-shell run correctly refused startup background jobs.

- Mate-owned unstarted-continuation recovery through `mate_continue`: original
  pane/shell/process inventory, exact lease/worktree/session and no-execution gates;
  preserve failed-attempt history/events and start a new attempt without reset,
  startup rerun or approval changes. Shared launch preflight prevents sending into
  a busy foreground app and explicitly enters the saved worktree root. No generic
  crash bypass, cleanup, new upstream import or provenance baseline change.

- Mate-owned stopped-terminal rebind after a host reboot: `mate_continue` retains the
  immutable creation receipt and accepts a changed terminal ID only for `review`/`failed`
  tasks after exact endpoint, lease, worktree/repository/branch/base ancestry, cwd,
  idle-shell, sole Treehouse process and orphan-process checks under the worker lock;
  append old/new identity audit before one continuation launch. Selected Firstmate
  `3e817d3` missing-endpoint recovery and `e0d269e` process-level stale-agent classifier
  as reference-only safety boundaries; Mate resumes its saved Pi session rather than
  importing Firstmate's fresh-agent relaunch/projection stack. Generic attention,
  cleanup, reacquisition and uncertain/live-pane adoption remain refused.
  Local follow-up: use parent/process-group ownership, not shared TTY, for shell
  background-job checks; detached prompt helpers are not worker jobs. Keep separate
  task/session and worktree orphan checks; no process-name whitelist.

- Mate-owned replacement of unapproved task scope: repeating `mate_propose` with the
  same ID/repository/base updates only an `awaiting-base` brief while retaining its
  pinned SHA and branch. `/mate-approve` submits the exact displayed brief so a stale
  dialog fails closed. Approved tasks remain immutable through proposal. No upstream
  import or provenance baseline change.

- Mate-owned scope additions for stopped review/failed tasks: `mate_extend` proposes,
  `/mate-approve` accepts/discards the exact pending token/attempt/base under a worker
  lock, and `mate_continue` reuses the existing worktree/session. Retain original
  approval and addition history; durable approval wake, no startup/reset/reacquire,
  and completion waits for a reviewed run of the added scope. No upstream import
  or provenance baseline change; complete tasks remain terminal.

- Mate-owned offline `recover-acquire ID`: human exact ID/attempt/SHA confirmation,
  supervisor/worker locks and fail-closed pre-receipt-only recovery. Preserve original
  approval, profile/startup and full failed-task history; retry uses a new attempt and
  holder without deleting evidence. No model RPC, cleanup or upstream import.

- Mate-owned footer correction: count non-complete/non-cancelled tasks across the full snapshot,
  retaining total/history and pending events. No upstream import or baseline change.

- Local config packaging: track `mate.config.example.json` instead of personal
  `mate.config.json`; keep local config ignored, document setup/schema in
  `CONFIGURATION.md`, and isolate smoke-test config. No runtime fallback or upstream
  baseline changes; existing provenance entries remain historical records.

- Mate-owned per-project `base_branch` + startup config: exact resolved repo matching,
  mandatory configured branch → human-approved pinned SHA, no fetch/repin; setup argv
  runs once after checkout/before worker, private output and fail-closed recovery.
  Continuation never reruns setup; no new upstream import. Python regression suite
  (20 tests), extension check and localhost fake-model TUI smoke passed.

- Supervisor-only tool allowlist และ human-only base approval dialog
- Mate-owned `MATE_MODE=dev pi`: ไม่ register supervisor hooks/tools/commands/watcher;
  ย้าย policy runtime ไป `SUPERVISOR.md`, `AGENTS.md` เป็น development guidance
- Treehouse durable lease → task branch ที่ approved SHA → pi worker ใน Herdr tab
- SQLite journal/events, worker reports, acknowledgement และ restart replay
- Mate-owned usage accounting: tokens + Pi-reported estimated USD แยก attempt และรวม task,
  persist ทุก finalized assistant message; แสดง unknown/partial coverage ไม่ตีราคา subscription
  และไม่รวม supervisor usage (ไม่มี upstream import เพิ่ม; regression + real Pi fixture checks)
- Pi-owned control plane พร้อม Herdr native event helper และ 2s durable-result polling
- Two-worker limit, stopped-worker continuation, stalled alerts และ fail-closed recovery
- Mate-owned optional tab closure + Treehouse return: หลัง complete ถามยืนยันแยก;
  ตรวจ original terminal identity/worker lock/single pane/foreground shell ก่อนปิดแท็บ และคืนเฉพาะ
  exact lease path/ID/holder เมื่อ worktree clean/process inventory ปลอดภัย โดยไม่ใช้ `--force`;
  หลัง close อ่าน exact pane ซ้ำและยอมรับเฉพาะ structured `pane_not_found`; success-but-present,
  unreadable หรือ close error ที่ไม่มี proof นี้เป็น uncertain และไม่ retry. แนวคิด fail-closed
  อ้างอิง Firstmate #4510 (`1bdfd8ce`) และ Herdr confirmed-gone gate ที่ current `b430bf50`,
  reference-only ไม่ copy shell code; เก็บ task branch/report/session/cost และ durable identity ไว้
- Mate-owned `/mate-complete ID`: human confirmation จาก review → complete เท่านั้น,
  ตรวจ exact attempt + worker lock, บันทึกเวลา/OS account; ไม่ ack/cleanup/merge และไม่มี reopen
  (ไม่มี upstream import เพิ่ม; tests ตรวจ gate/idempotency/persistence และ UI deny/accept)
- Mate-owned human-only `/mate-cancel ID`: read-only preflight and exact stale-confirmation
  gate for attempt-0 awaiting-base/approved tasks and inspected initial pre-receipt attention;
  preserve all evidence/history/ack, record distinct cancelled state/audit/event, and refuse
  any resource/orphan/lock/process/Herdr uncertainty. Rebased integration retains
  shared-tab placement intent, refuses both endpoint receipt shapes/uncertain splits,
  and keeps cancelled tasks outside force completion. No upstream code copied.
- Mate-owned change: per-task model/effort overrides ใน dispatch/continue; validate ผ่าน Pi catalog/capabilities,
  persist resolved profile และส่ง effort เป็น `--thinking` โดยไม่เปลี่ยน supervisor model หรือ approved base
- Mate-owned config: `mate.config.json` กำหนด worker model/effort default แยกจากตัวหลัก;
  อ่านทุก new dispatch, task override มาก่อน config, continue เก็บ profile เดิม
  (ไม่ import upstream เพิ่ม; tests ตรวจ config validation/precedence/reload)
- Mate-owned worker discovery: โหลด global/project skills/extensions ตาม Pi settings,
  trust project ต่อ run ด้วย `--approve`, ใช้ `MATE_MODE=dev` เฉพาะ Pi child กัน supervisor ซ้อน;
  คง explicit event bridge และปิด prompt templates (ไม่มี upstream import เพิ่ม)
- Mate-owned dispatch routing: natural-language rules in existing `mate.config.json`,
  concrete Pi model/effort only; human overrides win, unmatched work uses `worker`, and
  active rules require explicit dispatch axes. No separate `crew-dispatch.json`, arrays,
  quota selector or multi-harness routing.
- Worker ใช้ native pi TUI + Mate-owned event bridge ผ่าน pipe แยก; จบ attempt
  เมื่อ `agent_settled` แล้วเก็บ report/wake และคา Pi ไว้ idle ให้คุยต่อ;
  graceful exit เมื่อ human ยืนยัน completion หรือออกจาก Pi เอง
- Mate-owned delivery policy: brief ที่ human approve และระบุ PR ชัดเจนอนุญาตให้ worker
  push เฉพาะ assigned task branch และเปิด/อัปเดต PR นั้นได้; local merge เข้า assigned
  task branch ทำได้ตาม scope และ scope ที่ approve สามารถระบุ local target branch ให้
  fast-forward จาก task branch ได้เฉพาะ existing non-task worktree ที่ clean และไม่ diverge;
  ห้าม force/reset, แก้ conflict ใน target, push delivery หรือ merge GitHub/remote PR;
  ยังไม่ทำ automatic cleanup, automatic PR/deploy, multi-harness หรือ supervisor ย่อย
- Ambiguous launch/crash เก็บสถานะ attention ให้ตรวจ ไม่เสี่ยง auto-relaunch
- Mate-owned recovery fix: initial preflight refusal from a shell background/stopped
  process can continue after the same fail-closed idle-pane/resource checks; legacy
  idle-shell recovery remains exact. No upstream code copied.
- ผ่าน local checks และ real Herdr/Treehouse smoke ด้วย fake pi; **ยังไม่ได้ทดสอบกับโมเดล OpenAI จริง**

รายละเอียดใช้งาน/ข้อจำกัดอยู่ [README.md](README.md)

## Source baseline

- Repository: https://github.com/kunchenguid/firstmate
- Inspected/imported source commit: `4930d2caaba8a14b13b754cefc4bd22d77d993d0`
- Reference checkout ที่อ่าน: `/Users/win/mine/firstmate`
- Commit subject: `feat: add Cursor CLI crew harness (#2238)`
- ไม่ได้ fetch จึงไม่อ้างว่าเป็น upstream ล่าสุด
- Untracked `.pi/pi-openai-fast-mode/` ใน reference ไม่ถูกนำเข้าและไม่ถูกแก้

[UPSTREAM.json](UPSTREAM.json) เป็น machine-readable ledger: source commit/path/blob,
mode, destination และ local SHA-256 snapshot เพื่อเทียบการแก้ไขของเรา ภายหลัง local
hash เปลี่ยนไม่ได้หมายถึง upstream เปลี่ยน ต้องดู Git diff ของ Mate ประกอบ

## R01 — Herdr native event transport — ADAPTED

- Source: `bin/backends/herdr-eventwait.py` (อ่านครบก่อน adapt)
- Source blob: `96e7f6650bea39852936fcb34f06540a85292cec`
- Destination: `bin/herdr-eventwait.py`
- Runtime caller: `bin/mate.py` → `NativeEvents`
- Dependencies: Python stdlib; Herdr AF_UNIX `events.subscribe`
- เก็บ: bounded stream wait, subscription handshake, TSV projection, failure exit codes
- เปลี่ยน: validate JSON shapes/finite timeout/pane allowlist, จำกัด buffer 1 MiB,
  context-managed socket, ใช้ Mate request ID
- ไม่ยกมา: Firstmate transition classifier, metadata/home schema หรือ shell adapter
- Failure policy ของเรา: retry แบบจำกัด แล้วแจ้งพร้อมใช้ durable-result polling ต่อ
- Tests: `test_event_transport_filters_and_reports_disconnect`, real subscription ใน `tests/live-smoke.py`

## R02 — Pi watcher lifecycle — ADAPTED

- Source: `.pi/extensions/fm-primary-pi-watch.ts` (อ่านครบก่อน adapt)
- Source blob: `923ec6c310dd0ee1c0f753882403ab77e48737d8`
- Destination: `.pi/extensions/mate-supervisor.ts`
- Adapted scope: generation ownership, lifecycle-owned child, shutdown/stale callback
  protection, bounded retries และ model follow-up delivery
- เปลี่ยน: child เป็น control plane ของ Mate; JSON request protocol; kernel-held lock
  แทน Firstmate PID ancestry lock; durable events อยู่ SQLite; human approval เป็น command
- ตัดออกจาก watcher port: Calm/TUI custom rendering (เพิ่มเฉพาะ Mate Calm แยกใน R07), fm-watch-arm/wake-drain shell chain, FM_* globals,
  operational-input encoding, supervision branch
- Dependencies ใหม่: Node stdlib, installed Pi ExtensionAPI, typebox, `bin/mate.py`
- Startup ของเรา auto-start จาก session_start; ไม่คัดลอกข้อจำกัด initial manual arm ของ source version นี้
- Tests: `node tests/extension-check.mjs` — load, tool guard, deny/approve UI, follow-up,
  dedup, restart replay, ack และ shutdown โดยใช้ real Python control plane + mocked Pi UI

## R03 — Durable wake/ack/recovery — REFERENCE-ONLY

- Source references: `bin/fm-wake-lib.sh`, `bin/fm-wake-drain.sh`,
  `bin/fm-watch-arm.sh`, `bin/fm-watch.sh`
- Scope ที่ศึกษา: state/event/ack contract และ dependency graph บางส่วน ไม่ใช่ full code audit
- Destination: `bin/mate.py` — implementation ใหม่ด้วย SQLite และ fcntl ไม่ copy shell queue code
- เก็บแนวคิด: persist before notify, ack after handling, replay, single owner, explicit failure
- ไม่รับ graph ของ PR/Relay/checks/pending-replies/secondmates และไม่ได้แปลง schema ของ Firstmate
- Tests: durable ack/rollback, ownership lock, restart snapshot, missing/stalled worker,
  uncertain acquire, duplicate dispatch ใน `tests/test_mate.py`
- Known ceiling: scan task records บน local disk และ poll ทุก 2s; `worker.max_active`
  defaults to 2 and remains intended for a small personal fleet.

## R04 — Treehouse/worktree safety — REFERENCE-ONLY + OWN WORKFLOW

- Source: `bin/fm-spawn.sh` → `validate_spawn_worktree`, `freshen_spawn_worktree_base`, acquire block
- Source blob: `bd461ed96040c8c20d40dc707bfea4be38dd8acc`
- Destination: `bin/mate.py` → propose/approve/dispatch/check_lease
- ไม่ copy fm-spawn ทั้ง script หรือ dependency graph
- Source เดิมส่ง `treehouse get` เข้า shell และปรับ pool ไป origin/default branch ด้วย reset
- ของเราใช้ installed Treehouse `get --lease --json`, บันทึก lease receipt, ตรวจ lease ID/holder
  ผ่าน `status --json`, ตรวจ isolated worktree/common Git directory แล้ว `git switch -c` จาก approved SHA
- **ไม่รับ default-branch refresh/reset behavior** เพราะขัดกับการยืนยันฐานของเรา
- ไม่แก้ shared Treehouse config ต่อ task ไม่ reset --hard และเก็บ pooled branch/commits ไว้
- Journal ก่อน acquire; ambiguous outcome ไม่ acquire ซ้ำ/return/rollback แบบเดา
- Local launch update: อ่าน `launch_template` ของ source pinned เดิมเพิ่มเติมเพื่อยืนยัน
  Firstmate เรียก pi interactive + explicit extension ไม่ใช่ print/JSON stdout
- ของเราใช้ `--tui-mode regular` และ `bin/worker-events.ts` ที่เขียนเอง ส่ง event ผ่าน
  inherited pipe โดยไม่ดัก stdin/stdout; publish report เมื่อ `agent_settled` ไม่ใช่ `agent_end`
- เดิมหนึ่ง attempt จบแล้ว Pi exit; persistent-worker follow-up ด้านบนเปลี่ยนให้คา Pi
  เหมือน Firstmate แล้ว โดยใช้ lifecycle/control ของ Mate เอง ไม่ copy turn-end extension.
  Continue ใช้ Pi เดิมขณะ idle หรือเปิด saved session หากปิดไปแล้ว ไม่สร้าง lease ซ้ำ
- Tests เพิ่ม: real Python worker + real Pi TUI ใน PTY / localhost fake model
  (`tests/tui-smoke.py`), bridge lifecycle, missing-settled และ forced-kill fail-closed
- Tests: deny approval, moving ref, exact SHA, unchanged original checkout, lease identity,
  idempotency และ real Treehouse fixture

## R05 — Herdr endpoint control — REFERENCE-ONLY

- Source reference: `bin/backends/herdr.sh` (ศึกษา selected control operations ไม่ใช่ full audit)
- Source blob: `7367a8db5c7361780990294e649044f0e0d31be6`
- Destination: `bin/mate.py` → herdr/check_endpoint/launch_worker
- ของเราเรียก installed CLI โดยตรง: exact named session/socket, pane/workspace IDs,
  create tab/workspace --no-focus, pane run; ไม่ copy adapter
- CLI factsตรวจจาก installed Herdr 0.8.0 และ live fixture:
  create/get คืน JSON identity receipts; pane run คืนข้อความ ไม่ใช่ JSON; --json ไม่ใช่ global flag
- Mate-owned opt-in `workspace_per_task`: Treehouse ยังเป็นเจ้าของ worktree; สร้าง Herdr
  workspace ที่ CWD นั้นและใช้ seeded tab/root pane จาก exact create receipt โดยตรง
  เพื่อคง tab bar สำหรับ shell/server/log เพิ่มเติม; `same_tab_as` ยัง override เป็น split
- Reference เพิ่มเติมจาก Firstmate local `a27646c`: presentation-space create/label concept
  เท่านั้น ไม่ copy token journal, seeded-tab prune, recovery/adoption, ordering/move,
  stale-space cleanup, version floor, multi-home lock หรือ multi-backend routing
- Tests: unit red/green และ real isolated Herdr server/pane/subscription/Treehouse ใน
  `tests/live-smoke.py`, รวม sibling tab ที่คง workspace และ block lease return

## R06 — Supervisor contract / briefs — REFERENCE-ONLY

- Source reference: `AGENTS.md` role/authority/worktree safety concepts
- Destinations: `SUPERVISOR.md` (เดิม `AGENTS.md`), `WORKER.md` — เขียน workflow ของ Mate เอง
- Development-mode update: ย้าย supervisor policy โดยคงเนื้อหาเดิม; root `AGENTS.md`
  เขียนใหม่สำหรับพัฒนา repo และ defer ให้ runtime policy เมื่อ supervisor tools active
- Ledger เก็บ `original_path` และ initial SHA-256 ของ policy เดิม ไม่เปลี่ยน baseline upstream
  และไม่ถือ developer instructions ใหม่เป็น copy จาก Firstmate
- ตัวหลัก delegate project work ทุกประเภท; ไม่มี bash/read/write tools สำหรับตัวหลัก
- Approval ผูก task/repo/base SHA และ scope ผ่าน UI; worker output ไม่ใช่ human approval
- ไม่ยก bootstrap commands, mandatory vocabulary, internal skills หรือ project delivery modes
- Worker policy เป็น instructions ไม่ใช่ OS sandbox; documented ใน README

## R07 — Focused Calm presentation — ADAPTED

- Source: `.pi/extensions/fm-calm.ts` (อ่านครบก่อน adapt)
- Source blob: `13bafc6fe53befed3bc2b95d0a3466a461377e8e`
- Related reference inspected: `.pi/extensions/lib/fm-calm-visibility.ts`,
  blob `27a03f04c1f4ae9bbbf1cad530fb23221ec48b3b`
- Baseline: pinned commit เดียวกับ Source baseline ข้างต้น ไม่ได้ fetch version ใหม่
- Destination: `.pi/extensions/lib/calm.ts`; hookup ใน `.pi/extensions/mate-supervisor.ts`
- เก็บ: home-persistent preference ผ่าน atomic rename, self-rendering tool rows,
  public tool-expansion redraw pattern
- เขียนเอง: conservative result classifier สำหรับ Mate เท่านั้น, `/calm [on|off|status]`,
  default off, report-only wake แสดง compact notice แต่ไม่ถือว่าจบงาน
- ไม่ซ่อน: reports, blockers, errors, pending/unknown results และ human approval dialog
- ไม่รับ: blanket operational-input hiding, built-in/global tool overrides, private
  prototype patches, thinking suppression, boat animation และ export interception
- ไม่เปลี่ยน model context/raw messages, execution, delivery หรือ ack; rendered exports
  อาจสะท้อน Calm จึงใช้ `/calm off` ก่อน export/share แบบเต็ม
- Dependencies: Node stdlib, installed Pi ExtensionAPI และ pi-tui Text
- Tests: `tests/extension-check.mjs` — persistence/restart, toggle existing rows,
  routine hiding, retained failures/reports/approval, payload preservation และ wake replay
- ยังไม่ได้ทดสอบ visual terminal จริง; renderer/UI tests ใช้ mock

## License

R01/R02/R07 เป็น adapted code ไม่ใช่แค่ inspiration เก็บ full MIT copyright + permission
notice ที่ [third_party/firstmate/LICENSE](third_party/firstmate/LICENSE) และใส่ attribution
ในไฟล์ที่เกี่ยวข้อง ห้ามลบ notice ตอนปรับโค้ดหรือเผยแพร่

## How to review upstream updates

1. Fetch ใน reference clone แยก โดยไม่ bootstrap/load extensions และไม่เปลี่ยน runtime จริง
2. ต่อ component เปรียบเทียบ imported/last-reviewed commit กับ candidate เช่น:

   ```sh
   git -C /path/to/firstmate-reference fetch origin
   git -C /path/to/firstmate-reference diff \
     4930d2caaba8a14b13b754cefc4bd22d77d993d0 origin/main -- \
     bin/backends/herdr-eventwait.py .pi/extensions/fm-primary-pi-watch.ts
   ```

3. อ่าน call sites, related tests, dependencies และ safety fixes ของ change set ด้วย
   ห้ามเลือกจากชื่อไฟล์อย่างเดียว R03–R06 ใช้ review ว่าหลักการ/assumption ของเรายังถูกไหม
4. บันทึก **accept / defer / reject / not-applicable** พร้อมเหตุผลและ dependencies
5. Accept = port เฉพาะ change set ที่สัมพันธ์กันเข้าของเรา แยก commit อ้าง Rxx ไม่ merge ทั้ง Firstmate
6. รัน checks ใน README และเพิ่ม regression check ถ้า behavior ที่ port ยังไม่มี check
7. บันทึก local change commit/hash และ evidence แล้วค่อยเลื่อน imported/reviewed commit
   ของ component นั้น ห้ามเลื่อนทุกตัวโดยไม่ได้ตรวจ
8. Deferred items ต้องคงอยู่ใน decision log แม้ reviewed-through จะขยับแล้ว เพื่อไม่ลืมของที่ค้าง
9. เปลี่ยน runtime เมื่อไม่มี worker live และจัดการผลค้างแล้ว สำรอง state/config ก่อนเปลี่ยน schema
   Rollback code อย่างเดียวอาจไม่พอหาก state schema เปลี่ยน

## Decision log

- Architecture: separate Mate runtime + selective copy/adapt — ผู้ใช้อนุมัติ
- R01/R02: adapt selected transport/lifecycle; import source pinned ข้างต้น
- R03–R06: reference-only; implement ด้วย native/stdlib แทน bulk-copy graph
- R07: adapt focused Calm presentation; ไม่รับ full Firstmate UI/private patches
- Upstream default-branch reset: reject สำหรับ Mate; ใช้ human-approved SHA
- Full watcher stack, remote/Relay/multi-harness, auto-cleanup: ไม่รับในรุ่นแรก
- Real OpenAI subscription E2E: ยังไม่ได้รัน ไม่ถือว่าผ่านจาก fake-model smoke

ต่อหนึ่ง update บันทึก: Rxx, old/candidate commits, change set, decision/reason,
related dependencies, local port commit, test evidence, migration/rollback และ unresolved deferrals
