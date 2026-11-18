"""Queue rules that need no git: who may claim what, and what happens to a lost lease."""
from __future__ import annotations

from workboard import queue, store, tasks
from tests.support import ProjectCase

ALIVE = lambda _pid: True  # noqa: E731


class ClaimRules(ProjectCase):
    def claim(self, harness: str, **kw: object) -> dict[str, object]:
        return queue.claim(self.conn, self.cfg, harness, f"{harness}-w", 1, alive=ALIVE, **kw)  # type: ignore[arg-type]

    def to_review(self, task_id: str, author: str = "alpha") -> None:
        got = self.claim(author, task_id=task_id)
        queue.finish_author(self.conn, self.cfg, task_id, str(got["token"]), ok=True, candidate_sha="c" * 40)

    def test_author_does_not_review_its_own_work_while_another_harness_is_running(self) -> None:
        self.task("one")
        self.sync()
        self.to_review("one")
        with store.write(self.conn):
            self.conn.execute("INSERT INTO workers VALUES ('beta-1','beta',1,1,0)")
        self.assertEqual(self.claim("alpha")["reason"], "no_ready_work")
        got = self.claim("beta")
        self.assertEqual((got["role"], got["task_id"]), ("review", "one"))

    def test_same_harness_reviews_when_no_other_is_running_or_after_the_wait(self) -> None:
        self.task("one")
        self.sync()
        self.to_review("one")
        self.assertEqual(self.claim("alpha")["role"], "review")  # nobody else is live
        self.task("two")
        self.sync()
        self.to_review("two")
        with store.write(self.conn):
            self.conn.execute("INSERT INTO workers VALUES ('beta-1','beta',1,1,0)")
        row = self.row("two")
        late = row["updated_at"] + self.cfg.review_fallback_s + 1
        self.assertEqual(self.claim("alpha", now=row["updated_at"] + 1)["reason"], "no_ready_work")
        self.assertEqual(self.claim("alpha", now=late)["task_id"], "two")

    def test_review_is_handed_out_before_new_authoring(self) -> None:
        self.task("old")
        self.task("fresh", priority=900)
        self.sync()
        self.to_review("old")
        self.assertEqual(self.claim("beta")["task_id"], "old")

    def test_fresh_work_is_steered_away_from_paths_already_in_flight(self) -> None:
        self.task("busy", paths="[src/audio]", priority=500)
        self.task("clash", paths="[src/audio/mixer.rs]", priority=400)
        self.task("apart", paths="[docs]", priority=100)
        self.sync()
        self.assertEqual(self.claim("alpha")["task_id"], "busy")
        self.assertEqual(self.claim("beta")["task_id"], "apart")
        self.assertEqual(self.claim("beta")["task_id"], "clash")  # overlap delays work, it never refuses it

    def test_takeover_comes_before_fresh_work_and_prefers_a_different_harness(self) -> None:
        self.task("half")
        self.task("new", priority=900)
        self.sync()
        got = self.claim("alpha", task_id="half")
        queue.finish_author(self.conn, self.cfg, "half", str(got["token"]), ok=False, reason="no commits")
        self.assertEqual(self.claim("alpha")["task_id"], "new")
        self.assertEqual(self.claim("beta")["task_id"], "half")

    def test_capacity_and_usage_verdict_refuse_authoring_but_not_an_explicit_claim(self) -> None:
        for name in ("a", "b", "c", "d", "e"):
            self.task(name)
        self.sync()
        refused = self.claim("alpha", usage_verdict={"allow": False, "reason": "subscription_reserve"})
        self.assertEqual(refused["reason"], "subscription_reserve")
        self.assertTrue(self.claim("alpha", task_id="a", usage_verdict={"allow": False, "reason": "x"})["claimed"])
        for _ in range(self.cfg.max_coding - 1):
            self.assertTrue(self.claim("alpha")["claimed"])
        self.assertEqual(self.claim("alpha")["reason"], "capacity")

    def test_dependencies_gate_readiness_and_cancelled_tasks_stay_cancelled(self) -> None:
        self.task("base")
        self.task("top", depends_on="[base]")
        self.sync()
        self.assertEqual(self.status("top"), "BLOCKED")
        queue.cancel(self.conn, "base")
        queue.refresh(self.conn, self.cfg)
        self.assertEqual((self.status("base"), self.status("top")), ("CANCELLED", "BLOCKED"))
        self.assertEqual(self.claim("alpha")["reason"], "no_ready_work")
        self.assertEqual(queue.requeue(self.conn, "base"), "BLOCKED")

    def test_board_complete_only_when_nothing_is_open(self) -> None:
        self.task("only")
        self.sync()
        got = self.claim("alpha")
        queue.finish_author(self.conn, self.cfg, "only", str(got["token"]), ok=True, candidate_sha="c" * 40)
        self.assertNotEqual(self.claim("zeta").get("reason"), "board_complete")
        queue.landed(self.conn, self.cfg, "only", "d" * 40)
        self.assertEqual(self.claim("alpha")["reason"], "board_complete")


class Leases(ProjectCase):
    def test_lost_heartbeat_dead_worker_and_deadline_all_release_the_task(self) -> None:
        for name in ("beat", "dead", "late"):
            self.task(name)
        self.sync()
        tokens = {n: queue.claim(self.conn, self.cfg, "alpha", "w", 100 + i, task_id=n, now=1000.0, alive=ALIVE)
                  for i, n in enumerate(("beat", "dead", "late"))}
        self.assertTrue(queue.heartbeat(self.conn, self.cfg, "late", str(tokens["late"]["token"]), now=1050.0))
        queue.refresh(self.conn, self.cfg, now=1010.0, alive=lambda pid: pid != 101)
        self.assertEqual((self.status("beat"), self.status("dead")), ("CLAIMED", "NEEDS_TAKEOVER"))
        self.assertEqual(self.row("dead")["reason"], "worker_gone")
        queue.refresh(self.conn, self.cfg, now=1000.0 + self.cfg.heartbeat_ttl_s + 1, alive=ALIVE)
        self.assertEqual(self.row("beat")["reason"], "heartbeat_lost")
        self.assertEqual(self.status("late"), "CLAIMED")
        deadline = 1000.0 + self.cfg.default_lease_min * 60 + 1
        self.assertFalse(queue.heartbeat(self.conn, self.cfg, "late", str(tokens["late"]["token"]), now=deadline))
        self.assertIsNone(queue.finish_author(self.conn, self.cfg, "beat", str(tokens["beat"]["token"]), ok=True))

    def test_cancel_takes_the_lease_away_from_a_running_worker(self) -> None:
        self.task("gone")
        self.sync()
        got = queue.claim(self.conn, self.cfg, "alpha", "w", 1, alive=ALIVE)
        queue.cancel(self.conn, "gone")
        self.assertFalse(queue.heartbeat(self.conn, self.cfg, "gone", str(got["token"])))
        self.assertEqual(self.status("gone"), "CANCELLED")


class TaskFiles(ProjectCase):
    def test_sync_reports_bad_files_and_never_resets_progress(self) -> None:
        self.task("good")
        self.task("needs", depends_on="[missing]")
        (self.cfg.tasks / "broken.md").write_text("no header here\n")
        result = self.sync()
        self.assertEqual(result["added"], 1)
        self.assertEqual(len(result["errors"]), 2)
        queue.claim(self.conn, self.cfg, "alpha", "w", 1, alive=ALIVE)
        self.task("good", priority=7)
        self.sync()
        row = self.row("good")
        self.assertEqual((row["status"], row["priority"]), ("CLAIMED", 7))

    def test_added_task_keeps_its_brief_outside_the_repository(self) -> None:
        tasks.add(self.conn, self.cfg, "adhoc", "An ad hoc slice", "Body `$(touch nope)`", priority=5)
        row = self.row("adhoc")
        self.assertEqual(row["source"], "queue")
        self.assertTrue(row["brief_path"].startswith(str(self.cfg.state)))
        with self.assertRaises(tasks.TaskError):
            tasks.add(self.conn, self.cfg, "Bad Id", "x", "y")


if __name__ == "__main__":
    import unittest
    unittest.main()
