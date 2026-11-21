# AGENTS.md

Context for coding agents working with this repository. Read all of it before acting. [README.md](README.md) is the user guide; this file tells you what you need to act correctly.

## What Workboard is

Workboard runs coding agents from several subscriptions (Claude Code, Codex and any other agent CLI that runs unattended) on one git repository at once, as one delivery pipeline. It is a work queue plus a control room:

- It hands tasks to agent CLIs under time-limited leases, each in its own git worktree.
- An agent from a different harness reviews each result.
- Approved work is rebased, checked and pushed to the main branch in small linear commits.
- Builds run on a schedule, and a person accepts or rejects each slice.
- It reads each subscription's remaining allowance and paces work across them.

It is a Python 3.11+ command-line tool, `workboard`, with no dependencies outside the standard library. It runs on macOS and Linux, on one machine.

## Which job are you doing?

Work out which of these applies, then read that section.

| You were asked to | Read |
|---|---|
| Set up or run Workboard on some other project | [Using Workboard on a project](#using-workboard-on-a-project) |
| Change Workboard itself: fix a bug, add a feature, update the docs | [Working on Workboard](#working-on-workboard) |
| Complete a task handed out by Workboard (your prompt starts with `<!-- workboard role=... -->`) | Nothing here. Follow the "Working agreement", "Review assignment" or "Audit assignment" in your prompt; it is complete. |

## Vocabulary

| Term | Meaning |
|---|---|
| **Harness** | One configured agent CLI, usually one per subscription, in a `[harness.<name>]` block. |
| **Slot** | One worker process for a harness. `slots = 2` runs two agents of that harness at once. |
| **Task / slice** | One outcome, defined by a Markdown file in `workboard/tasks/`. A slice is a task once it is on its way to a build. |
| **Drain** | The fleet-wide pause (`workboard drain`): reviews and landing continue, no new authoring or audit job is handed out until `workboard resume`. |
| **Lease** | A time-limited claim on a task, kept alive by a heartbeat. A lost lease returns the task to the queue. |
| **Takeover** | A task whose last attempt ended without a result. The next author continues in the same worktree with notes. |
| **Compile queue** | Optional `[compile]` slots that heavy build commands (agents', guards', landings', builds') wait for, through PATH shims. |
| **Guard** | A fast check command (lint, focused tests) that must pass before review and again after rebase. |
| **Landing** | Rebase onto main, run guards, fast-forward push under a short lock, retire the worktree. |
| **Build / train** | A scheduled full build of main. Each records which slices it contains. A slice "rides" the first build whose cutoff it can make. |
| **Sign-off** | A person's accept or reject verdict on one slice of a build. A rejection creates a top-priority fix task. |
| **Pace** | For each subscription: `under`, `on`, `over` or `reserve`, from its remaining allowance and reset time. |
| **Intervention** | A decision Workboard leaves to a person, shown with the command that takes it. |
| **Audit** | A repeatable re-examination of one built commit of main by several agents. |
| **Audit job** | One claimable piece of an audit (`slice`, `checklist`, `verify`, `triage`), leased like a task but with no branch. |
| **Finding** | A defect recorded during an audit, with a permanent ID (`F-0001`) and the task that owns it. |
| **Root cause** | Findings that share one cause (`RC-0001`). Each has at most one fix task in flight. |

## The delivery cycle

1. **Plan:** a task file states one outcome. It becomes `READY` once its dependencies are `DONE`.
2. **Author:** a worker claims it (`CLAIMED`) and runs the agent in `.worktrees/<task>/` on branch `workboard/<task>`. The attempt counts only if the worktree is clean, the branch has commits, history is linear and every guard passes (`AWAITING_REVIEW`).
3. **Review:** an agent from another harness reads the branch and writes `{"passed": true|false, ...}` to the task's verdict file (`REVIEWING`).
4. **Land:** rebase, guard, locked fast-forward push (`LANDING`, then `DONE`).
5. **Build:** `workboard build` at the scheduled times records a manifest.
6. **Sign-off:** a person runs `workboard signoff accept|reject <task>`.
7. **Audit:** `workboard audit open` creates audit jobs for a build; findings become root causes with one fix task each, and a later audit verifies each fix.

Failures (quota stop, crash, timeout, failed review, landing conflict) send the task to `NEEDS_TAKEOVER`. After `max_attempts` it goes to `NEEDS_TRIAGE` and waits for a person.

## Using Workboard on a project

### Set it up

```sh
pip install -e /path/to/workboard            # or: PYTHONPATH=/path/to/workboard python3 -m workboard ...
cd /path/to/project                          # a git repository with a remote and a main branch
workboard init                               # writes workboard.toml and workboard/tasks/example-task.md
printf '.workboard/\n.worktrees/\n' >> .gitignore
```

Then edit `workboard.toml`:

1. **`[guards] commands`:** the project's fast checks, each under about a minute. Never the full test suite or the release build; those belong in `[build] command`.
2. **One `[harness.<name>]` block per agent CLI or subscription.** Uncomment the Claude Code and Codex examples or add your own. Each needs `command`, `prompt` (`stdin`, `arg` or `file`), `slots`, `usage` (`claude-code`, `codex`, `command` or `none`) and `quota_patterns`. Configure at least two harnesses from different vendors if you can; cross-review depends on it.
3. **`[project] rules`:** the target project's own `AGENTS.md` or equivalent, so every agent gets the project's rules.
4. **`[build]`:** `schedule` (for example `["12:30", "20:30"]`) and `command`. Then add `workboard build` to cron or launchd at those times.

### Write tasks

One file per outcome in `workboard/tasks/`:

```markdown
---
id: export-csv
title: A user can export the report as CSV
priority: 100
paths: [src/report, tests/report]
depends_on: []
---
## Outcome
One sentence a user or engineer could check.

## Acceptance
- An observable result that is either true or false.
- The smallest test that fails without the change.

## Notes
What the agent cannot learn from the repository: the decision behind it, the edge case, what was tried before.
```

A good task fits in one lease (60 minutes by default), touches few paths, and has acceptance criteria a reviewer can check without asking anyone. Split anything larger. Use `depends_on` for order, not for grouping.

### Run it

```sh
workboard sync          # load task files; run again after editing them
workboard status        # tasks, subscription pace, the next build, interventions
workboard up            # start the configured workers in the background
workboard dashboard     # control room at http://127.0.0.1:8787
```

To check that it is working: `workboard board` lists every task, `workboard show <task>` shows one task's history, `workboard events` prints the event log, and logs are in `.workboard/logs/`.

### Rules when operating Workboard

- Change state only through `workboard` commands. Never edit `.workboard/workboard.db`, and never move a task by hand.
- Never delete a worktree under `.worktrees/` yourself, and never push to main outside Workboard's landing.
- Never stop agents by process name (`pkill`, `killall`). Use `workboard down`, or `workboard down --now`.
- Decisions listed by `workboard interventions` belong to a person unless they told you to take them.
- Harness commands run agents unattended with full permissions in their worktree. Do not configure them on a machine or account that holds anything an agent should not touch, and say so if you are asked to.

## Working on Workboard

### Layout

```
workboard/            the package; the `workboard` command is workboard.cli:main
  cli.py              argument parsing and the commands; thin, delegates to the modules below
  config.py           loads workboard.toml into Config and Harness dataclasses
  template.py         the files `workboard init` writes; update it with every config change
  tasks.py            parses task Markdown files and syncs them into the database
  store.py            SQLite schema, connections, one-transaction writes, the event log
  queue.py            the task state machine: claim, heartbeat, finish, land, requeue, cancel
  worker.py           the worker loop: claim, run the agent, judge the evidence, land
  harness.py          runs one agent CLI: own process group, heartbeat, deadline, quota detection
  prompt.py           renders the text each agent receives
  gitops.py           worktrees, guards, the landing lock, rebase and push
  compile_queue.py    the compile queue: PATH shims, slot locks, the event log and its view; runs on its own as the shim
  usage.py            reads subscription allowance from local caches; burn rate, pace, claim verdict
  train.py            the build schedule: which build each slice is going for
  builds.py           runs builds, writes manifests, records sign-off verdicts
  audits.py           audits of a build: audit jobs and their leases, findings, triage, fix reconciliation, reports
  interventions.py    the decisions shown to a person, with the command that takes each
  movecheck.py        `workboard move-check`: what would be lost moving the project to another machine (read-only)
  control.py          starts, resizes and stops workers; the closed list of dashboard operations
  dashboard.py        the local control-room page and its HTTP handler
tests/                unittest suite: real git repositories and a fake agent CLI
docs/                 operating-model.md (the git rules), multi-machine.md (design), vision.md
```

### Invariants

Keep all of these. A change that needs to break one needs the user's agreement first.

1. **Standard library only.** No third-party dependency, including for tests. Python 3.11 or newer.
2. **Every task status change goes through `queue.py`,** inside one `store.write()` transaction, and records exactly one event with its actor. The only other writer is `tasks.py`, which inserts new tasks as `BLOCKED` and refreshes task definitions without touching progress.
3. **Task text is data.** Briefs and titles are passed to agents as text. Never `eval` them, never format them twice, never build a shell command from them.
4. **Processes are stopped by their own process group** (`os.killpg` on the group the harness started). Never by name or pattern.
5. **The landing lock covers only fetch, check and push.** Guards and rebases run outside it.
6. **A worktree with uncommitted changes is never removed automatically.**
7. **Usage readers only read local files or the configured command.** They never call a provider, never read a credential, and never raise into a caller. Unknown or stale usage means "no opinion".
8. **Workboard never lowers total throughput on its own,** except stopping a harness at its reserve. Trade-offs become interventions.
9. **A quota stop is not an attempt.** It cools the harness down and requeues the task without using an attempt.
10. **The dashboard binds to loopback.** Every write needs the page token in its custom header and a loopback `Host`. Only the operations listed in `control.py` exist.
11. **A reviewer that writes no verdict has not rejected anything.** The review is offered to another harness.
12. **Every audit job and finding change goes through `audits.py`,** inside one `store.write()` transaction, and leaves an event. Fix tasks are created with `tasks.insert` in the same transaction as their root cause.
13. **A finding is fixed only by evidence and verified only by a later audit.** `fixed` needs a landed trailer on a commit that touches a regression path; `verified` needs a verify job in a later audit. Only a person turns a proposed `wont-fix` into a decision.
14. **The audit checkout stays at its commit.** An agent's change to it is saved as a patch in the job's evidence and undone before the job is judged.
15. **The compile queue only decides when a command starts.** The shim waits for a slot and then `exec`s the real tool with the same arguments, so process IDs, signals and exit codes are the tool's own. A logging failure never affects the build, and the release watcher holds neither the slot nor the build's pipes.

### Making a change

1. Read the module that owns the behaviour (see the layout) and its tests.
2. Write or change a test first. `tests/test_flow.py` drives the real worker loop end to end, `tests/test_queue.py` covers the state machine, and `tests/test_control_room.py` covers usage, prompts, builds, interventions and the dashboard, and `tests/test_audit.py` covers audits end to end. `tests/fake_agent.py` stands in for an agent CLI: it reads the `<!-- workboard role=... -->` header of its prompt and acts on it.
3. Run the suite: `python3 -m unittest discover -s tests -t .` It needs git and no network, and takes about 30 seconds.
4. When behaviour or configuration changes, update in the same change: [README.md](README.md), `template.py` for any `workboard.toml` key, this file if a rule or the layout changes, and [CHANGELOG.md](CHANGELOG.md).

### Style

- Type hints everywhere, `from __future__ import annotations` at the top of each module.
- Every module starts with a docstring stating what it owns. Functions are small and named for what they do.
- User-facing text is plain English, with no jargon a newcomer would have to look up.
- Errors a user can fix are reported as a message and an exit code, not a traceback.
