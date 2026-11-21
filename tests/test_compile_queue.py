"""The compile queue: heavy commands share slots, everything else passes through, and every wait is recorded."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

from workboard import compile_queue, config, gitops, interventions
from tests.support import ProjectCase

CARGO = {"subcommands": ["build", "test", "check"], "flags": [], "value_options": ["-C", "--config"]}
TOOLS = {"faketool": CARGO, "cmake": {"subcommands": [], "flags": ["--build"], "value_options": []}}


class Rules(unittest.TestCase):
    def test_value_options_are_skipped_and_flags_or_the_first_word_decide(self) -> None:
        self.assertTrue(compile_queue.is_heavy(CARGO, ["-C", "crates/app", "test", "--lib"]))
        self.assertTrue(compile_queue.is_heavy(CARGO, ["--config", "net.offline=true", "build"]))
        self.assertTrue(compile_queue.is_heavy(CARGO, ["+stable", "--locked", "check"]))
        self.assertFalse(compile_queue.is_heavy(CARGO, ["-C", "crates/app", "fmt", "--check"]))
        self.assertFalse(compile_queue.is_heavy(CARGO, ["--version"]))
        self.assertFalse(compile_queue.is_heavy(None, ["build"]))
        self.assertTrue(compile_queue.is_heavy(TOOLS["cmake"], ["--build", "out", "--target", "app"]))
        self.assertFalse(compile_queue.is_heavy(TOOLS["cmake"], ["-S", ".", "-B", "out"]))

    def test_no_slots_or_no_tools_means_no_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(compile_queue.env_for(Path(tmp), 0, TOOLS), {})
            self.assertEqual(compile_queue.env_for(Path(tmp), 2, {}), {})


class Shim(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="workboard-compile-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.state, self.log = root / "state", root / "log"
        bin_dir = root / "bin"
        bin_dir.mkdir()
        tool = bin_dir / "faketool"
        tool.write_text('#!/bin/sh\necho "start ${WORKBOARD_COMPILE_HELD:-none} $*" >> "$LOG"\nsleep "${SLEEP:-1}"\n'
                        'echo "end $*" >> "$LOG"\n')
        tool.chmod(0o755)
        base = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        self.env = {**os.environ, **compile_queue.env_for(self.state, 1, TOOLS, base), "LOG": str(self.log)}
        self.env.pop(compile_queue.HELD_ENV, None)

    def run_pair(self, a: list[str], b: list[str], env: dict[str, str] | None = None) -> None:
        env = env or self.env
        first = subprocess.Popen(["faketool", *a], env=env, stderr=subprocess.DEVNULL)
        time.sleep(0.3)
        second = subprocess.Popen(["faketool", *b], env=env, stderr=subprocess.DEVNULL)
        self.assertEqual(first.wait(timeout=30), 0)
        self.assertEqual(second.wait(timeout=30), 0)

    def kinds(self) -> list[str]:
        kinds = [line.split()[0] for line in self.log.read_text().splitlines()]
        self.log.unlink()
        return kinds

    def events(self, released: int) -> list[dict[str, object]]:
        path = compile_queue.queue_dir(self.state) / "events.jsonl"
        deadline = time.time() + 15
        rows: list[dict[str, object]] = []
        while time.time() < deadline:
            rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
            if sum(r["event"] == "released" for r in rows) >= released:
                return rows
            time.sleep(0.1)
        self.fail(f"expected {released} released record(s), got {rows}")

    def test_heavy_commands_take_turns_and_record_their_wait_and_hold(self) -> None:
        self.run_pair(["build"], ["-C", "x", "test"])
        self.assertEqual(self.kinds(), ["start", "end", "start", "end"])
        rows = self.events(released=2)
        self.assertEqual([r["event"] for r in rows].count("queued"), 1)
        waits = sorted(float(r["wait_s"]) for r in rows if r["event"] == "acquired")  # type: ignore[arg-type]
        self.assertEqual(waits[0], 0.0)
        self.assertGreater(waits[1], 0.3)
        holds = [float(r["hold_s"]) for r in rows if r["event"] == "released"]  # type: ignore[arg-type]
        self.assertTrue(all(0.9 <= h < 10 for h in holds), holds)
        self.assertTrue(all(str(r["cmd"]).startswith("faketool ") and r["cwd"] for r in rows))

    def test_light_and_nested_commands_never_wait_and_record_nothing(self) -> None:
        self.run_pair(["fmt"], ["--version"])
        self.assertEqual(self.kinds()[:2], ["start", "start"])
        self.run_pair(["build"], ["test"], env={**self.env, compile_queue.HELD_ENV: "1"})
        self.assertEqual(self.kinds()[:2], ["start", "start"])
        self.assertFalse((compile_queue.queue_dir(self.state) / "events.jsonl").exists())

    def test_the_watcher_holds_no_pipe_and_no_slot(self) -> None:
        for _ in range(2):  # reading to EOF would hang if the watcher held the pipe
            done = subprocess.run(["faketool", "build"], env=self.env, capture_output=True, timeout=30)
            self.assertEqual(done.returncode, 0)
        rows = self.events(released=2)
        self.assertNotIn("queued", [r["event"] for r in rows])  # the first build's watcher held no slot

    def test_a_stopped_waiter_exits_cleanly_and_records_abandoned(self) -> None:
        holder = subprocess.Popen(["faketool", "build"], env={**self.env, "SLEEP": "2"}, stderr=subprocess.DEVNULL)
        time.sleep(0.4)
        waiter = subprocess.Popen(["faketool", "check"], env=self.env, stderr=subprocess.DEVNULL)
        time.sleep(0.6)
        view = compile_queue.view(self.state, 1)
        self.assertEqual([h["pid"] for h in view["holders"]], [holder.pid])  # type: ignore[union-attr]
        self.assertEqual([w["pid"] for w in view["waiters"]], [waiter.pid])  # type: ignore[union-attr]
        waiter.send_signal(signal.SIGQUIT)
        self.assertEqual(waiter.wait(timeout=10), 128 + signal.SIGQUIT)
        holder.wait(timeout=30)
        rows = self.events(released=1)
        self.assertEqual([r["event"] for r in rows if r["pid"] == waiter.pid], ["queued", "abandoned"])
        self.assertEqual(compile_queue.view(self.state, 1)["waiters"], [])


class Wiring(ProjectCase):
    guards = '"faketool build"'

    def setUp(self) -> None:
        super().setUp()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "faketool").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "faketool").chmod(0o755)
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
        self.addCleanup(os.environ.__setitem__, "PATH", os.environ["PATH"].split(os.pathsep, 1)[1])
        with open(self.repo / "workboard.toml", "a", encoding="utf-8") as fh:
            fh.write('\n[compile]\nslots = 2\nalert_wait_min = 1\n\n[compile.tools.faketool]\nsubcommands = ["build"]\n')
        self.cfg = config.load(self.repo)

    def test_guards_run_through_the_queue_and_long_waits_with_free_memory_raise_a_card(self) -> None:
        passed, why = gitops.run_guards(self.cfg, self.repo, self.tmp / "guard.log")
        self.assertTrue(passed, why)
        events = (compile_queue.queue_dir(self.cfg.state) / "events.jsonl").read_text().splitlines()
        self.assertIn("acquired", [json.loads(e)["event"] for e in events])
        from workboard import dashboard
        from workboard.worker import Worker
        agent_path = Worker(self.cfg, "alpha")._queue_env()["PATH"]
        self.assertTrue(agent_path.startswith(str(compile_queue.queue_dir(self.cfg.state) / "shims") + os.pathsep))
        self.assertEqual(dashboard.snapshot(self.cfg)["compile"]["slots"], 2)  # type: ignore[index]
        view = {"enabled": True, "slots": 2, "waiters": [{"pid": 1}], "longest_wait_s": 600, "memory": {"mem_available_pct": 70.0}}
        self.assertEqual([c["kind"] for c in interventions.compile_cards(self.cfg, view)], ["compile_wait"])
        tight = {**view, "memory": {"mem_available_pct": 5.0}}
        self.assertEqual(interventions.compile_cards(self.cfg, tight), [])  # memory is the limit, not the slots

    def test_unknown_or_empty_tool_rules_are_refused(self) -> None:
        with open(self.repo / "workboard.toml", "a", encoding="utf-8") as fh:
            fh.write('\n[compile.tools.other]\nsubcomands = ["build"]\n')
        with self.assertRaises(config.ConfigError):
            config.load(self.repo)


if __name__ == "__main__":
    unittest.main()
