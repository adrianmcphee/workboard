"""Run one agent CLI under a lease: own process group, heartbeat, hard deadline, clean stop."""
from __future__ import annotations

import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Callable

from .config import Harness

STOPPED = 124  # exit code reported when the lease ended before the agent did


def build_command(harness: Harness, worktree: Path, prompt_file: Path) -> list[str]:
    values = {"worktree": str(worktree), "prompt_file": str(prompt_file), "model": harness.model}
    command = [re.sub(r"\{(worktree|prompt_file|model)\}", lambda m: values[m.group(1)], part) for part in harness.command]
    if harness.model and harness.model_flag:
        command += [harness.model_flag, harness.model]
    if harness.prompt == "arg":
        command.append(prompt_file.read_text(encoding="utf-8"))
    return command


def launches_claude_code(harness: Harness) -> bool:
    return harness.usage == "claude-code" or Path(harness.command[0]).name == "claude"


def agent_env(harness: Harness, extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """The agent's environment. A headless Claude Code turn ends when the agent stops issuing commands, which kills
    any build it left in the background, so its background tasks are off unless the harness's own env says otherwise."""
    env = {k: v for k, v in os.environ.items() if k not in harness.env_unset}
    if launches_claude_code(harness):
        env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] = "1"
    env.update(harness.env)
    env.update(extra_env or {})
    return env


def run(harness: Harness, worktree: Path, prompt_file: Path, log: Path, *, beat: Callable[[], bool],
        beat_every_s: float, deadline: float, should_stop: Callable[[], bool] = lambda: False,
        extra_env: dict[str, str] | None = None) -> int:
    """Run the agent to completion. `beat` returns False once the lease is gone, which stops the agent."""
    env = agent_env(harness, extra_env)
    command = build_command(harness, worktree, prompt_file)
    with open(log, "ab") as fh:
        try:
            proc = subprocess.Popen(
                command, cwd=str(worktree), env=env, stdout=fh, stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE if harness.prompt == "stdin" else subprocess.DEVNULL,
                start_new_session=True, close_fds=True)
        except OSError as exc:
            fh.write(f"\nworkboard: cannot start {command[0]}: {exc}\n".encode())
            return 127
        if harness.prompt == "stdin" and proc.stdin:
            try:
                proc.stdin.write(prompt_file.read_bytes())
                proc.stdin.close()
            except BrokenPipeError:
                pass
        next_beat = 0.0
        while proc.poll() is None:
            now = time.time()
            if now >= next_beat:
                if not beat() or now > deadline:
                    _stop(proc)
                    return STOPPED
                next_beat = now + beat_every_s
            if should_stop():
                _stop(proc)
                return STOPPED
            time.sleep(0.2)
        return proc.returncode or 0


def _stop(proc: subprocess.Popen) -> None:
    """Terminate the agent's own process group, never anything matched by name."""
    for sig, wait in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            return
        end = time.time() + wait
        while proc.poll() is None and time.time() < end:
            time.sleep(0.1)
        if proc.poll() is not None:
            return


def quota_hit(harness: Harness, log: Path, tail_bytes: int = 200_000) -> bool:
    if not harness.quota_patterns:
        return False
    try:
        with open(log, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - tail_bytes))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in harness.quota_patterns)
