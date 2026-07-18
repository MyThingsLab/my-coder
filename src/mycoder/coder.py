from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from mythings.github import GitHub, Issue
from mythings.isolation import Workspace, in_github_actions
from mythings.ledger import Ledger
from mythings.policy import ALLOW, Action, Decision, Policy, PolicyResult

from mycoder.session import SessionRunner

TOOL = "mycoder"
LEDGER_KIND = "code"
BACKLOG_LABEL = "my-coder"  # my-coder's own bugs; target issues arrive via --issue

_PROMPT = """\
You are MyCoder, the MyThingsLab fleet's worker. Close this one GitHub issue in \
{repo} by editing files in the current checkout.

Issue #{number}: {title}

{body}

Target-repo conventions (its own CLAUDE.md / HARNESS.md, authoritative here):
{conventions}

Rules:
- Make the smallest change that fully closes the issue, with tests.
- Run the repo's own test suite and linter; leave them green.
- Commit your work with git and a clear message. Do NOT run `git push`, and do \
NOT use any `gh` command — MyCoder pushes the branch and opens the draft PR.
- Stay entirely within this repo's checkout; never touch another repo.
"""


class _AllowAll:
    # Default gate for the one side effect (a draft PR). The fleet driver injects
    # myguard.Guard in production; a lone invocation opens PRs unguarded, same
    # convention as every other tool's template default.
    def evaluate(self, action: Action) -> PolicyResult:
        return ALLOW


def _run_git(tree: Path, argv: list[str]) -> str:
    proc = subprocess.run(["git", "-C", str(tree), *argv], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(argv)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


@dataclass(frozen=True)
class Result:
    outcome: str  # success | needs_review | no_changes | skipped | denied | failure
    detail: str
    issue: int | None = None
    pr: int | None = None
    files_touched: list[str] = field(default_factory=list)
    tests_passed: bool | None = None
    cost_usd: float = 0.0


class Coder:
    # The worker loop: read one issue → run a bounded, sandboxed coding session
    # (the single judgment step, iterate by re-invoking) → count what it
    # committed → push + open a draft PR through Policy, never merge → ledger.
    def __init__(
        self,
        *,
        repo: str | Path,
        github: GitHub,
        ledger: Ledger,
        session_runner: SessionRunner,
        repo_slug: str | None = None,
        policy: Policy | None = None,
        base: str = "main",
        run_tests: bool = False,
        test_command: list[str] | None = None,
        max_budget_usd: float = 5.0,
        max_turns: int = 60,
        session_timeout_s: float = 1800.0,
        transcripts_dir: Path | None = None,
        git: Callable[[Path, list[str]], str] = _run_git,
        workspace_factory: Callable[..., Workspace] = Workspace,
    ) -> None:
        self.repo = Path(repo)
        self.github = github
        self.ledger = ledger
        self.session_runner = session_runner
        self.repo_slug = repo_slug
        self.policy = policy or _AllowAll()
        self.base = base
        self.run_tests = run_tests
        self.test_command = test_command or ["python", "-m", "pytest", "-q"]
        self.max_budget_usd = max_budget_usd
        # A cap too low is indistinguishable from a real failure: a session that
        # hits it exits `is_error`. Default generously; a small issue already
        # needs ~30 turns, so 60 leaves headroom without inviting runaway spend
        # (--max-budget-usd is the real backstop).
        self.max_turns = max_turns
        self.session_timeout_s = session_timeout_s
        self.transcripts_dir = Path(transcripts_dir) if transcripts_dir else None
        self._git = git
        self._workspace = workspace_factory

    # -- helpers ---------------------------------------------------------

    def pick_issue(self, number: int) -> Issue | None:
        return next((i for i in self.github.list_issues() if i.number == number), None)

    def _repo_name(self) -> str:
        if self.repo_slug:
            return self.repo_slug.split("/")[-1]
        return self.repo.resolve().name

    def _conventions(self, tree: Path) -> str:
        parts = []
        for name in ("CLAUDE.md", "HARNESS.md"):
            path = tree / name
            if path.exists():
                parts.append(f"--- {name} ---\n{path.read_text(encoding='utf-8')}")
        return "\n\n".join(parts) if parts else "(no CLAUDE.md/HARNESS.md found)"

    def _prompt(self, issue: Issue, tree: Path) -> str:
        return _PROMPT.format(
            repo=self.repo_slug or self._repo_name(),
            number=issue.number,
            title=issue.title,
            body=issue.body or "(no description)",
            conventions=self._conventions(tree),
        )

    def _commit_count(self, tree: Path, base_sha: str) -> int:
        out = self._git(tree, ["rev-list", "--count", f"{base_sha}..HEAD"]).strip()
        return int(out or "0")

    def _changed_files(self, tree: Path, base_sha: str) -> list[str]:
        out = self._git(tree, ["diff", "--name-only", f"{base_sha}..HEAD"]).strip()
        return [line for line in out.splitlines() if line]

    def _tests_pass(self, tree: Path) -> bool:
        proc = subprocess.run(self.test_command, cwd=str(tree), capture_output=True, text=True)
        return proc.returncode == 0

    def _pr_body(self, issue: Issue, files: list[str]) -> str:
        listed = "\n".join(f"- `{f}`" for f in files) or "- (none reported)"
        return (
            f"Closes #{issue.number}.\n\n"
            "Implemented by MyCoder via a headless coding session.\n\n"
            "## Readiness\n"
            "- [ ] scope matches the issue\n"
            "- [ ] tests green\n\n"
            f"## Files touched\n{listed}\n"
        )

    def _persist_transcript(self, issue: Issue, transcript: str) -> str | None:
        # The transcript is a session's only forensic record (my-coder's judgment
        # step is opaque otherwise); persist the already-redacted stream so a
        # failure or a surprising diff can be traced after the worktree is gone.
        if not self.transcripts_dir or not transcript:
            return None
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        path = self.transcripts_dir / f"{self._repo_name()}-{issue.number}-{ts}.jsonl"
        path.write_text(transcript, encoding="utf-8")
        return str(path)

    def _record(self, outcome: str, detail: str, **data: object) -> None:
        self.ledger.record(TOOL, LEDGER_KIND, outcome, detail, **data)

    # -- loop ------------------------------------------------------------

    def run(self, issue_number: int) -> Result:
        issue = self.pick_issue(issue_number)
        if issue is None:
            detail = f"no open issue #{issue_number} in {self.repo_slug or self._repo_name()}"
            self._record("skipped", detail, issue=issue_number)
            return Result("skipped", detail, issue=issue_number)

        with self._workspace(self.repo, base_ref=f"origin/{self.base}") as tree:
            branch = f"{TOOL}/{self._repo_name()}-{issue.number}"
            # Name the detached worktree HEAD before the session runs so every
            # commit it makes lands on this branch (local-only, no side effect).
            self._git(tree, ["checkout", "-B", branch])
            base_sha = self._git(tree, ["rev-parse", "HEAD"]).strip()

            session = self.session_runner.run(
                prompt=self._prompt(issue, tree),
                cwd=tree,
                max_budget_usd=self.max_budget_usd,
                max_turns=self.max_turns,
                timeout_s=self.session_timeout_s,
            )
            transcript_path = self._persist_transcript(issue, session.transcript)
            if session.leaked:
                self.ledger.record(
                    TOOL,
                    "secret_alert",
                    "redacted",
                    f"redacted credential-shaped text from #{issue.number}'s transcript",
                    issue=issue.number,
                    patterns=session.leaked,
                )

            # Fields every terminal record below carries — including the
            # session's last words and the transcript path, so any outcome
            # (especially a failure) is diagnosable after the worktree is gone.
            common: dict[str, object] = {
                "issue": issue.number,
                "turns": session.turns,
                "cost_usd": session.cost_usd,
                "final_message": session.final_message[:500],
                "transcript": transcript_path,
            }

            commits = self._commit_count(tree, base_sha)
            if commits == 0:
                # Nothing durable to keep: an errored session that committed
                # nothing is a real failure; a clean one is an honest no-op.
                if session.ok:
                    outcome, detail = "no_changes", f"session left no commit for #{issue.number}"
                else:
                    outcome = "failure"
                    detail = f"session failed for #{issue.number} with no commit: {session.error}"
                self._record(outcome, detail, **common)
                return Result(outcome, detail, issue=issue.number, cost_usd=session.cost_usd)

            files = self._changed_files(tree, base_sha)

            if self.run_tests and not self._tests_pass(tree):
                detail = f"generated code for #{issue.number} failed the test suite"
                self._record("failure", detail, files_touched=files, tests_passed=False, **common)
                return Result(
                    "failure",
                    detail,
                    issue=issue.number,
                    files_touched=files,
                    tests_passed=False,
                    cost_usd=session.cost_usd,
                )
            tests_passed: bool | None = True if self.run_tests else None

            gate = self.policy.evaluate(
                Action(kind="bash", payload={"command": f"gh pr create --head {branch}"})
            )
            if gate.under(unattended=in_github_actions()) is not Decision.ALLOW:
                detail = f"policy blocked the PR for #{issue.number}: {gate.reason or gate.rule}"
                self._record("denied", detail, files_touched=files, **common)
                return Result(
                    "denied",
                    detail,
                    issue=issue.number,
                    files_touched=files,
                    cost_usd=session.cost_usd,
                )

            # Push the durable commits regardless of how the session ended, so a
            # turn-capped or timed-out session's real work is never discarded.
            try:
                self._git(tree, ["push", "-u", "origin", branch])
            except RuntimeError as exc:
                detail = f"commits present but push failed for #{issue.number}: {exc}"
                self._record("needs_review", detail, files_touched=files, **common)
                return Result(
                    "needs_review",
                    detail,
                    issue=issue.number,
                    files_touched=files,
                    cost_usd=session.cost_usd,
                )

            # Open the draft PR only when the session finished cleanly. A session
            # that committed real work but ended in error/timeout leaves its
            # branch pushed for a human to resume — durable, but not "done".
            if not session.ok:
                detail = (
                    f"branch {branch} pushed for #{issue.number}, no PR — session ended "
                    f"early ({session.error}); resume or review the branch"
                )
                self._record("needs_review", detail, files_touched=files, branch=branch, **common)
                return Result(
                    "needs_review",
                    detail,
                    issue=issue.number,
                    files_touched=files,
                    tests_passed=tests_passed,
                    cost_usd=session.cost_usd,
                )

            pr = self.github.open_pr(
                title=issue.title,
                body=self._pr_body(issue, files),
                base=self.base,
                head=branch,
                draft=True,
            )

        self._record(
            "success",
            f"opened draft PR #{pr.number} for #{issue.number}",
            pr=pr.number,
            files_touched=files,
            tests_passed=tests_passed,
            pr_url=pr.url,
            **common,
        )
        return Result(
            "success",
            f"opened draft PR #{pr.number}",
            issue=issue.number,
            pr=pr.number,
            files_touched=files,
            tests_passed=tests_passed,
            cost_usd=session.cost_usd,
        )
