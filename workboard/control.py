"""Worker control: start, resize and stop workers, and the closed list of dashboard operations."""
from __future__ import annotations

import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

from . import builds, directives, queue, store
from .config import Config

MAX_SLOTS = 16


_LAUNCH = ("import subprocess, sys; subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, "
           "start_new_session=True)")


def _detach(argv: list[str], log: Path, cwd: Path, env: dict[str, str]) -> None:
    """Start a process that outlives this one and is never left behind as a zombie of it."""
    with open(log, "ab") as fh:
        subprocess.run([sys.executable, "-c", _LAUNCH, *argv], cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                       stdout=fh, stderr=subprocess.STDOUT, check=True)


def set_slots(cfg: Config, conn: sqlite3.Connection, harness: str, slots: int, actor: str = "human") -> dict[str, object]:
    """Run `harness` at `slots` workers. Extra workers finish their current task and exit; none is killed."""
    if harness not in cfg.harnesses:
        raise ValueError(f"unknown harness {harness!r}")
    if not 0 <= slots <= MAX_SLOTS:
        raise ValueError(f"slots must be 0 to {MAX_SLOTS}")
    with store.write(conn):
        store.set_meta(conn, f"slots:{harness}", str(slots))
        store.event(conn, actor, "slots", None, f"{harness}={slots}")
    live = {r["slot"] for r in conn.execute("SELECT slot, pid FROM workers WHERE harness=?", (harness,)).fetchall()
            if queue.pid_alive(r["pid"])}
    started = []
    package_parent = str(Path(__file__).resolve().parent.parent)
    pythonpath = os.pathsep.join(filter(None, [package_parent, os.environ.get("PYTHONPATH", "")]))
    logs = cfg.state / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for slot in range(1, slots + 1):
        if slot in live:
            continue
        _detach([sys.executable, "-m", "workboard", "worker", "--harness", harness, "--slot", str(slot)],
                logs / f"worker-{harness}-{slot}.log", cfg.root,
                {**os.environ, "WORKBOARD_ROOT": str(cfg.root), "PYTHONPATH": pythonpath})
        started.append(slot)
    return {"harness": harness, "slots": slots, "started": started, "draining": sorted(s for s in live if s > slots)}


def stop_now(cfg: Config, conn: sqlite3.Connection, harness: str | None = None, actor: str = "human") -> list[int]:
    """Signal worker processes by their recorded pid. Their agents stop and their tasks become takeovers."""
    stopped = []
    for name in ([harness] if harness else list(cfg.harnesses)):
        with store.write(conn):
            store.set_meta(conn, f"slots:{name}", "0")
            store.event(conn, actor, "stop_now", None, name)
    rows = conn.execute("SELECT pid FROM workers" + (" WHERE harness=?" if harness else ""), (harness,) if harness else ()).fetchall()
    for row in rows:
        if queue.pid_alive(row["pid"]):
            try:
                os.kill(row["pid"], signal.SIGTERM)
                stopped.append(row["pid"])
            except OSError:
                pass
    return stopped


def run(cfg: Config, conn: sqlite3.Connection, name: str, args: dict[str, object]) -> dict[str, object]:
    """Execute one dashboard operation. Only the operations named here exist."""
    try:
        if name == "workers":
            result = set_slots(cfg, conn, str(args.get("harness") or ""), int(args.get("slots", -1)))  # type: ignore[arg-type]
            return {"ok": True, "output": f"{result['harness']} set to {result['slots']} slot(s); started {result['started']}, draining {result['draining']}"}
        if name == "requeue":
            return {"ok": True, "output": f"{args['task']} -> {queue.requeue(conn, str(args['task']))}"}
        if name == "cancel":
            queue.cancel(conn, str(args["task"]))
            return {"ok": True, "output": f"{args['task']} cancelled"}
        if name == "priority":
            queue.set_priority(conn, str(args["task"]), int(args["priority"]))  # type: ignore[arg-type]
            return {"ok": True, "output": f"{args['task']} priority {args['priority']}"}
        if name in ("signoff_accept", "signoff_reject"):
            entry = builds.sign(cfg, conn, str(args.get("build") or "") or None, str(args.get("task") or ""),
                                name == "signoff_accept", str(args.get("note") or ""))
            extra = f"; queued {entry['followup']}" if entry.get("followup") else ""
            return {"ok": True, "output": f"{args.get('task')} {entry['verdict']}{extra}"}
        if name == "directive_set":
            res = directives.set_directive(
                conn, mode=str(args.get("mode") or ""), message=str(args.get("message") or ""),
                harness=str(args.get("harness") or "") or None, role=str(args.get("role") or "") or None,
                task_id=str(args.get("task") or "") or None,
                ttl_min=float(args["ttl_min"]) if args.get("ttl_min") else None,  # type: ignore[arg-type]
                kill_builds=bool(args.get("kill_builds")), valid_harnesses=set(cfg.harnesses), actor="human",
            )
            did = res["directive"]["directive_id"]  # type: ignore[index]
            return {"ok": True, "output": f"set {did} ({len(res['signalled_builds'])} build(s) stopped)"}  # type: ignore[arg-type]
        if name == "directive_clear":
            ids = directives.clear(conn, directive_id=str(args.get("directive_id") or "") or None,
                                   clear_all=bool(args.get("all")), actor="human")
            return {"ok": True, "output": f"cleared {', '.join(ids) or 'nothing'}"}
        return {"ok": False, "output": f"unknown operation {name!r}"}
    except (KeyError, ValueError, TypeError) as exc:
        return {"ok": False, "output": f"{type(exc).__name__}: {exc}"[:400]}


def wait_stopped(conn: sqlite3.Connection, pids: list[int], timeout_s: float = 15.0) -> bool:
    end = time.time() + timeout_s
    while time.time() < end and any(queue.pid_alive(p) for p in pids):
        time.sleep(0.2)
    return not any(queue.pid_alive(p) for p in pids)
