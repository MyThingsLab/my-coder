from __future__ import annotations

import argparse
import json
from pathlib import Path

from mythings.github import GitHub
from mythings.ledger import Ledger

from mycoder.coder import Coder, Result
from mycoder.session import ClaudeSessionRunner, NoopSessionRunner, SessionRunner


def _render(result: Result) -> str:
    line = f"{result.outcome}: {result.detail}"
    if result.issue is not None:
        line += f" (issue #{result.issue})"
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
    build.add_argument("--max-turns", type=int, default=40, help="session turn cap")
    build.add_argument(
        "--session-timeout-s", type=float, default=1800.0, help="session wall-clock cap"
    )
    build.add_argument(
        "--run-tests",
        action="store_true",
        help="re-run the target repo's tests in the worktree before opening the PR",
    )
    build.add_argument("--ledger", type=Path, default=Path(".mythings/ledger.jsonl"))
    build.add_argument("--json", action="store_true", help="print the result as JSON")

    args = parser.parse_args(argv)
    coder = coder_factory(
        repo=args.source,
        repo_slug=args.repo,
        github=GitHub(args.repo),
        ledger=Ledger(args.ledger),
        session_runner=_runner(args.session_runner),
        base=args.base,
        run_tests=args.run_tests,
        max_budget_usd=args.max_budget_usd,
        max_turns=args.max_turns,
        session_timeout_s=args.session_timeout_s,
    )
    result = coder.run(issue_number=args.issue)
    print(_json(result) if args.json else _render(result))
    return 0 if result.outcome not in ("failure", "denied") else 1


if __name__ == "__main__":
    raise SystemExit(main())
