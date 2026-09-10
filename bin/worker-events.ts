// Mate-owned bridge: native Pi owns the terminal; Python owns the durable report.
import { writeSync } from "node:fs";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

export default function (pi: ExtensionAPI) {
  const descriptor = process.env.MATE_EVENT_FD;
  // Keep the descriptor available across Pi's /reload; subprocesses do not inherit it by default.
  if (!descriptor || !/^\d+$/.test(descriptor) || Number(descriptor) < 3) throw new Error("Missing Mate event descriptor");
  const fd = Number(descriptor);
  let failed = false;
  const send = (event: unknown, ctx: ExtensionContext) => {
    if (failed) return;
    try {
      const data = Buffer.from(JSON.stringify(event) + "\n");
      if (data.length > 4 * 1024 * 1024) throw new Error("Pi event exceeded 4 MiB");
      for (let offset = 0; offset < data.length;) offset += writeSync(fd, data, offset, data.length - offset);
    } catch (error) {
      failed = true;
      process.exitCode = 1;
      ctx.ui.notify(`Mate event bridge failed: ${String(error)}`, "error");
      ctx.abort();
      ctx.shutdown();
    }
  };
  for (const name of ["agent_start", "message_update", "message_end", "tool_execution_start", "tool_execution_end"] as const) {
    pi.on(name, (event, ctx) => { send(event, ctx); });
  }
  pi.on("agent_settled", (event, ctx) => {
    if (!ctx.isIdle() || ctx.hasPendingMessages()) return;
    send(event, ctx); // Not agent_end: retries/compaction/follow-ups must finish first.
    ctx.shutdown(); // One delegated run per attempt; continuation reopens this saved session.
  });
}
