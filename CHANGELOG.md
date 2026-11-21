# Changelog

## Unreleased

- `workboard move-check`: a read-only report of everything that would be lost moving the project to another machine (refs and stashes on no remote, with landed-by-patch branches told apart from unique work, dirty worktrees, tasks in flight, and the state to copy), exiting 1 when anything unique would be. The README describes the move: drain, wait, check, stop, copy, clone.
- Prompts state the build rules once and the same way for author and reviewer: build and test only what the task touches, one heavy command at a time, acceptance commands from the brief once near the end, and a `## Commands run` record that the reviewer judges with the guards. How the author built is never a reason to fail.
- `[compile]`: an optional compile queue. Heavy invocations of the listed tools, from agents, guards, landings and builds, take one of a fixed number of OS-lock slots through PATH shims, line up first come first served, and log wait, hold and memory to `.workboard/compile/events.jsonl`. Status and the dashboard show the queue; an intervention flags long waits while memory is free.
- `workboard drain` and `workboard resume`: a fleet-wide pause. Every claim path (authoring and audit jobs) honours it, while reviews and landing finish the work in flight. Status, the dashboard data and a card show it.
- Audits: `workboard audit open` re-examines a build with claimable audit jobs (one per newly landed slice, one per checklist in `workboard/audits/`, verify jobs for fixes, and triage).
- Findings get permanent IDs in one transaction; repeats become sightings. Triage groups them into root causes with exactly one fix task each.
- A landed `Fixes: F-NNNN` trailer on a commit that touches a regression test path marks a finding fixed; only a later audit verifies it, and a reopen queues the next fix.
- Workers claim branch reviews, then audit jobs, then authoring; P0 fix tasks go first. Status, the dashboard data and interventions include audits.

## 0.1.0

First public release.

- Task queue with leases, heartbeats, takeovers and an attempt limit.
- One git worktree and branch per task; landing as rebase, guard, then a locked fast-forward push.
- Cross-harness review with a JSON verdict.
- Scheduled builds with manifests and per-slice human sign-off; a rejection queues a fix task.
- Subscription usage and pace for Claude Code, Codex and custom commands, feeding claims and prompts.
- Quota detection with cooldown that does not spend an attempt.
- Local control-room dashboard and `workboard status` with potential interventions.
