import { spawn } from "node:child_process";
import { chmodSync, existsSync, lstatSync, mkdirSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const captainStates = new Set(["awaiting-base", "review", "failed", "attention"]);
const underwayStates = new Set(["acquiring", "launching", "running"]);

const esc = (value: unknown) => String(value ?? "").replace(/[&<>"']/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]!);

function commandFor(task: any) {
  if (task.scope_pending || task.state === "awaiting-base") return `/mate-approve ${task.id}`;
  if (task.state === "review") return `/mate-complete ${task.id}`;
  return `/mate-status`;
}

function card(task: any, tone: string) {
  const usage = task.usage_total ?? {};
  const cost = usage.estimated_cost_usd == null ? "unknown" : `$${Number(usage.estimated_cost_usd).toFixed(4)}`;
  const detail = task.error || (task.scope_pending ? "Additional scope awaits your approval." : "No blocker recorded.");
  const command = commandFor(task);
  return `<article class="card card-border bg-base-100 shadow-sm task" data-state="${esc(task.state)}">
    <div class="card-body gap-3">
      <div class="flex flex-wrap items-center gap-2"><h3 class="card-title text-lg">${esc(task.id)}</h3><span class="badge badge-${tone} badge-soft">${esc(task.state)}</span>${task.scope_pending ? '<span class="badge badge-warning">scope pending</span>' : ""}</div>
      <p class="text-sm opacity-70">${esc(task.project || "repository unavailable")} · attempt ${esc(task.attempt)}</p>
      <p>${esc(detail)}</p>
      <dl class="grid grid-cols-2 gap-x-4 gap-y-1 text-xs opacity-75">
        <dt>Base</dt><dd class="font-mono">${esc(task.base || "—")} ${task.sha ? `@ ${esc(String(task.sha).slice(0, 12))}` : ""}</dd>
        <dt>Worker</dt><dd>${esc(task.model || "not selected")} · ${esc(task.effort || "—")}</dd>
        <dt>Usage</dt><dd>${esc(usage.output_tokens ?? 0)} output tokens · ${esc(cost)}</dd>
      </dl>
      <div class="card-actions justify-end"><button class="btn btn-sm btn-outline copy" data-copy="${esc(command)}">Copy ${esc(command)}</button></div>
    </div>
  </article>`;
}

function empty(text: string) {
  return `<p class="rounded-box border border-dashed border-base-content/20 p-5 opacity-60">${esc(text)}</p>`;
}

export function renderBearingsBoard(snapshot: any) {
  const tasks = Array.isArray(snapshot?.tasks) ? snapshot.tasks : [];
  const events = Array.isArray(snapshot?.events) ? snapshot.events : [];
  const captain = tasks.filter((task: any) => task.scope_pending || captainStates.has(task.state));
  const landed = tasks.filter((task: any) => task.state === "complete").slice(0, 8);
  const underway = tasks.filter((task: any) => underwayStates.has(task.state));
  const charted = tasks.filter((task: any) => task.state === "approved" ||
    (!captainStates.has(task.state) && !underwayStates.has(task.state) && !["complete", "cancelled"].includes(task.state)));
  const eventRows = events.map((event: any) => `<article class="alert alert-warning alert-soft"><span><strong>${esc(event.task)}</strong> · ${esc(event.kind)} — ${esc(event.note)}</span></article>`).join("");
  const generated = new Date().toISOString();
  return `<!doctype html>
<html lang="en" data-theme="luxury">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mate Bearings</title>
<style>
:root{color-scheme:dark;--bg:#10100f;--surface:#191916;--surface2:#22221d;--text:#f2ead8;--muted:#a79f90;--line:#39362f;--gold:#d4a84f;--green:#70b887;--blue:#73a9d8;--amber:#e4b85f;--red:#d9786f;font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{min-height:100vh;margin:0;color:var(--text);background:radial-gradient(circle at 18% 0%,#4b391b 0,transparent 34rem),var(--bg)}
main{width:min(100% - 2rem,80rem);margin:auto;padding:2rem 0 3rem}header{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:1.5rem;margin-bottom:2rem}h1,h2,h3,p{margin-top:0}h1{margin-bottom:.4rem;font-size:clamp(3rem,8vw,5.5rem);line-height:.9}h2{font-size:1.55rem}.mast{margin-bottom:.8rem;color:var(--gold);font-size:.72rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase}.section{margin-bottom:2.5rem;scroll-margin-top:1rem}
.grid{display:grid;gap:1rem}.grid>*{min-width:0}.flex{display:flex}.flex-wrap{flex-wrap:wrap}.items-center{align-items:center}.gap-2{gap:.5rem}.gap-3{gap:.75rem}.gap-4{gap:1rem}.justify-end{justify-content:flex-end}.font-mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.font-bold,.font-black,strong{font-weight:800}.text-xs{font-size:.75rem}.text-sm{font-size:.875rem}.text-lg{font-size:1.1rem}.opacity-60{color:var(--muted)}.opacity-65,.opacity-70,.opacity-75{color:var(--muted)}
.card,.stats,.alert{border:1px solid var(--line);border-radius:1rem;background:linear-gradient(145deg,var(--surface),var(--surface2));box-shadow:0 16px 40px #0005}.card-body{display:grid;padding:1.2rem}.card-title{margin:0}.card p,.card dd,.alert{overflow-wrap:anywhere}.card dl{display:grid;grid-template-columns:auto minmax(0,1fr);gap:.35rem 1rem;margin:0}.card dt{color:var(--muted)}.card dd{margin:0}.card-actions{display:flex;margin-top:.3rem}
.stats{display:flex;overflow:hidden}.stat{min-width:7rem;padding:1rem 1.2rem}.stat+.stat{border-left:1px solid var(--line)}.stat-title{color:var(--muted);font-size:.75rem}.stat-value{font-size:1.8rem;font-weight:900}.text-warning{color:var(--amber)}.text-info{color:var(--blue)}
.badge{display:inline-flex;align-items:center;border:1px solid currentColor;border-radius:999px;padding:.16rem .55rem;font-size:.72rem;font-weight:700}.badge-warning{color:var(--amber)}.badge-success{color:var(--green)}.badge-info{color:var(--blue)}.badge-neutral{color:var(--muted)}
.alert{display:flex;margin-bottom:2rem;padding:1rem 1.15rem}.alert-info{border-color:#36556f;color:#c7e7ff;background:#132533}.alert-warning{border-color:#665123;color:#f6d994;background:#302711}
nav{display:flex;flex-wrap:wrap;gap:.5rem;margin-bottom:2rem}.btn{appearance:none;border:1px solid var(--line);border-radius:.7rem;padding:.55rem .8rem;color:var(--text);background:#26231d;font:inherit;font-size:.8rem;font-weight:700;text-decoration:none;cursor:pointer}.btn:hover,.btn:focus-visible{border-color:var(--gold);outline:none;background:#332a1c}.rounded-box{border-radius:1rem}.border-dashed{border:1px dashed var(--line);padding:1.2rem}footer{border-top:1px solid var(--line);padding-top:1.5rem;color:var(--muted);font-size:.85rem}
@media(min-width:48rem){section>.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:42rem){.stats{width:100%;display:grid;grid-template-columns:repeat(3,1fr)}.stat{min-width:0;padding:.8rem}.stat+.stat{border-left:1px solid var(--line)}.stat-value{font-size:1.35rem}}
</style>
</head>
<body>
<main class="mx-auto max-w-7xl p-4 sm:p-7 lg:p-10">
<header class="mb-8 flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
  <div><p class="mast text-xs font-bold uppercase text-primary">Mate · fleet snapshot</p><h1 class="mt-2 text-4xl font-black sm:text-6xl">Bearings</h1><p class="mt-2 opacity-65">Generated ${esc(generated)} · showing ${esc(tasks.length)} of ${esc(snapshot?.total_tasks ?? tasks.length)} retained tasks</p></div>
  <div class="stats stats-vertical bg-base-100 shadow sm:stats-horizontal">
    <div class="stat"><div class="stat-title">Your call</div><div class="stat-value text-warning">${captain.length}</div></div>
    <div class="stat"><div class="stat-title">Underway</div><div class="stat-value text-info">${underway.length}</div></div>
    <div class="stat"><div class="stat-title">Pending events</div><div class="stat-value">${events.length}</div></div>
  </div>
</header>
<div role="alert" class="alert alert-info alert-soft mb-8"><span><strong>Read-only board.</strong> Buttons only copy a command. Paste it into the Mate TUI; approval, completion and cancellation still require their normal human confirmation.</span></div>
<nav class="mb-8 flex flex-wrap gap-2" aria-label="Board sections">
  <a class="btn btn-sm" href="#captains-call">Captain’s Call</a><a class="btn btn-sm" href="#landed">Recently Landed</a><a class="btn btn-sm" href="#underway">Underway</a><a class="btn btn-sm" href="#charted">Charted Next</a>
</nav>
<section id="captains-call" class="section mb-10"><h2 class="mb-4 text-2xl font-bold">Captain’s Call</h2><div class="grid gap-4 md:grid-cols-2">${captain.length ? captain.map((task: any) => card(task, "warning")).join("") : empty("Nothing needs your action right now.")}</div></section>
<section id="landed" class="section mb-10"><h2 class="mb-4 text-2xl font-bold">Recently Landed</h2><div class="grid gap-4 md:grid-cols-2 xl:grid-cols-3">${landed.length ? landed.map((task: any) => card(task, "success")).join("") : empty("No recent completions are in the current baseline.")}</div></section>
<section id="underway" class="section mb-10"><h2 class="mb-4 text-2xl font-bold">Underway</h2><div class="grid gap-4 md:grid-cols-2">${underway.length ? underway.map((task: any) => card(task, "info")).join("") : empty("Nothing is underway.")}</div></section>
<section id="charted" class="section mb-10"><h2 class="mb-4 text-2xl font-bold">Charted Next</h2><div class="grid gap-4 md:grid-cols-2">${charted.length ? charted.map((task: any) => card(task, "neutral")).join("") : empty("Nothing is queued.")}</div>${events.length ? `<h3 class="mb-3 mt-6 font-bold">Waiting for Mate handling</h3><div class="grid gap-3">${eventRows}</div>` : ""}</section>
<footer class="border-t border-base-content/10 py-6 text-sm opacity-60">Snapshot only: no task state, acknowledgement, worker, lease or repository was changed.</footer>
</main>
<script>
document.querySelectorAll('.copy').forEach(button=>button.addEventListener('click',async()=>{const text=button.dataset.copy;try{await navigator.clipboard.writeText(text);button.textContent='Copied';setTimeout(()=>button.textContent='Copy '+text,1200)}catch{window.prompt('Copy command',text)}}));
</script>
</body></html>`;
}

export function writeBearingsBoard(snapshot: any, home: string) {
  const directory = join(home, ".lavish");
  if (existsSync(directory)) {
    const stat = lstatSync(directory);
    if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error(`Unsafe Bearings directory: ${directory}`);
  } else mkdirSync(directory, { recursive: true, mode: 0o700 });
  chmodSync(directory, 0o700);
  const path = join(directory, "bearings-board.html");
  const temporary = join(directory, `.bearings-board.${process.pid}.${Date.now()}.tmp`);
  try {
    writeFileSync(temporary, renderBearingsBoard(snapshot), { encoding: "utf8", mode: 0o600 });
    renameSync(temporary, path);
    chmodSync(path, 0o600);
  } finally { rmSync(temporary, { force: true }); }
  return path;
}

export function openBearingsBoard(path: string) {
  return new Promise<{ url: string; status: string }>((resolve, reject) => {
    // Invoking /bearings lavish is an explicit request to reopen a previously ended board.
    const child = spawn("lavish-axi", [path, "--reopen"], { stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "", stderr = "", done = false;
    const finish = (error?: Error, value?: { url: string; status: string }) => {
      if (done) return; done = true; clearTimeout(timer);
      error ? reject(error) : resolve(value!);
    };
    const timer = setTimeout(() => { child.kill("SIGTERM"); finish(new Error("Lavish did not open within 30 seconds")); }, 30_000);
    child.on("error", error => finish(new Error(`Cannot start lavish-axi: ${error.message}`)));
    child.stdout.on("data", data => { stdout = (stdout + data).slice(-1_000_000); });
    child.stderr.on("data", data => { stderr = (stderr + data).slice(-20_000); });
    child.on("close", code => {
      if (done) return;
      if (code !== 0) return finish(new Error(`lavish-axi failed: ${(stderr || stdout).trim().slice(-2000)}`));
      const url = stdout.match(/^\s*url:\s*["']?([^\s"']+)/m)?.[1];
      const status = stdout.match(/^\s*status:\s*["']?([^\s"']+)/m)?.[1] ?? "opened";
      if (!url) return finish(new Error("lavish-axi returned no session URL"));
      finish(undefined, { url, status });
    });
  });
}
