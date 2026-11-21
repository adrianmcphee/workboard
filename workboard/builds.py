"""Scheduled builds, their manifests and human sign-off.

`workboard build` builds the current head of main in its own worktree and writes one record: the
slices that landed since the previous good build, their author and reviewer, and the slices that
were expected aboard but missed. A human accepts or rejects each slice; a rejection becomes a
new top-priority task and is counted against the slice's author and reviewer.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

from . import compile_queue, gitops, store, tasks, train
from .config import Config

SLOT_TOLERANCE = timedelta(minutes=25)
FOLLOWUP_PRIORITY = 1000
BUILD_WORKTREE = "_build"


def _dir(cfg: Config) -> Path:
    path = cfg.state / "builds"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write(path: Path, data: dict[str, object]) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _planned_path(cfg: Config, slot: str) -> Path:
    return _dir(cfg) / f"planned-{slot.replace(' ', 'T').replace(':', '')}.json"


def note_planned(cfg: Config, manifest: dict[str, object], now: float | None = None) -> None:
    """Remember every slice that was ever expected aboard an upcoming build."""
    for build in manifest.get("builds", []):  # type: ignore[union-attr]
        if not build["tasks"]:
            continue
        path = _planned_path(cfg, str(build["build"]))
        data = _read(path) or {"build": build["build"], "tasks": {}}
        new = [t["task_id"] for t in build["tasks"] if t["task_id"] not in data["tasks"]]  # type: ignore[operator]
        if new:
            for task_id in new:
                data["tasks"][task_id] = now or time.time()  # type: ignore[index]
            _write(path, data)


def records(cfg: Config) -> list[dict[str, object]]:
    return [r for r in (_read(p) for p in sorted(_dir(cfg).glob("build-*.json"))) if r]


def latest(cfg: Config, result: str = "built") -> dict[str, object] | None:
    found = [r for r in records(cfg) if r.get("result") == result]
    return found[-1] if found else None


def slot_for(cfg: Config, started: datetime) -> str | None:
    for day in (0, -1, 1):
        for hour, minute in cfg.build_schedule:
            slot = (started + timedelta(days=day)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if abs(started - slot) <= SLOT_TOLERANCE:
                return train.build_id(slot)
    return None


def _slices(conn: sqlite3.Connection, commits: set[str]) -> list[dict[str, object]]:
    out = []
    for row in gitops.landed_tasks(conn, commits):
        reviewer = conn.execute("SELECT harness FROM attempts WHERE task_id=? AND role='review' AND outcome='pass' "
                                "ORDER BY id DESC LIMIT 1", (row["task_id"],)).fetchone()
        out.append({"task_id": row["task_id"], "title": row["title"], "landed_sha": row["landed_sha"],
                    "author": row["author_harness"], "reviewer": reviewer[0] if reviewer else None})
    return out


def record(cfg: Config, conn: sqlite3.Connection, sha: str, result: str, started: datetime, log: str = "") -> dict[str, object]:
    """Write the manifest for one build of `sha`."""
    good = latest(cfg)
    previous = str(good["sha"]) if good else None
    commits = gitops.commits_between(cfg, previous, sha) or set()
    slot = slot_for(cfg, started)
    landed = _slices(conn, commits)
    planned = (_read(_planned_path(cfg, slot)) or {}).get("tasks", {}) if slot else {}
    aboard = {s["task_id"] for s in landed}
    data: dict[str, object] = {
        "id": f"{started.strftime('%Y%m%dT%H%M%S')}-{sha[:9]}", "sha": sha, "previous_sha": previous,
        "slot": slot or "unscheduled", "started_at": started.strftime("%Y-%m-%d %H:%M"), "result": result,
        "commit_count": len(commits), "slices": landed, "log": log, "signoff": {},
        "missed": sorted(t for t in planned if t not in aboard),  # type: ignore[union-attr]
    }
    _write(_dir(cfg) / f"build-{data['id']}.json", data)
    with store.write(conn):
        store.event(conn, "system", f"build_{result}", None, f"{data['id']} {len(landed)} slice(s)")
    return data


def run(cfg: Config, conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, object]:
    """Build the head of main in a dedicated worktree, so no task checkout is disturbed."""
    if not cfg.build_command:
        raise ValueError("[build] command is not set in workboard.toml")
    started = now or datetime.now()
    gitops.fetch(cfg)
    sha = gitops.out(cfg.repo, "rev-parse", cfg.remote_main)
    good = latest(cfg)
    if good and good["sha"] == sha:
        return {**good, "unchanged": True}
    worktree = cfg.worktree(BUILD_WORKTREE)
    if (worktree / ".git").exists():
        gitops.out(worktree, "checkout", "--detach", sha)
    else:
        worktree.parent.mkdir(parents=True, exist_ok=True)
        gitops.out(cfg.repo, "worktree", "add", "--detach", str(worktree), sha)
    logs = cfg.state / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"build-{started.strftime('%Y%m%d-%H%M%S')}.log"
    with open(log, "ab") as fh:
        done = subprocess.run(cfg.build_command, shell=True, cwd=str(worktree), stdout=fh, stderr=subprocess.STDOUT,
                              env={**os.environ, **compile_queue.env(cfg), "WORKBOARD_BUILD_SHA": sha}, check=False)
    return record(cfg, conn, sha, "built" if done.returncode == 0 else "failed", started, str(log))


def find(cfg: Config, build: str | None) -> dict[str, object]:
    found = latest(cfg) if not build else next((r for r in records(cfg) if r["id"] == build), None)
    if not found:
        raise ValueError(f"no build record {build or '(latest good build)'}")
    return found


def pending(build: dict[str, object]) -> list[str]:
    return [str(s["task_id"]) for s in build["slices"] if s["task_id"] not in build["signoff"]]  # type: ignore[union-attr,operator]


def sign(cfg: Config, conn: sqlite3.Connection, build_id: str | None, task_id: str, accepted: bool,
         note: str = "", actor: str = "human") -> dict[str, object]:
    """Record a verdict on one slice of a build. A rejection needs a note and queues a fix."""
    build = find(cfg, build_id)
    piece = next((s for s in build["slices"] if s["task_id"] == task_id), None)  # type: ignore[union-attr]
    if not piece:
        raise ValueError(f"{task_id} did not land in build {build['id']}")
    note = note.strip()
    if not accepted and not note:
        raise ValueError("a rejection needs a note saying what is wrong")
    entry: dict[str, object] = {"verdict": "accepted" if accepted else "rejected", "note": note,
                                "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                "author": piece.get("author"), "reviewer": piece.get("reviewer")}
    if not accepted:
        row = conn.execute("SELECT title, paths_json FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        number = 1
        while conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (f"{task_id}-fix{number}",)).fetchone():
            number += 1
        brief = (f"The slice `{task_id}` ({row['title']}) landed in build `{build['id']}` and was rejected at sign-off.\n\n"
                 f"## What the reviewer of the build said\n\n{note}\n\n## Outcome\n\n"
                 "- Reproduce what the note describes in the running product.\n"
                 "- Fix every code path that contradicts it.\n"
                 "- Add a deterministic test that fails before the fix and passes after it.\n")
        entry["followup"] = tasks.add(conn, cfg, f"{task_id}-fix{number}", f"Sign-off fix: {row['title']}", brief,
                                      priority=FOLLOWUP_PRIORITY, paths=store.loads(row["paths_json"]), actor=actor)
    build["signoff"][task_id] = entry  # type: ignore[index]
    _write(_dir(cfg) / f"build-{build['id']}.json", build)
    with store.write(conn):
        store.event(conn, actor, f"signoff_{entry['verdict']}", task_id, note[:300])
    return entry


def escapes(cfg: Config) -> dict[str, dict[str, dict[str, int]]]:
    """Sign-off verdicts per author and per reviewer: a rejected slice is one its reviewer passed."""
    out: dict[str, dict[str, dict[str, int]]] = {"author": {}, "reviewer": {}}
    for build in records(cfg):
        for entry in build.get("signoff", {}).values():  # type: ignore[union-attr]
            for role in ("author", "reviewer"):
                tally = out[role].setdefault(entry.get(role) or "unknown", {"accepted": 0, "rejected": 0})
                tally[entry["verdict"]] += 1
    return out


def summary(cfg: Config) -> dict[str, object]:
    good, bad = latest(cfg), latest(cfg, "failed")
    out: dict[str, object] = {"latest": None, "failed": None, "escapes": escapes(cfg)}
    if good:
        out["latest"] = {**good, "pending": pending(good)}
    if bad and (not good or bad["id"] > good["id"]):
        out["failed"] = bad
    return out
