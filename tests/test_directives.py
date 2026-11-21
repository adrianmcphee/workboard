"""Live operator directives, mid-turn compile cancellation, lease aborts and rejected-SHA guards."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import time
import unittest

from workboard import cli, compile_queue, control, dashboard, directives, gitops, interventions, prompt, queue, store
from tests.support import ProjectCase, sh

ALIVE = lambda _pid: True  # noqa: E731


class DirectivesAndGuards(ProjectCase):
    def test_scoped_directives_gate_claims_and_render_only_in_matching_prompts(self) -> None:
        self.task("export-csv")
        self.task("import-xml")
        self.sync()
        directives.set_directive(self.conn, mode="instruction", role="review", harness="beta",
                                 message="Check CSV escaping carefully.", valid_harnesses=set(self.cfg.harnesses))
        directives.set_directive(self.conn, mode="pause_claims", role="author", task_id="export-*",
                                 message="Hold export slices.", valid_harnesses=set(self.cfg.harnesses))
        # Authoring skips export-csv and claims import-xml
        claimed = queue.claim(self.conn, self.cfg, "alpha", "alpha-1", 1, alive=ALIVE)
        self.assertEqual(claimed["task_id"], "import-xml")
        author_prompt = prompt.render(self.cfg, claimed, "alpha", self.cfg.worktree("import-xml"), self.conn)
        self.assertNotIn("Check CSV escaping carefully.", author_prompt)

        # Now only export-csv remains READY, so authoring returns reason=directive
        blocked = queue.claim(self.conn, self.cfg, "beta", "beta-1", 2, alive=ALIVE)
        self.assertEqual((blocked["claimed"], blocked["reason"]), (False, "directive"))

        # Finish import-xml authoring -> beta can still claim review and receives the scoped instruction
        queue.finish_author(self.conn, self.cfg, "import-xml", str(claimed["token"]), ok=True, candidate_sha="a" * 40)
        rev = queue.claim(self.conn, self.cfg, "beta", "beta-1", 2, alive=ALIVE)
        self.assertEqual((rev["role"], rev["task_id"]), ("review", "import-xml"))
        rev_prompt = prompt.render(self.cfg, rev, "beta", self.cfg.worktree("import-xml"), self.conn)
        self.assertIn("Check CSV escaping carefully.", rev_prompt)

        # Dashboard and CLI status include directives, and control.run clears them
        snap = dashboard.snapshot(self.cfg)
        self.assertEqual(len(snap["directives"]), 2)  # type: ignore[arg-type]
        self.assertIn("directive", [c["kind"] for c in snap["interventions"]])  # type: ignore[union-attr]
        res = control.run(self.cfg, self.conn, "directive_clear", {"all": True})
        self.assertTrue(res["ok"])
        self.assertEqual(directives.active(self.conn), [])

    def test_abort_leases_stops_heartbeat_and_preserves_attempt_budget(self) -> None:
        self.task("long-slice")
        self.sync()
        got = queue.claim(self.conn, self.cfg, "alpha", "alpha-1", 1, alive=ALIVE)
        self.assertEqual(self.row("long-slice")["attempts"], 1)
        self.assertTrue(queue.heartbeat(self.conn, self.cfg, "long-slice", str(got["token"])))

        directives.set_directive(self.conn, mode="abort_leases", harness="alpha", message="Reassign alpha tasks")
        self.assertFalse(queue.heartbeat(self.conn, self.cfg, "long-slice", str(got["token"])))
        status = queue.finish_author(self.conn, self.cfg, "long-slice", str(got["token"]), ok=False,
                                     reason="stopped by directive", aborted=True)
        self.assertEqual(status, "NEEDS_TAKEOVER")
        row = self.row("long-slice")
        self.assertEqual(row["attempts"], 0)
        self.assertIsNone(row["avoid_harness"])

    def test_cancel_builds_stops_running_shim_compile_and_wrap_up_blocks_new_compiles(self) -> None:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        ready = self.tmp / "started"
        faketool = bin_dir / "faketool"
        faketool.write_text('#!/bin/sh\necho ok > "$READY_FILE"\nsleep 30\n', encoding="utf-8")
        faketool.chmod(0o755)
        tools = {"faketool": {"subcommands": ["build"], "flags": [], "value_options": [], "warm": False}}
        qenv = {
            **os.environ,
            **compile_queue.env_for(self.cfg.state, 1, tools, f"{bin_dir}{os.pathsep}{os.environ['PATH']}"),
            "READY_FILE": str(ready),
            "WORKBOARD_HARNESS": "alpha",
            "WORKBOARD_ROLE": "author",
            "WORKBOARD_TASK": "slice-1",
        }
        qenv.pop(compile_queue.HELD_ENV, None)
        proc = subprocess.Popen(["faketool", "build"], cwd=self.repo, env=qenv, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: proc.stderr.close() if proc.stderr else None)
        self.addCleanup(lambda: (proc.kill(), proc.wait()) if proc.poll() is None else None)
        deadline = time.time() + 8.0
        while time.time() < deadline and not ready.exists():
            time.sleep(0.05)
        self.assertTrue(ready.exists())

        # Unrelated harness does not stop alpha's compile
        miss = directives.set_directive(self.conn, mode="cancel_builds", harness="beta", message="Stop beta")
        self.assertEqual(miss["signalled_builds"], [])
        self.assertIsNone(proc.poll())

        # Matching cancel_builds signals the tracked compile immediately
        hit = directives.set_directive(self.conn, mode="cancel_builds", harness="alpha", message="Free RAM now")
        self.assertEqual(len(hit["signalled_builds"]), 1)  # type: ignore[arg-type]
        self.assertNotEqual(proc.wait(timeout=8.0), 0)

        # Subsequent compile exits 1 immediately with the directive on stderr
        refused = subprocess.run(["faketool", "build"], cwd=self.repo, env=qenv, capture_output=True, text=True)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("Free RAM now", refused.stderr)

    def test_unchanged_rejected_commit_cannot_return_to_review_and_repeated_land_failures_raise_a_card(self) -> None:
        self.task("strict", body="FAKE:reject-once")
        self.sync()
        self.work("alpha")  # attempt 1 -> AWAITING_REVIEW
        first_sha = self.row("strict")["candidate_sha"]
        self.work("beta")   # rejects -> NEEDS_TAKEOVER + review_rejections recorded
        self.assertEqual(self.status("strict"), "NEEDS_TAKEOVER")
        self.assertTrue(queue.is_rejected_sha(self.conn, "strict", first_sha))

        # Simulate a takeover agent that exits 0 cleanly without creating a new commit
        wt = gitops.ensure_worktree(self.cfg, "strict", "workboard/strict")
        self.assertEqual(gitops.head(wt), first_sha)
        (self.cfg.tasks / "strict.md").write_text("---\nid: strict\ntitle: Title strict\n---\nFAKE:nothing\n")
        self.sync()
        self.work("beta")
        self.assertEqual(self.status("strict"), "NEEDS_TAKEOVER")
        self.assertIn("already rejected", self.row("strict")["reason"])

        # Repeated identical land_failed events with no subsequent landing raise landing_blocked
        with store.write(self.conn):
            store.event(self.conn, "system", "land_failed", "strict", "rebase conflict on: src/app.py", 100.0)
            store.event(self.conn, "system", "land_failed", "strict", "rebase conflict on: src/app.py", 200.0)
        cards = interventions.fleet_cards(self.conn)
        self.assertIn("landing_blocked", [c["kind"] for c in cards])

        # CLI smoke check for directive subcommands
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli.main(["--root", str(self.repo), "directive", "set", "--mode", "wrap_up",
                                       "--message", "Finishing for today"]), 0)
            self.assertEqual(cli.main(["--root", str(self.repo), "directive", "list"]), 0)
            self.assertEqual(cli.main(["--root", str(self.repo), "directive", "clear", "--all"]), 0)
        self.assertIn("Finishing for today", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
