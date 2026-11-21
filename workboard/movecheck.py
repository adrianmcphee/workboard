"""What would be lost moving a project to another machine, or retiring this one. Read-only."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3

from . import gitops, queue, store
from .config import Config

STATE_PARTS = (("workboard.db", "queue, attempts, events and audit records"),
               ("handoffs", "prompts, takeover notes, review verdicts"),
               ("builds", "build records and sign-off verdicts"),
               ("audits", "audit evidence, findings and reports"),
               ("logs", "agent, landing and build output"),
               ("compile", "compile queue event log"))


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for folder, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                pass
    return total


def _unique(cfg: Config, cwd: Path, ref: str) -> tuple[int, int]:
    """Commits on `ref` that no remote ref holds, and how many of those have no equivalent patch on main."""
    ahead = int(gitops.out(cwd, "rev-list", "--count", ref, "--not", "--remotes") or 0)
    if not ahead:
        return 0, 0
    cherry = gitops.git(cwd, "cherry", cfg.remote_main, ref).stdout.splitlines()
    return ahead, min(ahead, sum(1 for line in cherry if line.startswith("+")))


def check(cfg: Config, conn: sqlite3.Connection) -> dict[str, object]:
    repo = cfg.repo
    refs = []
    for ref in gitops.out(repo, "for-each-ref", "--format=%(refname)").splitlines():
        if ref.startswith("refs/remotes/") or ref == "refs/stash":
            continue
        ahead, unique = _unique(cfg, repo, ref)
        if ahead:
            refs.append({"ref": ref, "ahead": ahead, "unique": unique})
    stashes = [line for line in gitops.git(repo, "stash", "list", "--format=%gd %h %s").stdout.splitlines() if line]
    worktrees = []
    for block in gitops.out(repo, "worktree", "list", "--porcelain").split("\n\n"):
        fields = dict(line.split(" ", 1) if " " in line else (line, "") for line in block.splitlines())
        path = fields.get("worktree")
        if not path:
            continue
        if not Path(path).is_dir():
            worktrees.append({"path": path, "missing": True, "dirty": 0, "detached_unique": 0})
            continue
        dirty = len([line for line in gitops.dirty(Path(path)).splitlines() if line])
        detached = 0 if "branch" in fields else _unique(cfg, Path(path), "HEAD")[1]
        worktrees.append({"path": path, "branch": fields.get("branch", "(detached)"), "missing": False,
                          "dirty": dirty, "detached_unique": detached})
    marks = ",".join("?" * len(store.IN_FLIGHT))
    flying = {r[0]: r[1] for r in conn.execute(
        f"SELECT status, COUNT(*) FROM tasks WHERE status IN ({marks}) GROUP BY status", store.IN_FLIGHT)}
    waiting = {r[0]: r[1] for r in conn.execute(
        "SELECT status, COUNT(*) FROM tasks WHERE status IN ('READY','BLOCKED','NEEDS_TRIAGE') GROUP BY status")}
    state_paths = [{"path": str(cfg.state / name), "holds": what, "bytes": _size(cfg.state / name)}
                   for name, what in STATE_PARTS if (cfg.state / name).exists()]
    would_lose = (any(r["unique"] for r in refs) or bool(stashes) or bool(flying)
                  or any(w["dirty"] or w["detached_unique"] for w in worktrees))
    return {"would_lose": would_lose, "draining": queue.draining(conn), "refs": refs, "stashes": stashes,
            "worktrees": worktrees, "tasks_in_flight": flying, "tasks_waiting": waiting, "state_paths": state_paths}


def lines(report: dict[str, object]) -> list[str]:
    out = []
    unique = [r for r in report["refs"] if r["unique"]]  # type: ignore[union-attr]
    landed = [r for r in report["refs"] if not r["unique"]]  # type: ignore[union-attr]
    out.append(f"refs with commits on no remote: {len(unique)} unique, {len(landed)} already on main by patch")
    out += [f"  LOSE {r['ref']}: {r['unique']} of {r['ahead']} commit(s) have no equivalent on main" for r in unique]
    out.append(f"stashes: {len(report['stashes'])}")  # type: ignore[arg-type]
    out += [f"  LOSE {s}" for s in report["stashes"]]  # type: ignore[union-attr]
    for w in report["worktrees"]:  # type: ignore[union-attr]
        if w["missing"]:
            out.append(f"  worktree {w['path']} is missing; `git worktree prune` forgets it")
        elif w["dirty"] or w["detached_unique"]:
            out.append(f"  LOSE worktree {w['path']}: {w['dirty']} uncommitted path(s)"
                       + (f", {w['detached_unique']} unique detached commit(s)" if w["detached_unique"] else ""))
    flying = report["tasks_in_flight"]
    out.append("tasks in flight: " + ("  ".join(f"{k}={v}" for k, v in sorted(flying.items())) or "none")  # type: ignore[union-attr]
               + ("" if report["draining"] or not flying else "  (run `workboard drain` and let them land)"))
    waiting = report["tasks_waiting"]
    if waiting:
        out.append("tasks waiting: " + "  ".join(f"{k}={v}" for k, v in sorted(waiting.items()))  # type: ignore[union-attr]
                   + "  (their definitions are in git; their history is in the database)")
    out.append("copy after the drain, with workers stopped:")
    out += [f"  {p['path']}  {p['bytes'] / 2**20:.1f} MiB  ({p['holds']})" for p in report["state_paths"]]  # type: ignore[union-attr]
    out.append("nothing unique would be lost" if not report["would_lose"] else
               "work above marked LOSE exists only on this machine: land it, push it or bundle it before moving")
    return out
