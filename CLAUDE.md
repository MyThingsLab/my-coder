# my-coder — agent instructions

You are developing **my-coder**, a MyThingsLab My[X] tool.

**Inherited rules:** obey [`./HARNESS.md`](./HARNESS.md) in full — the vendored
MyThingsLab build-harness rules. Do not restate or override them. Anything not
covered here defers to `HARNESS.md`, then `my-things-core/docs/CONVENTIONS.md`.

## This tool

- **Purpose:** takes one picked issue (a `Candidate` from `my-orchestrator`,
  dispatched by `fleet-dispatch`) and closes it as a draft PR: reads the
  target repo, makes the smallest change with tests, runs its suite and
  linter, commits, and opens `gh pr create --draft`. This is the fleet's
  **worker** role — formerly inlined in `fleet-dispatch/fleet_dispatch.py`
  (`_prompt_for`/`_dispatch_one`/`_finalize_pr`), now a real, tested, versioned
  tool instead of a workspace script.
- **The single Engine call:** **none — this tool is the fleet's one deliberate
  exception to the single-narrow-Engine-call pattern.** Every other My[X] tool
  makes exactly one tools-*disabled* `ClaudeCLIEngine` call ("judgment only,
  never a side effect") and does everything else deterministically. my-coder's
  core action cannot be that shape: closing an arbitrary issue requires an
  open-ended, multi-turn, tools-*enabled* headless `claude -p` session — read
  arbitrary files, edit arbitrary files, run the test suite and linter,
  decide when it's done. That session is not routed through
  `mythings.engine.Engine`; it is my-coder's own seam, invoked directly via
  `subprocess` against the `claude` CLI with a real tool allowlist, inside a
  `mythings.isolation.Workspace` git-worktree sandbox under the caller's
  `CLAUDE_CONFIG_DIR`. Everything *around* that session — outcome
  classification, PR-readiness checks, ledger writes, the blocker protocol —
  stays deterministic, same as every other tool.
- **Invariants / rules:** every `git`/`gh` side effect the session takes is
  its own responsibility inside the sandbox, but the PR open/promote steps
  my-coder performs itself are wrapped as `Action(kind="bash", ...)` through
  `Policy.evaluate` (MyGuard) first, same as every other tool. Opens at most
  **one** PR per issue, as a **draft**, head `mycoder/<repo>-<issue-number>`,
  and never promotes it to ready or merges it — promotion requires the PR
  body's checklist to hold (`Closes #<n>` + at least one checked box) and CI
  green; a human always does the actual merge. A session that leaves no
  commit is `outcome=no_changes`; one that commits but doesn't open a PR is
  `outcome=needs_review`; success requires both a real commit *and* an open
  PR. A session may end by filing a cross-repo blocker issue instead
  (`FLEET-DISPATCH-BLOCKED: MyThingsLab/<repo>#<number>`), which pauses the
  candidate rather than failing it. Never touches a repo other than the one
  named by the issue it was given.
- **Backlog label:** `my-coder` (issues my-coder itself needs — bugs in the
  tool. It does not pick up arbitrary fleet backlog items; `my-orchestrator`
  picks those and hands them to my-coder as the worker.)
