# Changelog

## Unreleased

- `workboard finish-only on|off`: claims hand out reviews, takeovers and landing, and take over audit jobs that already ran, but never start a `READY` task or a never-run audit job; such a claim returns `finish_only`. A task claimed by name still starts.
- `workboard landing-hold on|off`: while held, the landing step returns `landing_hold_all` without touching the queue or git, so main stays at its commit (for example while a release workflow builds from it and checks its commit is still main's tip at publish). `workboard land` reports the hold. Both switches show in status, the dashboard and as interventions.
- Foreground builds: every author, review and audit prompt tells the agent to run each build and test in the foreground and never end its turn waiting for a background notification. Harnesses that launch Claude Code run with `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` unless their `env` sets it.
- `workboard directive set|list|clear`: live operator directives stored in `workboard.db` (`instruction`, `pause_claims`, `wrap_up`, `cancel_builds`, `abort_leases`), scoped by harness, role (`author`, `review`, `audit`, `interactive`) and task pattern, with optional TTLs. Compile shims surface instructions and wrap-up/cancel refusals mid-turn on stderr and track active compile PIDs so `cancel_builds` stops running compiles immediately; `abort_leases` ends matching leases on the next heartbeat without spending an attempt.
- `workboard move-archive [--apply]`: the write-and-verify counterpart to `move-check`. Snapshots dirty worktrees on a scratch Git index without touching their working files or index, pushes unique local refs and stashes to `refs/archive/<date>/` without forcing, and confirms every object ID on the remote.
- `[compile.tools.<tool>] warm = true`: optional per-slot warm checkouts under `.workboard/compile/worktrees/slot-<n>`. Snapshots the caller's tree through a scratch index, checks it out in a fixed path with slot affinity, and preserves unchanged file timestamps so incremental compiler caches stay warm across task worktrees.
- Rejected-commit guard and stalled-landing intervention: a takeover author cannot send the exact commit SHA a reviewer already rejected back to review unchanged, and repeated identical `land_failed` events with no subsequent landing raise a `CRITICAL` `landing_blocked` intervention card.

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
