# Remote Mate dispatch

Decision: delegate to a **separate Mate home on each remote host**, not a local
worker whose Herdr pane happens to be remote. The remote home owns its repository,
Treehouse lease, Pi process, Herdr endpoint, session, reports and worker locks.
The local home owns the human-facing task record, approval and delivery receipts.
This adapts the architecture (not code) of Firstmate remote secondmates; Firstmate
itself does not remotely place individual workers. See
`../firstmate/docs/remote-secondmates.md` and `../firstmate/bin/fm-spawn.sh`.

## Confirmed review decisions

- Full remote **secondmate supervisor agent**, not just a deterministic executor.
- 1A: all work, including secondmate-proposed child tasks, requires human approval
  through the primary. Approval must bind remote home identity, task ID, proposal
  revision, repository, SHA and scope; remote-local approval is not authorized.
- 2A: separate transport module; reuse the existing worker lifecycle on its owning
  host rather than duplicating dispatch/lease/report code.
- 3A: automated disposable fixtures AND an actual disposable SSH smoke test are
  required before claiming production readiness.
- 4A: independent transport process per remote home, bounded requests/event pages
  and timeouts; unreachable hosts must not block local monitoring or other homes.
- Auto-provision/update is deferred in `TODOS.md`. No migration or automatic failover.

## Implementation status

First increment: `bin/mate_remote.py` implements the internal durable inbox journal,
with explicit home/primary identity binding, stable acceptance receipts, conflicting
replay refusal, atomic single-owner claims and transactional outcome publication.
A process crash after claiming leaves the request claimed; it is never automatically
requeued. Outcome reads are bounded and non-destructive. `tests/test_mate_remote.py`
uses disposable SQLite journals, concurrent connections and a real crashing process.

Second increment: `bin/mate_remote_transport.py` adds a one-request SSH receiver
and a long-lived stdio transport process pinned to one home. Only `hello`, `accept`,
`status` and `outcomes` are allowed. SSH requests carry JSON over stdin, not shell
payloads. OpenSSH uses batch authentication, strict host-key checking, disabled
forwarding and bounded connection/dead-peer timers; the transport bounds elapsed
time and both output streams, validates reply identity and acceptance fingerprints,
and never retries automatically. Tests execute the real receiver and transport
processes behind a deterministic fake SSH executable. They do not prove real SSH
authentication, host-key behavior or Mac login-session readiness.

Third increment: `bin/mate_remote_primary.py` adds the primary-owned durable outbox.
The transport now requires an explicitly provisioned outbox and pins its SSH alias,
remote home and primary identity. It commits the exact payload/fingerprint before
send and `sending` before crossing SSH. Repeated `accept` calls inspect existing
sending/uncertain/accepted/rejected deliveries instead of resubmitting. Explicit
`reconcile` performs only a remote `status` read, requires the matching payload
fingerprint, and does not treat an unknown request as permission to resend.
Disposable tests cover concurrent senders, route changes, failure before delivery,
a process killed after remote commit but before local receipt, and recovery without
resubmission. This prevents a lost local receipt from turning into a new send;
it does not yet apply approvals or consume remote work.

Fourth increment: `bin/mate_remote_runtime.py` connects the inbox to the existing
`mate.py serve` control plane. An explicit private `MATE_HOME/remote.json` opts in a
fresh home; its identity/parent/journal binding is latched in the task database.
Removing or changing the binding refuses startup/operations rather than silently
becoming a local supervisor. The supervisor lock owns consumption; one queued
operation is claimed per loop. Claimed requests are never automatically replayed.

Local secondmate RPC refuses approval, scope acceptance, completion, cancellation,
tab closure and lease return. Authenticated-parent inbox operations use the existing
Mate methods and their endpoint/lease/attempt gates. Human operations additionally
require the exact `confirmation` hash from the displayed task: it binds the remote
home, primary and full stored task (repo, SHA, scope, branch, state and revision).
Status and confirmation are read in one SQLite snapshot; a concurrent change cannot
attach a newer confirmation to an older displayed scope. Initial and scope-addition
approvals retain the parent request identity in the task audit. An operation failure
is recorded as uncertain, not as proof that effects were rolled back.

Fifth increment: `.pi/extensions/lib/remote.ts` adds the primary-only `mate_remote`
model tool and `/mate-remote` command. The model operation allowlist excludes human
mutations. Primary dialogs display remote host/home, repository, SHA, branch, scope
and the exact revision; stale scope or session replacement refuses without silently
refreshing approval. Scope approval, completion, cancellation, tab closure and lease
return use their existing runtime gates with separate explicit human confirmations.
Each lazily started per-home transport has its own queue, timeout and shutdown; an
unavailable home does not block another home or the local RPC queue.

Remote runtime outcomes now commit into the primary outbox before returning to the
UI. Unresolved/uncertain mutations block new mutations on that route, while status
and inspection stay available. `pending` lists them and `result REQUEST_UUID` reads
the original result without resubmitting. Terminal uncertain outcomes remain blocked;
there is deliberately no implicit clearance/retry button. `reconnect` only resets
the local transport and leaves durable journals and remote processes untouched.

**Disposable real-host verification passed.** Explicit setup, readiness checks, human-confirmed
secondmate start/recovery and durable hash-checked event mirroring are implemented. `accepted` still means only
durably queued and inbox `done` means an operation has an outcome, never automatic
human task completion. IDs and confirmation hashes are not signatures/credentials;
receiver access must be bound to an operator-configured SSH key/forced command.
Same-account processes remain trusted. Normal local homes retain their original
runtime path; no live task state has been modified.

Implemented: exclusive home/route setup; doctor/start/recover human commands;
journal-before-launch and exact endpoint/lock/heartbeat proofs; durable notification
stream identity, prefix hashes, atomic local cursor/event commit and acknowledgements;
independent automatic primary wakes. Recovery never replays an unobserved uncertain
submission; observed stopped supervisors reuse only their original idle endpoint.
A successful bootstrap recovery audits resolution without rewriting original evidence.

`python3 tests/remote-live-smoke.py --local` passed with real Pi, Herdr and Treehouse,
a localhost fake model and disposable Git/home/lease state: start, automatic secondmate
dispatch after fixture approval, report, resident continuation, exact cleanup, mirror/
ack and stopped-supervisor recovery. Human dialogs are separately exercised by
`node tests/remote-ui-check.mjs`. `--host omarchy` subsequently passed this lifecycle
over real SSH with real Pi/Herdr/Treehouse and a localhost fake model. The earlier
network-unreachable result no longer applies. No production route/home is deployed
and real-provider authentication remains untested.
See CONFIGURATION.md for setup; full software provisioning/update remains deferred.

## Remote prerequisite verification

With human authorization, Treehouse v2.3.0 was installed user-locally on the
Omarchy Linux x86_64 host, without sudo or credential copying. The upstream
`kunchenguid/treehouse` v2.3.0 release archive was downloaded through `gh-axi`
and verified against the GitHub asset SHA-256:
`94fd2b2c20c35aac1ddc2941317890ad82c9916f5ccecbac4a50cda783eed10f`.
Installation used an exclusive link into `~/.local/bin/treehouse`, refusing to
overwrite an existing executable.

A real SSH-run disposable Git repository/pool passed Treehouse lease acquisition,
status/identity checks, wrong-lease return refusal, exact lease return and safe
worktree disposal. The fixture was removed after verification. No existing
Herdr servers, workers, task state or credentials were touched. This verifies the
Treehouse prerequisite **only**, not end-to-end remote Mate dispatch or Pi/model
authentication. The subsequent full SSH fixture lifecycle passed as recorded above.

The 26 remote inbox/transport/runtime tests also passed when executed over SSH
on Omarchy from a disposable source copy, including the real `mate.py serve`
process consuming an approval and refusing local approval. The copy was removed.
These tests still use a deterministic fake SSH executable inside the transport
suite and synthetic fixture approvals; they are not the final cross-machine
Pi/Herdr worker lifecycle or primary-human-dialog smoke test.

## Internal runtime request contract

The same receiver binding JSON is installed as private `MATE_HOME/remote.json` in
a **fresh** secondmate home. Starting its normal Pi supervisor starts the existing
control plane, which consumes inbox operations independently of receiver SSH
connections. It must run inside the remote Herdr pane, just like a local Mate
supervisor. The explicit human start command can bootstrap this dedicated pane;
no production installation is performed by tests.

An inbox body is `{"method":"status","params":{"id":"task","history":true}}`.
The returned task has `remote_home` and `confirmation`. A parent human operation
uses `{"method":"approve","params":{"id":"task","sha":"...","brief":"..."},
"confirmation":"..."}` with that exact revision. The parent UI must show the
remote identity, full scope and SHA before producing such a request. The model
must never be given a tool that can manufacture human operations. The primary
`mate_remote` operation allowlist enforces that boundary; `/mate-remote` supplies
confirmations only through its human TUI command.
Private memory is not accessible through the parent inbox. Ordinary worker tools
and resident control sockets stay host-local and retain the existing checks.

## Internal transport contract (not an end-user setup workflow)

The operator supplies private account-owned JSON files (0600), outside worker
repositories. A local route contains exactly `host` (an OpenSSH alias), `home`
(remote UUID), `primary` (primary UUID), and `outbox` (absolute existing primary
journal path). Use `mate_remote_primary.create(route)` only during explicit
provisioning; the running transport refuses missing or differently bound outboxes.
A receiver config contains exactly
`journal` (absolute existing inbox database path), `home` and `primary`, plus optional
`supervisor` with provider/model/effort for explicit bootstrap. Missing
journals are refused, never provisioned by an incoming request. The inbox is
explicitly created through the storage API during controlled provisioning.

An authorized SSH key should use `restrict` and a fixed command invoking an absolute
Python interpreter and `bin/mate_remote_transport.py receive CONFIG`. The key must
not have an unrestricted alternate login route. The receiver checks that
`SSH_ORIGINAL_COMMAND` is `mate-remote-v1`; it never evaluates that string. This is
SSH account/key authentication, not a claim that the JSON primary UUID authenticates
a user. No SSH settings, authorized keys, passwords or live host configuration are
changed by these source changes.

`python3 bin/mate_remote_transport.py transport ROUTE` reads newline-delimited
`{"method":"hello","params":{}}`-shaped requests and returns one JSON response per
line. One process handles one route; the primary extension owns independent
processes. Read polling can reconnect the local channel after failure with bounded
backoff, but never resubmits a mutation. A timeout/invalid reply/nonzero SSH exit returns
`uncertain: true`, never proof of nondelivery. A verified receiver validation error
has `ok: false` with no uncertainty marker. `hello` proves only protocol and journal
binding, **not** toolchain/Herdr/credentials readiness. The caller must journal a
stable inbox request before sending `accept`; the transport journals that supplied
identity and does not create new task/request identities or retry submissions. It creates only an ephemeral
call correlation ID to validate each response.

The `transport` CLI now journals `accept` itself before calling the low-level sender.
Local `accept`, `delivery` and `reconcile` return `{home, delivery}`; inspect
`delivery.state` rather than assuming that receiving a response proves delivery.
`delivery` reads the local journal only; `reconcile` reads remote status only.
`staged` can be sent once; `sending` after a restart is uncertain, not unsent.
`rejected` records a verified refusal of this delivery, not proof that the remote
has never performed any earlier work under a conflicting ID. No state in this
outbox grants approval or authorizes a new worker. Read-only `hello`, `status`,
`outcomes` retain the verified wire response shape; raw `send()` remains an internal
primitive whose caller must journal first for mutations.

## Required contract

- Register an explicit SSH route, remote Mate code root, remote Mate home and
  remote repository for each project. Never infer a remote path from a local
  path, selected Herdr UI machine, or repository name. Provision credentials,
  Git checkout, Treehouse, Pi and Herdr on the host separately; no credential
  or worktree copying.
- Keep `/mate-approve` on the local human-facing supervisor. An approval pins
  scope and SHA; the remote host must prove it has **that exact commit** before
  acquiring a Treehouse lease. A remote task may not invent its own approval.
- Give each request a stable task/attempt identity. The remote endpoint must
  persist the identity and either return the original receipt or refuse a
  conflicting replay. Journal locally *before* crossing SSH. On an ambiguous
  reply, keep `attention` and reconcile the same remote identity; never
  dispatch a new worker or fall back to Local.
- Execute remote Git, Treehouse and Mate worker logic on the remote host, not
  through `herdr --machine` alone. The local side may use Herdr's saved machine
  CLI for read-only pane visibility, but Herdr selection does not retarget
  local process execution.
- Pull remote result and event records into the local durable task history with
  stable sequence identities; only acknowledge a remote record after the local
  commit. Unreachable means unknown, not stopped or complete. Preserve human-only
  completion, exact lease checks and separate confirmed resource cleanup.
- Refuse cross-host `same_tab_as` and remote continuation until exact endpoint,
  resident-control and lease checks can be performed on the owning host. Do not
  migrate existing local tasks or change their behavior.

Implementation sequence: remote-owned idempotent request/status protocol and
fixtures; local route/approval/dispatch journal; report/event reconciliation;
continuation and human-confirmed completion/cleanup. Test against disposable
remote homes and SSH/Herdr fixtures, never the real `data/` or live workers.
