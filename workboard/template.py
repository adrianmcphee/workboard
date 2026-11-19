"""Files written by `workboard init`."""

WORKBOARD_TOML = '''# Workboard configuration. Paths are relative to this file.

[project]
name = "{name}"
repo = "."                    # the git checkout agents branch from
remote = "origin"
main = "main"                 # the only integration branch
worktrees = ".worktrees"      # one checkout per task lives here
state = ".workboard"              # queue database, logs, handoffs, build records
tasks = "workboard/tasks"         # one Markdown file per task
rules = []                    # files appended to every prompt, e.g. ["AGENTS.md"]
branch_prefix = "workboard/"

[limits]
max_coding = 4                # tasks being authored at once, across all harnesses
max_attempts = 6              # then a task is parked for a human
default_lease_min = 60        # authoring time per attempt unless the task says otherwise
review_lease_min = 20
max_load = 0                  # refuse new authoring above this 1-minute load; 0 disables
min_free_gib = 10             # pause claims below this much free disk

[review]
required = true               # every slice is checked by a second agent before it lands
cross_harness = true          # prefer a reviewer from a different harness than the author
fallback_after_s = 180        # after this wait, any harness may review

[guards]
# Fast checks every slice must pass before review and again after it is rebased onto main.
commands = []                 # e.g. ["make lint", "make test-fast"]

[land]
command = ""                  # empty: rebase, guard, fast-forward push. Or your own, e.g. "make land"

[build]
schedule = []                 # e.g. ["12:30", "20:30"]; run `workboard build` at these times
cutoff_min = 30               # a slice must be on main this long before a build to ride it
command = ""                  # e.g. "make release-candidate"

[audit]
checklists = "workboard/audits"   # one Markdown checklist per file; each becomes a job in every audit
per_slice = true              # also audit every task that landed since the last audit against its brief
lease_min = 60
prepare = ""                  # run once in the audit checkout when an audit opens, e.g. "make dev-build"
refs_command = ""             # prints valid reference IDs, one per line, for findings' --ref
regression_paths = ["tests/**", "test/**", "spec/**", "**/tests/**", "**/test_*", "**/*_test.*", "**/*.test.*", "**/*.spec.*"]
trailer = "Fixes"             # a landed "Fixes: F-0001" on a commit touching a regression path marks it fixed

[usage]
policy = "enforce"            # enforce | advise | off
reserve_pct = 3               # stop authoring on a subscription with this little left

[dashboard]
host = "127.0.0.1"
port = 8787

# One block per agent CLI. `command` receives the prompt on stdin (prompt = "stdin"), as a final
# argument ("arg"), or as a file named by the placeholder {{prompt_file}} ("file").
# The agent runs unattended inside the task worktree, so it needs a non-interactive mode with
# permission to edit files and run commands. Read your CLI's documentation on what that grants.
#
# [harness.claude]
# command = ["claude", "-p", "--dangerously-skip-permissions"]
# prompt = "stdin"
# slots = 2
# usage = "claude-code"
# quota_patterns = ["usage limit", "rate_limit", "hit your limit"]
#
# [harness.codex]
# command = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "-"]
# prompt = "stdin"
# slots = 2
# usage = "codex"
# quota_patterns = ["usage limit", "insufficient_quota", "429 Too Many"]
'''

EXAMPLE_TASK = '''---
id: example-task
title: Say what a user can do once this lands
priority: 100
paths: [src/example]
depends_on: []
---
## Outcome

One sentence describing the behaviour a user or engineer can observe when this slice is on main.

## Acceptance

- The observable result, stated so that it is either true or false.
- The smallest test that fails without the change.

## Notes

Anything the agent cannot learn from the repository: the decision behind the task, the edge case
that matters, the thing that was tried before and why it was wrong.
'''
