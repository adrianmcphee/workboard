"""Audits: claimable audit jobs, atomic findings, one fix per root cause, and fixes proven by a later audit."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from workboard import audits, builds, queue, tasks
from tests.support import ProjectCase, sh

PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)


class AuditCase(ProjectCase):
    def land(self, task_id: str, body: str = "Do the thing.") -> None:
        """Author with alpha, review and land with beta."""
        self.task(task_id, body=body)
        self.sync()
        self.work("alpha")
        self.work("beta")
        self.assertEqual(self.status(task_id), "DONE")

    def checklist(self, check_id: str, body: str, **fields: object) -> None:
        folder = self.cfg.audit_checklists
        folder.mkdir(parents=True, exist_ok=True)
        head = "\n".join(f"{k}: {v}" for k, v in {"id": check_id, "title": f"Check {check_id}", **fields}.items())
        (folder / f"{check_id}.md").write_text(f"---\n{head}\n---\n{body}\n")

    def make_build(self) -> dict[str, object]:
        record = builds.run(self.cfg, self.conn)
        self.assertEqual(record["result"], "built")
        return record

    def finding(self, finding_id: str):
        return self.conn.execute("SELECT * FROM findings WHERE finding_id=?", (finding_id,)).fetchone()

    def push_commit(self, path: str, message: str) -> str:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{message}\n")
        sh(self.repo, "git", "pull", "--rebase", "origin", "main")
        sh(self.repo, "git", "add", "--", path)
        sh(self.repo, "git", "commit", "-m", message)
        sh(self.repo, "git", "push", "origin", "main")
        return sh(self.repo, "git", "rev-parse", "HEAD")


class AuditFlow(AuditCase):
    def test_a_finding_becomes_one_fix_task_lands_with_its_test_and_is_verified_by_the_next_audit(self) -> None:
        self.land("export", body="Export works. FAKE:audit-finding=export")
        self.make_build()
        opened = audits.open_audit(self.cfg, self.conn)
        self.assertTrue(opened["opened"])
        self.assertEqual(opened["jobs"], {"slice": 1, "triage": 1})
        self.assertEqual(opened["scope"]["slices"], ["export"])  # type: ignore[index]

        self.work("beta")  # the slice audit, by the harness that did not author it
        self.assertEqual(self.finding("F-0001")["state"], "open")
        self.assertEqual(self.finding("F-0001")["task_id"], "export")
        self.work("alpha")  # triage: one root cause, one fix task
        fix = self.finding("F-0001")["fix_task"]
        self.assertEqual(fix, "fix-rc-0001")
        self.assertEqual((self.status(fix), self.row(fix)["priority"]), ("READY", audits.SEVERITY_PRIORITY["P1"]))
        self.assertIn("Fixes: F-0001", Path(self.row(fix)["brief_path"]).read_text())

        self.work("alpha")  # author the fix with its regression test and trailer
        self.work("beta")   # review and land; landing reconciles the trailer
        found = self.finding("F-0001")
        self.assertEqual(found["state"], "fixed")
        self.assertEqual(json.loads(found["regression_json"]), ["tests/fix-rc-0001.txt"])
        self.assertEqual(found["fixed_sha"], self.row(fix)["landed_sha"])

        self.make_build()
        second = audits.open_audit(self.cfg, self.conn)
        self.assertEqual(second["jobs"], {"verify": 1, "triage": 1})  # the fix itself is not re-audited as a slice
        self.work("beta")
        self.assertEqual(self.finding("F-0001")["state"], "verified")
        summary = audits.report(self.cfg, self.conn, str(second["audit_id"]))
        self.assertEqual((summary["status"], summary["verified_here"], summary["findings_new"]), ("CLOSED", 1, 0))
        self.assertEqual(summary["never_audited"], [])
        report = audits.audit_dir(self.cfg, str(second["audit_id"])) / "report.md"
        self.assertIn("Fixes verified | 1", report.read_text())

    def test_an_agent_that_changes_the_audit_checkout_has_its_change_saved_undone_and_the_job_requeued(self) -> None:
        self.land("first")
        self.cfg.audit_per_slice = False
        self.checklist("smoke", "Look around. FAKE:audit-touch")
        self.make_build()
        opened = audits.open_audit(self.cfg, self.conn)
        self.work("alpha")
        job = self.conn.execute("SELECT * FROM audit_jobs WHERE kind='checklist'").fetchone()
        self.assertEqual((job["status"], job["avoid_harness"]), ("READY", "alpha"))
        self.assertIn("changed the audit checkout", job["reason"])
        worktree = Path(str(opened["worktree"]))
        self.assertEqual(sh(worktree, "git", "status", "--porcelain"), "")
        self.assertEqual(sh(worktree, "git", "rev-parse", "HEAD"), opened["sha"])
        patches = list(Path(job["evidence_dir"]).glob("checkout-changes-*.patch"))
        self.assertEqual(len(patches), 1)
        self.assertIn("left behind", patches[0].read_text())

    def test_open_is_idempotent_per_commit_and_refuses_what_has_not_landed(self) -> None:
        self.land("first")
        record = self.make_build()
        first = audits.open_audit(self.cfg, self.conn)
        again = audits.open_audit(self.cfg, self.conn, sha=str(record["sha"]))
        self.assertEqual((again["opened"], again["audit_id"]), (False, first["audit_id"]))
        (self.repo / "local.txt").write_text("not pushed\n")
        sh(self.repo, "git", "add", "--", "local.txt")
        sh(self.repo, "git", "commit", "-m", "local only")
        with self.assertRaises(audits.AuditError):
            audits.open_audit(self.cfg, self.conn, sha=sh(self.repo, "git", "rev-parse", "HEAD"))


class AuditClaims(AuditCase):
    def setUp(self) -> None:
        super().setUp()
        self.land("first")
        self.make_build()
        self.cfg.audit_per_slice = False

    def test_concurrent_filings_from_separate_processes_get_unique_ids_and_merge_repeats(self) -> None:
        self.checklist("journey", "Walk through it.")
        audits.open_audit(self.cfg, self.conn)
        job = audits.claim(self.conn, self.cfg, "alpha", "w1", os.getpid())
        self.assertTrue(job["claimed"])

        def spawn(title: str) -> subprocess.Popen:
            return subprocess.Popen(
                [sys.executable, "-m", "workboard", "--root", str(self.repo), "audit", "file", "--job", str(job["job_id"]),
                 "--task", "first", "--severity", "P2", "--title", title, "--repro", "r", "--expected", "e"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": PACKAGE_PARENT})

        procs = [spawn("Same defect") for _ in range(4)] + [spawn(f"Other defect {n}") for n in range(4)]
        outputs = [p.communicate(timeout=60) for p in procs]
        self.assertEqual([p.returncode for p in procs], [0] * 8, outputs)
        ids = [r[0] for r in self.conn.execute("SELECT finding_id FROM findings ORDER BY seq")]
        self.assertEqual(ids, [f"F-{n:04d}" for n in range(1, 6)])
        repeated = {out.split()[0] for out, _ in outputs[:4]}
        self.assertEqual(len(repeated), 1)
        sightings = self.conn.execute("SELECT COUNT(*) FROM finding_notes WHERE finding_id=? AND kind='sighting'",
                                      (repeated.pop(),)).fetchone()[0]
        self.assertEqual(sightings, 4)

    def test_exclusive_resources_author_avoidance_and_urgent_fixes_shape_who_claims_what(self) -> None:
        self.checklist("bench-a", "Measure A.", resource="quiet-machine")
        self.checklist("bench-b", "Measure B.", resource="quiet-machine")
        self.checklist("tour", "Tour it.")
        audits.open_audit(self.cfg, self.conn)
        first = audits.claim(self.conn, self.cfg, "alpha", "w1", os.getpid())
        second = audits.claim(self.conn, self.cfg, "beta", "w2", os.getpid())
        third = audits.claim(self.conn, self.cfg, "beta", "w3", os.getpid())
        held = [j for j in (first, second, third) if j.get("claimed") and j["resource"] == "quiet-machine"]
        self.assertEqual(len(held), 1)
        self.assertEqual(third, {"claimed": False, "reason": "no_audit_work"})

        tasks.add(self.conn, self.cfg, "fix-now", "Urgent", "Fix it.", priority=audits.SEVERITY_PRIORITY["P0"])
        audits.finish(self.conn, self.cfg, str(first["job_id"]), str(first["token"]), ok=False, reason="try again")
        self.assertEqual(audits.claim(self.conn, self.cfg, "beta", "w4", os.getpid())["reason"], "urgent_fix_first")

    def test_a_drained_fleet_claims_no_audit_job_until_it_resumes(self) -> None:
        self.checklist("tour", "Tour it.")
        audits.open_audit(self.cfg, self.conn)
        queue.set_draining(self.conn, True, "test")
        self.assertEqual(audits.claim(self.conn, self.cfg, "beta", "w1", os.getpid()), {"claimed": False, "reason": "draining"})
        queue.set_draining(self.conn, False, "test")
        self.assertTrue(audits.claim(self.conn, self.cfg, "beta", "w1", os.getpid())["claimed"])

    def test_finish_only_takes_over_a_started_audit_job_and_starts_no_new_one(self) -> None:
        self.checklist("tour", "Tour it.")
        self.checklist("walk", "Walk it.")
        audits.open_audit(self.cfg, self.conn)
        first = audits.claim(self.conn, self.cfg, "alpha", "w1", os.getpid())
        audits.finish(self.conn, self.cfg, str(first["job_id"]), str(first["token"]), ok=False, reason="try again")
        queue.set_finish_only(self.conn, True, "test")
        again = audits.claim(self.conn, self.cfg, "beta", "w2", os.getpid())
        self.assertEqual(again["job_id"], first["job_id"])
        self.assertEqual(audits.claim(self.conn, self.cfg, "beta", "w3", os.getpid()), {"claimed": False, "reason": "finish_only"})
        queue.set_finish_only(self.conn, False, "test")
        self.assertTrue(audits.claim(self.conn, self.cfg, "beta", "w3", os.getpid())["claimed"])

    def test_slice_jobs_prefer_a_harness_other_than_the_author(self) -> None:
        self.cfg.audit_per_slice = True
        self.checklist("tour", "Tour it.")
        audits.open_audit(self.cfg, self.conn)
        mine = audits.claim(self.conn, self.cfg, "alpha", "w1", os.getpid())  # alpha authored `first`
        self.assertEqual(mine["kind"], "checklist")
        other = audits.claim(self.conn, self.cfg, "beta", "w2", os.getpid())
        self.assertEqual(other["kind"], "slice")

    def test_a_lost_lease_or_an_incomplete_result_returns_the_job_to_another_harness(self) -> None:
        self.checklist("tour", "Tour it. FAKE:audit-silent")
        audits.open_audit(self.cfg, self.conn)
        self.work("alpha")
        job = self.conn.execute("SELECT * FROM audit_jobs WHERE kind='checklist'").fetchone()
        self.assertEqual((job["status"], job["avoid_harness"]), ("READY", "alpha"))
        claimed = audits.claim(self.conn, self.cfg, "beta", "w2", os.getpid())
        self.assertTrue(claimed["claimed"])
        audits.claim(self.conn, self.cfg, "alpha", "w3", os.getpid(), alive=lambda pid: False)
        job = self.conn.execute("SELECT * FROM audit_jobs WHERE kind='checklist'").fetchone()
        self.assertEqual((job["reason"], job["avoid_harness"]), ("worker_gone", "beta"))
        self.assertFalse(audits.heartbeat(self.conn, self.cfg, str(claimed["job_id"]), str(claimed["token"])))


class AuditFixes(AuditCase):
    def setUp(self) -> None:
        super().setUp()
        self.land("first")
        self.cfg.audit_per_slice = False
        self.make_build()
        audits.open_audit(self.cfg, self.conn)
        self.fid = str(audits.file_finding(self.cfg, self.conn, task_id="first", severity="P1", title="Totals are wrong",
                                           repro="Add two rows", expected="The sum")["finding_id"])

    def test_a_trailer_closes_a_finding_only_on_a_commit_that_touches_a_regression_test(self) -> None:
        grouped = audits.triage_group(self.cfg, self.conn, [self.fid], title="Rounding")
        self.assertTrue(grouped["queued"])
        again = audits.triage_group(self.cfg, self.conn,
                                    [audits.file_finding(self.cfg, self.conn, task_id="first", severity="P2", title="Totals drift",
                                                         repro="r", expected="e")["finding_id"]],  # type: ignore[list-item]
                                    root_cause=str(grouped["root_cause"]))
        self.assertEqual((again["fix_task"], again["queued"]), (grouped["fix_task"], False))
        self.push_commit("docs/note.md", f"note the rounding issue\n\nFixes: {self.fid}")
        self.assertEqual(audits.reconcile(self.cfg, self.conn)["refused"], [self.fid])
        self.assertEqual(self.finding(self.fid)["state"], "fix-scheduled")
        sha = self.push_commit("tests/test_totals.txt", f"round totals\n\nFixes: {self.fid}")
        self.assertEqual(audits.reconcile(self.cfg, self.conn)["fixed"], [self.fid])
        self.assertEqual(audits.reconcile(self.cfg, self.conn), {"fixed": [], "refused": []})
        self.assertEqual((self.finding(self.fid)["state"], self.finding(self.fid)["fixed_sha"]), ("fixed", sha))

    def test_a_reopened_fix_counts_against_the_audit_and_queues_the_next_fix_generation(self) -> None:
        grouped = audits.triage_group(self.cfg, self.conn, [self.fid], title="Rounding")
        queue.cancel(self.conn, str(grouped["fix_task"]))
        self.push_commit("tests/test_totals.txt", f"round totals\n\nFixes: {self.fid}")
        audits.reconcile(self.cfg, self.conn)
        (self.repo / "FAKE-verify-reopen").write_text("yes\n")
        later = audits.open_audit(self.cfg, self.conn, sha=sh(self.repo, "git", "rev-parse", "origin/main"))
        self.assertEqual(later["scope"]["verify"], [self.fid])  # type: ignore[index]
        self.work("beta")
        self.assertEqual(self.finding(self.fid)["state"], "reopened")
        regrouped = audits.triage_group(self.cfg, self.conn, [self.fid], root_cause=str(grouped["root_cause"]))
        self.assertEqual(regrouped["fix_task"], f"{grouped['fix_task']}-r2")
        self.assertEqual(audits.report(self.cfg, self.conn, str(later["audit_id"]))["reopened_here"], 1)

    def test_only_a_person_decides_wont_fix_and_references_must_exist_in_the_build(self) -> None:
        self.checklist("tour", "Tour it.")
        self.cfg.audit_refs_command = "printf 'REQ-1\\nREQ-2\\n'"
        self.push_commit("tests/keep.txt", "move main")
        audit = audits.open_audit(self.cfg, self.conn, sha=sh(self.repo, "git", "rev-parse", "origin/main"))
        with self.assertRaises(audits.AuditError):
            audits.file_finding(self.cfg, self.conn, task_id="first", severity="P2", title="x", repro="r", expected="e",
                                refs=["REQ-9"], audit_id=str(audit["audit_id"]))
        with self.assertRaises(audits.AuditError):
            audits.file_finding(self.cfg, self.conn, task_id="nobody", severity="P2", title="x", repro="r", expected="e")
        audits.file_finding(self.cfg, self.conn, task_id="first", severity="P2", title="x", repro="r", expected="e",
                            refs=["REQ-1"], audit_id=str(audit["audit_id"]))
        audits.mark(self.conn, self.fid, "wont-fix-proposed", "cosmetic")
        job = self.conn.execute("SELECT job_id FROM audit_jobs WHERE kind='checklist' AND audit_id=?",
                                (audit["audit_id"],)).fetchone()[0]
        audits.claim(self.conn, self.cfg, "alpha", "w1", os.getpid(), job_id=job)
        with self.assertRaises(audits.AuditError):
            audits.mark(self.conn, self.fid, "wont-fix", "agents cannot decide this", job_id=job)
        self.assertEqual(audits.mark(self.conn, self.fid, "wont-fix", "agreed")["state"], "wont-fix")


if __name__ == "__main__":
    unittest.main()
