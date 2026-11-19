"""Audits: review what landed, turn every defect into tracked work, and prove each fix.

An audit examines one built state of main. Its work is split into audit jobs that any harness
claims under a lease, exactly like tasks but without a branch: an audit job reads the audit
worktree (a detached checkout of the audited commit) and writes only evidence and findings.

    slice      one per task that landed since the last audit: does it still meet its brief?
    checklist  one per file in the checklists folder: a journey, a performance or UX pass, anything
    verify     one per task with fixed findings: is each fix real in this build?
    triage     after the others: group findings into root causes, one fix task per cause

A finding gets a permanent ID (F-0001) inside one transaction and names the task that owns the
defect. Triage creates one ordinary fix task per root cause (RC-0001), so the fleet authors,
reviews and lands the fix like any other slice. A fix closes a finding only when a landed commit
carries the trailer `Fixes: F-0001` and touches a regression test path; only a later audit's
verify job marks it verified. Every job state change happens here, inside one transaction, and
leaves an event.
"""
from __future__ import annotations

from datetime import datetime
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid

from . import gitops, queue, store, tasks
from .config import Config

SEVERITY_PRIORITY = {"P0": 1100, "P1": 1000, "P2": 300}
SEVERITIES = tuple(SEVERITY_PRIORITY)
OPEN = ("open", "reopened")
LIVE = ("open", "reopened", "fix-scheduled", "fixed", "wont-fix-proposed")
KIND_ORDER = {"verify": 0, "slice": 1, "checklist": 2, "triage": 3}
_CLEAR = ("lease_owner=NULL, lease_harness=NULL, lease_token=NULL, lease_pid=NULL, "
          "heartbeat_expires_at=NULL, deadline_at=NULL")


class AuditError(Exception):
    """A refused audit operation. Nothing was changed."""


# ------------------------------------------------------------------ helpers

def _now() -> float:
    return time.time()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit_dir(cfg: Config, audit_id: str) -> Path:
    path = cfg.state / "audits" / audit_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def result_path(evidence: Path) -> Path:
    return evidence / "result.json"


def cli_prefix(cfg: Config) -> str:
    """The exact command an agent runs to talk to this board, independent of its working directory."""
    return f"{sys.executable} -m workboard --root {cfg.root}"


def regression_path(cfg: Config, path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    for pattern in cfg.audit_regression_paths:
        if fnmatch.fnmatch(path, pattern):
            return True
        if pattern.startswith("**/") and (fnmatch.fnmatch(path, pattern[3:]) or fnmatch.fnmatch(name, pattern[3:])):
            return True
    return False


def _note(conn: sqlite3.Connection, finding_id: str, kind: str, actor: str, *, audit_id: str | None = None,
          job_id: str | None = None, note: str = "", evidence: list[dict[str, str]] | None = None,
          commit_sha: str | None = None, now: float | None = None) -> None:
    conn.execute("INSERT INTO finding_notes (finding_id, audit_id, job_id, actor, kind, note, evidence_json, commit_sha, at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (finding_id, audit_id, job_id, actor, kind, note[:2000],
                                                json.dumps(evidence or []), commit_sha, now or _now()))


def _evidence(paths: list[str]) -> list[dict[str, str]]:
    records = []
    for item in paths:
        path = Path(item)
        if not path.is_file():
            raise AuditError(f"evidence file {item} does not exist")
        records.append({"path": str(path.resolve()), "sha256": _sha256(path)})
    return records


# ------------------------------------------------------------------ checklists

def parse_checklist(path: Path) -> dict[str, object]:
    """A checklist is a Markdown file like a task: a header of facts, then what to check."""
    text = path.read_text(encoding="utf-8")
    fields: dict[str, object] = {}
    body = text
    if text.startswith("---\n"):
        head, sep, rest = text[4:].partition("\n---\n")
        if not sep:
            raise AuditError(f"{path}: header is not closed with `---`")
        body = rest
        for line in head.splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                key, colon, raw = line.partition(":")
                if not colon:
                    raise AuditError(f"{path}: cannot read header line {line!r}")
                fields[key.strip()] = tasks._value(raw)
    check_id = str(fields.get("id") or path.stem)
    if not tasks._ID.fullmatch(check_id):
        raise AuditError(f"{path}: id {check_id!r} must be lowercase letters, digits, dot, dash or underscore")
    if not body.strip():
        raise AuditError(f"{path}: the checklist is empty")
    return {"id": check_id, "title": str(fields.get("title") or check_id), "body": body.strip(),
            "resource": str(fields.get("resource") or "") or None, "lease_min": int(str(fields.get("lease_min") or 0)),
            "quick": str(fields.get("quick", "false")).lower() == "true"}


def checklists(cfg: Config) -> list[dict[str, object]]:
    folder = cfg.audit_checklists
    return [parse_checklist(p) for p in sorted(folder.glob("*.md"))] if folder.is_dir() else []


# ------------------------------------------------------------------ briefs

def _slice_brief(conn: sqlite3.Connection, task_id: str, sha: str) -> str:
    row = conn.execute("SELECT title, brief_path, landed_sha FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    from .prompt import brief_body
    try:
        body = brief_body(Path(row["brief_path"]))
    except OSError:
        body = "(the brief file is no longer readable; judge the slice from its commits)"
    return (f"## Audit of the landed slice `{task_id}`: {row['title']}\n\n"
            f"It landed at `{row['landed_sha']}`; this audit examines `{sha}`. Check that every outcome and "
            "acceptance item in its brief holds in this build, through the same entry points a user or caller "
            "would use, that the test it names exists and passes, and that nothing next to it regressed. "
            "A promise in the brief that this build does not keep is a finding.\n\n"
            f"### The slice's brief\n\n{body}\n")


def _verify_brief(findings: list[sqlite3.Row]) -> str:
    rows = "\n".join(f"- `{f['finding_id']}` ({f['severity']}) {f['title']}\n  - Reproduce: {f['repro']}\n"
                     f"  - Expected: {f['expected']}\n  - Fixed in: `{f['fixed_sha']}`" for f in findings)
    return ("## Verify fixes\n\nReproduce each finding below in this build and record exactly one verdict per finding "
            "with the `verify` command in the assignment. `reopened` means the defect is still there.\n\n" + rows + "\n")


TRIAGE_BRIEF = """## Triage the findings of this audit

List the findings that still need a decision with the `findings --untriaged` command in the assignment.
Resolve every one of them:

- Group findings that share one cause into a root cause. Each root cause gets exactly one fix task.
- Attach a finding to an existing root cause when it is a new symptom of a known cause, or a reopened one.
- Mark exact duplicates, and propose `wont-fix` (with a reason) for anything a person should decide.

Severity is the worst among the grouped findings unless you have a reason to change it. Never create a
second fix for a cause whose fix is still in flight.
"""


# ------------------------------------------------------------------ opening an audit

def _insert_job(conn: sqlite3.Connection, cfg: Config, audit_id: str, kind: str, subject: str, title: str,
                brief: str, *, resource: str | None = None, avoid: str | None = None, status: str = "READY",
                lease_min: int = 0, now: float) -> str:
    job_id = f"{audit_id}-{kind}-{subject}"
    evidence = audit_dir(cfg, audit_id) / f"{kind}-{subject}"
    evidence.mkdir(parents=True, exist_ok=True)
    brief_path = evidence / "brief.md"
    brief_path.write_text(brief, encoding="utf-8")
    conn.execute(
        "INSERT INTO audit_jobs (job_id, audit_id, kind, subject, title, brief_path, evidence_dir, resource, avoid_harness, "
        "status, lease_min, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, audit_id, kind, subject, title, str(brief_path), str(evidence), resource, avoid, status,
         lease_min or cfg.audit_lease_min, now))
    store.event(conn, "system", "audit_job_added", subject if kind == "slice" else None, job_id, now)
    return job_id


def open_audit(cfg: Config, conn: sqlite3.Connection, *, sha: str | None = None, build_id: str | None = None,
               quick: bool = False, now: float | None = None) -> dict[str, object]:
    """Open an audit of one commit on main. Idempotent per commit."""
    from . import builds
    now = now or _now()
    gitops.fetch(cfg)
    if not sha:
        build = builds.find(cfg, build_id)
        sha, build_id = str(build["sha"]), str(build["id"])
    sha = gitops.out(cfg.repo, "rev-parse", "--verify", f"{sha}^{{commit}}")
    existing = conn.execute("SELECT audit_id FROM audits WHERE sha=?", (sha,)).fetchone()
    if existing:
        return {"opened": False, "audit_id": existing[0], "reason": "this commit already has an audit"}
    if gitops.git(cfg.repo, "merge-base", "--is-ancestor", sha, cfg.remote_main).returncode != 0:
        raise AuditError(f"{sha[:9]} is not on {cfg.remote_main}; audit only what has landed")
    reconcile(cfg, conn)
    audit_id = f"{datetime.fromtimestamp(now).strftime('%Y%m%d')}-{sha[:9]}"
    worktree = cfg.worktree(f"_audit-{audit_id}")
    if not (worktree / ".git").exists():
        worktree.parent.mkdir(parents=True, exist_ok=True)
        gitops.out(cfg.repo, "worktree", "add", "--detach", str(worktree), sha)
    folder = audit_dir(cfg, audit_id)
    if cfg.audit_prepare:
        log = folder / "prepare.log"
        with open(log, "ab") as fh:
            done = subprocess.run(cfg.audit_prepare, shell=True, cwd=str(worktree), stdout=fh, stderr=subprocess.STDOUT,
                                  env={**os.environ, "WORKBOARD_AUDIT_SHA": sha}, check=False)
        if done.returncode != 0:
            raise AuditError(f"[audit] prepare failed with status {done.returncode}; see {log}")
    refs: list[str] = []
    if cfg.audit_refs_command:
        done = subprocess.run(cfg.audit_refs_command, shell=True, cwd=str(worktree), capture_output=True, text=True,
                              env={**os.environ, "WORKBOARD_AUDIT_SHA": sha}, check=False)
        if done.returncode != 0:
            raise AuditError(f"[audit] refs_command failed: {done.stderr.strip()[:300]}")
        refs = sorted({line.strip() for line in done.stdout.splitlines() if line.strip()})
    (folder / "refs.json").write_text(json.dumps(refs), encoding="utf-8")
    lists = [c for c in checklists(cfg) if c["quick"] or not quick]

    with store.write(conn):
        audited = {r[0] for r in conn.execute("SELECT DISTINCT subject FROM audit_jobs WHERE kind='slice'")}
        landed = conn.execute("SELECT task_id, landed_sha, author_harness FROM tasks WHERE status='DONE' AND landed_sha IS NOT NULL").fetchall()
        new_slices = [r for r in landed if r["task_id"] not in audited and not r["task_id"].startswith("fix-rc-")
                      and gitops.git(cfg.repo, "merge-base", "--is-ancestor", r["landed_sha"], sha).returncode == 0]
        fixed = [f for f in conn.execute("SELECT * FROM findings WHERE state='fixed' ORDER BY seq").fetchall()
                 if gitops.git(cfg.repo, "merge-base", "--is-ancestor", f["fixed_sha"], sha).returncode == 0]
        scope = {"slices": [] if quick or not cfg.audit_per_slice else [r["task_id"] for r in new_slices],
                 "verify": [f["finding_id"] for f in fixed], "checklists": [c["id"] for c in lists], "quick": quick}
        conn.execute("INSERT INTO audits (audit_id, sha, build_id, quick, worktree, scope_json, status, opened_at) "
                     "VALUES (?,?,?,?,?,?,'OPEN',?)", (audit_id, sha, build_id, int(quick), str(worktree), json.dumps(scope), now))
        by_task: dict[str, list[sqlite3.Row]] = {}
        for f in fixed:
            by_task.setdefault(f["task_id"], []).append(f)
        for task_id, items in sorted(by_task.items()):
            _insert_job(conn, cfg, audit_id, "verify", task_id, f"Verify {len(items)} fix(es) on {task_id}",
                        _verify_brief(items), now=now)
        if not quick and cfg.audit_per_slice:
            for row in new_slices:
                title = conn.execute("SELECT title FROM tasks WHERE task_id=?", (row["task_id"],)).fetchone()[0]
                _insert_job(conn, cfg, audit_id, "slice", row["task_id"], f"Audit slice: {title}",
                            _slice_brief(conn, row["task_id"], sha), avoid=row["author_harness"], now=now)
        for check in lists:
            _insert_job(conn, cfg, audit_id, "checklist", str(check["id"]), str(check["title"]),
                        f"## Checklist: {check['title']}\n\n{check['body']}\n", resource=check["resource"],  # type: ignore[arg-type]
                        lease_min=int(check["lease_min"]), now=now)  # type: ignore[arg-type]
        _insert_job(conn, cfg, audit_id, "triage", "findings", "Triage the findings", TRIAGE_BRIEF, status="BLOCKED", now=now)
        store.event(conn, "system", "audit_opened", None, f"{audit_id} at {sha[:9]}", now)
        counts = {r[0]: r[1] for r in conn.execute("SELECT kind, COUNT(*) FROM audit_jobs WHERE audit_id=? GROUP BY kind", (audit_id,))}
    return {"opened": True, "audit_id": audit_id, "sha": sha, "build": build_id, "worktree": str(worktree),
            "scope": scope, "jobs": counts}


def resolve_audit(conn: sqlite3.Connection, audit_id: str | None) -> sqlite3.Row:
    row = (conn.execute("SELECT * FROM audits WHERE audit_id=?", (audit_id,)).fetchone() if audit_id else
           conn.execute("SELECT * FROM audits ORDER BY opened_at DESC LIMIT 1").fetchone())
    if row is None:
        raise AuditError(f"no audit {audit_id}" if audit_id else "no audit has been opened; run `workboard audit open`")
    return row


# ------------------------------------------------------------------ leases

def untriaged(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM findings WHERE state='reopened' OR (state='open' AND root_cause IS NULL) "
                        "ORDER BY seq").fetchall()


def _refresh(conn: sqlite3.Connection, cfg: Config, now: float, alive=queue.pid_alive) -> None:
    for row in conn.execute("SELECT * FROM audit_jobs WHERE status='CLAIMED'").fetchall():
        if (row["heartbeat_expires_at"] or 0) < now:
            why = "heartbeat_lost"
        elif (row["deadline_at"] or 0) < now:
            why = "lease_expired"
        elif not alive(row["lease_pid"]):
            why = "worker_gone"
        else:
            continue
        status = "NEEDS_TRIAGE" if row["attempts"] >= cfg.max_attempts else "READY"
        conn.execute(f"UPDATE audit_jobs SET status=?, avoid_harness=?, reason=?, updated_at=?, {_CLEAR} WHERE job_id=?",
                     (status, row["lease_harness"], why, now, row["job_id"]))
        store.event(conn, "system", "audit_job_expired", None, f"{row['job_id']} {why} -> {status}", now)
    for audit in conn.execute("SELECT DISTINCT audit_id FROM audit_jobs WHERE kind='triage' AND status='BLOCKED'").fetchall():
        pending = conn.execute("SELECT COUNT(*) FROM audit_jobs WHERE audit_id=? AND kind!='triage' "
                               "AND status NOT IN ('DONE','NEEDS_TRIAGE')", (audit[0],)).fetchone()[0]
        if pending:
            continue
        if untriaged(conn):
            conn.execute("UPDATE audit_jobs SET status='READY', updated_at=? WHERE audit_id=? AND kind='triage'", (now, audit[0]))
        else:
            conn.execute("UPDATE audit_jobs SET status='DONE', finished_by='system', finished_at=?, reason='nothing to triage', "
                         "updated_at=? WHERE audit_id=? AND kind='triage'", (now, now, audit[0]))
        store.event(conn, "system", "audit_triage_open", None, audit[0], now)


def claim(conn: sqlite3.Connection, cfg: Config, harness: str, worker: str, pid: int, *,
          usage_verdict: dict[str, object] | None = None, job_id: str | None = None,
          now: float | None = None, alive=queue.pid_alive) -> dict[str, object]:
    """Hand one audit job to a worker. Defers to urgent fix tasks so a P0 fix is never queued behind audits."""
    now = now or _now()
    with store.write(conn):
        _refresh(conn, cfg, now, alive)
        cool = conn.execute("SELECT cooldown_until FROM harness_state WHERE harness=?", (harness,)).fetchone()
        if cool and cool[0] > now:
            return {"claimed": False, "reason": "harness_cooldown"}
        if usage_verdict and not usage_verdict.get("allow", True) and not job_id:
            return {"claimed": False, "reason": "usage"}
        if not job_id:
            urgent = conn.execute("SELECT task_id FROM tasks WHERE status IN ('READY','NEEDS_TAKEOVER') AND priority>=? LIMIT 1",
                                  (SEVERITY_PRIORITY["P0"],)).fetchone()
            if urgent:
                return {"claimed": False, "reason": "urgent_fix_first", "task_id": urgent[0]}
        held = {r[0] for r in conn.execute("SELECT resource FROM audit_jobs WHERE status='CLAIMED' AND resource IS NOT NULL")}
        rows = (conn.execute("SELECT * FROM audit_jobs WHERE job_id=? AND status='READY'", (job_id,)).fetchall() if job_id else
                conn.execute("SELECT * FROM audit_jobs WHERE status='READY'").fetchall())
        rows = [r for r in rows if not r["resource"] or r["resource"] not in held]
        if not rows:
            return {"claimed": False, "reason": "no_audit_work"}
        row = min(rows, key=lambda r: (r["avoid_harness"] == harness, KIND_ORDER.get(r["kind"], 9), r["audit_id"], r["job_id"]))
        token = str(uuid.uuid4())
        conn.execute("UPDATE audit_jobs SET status='CLAIMED', attempts=attempts+1, lease_owner=?, lease_harness=?, lease_token=?, "
                     "lease_pid=?, claimed_at=?, heartbeat_expires_at=?, deadline_at=?, updated_at=? WHERE job_id=?",
                     (worker, harness, token, pid, now, now + cfg.heartbeat_ttl_s, now + int(row["lease_min"]) * 60.0, now,
                      row["job_id"]))
        store.event(conn, worker, "audit_job_claimed", row["subject"] if row["kind"] == "slice" else None, row["job_id"], now)
        audit = conn.execute("SELECT * FROM audits WHERE audit_id=?", (row["audit_id"],)).fetchone()
    return {"claimed": True, "role": "audit", "job_id": row["job_id"], "kind": row["kind"], "title": row["title"],
            "token": token, "lease_min": int(row["lease_min"]), "brief_path": row["brief_path"],
            "evidence": row["evidence_dir"], "worktree": audit["worktree"], "sha": audit["sha"],
            "audit_id": audit["audit_id"], "resource": row["resource"]}


def heartbeat(conn: sqlite3.Connection, cfg: Config, job_id: str, token: str, now: float | None = None) -> bool:
    now = now or _now()
    with store.write(conn):
        cur = conn.execute("UPDATE audit_jobs SET heartbeat_expires_at=? WHERE job_id=? AND lease_token=? AND deadline_at>?",
                           (now + cfg.heartbeat_ttl_s, job_id, token, now))
        return cur.rowcount == 1


def _job(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM audit_jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        raise AuditError(f"unknown audit job {job_id}")
    return row


def finish(conn: sqlite3.Connection, cfg: Config, job_id: str, token: str, *, ok: bool, reason: str = "",
           quota: bool = False, now: float | None = None) -> str | None:
    """Record the end of an audit job. Returns the new status, or None if the lease was lost."""
    now = now or _now()
    with store.write(conn):
        row = conn.execute("SELECT * FROM audit_jobs WHERE job_id=? AND lease_token=?", (job_id, token)).fetchone()
        if row is None:
            return None
        harness = row["lease_harness"]
        if quota:
            queue.cool_down(conn, cfg, harness, None, now)
        if ok and row["kind"] == "verify":
            audit = conn.execute("SELECT scope_json FROM audits WHERE audit_id=?", (row["audit_id"],)).fetchone()
            scoped = set(json.loads(audit[0])["verify"])
            missing = [f[0] for f in conn.execute("SELECT finding_id FROM findings WHERE task_id=? AND state='fixed'",
                                                  (row["subject"],)) if f[0] in scoped]
            if missing:
                ok, reason = False, "no verdict recorded for " + ", ".join(missing)
        if ok and row["kind"] == "triage":
            left = untriaged(conn)
            if left:
                ok, reason = False, "still untriaged: " + ", ".join(f["finding_id"] for f in left)
        if ok:
            status = "DONE"
            conn.execute(f"UPDATE audit_jobs SET status='DONE', finished_by=?, finished_at=?, reason=NULL, updated_at=?, {_CLEAR} "
                         "WHERE job_id=?", (harness, now, now, job_id))
        else:
            attempts = row["attempts"] - (1 if quota else 0)
            status = "NEEDS_TRIAGE" if attempts >= cfg.max_attempts else "READY"
            conn.execute(f"UPDATE audit_jobs SET status=?, attempts=?, avoid_harness=?, reason=?, updated_at=?, {_CLEAR} "
                         "WHERE job_id=?", (status, attempts, harness, reason[:500], now, job_id))
        store.event(conn, row["lease_owner"] or "system", "audit_job_finished",
                    row["subject"] if row["kind"] == "slice" else None, f"{job_id} {status} {reason}".strip(), now)
        _refresh(conn, cfg, now)
        return status


def requeue_job(conn: sqlite3.Connection, job_id: str, actor: str = "human", now: float | None = None) -> None:
    now = now or _now()
    with store.write(conn):
        cur = conn.execute(f"UPDATE audit_jobs SET status='READY', attempts=0, avoid_harness=NULL, reason='requeued', "
                           f"updated_at=?, {_CLEAR} WHERE job_id=? AND status IN ('NEEDS_TRIAGE','DONE')", (now, job_id))
        if cur.rowcount != 1:
            raise AuditError(f"{job_id} is not parked or done")
        store.event(conn, actor, "audit_job_requeued", None, job_id, now)


# ------------------------------------------------------------------ findings

def _actor_and_audit(conn: sqlite3.Connection, job_id: str | None, audit_id: str | None,
                     kinds: tuple[str, ...] | None = None) -> tuple[str, sqlite3.Row, str | None]:
    """A job files as its lease holder; without a job, a person acts on an audit."""
    if job_id:
        job = _job(conn, job_id)
        if job["status"] != "CLAIMED":
            raise AuditError(f"{job_id} is not claimed; only the agent holding it can act for it")
        if kinds and job["kind"] not in kinds:
            raise AuditError(f"a {job['kind']} job cannot do this; it needs a {' or '.join(kinds)} job")
        return str(job["lease_owner"]), resolve_audit(conn, job["audit_id"]), job_id
    return "human", resolve_audit(conn, audit_id), None


def fingerprint(task_id: str, title: str, step: str | None) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return hashlib.sha1(f"{task_id}|{norm}|{step or ''}".encode()).hexdigest()


def file_finding(cfg: Config, conn: sqlite3.Connection, *, task_id: str, severity: str, title: str, repro: str,
                 expected: str, job_id: str | None = None, audit_id: str | None = None, refs: list[str] | None = None,
                 step: str | None = None, reference: str | None = None, evidence: list[str] | None = None,
                 now: float | None = None) -> dict[str, object]:
    """Record a defect. The same defect filed again becomes another sighting of the first finding."""
    if severity not in SEVERITIES:
        raise AuditError(f"severity must be one of {', '.join(SEVERITIES)}")
    for name, value in (("title", title), ("repro", repro), ("expected", expected)):
        if not value.strip():
            raise AuditError(f"--{name} is required")
    actor, audit, job = _actor_and_audit(conn, job_id, audit_id)
    if not conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (task_id,)).fetchone():
        raise AuditError(f"unknown task {task_id}; a finding names the task that owns the defect")
    refs = refs or []
    unknown: list[str] = []
    if cfg.audit_refs_command:
        refs_file = audit_dir(cfg, audit["audit_id"]) / "refs.json"
        known = set(json.loads(refs_file.read_text(encoding="utf-8"))) if refs_file.is_file() else set()
        unknown = [r for r in refs if r not in known]
    if unknown:
        raise AuditError(f"unknown reference(s) in this build: {', '.join(unknown)}")
    records = _evidence(evidence or [])
    now = now or _now()
    key = fingerprint(task_id, title, step)
    with store.write(conn):
        match = conn.execute(f"SELECT finding_id FROM findings WHERE fingerprint=? AND state IN ({','.join('?' * len(LIVE))}) "
                             "ORDER BY seq LIMIT 1", (key, *LIVE)).fetchone()
        if match:
            finding_id, created = match[0], False
        else:
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM findings").fetchone()[0]
            finding_id, created = f"F-{seq:04d}", True
            conn.execute("INSERT INTO findings (finding_id, seq, fingerprint, audit_id, job_id, task_id, refs_json, step, severity, "
                         "title, repro, expected, reference, state, created_at, updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?)",
                         (finding_id, seq, key, audit["audit_id"], job, task_id, json.dumps(refs), step, severity,
                          title.strip(), repro.strip(), expected.strip(), reference, now, now))
            store.event(conn, actor, "finding_filed", task_id, f"{finding_id} {severity} {title.strip()}", now)
        _note(conn, finding_id, "sighting", actor, audit_id=audit["audit_id"], job_id=job,
              note="" if created else f"{severity}: {title}", evidence=records, now=now)
    return {"finding_id": finding_id, "created": created}


def verify(conn: sqlite3.Connection, finding_id: str, result: str, *, job_id: str | None = None,
           audit_id: str | None = None, note: str = "", evidence: list[str] | None = None,
           now: float | None = None) -> dict[str, object]:
    if result not in ("verified", "reopened"):
        raise AuditError("--result must be verified or reopened")
    actor, audit, job = _actor_and_audit(conn, job_id, audit_id, ("verify",))
    records = _evidence(evidence or [])
    now = now or _now()
    with store.write(conn):
        row = conn.execute("SELECT * FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
        if row is None or row["state"] != "fixed":
            raise AuditError(f"{finding_id} is not waiting for verification")
        if result == "verified":
            conn.execute("UPDATE findings SET state='verified', verified_audit=?, updated_at=? WHERE finding_id=?",
                         (audit["audit_id"], now, finding_id))
        else:
            conn.execute("UPDATE findings SET state='reopened', updated_at=? WHERE finding_id=?", (now, finding_id))
        _note(conn, finding_id, result, actor, audit_id=audit["audit_id"], job_id=job, note=note, evidence=records, now=now)
        store.event(conn, actor, f"finding_{result}", row["task_id"], f"{finding_id} {note}"[:300], now)
    return {"finding_id": finding_id, "state": result}


def _queue_fix(conn: sqlite3.Connection, cfg: Config, rc: sqlite3.Row, actor: str, now: float) -> str:
    generation = int(rc["generation"])
    task_id = f"fix-{rc['rc_id'].lower()}" + (f"-r{generation}" if generation > 1 else "")
    findings = conn.execute("SELECT * FROM findings WHERE root_cause=? AND state IN ('open','reopened') ORDER BY seq",
                            (rc["rc_id"],)).fetchall()
    owner = conn.execute("SELECT title, paths_json FROM tasks WHERE task_id=?", (rc["task_id"],)).fetchone()
    ids = ", ".join(f["finding_id"] for f in findings)
    lines = "\n".join(f"- `{f['finding_id']}` ({f['severity']}) {f['title']}\n  - Reproduce: {f['repro']}\n"
                      f"  - Expected: {f['expected']}" + (f"\n  - Reference: {f['reference']}" if f["reference"] else "")
                      for f in findings)
    brief = (f"## Outcome\n\nFix root cause {rc['rc_id']}: {rc['title']}. It was found by an audit of `{rc['task_id']}` "
             f"({owner['title'] if owner else 'unknown task'}).\n\n## Findings this fix closes\n\n{lines}\n\n## Acceptance\n\n"
             "- Reproduce every finding above, then fix every code path that contradicts the expected behaviour.\n"
             "- Add a deterministic regression test that fails before the fix and passes after it, in a path the project "
             f"treats as a test ({', '.join(cfg.audit_regression_paths) or 'any'}).\n"
             f"- End the commit message that adds the fix and its test with the trailer line `{cfg.audit_trailer}: {ids}`. "
             "Workboard reads it after landing; a trailer on a commit without a regression test leaves the findings open.\n")
    return tasks.insert(conn, cfg, task_id, f"Fix {rc['rc_id']}: {rc['title']}", brief,
                                  priority=SEVERITY_PRIORITY[rc["severity"]],
                                  paths=store.loads(owner["paths_json"]) if owner else [], actor=actor, now=now)


def triage_group(cfg: Config, conn: sqlite3.Connection, finding_ids: list[str], *, title: str | None = None,
                 severity: str | None = None, task_id: str | None = None, root_cause: str | None = None,
                 job_id: str | None = None, audit_id: str | None = None, now: float | None = None) -> dict[str, object]:
    """Group findings under one root cause. A cause has at most one fix in flight."""
    actor, audit, job = _actor_and_audit(conn, job_id, audit_id, ("triage",))
    now = now or _now()
    with store.write(conn):
        rows = [conn.execute("SELECT * FROM findings WHERE finding_id=?", (f,)).fetchone() for f in finding_ids]
        if not rows or any(r is None for r in rows):
            raise AuditError("unknown finding in the group")
        if any(r["state"] not in OPEN for r in rows):
            raise AuditError("only open or reopened findings can be grouped")
        if root_cause:
            rc = conn.execute("SELECT * FROM root_causes WHERE rc_id=?", (root_cause,)).fetchone()
            if rc is None:
                raise AuditError(f"unknown root cause {root_cause}")
        else:
            if not title:
                raise AuditError("--title is required for a new root cause")
            sev = severity or min((r["severity"] for r in rows), key=SEVERITIES.index)
            if sev not in SEVERITIES:
                raise AuditError(f"severity must be one of {', '.join(SEVERITIES)}")
            owner = task_id or rows[0]["task_id"]
            if not conn.execute("SELECT 1 FROM tasks WHERE task_id=?", (owner,)).fetchone():
                raise AuditError(f"unknown task {owner}")
            seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM root_causes").fetchone()[0]
            conn.execute("INSERT INTO root_causes (rc_id, seq, audit_id, title, severity, task_id, created_at) VALUES (?,?,?,?,?,?,?)",
                         (f"RC-{seq:04d}", seq, audit["audit_id"], title, sev, owner, now))
            rc = conn.execute("SELECT * FROM root_causes WHERE seq=?", (seq,)).fetchone()
        for r in rows:
            conn.execute("UPDATE findings SET root_cause=?, updated_at=? WHERE finding_id=?", (rc["rc_id"], now, r["finding_id"]))
            _note(conn, r["finding_id"], "triaged", actor, audit_id=audit["audit_id"], job_id=job, note=rc["rc_id"], now=now)
        active = conn.execute("SELECT status FROM tasks WHERE task_id=?", (rc["fix_task"],)).fetchone() if rc["fix_task"] else None
        if active and active[0] not in ("DONE", "CANCELLED"):
            fix, queued = rc["fix_task"], False
        else:
            if rc["fix_task"]:
                conn.execute("UPDATE root_causes SET generation=generation+1 WHERE rc_id=?", (rc["rc_id"],))
                rc = conn.execute("SELECT * FROM root_causes WHERE rc_id=?", (rc["rc_id"],)).fetchone()
            fix, queued = _queue_fix(conn, cfg, rc, actor, now), True
            conn.execute("UPDATE root_causes SET fix_task=? WHERE rc_id=?", (fix, rc["rc_id"]))
        conn.execute("UPDATE findings SET state='fix-scheduled', fix_task=?, updated_at=? WHERE root_cause=? AND state IN ('open','reopened')",
                     (fix, now, rc["rc_id"]))
        store.event(conn, actor, "root_cause", rc["task_id"], f"{rc['rc_id']} {', '.join(finding_ids)} -> {fix}", now)
        queue._refresh(conn, cfg, now)
    return {"root_cause": rc["rc_id"], "fix_task": fix, "queued": queued}


def mark(conn: sqlite3.Connection, finding_id: str, state: str, note: str, *, job_id: str | None = None,
         audit_id: str | None = None, now: float | None = None) -> dict[str, object]:
    """duplicate and wont-fix-proposed come from triage; wont-fix is a person's decision."""
    actor, audit, job = _actor_and_audit(conn, job_id, audit_id, ("triage",))
    if state == "wont-fix" and job is not None:
        raise AuditError("only a person decides wont-fix; triage proposes it")
    allowed = {"duplicate": OPEN, "wont-fix-proposed": OPEN, "wont-fix": OPEN + ("wont-fix-proposed",),
               "open": ("wont-fix-proposed",)}
    if state not in allowed:
        raise AuditError(f"cannot mark a finding {state}")
    now = now or _now()
    with store.write(conn):
        row = conn.execute("SELECT state, task_id FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
        if row is None or row["state"] not in allowed[state]:
            raise AuditError(f"{finding_id} cannot move to {state} from {row['state'] if row else 'nowhere'}")
        conn.execute("UPDATE findings SET state=?, updated_at=? WHERE finding_id=?", (state, now, finding_id))
        _note(conn, finding_id, state, actor, audit_id=audit["audit_id"], job_id=job, note=note, now=now)
        store.event(conn, actor, f"finding_{state}", row["task_id"], f"{finding_id} {note}"[:300], now)
    return {"finding_id": finding_id, "state": state}


# ------------------------------------------------------------------ landing reconciliation

def reconcile(cfg: Config, conn: sqlite3.Connection, now: float | None = None) -> dict[str, list[str]]:
    """Mark findings fixed from landed trailers, but only on commits that touch a regression test path."""
    trailer = re.compile(rf"^{re.escape(cfg.audit_trailer)}:\s*(F-\d{{4,}}(?:\s*,\s*F-\d{{4,}})*)\s*$", re.MULTILINE)
    head = gitops.git(cfg.repo, "rev-parse", cfg.remote_main).stdout.strip()
    if not head:
        return {"fixed": [], "refused": []}
    last = store.get_meta(conn, "audit_reconciled_sha")
    span = f"{last}..{head}" if last and gitops.git(cfg.repo, "merge-base", "--is-ancestor", last, head).returncode == 0 else head
    log = gitops.git(cfg.repo, "log", span, f"--grep={cfg.audit_trailer}: F-", "--format=%H%x1f%B%x1e").stdout
    fixed: list[str] = []
    refused: list[str] = []
    now = now or _now()
    with store.write(conn):
        for entry in reversed([e.strip() for e in log.split("\x1e") if e.strip()]):
            sha, _, body = entry.partition("\x1f")
            ids = [i.strip() for m in trailer.finditer(body) for i in m.group(1).split(",")]
            if not ids:
                continue
            touched = [p for p in gitops.git(cfg.repo, "show", "--name-only", "--format=", sha).stdout.splitlines() if p]
            tests = [p for p in touched if regression_path(cfg, p)]
            for finding_id in ids:
                row = conn.execute("SELECT state, task_id FROM findings WHERE finding_id=?", (finding_id,)).fetchone()
                seen = conn.execute("SELECT 1 FROM finding_notes WHERE finding_id=? AND commit_sha=?", (finding_id, sha)).fetchone()
                if row is None or seen:
                    continue
                if not tests:
                    _note(conn, finding_id, "trailer_without_test", "system", commit_sha=sha, now=now,
                          note="the commit names this finding but touches no regression test path")
                    refused.append(finding_id)
                    continue
                if row["state"] in ("open", "reopened", "fix-scheduled"):
                    conn.execute("UPDATE findings SET state='fixed', fixed_sha=?, regression_json=?, updated_at=? WHERE finding_id=?",
                                 (sha, json.dumps(tests), now, finding_id))
                    fixed.append(finding_id)
                    store.event(conn, "system", "finding_fixed", row["task_id"], f"{finding_id} at {sha[:9]}", now)
                _note(conn, finding_id, "fixed", "system", commit_sha=sha, evidence=[{"path": t, "sha256": ""} for t in tests], now=now)
        store.set_meta(conn, "audit_reconciled_sha", head)
    return {"fixed": fixed, "refused": refused}


# ------------------------------------------------------------------ reporting

def report(cfg: Config, conn: sqlite3.Connection, audit_id: str | None = None, now: float | None = None) -> dict[str, object]:
    """Write the audit's snapshot to its folder and return the numbers that track progress across audits."""
    now = now or _now()
    queue.refresh(conn, cfg, now)
    with store.write(conn):
        _refresh(conn, cfg, now)
    reconciled = reconcile(cfg, conn, now)
    audit = resolve_audit(conn, audit_id)
    aid = audit["audit_id"]
    folder = audit_dir(cfg, aid)
    findings = [dict(r) for r in conn.execute("SELECT * FROM findings ORDER BY seq")]
    with open(folder / "findings.jsonl", "w", encoding="utf-8") as fh:
        for f in findings:
            f["notes"] = [dict(n) for n in conn.execute("SELECT * FROM finding_notes WHERE finding_id=? ORDER BY id", (f["finding_id"],))]
            fh.write(json.dumps(f) + "\n")
    jobs = [dict(r) for r in conn.execute("SELECT job_id, kind, subject, status, attempts, finished_by, reason FROM audit_jobs "
                                          "WHERE audit_id=? ORDER BY job_id", (aid,))]
    earlier = {r[0] for r in conn.execute("SELECT DISTINCT subject FROM audit_jobs WHERE kind='slice' AND status='DONE' "
                                          "AND audit_id IN (SELECT audit_id FROM audits WHERE opened_at < ?)",
                                          (audit["opened_at"],))}
    new_here = [f for f in findings if f["audit_id"] == aid]
    open_by: dict[str, int] = {s: 0 for s in SEVERITIES}
    for f in findings:
        if f["state"] in LIVE:
            open_by[f["severity"]] += 1
    done_tasks = {r[0] for r in conn.execute("SELECT task_id FROM tasks WHERE status='DONE' AND task_id NOT LIKE 'fix-rc-%'")}
    audited = {r[0] for r in conn.execute("SELECT DISTINCT subject FROM audit_jobs WHERE kind='slice' AND status='DONE'")}
    fixes = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM tasks WHERE task_id LIKE 'fix-rc-%' GROUP BY status")}
    pending = [j for j in jobs if j["status"] not in ("DONE",)]
    if not pending and audit["status"] == "OPEN":
        with store.write(conn):
            conn.execute("UPDATE audits SET status='CLOSED', closed_at=? WHERE audit_id=?", (now, aid))
            store.event(conn, "system", "audit_closed", None, aid, now)
    summary: dict[str, object] = {
        "audit_id": aid, "sha": audit["sha"], "status": "CLOSED" if not pending else "OPEN",
        "jobs": {s: sum(1 for j in jobs if j["status"] == s) for s in sorted({j["status"] for j in jobs})},
        "findings_new": len(new_here),
        "verified_here": sum(1 for f in findings if f["verified_audit"] == aid),
        "reopened_here": conn.execute("SELECT COUNT(DISTINCT finding_id) FROM finding_notes WHERE audit_id=? AND kind='reopened'",
                                      (aid,)).fetchone()[0],
        "escapes": [f["finding_id"] for f in new_here if f["task_id"] in earlier],
        "open_by_severity": open_by, "fix_tasks": fixes,
        "never_audited": sorted(done_tasks - audited), "reconciled": reconciled,
        "parked_jobs": [j["job_id"] for j in jobs if j["status"] == "NEEDS_TRIAGE"],
    }
    lines = [f"# Audit {aid}", "", f"Commit `{audit['sha']}`, {summary['status'].lower()}.", "",  # type: ignore[union-attr]
             "| Measure | Value |", "|---|---|",
             f"| New findings | {summary['findings_new']} |", f"| Fixes verified | {summary['verified_here']} |",
             f"| Fixes reopened | {summary['reopened_here']} |", f"| Escapes (found in a slice an earlier audit passed) | {len(summary['escapes'])} |",  # type: ignore[arg-type]
             f"| Live findings P0 / P1 / P2 | {open_by['P0']} / {open_by['P1']} / {open_by['P2']} |",
             f"| Landed tasks never audited | {len(summary['never_audited'])} |", "",  # type: ignore[arg-type]
             "| Job | Kind | Status | By |", "|---|---|---|---|"]
    lines += [f"| {j['job_id']} | {j['kind']} | {j['status']} | {j['finished_by'] or ''} |" for j in jobs]
    (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (folder / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def overview(conn: sqlite3.Connection) -> dict[str, object]:
    """Small numbers for `workboard status` and the dashboard."""
    latest = conn.execute("SELECT audit_id, sha, status FROM audits ORDER BY opened_at DESC LIMIT 1").fetchone()
    jobs = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM audit_jobs WHERE audit_id=? GROUP BY status",
                                             (latest[0],))} if latest else {}
    live = {r[0]: r[1] for r in conn.execute(f"SELECT severity, COUNT(*) FROM findings WHERE state IN ({','.join('?' * len(LIVE))}) "
                                             "GROUP BY severity", LIVE)}
    return {"latest": dict(latest) if latest else None, "jobs": jobs, "live_findings": live,
            "awaiting_decision": [r[0] for r in conn.execute("SELECT finding_id FROM findings WHERE state='wont-fix-proposed'")],
            "parked_jobs": [r[0] for r in conn.execute("SELECT job_id FROM audit_jobs WHERE status='NEEDS_TRIAGE'")]}


def has_work(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM audit_jobs WHERE status IN ('READY','CLAIMED','BLOCKED') LIMIT 1").fetchone() is not None
