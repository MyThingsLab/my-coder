# my-coder

[![CI](https://github.com/MyThingsLab/my-coder/actions/workflows/ci.yml/badge.svg)](https://github.com/MyThingsLab/my-coder/actions/workflows/ci.yml) [![codecov](https://codecov.io/gh/MyThingsLab/my-coder/branch/main/graph/badge.svg)](https://codecov.io/gh/MyThingsLab/my-coder) ![Python](https://img.shields.io/badge/python-3.11%2B-blue) [![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

The MyThingsLab fleet's **worker**: takes one picked issue and closes it as a
pull request — reads the target repo, makes the smallest change with tests,
runs the suite and linter, commits, and opens the PR.

## The exception, and why it is scoped to one repo

Every other `My[X]` tool makes exactly one tools-*disabled* Engine call —
judgment only, never a side effect — and does everything else
deterministically. **my-coder is the fleet's one deliberate exception.**

Closing an arbitrary issue cannot be that shape. It needs an open-ended,
multi-turn, tools-*enabled* session: read arbitrary files, edit them, run the
suite, decide when it is done. So my-coder's session is not routed through
`mythings.engine.Engine` at all — it is my-coder's own seam, invoked directly
against the `claude` (or `gemini`) CLI with a real tool allowlist.

The design point is that the exception is **contained**. Everything around
the session — outcome classification, PR-readiness checks, the policy gate on
the push, ledger writes, the blocker protocol — is ordinary deterministic
code. One repo in the fleet runs an open-ended agent; the other seventeen
keep the narrow-seam discipline, and this README is where you come to find
out that is true.

## What bounds the session

The session is the least predictable thing the fleet does, so the limits are
explicit rather than implied:

- **Sandbox.** It runs inside a `mythings.isolation.Workspace` git worktree.
  It may edit files and commit; it never pushes.
- **Spend and time.** `--max-budget-usd` (default 5), `--max-turns`
  (default 60), `--session-timeout-s` (default 1800). `--max-attempts` can
  retry from the checkpointed branch, capped by `--max-total-budget-usd`.
- **Side effects.** my-coder performs the single push + `gh pr create`
  itself, wrapped as an `Action` through `Policy.evaluate` — with
  `--guarded`, that becomes a real ASK-channel approval a human answers.
- **One PR per issue**, on head `mycoder/<repo>-<issue-number>`. It never
  touches a repo other than the one its issue names.
- **Dependency installs are allowed** — install subcommands only. A worktree
  carries only tracked files, so without this the target's suite is
  unrunnable and the worker would commit code it could not verify. This does
  mean a session executes code from the network inside its worktree; see
  [`CLAUDE.md`](CLAUDE.md) before pointing it at an untrusted dependency list.
- **Out-of-fleet targets are enforced, not just discouraged.** With a
  `--repo` outside `MyThingsLab`, the session loses `gh` entirely and its
  prompt drops both cross-repo escapes — a repo my-coder was merely pointed
  at must not be able to file issues in the org or halt the fleet.

## Draft vs. ready is a correctness decision

The PR opens **ready for review** when `--run-tests` ran the target's suite in
the worktree and it passed, and as a **draft** otherwise.

This is not cosmetic. CI skips required checks while a PR is a draft, so a PR
born as a draft can never show a green check — and the fleet's old promotion
gate read that skip as a pass and promoted on it
([`my-fleet#32`](https://github.com/MyThingsLab/my-fleet/issues/32)). Opening
ready is what makes CI run at all.

**my-coder never merges.** It opens the PR and stops. Whether that PR then
merges is decided elsewhere — by a human, or by `myfleet.accept`, which
requires that the required checks actually report `pass`, that the PR is not
a draft, that `main` is branch-protected, and that the diff stays inside its
issue's scope.

## Outcomes

`success` requires both a real commit *and* an open PR. Anything less is
named rather than rounded up:

| Outcome | Means |
|---|---|
| `success` | Committed and opened a PR. |
| `no_changes` | The session ended leaving no commit. **Not** proof the issue was trivial — it is also what a misconfigured environment looks like. |
| `needs_review` | Committed, but no PR was opened. |
| `blocked` | Paused on a missing capability in another repo, for which the session filed an issue (`FLEET-DISPATCH-BLOCKED:` sentinel). Pauses the candidate rather than failing it. |
| `denied` | Policy refused the side effect. |
| `skipped` / `failure` | Did not run / errored. |

## Usage

```bash
# Dry run — --session-runner defaults to `noop`, which changes nothing.
mycoder build --repo MyThingsLab/my-raytracer --issue 12 --source ../my-raytracer

# For real: a bounded headless session that opens a ready-for-review PR.
mycoder build --repo MyThingsLab/my-raytracer --issue 12 \
  --source ../my-raytracer --session-runner claude \
  --run-tests --max-budget-usd 5 --max-turns 40
```

`--json` prints the result machine-readably. A redacted session transcript is
written alongside the ledger.

## In the fleet

`my-orchestrator` picks the candidate; `my-fleet`'s dispatcher hands it to
my-coder as the worker; `my-searcher` is imported to rank the files most
relevant to the issue before the session starts spending tokens. my-coder
does not pick its own work — the `my-coder` backlog label is for bugs in
this tool, not for work it should do.

Its first real build target is
[`my-raytracer`](https://github.com/MyThingsLab/my-raytracer).

See [`CLAUDE.md`](CLAUDE.md) for the full contract.

## Install (development)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ../my-things-core -e ".[dev]"
pytest
```

## License

MIT — see [`LICENSE`](LICENSE).
