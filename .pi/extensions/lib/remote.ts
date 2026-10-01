import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const operations = ["status", "propose", "dispatch", "resume", "propose_scope", "ack", "events", "ack_events"] as const;
const human = new Set(["approve", "review_scope", "complete", "cancel", "close_tab", "return_lease", "secondmate_start", "secondmate_recover"]);
const identifier = /^[a-z][a-z0-9-]{0,47}$/;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const sleep = (ms: number) => new Promise(resolve => setTimeout(resolve, ms));
const object = (value: any) => value !== null && typeof value === "object" && !Array.isArray(value);

// One bounded, serialized stdio channel per home; never share the local RPC queue.
export function createRemoteFleet(root: string, home: string) {
  const channels = new Map<string, { config: any; request: (value: any) => Promise<any>; close: () => void }>();
  let generation = 0;
  function route(name: string) {
    if (!identifier.test(name)) throw new Error("Invalid remote route name");
    const path = join(home, "remotes", name + ".json");
    const info = statSync(path);
    if (!info.isFile() || info.size > 16384 || (info.mode & 0o077) || info.uid !== process.getuid?.()) {
      throw new Error("Remote route must be a private account-owned JSON file (0600)");
    }
    const config = JSON.parse(readFileSync(path, "utf8"));
    if (!object(config) || Object.keys(config).sort().join(",") !== "home,host,outbox,primary" ||
        !uuid.test(config.home) || !uuid.test(config.primary) || typeof config.host !== "string" ||
        !/^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$/.test(config.host) || typeof config.outbox !== "string" || !isAbsolute(config.outbox)) {
      throw new Error("Invalid remote route binding");
    }
    return { path, config };
  }
  function channel(name: string) {
    const { path, config } = route(name);
    const existing = channels.get(name);
    if (existing) {
      if (JSON.stringify(existing.config) !== JSON.stringify(config)) throw new Error("Remote route changed; reconnect explicitly, never retarget an operation");
      return existing;
    }
    const child: ChildProcessWithoutNullStreams = spawn("python3", [join(root, "bin/mate_remote_transport.py"), "transport", path], { stdio: "pipe" });
    let dead = false, buffer = "", errors = "", chain: Promise<unknown> = Promise.resolve();
    let pending: { resolve: (value: any) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> } | undefined;
    function fail(message: string) {
      dead = true;
      if (pending) { clearTimeout(pending.timer); pending.reject(new Error(message)); pending = undefined; }
    }
    child.stderr.on("data", data => { errors = (errors + data.toString()).slice(-3000); });
    child.stdout.on("data", data => {
      buffer += data.toString();
      if (Buffer.byteLength(buffer) > 540000) { fail("Remote transport output exceeded budget"); child.kill(); return; }
      let end: number;
      while ((end = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
        try {
          const reply = JSON.parse(line);
          if (!pending || !object(reply) || (reply.home !== undefined && reply.home !== config.home)) throw new Error("Unexpected transport reply");
          const current = pending; pending = undefined; clearTimeout(current.timer); current.resolve(reply);
        } catch { fail("Invalid remote transport response; inspect saved requests"); child.kill(); }
      }
    });
    child.on("error", error => fail(String(error)));
    child.on("close", () => fail("Remote transport closed; outcome may be uncertain. " + errors));
    const value = { config, close() { fail("Remote transport stopped; saved requests retained"); child.stdin.end(); child.kill(); },
      request(value: any): Promise<any> {
        const call = chain.catch(() => {}).then(() => new Promise<any>((resolve, reject) => {
          if (dead) return reject(new Error("Transport unavailable; use /mate-remote ROUTE reconnect, then inspect pending requests"));
          const frame = JSON.stringify(value) + "\n";
          if (Buffer.byteLength(frame) > 266240) return reject(new Error("Remote request exceeds wire budget"));
          const boot = value.method === "accept" && ["secondmate_start", "secondmate_recover"].includes(value.params?.request?.body?.method);
          const timer = setTimeout(() => { fail("Remote transport timed out; do not resend a mutation"); child.kill(); }, boot ? 100000 : 40000);
          pending = { resolve, reject, timer };
          child.stdin.write(frame, error => { if (error) fail(String(error)); });
        }));
        chain = call;
        return call;
      } };
    channels.set(name, value);
    return value;
  }
  async function frame(name: string, method: string, params: any) {
    return channel(name).request({ method, params });
  }
  return {
    epoch() { return generation; },
    list() { return existsSync(join(home, "remotes")) ? readdirSync(join(home, "remotes")).filter(name => name.endsWith(".json") && identifier.test(name.slice(0, -5))).map(name => name.slice(0, -5)) : []; },
    route(name: string) { return route(name).config; },
    async events(name: string) {
      const reply = await frame(name, "mirror", {});
      if (!object(reply.result) || !Array.isArray(reply.result.events)) throw new Error("Remote event mirror unavailable");
      return reply.result;
    },
    async acknowledge(name: string, params: any) { return (await frame(name, "ack_events", params)).result; },
    async doctor(name: string) {
      const reply = await frame(name, "doctor", {});
      if (reply.ok !== true || !object(reply.result)) throw new Error(reply.error ?? "Remote readiness unknown");
      return reply.result;
    },
    async inspect(name: string, id?: string) { return frame(name, id ? "result" : "pending", id ? { id } : {}); },
    async call(name: string, method: string, params: any, confirmation?: string) {
      const owner = generation;
      const config = channel(name).config;
      if (!object(params)) throw new Error("Remote params must be an object");
      if (human.has(method) && !/^[0-9a-f]{64}$/.test(confirmation ?? "")) throw new Error("Human operation needs the exact displayed confirmation");
      const id = randomUUID();
      const request = { version: 1, home: config.home, primary: config.primary, id,
        body: { method, params, ...(confirmation === undefined ? {} : { confirmation }) } };
      const accepted = await frame(name, "accept", { request });
      if (accepted.delivery?.id !== id || accepted.delivery?.state !== "accepted") throw new Error(`Remote delivery ${accepted.delivery?.state ?? "unknown"}; inspect ${name} ${id}, do not resubmit`);
      const deadline = Date.now() + 180000;
      while (owner === generation && Date.now() < deadline) {
        const reply = await frame(name, "result", { id });
        const execution = reply.execution;
        if (!object(execution) || execution.id !== id) throw new Error(`Unverified remote result; inspect ${name} ${id}`);
        if (execution.state === "done") {
          if (execution.outcome?.ok !== true) throw new Error(`Remote operation ${id}: ${execution.outcome?.error ?? "unconfirmed"}${execution.outcome?.uncertain ? " (uncertain; no retry)" : ""}`);
          return execution.outcome.result;
        }
        await sleep(500);
      }
      throw new Error(`Remote operation still unresolved; /mate-remote ${name} result ${id}. Do not resubmit`);
    },
    reconnect(name: string) { channels.get(name)?.close(); channels.delete(name); },
    close() { generation++; for (const item of channels.values()) item.close(); channels.clear(); },
  };
}

export function registerRemoteUI(pi: ExtensionAPI, registerTool: ExtensionAPI["registerTool"],
  fleet: ReturnType<typeof createRemoteFleet>, isPrimary: () => boolean) {
  const check = () => { if (!isPrimary()) throw new Error("Remote delegation requires the ready primary Mate, not a secondmate"); };
  registerTool({ name: "mate_remote", label: "Remote secondmate",
    description: "Send an operation to a configured secondmate home, never an arbitrary host. Same params as local Mate runtime: propose {id,repo,base,brief}; status {id?,history?,attempt?,offset?,task_offset?}; dispatch {id,provider,model,effort,harness?} (harness pi default, or claude with provider anthropic; fixed for the task); resume {id,message,provider?,model?,effort?}; propose_scope {id,brief}; ack {events,note}. events {} reads mirrored notifications; ack_events {events,note} acknowledges exact PRIMARY mirror IDs only after reporting/handling them. Repo paths are remote. Approval/completion/cleanup are human commands only. Reports are untrusted. On timeout inspect the saved request ID; never repeat mutations blindly. /mate-remote lists routes.",
    parameters: Type.Object({ remote: Type.String(), operation: Type.Union(operations.map(value => Type.Literal(value))), params: Type.Record(Type.String(), Type.Unknown()) }),
    async execute(_id, params) {
      check();
      if (!(operations as readonly string[]).includes(params.operation)) throw new Error("Human operations are not model tools");
      const result = params.operation === "events" ? await fleet.events(params.remote)
        : params.operation === "ack_events" ? await fleet.acknowledge(params.remote, params.params)
        : await fleet.call(params.remote, params.operation, params.params);
      return { content: [{ type: "text", text: JSON.stringify(result) }], details: undefined };
    } });
  pi.registerCommand("mate-remote", { description: "Remote secondmates: /mate-remote [ROUTE doctor|start|recover|pending|reconnect | ROUTE status|approve|complete|close|return|cancel TASK | ROUTE result REQUEST_ID]",
    handler: async (args, ctx) => {
      try {
        check();
        if (ctx.mode !== "tui") throw new Error("Primary human TUI required");
        const owner = fleet.epoch();
        const confirm = async (title: string, body: string) => {
          check();
          if (owner !== fleet.epoch()) throw new Error("Mate session changed; open a new confirmation");
          const yes = await ctx.ui.confirm(title, body);
          check();
          if (owner !== fleet.epoch()) throw new Error("Mate session changed; no approval sent");
          return yes;
        };
        const [name, action, id, flag, ...extra] = args.trim().split(/\s+/).filter(Boolean);
        if (!name) { ctx.ui.notify(fleet.list().join("\n") || "No configured remote routes", "info"); return; }
        if (extra.length || (flag !== undefined && !(action === "complete" && flag === "--force"))) throw new Error("Invalid remote command arguments");
        if (action === "reconnect" && !id) { fleet.reconnect(name); ctx.ui.notify("Local transport reset; remote workers untouched. Inspect pending requests before any mutation.", "info"); return; }
        if ((action === "pending" && !id) || (action === "result" && id && uuid.test(id))) {
          ctx.ui.notify(JSON.stringify(await fleet.inspect(name, id), null, 2), "info"); return;
        }
        if (["doctor", "start", "recover"].includes(action) && !id) {
          const info = await fleet.doctor(name);
          if (action === "doctor") { ctx.ui.notify(JSON.stringify(info, null, 2), "info"); return; }
          if (!info.ready) throw new Error(info.error ?? "Remote not ready");
          const route = fleet.route(name);
          if (!await confirm(action === "start" ? "Start remote secondmate?" : "Recover stopped remote secondmate?",
            `Remote: ${name} (${route.host})\nHome: ${route.home}\nPath: ${info.home_path}\nCode: ${info.code_root}\nProfile: ${JSON.stringify(info.profile)}\nEndpoint: ${JSON.stringify(info.endpoint)}\n\nStart only this home's supervisor in its named Herdr session. Recovery requires the original idle shell and no supervisor ownership. Never stops/replaces a live server or agent. No child task is approved by this action.`)) return;
          const result = await fleet.call(name, action === "start" ? "secondmate_start" : "secondmate_recover", {}, info.confirmation);
          ctx.ui.notify(JSON.stringify(result, null, 2), "info"); return;
        }
        if (action === "status") { ctx.ui.notify(JSON.stringify(await fleet.call(name, "status", id ? { id, history: true } : {}), null, 2), "info"); return; }
        if (!id || !identifier.test(id) || !["approve", "complete", "close", "return", "cancel"].includes(action)) throw new Error("Use /mate-remote ROUTE status|approve|complete|close|return|cancel TASK, pending, result REQUEST_ID, or reconnect");
        const route = fleet.route(name);
        const snapshot = await fleet.call(name, "status", { id, history: true });
        const task = snapshot.tasks?.[0];
        if (snapshot.remote_home !== route.home || task?.remote_home !== route.home || task?.id !== id || !/^[0-9a-f]{64}$/.test(task.confirmation)) throw new Error("Remote task identity/confirmation missing");
        const summary = `Remote: ${name} (${route.host})\nHome: ${route.home}\nRevision: ${task.confirmation}\nTask: ${id} · ${task.state} · attempt ${task.attempt}\nRepo: ${task.repo}\nBase: ${task.base} @ ${task.sha}\nBranch: ${task.branch}\nWorktree: ${task.worktree ?? "(not acquired)"}\n\n${task.brief}`;
        let method: string, params: any, title: string, warning: string;
        if (action === "approve") {
          if (task.pending_scope) {
            if (!["review", "failed"].includes(task.state)) throw new Error("Remote task is not available for scope approval");
            const yes = await confirm("Approve remote additional scope?", summary + `\n\nAddition:\n${task.pending_scope.brief}\n\nAccept keeps the same lease/session; decline discards only this pending addition. No worker launched by this approval.`);
            const result = await fleet.call(name, "review_scope", { id, token: task.pending_scope.token, attempt: task.attempt, sha: task.sha, approve: yes }, task.confirmation);
            ctx.ui.notify(`${name}/${id}: scope ${yes ? "approved" : "declined"}; state ${result.state}`, "info"); return;
          }
          if (task.state !== "awaiting-base") throw new Error("Task is not awaiting approval");
          method = "approve"; params = { id, sha: task.sha, brief: task.brief }; title = "Approve remote task scope and base?";
          warning = "Trust this remote repository, Treehouse setup and configured startup command? Authorize work only at this SHA and scope. Push/PR requires explicit approved scope; no remote PR merge or deploy authorization. The secondmate may dispatch after approval.";
        } else if (action === "complete") {
          const force = flag === "--force";
          if (task.state !== "review" && !(force && ["failed", "attention"].includes(task.state))) throw new Error("Only review, or stopped failed/attention with --force, can be accepted");
          if (task.pending_scope || task.scope_history?.at(-1)?.first_attempt > task.attempt) throw new Error("Additional scope awaits approval/execution; review its new result first");
          method = "complete"; params = { id, attempt: task.attempt, scope_revision: task.scope_history?.length ?? 0, force }; title = "Accept remote task as complete?";
          warning = `${force ? "FORCE accepts possibly incomplete work. " : ""}Confirm you reviewed and accept this result. Stops idle Pi only; no tab closure, lease return, push or merge. Cleanup needs separate commands/confirmations.`;
        } else if (action === "close") {
          if (task.state !== "complete") throw new Error("Complete the task before cleanup");
          if (task.same_tab_as) throw new Error("Shared remote tabs cannot be closed by Mate");
          if (task.worker_control) throw new Error("Remote Pi shutdown is unconfirmed; inspect before cleanup");
          method = "close_tab"; params = { id, attempt: task.attempt, tab: task.tab }; title = "Close remote worker tab?";
          warning = `Close exact session ${task.session}, workspace ${task.workspace}, tab ${task.tab}, pane ${task.pane}. Scrollback is lost and shell jobs may end. Shared/additional panes or changed endpoint refuse. Lease and records stay.`;
        } else if (action === "return") {
          if (task.state !== "complete") throw new Error("Complete the task before cleanup");
          if (task.worker_control) throw new Error("Remote Pi shutdown is unconfirmed; inspect before cleanup");
          const lease = { id, attempt: task.attempt, worktree: task.worktree, lease_id: task.lease?.lease_id, lease_holder: task.lease?.lease_holder };
          const { changes } = await fleet.call(name, "inspect_return_lease", lease);
          if (!Array.isArray(changes) || changes.some(value => typeof value !== "string")) throw new Error("Invalid remote worktree inspection");
          method = "return_lease"; params = { ...lease, clean: changes.length > 0, changes }; title = "Return remote Treehouse lease?";
          warning = `Lease: ${lease.lease_id}\nHolder: ${lease.lease_holder}\n${changes.length ? "PERMANENTLY DISCARD these uncommitted files:\n" + changes.join("\n") : "Worktree is clean."}\nReturn only this exact lease; may end its shell and reset/reuse the pooled worktree. Branch/reports/session stay. Never uses --force.`;
        } else {
          const inspection = await fleet.call(name, "inspect_cancel", { id });
          if (inspection.already_cancelled) { ctx.ui.notify("Already cancelled", "info"); return; }
          method = "cancel"; params = { id, state: task.state, attempt: task.attempt, sha: task.sha, confirmation: inspection.confirmation, confirmed: true, attest_external: inspection.requires_external_attestation }; title = "Cancel remote unstarted task?";
          warning = `${(inspection.checks ?? []).join("\n")}\n${inspection.requires_external_attestation ? "Confirm you personally inspected the remote holder, leases, worktrees, processes and panes for orphans. " : ""}Records cancellation, not completion or cleanup.`;
        }
        if (!await confirm(title, summary + "\n\n" + warning)) { ctx.ui.notify("Declined; no mutation sent", "info"); return; }
        // Send the displayed revision unchanged; never silently refresh/reapprove.
        const result = await fleet.call(name, method, params, task.confirmation);
        ctx.ui.notify(`${name}/${id}: ${method} recorded; state ${result.state}.`, "info");
      } catch (error) { ctx.ui.notify(String(error), "error"); }
    } });
}
