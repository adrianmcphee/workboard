# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private vulnerability reporting on this repository ("Report a vulnerability" under the Security tab). You will get a reply within a week.

## What Workboard trusts and what it does not

Understand these before running it.

- **Agents run unattended with full permissions in their worktree.** The harness commands in the examples use each CLI's flag for running without approval prompts. An agent can run any command your user account can. Run Workboard on a machine and account that hold nothing an agent should not reach, and keep code you are not allowed to expose to these agents out of it.
- **Task text is data.** Briefs are passed to agents as text and are never evaluated by Workboard. A malicious brief can still instruct an agent, so only queue tasks you would hand to the agent yourself.
- **The dashboard is local only.** It binds to `127.0.0.1`. Every write needs a token that only the served page carries, sent in a custom header, plus a loopback `Host` header, so another website open in your browser cannot trigger an action. Do not expose the port beyond the machine, including through a tunnel or reverse proxy.
- **No credentials are read.** Usage readers read only the local caches that Claude Code and Codex already write, or a command you configure. Workboard never calls a provider API.
- **Landing pushes to your main branch** with your git credentials, after the guards pass. Protect anything the guards should catch with a guard.

## Supported versions

Only the latest release receives fixes while Workboard is below version 1.0.
