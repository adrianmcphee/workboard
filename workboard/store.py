"""SQLite storage: tasks, attempts, an append-only event log and worker registry."""
from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
import time
from typing import Iterator

from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  branch TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100,
  harness_pref TEXT,
  lease_min INTEGER NOT NULL,
  paths_json TEXT NOT NULL DEFAULT '[]',
  depends_json TEXT NOT NULL DEFAULT '[]',
  brief_path TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'file',
  status TEXT NOT NULL DEFAULT 'BLOCKED',
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_harness TEXT,
  lease_token TEXT,
  lease_pid INTEGER,
  claimed_at REAL,
  heartbeat_expires_at REAL,
  deadline_at REAL,
  author_harness TEXT,
  avoid_harness TEXT,
  candidate_sha TEXT,
  landed_sha TEXT,
  landed_at REAL,
  reason TEXT,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  role TEXT NOT NULL,
  harness TEXT NOT NULL,
  worker TEXT NOT NULL,
  started_at REAL NOT NULL,
  ended_at REAL,
  outcome TEXT,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at REAL NOT NULL,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  task_id TEXT,
  detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS harness_state (
  harness TEXT PRIMARY KEY,
  cooldown_until REAL NOT NULL DEFAULT 0,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS workers (
  worker_id TEXT PRIMARY KEY,
  harness TEXT NOT NULL,
  slot INTEGER NOT NULL,
  pid INTEGER NOT NULL,
  started_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audits (
  audit_id TEXT PRIMARY KEY,
  sha TEXT NOT NULL UNIQUE,
  build_id TEXT,
  quick INTEGER NOT NULL DEFAULT 0,
  worktree TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  status TEXT NOT NULL,
  opened_at REAL NOT NULL,
  closed_at REAL
);
CREATE TABLE IF NOT EXISTS audit_jobs (
  job_id TEXT PRIMARY KEY,
  audit_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  subject TEXT NOT NULL,
  title TEXT NOT NULL,
  brief_path TEXT NOT NULL,
  evidence_dir TEXT NOT NULL,
  resource TEXT,
  avoid_harness TEXT,
  status TEXT NOT NULL,
  lease_min INTEGER NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_harness TEXT,
  lease_token TEXT,
  lease_pid INTEGER,
  claimed_at REAL,
  heartbeat_expires_at REAL,
  deadline_at REAL,
  finished_by TEXT,
  finished_at REAL,
  reason TEXT,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
  finding_id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL UNIQUE,
  fingerprint TEXT NOT NULL,
  audit_id TEXT NOT NULL,
  job_id TEXT,
  task_id TEXT NOT NULL,
  refs_json TEXT NOT NULL DEFAULT '[]',
  step TEXT,
  severity TEXT NOT NULL,
  title TEXT NOT NULL,
  repro TEXT NOT NULL,
  expected TEXT NOT NULL,
  reference TEXT,
  state TEXT NOT NULL,
  root_cause TEXT,
  fix_task TEXT,
  fixed_sha TEXT,
  regression_json TEXT,
  verified_audit TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS findings_fingerprint ON findings (fingerprint);
CREATE TABLE IF NOT EXISTS finding_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id TEXT NOT NULL,
  audit_id TEXT,
  job_id TEXT,
  actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  commit_sha TEXT,
  at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS root_causes (
  rc_id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL UNIQUE,
  audit_id TEXT NOT NULL,
  title TEXT NOT NULL,
  severity TEXT NOT NULL,
  task_id TEXT NOT NULL,
  fix_task TEXT,
  generation INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS review_rejections (
  task_id TEXT NOT NULL,
  candidate_sha TEXT NOT NULL,
  at REAL NOT NULL,
  PRIMARY KEY (task_id, candidate_sha)
);
CREATE TABLE IF NOT EXISTS directives (
  directive_id TEXT PRIMARY KEY,
  seq INTEGER NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  expires_at REAL,
  cleared_at REAL,
  scope_harness TEXT,
  scope_role TEXT,
  scope_task TEXT,
  mode TEXT NOT NULL,
  message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS directives_active ON directives (active, seq);
CREATE TABLE IF NOT EXISTS active_builds (
  build_id TEXT PRIMARY KEY,
  pid INTEGER NOT NULL,
  pgid INTEGER NOT NULL,
  slot TEXT NOT NULL DEFAULT '',
  worker TEXT NOT NULL DEFAULT '',
  harness TEXT NOT NULL DEFAULT '',
  role TEXT NOT NULL DEFAULT '',
  task_id TEXT NOT NULL DEFAULT '',
  cwd TEXT NOT NULL DEFAULT '',
  cmd TEXT NOT NULL DEFAULT '',
  started_at REAL NOT NULL
);
"""

OPEN_STATUSES = ("BLOCKED", "READY", "CLAIMED", "AWAITING_REVIEW", "REVIEWING", "LANDING", "NEEDS_TAKEOVER", "NEEDS_TRIAGE")
IN_FLIGHT = ("CLAIMED", "AWAITING_REVIEW", "REVIEWING", "LANDING", "NEEDS_TAKEOVER")


def connect(cfg: Config) -> sqlite3.Connection:
    cfg.state.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(cfg.db), timeout=60.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def write(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One immediate transaction: every state change commits or rolls back as a unit."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def event(conn: sqlite3.Connection, actor: str, kind: str, task_id: str | None = None, detail: str = "",
          now: float | None = None) -> None:
    conn.execute("INSERT INTO events (at, actor, kind, task_id, detail) VALUES (?,?,?,?,?)",
                 (now or time.time(), actor, kind, task_id, detail[:2000]))


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


def loads(text: str | None) -> list[str]:
    try:
        value = json.loads(text or "[]")
        return [str(v) for v in value] if isinstance(value, list) else []
    except ValueError:
        return []
