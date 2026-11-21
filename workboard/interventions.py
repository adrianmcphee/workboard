"""Potential interventions: decisions the fleet will not take on its own.

A card names a choice that trades speed against budget or needs judgement, the command that takes
it, and where one exists a dashboard operation that runs it.
"""
from __future__ import annotations

import sqlite3

from . import store, usage
from .config import Config

ORDER = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "INFO": 3}
CHURN_ATTEMPTS = 4
FANOUT_MIN = 3


def card(severity: str, kind: str, title: str, what: str, action: str,
         ops: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {"severity": severity, "kind": kind, "title": title, "what": what, "action": action, "ops": ops or []}


def op(label: str, name: str, confirm: str = "", **args: object) -> dict[str, object]:
    return {"label": label, "op": name, "args": args, "confirm": confirm}


def usage_cards(cfg: Config, snap: dict[str, object], slots: dict[str, int]) -> list[dict[str, object]]:
    cards = []
    entries = {str(e["harness"]): e for e in snap.get("harnesses", [])}  # type: ignore[union-attr]
    under = [h for h, e in entries.items() if e.get("pace") == "under" and not e.get("stale")]
    for name, entry in entries.items():
        running = slots.get(name, 0)
        win = usage.binding(entry)
        if not win:
            if not running:
                cards.append(card("INFO", "idle_harness", f"{name}: no workers running",
                                  "This harness is configured but idle.", f"workboard up --harness {name}",
                                  [op(f"Start {name}", "workers", harness=name, slots=cfg.harnesses[name].slots)]))
            continue
        reset = usage.when(win["resets_at"])
        if entry.get("stale"):
            cards.append(card("MEDIUM", "usage_stale", f"{name}: usage reading is {(entry.get('age_s') or 0) / 3600:.1f}h old",
                              "Stale readings are shown but never steer claims.",
                              f"Refresh the reading for {name} (for a CLI cache, open its usage screen)."))
        elif entry["pace"] == "reserve":
            cards.append(card("CRITICAL", "usage_reserve", f"{name}: {win['remaining_pct']:.0f}% left, authoring paused",
                              f"New authoring is refused until the reset at {reset}. Reviews still run.",
                              f"workboard down --harness {name}",
                              [op(f"Drain {name}", "workers", f"Stop {name} after its current work?", harness=name, slots=0)]))
        elif entry["pace"] == "over":
            rate, hours = float(win.get("rate_pct_per_h") or 0), float(win.get("hours_to_reset") or 0)
            sustainable = float(win["remaining_pct"]) / hours if hours > 0 else 0.0
            keep = max(1, int(max(1, running) * sustainable / rate)) if rate > 0 and running else 1
            gap = max(0.0, (float(win["resets_at"]) - float(win.get("exhausts_at") or win["resets_at"])) / 3600.0)
            shift = f" Shift authoring to {', '.join(under)}, which would otherwise leave allowance unused." if under else ""
            cards.append(card("HIGH" if gap >= 12 else "MEDIUM", "usage_over_pace",
                              f"{name}: runs out {usage.when(win.get('exhausts_at'))}, {gap:.0f}h before the {reset} reset",
                              f"{win['remaining_pct']:.0f}% left, burning {rate}%/h against a sustainable "
                              f"{sustainable:.2f}%/h with {running} worker(s).{shift}",
                              f"To last until the reset: workboard up --harness {name} --slots {keep}. "
                              "To keep speed: do nothing and accept the dry spell.",
                              [op(f"Run {name} at {keep} slot(s)", "workers",
                                  "Extra workers finish their current work, then stop.", harness=name, slots=keep)]
                              if running > keep else []))
        elif entry["pace"] == "under":
            unused = max(0.0, 100.0 - float(win.get("projected_pct_at_reset") or 100.0))
            cards.append(card("MEDIUM", "usage_under_pace", f"{name}: about {unused:.0f}% will expire unused at {reset}",
                              f"{win['remaining_pct']:.0f}% left with {running} worker(s).",
                              f"workboard up --harness {name} --slots {running + 1}",
                              [op(f"Run {name} at {running + 1} slot(s)", "workers", harness=name, slots=running + 1)]))
    return cards


def task_cards(conn: sqlite3.Connection) -> list[dict[str, object]]:
    cards = []
    rows = conn.execute("SELECT task_id, status, attempts, reason, depends_json FROM tasks").fetchall()
    status = {r["task_id"]: r["status"] for r in rows}
    for row in rows:
        if row["status"] == "NEEDS_TRIAGE":
            cards.append(card("CRITICAL", "triage", f"`{row['task_id']}` is parked after {row['attempts']} attempts",
                              f"Last reason: {row['reason'] or 'none'}. Workers will not claim it again on their own.",
                              f"Read its takeover notes, fix or narrow the brief, then: workboard requeue {row['task_id']}",
                              [op("Requeue", "requeue", task=row["task_id"]), op("Cancel", "cancel", "Cancel this task?", task=row["task_id"])]))
        elif row["status"] not in ("DONE", "CANCELLED") and row["attempts"] >= CHURN_ATTEMPTS:
            cards.append(card("MEDIUM", "churn", f"`{row['task_id']}` is on attempt {row['attempts']}",
                              f"Last reason: {row['reason'] or 'none'}. Repeated attempts usually mean the brief is wrong, not the workers.",
                              f"Compare the brief with the takeover notes and narrow the slice: workboard show {row['task_id']}"))
    fan: dict[str, int] = {}
    for row in rows:
        if row["status"] in ("DONE", "CANCELLED"):
            continue
        for dep in store.loads(row["depends_json"]):
            fan[dep] = fan.get(dep, 0) + 1
            if status.get(dep) == "CANCELLED":
                cards.append(card("HIGH", "blocked_by_cancelled", f"`{row['task_id']}` depends on cancelled `{dep}`",
                                  "It can never become ready.", f"workboard requeue {dep}   or   workboard cancel {row['task_id']}"))
    waiting = sorted(((n, t) for t, n in fan.items() if n >= FANOUT_MIN and status.get(t) == "READY"), reverse=True)
    if waiting:
        top = waiting[0][1]
        cards.append(card("MEDIUM", "critical_path", "Critical-path work is ready but unclaimed",
                          ", ".join(f"`{t}` unblocks {n}" for n, t in waiting[:4]) + ".",
                          f"workboard priority {top} 500", [op(f"Raise {top}", "priority", task=top, priority=500)]))
    return cards


def train_cards(manifest: dict[str, object]) -> list[dict[str, object]]:
    builds = manifest.get("builds") or []
    if not builds:
        return []
    nxt = builds[0]  # type: ignore[index]
    tight = [t for t in nxt["tasks"] if t["slack_min"] < 10]
    if not tight or nxt["minutes_to_cutoff"] > 90:
        return []
    return [card("MEDIUM", "build_cutoff", f"{len(tight)} slice(s) may miss the {nxt['label']} build",
                 f"Land cutoff {nxt['cutoff']}: " + ", ".join(f"`{t['task_id']}` ({t['status']})" for t in tight[:5]) + ".",
                 "Let them ride the following build, or review them now.")]


def audit_cards(conn: sqlite3.Connection) -> list[dict[str, object]]:
    cards = []
    for row in conn.execute("SELECT finding_id, task_id, title FROM findings WHERE state='wont-fix-proposed'"):
        cards.append(card("HIGH", "audit_decision", f"{row['finding_id']}: triage proposes not fixing it",
                          f"{row['title']} (owned by `{row['task_id']}`). Only a person accepts a defect.",
                          f"workboard audit decide {row['finding_id']} --wont-fix --note \"why\"  or  --reopen"))
    for row in conn.execute("SELECT job_id, attempts, reason FROM audit_jobs WHERE status='NEEDS_TRIAGE'"):
        cards.append(card("HIGH", "audit_parked", f"Audit job `{row['job_id']}` is parked after {row['attempts']} attempts",
                          str(row["reason"] or "no reason recorded"), f"workboard audit requeue {row['job_id']}"))
    live = conn.execute("SELECT COUNT(*) FROM findings WHERE severity='P0' AND state IN ('open','reopened')").fetchone()[0]
    if live:
        cards.append(card("CRITICAL", "audit_p0", f"{live} P0 finding(s) have no fix task yet",
                          "A P0 blocks core work or loses work. Triage queues one fix task per root cause.",
                          "workboard audit findings --untriaged"))
    return cards


def fleet_cards(conn: sqlite3.Connection) -> list[dict[str, object]]:
    from . import directives, queue
    cards: list[dict[str, object]] = []
    if queue.draining(conn):
        marks = ",".join("?" * len(store.IN_FLIGHT))
        flying = conn.execute(f"SELECT COUNT(*) FROM tasks WHERE status IN ({marks})", store.IN_FLIGHT).fetchone()[0]
        cards.append(card("HIGH", "draining", "The fleet is draining",
                          f"No new authoring or audit jobs are handed out; {flying} task(s) are still in flight. "
                          "Reviews and landing continue.", "workboard resume   (or, before moving machines: workboard move-check)"))
    if queue.finish_only(conn):
        cards.append(card("HIGH", "finish_only", "The fleet is finishing started work only",
                          "Reviews, takeovers and landing continue; no task or audit job that never began is started.",
                          "workboard finish-only off"))
    if queue.landing_held(conn):
        waiting = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='LANDING'").fetchone()[0]
        cards.append(card("HIGH", "landing_held", "Landing is held",
                          f"Nothing lands on the main branch; {waiting} reviewed task(s) wait in LANDING.",
                          "workboard landing-hold off"))
    for row in directives.active(conn):
        did, mode = str(row["directive_id"]), str(row["mode"])
        sev = "CRITICAL" if mode in ("cancel_builds", "abort_leases") else (
            "HIGH" if mode in ("pause_claims", "wrap_up") else "INFO"
        )
        rem = f" (expires in {round(int(row['remaining_s']) / 60)}m)" if row.get("remaining_s") is not None else ""
        cards.append(card(sev, "directive", f"Directive {did} ({mode}) on {row['scope']}{rem}",
                          str(row["message"]), f"workboard directive clear {did}",
                          [op(f"Clear {did}", "directive_clear", f"Clear directive {did}?", directive_id=did)]))
    last_landed = conn.execute("SELECT COALESCE(MAX(at), 0) FROM events WHERE kind='landed'").fetchone()[0]
    fails = conn.execute("SELECT detail FROM events WHERE kind='land_failed' AND at>? ORDER BY id DESC LIMIT 6",
                         (last_landed,)).fetchall()
    if len(fails) >= 2 and len({r[0] for r in fails}) == 1:
        cards.append(card("CRITICAL", "landing_blocked",
                          f"Landing failed {len(fails)} times in a row for the same reason",
                          f"Latest error: {fails[0][0]}. Nothing has landed since.",
                          "Fix what the error names, then run: workboard land"))
    return cards


def compile_cards(cfg: Config, view: dict[str, object] | None) -> list[dict[str, object]]:
    """Builds that wait long while memory sits free mean the compile queue has too few slots."""
    if not view or not view.get("enabled"):
        return []
    wait_min = float(view.get("longest_wait_s") or 0) / 60.0
    free = (view.get("memory") or {}).get("mem_available_pct")  # type: ignore[union-attr]
    if wait_min < cfg.compile_alert_wait_min or free is None or float(free) < cfg.compile_alert_free_pct:
        return []
    waiting = len(view.get("waiters") or [])  # type: ignore[arg-type]
    return [card("MEDIUM", "compile_wait", f"Builds wait {wait_min:.0f} min for a compile slot while {float(free):.0f}% of memory is free",
                 f"{waiting} build(s) queued behind {cfg.compile_slots} slot(s). Memory has room, so the slot count is "
                 "what holds them back.", f"Raise [compile] slots in workboard.toml (now {cfg.compile_slots}) and restart the workers.")]


def collect(cfg: Config, conn: sqlite3.Connection, snap: dict[str, object], slots: dict[str, int],
            manifest: dict[str, object], compiling: dict[str, object] | None = None) -> list[dict[str, object]]:
    cards: list[dict[str, object]] = []
    for source in (lambda: usage_cards(cfg, snap, slots), lambda: task_cards(conn), lambda: train_cards(manifest),
                   lambda: audit_cards(conn), lambda: fleet_cards(conn), lambda: compile_cards(cfg, compiling)):
        try:
            cards.extend(source())
        except Exception as exc:  # one broken source must not hide the others
            cards.append(card("INFO", "source_error", "An intervention source failed", str(exc)[:200], ""))
    return sorted(cards, key=lambda c: ORDER.get(str(c["severity"]), 9))
