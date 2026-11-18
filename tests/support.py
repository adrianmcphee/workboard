"""A disposable project: a bare remote, a clone, a workboard.toml and two fake harnesses."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from workboard import config, store, tasks

FAKE = Path(__file__).with_name("fake_agent.py")
IDENTITY = {"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}

WORKBOARD_TOML = """
[project]
name = "sample"
tasks = "workboard/tasks"
{extra_project}

[limits]
max_coding = 4
max_attempts = {max_attempts}
min_free_gib = 0

[review]
required = {review}

[guards]
commands = [{guards}]

[build]
schedule = ["12:30", "20:30"]
command = "{build}"

[harness.alpha]
command = ["{python}", "{fake}"]
quota_patterns = ["usage limit"]
cooldown_min = 30

[harness.beta]
command = ["{python}", "{fake}"]
"""


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class ProjectCase(unittest.TestCase):
    review = "true"
    guards = ""
    max_attempts = 6
    build = "true"
    extra_project = ""

    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="workboard-")
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name).resolve()
        for key, value in IDENTITY.items():
            os.environ[key] = value
        self.remote = self.tmp / "remote.git"
        sh(self.tmp, "git", "init", "--bare", "-b", "main", str(self.remote))
        self.repo = self.tmp / "repo"
        sh(self.tmp, "git", "clone", str(self.remote), str(self.repo))
        sh(self.repo, "git", "checkout", "-b", "main")
        (self.repo / "README.md").write_text("sample\n")
        (self.repo / ".gitignore").write_text(".workboard/\n.worktrees/\n")
        (self.repo / "workboard.toml").write_text(WORKBOARD_TOML.format(
            python=sys.executable, fake=FAKE, review=self.review, guards=self.guards,
            max_attempts=self.max_attempts, build=self.build, extra_project=self.extra_project))
        sh(self.repo, "git", "add", "--", "README.md", ".gitignore", "workboard.toml")
        sh(self.repo, "git", "commit", "-m", "start")
        sh(self.repo, "git", "push", "-u", "origin", "main")
        self.cfg = config.load(self.repo)
        self.conn = store.connect(self.cfg)
        self.addCleanup(self.conn.close)

    def task(self, task_id: str, body: str = "Do the thing.", **fields: object) -> None:
        folder = self.cfg.tasks
        folder.mkdir(parents=True, exist_ok=True)
        head = "\n".join(f"{k}: {v}" for k, v in {"id": task_id, "title": f"Title {task_id}", **fields}.items())
        (folder / f"{task_id}.md").write_text(f"---\n{head}\n---\n{body}\n")

    def sync(self) -> dict[str, object]:
        from workboard import queue
        result = tasks.sync(self.conn, self.cfg)
        queue.refresh(self.conn, self.cfg)
        return result

    def status(self, task_id: str) -> str:
        return self.conn.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()[0]

    def row(self, task_id: str):
        return self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()

    def work(self, harness: str) -> None:
        from workboard.worker import Worker
        Worker(self.cfg, harness, once=True).run()

    def remote_files(self) -> set[str]:
        return set(sh(self.tmp, "git", "--git-dir", str(self.remote), "ls-tree", "--name-only", "main").split())
