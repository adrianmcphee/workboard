# Changelog

## 0.1.0

First public release.

- Task queue with leases, heartbeats, takeovers and an attempt limit.
- One git worktree and branch per task; landing as rebase, guard, then a locked fast-forward push.
- Cross-harness review with a JSON verdict.
- Scheduled builds with manifests and per-slice human sign-off; a rejection queues a fix task.
- Subscription usage and pace for Claude Code, Codex and custom commands, feeding claims and prompts.
- Quota detection with cooldown that does not spend an attempt.
- Local control-room dashboard and `workboard status` with potential interventions.
