"""Build the text an agent receives. Task values are data: nothing here is evaluated or interpolated twice."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sqlite3

from . import train, usage
from .config import Config


def brief_body(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        _, sep, body = text[4:].partition("\n---\n")
        if sep:
            return body.strip()
    return text.strip()


def verdict_path(cfg: Config, task_id: str) -> Path:
    return cfg.handoff(task_id) / "review-verdict.json"


def takeover_path(cfg: Config, task_id: str) -> Path:
    return cfg.handoff(task_id) / "takeover.md"


def workboard_context(cfg: Config, conn: sqlite3.Connection | None, role: str, harness: str, lease_min: int,
                      now: datetime | None = None, usage_snap: dict[str, object] | None = None) -> str:
    """The build this slice is going for and the subscription budget it spends. Empty when neither is known."""
    now = now or datetime.now()
    lines: list[str] = []
    try:
        if cfg.build_schedule:
            author = min(float(lease_min), train.author_minutes(conn, harness) if conn else train.DEFAULT_AUTHOR_MIN)
            status = "CLAIMED" if role == "author" else "REVIEWING"
            ready = train.expected_on_main(cfg, status, now.timestamp(), now, author)
            target = train.target_for(cfg, ready, now)
            if target:
                cutoff = target - timedelta(minutes=cfg.build_cutoff_min)
                later = next((b for b in train.upcoming(cfg, now, 8) if b > target), None)
                lines.append(f"- **Build you are going for:** {target.strftime('%a %d %b %H:%M')} "
                             f"(build id `{train.build_id(target)}`), made from whatever is on `{cfg.main}` at that minute.")
                lines.append(f"- **Land cutoff:** {cutoff.strftime('%H:%M')}, "
                             f"{max(0, round((cutoff - now).total_seconds() / 60.0))} minutes from now.")
                if later:
                    lines.append(f"- Missing the cutoff moves the slice to the build at {later.strftime('%a %H:%M')}. "
                                 "That is fine. Never cut scope, skip a guard or hand over half-finished work to make a build.")
    except Exception:
        pass
    try:
        snap = usage_snap or usage.snapshot(cfg)
        entry = usage.entry_for(snap, harness)
        win = usage.binding(entry)
        if entry and win and not entry.get("stale"):
            text = (f"- **Subscription budget ({harness}, {win['label']}):** {win['remaining_pct']:.0f}% left, "
                    f"resets {usage.when(win['resets_at'])}.")
            if win.get("exhausts_at"):
                text += f" At the current burn it runs out {usage.when(win['exhausts_at'])}, before the reset."
            lines.append(text)
            if entry.get("pace") in ("over", "reserve"):
                lines.append("- This subscription is ahead of its pace. Start from the files the brief names and avoid "
                             "broad exploration; do not economise on guards or tests."
                             if role == "author" else
                             "- This subscription is ahead of its pace. Keep to the diff and the code it calls; "
                             "review rigour is not what to save on.")
    except Exception:
        pass
    return ("\n## Workboard context\n" + "\n".join(lines) + "\n") if lines else ""


def render(cfg: Config, claim: dict[str, object], harness: str, worktree: Path,
           conn: sqlite3.Connection | None = None) -> str:
    role, task_id = str(claim["role"]), str(claim["task_id"])
    lease = int(claim["lease_min"])  # type: ignore[arg-type]
    handoff = cfg.handoff(task_id)
    parts = [f"<!-- workboard role={role} task={task_id} handoff={handoff} verdict={verdict_path(cfg, task_id)} -->",
             f"# Task `{task_id}`: {claim['title']}", "", brief_body(Path(str(claim["brief_path"])))]
    guards = "\n".join(f"  - `{g}`" for g in cfg.guards) or "  - (none configured)"
    if role == "author":
        takeover = takeover_path(cfg, task_id)
        if takeover.is_file():
            parts += ["", "## Earlier attempts on this task",
                      "Work already exists in the worktree. Read these notes, keep what is sound and finish it.", "",
                      takeover.read_text(encoding="utf-8").strip()]
        parts += ["", "## Working agreement", f"""- Work only in `{worktree}`, on branch `{claim['branch']}`. It was opened from `{cfg.remote_main}`.
- You have **{lease} minutes**. Commit early; a lease that ends with no commit loses the work in progress.
- Deliver the whole outcome the brief describes, with the smallest test that fails without it. No placeholders.
- Commit in small linear commits. Stage explicit paths. Never create a merge commit, never push, never switch branch.
- Finish with `git status` completely clean and every guard passing:
{guards}
- Write a short note on what you did and what remains to `{handoff / 'notes.md'}`.
- Then stop. An independent reviewer checks the branch, and Workboard lands it on `{cfg.main}`."""]
    else:
        parts += ["", "## Review assignment", f"""You are the independent reviewer of the branch in `{worktree}`. You have **{lease} minutes**.
- Read `git log --stat {cfg.remote_main}..HEAD` and `git diff {cfg.remote_main}...HEAD` against the brief above and the project rules below.
- Check that the outcome is complete, that the test would fail without the change, and that nothing unrelated changed.
- You may run these guards:
{guards}
- Do **not** modify, commit or stash anything in the worktree.
- Write your verdict as JSON to `{verdict_path(cfg, task_id)}`:
  `{{"passed": true, "summary": "..."}}` or `{{"passed": false, "defects": ["file:line what is wrong and why", "..."]}}`
- Fail the branch for a real defect you can point to. Do not fail it for style you would have chosen differently."""]
    parts.append(workboard_context(cfg, conn, role, harness, lease))
    for rules in cfg.rules:
        if rules.is_file():
            parts += [f"## Project rules: `{rules.name}`", "", rules.read_text(encoding="utf-8").strip(), ""]
    return "\n".join(parts).rstrip() + "\n"
