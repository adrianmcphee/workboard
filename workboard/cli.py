"""The `workboard` command."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from . import __version__, audits, builds, compile_queue, config, control, dashboard, interventions, movecheck, queue, store, tasks, template, train, usage, worker


def _open(args: argparse.Namespace):
    cfg = config.load(Path(args.root) if getattr(args, "root", None) else None)
    return cfg, store.connect(cfg)


def cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.root or ".").resolve()
    target = root / config.CONFIG_NAME
    if target.exists():
        print(f"{target} already exists")
        return 1
    target.write_text(template.WORKBOARD_TOML.format(name=root.name), encoding="utf-8")
    example = root / "workboard" / "tasks" / "example-task.md"
    example.parent.mkdir(parents=True, exist_ok=True)
    if not example.exists():
        example.write_text(template.EXAMPLE_TASK, encoding="utf-8")
    print(f"wrote {target}\nwrote {example}\nAdd `.workboard/` and `.worktrees/` to .gitignore, enable a [harness.*] block, then run `workboard sync`.")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    result = tasks.sync(conn, cfg)
    queue.refresh(conn, cfg)
    print(f"{result['files']} task file(s), {result['added']} new")
    for error in result["errors"]:  # type: ignore[union-attr]
        print(f"error: {error}")
    return 1 if result["errors"] else 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg, _ = _open(args)
    snap = dashboard.snapshot(cfg)
    if args.json:
        print(json.dumps(snap, indent=2, default=str))
        return 0
    counts = snap["counts"]
    print(f"{snap['project']}  {snap['generated_at']}  load {snap['load']}/{snap['cores']}  {snap['free_gib']} GiB free")
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())) if counts else "  no tasks")  # type: ignore[union-attr]
    queue_view = snap.get("compile") or {}
    if queue_view.get("enabled"):  # type: ignore[union-attr]
        mem = queue_view.get("memory") or {}  # type: ignore[union-attr]
        print(f"  compile: {len(queue_view['holders'])}/{queue_view['slots']} slot(s) held, "  # type: ignore[index]
              f"{len(queue_view['waiters'])} waiting, longest wait {round(queue_view['longest_wait_s'] / 60)}m"  # type: ignore[index]
              + (f", {mem['mem_available_pct']:.0f}% memory free" if mem.get("mem_available_pct") is not None else ""))
    if snap.get("draining"):
        print("  fleet: draining (no new authoring or audit jobs); `workboard resume` undoes it")
    for line in snap["usage_lines"]:  # type: ignore[union-attr]
        print(f"  usage: {line}")
    for build in snap["train"]["builds"][:2]:  # type: ignore[index]
        print(f"  build: {build['label']} in {build['minutes_away']}m, cutoff {build['cutoff']}, {len(build['tasks'])} slice(s) in flight")
    latest = snap["builds"]["latest"]  # type: ignore[index]
    if latest:
        print(f"  sign-off: build {latest['id']}, {len(latest['pending'])} of {len(latest['slices'])} awaiting a verdict")
    view = snap.get("audits") or {}
    if view.get("latest"):  # type: ignore[union-attr]
        live = view["live_findings"]  # type: ignore[index]
        print(f"  audit: {view['latest']['audit_id']} {view['latest']['status'].lower()}, "  # type: ignore[index]
              f"live findings P0={live.get('P0', 0)} P1={live.get('P1', 0)} P2={live.get('P2', 0)}")
    cards = snap["interventions"]
    print(f"\npotential interventions ({len(cards)})")  # type: ignore[arg-type]
    for card in cards:  # type: ignore[union-attr]
        print(f"  [{card['severity']}] {card['title']}\n      {card['what']}\n      -> {card['action']}")
    return 0


def cmd_board(args: argparse.Namespace) -> int:
    cfg, _ = _open(args)
    for task in dashboard.snapshot(cfg)["tasks"]:  # type: ignore[union-attr]
        who = task["lease_harness"] or task["author_harness"] or ""
        print(f"{task['status']:16s} {task['task_id']:32s} p{task['priority']:<5d} a{task['attempts']} {who:10s} {task['reason'] or ''}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (args.task,)).fetchone()
    if row is None:
        print(f"unknown task {args.task}")
        return 1
    for key in ("task_id", "title", "status", "branch", "priority", "attempts", "author_harness", "reason",
                "candidate_sha", "landed_sha", "brief_path"):
        print(f"{key:15s} {row[key]}")
    print(f"{'worktree':15s} {cfg.worktree(args.task)}\n{'handoff':15s} {cfg.handoff(args.task)}")
    for ev in conn.execute("SELECT at, actor, kind, detail FROM events WHERE task_id=? ORDER BY id", (args.task,)):
        print(f"  {datetime.fromtimestamp(ev['at']).strftime('%m-%d %H:%M')} {ev['kind']:18s} {ev['actor']} {ev['detail']}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    brief = Path(args.brief).read_text(encoding="utf-8") if args.brief else sys.stdin.read()
    tasks.add(conn, cfg, args.id, args.title, brief, priority=args.priority, paths=args.path, depends_on=args.depends_on)
    queue.refresh(conn, cfg)
    print(f"added {args.id}")
    return 0


def cmd_requeue(args: argparse.Namespace) -> int:
    _, conn = _open(args)
    print(f"{args.task} -> {queue.requeue(conn, args.task)}")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    _, conn = _open(args)
    queue.cancel(conn, args.task)
    print(f"{args.task} cancelled")
    return 0


def cmd_priority(args: argparse.Namespace) -> int:
    _, conn = _open(args)
    queue.set_priority(conn, args.task, args.priority)
    print(f"{args.task} priority {args.priority}")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    for name in ([args.harness] if args.harness else list(cfg.harnesses)):
        slots = args.slots if args.slots is not None else cfg.harnesses[name].slots
        result = control.set_slots(cfg, conn, name, slots)
        print(f"{name}: {slots} slot(s), started {result['started']}, draining {result['draining']}")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    if args.now:
        pids = control.stop_now(cfg, conn, args.harness)
        control.wait_stopped(conn, pids)
        print(f"stopped {len(pids)} worker(s); their tasks are queued as takeovers")
        return 0
    for name in ([args.harness] if args.harness else list(cfg.harnesses)):
        control.set_slots(cfg, conn, name, 0)
        print(f"{name}: draining; workers stop after their current task")
    return 0


def in_flight(conn) -> dict[str, int]:
    marks = ",".join("?" * len(store.IN_FLIGHT))
    return {r[0]: r[1] for r in conn.execute(f"SELECT status, COUNT(*) FROM tasks WHERE status IN ({marks}) GROUP BY status",
                                             store.IN_FLIGHT)}


def cmd_drain(args: argparse.Namespace) -> int:
    _, conn = _open(args)
    queue.set_draining(conn, True, "cli")
    flying = in_flight(conn)
    print("draining: workers take no new authoring or audit jobs; reviews and landing continue.")
    print("in flight: " + ("  ".join(f"{k}={v}" for k, v in sorted(flying.items())) or "nothing"))
    print("Watch `workboard status`; undo with `workboard resume`.")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    _, conn = _open(args)
    queue.set_draining(conn, False, "cli")
    print("resumed: workers claim new work again")
    return 0


def cmd_move_check(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    report = movecheck.check(cfg, conn)
    print(json.dumps(report, indent=2) if args.json else "\n".join(movecheck.lines(report)))
    return 1 if report["would_lose"] else 0


def cmd_worker(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    conn.close()
    return worker.Worker(cfg, args.harness, args.slot, args.once).run()


def cmd_land(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    print(f"landed {worker.land_pending(cfg, conn, 'cli')} task(s)")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    record = builds.run(cfg, conn)
    if record.get("unchanged"):
        print(f"main is unchanged since build {record['id']}")
        return 0
    print(f"build {record['id']} {record['result']}: {len(record['slices'])} slice(s), {len(record['missed'])} missed, log {record['log']}")
    return 0 if record["result"] == "built" else 1


def cmd_builds(args: argparse.Namespace) -> int:
    cfg, _ = _open(args)
    if args.build or args.latest:
        record = builds.find(cfg, args.build or None)
        print(f"build {record['id']} ({record['slot']}) {record['result']} at {str(record['sha'])[:9]}, {record['commit_count']} commit(s)")
        for piece in record["slices"]:  # type: ignore[union-attr]
            verdict = record["signoff"].get(piece["task_id"], {}).get("verdict", "awaiting sign-off")  # type: ignore[union-attr]
            print(f"  {piece['task_id']:32s} {piece['author']} / {piece['reviewer']}  {verdict}\n      {piece['title']}")
        for missed in record["missed"]:  # type: ignore[union-attr]
            print(f"  {missed:32s} missed this build")
        return 0
    for record in builds.records(cfg):
        print(f"{record['id']}  {record['result']:6s} {record['slot']:17s} {len(record['slices'])} slice(s), "
              f"{len(builds.pending(record))} awaiting sign-off, {len(record['missed'])} missed")
    return 0


def cmd_signoff(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    entry = builds.sign(cfg, conn, args.build or None, args.task, args.verdict == "accept", args.note)
    print(f"{args.task} {entry['verdict']}" + (f"; queued {entry['followup']}" if entry.get("followup") else ""))
    return 0


def cmd_usage(args: argparse.Namespace) -> int:
    cfg, _ = _open(args)
    snap = usage.collect(cfg)
    print(json.dumps(snap, indent=2) if args.json else "\n".join(usage.lines(snap)) or "no harness configured")
    return 0


def cmd_interventions(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    compiling = compile_queue.view(cfg.state, cfg.compile_slots) if cfg.compile_slots > 0 else None
    cards = interventions.collect(cfg, conn, usage.snapshot(cfg), worker.live_slots(conn), train.manifest(conn, cfg, datetime.now()),
                                  compiling)
    if args.json:
        print(json.dumps(cards, indent=2))
        return 0
    for card in cards:
        print(f"[{card['severity']}] {card['title']}\n    {card['what']}\n    -> {card['action']}")
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    _, conn = _open(args)
    for ev in reversed(conn.execute("SELECT at, actor, kind, task_id, detail FROM events ORDER BY id DESC LIMIT ?", (args.limit,)).fetchall()):
        print(f"{datetime.fromtimestamp(ev['at']).strftime('%m-%d %H:%M:%S')} {ev['kind']:18s} {ev['task_id'] or '-':28s} {ev['actor']} {ev['detail']}")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    cfg, conn = _open(args)
    act = args.audit_cmd
    if act == "open":
        result = audits.open_audit(cfg, conn, sha=args.sha, build_id=args.build, quick=args.quick)
        if not result["opened"]:
            print(f"{result['audit_id']}: {result['reason']}")
            return 0
        jobs = ", ".join(f"{n} {k}" for k, n in sorted(result["jobs"].items()))  # type: ignore[union-attr]
        print(f"opened audit {result['audit_id']} of {str(result['sha'])[:9]}: {jobs}\ncheckout {result['worktree']}")
        return 0
    if act == "status":
        view = audits.overview(conn)
        latest = view["latest"]
        if not latest:
            print("no audit has been opened; run `workboard audit open` after a build")
            return 0
        print(f"audit {latest['audit_id']} ({latest['status'].lower()}) of {latest['sha'][:9]}")  # type: ignore[index]
        print("  jobs: " + ("  ".join(f"{k}={v}" for k, v in sorted(view["jobs"].items())) or "none"))  # type: ignore[union-attr]
        print("  live findings: " + ("  ".join(f"{k}={v}" for k, v in sorted(view["live_findings"].items())) or "none"))  # type: ignore[union-attr]
        for finding in view["awaiting_decision"]:  # type: ignore[union-attr]
            print(f"  {finding}: wont-fix proposed; decide with `workboard audit decide {finding} --wont-fix|--reopen`")
        for job in view["parked_jobs"]:  # type: ignore[union-attr]
            print(f"  {job}: parked after repeated attempts; `workboard audit requeue {job}`")
        return 0
    if act == "jobs":
        audit = audits.resolve_audit(conn, args.audit)
        for row in conn.execute("SELECT job_id, kind, status, attempts, lease_harness, finished_by, reason FROM audit_jobs "
                                "WHERE audit_id=? ORDER BY job_id", (audit["audit_id"],)):
            who = row["lease_harness"] or row["finished_by"] or ""
            print(f"{row['status']:13s} {row['job_id']:48s} a{row['attempts']} {who:10s} {row['reason'] or ''}")
        return 0
    if act == "findings":
        rows = audits.untriaged(conn) if args.untriaged else conn.execute(
            "SELECT * FROM findings" + (" WHERE state=?" if args.state else "") + " ORDER BY seq",
            (args.state,) if args.state else ()).fetchall()
        if args.json:
            print(json.dumps([dict(r) for r in rows], indent=2))
            return 0
        for row in rows:
            print(f"{row['finding_id']} {row['severity']} {row['state']:17s} {row['task_id']:24s} {row['root_cause'] or '':8s} {row['title']}")
        return 0
    if act == "show":
        row = conn.execute("SELECT * FROM findings WHERE finding_id=?", (args.finding,)).fetchone()
        if row is None:
            print(f"unknown finding {args.finding}")
            return 1
        for key in ("finding_id", "severity", "state", "task_id", "title", "repro", "expected", "reference", "step",
                    "refs_json", "root_cause", "fix_task", "fixed_sha", "regression_json", "verified_audit"):
            print(f"{key:15s} {row[key]}")
        for note in conn.execute("SELECT at, actor, kind, note, commit_sha FROM finding_notes WHERE finding_id=? ORDER BY id", (args.finding,)):
            when = datetime.fromtimestamp(note["at"]).strftime("%m-%d %H:%M")
            print(f"  {when} {note['kind']:20s} {note['actor']} {note['note']} {note['commit_sha'] or ''}".rstrip())
        return 0
    if act == "file":
        result = audits.file_finding(cfg, conn, task_id=args.task, severity=args.severity, title=args.title, repro=args.repro,
                                     expected=args.expected, job_id=args.job, audit_id=args.audit, refs=args.ref,
                                     step=args.step, reference=args.reference, evidence=args.evidence)
        print(f"{result['finding_id']} {'filed' if result['created'] else 'already filed; sighting added'}")
        return 0
    if act == "verify":
        result = audits.verify(conn, args.finding, args.result, job_id=args.job, audit_id=args.audit, note=args.note,
                               evidence=args.evidence)
        print(f"{result['finding_id']} {result['state']}")
        return 0
    if act == "triage":
        if args.group:
            result = audits.triage_group(cfg, conn, [f.strip() for f in args.group.split(",") if f.strip()], title=args.title,
                                         severity=args.severity, task_id=args.task, root_cause=args.root_cause,
                                         job_id=args.job, audit_id=args.audit)
            print(f"{result['root_cause']} -> {result['fix_task']}" + (" (queued)" if result["queued"] else " (already in flight)"))
            return 0
        target, state = (args.duplicate, "duplicate") if args.duplicate else (args.propose_wont_fix, "wont-fix-proposed")
        if not target:
            raise audits.AuditError("triage needs --group, --duplicate or --propose-wont-fix")
        result = audits.mark(conn, target, state, args.note, job_id=args.job, audit_id=args.audit)
        print(f"{result['finding_id']} {result['state']}")
        return 0
    if act == "decide":
        result = audits.mark(conn, args.finding, "wont-fix" if args.wont_fix else "open", args.note, audit_id=args.audit)
        print(f"{result['finding_id']} {result['state']}")
        return 0
    if act == "requeue":
        audits.requeue_job(conn, args.job)
        print(f"{args.job} requeued")
        return 0
    if act == "reconcile":
        result = audits.reconcile(cfg, conn)
        print(f"fixed: {', '.join(result['fixed']) or 'none'}; trailers without a regression test: {', '.join(result['refused']) or 'none'}")
        return 0
    summary = audits.report(cfg, conn, args.audit)
    print(json.dumps(summary, indent=2) if args.json else
          f"audit {summary['audit_id']} {str(summary['status']).lower()}: {summary['findings_new']} new finding(s), "
          f"{summary['verified_here']} fix(es) verified, {summary['reopened_here']} reopened, "
          f"{len(summary['escapes'])} escape(s); report in {audits.audit_dir(cfg, str(summary['audit_id'])) / 'report.md'}")  # type: ignore[arg-type]
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    cfg, _ = _open(args)
    server = dashboard.serve(cfg, args.host, args.port)
    host, port = server.server_address[:2]
    print(f"Workboard dashboard for {cfg.name}: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="workboard", description="A work queue and control room for parallel coding agents.")
    top.add_argument("--version", action="version", version=__version__)
    top.add_argument("--root", help="project directory holding workboard.toml (default: search upward from here)")
    sub = top.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(fn=fn)
        return p

    add("init", cmd_init, "write a starter workboard.toml and an example task")
    add("sync", cmd_sync, "load task files into the queue")
    add("status", cmd_status, "summary and potential interventions").add_argument("--json", action="store_true")
    add("board", cmd_board, "every task, one per line")
    add("show", cmd_show, "one task with its history").add_argument("task")
    p = add("add", cmd_add, "queue a task that has no file; the brief comes from --brief or stdin")
    p.add_argument("id")
    p.add_argument("--title", required=True)
    p.add_argument("--brief")
    p.add_argument("--priority", type=int, default=100)
    p.add_argument("--path", action="append", default=[])
    p.add_argument("--depends-on", action="append", default=[])
    add("requeue", cmd_requeue, "give a parked or cancelled task fresh attempts").add_argument("task")
    add("cancel", cmd_cancel, "stop a task").add_argument("task")
    p = add("priority", cmd_priority, "change a task's priority")
    p.add_argument("task")
    p.add_argument("priority", type=int)
    p = add("up", cmd_up, "start workers, or change how many run")
    p.add_argument("--harness")
    p.add_argument("--slots", type=int)
    p = add("down", cmd_down, "stop workers after their current task (--now stops them immediately)")
    p.add_argument("--harness")
    p.add_argument("--now", action="store_true")
    add("drain", cmd_drain, "stop new authoring and audit jobs on every harness; in-flight work still lands")
    add("resume", cmd_resume, "undo `workboard drain`")
    add("move-check", cmd_move_check, "what would be lost moving this project to another machine (exit 1 if anything)"
        ).add_argument("--json", action="store_true")
    p = add("worker", cmd_worker, "run one worker in the foreground")
    p.add_argument("--harness", required=True)
    p.add_argument("--slot", type=int, default=1)
    p.add_argument("--once", action="store_true", help="take one piece of work, then exit")
    add("land", cmd_land, "land every reviewed task that is waiting")
    add("build", cmd_build, "build the head of main and record its manifest")
    p = add("builds", cmd_builds, "list builds, or show one")
    p.add_argument("build", nargs="?")
    p.add_argument("--latest", action="store_true")
    p = add("signoff", cmd_signoff, "accept or reject a slice in a build")
    p.add_argument("verdict", choices=("accept", "reject"))
    p.add_argument("task")
    p.add_argument("--note", default="")
    p.add_argument("--build")
    add("usage", cmd_usage, "subscription allowance, resets and pace").add_argument("--json", action="store_true")
    add("interventions", cmd_interventions, "decisions waiting on a human").add_argument("--json", action="store_true")
    add("events", cmd_events, "the event log").add_argument("--limit", type=int, default=40)
    p = add("audit", cmd_audit, "audit what landed and track every defect to a verified fix")
    asub = p.add_subparsers(dest="audit_cmd", required=True)
    a = asub.add_parser("open", help="open an audit of the latest good build (or --build, or --sha)")
    a.add_argument("--build")
    a.add_argument("--sha")
    a.add_argument("--quick", action="store_true", help="only verify fixes and run checklists marked quick")
    asub.add_parser("status", help="the latest audit, live findings and decisions waiting on a person")
    asub.add_parser("jobs", help="the jobs of an audit").add_argument("--audit")
    a = asub.add_parser("findings", help="list findings")
    a.add_argument("--untriaged", action="store_true")
    a.add_argument("--state")
    a.add_argument("--json", action="store_true")
    asub.add_parser("show", help="one finding with its history").add_argument("finding")
    a = asub.add_parser("file", help="record a defect (agents pass --job; a person may pass --audit or nothing)")
    for flag in ("--job", "--audit", "--step", "--reference"):
        a.add_argument(flag)
    a.add_argument("--task", required=True, help="the task that owns the defect")
    a.add_argument("--severity", required=True, choices=audits.SEVERITIES)
    a.add_argument("--title", required=True)
    a.add_argument("--repro", required=True)
    a.add_argument("--expected", required=True)
    a.add_argument("--ref", action="append", default=[])
    a.add_argument("--evidence", action="append", default=[])
    a = asub.add_parser("verify", help="record whether a fixed finding is fixed in this build")
    a.add_argument("finding")
    a.add_argument("--result", required=True, choices=("verified", "reopened"))
    a.add_argument("--job")
    a.add_argument("--audit")
    a.add_argument("--note", default="")
    a.add_argument("--evidence", action="append", default=[])
    a = asub.add_parser("triage", help="group findings into a root cause with one fix task, or mark them")
    for flag in ("--job", "--audit", "--group", "--title", "--task", "--root-cause", "--duplicate", "--propose-wont-fix"):
        a.add_argument(flag)
    a.add_argument("--severity", choices=audits.SEVERITIES)
    a.add_argument("--note", default="")
    a = asub.add_parser("decide", help="a person's decision on a proposed wont-fix")
    a.add_argument("finding")
    group = a.add_mutually_exclusive_group(required=True)
    group.add_argument("--wont-fix", action="store_true")
    group.add_argument("--reopen", action="store_true")
    a.add_argument("--note", default="")
    a.add_argument("--audit")
    asub.add_parser("requeue", help="give a parked or finished audit job another run").add_argument("job")
    asub.add_parser("reconcile", help="mark findings fixed from landed trailers")
    a = asub.add_parser("report", help="write the audit's report and print its numbers")
    a.add_argument("--audit")
    a.add_argument("--json", action="store_true")
    p = add("dashboard", cmd_dashboard, "serve the control room page")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except (config.ConfigError, tasks.TaskError, audits.AuditError, ValueError, KeyError, RuntimeError) as exc:
        print(f"workboard: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
