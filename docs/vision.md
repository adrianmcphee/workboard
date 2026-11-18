# Vision

Where Workboard is going, and what it will not try to be. This is direction, not a promise of dates. What exists today is described in the [README](../README.md).

## The idea

Coding agents are now good enough that the limit on one person's output is no longer typing. It is how many agents they can keep usefully busy, how much allowance their subscriptions give them, and how much finished work they can check. Workboard is the work layer for that situation: it decides what each agent works on, checks the result with a second agent from another vendor, gets it onto main, and puts a small number of real decisions in front of a person.

Three layers make up a working setup, and Workboard should own only one of them:

| Layer | Job | Who does it |
|---|---|---|
| **Agents** | Read code, write code, run commands | Claude Code, Codex, OpenCode and other agent CLIs |
| **Terminals and sessions** | Keep agent sessions alive, show them, let a person attach to one | A terminal multiplexer, such as Herdr or tmux |
| **Work** | What to do next, who reviews it, when it lands, which build it rides, how fast to spend each subscription, what needs a person | Workboard |

## Where it goes next

### 1. Work alongside Herdr

[Herdr](https://herdr.dev) is an open-source terminal multiplexer built for coding agents. It keeps agent sessions alive through disconnects, recognises each agent's state (working, blocked waiting for input, done, idle), and since version 0.9 shows local and remote machines in one sidebar. A mobile app connects to its separate herdr-core server. It has a local socket API and command line for workspaces, panes, agents and worktrees.

Herdr and Workboard solve different problems and fit together. Herdr knows what each terminal is doing right now; Workboard knows what the work is, who checks it and where it goes. The plan is a **Herdr runner**: an alternative to today's headless subprocess, selected per harness.

- **Launch in panes.** Each claimed task opens in a Herdr pane, in a workspace for the project, using the task's worktree. A person can attach to any running agent, watch it and type into it, from the desktop or the Herdr mobile app.
- **Use Herdr's agent state.** An agent Herdr reports as `blocked` (waiting for input or approval) becomes a Workboard intervention, with a link to its pane. An agent reported `done` with no verdict or commits is judged by the same evidence rules as today.
- **Keep Workboard's guarantees.** Leases, heartbeats, the evidence gate, cross-review, landing and sign-off stay in Workboard. Herdr provides the terminals; it does not decide whether work lands.
- **Stay optional.** The headless runner remains the default and the one the tests depend on. Herdr's socket API is marked unstable before its 1.0, so the integration lives behind one small adapter that can follow its changes.

### 2. Several machines

Agents that build and test saturate one machine, and each machine can be signed in to different subscriptions. [multi-machine.md](multi-machine.md) is the design: one coordinator owns the queue, landing, builds and the control room; worker hosts run agents and hand code over through `refs/workboard/*` on the git remote. Usage is grouped by account, so one subscription signed in on two hosts is counted once.

With Herdr in place, the coordinator can also use Herdr's machine list to show and attach to agents on every host from one screen.

### 3. A control room on your phone

Most of what a person does in Workboard takes seconds and does not need a keyboard: accept or reject a slice, decide whether to spend a subscription faster, requeue a parked task, answer a blocked agent. The dashboard should become a mobile-first page that does exactly those things, with notifications for the interventions that matter. It must reach the phone safely, never by exposing the local dashboard port: through a private network, an SSH tunnel, or a coordinator that authenticates the person separately from the workers.

For watching or typing into an agent's terminal, the Herdr mobile app already covers it; Workboard links to the right pane instead of building a terminal of its own.

### 4. Any provider, priced honestly

A harness does not have to be a subscription. Agent CLIs can point at pay-per-token providers serving open-weight models, or at a team's own GPUs. Workboard should treat these as first-class: a spend budget paced the way allowances are paced today, and a clear view of what a slice costs on each harness. That lets a person use flat-rate subscriptions first, overflow to a per-token provider when every plan is at its reserve, and see when owning capacity would be cheaper.

### 5. From managing harnesses to being one

Today Workboard drives other people's agent CLIs. As it learns which harness writes work that passes review and sign-off, it can route each task to the harness most likely to land it at the lowest cost. The natural end point is a harness of its own: one entry point that receives a task and decides which agent, model and subscription works on it, while keeping the same delivery cycle around it. That only makes sense once the routing is measured, so it comes last.

## What Workboard will not become

- **An agent.** It does not read or write code itself. Agents do that.
- **A terminal multiplexer.** Herdr and tmux do that well.
- **A hosted service holding your code.** It runs on machines you control, next to your repository.
- **A way around subscription terms.** It paces and reports usage; it does not hide automated use from providers. Check each provider's terms.

## Order of work

1. Open-source release on one machine (this version).
2. The Herdr runner, behind an adapter, with `blocked` agents as interventions.
3. Spend budgets for pay-per-token harnesses.
4. The queue interface and `refs/workboard/*` handoff on one machine, then the coordinator and remote workers ([multi-machine.md](multi-machine.md)).
5. The mobile control room, served by the coordinator.
6. Routing by measured outcomes, and then a harness of its own.
