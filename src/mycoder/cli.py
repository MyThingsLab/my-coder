from __future__ import annotations

import argparse
import json
from pathlib import Path

from mythings.github import GitHub
from mythings.ledger import Ledger

from mycoder.coder import Coder, Result, default_guarded_policy
from mycoder.session import ClaudeSessionRunner, NoopSessionRunner, SessionRunner


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
            "cost_usd": result.cost_usd,
            "attempts": result.attempts,
            "blocker": result.blocker,
        }
    )


def _runner(name: str) -> SessionRunner:
    return ClaudeSessionRunner() if name == "claude" else NoopSessionRunner()


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
        choices=("claude", "noop"),
        default="noop",
        help="claude runs a real headless session; noop is a dry run (no change, no PR)",
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
    # Keep transcripts next to the ledger (its own provenance dir), not in the
    # invoking CWD — which is often the target repo's checkout.
    transcripts_dir = args.transcripts_dir or args.ledger.parent / "mycoder-transcripts"
    coder = coder_factory(
        repo=args.source,
        repo_slug=args.repo,
        github=GitHub(args.repo),
        ledger=Ledger(args.ledger),
        session_runner=_runner(args.session_runner),
        policy=default_guarded_policy() if args.guarded else None,
        base=args.base,
        run_tests=args.run_tests,
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
