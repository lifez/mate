# Mate — selective Firstmate reuse map

## Decision

**Mate เป็นระบบของเราเอง ไม่ใช่ Firstmate ทั้งชุด + customization layer**

`mate/` เป็นเจ้าของ workflow, instructions, state และ runtime ของตัวเอง เลือก
copy/adapt เฉพาะส่วนจาก Firstmate และรับ update โดย port เป็น change set ไม่ใช่
pull Firstmate ทับ Mate ไม่มี runtime import/source จาก `../firstmate` ไม่มี submodule
และไม่แชร์ Firstmate operational state

## Current implementation

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
- Mate-owned optional tab closure: หลัง complete ถามยืนยันแยกก่อนปิดแท็บ worker เท่านั้น;
  ตรวจ original terminal identity/worker lock/single pane/foreground shell, journal uncertain close,
  ไม่คืน lease/ลบ worktree/report/cost และไม่ import upstream cleanup machinery
- Mate-owned `/mate-complete ID`: human confirmation จาก review → complete เท่านั้น,
  ตรวจ exact attempt + worker lock, บันทึกเวลา/OS account; ไม่ ack/cleanup/merge และไม่มี reopen
  (ไม่มี upstream import เพิ่ม; tests ตรวจ gate/idempotency/persistence และ UI deny/accept)
- Mate-owned change: per-task model/effort overrides ใน dispatch/continue; validate ผ่าน Pi catalog/capabilities,
  persist resolved profile และส่ง effort เป็น `--thinking` โดยไม่เปลี่ยน supervisor model หรือ approved base
- Mate-owned config: `mate.config.json` กำหนด worker model/effort default แยกจากตัวหลัก;
  อ่านทุก new dispatch, task override มาก่อน config, continue เก็บ profile เดิม
  (ไม่ import upstream เพิ่ม; tests ตรวจ config validation/precedence/reload)
- Mate-owned worker discovery: โหลด global/project skills/extensions ตาม Pi settings,
  trust project ต่อ run ด้วย `--approve`, ใช้ `MATE_MODE=dev` เฉพาะ Pi child กัน supervisor ซ้อน;
  คง explicit event bridge และปิด prompt templates (ไม่มี upstream import เพิ่ม)
- Worker ใช้ native pi TUI + Mate-owned event bridge ผ่าน pipe แยก; จบ attempt
  เมื่อ `agent_settled` แล้ว graceful exit, เก็บ report/wake เหมือนเดิม
- ยังไม่ทำ automatic cleanup, PR/merge/deploy, remote, multi-harness หรือ supervisor ย่อย
- Ambiguous launch/crash เก็บสถานะ attention ให้ตรวจ ไม่เสี่ยง auto-relaunch
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
- Known ceiling: scan task records บน local disk และ poll ทุก 2s; ออกแบบสำหรับ personal fleet ≤2 active workers

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
  inherited pipe โดยไม่ดัก stdin/stdout; exit เมื่อ `agent_settled` ไม่ใช่ `agent_end`
- ต่างจาก persistent Firstmate worker: หนึ่ง attempt จบแล้ว Pi exit แต่ tab/transcript อยู่;
  continue เปิด saved session เดิมใหม่ ไม่สร้าง lease ซ้ำ ไม่ copy Firstmate turn-end extension
- Tests เพิ่ม: real Python worker + real Pi TUI ใน PTY / localhost fake model
  (`tests/tui-smoke.py`), bridge lifecycle, missing-settled และ forced-kill fail-closed
- Tests: deny approval, moving ref, exact SHA, unchanged original checkout, lease identity,
  idempotency และ real Treehouse fixture

## R05 — Herdr endpoint control — REFERENCE-ONLY

- Source reference: `bin/backends/herdr.sh` (ศึกษา selected control operations ไม่ใช่ full audit)
- Source blob: `7367a8db5c7361780990294e649044f0e0d31be6`
- Destination: `bin/mate.py` → herdr/check_endpoint/launch_worker
- ของเราเรียก installed CLI โดยตรง: exact named session/socket, pane/workspace IDs,
  create tab --no-focus, pane run; ไม่ copy 3,344-line adapter
- CLI facts ตรวจจาก installed Herdr 0.8.0 และ live fixture:
  create/get คืน JSON identity receipts; pane run คืนข้อความ ไม่ใช่ JSON; --json ไม่ใช่ global flag
- ไม่ยกมา: workspace presentation, rename/order/cleanup machinery, multi-backend routing
- Tests: real isolated Herdr server/pane/subscription ใน `tests/live-smoke.py`

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
