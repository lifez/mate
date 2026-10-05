# Firstmate upstream review — 2026-10-05

## ขอบเขตและหลักฐาน

- ตรวจ GitHub `kunchenguid/firstmate` ผ่าน `gh-axi api repos/kunchenguid/firstmate/commits/main` เวลาใกล้ `2026-10-05T06:42Z`.
- `main` ณ เวลาตรวจ: [`f470a01c098c1536d83b802874bd954a2c04b506`](https://github.com/kunchenguid/firstmate/commit/f470a01c098c1536d83b802874bd954a2c04b506), commit วันที่ 4 ต.ค. 2026 UTC, #6575.
- Reference checkout `/Users/win/mine/firstmate` มี HEAD ตรงกับ GitHub จึงไม่ต้อง fetch, checkout หรือแก้ไฟล์ใน reference. Untracked `.pi/pi-openai-fast-mode/` ไม่ถูกแตะ.
- Baseline code imports ของ R01–R07 ยังเป็น `4930d2caaba8a14b13b754cefc4bd22d77d993d0`: ถึง tip มี 495 commits. นี่ไม่ใช่จำนวน features ที่ Mate ขาด เพราะเรามี selective adaptations และ implementation ของตัวเองภายหลัง.
- จุด reference-only ใหม่สุดที่ระบุใน `UPSTREAM.json` คือ `3c2a91d70e07b48c982a8fe3460ba367795bb5d3` (27 ก.ย.), สำหรับ Claude launch/Stop hook บางส่วนเท่านั้น. หลังจุดนั้นถึง tip มี 53 commits; ไม่ใช่ whole-repo reviewed-through baseline.
- อ่าน commit log/diff และ selected source/docs เทียบกับ Mate. เป็น triage ไม่ใช่ full upstream audit หรือการพิสูจน์ bug ด้วย executable reproduction.
- ไม่มี runtime port, baseline advancement, schema/state/config mutation, worker launch หรือ live-host operation. ไม่รัน runtime tests สำหรับรายงานนี้.

## ควรหยิบมาพิจารณาก่อน

### 1. Quota read failures: adapt แนวคิด ไม่ copy watcher

Upstream [#6490](https://github.com/kunchenguid/firstmate/pull/6490) (`2e659ffd`) แยก temporary timeout/read failure จาก missing/incompatible tool. Transient failures มีงบสามครั้งติดกันและ reset เมื่ออ่านสำเร็จ; permanent failures รายงานทันที.

Source ที่ตรวจ: `bin/fm-procevent-quota.sh`, `bin/fm-quota-axi-lib.sh` commit diff และรายละเอียด regression ใน commit.

Mate `.pi/extensions/mate-supervisor.ts:refreshQuota` มี timeout 20 วินาทีและอ่านทุก 5 นาที. ถ้าอ่านผิดพลาดจะล้าง footer; interval ยังทำงาน จึง **ไม่มีหลักฐานว่า watcher ของเรา shutdown ตาม bug ของ Firstmate**. สิ่งที่ได้ประโยชน์คือ footer ที่บอก `unavailable`/`stale` ชัดเจน แทนตัวเลขหายเงียบ ๆ; ถ้าเก็บค่าครั้งก่อนต้องระบุว่าเก่าและเวลาของมัน ห้ามแสดงเหมือน quota ปัจจุบัน.

Recommendation: **adapt candidate** ขนาดเล็กเฉพาะ presentation/error state. ไม่ต้องเพิ่ม event watcher หรือ quota routing. Runnable check เมื่อทำ: good read → timeout → good read, stale value ไม่ถูกอ้างว่าปัจจุบัน และ session-generation เดิมไม่เขียน footer ทับ session ใหม่.

### 2. Waiting without model turns: adapt เฉพาะส่วนที่ยังขาด

Upstream [#4859](https://github.com/kunchenguid/firstmate/pull/4859) (`fd325b1b`) เพิ่ม opt-in `config/wait-no-turns`. Brief ให้ worker จบ turn เมื่อ blocked/needs-decision; external waits ใช้ blocking foreground command ที่มี timeout ไม่ background แล้วเสียหลาย model turns มาตรวจสถานะ. มีการ defer automatic sends บางชนิดและ durable retry bookkeeping ด้วย.

Source ที่ตรวจ: `bin/fm-brief.sh` diff, `docs/configuration.md` Waiting section.

Mate `WORKER.md` มี STOP + final blocker report อยู่แล้ว; persistent Pi กลายเป็น idle และ supervisor polling ไม่เรียกโมเดลเพื่อรอ. ไม่จำเป็นต้องสร้าง config flag/inbox/retry graph แบบ Firstmate. ช่องว่างคือคำแนะนำชัด ๆ สำหรับรอ CI/external checks ภายใน scope ที่อนุญาต.

Recommendation: **adapt candidate** เฉพาะ worker instructions ว่าใช้ native watch/หนึ่ง bounded foreground wait; ไม่ใช้ sleep/status model-turn loop. ไม่คัดลอก harness timeout ตัวเลขโดยไม่ตรวจ installed harness และไม่ขยายสิทธิ์ checks/network/background jobs.

### 3. Watcher process exit versus stdio close: investigate ก่อน port

Upstream [#5489](https://github.com/kunchenguid/firstmate/pull/5489) (`e31bc6e6`) ตรวจ child liveness ด้วย exit/signal state และ PID probe: OS process ตายแล้ว แต่ `close` ยังไม่มาเพราะ pipe เปิดอยู่ ไม่ควรกัน repair/retry slot. Confirmation ผูก recovery token เดิม ไม่ให้ผลจาก predecessor ไป retire successor. เพิ่ม bounded opt-in diagnostic log, default off.

Source ที่ตรวจ: `.pi/extensions/fm-primary-pi-watch.ts` diff และ commit regression description.

Mate `.pi/extensions/mate-supervisor.ts:start` รับ `error`/`close`; เมื่อ `close` มาแล้วจึง fail pending RPC และ schedule retry. Generation และ `child !== process` guards มีอยู่แล้ว. Architecture ไม่มี Firstmate arm/successor/branch confirmation graph.

Recommendation: **investigate** disposable test ที่ control-plane process exit แต่ descendant ยังถือ pipe ก่อนตัดสินว่า Mate ต้องแก้หรือไม่. ถ้าเกิดจริง ให้จัดการ process exit/close และ idempotent callback ใน lifecycle เดิม โดยรักษา ownership lock และไม่ retry mutating RPC. ไม่ยก `liveArmChild` หรือ marker stack มาทั้งชุด. Diagnostic log ให้ defer จนมีเหตุให้ใช้; ห้ามบันทึก briefs/reports/secrets โดยไม่จำเป็น.

### 4. Scratch output and dirty teardown messages: เลือกเฉพาะ UX

Upstream [#6505](https://github.com/kunchenguid/firstmate/pull/6505) (`918a5bf1`) แยก project edits จาก scratch/proof output และแจ้งว่า worktree สกปรกเพราะ tracked edits หรือ untracked-only leftovers พร้อมตัวอย่าง paths จำกัดจำนวน.

Source ที่ตรวจ: `bin/fm-brief.sh`, `bin/fm-teardown.sh` diff.

Mate `WORKER.md` จำกัด writes ใน assigned worktree; `bin/mate.py:lease_return_preview` คืนรายการ changes และ lease-return ตรวจ dirty state พร้อม human confirmation อยู่แล้ว. จึงไม่ต้องนำ cleanup engine หรือ Firstmate exemptions มาใช้.

Recommendation: **optional adapt** diagnostics ที่อ่านง่าย และ scratch convention เฉพาะ approved task-local temporary directory. อย่านำข้อยกเว้นให้ worker เขียน `MATE_HOME/data` โดยตรงมาใช้; reports/state ของ Mate เป็น runtime-owned. ห้ามเปลี่ยน refusal เป็น automatic deletion.

## อัปเดตที่สำคัญ แต่เรามี principle แล้ว / ต่างสถาปัตยกรรม

- **Seeded Pi trust** — [#6387](https://github.com/kunchenguid/firstmate/pull/6387), `87fa81b8`: secondmate launch เพิ่ม capability-probed `--approve` เพื่อไม่ติด trust prompt. Mate ใช้ `--approve` อยู่แล้วทั้ง worker launch และ `bin/mate_remote_bootstrap.py`; **no immediate port**. คงข้อจำกัดว่า trust รวม executable project resources และ remote setup เป็น operator-controlled.
- **Remote turnover / heartbeat** — [#6431](https://github.com/kunchenguid/firstmate/pull/6431), `9ea0c41a`: independent heartbeat, verified live ownership, serialized LaunchAgent repair, bounded bootout wait; heartbeat stale ไม่เท่ากับ process dead. Mate `bin/mate_remote_bootstrap.py` ตรวจ kernel ownership/exact endpoint และไม่ auto-relaunch จาก stale heartbeat อยู่แล้ว. **ใช้เป็น regression inspiration**, ไม่ copy macOS LaunchAgent remote-job stack โดยเฉพาะเส้นทาง Omarchy.
- **Remote polling/process churn** — [#6255](https://github.com/kunchenguid/firstmate/pull/6255), [#6363](https://github.com/kunchenguid/firstmate/pull/6363), [#6575](https://github.com/kunchenguid/firstmate/pull/6575): ลด process churn และ claim-directory sweep. #6575 ระบุปัญหา ~17k claim directories และแก้ single-walk cleanup. Mate ใช้ SQLite inbox/outbox แทน directory claims; **not directly applicable**. อ่าน performance contract ได้ แต่ไม่เพิ่ม sweeper ที่ไม่มี state แบบนั้น.
- **Busy inbox escalation** — [#6518](https://github.com/kunchenguid/firstmate/pull/6518), `42dd906d`: durable consecutive busy-deferral budget ก่อน escalate โดยไม่พิมพ์ใส่ worker pane. Mate supervisor continuation ยอมรับ idle worker และ uncertain sends ห้าม resubmit; ไม่มี queued mid-run steering inbox. **defer** จนมีความต้องการ asynchronous steering จริง. ห้ามเปลี่ยน current refusal ให้กลายเป็น blind send/retry.
- **Pi 1.0.1 export compatibility** — [#6530](https://github.com/kunchenguid/firstmate/pull/6530), `fede6197`: Firstmate test fixture รองรับ `getToolRenderers` ที่เปลี่ยนจาก `getToolDefinition`. เป็น test change ไม่ใช่ Calm runtime fix ของ Mate. **compatibility checkpoint** หากอัปเกรด Pi; README ของ Mate ระบุ tested Pi 0.85.1 จึงยังไม่อ้างว่า Mate รองรับ Pi 1.0.1 จาก upstream test นี้.

## Features ใหม่ที่ยังไม่ควรยกมาทั้งชุด

1. **Attended supervision / quiet / AFK สำหรับ Claude และ Cursor** — [#5748](https://github.com/kunchenguid/firstmate/pull/5748), [#6124](https://github.com/kunchenguid/firstmate/pull/6124). Claude supervision host เปิด default ใน Firstmate; main dialog mirror และ host รับ routine wakes. Mate มี Claude supervisor/Stop rewake adapter แต่ **ไม่ได้เท่ากับ attended host หรือ quiet branch**. Defer จนปัญหา wake รบกวนแชทคุ้มกับ agent/ownership/hand-back complexity; ต้องคง human-only approval/completion.
2. **Claude Calm supervision notes** — [#6039](https://github.com/kunchenguid/firstmate/pull/6039). Mate Calm/Bearings ปัจจุบัน Pi-only. ถ้าใช้ Claude เป็นหลักอาจมีประโยชน์ แต่ Firstmate mod เป็นคนละ surface; defer ไม่ copy plugin/runtime patches โดยอัตโนมัติ.
3. **Lavish feedback routing to worker + confirmed reply handoff** — [#5099](https://github.com/kunchenguid/firstmate/pull/5099), [#6169](https://github.com/kunchenguid/firstmate/pull/6169). #6169 ใช้ `lavish-axi reply` acceptance ก่อนกิน staged reply/handoff, รุ่นเก่ามี best-effort fallback. Mate Bearings read-only, ไม่มี feedback listener/answer binding. Defer จนผู้ใช้ต้องการ annotation → task workflow; ต้องทำ durable ownership, exact task/scope binding และ human approval ไม่ใช่เอาปุ่มบน board ไปสั่งงานทันที.
4. **Disposable live supervision lab** — [#6037](https://github.com/kunchenguid/firstmate/pull/6037). Lab แยก home/pool/session, ตรวจ trust/global-state preservation; ใช้ real provider turn. Mate มี disposable localhost fake-model TUI/live/remote-live smoke แล้ว. Reuse isolation/checklist ideas ถ้าเพิ่ม live Claude validation; ไม่สร้าง lab wrapper เพิ่มตอนนี้และไม่รัน quota-consuming test โดยไม่ขออนุญาต.
5. **Resource guard, account pin, extra harnesses, Gerrit, dispatch confidence** — [#5903](https://github.com/kunchenguid/firstmate/pull/5903), [#5358](https://github.com/kunchenguid/firstmate/pull/5358), [#5380](https://github.com/kunchenguid/firstmate/pull/5380), [#5427](https://github.com/kunchenguid/firstmate/pull/5427), [#5478](https://github.com/kunchenguid/firstmate/pull/5478). Commit-log discovery only สำหรับรายการนี้ ไม่ได้ full source/dependency audit. Defer จนมีเครื่อง RAM ตึง, หลาย accounts, harness/forge ใหม่ หรือ dispatch misrouting ที่วัดได้. ไม่จำเป็นสำหรับ Pi/Claude + Herdr + SQLite workflow ปัจจุบัน.

## ลำดับที่เสนอ

1. Small quota-footer failure UX.
2. Explicit no-model-turn external-wait guidance.
3. Watcher exit-with-held-pipe regression investigation.
4. Optional dirty/scratch UX; ก่อน production remote rollout เพิ่ม slow-request/stale-heartbeat no-relaunch regression โดย reuse fixtures เดิม.

ผล triage ข้างต้นเดิมเป็นข้อเสนอ. Follow-up: ผู้ใช้อนุมัติและ implement เฉพาะ dirty/scratch UX (#6505), ยืนยันไม่ทำ quota. Shared status summary ใช้ใน launch refusal และ local/remote Pi/Claude cleanup dialogs; เก็บ full paths และ exact confirmation เดิม. Worker guidance ใช้ task-specific temporary material ไม่เขียน Mate state. Python 107 tests, extension check และ remote UI check ผ่าน; ไม่มี reload/stop live workers. Quota/waiting/watcher และ features อื่นยังไม่ implement. ไม่เลื่อน baseline ใน `UPSTREAM.md`/`UPSTREAM.json`: report นี้ไม่ได้ตรวจทุก component จน tip และไม่มี imported code ใหม่. ทุก runtime change ต้องประสานผู้ใช้และทำเมื่อ workers หยุด; checks ใช้ disposable state เท่านั้น.
