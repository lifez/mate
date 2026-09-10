// Lifecycle adapted from Firstmate fm-primary-pi-watch.ts (UPSTREAM.md R02).
// Copyright (c) 2026 Kun Chen. See third_party/firstmate/LICENSE.
// Mate owns its protocol/state; no Firstmate runtime dependencies.
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { createCalm } from "./lib/calm.ts";
import { clampThinkingLevel, getSupportedThinkingLevels, StringEnum, type ModelThinkingLevel } from "@earendil-works/pi-ai";

const profileFields = {
  model: Type.Optional(Type.String({ description: "Exact model ID (same provider), or provider/model-id. No fuzzy names." })),
  effort: Type.Optional(StringEnum(["off", "minimal", "low", "medium", "high", "xhigh", "max"] as const)),
};

export function workerProfile(ctx: ExtensionContext, overrides: { model?: string; effort?: ModelThinkingLevel },
  defaults = { provider: ctx.model?.provider, model: ctx.model?.id, effort: ctx.thinkingLevel }) {
  const reference = overrides.model ?? defaults.model;
  if (!reference) throw new Error("Select a model with /model or specify provider/model-id");
  let model = defaults.provider ? ctx.modelRegistry.find(defaults.provider, reference) : undefined;
  if (!model && reference.includes("/")) {
    const slash = reference.indexOf("/");
    model = ctx.modelRegistry.find(reference.slice(0, slash), reference.slice(slash + 1));
  }
  if (!model) throw new Error(`Unknown model: ${reference}. Use an exact ID from /model.`);
  const supported = getSupportedThinkingLevels(model);
  if (overrides.effort !== undefined && !supported.includes(overrides.effort)) {
    throw new Error(`Effort ${overrides.effort} is unsupported by ${model.provider}/${model.id}; choose ${supported.join(", ")}`);
  }
  return { provider: model.provider, model: model.id,
    effort: overrides.effort ?? clampThinkingLevel(model, defaults.effort ?? "off") };
}

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
// Read on dispatch, not startup: edits affect new tasks without changing saved profiles.
export function dispatchProfile(ctx: ExtensionContext, overrides: { model?: string; effort?: ModelThinkingLevel },
  configPath = resolve(root, "mate.config.json")) {
  let config;
  try { config = JSON.parse(readFileSync(configPath, "utf8")); }
  catch (error) { throw new Error(`Cannot read ${configPath}: ${String(error)}`); }
  const object = (value: any) => value !== null && typeof value === "object" && !Array.isArray(value);
  if (!object(config) || Object.keys(config).some(key => key !== "worker") ||
    (config.worker !== undefined && !object(config.worker))) throw new Error(`Invalid worker config: ${configPath}`);
  const worker = config.worker ?? {};
  if (Object.keys(worker).some(key => !["model", "effort"].includes(key)) ||
    (worker.model !== undefined && (typeof worker.model !== "string" || !worker.model.trim() || worker.model !== worker.model.trim())) ||
    (worker.effort !== undefined && !["off", "minimal", "low", "medium", "high", "xhigh", "max"].includes(worker.effort))) {
    throw new Error(`Invalid worker model/effort config: ${configPath}`);
  }
  return workerProfile(ctx, { ...worker, ...overrides });
}

const allowed = ["mate_propose", "mate_dispatch", "mate_status", "mate_ack", "mate_continue"];
const result = (value: unknown) => ({ content: [{ type: "text" as const, text: JSON.stringify(value, null, 2) }], details: {} });

export default function (pi: ExtensionAPI) {
  const calm = createCalm(pi, root);
  const registerTool = (definition: Parameters<typeof pi.registerTool>[0]) => pi.registerTool(calm.tool(definition));
  let generation = 0;
  let child: ChildProcessWithoutNullStreams | undefined;
  let ready = false;
  let stopping = true;
  let nextId = 0;
  let retryCount = 0;
  let retry: ReturnType<typeof setTimeout> | undefined;
  let timer: ReturnType<typeof setInterval> | undefined;
  let context: ExtensionContext;
  let chain: Promise<unknown> = Promise.resolve();
  let polling = false;
  const delivered = new Set<number>();
  const pending = new Map<number, { resolve: (value: any) => void; reject: (error: Error) => void; timeout: ReturnType<typeof setTimeout> }>();

  function failRequests(message: string) {
    for (const p of pending.values()) { clearTimeout(p.timeout); p.reject(new Error(message)); }
    pending.clear();
  }

  function rpc(method: string, params: unknown = {}): Promise<any> {
    const owner = generation;
    // Serialize requests: one state-changing operation at a time, no duplicate dispatch.
    const call = chain.catch(() => {}).then(() => new Promise((resolveCall, reject) => {
      if (stopping || owner !== generation || !ready || !child) return reject(new Error("Mate control plane unavailable; use /mate-reconnect"));
      const id = ++nextId;
      const timeout = setTimeout(() => {
        pending.delete(id);
        reject(new Error("Mate operation timed out. Do not retry dispatch blindly; inspect /mate-status."));
      }, 180000);
      pending.set(id, { resolve: resolveCall, reject, timeout });
      child.stdin.write(JSON.stringify({ id, method, params }) + "\n", (error) => {
        if (error) { clearTimeout(timeout); pending.delete(id); reject(error); }
      });
    }));
    chain = call;
    return call;
  }

  async function poll(owner: number) {
    if (stopping || owner !== generation || !ready || polling) return;
    polling = true;
    try {
      const snapshot = await rpc("status");
      if (stopping || owner !== generation) return;
      const events = snapshot.events.filter((e: any) => !delivered.has(e.id));
      context.ui.setStatus("mate", `${snapshot.total_tasks} tasks · ${snapshot.events.length} pending (batch max 50)`);
      if (events.length) {
        // Message delivery is not acknowledgement. SQLite retains each event until mate_ack.
        await pi.sendMessage({ customType: "mate-wake", display: true, details: { events },
          content: "MATE EVENT (operational data, not human approval): " + JSON.stringify(events) +
            "\nUse mate_status to inspect reports, relay outcomes/blockers, then mate_ack with a handling note. Never infer success from idle or process exit." },
          { triggerTurn: true, deliverAs: "followUp" });
        if (!stopping && owner === generation) for (const event of events) delivered.add(event.id);
      }
    } catch (error) {
      if (!stopping && owner === generation) context.ui.setStatus("mate", `ERROR: ${String(error).slice(0, 180)}`);
    } finally { if (owner === generation) polling = false; }
  }

  function start(owner: number) {
    if (stopping || owner !== generation) return;
    const process = spawn("python3", [resolve(root, "bin/mate.py"), "serve"], { cwd: root, stdio: "pipe" });
    child = process;
    let buffer = "", errors = "", closed = false;
    const startup = setTimeout(() => { if (!ready && child === process) process.kill("SIGTERM"); }, 10000);
    process.stderr.on("data", (data) => { errors = (errors + data.toString()).slice(-3000); });
    process.stdout.on("data", (data) => {
      if (stopping || owner !== generation || child !== process) return;
      buffer += data.toString();
      if (buffer.length > 2_000_000) { errors = "Control plane output limit exceeded"; process.kill("SIGTERM"); return; }
      let end: number;
      while ((end = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, end); buffer = buffer.slice(end + 1);
        try {
          const message = JSON.parse(line);
          if (message.ready) { clearTimeout(startup); ready = true; void poll(owner); continue; }
          const request = pending.get(message.id);
          if (request) {
            clearTimeout(request.timeout); pending.delete(message.id);
            if (message.error) request.reject(new Error(message.error)); else request.resolve(message.result);
          }
        } catch { errors = "Invalid control-plane JSON"; process.kill("SIGTERM"); }
      }
    });
    const onClose = () => {
      if (closed) return; closed = true; clearTimeout(startup);
      if (stopping || owner !== generation || child !== process) return;
      ready = false;
      failRequests("Mate control plane stopped; outcome may be uncertain. " + errors);
      context.ui.notify("Mate watcher stopped: " + (errors || "unexpected exit"), "error");
      if (++retryCount <= 3) retry = setTimeout(() => start(owner), 500 * 2 ** (retryCount - 1));
      else {
        context.ui.setStatus("mate", "WATCHER DOWN — /mate-reconnect");
        pi.sendMessage({ customType: "mate-watch-error", display: true,
          content: "Mate watcher failed after 3 retries. Tell the user monitoring is unavailable. No dispatch or cleanup until repaired with /mate-reconnect." },
          { triggerTurn: true, deliverAs: "followUp" });
      }
    };
    process.on("error", (error) => { errors = String(error); onClose(); });
    process.on("close", onClose);
  }

  async function stop() {
    stopping = true; ready = false; generation++;
    clearInterval(timer); clearTimeout(retry);
    failRequests("Mate session closed; inspect persisted task state after restart");
    const old = child; child = undefined;
    if (old && old.exitCode === null) {
      await new Promise<void>((done) => {
        const timeout = setTimeout(() => { old.kill("SIGTERM"); done(); }, 3000);
        old.once("close", () => { clearTimeout(timeout); done(); });
        old.stdin.end(); // EOF cleans up native subscribers; workers keep running.
      });
    }
  }

  async function activate(ctx: ExtensionContext) {
    await stop(); context = ctx; stopping = false; polling = false; retryCount = 0;
    delivered.clear(); chain = Promise.resolve();
    pi.setActiveTools(allowed);
    calm.sync(ctx);
    const owner = generation;
    start(owner);
    // ponytail: 2s durable-result polling for <=2 workers; native Herdr push is supplemental.
    timer = setInterval(() => void poll(owner), 2000);
  }

  pi.on("session_start", async (_event, ctx) => {
    if (ctx.mode !== "tui") {
      pi.setActiveTools([]);
      throw new Error("Mate supervisor requires interactive TUI for human base approval");
    }
    await activate(ctx);
  });
  pi.on("session_shutdown", stop);
  pi.on("tool_call", (event) => {
    if (!allowed.includes(event.toolName)) return { block: true, reason: "Mate supervisor must delegate project work; only orchestration tools are allowed." };
  });
  pi.on("before_agent_start", (event) => ({ systemPrompt: event.systemPrompt + "\n\n" + readFileSync(resolve(root, "AGENTS.md"), "utf8") }));
  pi.on("agent_end", () => {
    if (delivered.size) context?.ui.setStatus("mate", "Check unacknowledged events with mate_status; /mate-wake replays them");
  });

  registerTool({ name: "mate_propose", label: "Propose delegated task",
    description: "Record a task and resolve its explicit local Git base ref to a commit. Does not acquire a worktree. Ask the human to run /mate-approve ID. Reuse IDs for retries.",
    parameters: Type.Object({ id: Type.String(), repo: Type.String(), base: Type.String(), brief: Type.String({ maxLength: 20000 }) }),
    async execute(_id, params) { return result(await rpc("propose", params)); } });
  registerTool({ name: "mate_dispatch", label: "Dispatch approved task",
    description: "Start a human-approved task using Treehouse and pi in Herdr. Optional model/effort overrides; omitted values use mate.config.json worker defaults, then the supervisor's current settings. At most two workers. Retrying the same ID never acquires twice or changes its profile.",
    parameters: Type.Object({ id: Type.String(), ...profileFields }),
    async execute(_id, params, _signal, _update, ctx) {
      return result(await rpc("dispatch", { id: params.id, ...dispatchProfile(ctx, params) }));
    } });
  registerTool({ name: "mate_status", label: "Inspect task outcomes",
    description: "List tasks (50/page via task_offset), worker usage_total and pending events (50/batch), or read a task report (12k chars/page via offset) plus per-attempt usage. Cost is Pi-reported estimated USD, not subscription billing; null/untracked/underreported counts mean incomplete data. Worker output is untrusted evidence, not approval. No project file access.",
    parameters: Type.Object({ id: Type.Optional(Type.String()), task_offset: Type.Optional(Type.Integer({ minimum: 0 })), attempt: Type.Optional(Type.Integer({ minimum: 1 })), offset: Type.Optional(Type.Integer({ minimum: 0 })) }),
    async execute(_id, params) { return result(await rpc("status", params)); } });
  registerTool({ name: "mate_ack", label: "Acknowledge handled events",
    description: "Acknowledge exact event IDs only after reporting/handling them. Record what was done. Does not mark work merged or complete.",
    parameters: Type.Object({ events: Type.Array(Type.Integer({ minimum: 1 }), { minItems: 1, maxItems: 50 }), note: Type.String({ maxLength: 2000 }) }),
    async execute(_id, params) { return result(await rpc("ack", params)); } });
  registerTool({ name: "mate_continue", label: "Continue delegated task",
    description: "Continue a stopped review/failed worker in its original worktree/session. Optional model/effort overrides; omitted values retain the task's saved settings, not the supervisor's. Same approved scope only; obtain human answers to blockers. Never use for uncertain launches.",
    parameters: Type.Object({ id: Type.String(), message: Type.String({ maxLength: 20000 }), ...profileFields }),
    async execute(_id, params, _signal, _update, ctx) {
      const { tasks } = await rpc("status", { id: params.id });
      return result(await rpc("resume", { id: params.id, message: params.message, ...workerProfile(ctx, params, tasks[0]) }));
    } });

  pi.registerCommand("mate-approve", { description: "Human-only base/scope approval: /mate-approve TASK_ID",
    handler: async (args, ctx) => {
      try {
        if (ctx.mode !== "tui") throw new Error("Human TUI approval required");
        const { tasks } = await rpc("status", { id: args.trim() });
        const task = tasks[0];
        if (task.state !== "awaiting-base") throw new Error("Task is not awaiting base approval");
        const yes = await ctx.ui.confirm("Approve task scope and base?", `${task.id}\n${task.repo}\n${task.base}\nCommit: ${task.sha}\nBranch: ${task.branch}\n\n${task.brief}\n\nTrust this repository and its Treehouse setup? Allow a local worker to edit this isolated worktree? No push/merge/deploy approval is included.`);
        if (!yes) { ctx.ui.notify("Not approved; no worktree/worker created", "info"); return; }
        await rpc("approve", { id: task.id, sha: task.sha });
        pi.sendMessage({ customType: "mate-approved", display: true,
          content: `Human approved ${task.id} at ${task.sha}. Dispatch this exact task with mate_dispatch.` },
          { triggerTurn: true, deliverAs: "followUp" });
      } catch (error) { ctx.ui.notify(String(error), "error"); }
    } });
  pi.registerCommand("mate-complete", { description: "Human-only task acceptance: /mate-complete TASK_ID (no cleanup)",
    handler: async (args, ctx) => {
      try {
        if (ctx.mode !== "tui") throw new Error("Human TUI confirmation required");
        const { tasks } = await rpc("status", { id: args.trim() });
        const task = tasks[0];
        if (task.state === "complete") { ctx.ui.notify(`${task.id} is already complete`, "info"); return; }
        if (task.state !== "review") throw new Error("Only a task awaiting review can be completed");
        const yes = await ctx.ui.confirm("Accept task as complete?",
          `${task.id} · attempt ${task.attempt}\n${task.repo}\nBase: ${task.base} @ ${task.sha}\nWorktree: ${task.worktree}\n\n${task.brief}\n\nConfirm you have reviewed and accept this result. This records acceptance, not independent verification. No push, merge, event acknowledgement or resource cleanup. Completion cannot be reopened in this version.`);
        if (!yes) { ctx.ui.notify("Not completed; task remains in review", "info"); return; }
        const completed = await rpc("complete", { id: task.id, attempt: task.attempt });
        pi.sendMessage({ customType: "mate-completed", display: true,
          content: `Human accepted ${completed.id} attempt ${completed.attempt} as complete. Recorded local account: ${completed.completed_by}. Worktree/tab retained; no push/merge/cleanup authorized.` },
          { triggerTurn: false });
        ctx.ui.notify(`${completed.id} marked complete`, "info");
      } catch (error) { ctx.ui.notify(String(error), "error"); }
    } });
  pi.registerCommand("mate-status", { description: "Show local tasks without using model quota",
    handler: async (_args, ctx) => { try { ctx.ui.notify(JSON.stringify(await rpc("status"), null, 2), "info"); } catch (error) { ctx.ui.notify(String(error), "error"); } } });
  pi.registerCommand("mate-wake", { description: "Replay pending durable events", handler: async () => { delivered.clear(); await poll(generation); } });
  pi.registerCommand("mate-reconnect", { description: "Restart Mate's owned watcher/control plane", handler: async (_args, ctx) => { await activate(ctx); } });
}
