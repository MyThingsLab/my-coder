from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from myguard.guard import Guard
from myguard.rules import Rule
from mysearcher.searcher import Issue as SearchIssue
from mysearcher.searcher import Searcher
from mythings import _secrets
from mythings.github import GitHub, Issue
from mythings.isolation import Workspace, in_github_actions
from mythings.ledger import Ledger
from mythings.policy import ALLOW, Action, Decision, Policy, PolicyResult

from mycoder.session import SessionRunner, billed

PR_ACTION_KIND = "draft-pr-create"

TOOL = "mycoder"
LEDGER_KIND = "code"
BACKLOG_LABEL = "my-coder"  # my-coder's own bugs; target issues arrive via --issue

_BLOCKED_SENTINEL = "FLEET-DISPATCH-BLOCKED:"

FLEET_ORG = "MyThingsLab"

# The blocker + critical-bug escapes only make sense inside the fleet: they file
# into sibling repos and the `critical` label halts dispatch org-wide. Pointed at
# a repo outside the org (`--repo someone/theirs`), that is a cross-org side
# effect a target repo never consented to, so the escapes are withheld entirely
# rather than retargeted -- there is no sibling repo set to file against.
_FLEET_PROTOCOL = """\
If this issue turns out to be blocked by a missing capability in ANOTHER \
{org} repo (a contract, helper, or fix that repo must land first), do \
not thrash against it. Use `gh issue create --repo {org}/<repo>` to file \
a precise issue describing exactly what that repo must add and why, then END \
your run by printing one final line, exactly:
  {blocked_sentinel} {org}/<repo>#<number>
naming the issue you just filed. That records the dependency so this issue is \
paused, not failed, until the blocker is resolved.

If while working you discover a SEPARATE bug that is a security issue or \
breaks a core invariant shared across the fleet (a `my-things-core` contract, \
the build harness, or anything that would let other tools ship broken work on \
top of it), file it immediately with `gh issue create --label critical --label \
bug --repo {org}/<repo>` describing exactly what's broken and its blast \
radius. That label halts new fleet dispatch org-wide until it's closed — do \
not wait until you finish this task to file it. Filing it does not abort your \
own work; keep going on this issue unless the critical bug blocks it directly, \
in which case treat it as a blocker per the paragraph above.
"""

_OUT_OF_FLEET_PROTOCOL = """\
This repo is outside the {org} fleet. You have no authority to file issues, \
open pull requests, or make any change anywhere but this checkout. If this \
issue turns out to be blocked by something you cannot fix here, do not thrash \
against it and do not file anything elsewhere: stop, leave whatever partial \
work is genuinely correct committed, and END your run by explaining precisely \
what blocks it.
"""

# Fleet house style, asserted only when the target repo states no style of its
# own. A repo with a CLAUDE.md has already been told its conventions are
# "authoritative here"; repeating `from __future__ import annotations` and
# mandatory type hints underneath that contradicts it.
_FLEET_STYLE_RULE = """\
- Match the conventions of the existing code shown above: module layout, import
  style (e.g. `from __future__ import annotations`), type hints on EVERY
  signature (test functions included), naming, and the existing test style. When
  in doubt, imitate the nearest existing file rather than inventing a style."""

_TARGET_STYLE_RULE = """\
- Follow the target-repo conventions quoted above; they win over any habit of
  yours. For anything they leave unsaid, imitate the nearest existing file in
  the repo rather than inventing a style."""

_PROMPT = """\
You are MyCoder, the {org} fleet's worker. Close this one GitHub issue in \
{repo} by editing files in the current checkout.

Issue #{number}: {title}

{body}

{resume_note}\
{context_pack}\
{relevant_files}\
{research_context}\
{fleet_context}\
Target-repo conventions (its own AGENTS.md / CLAUDE.md / HARNESS.md, authoritative here):
{conventions}

{style_anchor}

You are running fully non-interactively, as a headless session: no human is \
watching and no one can approve a permission prompt. If a command is denied, \
do NOT ask for approval or wait for it — it will never come. Work only with \
the tools you already have.

{protocol}
Rules:
- Make the smallest change that fully closes the issue, with tests.
{style_rule}
- Run the repo's own test suite and linter; leave them green. If its
  dependencies are missing from this checkout, install them first (the repo's
  own declared dependencies only) and then run the suite.
- Commit as you go, not once at the end. You are on a wall clock and may be
  killed mid-run; anything uncommitted at that moment is unverified work
  someone else has to review. Commit each coherent step as soon as it stands
  on its own, even before the suite is green.
- Commit your work with git and a clear message. Do NOT run `git push`{gh_rule}
- Stay entirely within this repo's checkout; never touch another repo.
"""

_FLEET_GH_RULE = """, and do \
NOT use any `gh` command other than `gh issue create` for a blocker/critical bug \
above — MyCoder pushes the branch and opens the PR."""

_OUT_OF_FLEET_GH_RULE = """, and do NOT use `gh` at all — \
MyCoder pushes the branch and opens the PR."""

# Generated, vendored or provenance directories: present in the tree but never
# evidence of the repo's house style. Everything else counts, whatever the
# layout -- requiring a `src/` package silently blanked the anchor on every
# flat-layout repo (my-coder#13).
_ANCHOR_NOISE = frozenset(
    {
        ".venv",
        "venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "vendor",
        "third_party",
        "migrations",
        "dev-ledger",
        ".mythings",
    }
)


def _is_anchor_noise(rel: str) -> bool:
    return any(part in _ANCHOR_NOISE for part in Path(rel).parts)


def default_test_command() -> list[str]:
    # "python" is absent on any distro without the python-is-python3 shim -- and
    # a missing interpreter used to surface as FileNotFoundError out of the run
    # rather than as a failing suite (my-coder#12). Resolve one that exists;
    # sys.executable is the last resort because in a target-repo worktree it is
    # my-coder's own interpreter, which has the fleet's dependencies, not the
    # target's.
    for name in ("python3", "python"):
        if shutil.which(name):
            return [name, "-m", "pytest", "-q"]
    return [sys.executable, "-m", "pytest", "-q"]


class _AllowAll:
    # Default gate for the one side effect (opening a PR). The fleet driver injects
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
                "opens a PR (ready when tests passed, draft otherwise)",
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
    # Summed across every attempt, not just the last. An attempt whose real cost
    # could not be recovered (a timeout) contributes its budget cap here rather
    # than nothing, so this is a floor on spend, never an under-count.
    cost_usd: float = 0.0
    cost_known: bool = True  # False once any attempt's true cost was unrecoverable
    attempts: int = 1
    blocker: str | None = None  # "<org>/<repo>#<n>" when outcome == "blocked"
    failing_tests: list[str] = field(default_factory=list)
    failure_trace: str = ""


@dataclass(frozen=True)
class TestResult:
    ok: bool
    error: str | None = None
    failing_tests: list[str] = field(default_factory=list)
    failure_trace: str = ""


def _parse_test_failures(stdout: str, stderr: str) -> tuple[list[str], str]:
    failing_tests: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("FAILED "):
            parts = line.split()
            if len(parts) >= 2:
                node = parts[1]
                if node not in failing_tests:
                    failing_tests.append(node)

    trace_lines = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("E   ") or "AssertionError" in stripped or "Error:" in stripped:
            trace_lines.append(stripped)

    if trace_lines:
        failure_trace = "\n".join(trace_lines[:15])[:1000]
    else:
        combined = (stdout + "\n" + stderr).strip()
        failure_trace = (
            combined[-1000:] if combined else "Test command exited non-zero with no output"
        )

    return failing_tests, failure_trace


def _prune_python_exemplar(source: str, *, max_chars: int = 2000) -> str:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return source[:max_chars]

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.body = [ast.Expr(value=ast.Constant(value=Ellipsis))]
        elif isinstance(node, ast.ClassDef):
            new_body = []
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    item.body = [ast.Expr(value=ast.Constant(value=Ellipsis))]
                    new_body.append(item)
                elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                    new_body.append(item)
            node.body = new_body or [ast.Pass()]

    try:
        pruned = ast.unparse(tree)
    except Exception:
        return source[:max_chars]

    if not pruned.strip():
        return source[:max_chars]
    return pruned[:max_chars]


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
        self.test_command = test_command or default_test_command()
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
        for name in ("AGENTS.md", "GEMINI.md", "CLAUDE.md"):
            path = tree / name
            if path.exists():
                parts.append(f"--- {name} ---\n{path.read_text(encoding='utf-8')}")
                break
        harness = tree / "HARNESS.md"
        if harness.exists():
            parts.append(f"--- HARNESS.md ---\n{harness.read_text(encoding='utf-8')}")
        return "\n\n".join(parts) if parts else "(no AGENTS.md/CLAUDE.md/HARNESS.md found)"

    def _style_anchor(self, tree: Path, *, max_files: int = 3, max_chars: int = 2000) -> str:
        # A repo without a CLAUDE.md still has a house style in its existing
        # code; show the session that code so it matches conventions (type
        # hints, imports, test shape) the first time instead of guessing — the
        # single biggest source of review-only polish on generated PRs.
        # Exemplar files are pruned via AST to preserve imports, class definitions,
        # and function signatures while stripping implementation bodies, drastically
        # cutting token spend. Largest files first: more content is a stronger signal.
        try:
            listed = [p for p in self._git(tree, ["ls-files"]).splitlines() if p]
        except RuntimeError:
            return ""
        exemplars = sorted(
            (p for p in listed if p.endswith(".py") and not _is_anchor_noise(p)),
            key=lambda p: (tree / p).stat().st_size if (tree / p).is_file() else 0,
            reverse=True,
        )[:max_files]
        blocks = []
        for rel in exemplars:
            path = tree / rel
            if path.is_file():
                raw = path.read_text(encoding="utf-8")
                pruned = _prune_python_exemplar(raw, max_chars=max_chars)
                blocks.append(f"--- {rel} ---\n{pruned}")
        if not blocks:
            return "Existing code: (none yet — this is an early/greenfield repo)."
        tree_view = "\n".join(listed[:300])
        return (
            "Existing code in this repo (match its conventions exactly):\n\n"
            f"Repository files:\n{tree_view}\n\n"
            "Representative existing files (signatures and style):\n\n" + "\n\n".join(blocks)
        )

    def _last_attempt_diagnostic(self, issue_number: int) -> dict[str, object] | None:
        entries = [
            e
            for e in self.ledger
            if e.tool == TOOL and e.kind == LEDGER_KIND and e.data.get("issue") == issue_number
        ]
        if not entries:
            return None
        last = entries[-1]
        return {
            "outcome": last.outcome,
            "detail": last.detail,
            "failing_tests": last.data.get("failing_tests", []),
            "failure_trace": last.data.get("failure_trace", ""),
            "final_message": last.data.get("final_message", ""),
        }

    def _resume_note(
        self,
        prior_commits: int,
        *,
        diff_stat: str = "",
        diagnostic: dict[str, object] | None = None,
    ) -> str:
        if prior_commits == 0:
            return ""
        parts = [
            f"This branch already carries {prior_commits} commit(s) from a prior attempt at "
            "this same issue -- an earlier run left them here instead of discarding them "
            "(e.g. it failed the test suite, or was denied a PR)."
        ]
        if diagnostic:
            outcome = diagnostic.get("outcome")
            if outcome:
                parts.append(f"Prior attempt outcome: {outcome}.")
            failing = diagnostic.get("failing_tests")
            if isinstance(failing, list) and failing:
                failing_str = ", ".join(f"`{t}`" for t in failing)
                parts.append(f"- Failing test(s): {failing_str}")
            trace = diagnostic.get("failure_trace")
            if trace:
                parts.append(f"- Error summary:\n```\n{trace}\n```")
            final_msg = diagnostic.get("final_message")
            if final_msg:
                parts.append(f"- Prior worker note: {final_msg}")

        if diff_stat:
            parts.append(f"Files already modified:\n```\n{diff_stat}\n```")

        if diagnostic and (diagnostic.get("failing_tests") or diagnostic.get("failure_trace")):
            parts.append(
                "⚠️ Do NOT start over from scratch or repeat exploratory commands. "
                "Inspect the diff and test failure above, fix the root cause directly, "
                "and ensure tests pass."
            )
        else:
            parts.append(
                "Inspect what's already done with `git log` and `git diff`, keep what's good, "
                "and finish the job (fix a failing test, complete a partial implementation) "
                "rather than starting over."
            )
        return "\n\n".join(parts) + "\n\n"

    def _agent_context_pack(self, tree: Path, issue: Issue) -> str:
        """Attempt to extract an Agent Context Pack (ACP) from the deterministic codebase graph."""
        try:
            from mythings.graph import (
                CodebaseGraph,
                MarkdownExtractor,
                PythonAstExtractor,
                render_context_pack,
            )
        except ImportError:
            return ""

        cached_db = tree / ".mythings" / "graph.sqlite"
        if cached_db.exists():
            graph = CodebaseGraph(cached_db)
        else:
            graph = CodebaseGraph.in_memory()
            try:
                PythonAstExtractor(repo_root=tree).index_repo(graph)
                MarkdownExtractor(repo_root=tree).index_docs(graph)
            except Exception:
                return ""

        text = f"{issue.title} {issue.body or ''}"
        words = set(re.findall(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b", text))
        matched_symbols = []
        for w in words:
            found = graph.find_symbols(w)
            for s in found:
                if s.kind in ("function", "method", "class"):
                    matched_symbols.append(s)

        if not matched_symbols:
            return ""

        title_lower = issue.title.lower()
        matched_symbols.sort(
            key=lambda s: (
                s.name.lower() in title_lower,
                s.kind != "class",
                -(s.end_line or 0) + (s.start_line or 0),
            ),
            reverse=True,
        )

        primary_symbol = matched_symbols[0]
        try:
            acp = render_context_pack(graph, primary_symbol.id, repo_root=tree)
            return (
                "## Deterministic Agent Context Pack (Grounded Focus & Blast Radius)\n\n"
                f"{acp}\n\n"
            )
        except Exception:
            return ""

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

    def in_fleet(self) -> bool:
        # No slug at all means a bare local invocation inside the fleet's own
        # workspace, which is how every existing caller runs; only an explicit
        # out-of-org slug withholds the cross-repo escapes.
        if not self.repo_slug:
            return True
        return self.repo_slug.split("/")[0] == FLEET_ORG

    def _fleet_context(self) -> str:
        ctx = os.environ.get("MYTHINGS_FLEET_CONTEXT", "").strip()
        return f"{ctx}\n\n" if ctx else ""

    def _prompt(
        self,
        issue: Issue,
        tree: Path,
        *,
        prior_commits: int = 0,
        diff_stat: str = "",
        diagnostic: dict[str, object] | None = None,
    ) -> str:
        fleet = self.in_fleet()
        conventions = self._conventions(tree)
        protocol = (
            _FLEET_PROTOCOL.format(org=FLEET_ORG, blocked_sentinel=_BLOCKED_SENTINEL)
            if fleet
            else _OUT_OF_FLEET_PROTOCOL.format(org=FLEET_ORG)
        )
        stated_own_style = not conventions.startswith("(no ")
        return _PROMPT.format(
            org=FLEET_ORG,
            repo=self.repo_slug or self._repo_name(),
            number=issue.number,
            title=issue.title,
            body=issue.body or "(no description)",
            resume_note=self._resume_note(
                prior_commits, diff_stat=diff_stat, diagnostic=diagnostic
            ),
            context_pack=self._agent_context_pack(tree, issue),
            relevant_files=self._relevant_files(tree, issue),
            research_context=self._research_context(issue),
            fleet_context=self._fleet_context(),
            conventions=conventions,
            style_anchor=self._style_anchor(tree),
            protocol=protocol,
            style_rule=_TARGET_STYLE_RULE if stated_own_style else _FLEET_STYLE_RULE,
            gh_rule=_FLEET_GH_RULE if fleet else _OUT_OF_FLEET_GH_RULE,
        )

    def _commit_count(self, tree: Path, base_sha: str) -> int:
        out = self._git(tree, ["rev-list", "--count", f"{base_sha}..HEAD"]).strip()
        return int(out or "0")

    def _changed_files(self, tree: Path, base_sha: str) -> list[str]:
        out = self._git(tree, ["diff", "--name-only", f"{base_sha}..HEAD"]).strip()
        return [line for line in out.splitlines() if line]

    def _is_dirty(self, tree: Path) -> bool:
        return bool(self._git(tree, ["status", "--porcelain"]).strip())

    def _salvage(self, tree: Path, issue: Issue) -> bool:
        # A session killed by the wall clock has usually made its edits and not
        # reached `git commit` -- it commits last, after the suite is green.
        # Those edits used to die with the worktree (my-coder#19). Bank them as
        # a checkpoint so the next attempt resumes instead of restarting.
        # Environment directories are excluded explicitly rather than trusted to
        # the target's .gitignore: a session may well have built a multi-GB venv
        # in here, and this commit gets pushed.
        try:
            self._git(
                tree,
                [
                    "add",
                    "-A",
                    "--",
                    ".",
                    ":(exclude).venv",
                    ":(exclude)venv",
                    ":(exclude)node_modules",
                    ":(exclude)__pycache__",
                ],
            )
            staged = self._git(tree, ["diff", "--cached", "--name-only"]).strip()
            if not staged:
                return False
            self._git(
                tree,
                [
                    "commit",
                    "-m",
                    f"Checkpoint uncommitted work on #{issue.number}\n\n"
                    "Salvaged by MyCoder: the session ended before committing. This is "
                    "unverified work-in-progress, not a session's own considered commit.",
                ],
            )
        except RuntimeError:
            return False
        return True

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

    def _scan_diff_for_secrets(self, tree: Path, branch: str) -> list[_secrets.Finding]:
        # session.py's redact_secrets scrubs the transcript, not the commits: a
        # session can still write a real credential straight into a file. This
        # is the last chance to catch it, after the branch is safely pushed
        # (so the checkpoint isn't lost) but before it becomes a public PR.
        diff_text = self._git(tree, ["diff", "-U0", f"origin/{self.base}...{branch}"])
        return _secrets.scan_text(_secrets.added_lines(diff_text))

    def _tests_pass(self, tree: Path) -> TestResult:
        # A test command that cannot even be launched (no such interpreter, not
        # executable) is an operator misconfiguration, not a failing suite. It
        # used to raise out of _attempt and abort the run with a traceback,
        # discarding the session's committed work; report it as a verdict with
        # its own reason instead (my-coder#12).
        try:
            proc = subprocess.run(self.test_command, cwd=str(tree), capture_output=True, text=True)
        except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
            return TestResult(
                ok=False,
                error=f"could not run test command {' '.join(self.test_command)!r}: {exc}",
            )
        if proc.returncode == 0:
            return TestResult(ok=True)
        failing_tests, failure_trace = _parse_test_failures(proc.stdout, proc.stderr)
        return TestResult(
            ok=False,
            error=None,
            failing_tests=failing_tests,
            failure_trace=failure_trace,
        )

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
        cost_known = True
        result = None
        for attempt in range(1, self.max_attempts + 1):
            result = self._attempt(issue)
            # `_attempt` already substitutes the budget cap for an unrecoverable
            # cost, so this sum is a floor on real spend. Before that, a timeout
            # added 0.0 here and `_RETRYABLE` includes `failure` -- so a
            # candidate that timed out every time retried forever against a
            # ceiling that believed nothing had been spent (my-coder#18).
            total_cost += result.cost_usd
            cost_known = cost_known and result.cost_known
            done = result.outcome not in self._RETRYABLE or attempt == self.max_attempts
            done = done or total_cost >= self.max_total_budget_usd
            if done:
                break
        assert result is not None
        return replace(result, cost_usd=total_cost, cost_known=cost_known, attempts=attempt)

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
            diff_stat = (
                self._git(tree, ["diff", "--stat", f"origin/{self.base}..HEAD"]).strip()
                if resuming
                else ""
            )
            diagnostic = self._last_attempt_diagnostic(issue.number) if resuming else None

            session = self.session_runner.run(
                prompt=self._prompt(
                    issue,
                    tree,
                    prior_commits=prior_commits,
                    diff_stat=diff_stat,
                    diagnostic=diagnostic,
                ),
                cwd=tree,
                max_budget_usd=self.max_budget_usd,
                max_turns=self.max_turns,
                timeout_s=self.session_timeout_s,
            )
            # A killed session reports no price at all. Meter it as the cap it
            # was allowed to spend rather than as zero, so the retry loop and
            # every ceiling above it see a floor on the real spend (my-coder#18).
            billed_cost = billed(session.cost_usd, self.max_budget_usd)

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
                # The metered figure, plus an explicit flag saying whether it is
                # the real one. A reader of this record must never mistake an
                # assumed cap for a measured price.
                "cost_usd": billed_cost,
                "cost_known": session.cost_usd is not None,
                "tokens": session.tokens,
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
                    cost_usd=billed_cost,
                    cost_known=session.cost_usd is not None,
                    blocker=blocker,
                )

            salvaged = False
            if commits == 0 and self._is_dirty(tree):
                salvaged = self._salvage(tree, issue)
                commits = self._commit_count(tree, base_sha) if salvaged else commits

            if commits == 0:
                # Nothing durable to keep: an errored session that committed
                # nothing is a real failure; a clean one is an honest no-op.
                if session.ok:
                    outcome, detail = "no_changes", f"session left no commit for #{issue.number}"
                else:
                    outcome = "failure"
                    detail = f"session failed for #{issue.number} with no commit: {session.error}"
                self._record(outcome, detail, **common)
                return Result(
                    outcome,
                    detail,
                    issue=issue.number,
                    cost_usd=billed_cost,
                    cost_known=session.cost_usd is not None,
                )

            files = self._changed_files(tree, base_sha)

            if salvaged:
                # Never a PR: nothing here passed the session's own review, let
                # alone a test run. Push it so the next attempt resumes from it.
                base_detail = (
                    f"session ended without committing ({session.error or 'no error reported'}); "
                    f"uncommitted work salvaged for #{issue.number}"
                )
                push_error = self._push(tree, branch)
                if push_error is not None:
                    detail = f"{base_detail}; checkpoint push also failed: {push_error}"
                    self._record("failure", detail, files_touched=files, **common)
                    return Result(
                        "failure",
                        detail,
                        issue=issue.number,
                        files_touched=files,
                        cost_usd=billed_cost,
                        cost_known=session.cost_usd is not None,
                    )
                detail = f"{base_detail} onto {branch} -- re-run to resume and finish"
                self._record(
                    "needs_review",
                    detail,
                    files_touched=files,
                    branch=branch,
                    salvaged=True,
                    **common,
                )
                return Result(
                    "needs_review",
                    detail,
                    issue=issue.number,
                    files_touched=files,
                    cost_usd=billed_cost,
                    cost_known=session.cost_usd is not None,
                )

            test_res = self._tests_pass(tree) if self.run_tests else TestResult(ok=True)
            if not test_res.ok:
                base_detail = test_res.error or (
                    f"generated code for #{issue.number} failed the test suite"
                )
                if test_res.failing_tests:
                    base_detail += f" ({', '.join(test_res.failing_tests)})"
                push_error = self._push(tree, branch)
                if push_error is not None:
                    # Nothing durable reached origin either -- this really is a
                    # loss, not a checkpoint.
                    detail = f"{base_detail}; checkpoint push also failed: {push_error}"
                    self._record(
                        "failure",
                        detail,
                        files_touched=files,
                        tests_passed=False,
                        failing_tests=test_res.failing_tests,
                        failure_trace=test_res.failure_trace,
                        **common,
                    )
                    return Result(
                        "failure",
                        detail,
                        issue=issue.number,
                        files_touched=files,
                        tests_passed=False,
                        cost_usd=billed_cost,
                        cost_known=session.cost_usd is not None,
                        failing_tests=test_res.failing_tests,
                        failure_trace=test_res.failure_trace,
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
                    failing_tests=test_res.failing_tests,
                    failure_trace=test_res.failure_trace,
                    **common,
                )
                return Result(
                    "needs_review",
                    detail,
                    issue=issue.number,
                    files_touched=files,
                    tests_passed=False,
                    cost_usd=billed_cost,
                    cost_known=session.cost_usd is not None,
                    failing_tests=test_res.failing_tests,
                    failure_trace=test_res.failure_trace,
                )
            tests_passed: bool | None = True if self.run_tests else None
            # Ready when the suite actually ran and passed in this worktree;
            # draft otherwise. CI skips required checks on a draft, so a PR born
            # as a draft can never show a green check -- and the fleet's
            # promotion gate used to read that skip as a pass and promote on it
            # (my-fleet#32). Opening ready is what makes CI run at all; the
            # human merge is the gate, not the promotion.
            #
            # `tests_passed is None` means --run-tests was off, so nothing was
            # verified here. That stays a draft: unverified work should not
            # present itself as reviewable.
            verified = tests_passed is True

            gate = self.policy.evaluate(
                Action(
                    kind=PR_ACTION_KIND,
                    payload={
                        "repo": self.repo_slug or self._repo_name(),
                        "issue": issue.number,
                        "branch": branch,
                        # The human approving this over the ASK channel is
                        # approving a *reviewable* PR when verified, not a
                        # draft. Say which, rather than letting the rule's
                        # static description speak for both.
                        "draft": not verified,
                        "command": f"gh pr create --head {branch}"
                        + ("" if verified else " --draft"),
                    },
                )
            )
            decision = gate.under(unattended=in_github_actions())
            if decision is not Decision.ALLOW:
                # A DENY is a human saying no. A surviving ASK is nobody having
                # *been* asked: Guard resolves an ASK only when MYTHINGS_ASK_CMD
                # names a channel, so an unarmed run arrives here with the ASK
                # intact. Reporting both as "denied" made a missing channel
                # indistinguishable from a refusal, and three paid sessions were
                # discarded before anyone noticed (#29).
                if decision is Decision.ASK:
                    outcome = "needs_human"
                    detail = (
                        f"no ask channel to approve the PR for #{issue.number}: "
                        f"{gate.reason or gate.rule}. Nobody was asked — set "
                        "MYTHINGS_ASK_CMD, or run under fleet_dispatch"
                    )
                else:
                    outcome = "denied"
                    detail = (
                        f"policy blocked the PR for #{issue.number}: {gate.reason or gate.rule}"
                    )
                data: dict[str, object] = {"files_touched": files, **common}
                # Checkpoint either way, best-effort: the commits are otherwise
                # thrown away with the worktree on the way out.
                if self._push(tree, branch) is None:
                    data["branch"] = branch
                    detail = f"{detail} (commits checkpointed on {branch})"
                self._record(outcome, detail, **data)
                return Result(
                    outcome,
                    detail,
                    issue=issue.number,
                    files_touched=files,
                    cost_usd=billed_cost,
                    cost_known=session.cost_usd is not None,
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
                    cost_usd=billed_cost,
                    cost_known=session.cost_usd is not None,
                )

            # Open the PR only when the session finished cleanly. A session
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
                    cost_usd=billed_cost,
                    cost_known=session.cost_usd is not None,
                )

            secret_findings = self._scan_diff_for_secrets(tree, branch)
            if secret_findings:
                patterns = sorted({f.pattern for f in secret_findings})
                self.ledger.record(
                    TOOL,
                    "secret_alert",
                    "blocked",
                    f"blocked the PR for #{issue.number}: possible secret(s) in the diff",
                    issue=issue.number,
                    patterns=patterns,
                )
                detail = (
                    f"branch {branch} pushed for #{issue.number}, no PR — the diff contains "
                    f"possible secret(s) ({', '.join(patterns)}); scrub the branch and re-run"
                )
                self._record("needs_review", detail, files_touched=files, branch=branch, **common)
                return Result(
                    "needs_review",
                    detail,
                    issue=issue.number,
                    files_touched=files,
                    tests_passed=tests_passed,
                    cost_usd=billed_cost,
                    cost_known=session.cost_usd is not None,
                )

            pr = self.github.open_pr(
                title=issue.title,
                body=self._pr_body(issue, files),
                base=self.base,
                head=branch,
                draft=not verified,
            )

        kind = "PR" if verified else "draft PR"
        self._record(
            "success",
            f"opened {kind} #{pr.number} for #{issue.number}",
            pr=pr.number,
            files_touched=files,
            tests_passed=tests_passed,
            pr_url=pr.url,
            draft=not verified,
            **common,
        )
        return Result(
            "success",
            f"opened {kind} #{pr.number}",
            issue=issue.number,
            pr=pr.number,
            files_touched=files,
            tests_passed=tests_passed,
            cost_usd=billed_cost,
            cost_known=session.cost_usd is not None,
        )
