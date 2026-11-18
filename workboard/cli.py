"""The `workboard` command."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

from . import __version__, builds, config, control, dashboard, interventions, queue, store, tasks, template, train, usage, worker


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
    for line in snap["usage_lines"]:  # type: ignore[union-attr]
        print(f"  usage: {line}")
    for build in snap["train"]["builds"][:2]:  # type: ignore[index]
        print(f"  build: {build['label']} in {build['minutes_away']}m, cutoff {build['cutoff']}, {len(build['tasks'])} slice(s) in flight")
    latest = snap["builds"]["latest"]  # type: ignore[index]
    if latest:
        print(f"  sign-off: build {latest['id']}, {len(latest['pending'])} of {len(latest['slices'])} awaiting a verdict")
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
    cards = interventions.collect(cfg, conn, usage.snapshot(cfg), worker.live_slots(conn), train.manifest(conn, cfg, datetime.now()))
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
    p = add("dashboard", cmd_dashboard, "serve the control room page")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    return top


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except (config.ConfigError, tasks.TaskError, ValueError, KeyError, RuntimeError) as exc:
        print(f"workboard: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
