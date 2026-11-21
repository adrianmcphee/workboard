"""`workboard move-check`: everything that would be lost moving the project to another machine."""
from __future__ import annotations

import contextlib
import io

from workboard import cli, movecheck, queue
from tests.support import ProjectCase, sh


class MoveCheck(ProjectCase):
    def report(self) -> dict[str, object]:
        return movecheck.check(self.cfg, self.conn)

    def commit(self, path: str, text: str) -> str:
        (self.repo / path).write_text(text)
        sh(self.repo, "git", "add", "--", path)
        sh(self.repo, "git", "commit", "-m", f"change {path}")
        return sh(self.repo, "git", "rev-parse", "HEAD")

    def test_a_clean_pushed_project_has_nothing_to_lose(self) -> None:
        report = self.report()
        self.assertFalse(report["would_lose"], report)
        self.assertIn(str(self.cfg.db), [p["path"] for p in report["state_paths"]])  # type: ignore[union-attr]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--root", str(self.repo), "move-check"]), 0)

    def test_unique_commits_stashes_dirty_worktrees_and_in_flight_tasks_are_reported(self) -> None:
        sh(self.repo, "git", "checkout", "-q", "-b", "idea")
        self.commit("idea.txt", "only here\n")
        sh(self.repo, "git", "checkout", "-q", "main")
        (self.repo / "README.md").write_text("edited but not committed\n")
        sh(self.repo, "git", "stash", "push", "-q", "-m", "half done")
        (self.repo / "scratch.txt").write_text("untracked\n")
        self.task("flying")
        self.sync()
        queue.claim(self.conn, self.cfg, "alpha", "w", 1, alive=lambda _p: True)
        report = self.report()
        self.assertTrue(report["would_lose"])
        refs = {r["ref"]: r for r in report["refs"]}  # type: ignore[union-attr]
        self.assertEqual(refs["refs/heads/idea"]["unique"], 1)
        self.assertEqual(len(report["stashes"]), 1)  # type: ignore[arg-type]
        dirty = [w for w in report["worktrees"] if w["dirty"]]  # type: ignore[union-attr]
        self.assertEqual([w["path"] for w in dirty], [str(self.repo)])
        self.assertEqual(report["tasks_in_flight"], {"CLAIMED": 1})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--root", str(self.repo), "move-check"]), 1)
        self.assertIn("refs/heads/idea", out.getvalue())

    def test_a_branch_already_landed_by_patch_is_not_counted_as_unique(self) -> None:
        sh(self.repo, "git", "checkout", "-q", "-b", "landed")
        sha = self.commit("landed.txt", "same change\n")
        sh(self.repo, "git", "checkout", "-q", "main")
        self.commit("other.txt", "main moved on\n")  # a different parent, so the pick is a new commit, not the same one
        sh(self.repo, "git", "cherry-pick", sha)
        sh(self.repo, "git", "push", "-q", "origin", "main")
        sh(self.repo, "git", "fetch", "-q", "origin")
        refs = {r["ref"]: r for r in self.report()["refs"]}  # type: ignore[union-attr]
        self.assertEqual((refs["refs/heads/landed"]["ahead"], refs["refs/heads/landed"]["unique"]), (1, 0))
        self.assertFalse(self.report()["would_lose"])

    def test_move_archive_snapshots_dirty_worktrees_pushes_unique_refs_and_confirms_remote(self) -> None:
        sh(self.repo, "git", "checkout", "-q", "-b", "unlanded")
        branch_sha = self.commit("unlanded.txt", "keep me\n")
        sh(self.repo, "git", "checkout", "-q", "main")
        (self.repo / "README.md").write_text("dirty edit\n")
        sh(self.repo, "git", "stash", "push", "-q", "-m", "saved stash")
        (self.repo / "README.md").write_text("uncommitted takeover edit\n")
        (self.repo / "new-note.txt").write_text("untracked note\n")
        before_status = sh(self.repo, "git", "status", "--porcelain=v1", "--untracked-files=all")

        dry = movecheck.archive(self.cfg, self.conn, apply=False, date="2030-01-15")
        self.assertFalse(dry["ok"])
        self.assertEqual(len(dry["snapshots"]), 1)  # type: ignore[arg-type]

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--root", str(self.repo), "move-archive", "--apply", "--date", "2030-01-15"]), 0)
        self.assertIn("confirmed on the remote", out.getvalue())
        # Dirty worktree files and index were not altered by the scratch-index snapshot
        self.assertEqual(sh(self.repo, "git", "status", "--porcelain=v1", "--untracked-files=all"), before_status)
        self.assertEqual(sh(self.repo, "git", "diff", "--cached"), "")
        remote_refs = sh(self.repo, "git", "ls-remote", "origin", "refs/archive/2030-01-15/*")
        self.assertIn("refs/archive/2030-01-15/heads/unlanded", remote_refs)
        self.assertIn(branch_sha, remote_refs)
        self.assertIn("refs/archive/2030-01-15/stash/0", remote_refs)
        self.assertIn("refs/archive/2030-01-15/recovery/2030-01-15/worktrees/repo", remote_refs)
