// Mate-owned bridge: Pi owns the terminal; Python admits rounds and publishes reports.
import { readSync, writeSync } from "node:fs";
import { createServer, type Server } from "node:net";
import { resolve } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  const descriptor = (name: string) => {
    const value = process.env[name];
    if (!value || !/^\d+$/.test(value) || Number(value) < 3) throw new Error(`Missing ${name}`);
    return Number(value);
  };
  // Keep both descriptors across /reload; tool subprocesses do not inherit them.
  const fd = descriptor("MATE_EVENT_FD"), replyFd = descriptor("MATE_REPLY_FD");
  const control = JSON.parse(process.env.MATE_WORKER_CONTROL || "null");
  if (!control?.socket || !control?.generation) throw new Error("Missing Mate worker control identity");
  let failed = false, admitted = false, transferring = false, closing = false;
  let brief = "", server: Server | undefined;
  let attempt = Number(process.env.MATE_ATTEMPT);
  const fail = (error: unknown, ctx: ExtensionContext) => {
    failed = true;
    process.exitCode = 1;
    ctx.ui.notify(`Mate event bridge failed: ${String(error)}`, "error");
    ctx.abort();
    ctx.shutdown();
  };
  const send = (event: unknown) => {
    if (failed) throw new Error("Mate event bridge unavailable");
    const data = Buffer.from(JSON.stringify(event) + "\n");
    if (data.length > 4 * 1024 * 1024) throw new Error("Pi event exceeded 4 MiB");
    for (let offset = 0; offset < data.length;) offset += writeSync(fd, data, offset, data.length - offset);
  };
  const exchange = (event: unknown, ctx: ExtensionContext) => {
    try {
      send(event);
      const chunks: Buffer[] = [];
      let size = 0;
      while (size < 256 * 1024) {
        const buf = Buffer.alloc(4096);
        const n = readSync(replyFd, buf, 0, buf.length, null);
        if (!n) throw new Error("Worker owner disconnected");
        chunks.push(buf.subarray(0, n)); size += n;
        if (buf[n - 1] === 10) return JSON.parse(Buffer.concat(chunks).toString());
      }
      throw new Error("Worker reply exceeded 256 KiB");
    } catch (error) { fail(error, ctx); throw error; }
  };
  const admit = (ctx: ExtensionContext, request?: string) => {
    if (failed || closing) throw new Error("Worker is shutting down");
    if (resolve(ctx.sessionManager.getSessionFile() || "") !== resolve(process.env.MATE_SESSION_FILE || "")) {
      throw new Error("Worker session identity changed");
    }
    const reply = exchange({ type: "mate_admit", request, provider: ctx.model?.provider,
      model: ctx.model?.id, effort: pi.getThinkingLevel() }, ctx);
    if (!reply.ok) throw new Error(reply.error);
    brief = reply.brief;
    attempt = reply.attempt;
    process.env.MATE_ATTEMPT = String(attempt);
    admitted = true;
  };
  pi.on("input", (_event, ctx) => {
    try {
      if (transferring) throw new Error("Supervisor continuation is being submitted; wait for it to start");
      admit(ctx);
    } catch (error) {
      ctx.ui.notify(String(error), "error");
      return { action: "handled" };
    }
  });
  pi.on("before_agent_start", (event) => ({
    systemPrompt: event.systemPrompt + "\n\nCurrent human-approved task scope (follow-ups cannot expand it):\n" + brief,
  }));
  pi.on("agent_start", (event, ctx) => {
    try { admit(ctx); send(event); }
    catch (error) { admitted = false; ctx.abort(); ctx.ui.notify(String(error), "error"); }
  });
  pi.on("tool_call", () => {
    if (!admitted || failed || closing) return { block: true, reason: "No admitted Mate worker round", terminate: true };
  });
  for (const name of ["message_update", "message_end", "tool_execution_start", "tool_execution_end"] as const) {
    pi.on(name, (event, ctx) => {
      try { send(event); } catch (error) { fail(error, ctx); }
    });
  }
  pi.on("agent_settled", (event, ctx) => {
    if (!ctx.isIdle() || ctx.hasPendingMessages() || !admitted) return;
    exchange({ ...event, reply: true }, ctx);
    admitted = false;
    ctx.ui.notify("Mate report saved; Pi stays open. Follow-ups stay within approved scope.", "info");
  });
  for (const name of ["session_before_switch", "session_before_fork", "session_before_tree"] as const) {
    pi.on(name, (_event, ctx) => {
      ctx.ui.notify("Keep the assigned Mate session; create other sessions outside this worker.", "warning");
      return { cancel: true };
    });
  }
  pi.on("user_bash", () => ({ result: { output: "Ask the worker to run commands within a tracked round, or use a separate shell.", exitCode: 1, cancelled: false, truncated: false } }));
  pi.on("session_start", async (_event, ctx) => {
    // Private, generation-fenced control, not terminal keystrokes or a model tool.
    server = createServer((socket) => {
      let input = "", received = false;
      socket.setEncoding("utf8");
      socket.setTimeout(20000, () => socket.destroy());
      socket.on("error", () => {});
      socket.on("data", async (chunk) => {
        if (received) return;
        input += chunk;
        if (Buffer.byteLength(input) > 256 * 1024) { socket.destroy(); return; }
        if (!input.includes("\n")) return;
        received = true;
        let command: any;
        let owned = false;
        try {
          command = JSON.parse(input);
          if (command.generation !== control.generation || !Number.isInteger(command.attempt) || command.attempt < 1) throw new Error("Stale worker control identity");
          if (failed || closing || transferring || admitted || !ctx.isIdle() || ctx.hasPendingMessages()) throw new Error("Pi is not idle");
          if (command.attempt !== attempt + (command.action === "continue" ? 1 : 0)) throw new Error("Stale worker attempt");
          transferring = owned = true;
          if (command.action === "shutdown") {
            closing = true;
            socket.end(JSON.stringify({ ok: true, generation: control.generation, attempt: command.attempt }) + "\n", () => ctx.shutdown());
            return;
          }
          if (command.action !== "continue" || !/^[a-f0-9]{32}$/.test(command.request) ||
              typeof command.message !== "string" || !command.message.trim() || command.message.length > 20000) throw new Error("Invalid continuation");
          const model = ctx.modelRegistry.find(command.provider, command.model);
          if (!model || !await pi.setModel(model)) throw new Error("Worker model unavailable");
          pi.setThinkingLevel(command.effort);
          // Python verifies the reserved attempt, request and effective profile before any prompt.
          admit(ctx, command.request);
          transferring = false;
          pi.sendUserMessage(command.message);
          socket.end(JSON.stringify({ ok: true, generation: control.generation, attempt: command.attempt }) + "\n");
        } catch (error) {
          socket.end(JSON.stringify({ ok: false, generation: control.generation, attempt: command?.attempt, error: String(error) }) + "\n");
        } finally { if (owned) transferring = false; }
      });
    });
    await new Promise<void>((done, reject) => {
      server!.once("error", reject);
      server!.listen(control.socket, done);
    });
    server.on("error", (error) => fail(error, ctx));
  });
  pi.on("session_shutdown", async () => {
    closing = true;
    if (server) await new Promise<void>((done) => server!.close(() => done()));
  });
}
