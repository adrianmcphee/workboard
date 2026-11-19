# Changelog

## Unreleased

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
