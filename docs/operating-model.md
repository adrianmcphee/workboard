# Operating model: parallel delivery on one trunk

Many agents can work on one repository at the same time only if the way changes reach the main branch stays fast and never waits on a person or on another agent. This document states the rules that achieve that, and why each one exists. Workboard enforces most of them; the rest are conventions your project keeps.

## The loop

Author in an isolated worktree, land small linear commits straight onto main in seconds, and verify the combined result once per build.

## Rules

### 1. One integration branch

Main is the only branch work lands on. There are no long-lived feature branches and no pull request queue. A task branch exists only between a claim and a landing.

*Why:* every extra integration point is a place where finished work waits.

### 2. One worktree per task

Each task has its own checkout and branch, opened from the current head of main. Nobody writes in another task's worktree.

*Why:* agents sharing a working directory overwrite each other and cannot tell whose uncommitted change is whose.

### 3. Paths are a notice board, not a lock

A task declares where it expects to write. The queue uses that to steer fresh work away from paths already in flight. An overlap delays a claim at most; it never refuses one.

*Why:* file locks turn every shared file into a queue. Most concurrent edits to the same file merge cleanly, and the ones that do not are cheaper to resolve than to prevent.

### 4. History is linear

Task branches contain no merge commits. They are brought up to date with a rebase.

*Why:* a linear branch replays onto a moved main one commit at a time, so a conflict is small and attributable.

### 5. Landing is rebase, guard, then a locked push

A branch is rebased onto main and the guards run on the result, all outside any lock. The lock covers only fetch, check and push, and the push names the exact commit that was guarded. If main moved while the guards ran, the lock is released and the loop starts again.

*Why:* a lock held across a test run makes landing as slow as the slowest check. Holding it only for the push keeps the critical section to a second or two.

### 6. Guards are fast; the full build runs once per wave

Guard commands are the checks cheap enough to run on every landing: formatting, static analysis, focused tests. The expensive build and the full test suite run once on a schedule against the combined head of main.

*Why:* running the full build in every worktree multiplies its cost by the number of agents and saturates the machine. Running it once per wave costs the same no matter how many slices are in the wave.

### 7. A conflict goes back to an author with the files named

When a rebase conflicts, landing aborts it, records which files conflicted, and returns the task for another attempt in the same worktree. The landing does not retry until the branch has changed.

*Why:* retrying an unchanged branch against an unchanged main can only fail again.

### 8. Shared registries are generated, not edited

A file that every slice must append to, such as a changelog, a status ledger, a module list or a test manifest, is the main source of avoidable conflicts. Have each slice add its own small fragment file, and generate the shared file from the fragments.

*Why:* two slices that each add one line to the same list conflict; two slices that each add one file do not.

### 9. Only physical things are exclusive

A device, a signing identity, an installed application or a benchmark that needs a quiet machine can be used by one task at a time. Hold such a resource only for the operation that needs it. Source files are never in this category.

### 10. Processes are stopped by identity, never by name

A worker stops the agent it started through that agent's own process group. Nothing is killed by matching a command name.

*Why:* a pattern kill takes out every sibling agent running the same tool.

### 11. Done means on main and retired

A task is complete when its commit is reachable from the remote main branch and its worktree and branch are gone. A worktree with uncommitted changes is never removed automatically.

### 12. Verification has a clock

Builds happen at fixed times. A slice is told which build it is going for and when the cutoff is, and a slice that misses simply rides the next one. Each build records exactly which slices it contains, and a human gives each a verdict.

*Why:* a fixed rhythm turns "is main good?" into a question with a scheduled answer, and gives review by a human a bounded batch to look at.

## What a human decides

The fleet handles claiming, retrying, reviewing and landing on its own. It leaves these to a person:

- Whether a slice in a build is acceptable.
- Whether to spend a subscription fast or make it last until its reset.
- What to do with a task that has used all its attempts.
- Whether a brief that keeps failing should be rewritten, split or dropped.

## Recovering an interrupted task

Nothing is lost when a worker dies. Its lease expires, the task returns to the queue, and the worktree stays as it was. The next author receives notes describing why the previous attempt ended, the commits on the branch and any uncommitted files, and continues from there.

For a worktree in a state nobody understands: record its status and diff, keep a reference to its tip, and extract what is useful as a fresh small task from current main. Never merge the old branch.

## Scaling limits

The model as implemented assumes one machine: leases are checked against local processes, the landing lock is a local file lock, and worktrees are local directories. Past the capacity of one machine the same rules hold, and the landing step becomes a single integrator that other hosts hand finished branches to. [multi-machine.md](multi-machine.md) is that design.
