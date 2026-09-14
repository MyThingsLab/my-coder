from __future__ import annotations

import argparse
import json
import os
import shlex
from pathlib import Path

from myguard.ask import ASK_COMMAND_ENV
from mythings.github import GitHub
from mythings.ledger import Ledger

from mycoder.coder import FLEET_ORG, Coder, Result, default_guarded_policy
from mycoder.session import (
    ClaudeSessionRunner,
    GeminiSessionRunner,
    NoopSessionRunner,
    SessionRunner,
)


def _render(result: Result) -> str:
    line = f"{result.outcome}: {result.detail}"
    if result.issue is not None:
        line += f" (issue #{result.issue})"
    if result.blocker is not None:
        line += f" [blocked on {result.blocker}]"
    if result.attempts > 1:
        line += f" [{result.attempts} attempts, ${result.cost_usd:.2f} total]"
    return line


def _json(result: Result) -> str:
    return json.dumps(
        {
            "outcome": result.outcome,
            "detail": result.detail,
            "issue": result.issue,
            "pr": result.pr,
            "files_touched": result.files_touched,
            "tests_passed": result.tests_passed,
            "tests_env": result.tests_env,
            "supplied_test_command": result.supplied_test_command,
            "cost_usd": result.cost_usd,
            "attempts": result.attempts,
            "blocker": result.blocker,
            "failing_tests": result.failing_tests,
            "failure_trace": result.failure_trace,
            "inherited_failures": result.inherited_failures,
        }
    )


def _runner(name: str, *, in_fleet: bool = True) -> SessionRunner:
    if name == "claude":
        return ClaudeSessionRunner(in_fleet=in_fleet)
    if name == "gemini":
        return GeminiSessionRunner(in_fleet=in_fleet)
    return NoopSessionRunner()


def main(argv: list[str] | None = None, *, coder_factory: type[Coder] = Coder) -> int:
    parser = argparse.ArgumentParser(
        prog="mycoder",
        description="Close one target-repo issue as a draft PR via a headless coding session.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    build = sub.add_parser("build", help="run one bounded coding session against a single issue")
    build.add_argument("--repo", required=True, help="target repo slug owner/name")
    build.add_argument("--issue", type=int, required=True, help="issue number to close")
    build.add_argument(
        "--source", type=Path, default=Path.cwd(), help="local checkout of the target repo"
    )
    build.add_argument("--base", default="main", help="base branch for the PR")
    build.add_argument(
        "--session-runner",
        choices=("claude", "gemini", "noop"),
        default="noop",
        help="claude runs a real headless session; gemini runs headless via gemini/agy CLI; "
        "noop is a dry run (no change, no PR)",
    )
    build.add_argument("--max-budget-usd", type=float, default=5.0, help="session spend cap")
    build.add_argument(
        "--max-turns",
        type=int,
        default=60,
        help="session turn cap (too low reads as a failure — a small issue already needs ~30)",
    )
    build.add_argument(
        "--session-timeout-s", type=float, default=1800.0, help="session wall-clock cap"
    )
    build.add_argument(
        "--max-attempts",
        type=int,
        default=1,
        help="retry with a fresh session (resuming the checkpointed branch) on a recoverable "
        "outcome; 1 keeps the old single-shot behavior",
    )
    build.add_argument(
        "--max-total-budget-usd",
        type=float,
        default=None,
        help="spend cap across all attempts combined (default: 3x --max-budget-usd)",
    )
    build.add_argument(
        "--run-tests",
        action="store_true",
        help="re-run the target repo's tests in the worktree before opening the PR",
    )
    build.add_argument(
        "--test-command",
        default=None,
        help="command --run-tests runs, as one shell-quoted string (default: pytest under the "
        "first interpreter found on PATH). Point this at a prepared environment when the "
        "target repo's dependencies are not importable from the ambient interpreter -- "
        "without it, a pass is reported as tests_env=ambient and the PR opens as a draft "
        "even if tests_passed is true, since an ambient pass is not confirmed to match "
        "what the target's CI installs",
    )
    build.add_argument(
        "--guarded",
        action="store_true",
        help="gate opening the draft PR through myguard.Guard (real ASK-channel human "
        "approval via MYTHINGS_ASK_CMD) instead of always allowing it; default stays "
        "unguarded for a lone invocation",
    )
    build.add_argument("--ledger", type=Path, default=Path(".mythings/ledger.jsonl"))
    build.add_argument(
        "--transcripts-dir",
        type=Path,
        default=None,
        help="where to write the redacted session transcript (default: alongside the ledger)",
    )
    build.add_argument("--json", action="store_true", help="print the result as JSON")

    args = parser.parse_args(argv)
    # Preflight, before a paid session runs. `--guarded` turns opening the PR
    # into an ASK, and Guard resolves an ASK only through MYTHINGS_ASK_CMD.
    # Unarmed, a session does its work, costs real money, and then cannot open
    # anything -- which is exactly what happened to three corpus runs (#29).
    # Refusing up front costs nothing; refusing at the end costs the session.
    if getattr(args, "guarded", False) and not os.environ.get(ASK_COMMAND_ENV, "").strip():
        parser.error(
            f"--guarded needs an ask channel, but {ASK_COMMAND_ENV} is unset, so every PR "
            "would be blocked after the session had already been paid for. Run under "
            "fleet_dispatch/fleet_cycle (which arm it), set it yourself, or drop --guarded."
        )
    # Keep transcripts next to the ledger (its own provenance dir), not in the
    # invoking CWD — which is often the target repo's checkout.
    transcripts_dir = args.transcripts_dir or args.ledger.parent / "mycoder-transcripts"
    in_fleet = args.repo.split("/")[0] == FLEET_ORG
    coder = coder_factory(
        repo=args.source,
        repo_slug=args.repo,
        github=GitHub(args.repo),
        ledger=Ledger(args.ledger),
        session_runner=_runner(args.session_runner, in_fleet=in_fleet),
        policy=default_guarded_policy() if args.guarded else None,
        base=args.base,
        run_tests=args.run_tests,
        test_command=shlex.split(args.test_command) if args.test_command else None,
        max_budget_usd=args.max_budget_usd,
        max_turns=args.max_turns,
        session_timeout_s=args.session_timeout_s,
        max_attempts=args.max_attempts,
        max_total_budget_usd=args.max_total_budget_usd,
        transcripts_dir=transcripts_dir,
    )
    result = coder.run(issue_number=args.issue)
    print(_json(result) if args.json else _render(result))
    return 0 if result.outcome not in ("failure", "denied") else 1


if __name__ == "__main__":
    raise SystemExit(main())
