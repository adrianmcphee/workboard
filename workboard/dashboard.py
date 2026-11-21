"""The control room: one local page showing the fleet, and the buttons a human needs."""
from __future__ import annotations

from datetime import datetime
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import shutil
import time
from urllib.parse import urlparse

from . import audits, builds, compile_queue, control, directives, interventions, queue, store, train, usage, worker
from .config import Config

LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
STATUS_ORDER = ("CLAIMED", "REVIEWING", "LANDING", "AWAITING_REVIEW", "NEEDS_TRIAGE", "NEEDS_TAKEOVER", "READY", "BLOCKED", "DONE", "CANCELLED")


def snapshot(cfg: Config) -> dict[str, object]:
    """Everything the page shows, as plain data. Also what `workboard status --json` prints."""
    conn = store.connect(cfg)
    try:
        queue.refresh(conn, cfg)
        now = time.time()
        counts = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")}
        tasks = [dict(r) for r in conn.execute(
            "SELECT task_id, title, status, priority, attempts, lease_harness, lease_owner, claimed_at, author_harness, "
            "reason, updated_at, depends_json FROM tasks").fetchall()]
        for task in tasks:
            task["age_min"] = round((now - (task["claimed_at"] or task["updated_at"])) / 60.0)
        tasks.sort(key=lambda t: (STATUS_ORDER.index(t["status"]) if t["status"] in STATUS_ORDER else 99, -t["priority"]))
        slots = worker.live_slots(conn)
        snap = usage.snapshot(cfg)
        for entry in snap["harnesses"]:  # type: ignore[union-attr]
            entry["live_slots"] = slots.get(entry["harness"], 0)
            target = worker.target_slots(conn, entry["harness"])
            entry["target_slots"] = target
            for win in entry["windows"]:
                win["resets_label"] = usage.when(win.get("resets_at"))
                win["exhausts_label"] = usage.when(win["exhausts_at"]) if win.get("exhausts_at") else None
        manifest = train.manifest(conn, cfg, datetime.now())
        builds.note_planned(cfg, manifest)
        for build in manifest["builds"]:  # type: ignore[union-attr]
            build["resets_before"] = []
        for entry in snap["harnesses"]:  # type: ignore[union-attr]
            win = usage.binding(entry)
            if win and win.get("resets_at"):
                reset_id = datetime.fromtimestamp(float(win["resets_at"])).strftime("%Y-%m-%d %H:%M")
                first = next((b for b in manifest["builds"] if b["build"] >= reset_id), None)  # type: ignore[union-attr]
                if first is not None:
                    first["resets_before"].append(entry["harness"])
        compiling = compile_queue.view(cfg.state, cfg.compile_slots) if cfg.compile_slots > 0 else {"enabled": False}
        events = [dict(r) for r in conn.execute("SELECT at, actor, kind, task_id, detail FROM events ORDER BY id DESC LIMIT 60")]
        for ev in events:
            ev["time"] = datetime.fromtimestamp(ev["at"]).strftime("%H:%M:%S")
        return {
            "project": cfg.name, "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "counts": counts, "total": sum(counts.values()), "tasks": tasks,
            "free_gib": round(shutil.disk_usage(cfg.state).free / 2**30, 1),
            "load": round(os.getloadavg()[0], 1), "cores": os.cpu_count(),
            "usage": snap, "usage_lines": usage.lines(snap), "train": manifest, "builds": builds.summary(cfg),
            "interventions": interventions.collect(cfg, conn, snap, slots, manifest, compiling), "events": events,
            "compile": compiling,
            "audits": audits.overview(conn), "draining": queue.draining(conn),
            "finish_only": queue.finish_only(conn), "landing_held": queue.landing_held(conn),
            "directives": directives.active(conn, now), "active_builds": directives.live_builds(conn, now),
        }
    finally:
        conn.close()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Workboard</title>
<style>
:root{--bg:#f6f7f9;--panel:#fff;--line:#dfe3ea;--text:#18202c;--muted:#667085;--green:#16803c;--amber:#b45309;--red:#c02626;--blue:#1d6fd8;--bar:#e6e9ef}
@media (prefers-color-scheme:dark){:root{--bg:#0d1118;--panel:#151b26;--line:#263143;--text:#e7ecf4;--muted:#94a3b8;--green:#34d27b;--amber:#f5a524;--red:#f26d6d;--blue:#5aa9ff;--bar:#222c3c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;padding:14px 16px;border-bottom:1px solid var(--line)}
h1{font-size:18px;margin:0}h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:0 0 10px}
main{padding:16px;display:grid;gap:16px;grid-template-columns:1fr}@media(min-width:900px){main{grid-template-columns:1fr 1fr}}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;min-width:0}
section.wide{grid-column:1/-1}.muted{color:var(--muted)}.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;overflow-wrap:anywhere}
.kpis{display:flex;flex-wrap:wrap;gap:8px}.kpi{border:1px solid var(--line);border-radius:8px;padding:6px 10px;min-width:84px}.kpi b{display:block;font-size:20px}
.card{border-left:4px solid var(--line);padding:8px 10px;margin-bottom:10px;background:var(--bg);border-radius:6px}
.card.CRITICAL{border-left-color:var(--red)}.card.HIGH{border-left-color:var(--amber)}.card.MEDIUM{border-left-color:var(--blue)}
.card p{margin:3px 0}.bar{height:6px;background:var(--bar);border-radius:3px;overflow:hidden;margin:4px 0 8px}.bar i{display:block;height:100%}
button{font:inherit;border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:6px;padding:4px 10px;margin:4px 6px 0 0;cursor:pointer}
button:hover{border-color:var(--blue)}button.danger{color:var(--red)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:5px 8px 5px 0;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600}.scroll{max-height:360px;overflow:auto}.tag{font-size:11px;border:1px solid var(--line);border-radius:10px;padding:1px 7px;white-space:nowrap}
#result{white-space:pre-wrap;margin-bottom:8px}
</style></head><body>
<header><h1>Workboard</h1><span id="project" class="muted"></span><span id="clock" class="muted mono"></span></header>
<main>
<section class="wide"><div class="kpis" id="kpis"></div><p class="muted mono" id="compile"></p></section>
<section><h2>Potential interventions</h2><div id="result" class="mono"></div><div id="interventions"></div></section>
<section><h2>Subscriptions</h2><div id="usage"></div><h2 style="margin-top:14px">Build train</h2><div id="train"></div></section>
<section class="wide"><h2>Latest build and sign-off <span id="buildhead" class="mono"></span></h2><div id="builds"></div></section>
<section class="wide"><h2>Tasks</h2><div class="scroll"><table><thead><tr><th>Task</th><th>Status</th><th>Who</th><th>Attempts</th><th>Note</th><th></th></tr></thead><tbody id="tasks"></tbody></table></div></section>
<section class="wide"><h2>Events</h2><div class="scroll mono" id="events"></div></section>
</main>
<script>
const TOKEN='__WORKBOARD_TOKEN__';let ACTS=[];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function btn(o,cls){return `<button class="${cls||''}" onclick="act(${ACTS.push(o)-1})">${esc(o.label)}</button>`}
async function act(i){const o=ACTS[i];if(!o)return;const args=Object.assign({},o.args||{});
 if(o.ask){const note=window.prompt(o.ask);if(!note)return;args.note=note}
 if(o.confirm&&!window.confirm(o.confirm))return;
 const out=document.getElementById('result');out.textContent='Running '+o.label+' ...';
 try{const r=await fetch('/api/act',{method:'POST',headers:{'Content-Type':'application/json','X-Workboard-Token':TOKEN},body:JSON.stringify({op:o.op,args})});
  const d=await r.json();out.textContent=(d.ok?'Done: ':'Failed: ')+(d.output||d.error||'');out.style.color=d.ok?'var(--green)':'var(--red)'}
 catch(e){out.textContent='Failed: '+e;out.style.color='var(--red)'}
 refresh()}
const pace={reserve:'var(--red)',over:'var(--amber)',on:'var(--green)',under:'var(--blue)'};
function render(s){ACTS=[];
 document.getElementById('project').textContent=s.project;
 document.getElementById('clock').textContent=`${s.generated_at} · load ${s.load}/${s.cores} · ${s.free_gib} GiB free`;
 const order=['DONE','LANDING','REVIEWING','AWAITING_REVIEW','CLAIMED','READY','BLOCKED','NEEDS_TAKEOVER','NEEDS_TRIAGE','CANCELLED'];
 document.getElementById('kpis').innerHTML=order.filter(k=>s.counts[k]).map(k=>`<div class="kpi"><b>${s.counts[k]}</b><span class="muted">${k.replace(/_/g,' ').toLowerCase()}</span></div>`).join('')||'<span class="muted">No tasks yet. Add files to the tasks folder and run workboard sync.</span>';
 {const q=s.compile||{};const parts=[];if(s.draining)parts.push('Fleet: draining');if(s.finish_only)parts.push('Fleet: finish-only');if(s.landing_held)parts.push('Landing: held');if(s.directives&&s.directives.length)parts.push(`Directives: ${s.directives.map(d=>d.directive_id+' ['+d.mode+']').join(', ')}`);if(q.enabled)parts.push(`Compile queue: ${q.holders.length}/${q.slots} slot(s) held, ${q.waiters.length} waiting, longest wait ${Math.round(q.longest_wait_s/60)} min, recent p90 wait ${Math.round(q.recent_p90_wait_s)} s`+(q.memory&&q.memory.mem_available_pct!=null?`, ${Math.round(q.memory.mem_available_pct)}% memory free`:''));document.getElementById('compile').textContent=parts.join(' · ')}
 document.getElementById('interventions').innerHTML=s.interventions.length?s.interventions.map(c=>`<div class="card ${esc(c.severity)}"><p><b>${esc(c.title)}</b></p><p>${esc(c.what)}</p><p class="mono">${esc(c.action)}</p>${c.ops.map(o=>btn(o)).join('')}</div>`).join(''):'<p class="muted">No decision is waiting on you.</p>';
 document.getElementById('usage').innerHTML=s.usage.harnesses.map(h=>{
  const head=`<p><b>${esc(h.harness)}</b> <span class="muted">${h.live_slots} worker(s)${h.target_slots==null?'':' of '+h.target_slots}${h.age_s==null?'':' · read '+Math.round(h.age_s/60)+'m ago'+(h.stale?' (stale)':'')}</span></p>`;
  const ctl=btn({label:'+1',op:'workers',args:{harness:h.harness,slots:h.live_slots+1}})+(h.live_slots?btn({label:'-1',op:'workers',args:{harness:h.harness,slots:h.live_slots-1},confirm:'The extra worker finishes its current work, then stops.'}):'');
  if(!h.windows.length)return `<div class="card">${head}<p class="muted">Usage unknown: ${esc(h.error||'no data')}</p>${ctl}</div>`;
  return `<div class="card" style="border-left-color:${pace[h.pace]||'var(--line)'}">${head}`+h.windows.map(w=>`<p style="${w.binding?'':'opacity:.6'}">${esc(w.label)}: <b>${Math.round(w.remaining_pct)}% left</b>, resets ${esc(w.resets_label)}${w.exhausts_label?`, <b style="color:${pace[w.pace]}">runs out ${esc(w.exhausts_label)}</b>`:(w.projected_pct_at_reset==null?'':`, ${Math.max(0,Math.round(100-w.projected_pct_at_reset))}% unused at reset`)}</p><div class="bar"><i style="width:${Math.min(100,w.used_pct)}%;background:${pace[w.pace]||'var(--muted)'}"></i></div>`).join('')+ctl+'</div>'}).join('')||'<p class="muted">No harness configured.</p>';
 document.getElementById('train').innerHTML=s.train.builds.length?s.train.builds.map(b=>`<div class="card"><p><b>${esc(b.label)}</b> <span class="muted">in ${b.minutes_away}m · land cutoff ${esc(b.cutoff)}${b.resets_before.length?' · '+esc(b.resets_before.join(', '))+' resets before it':''}</span></p>${b.tasks.map(t=>`<p class="mono">${esc(t.task_id)} <span class="tag">${esc(t.status)}</span> on main ~${esc(t.expected_on_main)}, slack ${t.slack_min}m</p>`).join('')||'<p class="muted">Nothing in flight for this build.</p>'}</div>`).join(''):'<p class="muted">No build schedule configured.</p>';
 const b=s.builds.latest;let html='';
 if(s.builds.failed)html+=`<div class="card CRITICAL"><p><b>Build ${esc(s.builds.failed.id)} failed.</b> Landed since the last good build: ${s.builds.failed.slices.map(x=>esc(x.task_id)+' ('+esc(x.author||'?')+' / '+esc(x.reviewer||'?')+')').join(', ')||'nothing tracked'}</p></div>`;
 document.getElementById('buildhead').textContent=b?`${b.id} · ${b.slot} · ${b.pending.length} of ${b.slices.length} awaiting sign-off`:'';
 if(b){const tally=r=>Object.entries(s.builds.escapes[r]||{}).map(([k,v])=>`${esc(k)} ${v.rejected}/${v.rejected+v.accepted}`).join(', ')||'none yet';
  html+=`<p class="muted">Rejected at sign-off by author: ${tally('author')} · by reviewer: ${tally('reviewer')}${b.missed.length?' · missed this build: '+b.missed.map(esc).join(', '):''}</p><div class="scroll"><table><thead><tr><th>Slice</th><th>Author / reviewer</th><th>Verdict</th></tr></thead><tbody>`+b.slices.map(x=>{const v=b.signoff[x.task_id];
   return `<tr><td><span class="mono">${esc(x.task_id)}</span><br><span class="muted">${esc(x.title)}</span></td><td>${esc(x.author||'?')} / ${esc(x.reviewer||'none')}</td><td>${v?`<span class="tag">${esc(v.verdict)}</span> ${esc(v.note||'')}${v.followup?' → '+esc(v.followup):''}`:btn({label:'Accept',op:'signoff_accept',args:{build:b.id,task:x.task_id}})+btn({label:'Reject',op:'signoff_reject',args:{build:b.id,task:x.task_id},ask:'What is wrong with '+x.task_id+'? Your note becomes the brief for the fix.'},'danger')}</td></tr>`}).join('')+'</tbody></table></div>'}
 document.getElementById('builds').innerHTML=html||'<p class="muted">No build recorded yet. Run workboard build.</p>';
 document.getElementById('tasks').innerHTML=s.tasks.map(t=>`<tr><td><span class="mono">${esc(t.task_id)}</span><br><span class="muted">${esc(t.title)}</span></td><td><span class="tag">${esc(t.status)}</span></td><td>${esc(t.lease_harness||t.author_harness||'')}${t.lease_harness?' · '+t.age_min+'m':''}</td><td>${t.attempts}</td><td class="muted">${esc(t.reason||'')}</td><td>${['DONE','CANCELLED'].includes(t.status)?'':btn({label:'Cancel',op:'cancel',args:{task:t.task_id},confirm:'Cancel '+t.task_id+'?'},'danger')}${['NEEDS_TRIAGE','CANCELLED'].includes(t.status)?btn({label:'Requeue',op:'requeue',args:{task:t.task_id}}):''}</td></tr>`).join('');
 document.getElementById('events').innerHTML=s.events.map(e=>`<div>${esc(e.time)} <b>${esc(e.kind)}</b> ${esc(e.task_id||'')} <span class="muted">${esc(e.actor)} ${esc(e.detail)}</span></div>`).join('');
}
async function refresh(){try{render(await (await fetch('/api/state')).json())}catch(e){document.getElementById('clock').textContent='disconnected: '+e}}
refresh();setInterval(refresh,5000);
</script></body></html>
"""


def make_handler(cfg: Config, token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

        def _json(self, payload: object, code: int = 200) -> None:
            raw = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _local(self) -> bool:
            return (self.headers.get("Host") or "").rsplit(":", 1)[0] in LOCAL_HOSTS

        def do_GET(self) -> None:
            if not self._local():
                self._json({"error": "forbidden"}, 403)
                return
            if urlparse(self.path).path == "/api/state":
                self._json(snapshot(cfg))
                return
            body = PAGE.replace("__WORKBOARD_TOKEN__", token).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            # A write needs the token only the served page carries, sent in a custom header that a
            # cross-site request cannot set, and a loopback Host header to defeat DNS rebinding.
            sent = self.headers.get("X-Workboard-Token") or ""
            if not self._local() or not hmac.compare_digest(sent, token) or urlparse(self.path).path != "/api/act":
                self._json({"ok": False, "error": "forbidden"}, 403)
                return
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                name, args = str(data.get("op") or ""), data.get("args") or {}
            except (ValueError, AttributeError):
                self._json({"ok": False, "error": "bad request"}, 400)
                return
            conn = store.connect(cfg)
            try:
                self._json(control.run(cfg, conn, name, args if isinstance(args, dict) else {}))
            finally:
                conn.close()

    return Handler


def serve(cfg: Config, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((host or cfg.dashboard_host, port if port is not None else cfg.dashboard_port),
                                 make_handler(cfg, token))
    server.workboard_token = token  # type: ignore[attr-defined]
    return server
