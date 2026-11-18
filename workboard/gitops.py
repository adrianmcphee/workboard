"""Git mechanics: one worktree per task, and landing as rebase, guard, then a locked fast-forward push."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import sqlite3
import subprocess
from typing import Iterator

from .config import Config

PUSH_RETRIES = 4


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k.startswith(("GIT_AUTHOR", "GIT_COMMITTER", "GIT_SSH", "GIT_CONFIG"))}
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False, env=env)


def out(cwd: Path, *args: str) -> str:
    result = git(cwd, *args)
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {(result.stderr or result.stdout).strip()[-600:]}")
    return result.stdout.strip()


def fetch(cfg: Config) -> None:
    out(cfg.repo, "fetch", "--prune", cfg.remote)


def ensure_worktree(cfg: Config, task_id: str, branch: str) -> Path:
    """Open the task's worktree from current main, or return the one a previous attempt left."""
    path = cfg.worktree(task_id)
    if (path / ".git").exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    git(cfg.repo, "worktree", "prune")
    if git(cfg.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        out(cfg.repo, "worktree", "add", str(path), branch)
    else:
        fetch(cfg)
        out(cfg.repo, "worktree", "add", "-b", branch, str(path), cfg.remote_main)
    return path


def head(worktree: Path) -> str:
    return out(worktree, "rev-parse", "HEAD")


def dirty(worktree: Path) -> str:
    return out(worktree, "status", "--porcelain=v1", "--untracked-files=all")


def ahead(cfg: Config, worktree: Path) -> int:
    return int(out(worktree, "rev-list", "--count", f"{cfg.remote_main}..HEAD") or 0)


def has_merges(cfg: Config, worktree: Path) -> bool:
    return bool(out(worktree, "rev-list", "--merges", f"{cfg.remote_main}..HEAD"))


def is_landed(cfg: Config, sha: str) -> bool:
    return git(cfg.repo, "merge-base", "--is-ancestor", sha, cfg.remote_main).returncode == 0


def run_guards(cfg: Config, worktree: Path, log: Path, env: dict[str, str] | None = None) -> tuple[bool, str]:
    """Run every guard command in the worktree. Returns (passed, failing command and its output tail)."""
    for command in cfg.guards:
        with open(log, "ab") as fh:
            fh.write(f"\n$ {command}\n".encode())
            fh.flush()
            result = subprocess.run(command, shell=True, cwd=str(worktree), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env={**os.environ, **(env or {})}, check=False)
            fh.write(result.stdout)
        if result.returncode:
            tail = result.stdout.decode("utf-8", errors="replace")[-1500:]
            return False, f"guard failed: {command}\n{tail}"
    return True, ""


@contextmanager
def land_lock(cfg: Config, blocking: bool = True) -> Iterator[bool]:
    cfg.state.mkdir(parents=True, exist_ok=True)
    with open(cfg.state / "land.lock", "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def land(cfg: Config, task_id: str, branch: str, log: Path) -> tuple[bool, str]:
    """Put one reviewed branch on main. Returns (True, landed sha) or (False, reason).

    Rebase and guards run outside the lock. The lock covers only fetch, check and push, and the
    push names the exact commit that was guarded. If main moved meanwhile, the loop starts again.
    """
    worktree = cfg.worktree(task_id)
    if not (worktree / ".git").exists():
        return False, "worktree missing"
    if dirty(worktree):
        return False, "worktree has uncommitted changes"
    env = {"WORKBOARD_TASK": task_id, "WORKBOARD_BRANCH": branch}
    if cfg.land_command:
        with land_lock(cfg):
            with open(log, "ab") as fh:
                result = subprocess.run(cfg.land_command, shell=True, cwd=str(worktree), stdout=fh,
                                        stderr=subprocess.STDOUT, env={**os.environ, **env}, check=False)
            if result.returncode:
                return False, f"land command exited {result.returncode}"
            fetch(cfg)
            return True, out(cfg.repo, "rev-parse", cfg.remote_main)
    for _ in range(PUSH_RETRIES):
        fetch(cfg)
        if has_merges(cfg, worktree):
            return False, "branch contains merge commits; history must be linear"
        rebase = git(worktree, "rebase", cfg.remote_main)
        if rebase.returncode:
            files = git(worktree, "diff", "--name-only", "--diff-filter=U").stdout.split()
            git(worktree, "rebase", "--abort")
            return False, "rebase conflict on: " + (", ".join(files) or "unknown files")
        ok, why = run_guards(cfg, worktree, log, env)
        if not ok:
            return False, f"after rebase, {why}"
        sha = head(worktree)
        with land_lock(cfg):
            fetch(cfg)
            if git(worktree, "merge-base", "--is-ancestor", cfg.remote_main, sha).returncode:
                continue  # main moved while guards ran: rebase again outside the lock
            push = git(worktree, "push", cfg.remote, f"{sha}:refs/heads/{cfg.main}")
            if push.returncode == 0:
                fetch(cfg)
                return True, sha
    return False, "main kept moving; push was rejected repeatedly"


def retire(cfg: Config, task_id: str, branch: str, landed_sha: str) -> str:
    """Remove a landed task's worktree and branch. Never forces: a dirty worktree is left in place."""
    worktree = cfg.worktree(task_id)
    if (worktree / ".git").exists():
        if dirty(worktree):
            return "kept: worktree has uncommitted changes"
        removed = git(cfg.repo, "worktree", "remove", str(worktree))
        if removed.returncode:
            return f"kept: {removed.stderr.strip()[-200:]}"
    tip = git(cfg.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").stdout.strip()
    if tip and (tip == landed_sha or is_landed(cfg, tip) or bool(cfg.land_command)):
        git(cfg.repo, "branch", "-D", branch)
    return "retired"


def commits_between(cfg: Config, older: str | None, newer: str) -> set[str] | None:
    args = ["rev-list", f"{older}..{newer}"] if older else ["rev-list", "--max-count=500", newer]
    result = git(cfg.repo, *args)
    return None if result.returncode else set(result.stdout.split())


def landed_tasks(conn: sqlite3.Connection, commits: set[str]) -> list[sqlite3.Row]:
    return [r for r in conn.execute("SELECT * FROM tasks WHERE status='DONE' AND landed_sha IS NOT NULL ORDER BY task_id")
            if r["landed_sha"] in commits]
