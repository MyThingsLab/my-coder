# MyThingsLab build harness — rules for any My[X] tool

You are an agent developing a MyThingsLab tool. These rules are inherited by
**every** tool and backed by mechanical gates (ruff, pytest, CI, branch
protection, MyGuard). The canonical copy ships in `mythings/harness.md`; inside a
tool this is a **vendored copy** kept in sync by a drift-check test — never edit
it in a tool. To change a rule, edit the canonical in `mythings-core` and
re-vendor the `HARNESS.md` copies.

## The shape you must uphold
- A tool reads one unit of work (a GitHub issue), does deterministic pre-work
  (lint / index / checks) with **no** model call, calls the Engine **once** for
  the single step that needs judgment, and opens a **PR — never a merge**.
- Build on the mythings-core contracts (`ledger`, `policy`, `engine`, `github`,
  `isolation`). Do not re-implement them; do not add a sixth contract lightly.
- Dependency-free runtime: shell out to `gh` / `git`, don't pull SDKs. Dev-only
  deps (pytest, ruff) are fine.

## Test-driven, local-first
- Write tests alongside code: happy path **and** at least one edge/failure case.
- Your TDD loop runs `ruff check` + `pytest` **locally**. Cloud CI is the PR
  gate, not your inner loop — never wait on it to iterate.
- Mark slow/integration tests `@pytest.mark.slow`; keep the default suite fast.
- Mock only at system boundaries (gh / git / network), never internal modules.

## Code style
- Python ≥ 3.11, type hints on every signature, `from __future__ import annotations`.
- ruff (E, F, I, UP, B), line length 100. Prefer `pathlib`, dataclasses/Pydantic,
  `StrEnum`. No docstrings unless asked; comment only non-obvious *why*.

## Git, CI, and provenance
- Commits: imperative subject, `Co-Authored-By` trailer. Never push unless asked.
- The tool opens PRs; a human or a permission-scoped App merges. Never self-merge.
- CI is Linux-only, one job, with `concurrency: cancel-in-progress`, `paths-ignore`
  for docs, draft-PR skip, and a timeout. Do not add macOS/Windows runners.
- Append a `dev-ledger/` entry via `python -m mythings._devledger` for each
  milestone and decision — see `mythings-core/docs/PROVENANCE.md`.

## Ownership & placement
- All MyThingsLab repos live under the `MyThingsLab` GitHub org, public, and are
  kept **entirely isolated from other ventures**. Never mix them.

## The per-tool seams (fill these; everything above is fixed)
- Name `my-<x>` / package `my<x>`; one-line purpose.
- The single Engine judgment step.
- Tool-specific invariants / policy rules.
- The backlog label it consumes.
