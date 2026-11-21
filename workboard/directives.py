"""Live operator directives and tracked active compiles in the state database.

Directives broadcast scoped instructions or immediate control actions across
running workers, compile shims and interactive commands:

- `instruction`: plain guidance added to matching prompts and printed once on
  stderr before a heavy compile starts.
- `pause_claims`: hold new claims in matching scope (`author`, `review`,
  `audit`, or all roles).
- `wrap_up`: hold new claims and refuse new heavy compiles in scope with a
  wrap-up message on stderr so running agents save their notes and finish.
- `cancel_builds`: send `SIGTERM` to matching tracked compiles and refuse new
  heavy compiles while active.
- `abort_leases`: hold claims, cancel compiles, and end matching active leases
  at the next heartbeat without spending an attempt.
"""
from __future__ import annotations

import fnmatch
import os
import signal
import sqlite3
import time

from . import store

MODES = ("instruction", "pause_claims", "wrap_up", "cancel_builds", "abort_leases")
CLAIM_BLOCK_MODES = ("pause_claims", "wrap_up", "abort_leases")
BUILD_BLOCK_MODES = ("wrap_up", "cancel_builds", "abort_leases")
ROLES = ("author", "review", "audit", "interactive")


def scope_label(row: sqlite3.Row | dict[str, object]) -> str:
    parts = [f"{label}={row[key]}" for key, label in (
        ("scope_harness", "harness"), ("scope_role", "role"), ("scope_task", "task")
    ) if row[key]]
    return " ".join(parts) if parts else "all agents"


def matches(row: sqlite3.Row | dict[str, object], *, harness: str | None = None,
            role: str | None = None, task_id: str | None = None,
            modes: tuple[str, ...] | None = None) -> bool:
    if modes is not None and str(row["mode"]) not in modes:
        return False
    if row["scope_harness"] and (harness or "") != row["scope_harness"]:
        return False
    if row["scope_role"] and (role or "interactive") != row["scope_role"]:
        return False
    if row["scope_task"] and (not task_id or not fnmatch.fnmatchcase(str(task_id), str(row["scope_task"]))):
        return False
    return True


def active(conn: sqlite3.Connection, now: float | None = None) -> list[dict[str, object]]:
    ts = time.time() if now is None else now
    rows = conn.execute(
        "SELECT * FROM directives WHERE active=1 AND (expires_at IS NULL OR expires_at>?) ORDER BY seq",
        (ts,),
    ).fetchall()
    out: list[dict[str, object]] = []
    for row in rows:
        item = dict(row)
        item["scope"] = scope_label(item)
        item["remaining_s"] = max(0, round(float(item["expires_at"]) - ts)) if item["expires_at"] is not None else None
        out.append(item)
    return out


def matching(conn: sqlite3.Connection, *, harness: str | None = None, role: str | None = None,
             task_id: str | None = None, modes: tuple[str, ...] | None = None,
             now: float | None = None) -> list[dict[str, object]]:
    return [r for r in active(conn, now) if matches(r, harness=harness, role=role, task_id=task_id, modes=modes)]


def claim_blocker(conn: sqlite3.Connection, *, harness: str | None = None, role: str | None = None,
                  task_id: str | None = None, now: float | None = None) -> dict[str, object] | None:
    hits = matching(conn, harness=harness, role=role, task_id=task_id, modes=CLAIM_BLOCK_MODES, now=now)
    return hits[0] if hits else None


def abort_directive(conn: sqlite3.Connection, *, harness: str | None = None, role: str | None = None,
                    task_id: str | None = None, now: float | None = None) -> dict[str, object] | None:
    hits = matching(conn, harness=harness, role=role, task_id=task_id, modes=("abort_leases",), now=now)
    return hits[0] if hits else None


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def live_builds(conn: sqlite3.Connection, now: float | None = None) -> list[dict[str, object]]:
    ts = time.time() if now is None else now
    out: list[dict[str, object]] = []
    for row in conn.execute("SELECT * FROM active_builds ORDER BY started_at").fetchall():
        if _pid_alive(int(row["pid"])):
            item = dict(row)
            item["age_s"] = round(max(0.0, ts - float(item["started_at"])), 1)
            out.append(item)
    return out


def _cancel_builds_in_tx(conn: sqlite3.Connection, *, harness: str | None, role: str | None,
                         task_id: str | None) -> list[dict[str, object]]:
    rows = conn.execute("SELECT * FROM active_builds ORDER BY started_at").fetchall()
    dead: list[str] = []
    signalled: list[dict[str, object]] = []
    probe = {"mode": "cancel_builds", "scope_harness": harness, "scope_role": role, "scope_task": task_id}
    my_pid, my_pgrp = os.getpid(), os.getpgrp()
    for row in rows:
        bid, pid, pgid = str(row["build_id"]), int(row["pid"]), int(row["pgid"])
        if not _pid_alive(pid):
            dead.append(bid)
            continue
        if not matches(probe, harness=row["harness"], role=row["role"] or "interactive", task_id=row["task_id"]):
            continue
        try:
            if pgid > 1 and pgid == pid and pgid != my_pgrp:
                os.killpg(pgid, signal.SIGTERM)
            elif pid > 1 and pid != my_pid:
                os.kill(pid, signal.SIGTERM)
            else:
                continue
            signalled.append({"build_id": bid, "pid": pid, "task_id": row["task_id"], "cmd": row["cmd"]})
        except OSError:
            dead.append(bid)
    for bid in dead:
        conn.execute("DELETE FROM active_builds WHERE build_id=?", (bid,))
    return signalled


def set_directive(conn: sqlite3.Connection, *, mode: str, message: str, harness: str | None = None,
                  role: str | None = None, task_id: str | None = None, ttl_min: float | None = None,
                  kill_builds: bool = False, valid_harnesses: set[str] | None = None,
                  actor: str = "human", now: float | None = None) -> dict[str, object]:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    clean = message.strip()
    if not clean:
        raise ValueError("directive message must not be empty")
    h_val = (harness or "").strip() or None
    if h_val and valid_harnesses is not None and h_val not in valid_harnesses:
        raise ValueError(f"unknown harness {h_val!r}")
    r_val = (role or "").strip() or None
    if r_val and r_val not in ROLES:
        raise ValueError(f"role must be one of {', '.join(ROLES)}")
    t_val = (task_id or "").strip() or None
    if ttl_min is not None and ttl_min <= 0:
        raise ValueError("ttl_min must be positive")
    ts = time.time() if now is None else now
    expires_at = (ts + float(ttl_min) * 60.0) if ttl_min is not None else None
    with store.write(conn):
        conn.execute("UPDATE directives SET active=0, cleared_at=? WHERE active=1 AND expires_at IS NOT NULL AND expires_at<=?",
                     (ts, ts))
        seq = int(conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM directives").fetchone()[0])
        did = f"D-{seq:04d}"
        conn.execute(
            "INSERT INTO directives (directive_id, seq, active, created_at, expires_at, cleared_at, "
            "scope_harness, scope_role, scope_task, mode, message) VALUES (?,?,1,?,?,NULL,?,?,?,?,?)",
            (did, seq, ts, expires_at, h_val, r_val, t_val, mode, clean),
        )
        signalled = (_cancel_builds_in_tx(conn, harness=h_val, role=r_val, task_id=t_val)
                     if mode in ("cancel_builds", "abort_leases") or kill_builds else [])
        row = dict(conn.execute("SELECT * FROM directives WHERE directive_id=?", (did,)).fetchone())
        row["scope"] = scope_label(row)
        store.event(conn, actor, "directive_set", t_val, f"{did} [{mode}] ({row['scope']}): {clean}", ts)
    return {"directive": row, "signalled_builds": signalled}


def clear(conn: sqlite3.Connection, *, directive_id: str | None = None, clear_all: bool = False,
          actor: str = "human", now: float | None = None) -> list[str]:
    if not directive_id and not clear_all:
        raise ValueError("name a directive such as D-0001, or pass --all")
    ts = time.time() if now is None else now
    with store.write(conn):
        if clear_all:
            ids = [str(r[0]) for r in conn.execute("SELECT directive_id FROM directives WHERE active=1 ORDER BY seq")]
            conn.execute("UPDATE directives SET active=0, cleared_at=? WHERE active=1", (ts,))
        else:
            row = conn.execute("SELECT directive_id FROM directives WHERE directive_id=? AND active=1",
                               (directive_id,)).fetchone()
            ids = [str(row[0])] if row else []
            if ids:
                conn.execute("UPDATE directives SET active=0, cleared_at=? WHERE directive_id=?", (ts, directive_id))
        if ids:
            store.event(conn, actor, "directive_cleared", None, ", ".join(ids), ts)
    return ids


def list_all(conn: sqlite3.Connection, *, include_inactive: bool = False,
             now: float | None = None) -> list[dict[str, object]]:
    if not include_inactive:
        return active(conn, now)
    ts = time.time() if now is None else now
    out: list[dict[str, object]] = []
    for row in conn.execute("SELECT * FROM directives ORDER BY seq DESC LIMIT 50").fetchall():
        item = dict(row)
        expired = item["expires_at"] is not None and float(item["expires_at"]) <= ts
        item["effective_active"] = bool(item["active"] and not expired)
        item["scope"] = scope_label(item)
        out.append(item)
    return out


def prompt_lines(conn: sqlite3.Connection | None, *, harness: str, role: str,
                 task_id: str | None = None, now: float | None = None) -> list[str]:
    if conn is None:
        return []
    rows = matching(conn, harness=harness, role=role, task_id=task_id, now=now)
    return [f"- **Operator directive `{r['directive_id']}` ({r['mode']}, {r['scope']}):** {r['message']}" for r in rows]
