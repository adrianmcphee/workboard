"""End to end: real git, real worker loop, a fake agent CLI."""
from __future__ import annotations

import unittest

from workboard import queue
from tests.support import ProjectCase, sh


class AuthorReviewLand(ProjectCase):
    def test_slice_is_authored_reviewed_by_another_harness_landed_and_retired(self) -> None:
        self.task("first", paths="[src/a]")
        self.task("second", depends_on="[first]")
        self.assertEqual(self.sync()["errors"], [])
        self.assertEqual((self.status("first"), self.status("second")), ("READY", "BLOCKED"))

        self.work("alpha")
        self.assertEqual(self.status("first"), "AWAITING_REVIEW")
        self.assertTrue((self.cfg.worktree("first") / "first.txt").is_file())
        self.assertNotIn("first.txt", self.remote_files())

        self.work("beta")
        row = self.row("first")
        self.assertEqual((row["status"], row["author_harness"]), ("DONE", "alpha"))
        self.assertIn("first.txt", self.remote_files())
        self.assertEqual(sh(self.tmp, "git", "--git-dir", str(self.remote), "rev-parse", "main"), row["landed_sha"])
        self.assertFalse(self.cfg.worktree("first").exists())
        self.assertEqual(sh(self.repo, "git", "branch", "--list", "workboard/first"), "")
        self.assertEqual(self.status("second"), "READY")
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM events WHERE task_id='first' ORDER BY id")]
        self.assertEqual(kinds, ["task_added", "ready", "claimed_author", "author_finished", "claimed_review",
                                 "review_pass", "landed"])

    def test_rejected_review_returns_to_an_author_with_the_defects_then_lands(self) -> None:
        self.task("fix", body="FAKE:reject-once")
        self.sync()
        self.work("alpha")
        self.work("beta")
        self.assertEqual((self.status("fix"), self.row("fix")["reason"]), ("NEEDS_TAKEOVER", "review_rejected"))
        self.work("alpha")
        prompt = (self.cfg.handoff("fix") / "prompt-author.md").read_text()
        self.assertIn("a.txt:1 wrong on first look", prompt)
        self.assertIn("Earlier attempts on this task", prompt)
        self.work("beta")
        self.assertEqual((self.status("fix"), self.row("fix")["attempts"]), ("DONE", 2))
        self.assertEqual(sh(self.tmp, "git", "--git-dir", str(self.remote), "show", "main:fix.txt"), "fix attempt 2")

    def test_uncommitted_work_is_not_sent_to_review(self) -> None:
        self.task("messy", body="FAKE:dirty")
        self.sync()
        self.work("alpha")
        row = self.row("messy")
        self.assertEqual((row["status"], row["avoid_harness"]), ("NEEDS_TAKEOVER", "alpha"))
        self.assertIn("uncommitted", row["reason"])
        self.work("beta")  # another harness takes it over in the same worktree and finishes it
        self.assertEqual(self.status("messy"), "AWAITING_REVIEW")

    def test_attempt_limit_parks_the_task_for_a_human(self) -> None:
        self.task("empty", body="FAKE:nothing")
        self.sync()
        for _ in range(self.max_attempts):
            self.work("alpha")
        self.assertEqual(self.status("empty"), "NEEDS_TRIAGE")
        self.assertFalse(queue.claim(self.conn, self.cfg, "alpha", "w", 1, alive=lambda _p: True)["claimed"])
        self.assertEqual(queue.requeue(self.conn, "empty"), "NEEDS_TAKEOVER")

    def test_quota_stop_cools_the_harness_down_without_spending_an_attempt(self) -> None:
        self.task("capped", body="FAKE:quota")
        self.sync()
        self.work("alpha")
        self.assertEqual((self.status("capped"), self.row("capped")["attempts"]), ("NEEDS_TAKEOVER", 0))
        refused = queue.claim(self.conn, self.cfg, "alpha", "w", 1, alive=lambda _p: True)
        self.assertEqual(refused["reason"], "harness_cooldown")

    def test_reviewer_that_writes_no_verdict_is_not_a_rejection(self) -> None:
        self.task("quiet", body="FAKE:silent-review")
        self.sync()
        self.work("alpha")
        self.work("beta")
        row = self.row("quiet")
        self.assertEqual((row["status"], row["avoid_harness"], row["attempts"]), ("AWAITING_REVIEW", "beta", 1))


class Landing(ProjectCase):
    def test_conflict_with_main_goes_back_with_the_file_named_and_main_untouched(self) -> None:
        self.task("clash", body="FAKE:file=shared.txt")
        self.sync()
        self.work("alpha")
        (self.repo / "shared.txt").write_text("someone else got here first\n")
        sh(self.repo, "git", "add", "--", "shared.txt")
        sh(self.repo, "git", "commit", "-m", "competing change")
        sh(self.repo, "git", "push", "origin", "main")
        before = sh(self.tmp, "git", "--git-dir", str(self.remote), "rev-parse", "main")
        self.work("beta")
        row = self.row("clash")
        self.assertEqual(row["status"], "NEEDS_TAKEOVER")
        self.assertIn("rebase conflict on: shared.txt", row["reason"])
        self.assertEqual(sh(self.tmp, "git", "--git-dir", str(self.remote), "rev-parse", "main"), before)
        self.assertIn("Landing failed", (self.cfg.handoff("clash") / "takeover.md").read_text())

    def test_branch_is_rebased_over_unrelated_main_movement(self) -> None:
        self.task("calm")
        self.sync()
        self.work("alpha")
        (self.repo / "other.txt").write_text("unrelated\n")
        sh(self.repo, "git", "add", "--", "other.txt")
        sh(self.repo, "git", "commit", "-m", "unrelated change")
        sh(self.repo, "git", "push", "origin", "main")
        self.work("beta")
        self.assertEqual(self.status("calm"), "DONE")
        self.assertLessEqual({"calm.txt", "other.txt"}, self.remote_files())
        self.assertEqual(sh(self.tmp, "git", "--git-dir", str(self.remote), "rev-list", "--merges", "main"), "")


class GuardsAndNoReview(ProjectCase):
    review = "false"
    guards = '"test ! -f forbidden.txt"'

    def test_without_review_a_guarded_slice_lands_directly(self) -> None:
        self.task("solo")
        self.sync()
        self.work("alpha")
        self.assertEqual(self.status("solo"), "DONE")

    def test_failing_guard_blocks_the_slice(self) -> None:
        self.task("bad", body="FAKE:file=forbidden.txt")
        self.sync()
        self.work("alpha")
        row = self.row("bad")
        self.assertEqual(row["status"], "NEEDS_TAKEOVER")
        self.assertIn("guard failed: test ! -f forbidden.txt", row["reason"])
        self.assertNotIn("forbidden.txt", self.remote_files())


if __name__ == "__main__":
    unittest.main()
