"""Tasks are Markdown files: a small header of facts, then the brief the agent reads."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3
import time

from . import queue, store
from .config import Config

_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class TaskError(Exception):
    pass


def _value(raw: str) -> object:
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [item.strip().strip("\"'") for item in raw[1:-1].split(",") if item.strip()]
    return raw.strip("\"'")


def parse(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise TaskError(f"{path}: must start with a `---` header")
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        raise TaskError(f"{path}: header is not closed with `---`")
    fields: dict[str, object] = {}
    for line in head.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, colon, raw = line.partition(":")
        if not colon:
            raise TaskError(f"{path}: cannot read header line {line!r}")
        fields[key.strip()] = _value(raw)
    task_id = str(fields.get("id") or path.stem)
    if not _ID.fullmatch(task_id):
        raise TaskError(f"{path}: id {task_id!r} must be lowercase letters, digits, dot, dash or underscore")
    if not str(fields.get("title") or "").strip():
        raise TaskError(f"{path}: title is required")
    if not body.strip():
        raise TaskError(f"{path}: the brief is empty")

    def listing(key: str) -> list[str]:
        value = fields.get(key, [])
        return [str(v) for v in value] if isinstance(value, list) else [str(value)] if value else []

    return {
        "id": task_id, "title": str(fields["title"]), "priority": int(str(fields.get("priority", 100))),
        "harness": str(fields.get("harness", "")) or None, "lease_min": int(str(fields.get("lease_min", 0))),
        "paths": listing("paths"), "depends_on": listing("depends_on"), "brief_path": str(path),
    }


def upsert(conn: sqlite3.Connection, cfg: Config, task: dict[str, object], source: str, now: float) -> bool:
    """Insert a new task or refresh the definition of an existing one; never touches its progress."""
    lease = int(task.get("lease_min") or 0) or cfg.default_lease_min
    row = conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task["id"],)).fetchone()
    values = (task["title"], int(task.get("priority", 100)), task.get("harness"), lease,
              json.dumps(task.get("paths", [])), json.dumps(task.get("depends_on", [])), task["brief_path"])
    if row:
        conn.execute("UPDATE tasks SET title=?, priority=?, harness_pref=?, lease_min=?, paths_json=?, "
                     "depends_json=?, brief_path=? WHERE task_id=?", (*values, task["id"]))
        return False
    conn.execute(
        "INSERT INTO tasks (title, priority, harness_pref, lease_min, paths_json, depends_json, brief_path, "
        "task_id, branch, source, status, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,'BLOCKED',?)",
        (*values, task["id"], f"{cfg.branch_prefix}{task['id']}", source, now))
    store.event(conn, "system", "task_added", str(task["id"]), str(task["title"]), now)
    return True


def sync(conn: sqlite3.Connection, cfg: Config, now: float | None = None) -> dict[str, object]:
    """Load every task file. Files that disappear leave their task alone; cancel is an explicit act."""
    now = now or time.time()
    parsed, errors = [], []
    for path in sorted(cfg.tasks.glob("*.md")) if cfg.tasks.is_dir() else []:
        try:
            parsed.append(parse(path))
        except (TaskError, ValueError) as exc:
            errors.append(str(exc))
    ids = [t["id"] for t in parsed]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        errors.append(f"task id {dup!r} is defined by more than one file")
    added = 0
    with store.write(conn):
        known = {r[0] for r in conn.execute("SELECT task_id FROM tasks")} | set(ids)
        for task in parsed:
            missing = [d for d in task["depends_on"] if d not in known]  # type: ignore[union-attr]
            if missing:
                errors.append(f"{task['id']}: depends on unknown task(s) {', '.join(missing)}")
                continue
            added += upsert(conn, cfg, task, "file", now)
        queue._refresh(conn, cfg, now)
    return {"files": len(parsed), "added": added, "errors": errors}


def add(conn: sqlite3.Connection, cfg: Config, task_id: str, title: str, brief: str, priority: int = 100,
        paths: list[str] | None = None, depends_on: list[str] | None = None, harness: str | None = None,
        actor: str = "human", now: float | None = None) -> str:
    """Create a task that has no file in the repository; its brief lives in the state directory."""
    with store.write(conn):
        insert(conn, cfg, task_id, title, brief, priority=priority, paths=paths, depends_on=depends_on,
               harness=harness, actor=actor, now=now)
        queue._refresh(conn, cfg, now or time.time())
    return task_id


def insert(conn: sqlite3.Connection, cfg: Config, task_id: str, title: str, brief: str, priority: int = 100,
           paths: list[str] | None = None, depends_on: list[str] | None = None, harness: str | None = None,
           actor: str = "human", now: float | None = None) -> str:
    """Create a file-less task inside the caller's transaction (see `add` for the standalone form)."""
    if not _ID.fullmatch(task_id):
        raise TaskError(f"id {task_id!r} must be lowercase letters, digits, dot, dash or underscore")
    now = now or time.time()
    if conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
        raise TaskError(f"task {task_id!r} already exists")
    brief_path = cfg.state / "briefs" / f"{task_id}.md"
    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text(brief, encoding="utf-8")
    upsert(conn, cfg, {"id": task_id, "title": title, "priority": priority, "harness": harness,
                       "paths": paths or [], "depends_on": depends_on or [], "brief_path": str(brief_path)},
           "queue", now)
    store.event(conn, actor, "task_created", task_id, title, now)
    return task_id
