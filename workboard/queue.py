"""The task state machine. Every transition happens here, inside one transaction, and leaves an event.

BLOCKED -> READY -> CLAIMED -> AWAITING_REVIEW -> REVIEWING -> LANDING -> DONE
                       |              ^               |           |
                       v              |               v           v
                 NEEDS_TAKEOVER <-----+---------------+-----------+   (then claimed again)
                       |
                       v
                 NEEDS_TRIAGE   (attempt limit reached; a human decides)
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid

from . import store
from .config import Config

_CLEAR_LEASE = ("lease_owner=NULL, lease_harness=NULL, lease_token=NULL, lease_pid=NULL, "
                "heartbeat_expires_at=NULL, deadline_at=NULL")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def paths_overlap(left: list[str], right: list[str]) -> bool:
    for a in left:
        for b in right:
            a2, b2 = a.rstrip("/"), b.rstrip("/")
            if a2 == b2 or a2.startswith(b2 + "/") or b2.startswith(a2 + "/"):
                return True
    return False


def _end_attempt(conn: sqlite3.Connection, task_id: str, outcome: str, reason: str, now: float) -> None:
    conn.execute("UPDATE attempts SET ended_at=?, outcome=?, reason=? WHERE task_id=? AND ended_at IS NULL",
                 (now, outcome, reason[:500], task_id))


def _fail_status(cfg: Config, attempts: int) -> str:
    return "NEEDS_TRIAGE" if attempts >= cfg.max_attempts else "NEEDS_TAKEOVER"


def _refresh(conn: sqlite3.Connection, cfg: Config, now: float, alive=pid_alive) -> None:
    for row in conn.execute("SELECT * FROM tasks WHERE status IN ('CLAIMED','REVIEWING')").fetchall():
        if (row["heartbeat_expires_at"] or 0) < now:
            why = "heartbeat_lost"
        elif (row["deadline_at"] or 0) < now:
            why = "lease_expired"
        elif not alive(row["lease_pid"]):
            why = "worker_gone"
        else:
            continue
        if row["status"] == "CLAIMED":
            status = _fail_status(cfg, row["attempts"])
            conn.execute(f"UPDATE tasks SET status=?, avoid_harness=?, reason=?, updated_at=?, {_CLEAR_LEASE} WHERE task_id=?",
                         (status, row["lease_harness"], why, now, row["task_id"]))
        else:
            status = "AWAITING_REVIEW"
            conn.execute(f"UPDATE tasks SET status=?, avoid_harness=?, reason=?, updated_at=?, {_CLEAR_LEASE} WHERE task_id=?",
                         (status, row["lease_harness"], f"review_{why}", now, row["task_id"]))
        _end_attempt(conn, row["task_id"], "expired", why, now)
        store.event(conn, "system", "lease_expired", row["task_id"], f"{why} -> {status}", now)
    done = {r[0] for r in conn.execute("SELECT task_id FROM tasks WHERE status='DONE'")}
    for row in conn.execute("SELECT task_id, depends_json FROM tasks WHERE status='BLOCKED'").fetchall():
        if all(dep in done for dep in store.loads(row["depends_json"])):
            conn.execute("UPDATE tasks SET status='READY', updated_at=? WHERE task_id=?", (now, row["task_id"]))
            store.event(conn, "system", "ready", row["task_id"], "", now)


def refresh(conn: sqlite3.Connection, cfg: Config, now: float | None = None, alive=pid_alive) -> None:
    """Expire dead leases and release tasks whose dependencies are all done."""
    with store.write(conn):
        _refresh(conn, cfg, now or time.time(), alive)


def _lease(conn: sqlite3.Connection, cfg: Config, row: sqlite3.Row, status: str, role: str, harness: str,
           worker: str, pid: int, minutes: int, now: float) -> dict[str, object]:
    token = str(uuid.uuid4())
    attempts = row["attempts"] + (1 if role == "author" else 0)
    conn.execute(
        "UPDATE tasks SET status=?, attempts=?, lease_owner=?, lease_harness=?, lease_token=?, lease_pid=?, "
        "claimed_at=?, heartbeat_expires_at=?, deadline_at=?, updated_at=? WHERE task_id=?",
        (status, attempts, worker, harness, token, pid, now, now + cfg.heartbeat_ttl_s, now + minutes * 60.0, now,
         row["task_id"]))
    conn.execute("INSERT INTO attempts (task_id, role, harness, worker, started_at) VALUES (?,?,?,?,?)",
                 (row["task_id"], role, harness, worker, now))
    store.event(conn, worker, f"claimed_{role}", row["task_id"], f"attempt {attempts}", now)
    return {"claimed": True, "role": role, "task_id": row["task_id"], "title": row["title"],
            "branch": row["branch"], "token": token, "lease_min": minutes, "brief_path": row["brief_path"],
            "takeover": row["status"] == "NEEDS_TAKEOVER", "candidate_sha": row["candidate_sha"],
            "attempt": attempts}


def claim(conn: sqlite3.Connection, cfg: Config, harness: str, worker: str, pid: int, *,
          usage_verdict: dict[str, object] | None = None, task_id: str | None = None,
          now: float | None = None, load: float | None = None, alive=pid_alive) -> dict[str, object]:
    """Hand one unit of work to a worker: a review first, then a takeover, then fresh work."""
    now = now or time.time()
    with store.write(conn):
        _refresh(conn, cfg, now, alive)
        if not conn.execute(f"SELECT 1 FROM tasks WHERE status IN ({','.join('?' * len(store.OPEN_STATUSES))}) LIMIT 1",
                            store.OPEN_STATUSES).fetchone():
            return {"claimed": False, "reason": "board_complete"}
        cool = conn.execute("SELECT cooldown_until FROM harness_state WHERE harness=?", (harness,)).fetchone()
        if cool and cool[0] > now:
            return {"claimed": False, "reason": "harness_cooldown", "retry_after_s": int(cool[0] - now)}

        if cfg.review_required and not task_id:
            waiting = conn.execute("SELECT * FROM tasks WHERE status='AWAITING_REVIEW' "
                                   "ORDER BY priority DESC, updated_at ASC").fetchall()
            others_live = any(alive(r["pid"]) for r in conn.execute(
                "SELECT pid FROM workers WHERE harness != ?", (harness,)).fetchall())
            pick = None
            for row in waiting:
                if row["avoid_harness"] == harness and len(waiting) > 1:
                    continue
                independent = row["author_harness"] != harness
                waited = now - (row["updated_at"] or now) >= cfg.review_fallback_s
                if independent or not cfg.cross_harness or not others_live or waited:
                    pick = row
                    break
            if pick is not None:
                return _lease(conn, cfg, pick, "REVIEWING", "review", harness, worker, pid, cfg.review_lease_min, now)

        coding = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='CLAIMED'").fetchone()[0]
        if coding >= cfg.max_coding:
            return {"claimed": False, "reason": "capacity", "coding": coding, "max_coding": cfg.max_coding}
        if cfg.max_load > 0 and (load if load is not None else os.getloadavg()[0]) > cfg.max_load:
            return {"claimed": False, "reason": "machine_load"}
        if usage_verdict and not usage_verdict.get("allow", True) and not task_id:
            return {"claimed": False, **{k: v for k, v in usage_verdict.items() if k != "allow"}}

        if task_id:
            row = conn.execute("SELECT * FROM tasks WHERE task_id=? AND status IN ('READY','NEEDS_TAKEOVER','NEEDS_TRIAGE')",
                               (task_id,)).fetchone()
            if row is None:
                return {"claimed": False, "reason": "task_not_claimable"}
        else:
            candidates = conn.execute("SELECT * FROM tasks WHERE status IN ('READY','NEEDS_TAKEOVER')").fetchall()
            if not candidates:
                return {"claimed": False, "reason": "no_ready_work"}
            flight = {r["task_id"]: store.loads(r["paths_json"]) for r in conn.execute(
                f"SELECT task_id, paths_json FROM tasks WHERE status IN ({','.join('?' * len(store.IN_FLIGHT))})",
                store.IN_FLIGHT).fetchall()}

            def rank(r: sqlite3.Row) -> tuple:
                mine = store.loads(r["paths_json"])
                clash = any(paths_overlap(mine, other) for tid, other in flight.items() if tid != r["task_id"])
                takeover = r["status"] == "NEEDS_TAKEOVER"
                bonus = 10 if r["harness_pref"] == harness else 0
                # A takeover already owns its paths, so only fresh work is steered away from a clash.
                return (r["avoid_harness"] == harness, not takeover, clash and not takeover,
                        -(r["priority"] + bonus), r["updated_at"])

            row = sorted(candidates, key=rank)[0]
        return _lease(conn, cfg, row, "CLAIMED", "author", harness, worker, pid, row["lease_min"], now)


def heartbeat(conn: sqlite3.Connection, cfg: Config, task_id: str, token: str, now: float | None = None) -> bool:
    now = now or time.time()
    with store.write(conn):
        cur = conn.execute("UPDATE tasks SET heartbeat_expires_at=? WHERE task_id=? AND lease_token=? AND deadline_at>?",
                           (now + cfg.heartbeat_ttl_s, task_id, token, now))
        return cur.rowcount == 1


def _held(conn: sqlite3.Connection, task_id: str, token: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE task_id=? AND lease_token=?", (task_id, token)).fetchone()


def finish_author(conn: sqlite3.Connection, cfg: Config, task_id: str, token: str, *, ok: bool,
                  candidate_sha: str | None = None, reason: str = "", quota: bool = False,
                  now: float | None = None) -> str | None:
    """Record the end of an authoring attempt. Returns the new status, or None if the lease was lost."""
    now = now or time.time()
    with store.write(conn):
        row = _held(conn, task_id, token)
        if row is None:
            return None
        harness = row["lease_harness"]
        if quota:
            harness_cfg = cfg.harnesses.get(harness)
            until = now + (harness_cfg.cooldown_min if harness_cfg else 30) * 60.0
            conn.execute("INSERT INTO harness_state (harness, cooldown_until, last_error) VALUES (?,?,'quota') "
                         "ON CONFLICT(harness) DO UPDATE SET cooldown_until=excluded.cooldown_until, last_error='quota'",
                         (harness, until))
            store.event(conn, "system", "harness_cooldown", task_id, f"{harness} until {int(until)}", now)
        if ok:
            status = "AWAITING_REVIEW" if cfg.review_required else "LANDING"
            conn.execute(f"UPDATE tasks SET status=?, candidate_sha=?, author_harness=?, avoid_harness=NULL, reason=NULL, "
                         f"updated_at=?, {_CLEAR_LEASE} WHERE task_id=?", (status, candidate_sha, harness, now, task_id))
            _end_attempt(conn, task_id, "ready", "", now)
        else:
            # A quota stop is the subscription's fault, not the slice's, so it does not count as an attempt.
            attempts = row["attempts"] - (1 if quota else 0)
            status = _fail_status(cfg, attempts)
            conn.execute(f"UPDATE tasks SET status=?, attempts=?, avoid_harness=?, reason=?, updated_at=?, {_CLEAR_LEASE} "
                         "WHERE task_id=?", (status, attempts, harness, reason[:500], now, task_id))
            _end_attempt(conn, task_id, "quota" if quota else "incomplete", reason, now)
        store.event(conn, row["lease_owner"], "author_finished", task_id, f"{status} {reason}".strip(), now)
        return status


def finish_review(conn: sqlite3.Connection, cfg: Config, task_id: str, token: str, verdict: str,
                  notes: str = "", now: float | None = None) -> str | None:
    """verdict is `pass`, `fail`, or `incomplete` (the reviewer produced no verdict)."""
    now = now or time.time()
    with store.write(conn):
        row = _held(conn, task_id, token)
        if row is None:
            return None
        if verdict == "pass":
            status, avoid, reason = "LANDING", None, None
        elif verdict == "fail":
            status, avoid, reason = _fail_status(cfg, row["attempts"]), None, "review_rejected"
        else:
            last_author = conn.execute("SELECT COALESCE(MAX(id), 0) FROM attempts WHERE task_id=? AND role='author'",
                                       (task_id,)).fetchone()[0]
            stalls = conn.execute("SELECT COUNT(*) FROM attempts WHERE task_id=? AND role='review' AND outcome='incomplete' "
                                  "AND id>?", (task_id, last_author)).fetchone()[0]
            if stalls >= 2:  # a third reviewer in a row produced no verdict: stop looping and hand it back
                status, avoid, reason = _fail_status(cfg, row["attempts"]), None, "review_never_completed"
            else:
                status, avoid, reason = "AWAITING_REVIEW", row["lease_harness"], "review_incomplete"
        conn.execute(f"UPDATE tasks SET status=?, avoid_harness=?, reason=?, updated_at=?, {_CLEAR_LEASE} WHERE task_id=?",
                     (status, avoid, reason, now, task_id))
        _end_attempt(conn, task_id, verdict, notes, now)
        store.event(conn, row["lease_owner"], f"review_{verdict}", task_id, notes[:300], now)
        return status


def landed(conn: sqlite3.Connection, cfg: Config, task_id: str, sha: str, actor: str = "system",
           now: float | None = None) -> None:
    now = now or time.time()
    with store.write(conn):
        conn.execute(f"UPDATE tasks SET status='DONE', landed_sha=?, landed_at=?, reason=NULL, updated_at=?, {_CLEAR_LEASE} "
                     "WHERE task_id=?", (sha, now, now, task_id))
        store.event(conn, actor, "landed", task_id, sha, now)
        _refresh(conn, cfg, now)


def take_landing(conn: sqlite3.Connection, task_id: str, worker: str, pid: int, alive=pid_alive) -> bool:
    """Reserve a LANDING task so only one process rebases its worktree at a time."""
    with store.write(conn):
        row = conn.execute("SELECT lease_pid FROM tasks WHERE task_id=? AND status='LANDING'", (task_id,)).fetchone()
        if row is None or (row["lease_pid"] and row["lease_pid"] != pid and alive(row["lease_pid"])):
            return False
        conn.execute("UPDATE tasks SET lease_owner=?, lease_pid=? WHERE task_id=?", (worker, pid, task_id))
        return True


def land_failed(conn: sqlite3.Connection, cfg: Config, task_id: str, reason: str, now: float | None = None) -> str:
    now = now or time.time()
    with store.write(conn):
        row = conn.execute("SELECT attempts FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        status = _fail_status(cfg, row["attempts"] if row else 0)
        conn.execute(f"UPDATE tasks SET status=?, avoid_harness=NULL, reason=?, updated_at=?, {_CLEAR_LEASE} "
                     "WHERE task_id=? AND status='LANDING'", (status, reason[:500], now, task_id))
        store.event(conn, "system", "land_failed", task_id, reason[:500], now)
        return status


def requeue(conn: sqlite3.Connection, task_id: str, actor: str = "human", now: float | None = None) -> str:
    """Give a parked or cancelled task a fresh set of attempts."""
    now = now or time.time()
    with store.write(conn):
        row = conn.execute("SELECT status, attempts, candidate_sha FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        if row["status"] in ("DONE", "CLAIMED", "REVIEWING", "LANDING"):
            raise ValueError(f"{task_id} is {row['status']} and cannot be requeued")
        status = "NEEDS_TAKEOVER" if row["attempts"] else "BLOCKED"
        conn.execute("UPDATE tasks SET status=?, attempts=0, avoid_harness=NULL, reason='requeued', updated_at=? WHERE task_id=?",
                     (status, now, task_id))
        store.event(conn, actor, "requeued", task_id, status, now)
        return status


def cancel(conn: sqlite3.Connection, task_id: str, actor: str = "human", now: float | None = None) -> None:
    """Stop a task. A worker holding it loses its lease at the next heartbeat and stops its agent."""
    now = now or time.time()
    with store.write(conn):
        cur = conn.execute(f"UPDATE tasks SET status='CANCELLED', reason='cancelled', updated_at=?, {_CLEAR_LEASE} "
                           "WHERE task_id=? AND status!='DONE'", (now, task_id))
        if cur.rowcount != 1:
            raise ValueError(f"{task_id} is done or unknown")
        _end_attempt(conn, task_id, "cancelled", "", now)
        store.event(conn, actor, "cancelled", task_id, "", now)


def set_priority(conn: sqlite3.Connection, task_id: str, priority: int, actor: str = "human",
                 now: float | None = None) -> None:
    with store.write(conn):
        cur = conn.execute("UPDATE tasks SET priority=?, updated_at=? WHERE task_id=?", (priority, now or time.time(), task_id))
        if cur.rowcount != 1:
            raise KeyError(task_id)
        store.event(conn, actor, "priority", task_id, str(priority), now)
