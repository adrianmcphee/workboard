# Design: workers on several machines

Workboard runs every worker for a project on one host. This document is the design for spreading workers across several hosts while keeping one queue, one integrator and one control room.

## Why

- **Compute.** Agents that build and test saturate one machine long before the queue runs out of work.
- **Subscriptions.** Each host can be signed in to different agent accounts, so work can be spread across more allowance than one login holds.
- **Isolation.** An agent with permission to run commands is safer on a machine that holds nothing else.

## Shape

One **coordinator** and any number of **worker hosts**.

```
                      +-------------------------------+
   worker host A ---> |  coordinator                  | ---> git remote (main)
   worker host B ---> |  queue, events, landing,      |
   worker host C ---> |  builds, dashboard            |
                      +-------------------------------+
        ^                                                        ^
        +------------- fetch and push refs/workboard/* --------------+
```

- The coordinator owns the database, lands every branch, runs builds and serves the dashboard.
- A worker host runs agents. It talks to the coordinator over HTTP for queue operations and to the git remote for code.
- Worker hosts never talk to each other.

A single-machine project is the same design with the coordinator and one worker host in one process, which is how the code runs without any of this configured.

## What each single-machine assumption becomes

| Assumption in the code | Where | Replacement |
|---|---|---|
| Workers open the database file | `store.connect` in `worker.py` | Workers call the coordinator; only the coordinator opens the file |
| A dead worker is found by its process ID | `queue.pid_alive` in `_refresh` | Heartbeat expiry alone |
| The reviewer reads the author's worktree | `gitops.ensure_worktree` | The reviewer fetches the task's ref from the remote |
| A takeover continues in the same directory | `worker.note_takeover` | The next author starts from the task's ref; notes travel through the coordinator |
| Landing takes a local file lock | `gitops.land_lock` | Only the coordinator lands, in its own worktrees |
| Handoff notes and verdicts are local files | `cfg.handoff(task)` | Sent with the result and stored by the coordinator |
| Usage is read from this machine's caches | `usage.collect` | Each worker host reports its own readings |
| The dashboard trusts loopback | `dashboard.make_handler` | Unchanged for people; workers authenticate separately |

## The queue seam

`worker.py` calls five queue functions: `claim`, `heartbeat`, `finish_author`, `finish_review` and the landing pair. They move behind one interface with two implementations.

- **`LocalQueue`** calls `queue.py` directly on a connection. It is the single-machine path.
- **`RemoteQueue`** makes the same calls over HTTP.

The state machine in `queue.py` does not change. The coordinator's handlers call the same functions `LocalQueue` does, so both paths share every rule and every test.

## Coordinator API

JSON over HTTP, one endpoint per queue operation. Every request carries the worker's host name and a bearer secret.

| Request | Body | Result |
|---|---|---|
| `POST /v1/claim` | harness, worker id, live slots, usage reading | A claim, or a reason there is none |
| `POST /v1/heartbeat` | task, token | Whether the lease still stands |
| `POST /v1/author` | task, token, outcome, pushed commit, reason, notes | New status |
| `POST /v1/review` | task, token, verdict, defects | New status |
| `GET /v1/task/<id>/prompt` | role | The rendered prompt, including takeover notes |
| `POST /v1/host` | host name, harnesses, slots, usage readings | Target slots per harness for this host |

A claim response carries everything the worker needs: branch, the ref to start from, lease length and the prompt. The worker keeps no state between tasks.

Requests are idempotent on the lease token. A worker that repeats `POST /v1/author` after a lost reply gets the same answer, because a finished lease is recognised and not applied twice.

## Code handoff through git

Work moves between hosts through the git remote, in a ref namespace that is not a branch:

```
refs/workboard/<task-id>
```

- **Author.** Starts from `refs/workboard/<task-id>` if it exists, otherwise from main. Pushes to that ref after every commit, and reports the pushed commit when it finishes.
- **Reviewer.** Fetches the ref and checks out the exact commit named in the claim. A reviewer never pushes.
- **Coordinator.** Fetches the ref into its own worktree, then rebases, guards and pushes to main exactly as `gitops.land` does on one machine. After landing it deletes the ref.

Refs outside `refs/heads/` do not appear as branches, do not trigger branch protections or continuous integration, and are not fetched by an ordinary clone.

The coordinator accepts a result only when the reported commit is the tip of the task's ref on the remote. That replaces the local checks "the worktree is clean" and "the branch has commits" with one fact that can be verified from anywhere.

## Leases without process IDs

A lease is alive while its heartbeat is fresh and its deadline has not passed. The process ID check is an optimisation that notices a dead worker within one refresh instead of one heartbeat interval; without it a dead worker's task returns to the queue after `heartbeat_ttl_s`.

Clocks do not need to agree. The coordinator stamps every lease with its own clock, and a worker only ever asks whether its lease still stands.

## Takeovers

On one machine a takeover continues in the directory the last attempt left, including uncommitted files. Across machines the next author may be on another host, so a takeover starts from the commits on the task's ref.

Consequences:

- Uncommitted work at the moment a lease ends is lost. Pushing after every commit bounds the loss to the work since the last commit.
- Takeover notes list the commits on the ref and the reason the attempt ended. They no longer list uncommitted files.
- Guards run on the author's host before it reports, and again on the coordinator after the rebase. The second run is the one that decides.

## Slots and usage per host

Slots are set per host and harness. The dashboard shows each host with its harnesses, live workers and target, and the worker control acts on one host at a time.

A host reports the usage readings for the accounts signed in there. Two hosts signed in to the same account report the same allowance; readings are grouped by an account label in the harness configuration so one subscription is counted once. Pace and the claim verdict are computed per account, not per host.

## Security

The coordinator is the only component that listens beyond loopback, and only when told to.

- **Off by default.** With no `[coordinator]` section the API is not served at all.
- **Explicit address and secret.** Serving it needs a bind address and a secret of at least 32 bytes; workers present it as a bearer token.
- **Separate from the dashboard token.** The worker secret cannot perform dashboard actions, and the page token cannot claim work.
- **Transport.** The API speaks plain HTTP and is meant to run over a private network, a VPN or an SSH tunnel. It refuses to bind a public address without TLS termination configured in front of it.
- **Prompts are data.** A worker host receives task text and project rules. A compromised worker can submit bad work to review but cannot land: only the coordinator pushes to main, after its own guards.

Worker hosts need push access to `refs/workboard/*` on the remote and no access to main. Where the git host supports it, enforce that with a ref rule.

## Failure modes

| Failure | Effect | Recovery |
|---|---|---|
| A worker host disappears | Its heartbeats stop | Leases expire; tasks return as takeovers from their refs |
| The coordinator restarts | Workers get connection errors | Workers keep their agents running and retry; leases survive in the database |
| The coordinator is down longer than a lease | Heartbeats cannot be renewed | On return, expired leases are released; a late result for a released lease is refused and the work stays on its ref for the next author |
| The git remote is unreachable | Authors cannot push | The attempt ends as incomplete with that reason |
| Two workers hold the same task | Cannot happen through the queue | The lease token decides; the second result is refused |

## Configuration

Coordinator:

```toml
[coordinator]
bind = "10.0.0.5:8790"
secret_file = "~/.config/workboard/secret"
```

Worker host:

```toml
[coordinator]
url = "http://10.0.0.5:8790"
secret_file = "~/.config/workboard/secret"
host = "builder-2"

[harness.codex]
command = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "-"]
slots = 3
account = "team-pro"
```

A worker host needs a clone of the repository, the agent CLIs, and the guard commands' toolchain. It does not need the tasks folder or the build toolchain.

## Order of work

1. Put the queue behind the `LocalQueue` interface with no behaviour change.
2. Move code handoff to `refs/workboard/*` on one machine, so the reviewer and the landing step already read from the remote.
3. Add the coordinator API and `RemoteQueue`.
4. Add per-host slots and per-account usage to the dashboard.

Step 2 changes single-machine behaviour slightly (a takeover no longer sees uncommitted files) and is the one to evaluate on its own before going further.

## Tests

The existing suite runs every case through `LocalQueue`. The multi-machine suite reuses the same cases with a coordinator in one thread and two workers that each have their own clone of the remote, plus cases for a lost reply, a coordinator restart mid-task and a worker that pushes one commit and reports another.
