"""The worker loop: claim, run the agent under its lease, judge the result by evidence, land."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import time

from . import gitops, harness as runner, prompt, queue, store, usage
from .config import Config

IDLE_SLEEP_S = 5.0


def log_path(cfg: Config, task_id: str, harness: str, role: str) -> Path:
    folder = cfg.state / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{task_id}-{role}-{harness}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"


def target_slots(conn: sqlite3.Connection, harness: str) -> int | None:
    value = store.get_meta(conn, f"slots:{harness}")
    return int(value) if value else None


def live_slots(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in conn.execute("SELECT harness, pid FROM workers").fetchall():
        if queue.pid_alive(row["pid"]):
            out[row["harness"]] = out.get(row["harness"], 0) + 1
    return out


def note_takeover(cfg: Config, task_id: str, harness: str, heading: str, detail: str, worktree: Path | None) -> None:
    """Append what the next author needs: why the last attempt ended and what it left behind."""
    lines = [f"### {datetime.now().strftime('%Y-%m-%d %H:%M')} {heading} ({harness})", "", detail.strip(), ""]
    if worktree and (worktree / ".git").exists():
        log = gitops.git(worktree, "log", "--oneline", "-n", "15", f"{cfg.remote_main}..HEAD").stdout.strip()
        status = gitops.git(worktree, "status", "--short").stdout.strip()
        lines += ["Commits on the branch:", "```", log or "(none)", "```", "Uncommitted:", "```", status or "(clean)", "```", ""]
    with open(prompt.takeover_path(cfg, task_id), "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def land_task(cfg: Config, conn: sqlite3.Connection, task_id: str, branch: str, worker: str) -> str:
    if not queue.take_landing(conn, task_id, worker, os.getpid()):
        return "LANDING"
    log = log_path(cfg, task_id, "workboard", "land")
    try:
        ok, detail = gitops.land(cfg, task_id, branch, log)
    except Exception as exc:
        ok, detail = False, f"landing error: {exc}"[:400]
    if ok:
        queue.landed(conn, cfg, task_id, detail, actor=worker)
        kept = gitops.retire(cfg, task_id, branch, detail)
        if kept != "retired":
            with store.write(conn):
                store.event(conn, worker, "worktree_kept", task_id, kept)
        return "DONE"
    note_takeover(cfg, task_id, "workboard", "Landing failed", detail + "\nRebase onto current main keeping both intents, "
                  "rerun the guards and leave the branch clean.", cfg.worktree(task_id))
    return queue.land_failed(conn, cfg, task_id, detail)


def land_pending(cfg: Config, conn: sqlite3.Connection, worker: str) -> int:
    count = 0
    for row in conn.execute("SELECT task_id, branch FROM tasks WHERE status='LANDING' ORDER BY priority DESC, updated_at").fetchall():
        if land_task(cfg, conn, row["task_id"], row["branch"], worker) == "DONE":
            count += 1
    return count


class Worker:
    def __init__(self, cfg: Config, harness: str, slot: int = 1, once: bool = False):
        if harness not in cfg.harnesses:
            raise SystemExit(f"unknown harness {harness!r}; workboard.toml defines: {', '.join(cfg.harnesses) or 'none'}")
        self.cfg, self.harness, self.slot, self.once = cfg, cfg.harnesses[harness], slot, once
        self.id = f"{harness}-{slot}-{os.getpid()}"
        self.stop = False
        self.conn = store.connect(cfg)

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while not self.stop and time.time() < end:
            time.sleep(0.2)

    def run(self) -> int:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda *_: setattr(self, "stop", True))
        with store.write(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO workers (worker_id, harness, slot, pid, started_at) VALUES (?,?,?,?,?)",
                              (self.id, self.harness.name, self.slot, os.getpid(), time.time()))
            store.event(self.conn, self.id, "worker_started")
        try:
            while not self.stop:
                target = target_slots(self.conn, self.harness.name)
                if target is not None and self.slot > target:
                    break  # the fleet was resized down: finish cleanly instead of being killed mid-task
                if shutil.disk_usage(self.cfg.state).free / 2**30 < self.cfg.min_free_gib:
                    self._sleep(30)
                    continue
                land_pending(self.cfg, self.conn, self.id)
                verdict = None
                try:
                    snap = usage.snapshot(self.cfg)
                    slots = live_slots(self.conn)
                    mine = self.conn.execute("SELECT COUNT(*) FROM tasks WHERE status='CLAIMED' AND lease_harness=?",
                                             (self.harness.name,)).fetchone()[0]
                    verdict = usage.claim_verdict(snap, self.harness.name, mine, slots.get(self.harness.name, 0), set(slots))
                except Exception:
                    verdict = None
                claim = queue.claim(self.conn, self.cfg, self.harness.name, self.id, os.getpid(), usage_verdict=verdict)
                if not claim["claimed"]:
                    if claim["reason"] == "board_complete" or self.once:
                        break
                    self._sleep(IDLE_SLEEP_S)
                    continue
                try:
                    (self.author if claim["role"] == "author" else self.review)(claim)
                except Exception as exc:
                    self._crash(claim, exc)
                if self.once:
                    land_pending(self.cfg, self.conn, self.id)
                    break
        finally:
            with store.write(self.conn):
                self.conn.execute("DELETE FROM workers WHERE worker_id=?", (self.id,))
                store.event(self.conn, self.id, "worker_stopped")
        return 0

    def _crash(self, claim: dict[str, object], exc: Exception) -> None:
        task_id, token, reason = str(claim["task_id"]), str(claim["token"]), f"worker error: {exc}"[:400]
        if claim["role"] == "author":
            queue.finish_author(self.conn, self.cfg, task_id, token, ok=False, reason=reason)
        else:
            queue.finish_review(self.conn, self.cfg, task_id, token, "incomplete", reason)

    def _run_agent(self, claim: dict[str, object], worktree: Path, log: Path) -> int:
        task_id, token = str(claim["task_id"]), str(claim["token"])
        prompt_file = self.cfg.handoff(task_id) / f"prompt-{claim['role']}.md"
        prompt_file.write_text(prompt.render(self.cfg, claim, self.harness.name, worktree, self.conn), encoding="utf-8")
        return runner.run(
            self.harness, worktree, prompt_file, log,
            beat=lambda: queue.heartbeat(self.conn, self.cfg, task_id, token),
            beat_every_s=self.cfg.heartbeat_s, deadline=time.time() + int(claim["lease_min"]) * 60.0,  # type: ignore[arg-type]
            should_stop=lambda: self.stop)

    def author(self, claim: dict[str, object]) -> None:
        task_id, token, branch = str(claim["task_id"]), str(claim["token"]), str(claim["branch"])
        worktree = gitops.ensure_worktree(self.cfg, task_id, branch)
        log = log_path(self.cfg, task_id, self.harness.name, "author")
        code = self._run_agent(claim, worktree, log)
        quota = code != 0 and runner.quota_hit(self.harness, log)
        problem = ""
        if code == runner.STOPPED:
            problem = "the lease ended or the worker was stopped before the agent finished"
        elif code != 0:
            problem = f"the agent exited with status {code}" + (" after hitting its usage limit" if quota else "")
        elif gitops.dirty(worktree):
            problem = "the agent left uncommitted changes in the worktree"
        elif gitops.ahead(self.cfg, worktree) == 0:
            problem = "the agent made no commits"
        elif gitops.has_merges(self.cfg, worktree):
            problem = "the branch contains a merge commit; history must be linear"
        else:
            passed, why = gitops.run_guards(self.cfg, worktree, log, {"WORKBOARD_TASK": task_id, "WORKBOARD_BRANCH": branch})
            if not passed:
                problem = why
        if problem:
            note_takeover(self.cfg, task_id, self.harness.name, "Attempt did not reach review", problem, worktree)
            queue.finish_author(self.conn, self.cfg, task_id, token, ok=False, reason=problem.splitlines()[0], quota=quota)
            return
        status = queue.finish_author(self.conn, self.cfg, task_id, token, ok=True, candidate_sha=gitops.head(worktree))
        if status == "LANDING":
            land_task(self.cfg, self.conn, task_id, branch, self.id)

    def review(self, claim: dict[str, object]) -> None:
        task_id, token, branch = str(claim["task_id"]), str(claim["token"]), str(claim["branch"])
        worktree = gitops.ensure_worktree(self.cfg, task_id, branch)
        verdict_file = prompt.verdict_path(self.cfg, task_id)
        verdict_file.unlink(missing_ok=True)
        log = log_path(self.cfg, task_id, self.harness.name, "review")
        self._run_agent(claim, worktree, log)
        try:
            verdict = json.loads(verdict_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            verdict = None
        if not isinstance(verdict, dict) or not isinstance(verdict.get("passed"), bool):
            queue.finish_review(self.conn, self.cfg, task_id, token, "incomplete", "no readable verdict file")
            return
        if gitops.dirty(worktree) or gitops.head(worktree) != claim["candidate_sha"]:
            queue.finish_review(self.conn, self.cfg, task_id, token, "incomplete", "the reviewer changed the worktree")
            return
        if verdict["passed"]:
            if queue.finish_review(self.conn, self.cfg, task_id, token, "pass", str(verdict.get("summary", ""))) == "LANDING":
                land_task(self.cfg, self.conn, task_id, branch, self.id)
            return
        defects = [str(d) for d in verdict.get("defects", [])] or ["(the reviewer gave no detail)"]
        note_takeover(self.cfg, task_id, self.harness.name, "Review rejected the branch",
                      "Fix each defect, or show with evidence why it is not one:\n" + "\n".join(f"- {d}" for d in defects), worktree)
        queue.finish_review(self.conn, self.cfg, task_id, token, "fail", "; ".join(defects))
