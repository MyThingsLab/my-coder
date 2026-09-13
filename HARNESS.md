# MyThingsLab build harness — rules for any My[X] tool

You are an agent developing a MyThingsLab tool. These rules are inherited by
**every** tool and backed by mechanical gates (ruff, pytest, CI, branch
protection, MyGuard). The canonical copy ships in `mythings/harness.md`; inside a
tool this is a **vendored copy** kept in sync by a drift-check test — never edit
it in a tool. To change a rule, edit the canonical in `my-things-core`, then
re-vendor every copy: `python -m mythings._harness <workspace-root>`.

## The shape you must uphold
- A tool reads one unit of work (a GitHub issue), does deterministic pre-work
  (lint / index / checks) with **no** model call, calls the Engine **once** for
  the single step that needs judgment, and opens a **PR — never a merge**.
- Build on the my-things-core contracts (`ledger`, `policy`, `engine`, `github`,
  `isolation`). Do not re-implement them; do not add a sixth contract lightly.
- Dependency-free runtime: shell out to `gh` / `git`, don't pull SDKs. Dev-only
  deps (pytest, ruff) are fine.

## Before you touch anything
- `git fetch` and scan open PRs/issues first — dispatched workers and other
  sessions develop these repos concurrently; never duplicate or clobber
  in-flight work, and never trust a local checkout to be current.
- Never work on a local `main`: branch immediately after syncing it. Every
  change lands via PR — `main` is branch-protected (PR + green `test` check).

## Test-driven, local-first
- Write tests alongside code: happy path **and** at least one edge/failure case.
- Your TDD loop runs `ruff check` + `pytest` **locally**. Cloud CI is the PR
  gate, not your inner loop — never wait on it to iterate.
- Before shipping, run the suite the way CI will see it: `GITHUB_ACTIONS=true
  pytest` with `my-things-core` installed from `@main` — unattended `Policy`
  ASKs collapse to DENY, and your local editable core may be ahead of main.
- Mark slow/integration tests `@pytest.mark.slow`; keep the default suite fast.
- Mock only at system boundaries (gh / git / network), never internal modules.

## Code style
- Python ≥ 3.11, type hints on every signature, `from __future__ import annotations`.
- ruff (E, F, I, UP, B), line length 100. Prefer `pathlib`, dataclasses/Pydantic,
  `StrEnum`. No docstrings unless asked; comment only non-obvious *why*.

## Git, CI, and provenance
- Commits: imperative subject, `Co-Authored-By` trailer. Never push unless asked.
- The tool opens PRs; a human or a permission-scoped App merges. Never self-merge.
- Open PRs as **draft**; mark ready only once the body's checklist holds *and*
  CI is green.
- CI is Linux-only, one job, with `concurrency: cancel-in-progress`, `paths-ignore`
  for docs, draft-PR skip, and a timeout. Do not add macOS/Windows runners.
- Append a `dev-ledger/` entry via `python -m mythings._devledger` for each
  milestone and decision — see `my-things-core/docs/PROVENANCE.md`.

## Ownership & safety
- All MyThingsLab repos live under the `MyThingsLab` GitHub org, public, and are
  kept **entirely isolated from other ventures**. Never mix them.
- Never persist a credential — not on disk, not in git, not in the ledger. Use
  `gh secret set`. A token pasted into chat is already exposed: say so and
  suggest rotation.

## The per-tool seams (fill these; everything above is fixed)
- Name `my-<x>` / package `my<x>`; one-line purpose.
- The single Engine judgment step.
- Tool-specific invariants / policy rules.
- The backlog label it consumes.
- How to verify it end-to-end (the safe command(s) that prove it works, beyond
  pytest — typically an `--engine noop` dry run).
