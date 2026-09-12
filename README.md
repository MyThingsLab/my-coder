# my-coder

[![CI](https://github.com/MyThingsLab/my-coder/actions/workflows/ci.yml/badge.svg)](https://github.com/MyThingsLab/my-coder/actions/workflows/ci.yml) [![codecov](https://codecov.io/gh/MyThingsLab/my-coder/branch/main/graph/badge.svg)](https://codecov.io/gh/MyThingsLab/my-coder) ![Python](https://img.shields.io/badge/python-3.11%2B-blue) [![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

The MyThingsLab fleet's **worker**: takes one picked issue and closes it as a
PR — reads the target repo, makes the smallest change with tests, runs
its suite and linter, commits, and opens the PR. Formerly inlined in
[`fleet-dispatch`](../fleet-dispatch)'s `fleet_dispatch.py`; now a real,
tested, versioned `My[X]` tool.

Unlike every other fleet tool, my-coder makes no single narrow Engine call —
its core action is an open-ended, tools-enabled headless coding session, the
fleet's one deliberate exception to that pattern. See
[`CLAUDE.md`](CLAUDE.md) for the full contract.

## Usage

```bash
# Close issue #12 in a target repo by running one bounded coding session,
# then open a PR. --session-runner defaults to `noop` (a safe dry run);
# pass `claude` to run a real headless session.
mycoder build --repo MyThingsLab/my-raytracer --issue 12 \
  --source ../my-raytracer --session-runner claude \
  --max-budget-usd 5 --max-turns 40
```

The session is sandboxed to a throwaway git worktree and may only edit files
and commit; my-coder itself performs the single push + `gh pr create`
side effect, gated by `Policy`. It never merges — a human always does.

The PR opens **ready for review** when `--run-tests` ran the target repo's
suite in the worktree and it passed, and as a **draft** otherwise. CI skips
required checks on a draft, so a draft can never show a green check; opening
ready is what lets CI run, and the gate is the merge (see `my-fleet#32`).

Outcomes: `success` (commit +
PR), `no_changes`, `needs_review`, `denied`, `skipped`, `failure`,
`blocked` (paused on a missing capability in another repo the session filed
an issue for — see the `FLEET-DISPATCH-BLOCKED:` sentinel in the prompt).

## Install (development)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ../mythings-core -e ".[dev]"
pytest
```

## License

MIT — see [`LICENSE`](LICENSE).
