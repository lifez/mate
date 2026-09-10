// Adapted home-persistent preference/self-rendering pattern from Firstmate fm-calm.ts.
// Copyright (c) 2026 Kun Chen. See UPSTREAM.md R07 and third_party/firstmate/LICENSE.
// Presentation only: no tool execution, context, event delivery or acknowledgement changes.
import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import type { ExtensionAPI, ExtensionContext, ToolDefinition } from "@earendil-works/pi-coding-agent";
import { Text, type Component } from "@earendil-works/pi-tui";

type Output = { content: Array<{ type: string; text?: string }> };
type Row = { routine?: boolean };

// Fail open: only positively recognized routine results can disappear.
export function routineResult(name: string, result: Output): boolean {
  if (!result.content.length || result.content.some(part => part.type !== "text")) return false;
  let data;
  try { data = JSON.parse(result.content.map(part => part.text ?? "").join("\n")); }
  catch { return false; }
  if (!data || typeof data !== "object" || data.error || data.report) return false;
  if (name === "mate_ack") return Object.keys(data).length === 1 && Array.isArray(data.acknowledged) && data.acknowledged.length > 0 && data.acknowledged.every(Number.isInteger);
  if (name === "mate_dispatch" || name === "mate_continue") return data.state === "launching" || data.state === "running";
  if (name === "mate_status") return Array.isArray(data.events) && data.events.length === 0 &&
    Array.isArray(data.tasks) && data.tasks.every((task: any) => task && !task.error && ["approved", "launching", "running"].includes(task.state));
  return false; // Proposals, approval requests, reports, failures and unknown shapes stay visible.
}

export function createCalm(pi: ExtensionAPI, root: string) {
  const preference = resolve(process.env.MATE_HOME || resolve(root, "data"), "calm");
  const load = () => {
    try { return readFileSync(preference, "utf8").trim() === "on"; }
    catch { return false; }
  };
  let active = load();
  const persist = (enabled: boolean) => {
    mkdirSync(dirname(preference), { recursive: true, mode: 0o700 });
    const temporary = `${preference}.${process.pid}.${randomUUID()}.tmp`;
    try {
      writeFileSync(temporary, enabled ? "on\n" : "off\n", { flag: "wx", mode: 0o600 });
      renameSync(temporary, preference);
    } finally { rmSync(temporary, { force: true }); }
  };
  const status = (ctx: ExtensionContext) => ctx.ui.setStatus("mate-calm", active ? "calm on" : undefined);
  const component = (body: () => string | null): Component => ({
    render(width) { const text = body(); return text === null ? [] : new Text(text, 0, 0).render(width); },
    invalidate() {}, // No cached rendering: toggle/theme changes apply to existing rows too.
  });

  pi.registerCommand("calm", {
    description: "Calm presentation: /calm [on|off|status]. Reports, blockers, errors and approval dialogs stay visible.",
    handler: async (args, ctx) => {
      if (ctx.mode !== "tui") { ctx.ui.notify("Calm requires interactive pi", "warning"); return; }
      const option = args.trim().toLowerCase();
      if (option === "status") { ctx.ui.notify(`Calm ${active ? "on" : "off"}`, "info"); return; }
      if (!["", "on", "off"].includes(option)) { ctx.ui.notify("Usage: /calm [on|off|status]", "warning"); return; }
      try {
        const enabled = option ? option === "on" : !active;
        persist(enabled); // A failed save must not change the live preference.
        active = enabled;
        status(ctx);
        // Public Pi API: redraw restored tool/custom-message rows without restarting the watcher.
        const expanded = ctx.ui.getToolsExpanded();
        ctx.ui.setToolsExpanded(!expanded);
        ctx.ui.setToolsExpanded(expanded);
        ctx.ui.notify(`Calm ${active ? "on" : "off"}`, "info");
      } catch (error) { ctx.ui.notify(`Calm preference failed: ${String(error)}`, "error"); }
    },
  });

  pi.registerMessageRenderer("mate-approved", (message, _options, theme) =>
    component(() => active ? null : theme.fg("customMessageText", typeof message.content === "string" ? message.content : JSON.stringify(message.content))));
  pi.registerMessageRenderer<{ events?: Array<{ id: number; task: string; kind: string }> }>("mate-wake", (message, _options, theme) =>
    component(() => {
      const events = message.details?.events;
      if (active && Array.isArray(events) && events.length && events.every(e => e && e.kind === "report" && typeof e.task === "string" && Number.isInteger(e.id))) {
        return theme.fg("accent", "Worker report ready (not verified): " + events.map(e => `#${e.id} ${e.task}`).join(", "));
      }
      return theme.fg("customMessageText", typeof message.content === "string" ? message.content : JSON.stringify(message.content));
    }));

  return {
    sync(ctx: ExtensionContext) { active = load(); status(ctx); },
    tool<T extends ToolDefinition<any, any, any>>(definition: T): T {
      return { ...definition, renderShell: "self",
        renderCall(args, theme, context) {
          const row = context.state as Row;
          return component(() => active && row.routine ? null : theme.fg("toolTitle", definition.name + (args.id ? ` ${args.id}` : "")));
        },
        renderResult(result, options, theme, context) {
          const row = context.state as Row;
          row.routine = !context.isError && !options.isPartial && routineResult(definition.name, result);
          const text = result.content.filter(part => part.type === "text").map(part => part.text).join("\n");
          const lines = text.split("\n");
          const preview = options.expanded || lines.length <= 10 ? text : lines.slice(0, 10).join("\n") + `\n… ${lines.length - 10} more lines (expand tool output)`;
          return component(() => active && row.routine ? null : theme.fg(context.isError ? "error" : "toolOutput", preview));
        },
      };
    },
  };
}
