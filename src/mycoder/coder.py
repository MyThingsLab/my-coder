from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from myguard.guard import Guard
from myguard.rules import Rule
from mysearcher.searcher import Issue as SearchIssue
from mysearcher.searcher import Searcher
from mythings.github import GitHub, Issue
from mythings.isolation import Workspace, in_github_actions
from mythings.ledger import Ledger
from mythings.policy import ALLOW, Action, Decision, Policy, PolicyResult

from mycoder.session import SessionRunner

PR_ACTION_KIND = "draft-pr-create"

TOOL = "mycoder"
LEDGER_KIND = "code"
BACKLOG_LABEL = "my-coder"  # my-coder's own bugs; target issues arrive via --issue

_BLOCKED_SENTINEL = "FLEET-DISPATCH-BLOCKED:"

_PROMPT = """\
You are MyCoder, the MyThingsLab fleet's worker. Close this one GitHub issue in \
{repo} by editing files in the current checkout.

Issue #{number}: {title}

{body}

{resume_note}\
{relevant_files}\
{research_context}\
Target-repo conventions (its own CLAUDE.md / HARNESS.md, authoritative here):
{conventions}

{style_anchor}

You are running fully non-interactively, as a headless session: no human is \
watching and no one can approve a permission prompt. If a command is denied, \
do NOT ask for approval or wait for it — it will never come. Work only with \
the tools you already have.

If this issue turns out to be blocked by a missing capability in ANOTHER \
MyThingsLab repo (a contract, helper, or fix that repo must land first), do \
not thrash against it. Use `gh issue create --repo MyThingsLab/<repo>` to file \
a precise issue describing exactly what that repo must add and why, then END \
your run by printing one final line, exactly:
  {blocked_sentinel} MyThingsLab/<repo>#<number>
naming the issue you just filed. That records the dependency so this issue is \
paused, not failed, until the blocker is resolved.

If while working you discover a SEPARATE bug that is a security issue or \
breaks a core invariant shared across the fleet (a `my-things-core` contract, \
the build harness, or anything that would let other tools ship broken work on \
top of it), file it immediately with `gh issue create --label critical --label \
bug --repo MyThingsLab/<repo>` describing exactly what's broken and its blast \
radius. That label halts new fleet dispatch org-wide until it's closed — do \
not wait until you finish this task to file it. Filing it does not abort your \
own work; keep going on this issue unless the critical bug blocks it directly, \
in which case treat it as a blocker per the paragraph above.

Rules:
- Make the smallest change that fully closes the issue, with tests.
- Match the conventions of the existing code shown above: module layout, import
  style (e.g. `from __future__ import annotations`), type hints on EVERY
  signature (test functions included), naming, and the existing test style. When
  in doubt, imitate the nearest existing file rather than inventing a style.
- Run the repo's own test suite and linter; leave them green.
- Commit your work with git and a clear message. Do NOT run `git push`, and do \
NOT use any `gh` command other than `gh issue create` for a blocker/critical bug \
above — MyCoder pushes the branch and opens the draft PR.
- Stay entirely within this repo's checkout; never touch another repo.
"""


class _AllowAll:
    # Default gate for the one side effect (a draft PR). The fleet driver injects
    # myguard.Guard in production; a lone invocation opens PRs unguarded, same
    # convention as every other tool's template default. `--guarded` opts a CLI
    # invocation into default_guarded_policy() below instead.
    def evaluate(self, action: Action) -> PolicyResult:
        return ALLOW


def default_guarded_policy() -> Policy:
    # PR_ACTION_KIND is its own structured kind, not "bash": myguard's rules
    # treat "bash" as an open-ended escape hatch that's permissive by design
    # (see myguard/rules.py), so a Guard() gating a `gh pr create` shelled out
    # as "bash" would silently no-op. Naming the kind is what lets a Rule (and
    # the fleet's real ASK channel -- MYTHINGS_ASK_CMD, live since 2026-07-12)
    # actually intercept it, the same pattern myplanner's default_policy()
    # uses for its own "tracking-issue-edit" kind.
    return Guard(
        rules=[
            Rule(
                "draft-pr-needs-a-human",
                Decision.ASK,
                "opens a draft PR",
                kind=PR_ACTION_KIND,
            )
        ]
    )


def _run_git(tree: Path, argv: list[str]) -> str:
    proc = subprocess.run(["git", "-C", str(tree), *argv], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(argv)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


@dataclass(frozen=True)
class Result:
    outcome: str  # success | needs_review | no_changes | skipped | denied | failure | blocked
    detail: str
    issue: int | None = None
    pr: int | None = None
    files_touched: list[str] = field(default_factory=list)
    tests_passed: bool | None = None
    cost_usd: float = 0.0  # summed across every attempt, not just the last
    attempts: int = 1
    blocker: str | None = None  # "<org>/<repo>#<n>" when outcome == "blocked"


def _parse_blocker(final_message: str) -> str | None:
    for line in final_message.splitlines():
        line = line.strip()
        if line.startswith(_BLOCKED_SENTINEL):
            ref = line[len(_BLOCKED_SENTINEL) :].strip()
            return ref or None
    return None


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
        max_attempts: int = 1,
        max_total_budget_usd: float | None = None,
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
        # Default max_attempts=1 keeps every existing caller's behavior
        # unchanged; opting into retries is a deliberate choice, not a new
        # default cost. Retries recycle v0.3's checkpoint branch (same issue,
        # same worktree state a prior attempt left off at) rather than
        # redoing work, so a harder issue gets more shots without more waste.
        self.max_attempts = max(1, max_attempts)
        self.max_total_budget_usd = (
            max_total_budget_usd if max_total_budget_usd is not None else max_budget_usd * 3
        )
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

    def _style_anchor(self, tree: Path, *, max_files: int = 3, max_chars: int = 6000) -> str:
        # A repo without a CLAUDE.md still has a house style in its existing
        # code; show the session that code so it matches conventions (type
        # hints, imports, test shape) the first time instead of guessing — the
        # single biggest source of review-only polish on generated PRs. Largest
        # files first: more content is a stronger convention signal.
        try:
            listed = [p for p in self._git(tree, ["ls-files"]).splitlines() if p]
        except RuntimeError:
            return ""
        exemplars = sorted(
            (p for p in listed if p.endswith(".py") and (p.startswith(("src/", "tests/")))),
            key=lambda p: (tree / p).stat().st_size if (tree / p).is_file() else 0,
            reverse=True,
        )[:max_files]
        blocks = []
        for rel in exemplars:
            path = tree / rel
            if path.is_file():
                blocks.append(f"--- {rel} ---\n{path.read_text(encoding='utf-8')[:max_chars]}")
        if not blocks:
            return "Existing code: (none yet — this is an early/greenfield repo)."
        tree_view = "\n".join(listed[:300])
        return (
            "Existing code in this repo (match its conventions exactly):\n\n"
            f"Repository files:\n{tree_view}\n\n"
            "Representative existing files:\n\n" + "\n\n".join(blocks)
        )

    def _resume_note(self, prior_commits: int) -> str:
        if prior_commits == 0:
            return ""
        return (
            f"This branch already carries {prior_commits} commit(s) from a prior attempt at "
            "this same issue -- an earlier run left them here instead of discarding them "
            "(e.g. it failed the test suite, or was denied a PR). Inspect what's already "
            "done with `git log` and `git diff`, keep what's good, and finish the job (fix a "
            "failing test, complete a partial implementation) rather than starting over.\n\n"
        )

    def _relevant_files(self, tree: Path, issue: Issue) -> str:
        # my-searcher's own CLAUDE.md documents this exact hand-off: "a
        # reusable 'which files matter here' step for later tools (MyGroomer,
        # MyCoder)". NoopEngine (the class default) keeps this free and
        # deterministic -- token-overlap pre-ranking only, no extra Engine
        # spend. comment=False: my-coder posts no issue comments of its own.
        result = Searcher(repo_path=tree, ledger=self.ledger, repo=self.repo_slug).rank(
            SearchIssue(number=issue.number, title=issue.title, body=issue.body or ""),
        )
        if not result.ranked:
            return ""
        listed = "\n".join(f"- `{p}`" for p in result.ranked[:15])
        return (
            "Files my-searcher ranked most relevant to this issue (start "
            "here, but you are not limited to these):\n" + listed + "\n\n"
        )

    def _research_context(self, issue: Issue) -> str:
        # Read-only fence, not a package import: my-researcher isn't designed
        # as a library call like my-searcher is, so this only reads its
        # already-published `kind=research` ledger entries (same convention
        # as MyTodo reading MyPlanner's ledger). Only ever finds anything when
        # a prior `myresearcher brief` ran against *this* repo's issues, since
        # that's the ledger this Coder was given.
        title_words = {w for w in issue.title.lower().split() if len(w) > 3}
        if not title_words:
            return ""
        blocks = []
        for entry in self.ledger.read(tool="myresearcher", kind="research"):
            if entry.outcome != "success":
                continue
            topic = str(entry.data.get("topic", ""))
            topic_words = {w for w in topic.lower().split() if len(w) > 3}
            if not (title_words & topic_words):
                continue
            summary = str(entry.data.get("summary", ""))[:1000]
            if summary:
                blocks.append(f"Prior research on {topic!r}:\n{summary}")
            if len(blocks) == 2:
                break
        return "\n\n".join(blocks) + "\n\n" if blocks else ""

    def _prompt(self, issue: Issue, tree: Path, *, prior_commits: int = 0) -> str:
        return _PROMPT.format(
            repo=self.repo_slug or self._repo_name(),
            number=issue.number,
            title=issue.title,
            body=issue.body or "(no description)",
            resume_note=self._resume_note(prior_commits),
            relevant_files=self._relevant_files(tree, issue),
            research_context=self._research_context(issue),
            conventions=self._conventions(tree),
            style_anchor=self._style_anchor(tree),
            blocked_sentinel=_BLOCKED_SENTINEL,
        )

    def _commit_count(self, tree: Path, base_sha: str) -> int:
        out = self._git(tree, ["rev-list", "--count", f"{base_sha}..HEAD"]).strip()
        return int(out or "0")

    def _changed_files(self, tree: Path, base_sha: str) -> list[str]:
        out = self._git(tree, ["diff", "--name-only", f"{base_sha}..HEAD"]).strip()
        return [line for line in out.splitlines() if line]

    def _existing_branch_ref(self, branch: str) -> str | None:
        # A prior run may have checkpointed commits on this issue's branch
        # without opening a PR (failed tests, a policy denial, a turn-capped
        # session). Fetching it here is how the next run recycles that work
        # instead of redoing it from origin/{base}. A non-zero exit means the
        # branch doesn't exist on origin yet -- an ordinary fresh run.
        try:
            self._git(self.repo, ["fetch", "origin", f"{branch}:refs/remotes/origin/{branch}"])
        except RuntimeError:
            return None
        return f"origin/{branch}"

    def _push(self, tree: Path, branch: str) -> str | None:
        try:
            self._git(tree, ["push", "-u", "origin", branch])
        except RuntimeError as exc:
            return str(exc)
        return None

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

    # Outcomes worth a fresh attempt: a checkpointed branch (needs_review) or
    # a session that left nothing durable (failure) both still have budget
    # left to try again. denied/no_changes/success/skipped/blocked are all a
    # considered stopping point, not a transient miss -- retrying them either
    # re-hits the same policy wall or burns money re-confirming a no-op.
    # blocked in particular waits on an external event (the blocker issue
    # closing), which this in-process attempt loop can't observe -- that's the
    # caller's job (e.g. the fleet dispatcher re-invoking once it's closed).
    _RETRYABLE = frozenset({"needs_review", "failure"})

    def run(self, issue_number: int) -> Result:
        issue = self.pick_issue(issue_number)
        if issue is None:
            detail = f"no open issue #{issue_number} in {self.repo_slug or self._repo_name()}"
            self._record("skipped", detail, issue=issue_number)
            return Result("skipped", detail, issue=issue_number)

        total_cost = 0.0
        result = None
        for attempt in range(1, self.max_attempts + 1):
            result = self._attempt(issue)
            total_cost += result.cost_usd
            done = result.outcome not in self._RETRYABLE or attempt == self.max_attempts
            done = done or total_cost >= self.max_total_budget_usd
            if done:
                break
        assert result is not None
        return replace(result, cost_usd=total_cost, attempts=attempt)

    def _attempt(self, issue: Issue) -> Result:
        branch = f"{TOOL}/{self._repo_name()}-{issue.number}"
        # Recycle a prior run's checkpointed commits instead of starting over:
        # if this issue's branch already exists on origin (a previous attempt
        # failed tests, was denied, or hit its turn cap), resume from its tip.
        base_ref = self._existing_branch_ref(branch) or f"origin/{self.base}"
        resuming = base_ref != f"origin/{self.base}"

        with self._workspace(self.repo, base_ref=base_ref) as tree:
            # Name the detached worktree HEAD before the session runs so every
            # commit it makes lands on this branch (local-only, no side effect).
            self._git(tree, ["checkout", "-B", branch])
            # merge-base with origin/{base}, not rev-parse HEAD: when resuming,
            # HEAD already carries the prior run's commits, and those must
            # still count as durable work even if this session adds nothing.
            base_sha = self._git(tree, ["merge-base", "HEAD", f"origin/{self.base}"]).strip()
            prior_commits = self._commit_count(tree, base_sha) if resuming else 0

            session = self.session_runner.run(
                prompt=self._prompt(issue, tree, prior_commits=prior_commits),
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

            # An explicit blocker signal wins over everything else: the model
            # chose to pause on a cross-repo dependency rather than thrash,
            # which is a distinct outcome from failing or a plain no-op --
            # checked before the commit-count branches below so a blocked run
            # with zero commits (it only filed an issue elsewhere) isn't
            # mistaken for no_changes/failure.
            blocker = _parse_blocker(session.final_message)
            if blocker is not None:
                detail = f"paused on cross-repo blocker {blocker}"
                files = self._changed_files(tree, base_sha) if commits > 0 else []
                data: dict[str, object] = {"blocker": blocker, "files_touched": files, **common}
                if commits > 0 and self._push(tree, branch) is None:
                    data["branch"] = branch
                self._record("blocked", detail, **data)
                return Result(
                    "blocked",
                    detail,
                    issue=issue.number,
                    files_touched=files,
                    cost_usd=session.cost_usd,
                    blocker=blocker,
                )

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
                base_detail = f"generated code for #{issue.number} failed the test suite"
                push_error = self._push(tree, branch)
                if push_error is not None:
                    # Nothing durable reached origin either -- this really is a
                    # loss, not a checkpoint.
                    detail = f"{base_detail}; checkpoint push also failed: {push_error}"
                    self._record(
                        "failure", detail, files_touched=files, tests_passed=False, **common
                    )
                    return Result(
                        "failure",
                        detail,
                        issue=issue.number,
                        files_touched=files,
                        tests_passed=False,
                        cost_usd=session.cost_usd,
                    )
                detail = (
                    f"{base_detail}; branch {branch} pushed as a checkpoint -- "
                    "re-run to resume and fix"
                )
                self._record(
                    "needs_review",
                    detail,
                    files_touched=files,
                    tests_passed=False,
                    branch=branch,
                    **common,
                )
                return Result(
                    "needs_review",
                    detail,
                    issue=issue.number,
                    files_touched=files,
                    tests_passed=False,
                    cost_usd=session.cost_usd,
                )
            tests_passed: bool | None = True if self.run_tests else None

            gate = self.policy.evaluate(
                Action(
                    kind=PR_ACTION_KIND,
                    payload={
                        "repo": self.repo_slug or self._repo_name(),
                        "issue": issue.number,
                        "branch": branch,
                        "command": f"gh pr create --head {branch}",
                    },
                )
            )
            if gate.under(unattended=in_github_actions()) is not Decision.ALLOW:
                detail = f"policy blocked the PR for #{issue.number}: {gate.reason or gate.rule}"
                data: dict[str, object] = {"files_touched": files, **common}
                # Checkpoint even a policy denial, best-effort: the commits are
                # otherwise thrown away with the worktree on the way out.
                if self._push(tree, branch) is None:
                    data["branch"] = branch
                    detail = f"{detail} (commits checkpointed on {branch})"
                self._record("denied", detail, **data)
                return Result(
                    "denied",
                    detail,
                    issue=issue.number,
                    files_touched=files,
                    cost_usd=session.cost_usd,
                )

            # Push the durable commits regardless of how the session ended, so a
            # turn-capped or timed-out session's real work is never discarded.
            push_error = self._push(tree, branch)
            if push_error is not None:
                detail = f"commits present but push failed for #{issue.number}: {push_error}"
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
