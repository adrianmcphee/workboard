# Contributing

Thanks for helping. Workboard is small on purpose, so the bar for a change is that it keeps it small and keeps the rules in [AGENTS.md](AGENTS.md#invariants).

## Before you start

- For anything bigger than a fix, open an issue first and describe the problem, not only the change. It saves both of us time if the direction turns out to differ.
- Read [AGENTS.md](AGENTS.md). It is written for coding agents, and it is also the shortest accurate description of how the code fits together and which rules it keeps.

## Making a change

1. Fork and branch from `main`.
2. Add or change a test that fails without your change.
3. Run the suite: `python3 -m unittest discover -s tests -t .` It needs git and no network.
4. Update the README, `workboard/template.py` and the changelog when behaviour or configuration changes.
5. Open a pull request that says what changes for a user and how you tested it.

Contributions written with coding agents are welcome. You are responsible for the change either way: read it, run it, and be ready to explain it.

## Scope

In scope: anything that makes running several agent subscriptions on one repository safer, faster or easier to steer.

Out of scope for now: third-party dependencies, calls to provider APIs from the usage readers, and features that need more than one machine before the coordinator in [docs/multi-machine.md](docs/multi-machine.md) exists.

By contributing you agree that your contribution is licensed under the MIT licence of this project.
