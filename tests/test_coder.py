from __future__ import annotations

import json
import subprocess
from pathlib import Path

from mythings.github import GitHub
from mythings.ledger import Ledger
from mythings.policy import Action, Decision, PolicyResult
from mythings.testing import FakeGh, make_git_repo

from mycoder.coder import Coder
from mycoder.session import NoopSessionRunner, SessionResult

SLUG = "MyThingsLab/my-raytracer"


class FakeSessionRunner:
    # Stands in for a real headless session: writes the given files into the
    # worktree and (optionally) commits them, so the mechanical path around the
    # session runs against a real git worktree without shelling out to `claude`.
    def __init__(
        self,
        files: dict[str, str] | None = None,
        *,
        ok: bool = True,
        commit: bool = True,
        leaked: list[str] | None = None,
        error: str | None = None,
        transcript: str = "",
        final_message: str = "done",
    ) -> None:
        self.files = files or {}
        self.ok = ok
        self.commit = commit
        self.leaked = leaked or []
        self.error = error
        self.transcript = transcript
        self.final_message = final_message
        self.calls: list[str] = []

    def run(
        self,
        *,
        prompt: str,
        cwd: Path,
        max_budget_usd: float,
        max_turns: int,
        timeout_s: float,
    ) -> SessionResult:
        self.calls.append(prompt)
        # Write + commit first (a real session that hits its turn cap has still
        # done durable work), then report ok/not-ok independently.
        for rel, content in self.files.items():
            target = Path(cwd) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if self.files and self.commit:
            subprocess.run(["git", "-C", str(cwd), "add", "-A"], check=True, capture_output=True)
            subprocess.run(
                ["git", "-C", str(cwd), "commit", "-m", "session work"],
                check=True,
                capture_output=True,
            )
        if not self.ok:
            return SessionResult(
                ok=False,
                error=self.error or "claude exited 1",
                leaked=self.leaked,
                transcript=self.transcript,
            )
        return SessionResult(
            ok=True,
            turns=3,
            cost_usd=0.01,
            final_message=self.final_message,
            leaked=self.leaked,
            transcript=self.transcript,
        )


class SequencedSessionRunner:
    # Stands in for a session whose behavior differs attempt to attempt (e.g.
    # a retry that actually fixes what the first attempt got wrong). `steps`
    # is one files-dict per call; the last step repeats if called more times
    # than it has steps for.
    def __init__(self, steps: list[dict[str, str]], *, cost_usd: float = 0.05) -> None:
        self.steps = steps
        self.cost_usd = cost_usd
        self.calls: list[str] = []
        self._i = 0

    def run(
        self,
        *,
        prompt: str,
        cwd: Path,
        max_budget_usd: float,
        max_turns: int,
        timeout_s: float,
    ) -> SessionResult:
        self.calls.append(prompt)
        files = self.steps[min(self._i, len(self.steps) - 1)]
        self._i += 1
        for rel, content in files.items():
            target = Path(cwd) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        subprocess.run(["git", "-C", str(cwd), "add", "-A"], check=True, capture_output=True)
        # A repeated step (a stuck session re-shown the same state) leaves
        # nothing new to commit; a real session in that position wouldn't
        # force an empty commit either.
        staged = subprocess.run(
            ["git", "-C", str(cwd), "diff", "--cached", "--name-only"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if staged.strip():
            subprocess.run(
                ["git", "-C", str(cwd), "commit", "-m", "session work"],
                check=True,
                capture_output=True,
            )
        return SessionResult(ok=True, turns=3, cost_usd=self.cost_usd, final_message="done")


def _issue(number: int, title: str, body: str = "") -> str:
    return json.dumps(
        [{"number": number, "title": title, "body": body, "url": f"u/{number}", "labels": []}]
    )


def _github(gh: FakeGh) -> GitHub:
    return GitHub(SLUG, runner=gh)


def _coder(repo_path, gh, ledger_path, runner, **kwargs) -> Coder:
    return Coder(
        repo=repo_path,
        repo_slug=SLUG,
        github=_github(gh),
        ledger=Ledger(ledger_path),
        session_runner=runner,
        **kwargs,
    )


def test_build_commits_and_opens_a_draft_pr(tmp_path, clean_git_env, attended_env):
    repo = make_git_repo(tmp_path, files={"README.md": "# r\n"})
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "add greet"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/7",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/greet.py": "def greet():\n    return 'hi'\n"})
    result = _coder(repo.path, gh, ledger_path, runner).run(issue_number=5)

    assert result.outcome == "success"
    assert result.pr == 7
    assert "pkg/greet.py" in result.files_touched
    # The draft PR really carries the session's commit, verified from the origin.
    assert "def greet()" in repo.read_committed("mycoder/my-raytracer-5", "pkg/greet.py")
    create = next(c for c in gh.calls if c[:2] == ["pr", "create"])
    assert "--draft" in create
    entries = list(Ledger(ledger_path))
    assert any(e.kind == "code" and e.outcome == "success" and e.data["pr"] == 7 for e in entries)


def test_build_no_changes_when_session_commits_nothing(tmp_path, clean_git_env, attended_env):
    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "noop")})
    ledger_path = tmp_path / "ledger.jsonl"
    result = _coder(repo.path, gh, ledger_path, NoopSessionRunner()).run(issue_number=5)

    assert result.outcome == "no_changes"
    assert not gh.saw("pr", "create")
    assert any(e.outcome == "no_changes" for e in Ledger(ledger_path))


def test_build_skips_when_issue_is_absent(tmp_path, clean_git_env, attended_env):
    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "other")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"x.py": "x = 1\n"})
    result = _coder(repo.path, gh, ledger_path, runner).run(issue_number=99)

    assert result.outcome == "skipped"
    assert runner.calls == []  # the session is never launched
    assert not gh.saw("pr", "create")


def test_build_blocked_when_session_reports_a_cross_repo_blocker(
    tmp_path, clean_git_env, attended_env
):
    # The model chose to pause on a missing capability elsewhere rather than
    # thrash -- committed nothing itself, just filed an issue and printed the
    # sentinel. Must read as "blocked", never "no_changes" or "failure".
    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "needs a core fix first")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(
        commit=False,
        final_message="FLEET-DISPATCH-BLOCKED: MyThingsLab/my-things-core#42",
    )
    result = _coder(repo.path, gh, ledger_path, runner).run(issue_number=5)

    assert result.outcome == "blocked"
    assert result.blocker == "MyThingsLab/my-things-core#42"
    assert not gh.saw("pr", "create")
    entry = next(e for e in Ledger(ledger_path) if e.outcome == "blocked")
    assert entry.data["blocker"] == "MyThingsLab/my-things-core#42"


def test_build_blocked_checkpoints_partial_commits(tmp_path, clean_git_env, attended_env):
    # A blocker discovered partway through still leaves durable work behind --
    # checkpoint it (same convention as a policy denial), don't discard it.
    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "partially blocked")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(
        files={"pkg/a.py": "a = 1\n"},
        final_message="FLEET-DISPATCH-BLOCKED: MyThingsLab/my-guard#7",
    )
    result = _coder(repo.path, gh, ledger_path, runner).run(issue_number=5)

    assert result.outcome == "blocked"
    assert result.blocker == "MyThingsLab/my-guard#7"
    assert "pkg/a.py" in result.files_touched
    assert "a = 1" in repo.read_committed("mycoder/my-raytracer-5", "pkg/a.py")


def test_build_denied_by_policy_opens_no_pr(tmp_path, clean_git_env, attended_env):
    class DenyPolicy:
        def evaluate(self, action: Action) -> PolicyResult:
            return PolicyResult(decision=Decision.DENY, reason="not allowed", rule="test")

    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "change")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})
    result = _coder(repo.path, gh, ledger_path, runner, policy=DenyPolicy()).run(issue_number=5)

    assert result.outcome == "denied"
    assert not gh.saw("pr", "create")
    assert any(e.outcome == "denied" for e in Ledger(ledger_path))


def test_build_checkpoints_commits_when_generated_code_fails_tests(
    tmp_path, clean_git_env, attended_env
):
    # Failing the test suite must not throw the session's work away: push the
    # branch as a checkpoint (needs_review) so a re-run can recycle it instead
    # of redoing it from scratch.
    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "broken")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})
    coder = _coder(
        repo.path, gh, ledger_path, runner, run_tests=True, test_command=["python", "-c", "exit(1)"]
    )
    result = coder.run(issue_number=5)

    assert result.outcome == "needs_review"
    assert result.tests_passed is False
    assert not gh.saw("pr", "create")
    assert "a = 1" in repo.read_committed("mycoder/my-raytracer-5", "pkg/a.py")


def test_build_fails_when_checkpoint_push_also_fails(tmp_path, clean_git_env, attended_env):
    from mycoder.coder import _run_git

    def flaky_git(tree, argv):
        if argv[:1] == ["push"]:
            raise RuntimeError("remote rejected the push")
        return _run_git(tree, argv)

    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "broken")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})
    coder = _coder(
        repo.path,
        gh,
        ledger_path,
        runner,
        run_tests=True,
        test_command=["python", "-c", "exit(1)"],
        git=flaky_git,
    )
    result = coder.run(issue_number=5)

    assert result.outcome == "failure"
    assert result.tests_passed is False
    assert not gh.saw("pr", "create")


def test_build_resumes_from_a_checkpointed_branch(tmp_path, clean_git_env, attended_env):
    # A second run for the same issue, after the first left a checkpoint
    # (failed tests, no PR), must build on those commits rather than restart
    # from origin/main -- the whole point of not discarding them.
    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "broken")})
    ledger_path = tmp_path / "ledger.jsonl"
    first = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})
    coder = _coder(
        repo.path, gh, ledger_path, first, run_tests=True, test_command=["python", "-c", "exit(1)"]
    )
    first_result = coder.run(issue_number=5)
    assert first_result.outcome == "needs_review"

    gh2 = FakeGh(
        {
            ("issue", "list"): _issue(5, "broken"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/10",
        }
    )
    second = FakeSessionRunner(files={"pkg/b.py": "b = 2\n"})
    coder2 = _coder(repo.path, gh2, ledger_path, second)
    second_result = coder2.run(issue_number=5)

    assert second_result.outcome == "success"
    assert second_result.pr == 10
    # Both the first run's checkpointed commit and the second run's new one
    # made it into the PR -- nothing from the first attempt was redone or lost.
    assert "a = 1" in repo.read_committed("mycoder/my-raytracer-5", "pkg/a.py")
    assert "b = 2" in repo.read_committed("mycoder/my-raytracer-5", "pkg/b.py")
    assert "already carries 1 commit" in second.calls[0]


def test_build_records_a_secret_alert_when_the_transcript_leaks(
    tmp_path, clean_git_env, attended_env
):
    repo = make_git_repo(tmp_path)
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "leaky"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/8",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"}, leaked=["aws_access_key_id"])
    result = _coder(repo.path, gh, ledger_path, runner).run(issue_number=5)

    assert result.outcome == "success"
    entries = list(Ledger(ledger_path))
    assert any(e.kind == "secret_alert" and e.outcome == "redacted" for e in entries)


def test_build_reports_a_failed_session(tmp_path, clean_git_env, attended_env):
    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "x")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(ok=False, error="claude exited 2")
    result = _coder(repo.path, gh, ledger_path, runner).run(issue_number=5)

    assert result.outcome == "failure"
    assert not gh.saw("pr", "create")
    assert any(e.outcome == "failure" for e in Ledger(ledger_path))


def test_build_needs_review_when_push_fails(tmp_path, clean_git_env, attended_env):
    from mycoder.coder import _run_git

    def flaky_git(tree, argv):
        if argv[:1] == ["push"]:
            raise RuntimeError("remote rejected the push")
        return _run_git(tree, argv)

    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "x")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})
    result = _coder(repo.path, gh, ledger_path, runner, git=flaky_git).run(issue_number=5)

    assert result.outcome == "needs_review"
    assert not gh.saw("pr", "create")
    assert any(e.outcome == "needs_review" for e in Ledger(ledger_path))


def test_errored_session_with_commits_pushes_but_opens_no_pr(tmp_path, clean_git_env, attended_env):
    # A session that committed real work then hit its turn cap (is_error) must
    # not throw that work away: push the branch, open NO PR, report needs_review.
    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "partial")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"}, ok=False, error="hit max turns")
    result = _coder(repo.path, gh, ledger_path, runner).run(issue_number=5)

    assert result.outcome == "needs_review"
    assert not gh.saw("pr", "create")
    # The durable work reached the origin even though the session errored.
    assert "a = 1" in repo.read_committed("mycoder/my-raytracer-5", "pkg/a.py")


def test_transcript_is_persisted_and_recorded(tmp_path, clean_git_env, attended_env):
    repo = make_git_repo(tmp_path)
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "traced"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/9",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    transcripts = tmp_path / "transcripts"
    runner = FakeSessionRunner(
        files={"pkg/a.py": "a = 1\n"}, transcript='{"type":"result","result":"ok"}'
    )
    result = _coder(repo.path, gh, ledger_path, runner, transcripts_dir=transcripts).run(
        issue_number=5
    )

    assert result.outcome == "success"
    written = list(transcripts.glob("*.jsonl"))
    assert len(written) == 1
    assert "result" in written[0].read_text()
    entry = next(e for e in Ledger(ledger_path) if e.kind == "code" and e.outcome == "success")
    assert entry.data["transcript"] == str(written[0])
    assert entry.data["final_message"] == "done"


def test_prompt_carries_a_style_anchor_from_existing_code(tmp_path, clean_git_env, attended_env):
    # The session must see the repo's existing code so it matches conventions
    # even when there is no CLAUDE.md to spell them out.
    repo = make_git_repo(
        tmp_path,
        files={
            "src/pkg/thing.py": "MARKER_SOURCE = 42\n",
            "tests/test_thing.py": "def test_marker() -> None:\n    assert True\n",
        },
    )
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "extend thing"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/11",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"src/pkg/more.py": "x = 1\n"})
    _coder(repo.path, gh, ledger_path, runner).run(issue_number=5)

    prompt = runner.calls[0]
    assert "match its conventions" in prompt
    assert "MARKER_SOURCE = 42" in prompt  # existing source content is shown
    assert "src/pkg/thing.py" in prompt  # and the file tree / exemplar header


_FIXED_TEST_CMD = [
    "python",
    "-c",
    "import pathlib, sys; sys.exit(0 if pathlib.Path('pkg/fixed.txt').exists() else 1)",
]


def test_build_retries_and_succeeds_on_a_later_attempt(tmp_path, clean_git_env, attended_env):
    # First attempt fails the test suite and checkpoints; a fresh session on
    # the second attempt (auto-resumed from that checkpoint) writes the fix
    # and the build succeeds -- the whole retry loop, in one `run()` call.
    repo = make_git_repo(tmp_path)
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "hard"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/20",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    runner = SequencedSessionRunner([{"pkg/a.py": "a = 1\n"}, {"pkg/fixed.txt": "ok\n"}])
    coder = _coder(
        repo.path,
        gh,
        ledger_path,
        runner,
        run_tests=True,
        test_command=_FIXED_TEST_CMD,
        max_attempts=2,
    )
    result = coder.run(issue_number=5)

    assert result.outcome == "success"
    assert result.pr == 20
    assert result.attempts == 2
    assert result.cost_usd == 0.10  # summed across both attempts, not just the last
    assert len(runner.calls) == 2
    assert "already carries 1 commit" in runner.calls[1]
    assert "a = 1" in repo.read_committed("mycoder/my-raytracer-5", "pkg/a.py")
    assert "ok" in repo.read_committed("mycoder/my-raytracer-5", "pkg/fixed.txt")


def test_build_stops_retrying_once_max_attempts_is_reached(tmp_path, clean_git_env, attended_env):
    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "never fixed")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = SequencedSessionRunner([{"pkg/a.py": "a = 1\n"}])  # never writes fixed.txt
    coder = _coder(
        repo.path,
        gh,
        ledger_path,
        runner,
        run_tests=True,
        test_command=_FIXED_TEST_CMD,
        max_attempts=3,
    )
    result = coder.run(issue_number=5)

    assert result.outcome == "needs_review"
    assert result.attempts == 3
    assert len(runner.calls) == 3
    assert not gh.saw("pr", "create")


def test_build_stops_retrying_when_the_total_budget_is_spent(tmp_path, clean_git_env, attended_env):
    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "never fixed")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = SequencedSessionRunner([{"pkg/a.py": "a = 1\n"}], cost_usd=1.0)
    coder = _coder(
        repo.path,
        gh,
        ledger_path,
        runner,
        run_tests=True,
        test_command=_FIXED_TEST_CMD,
        max_attempts=5,
        max_total_budget_usd=1.0,
    )
    result = coder.run(issue_number=5)

    # A single $1 attempt already exhausts a $1 total budget -- no second try
    # even though max_attempts allows four more.
    assert result.outcome == "needs_review"
    assert result.attempts == 1
    assert len(runner.calls) == 1


def test_default_max_attempts_is_one_unchanged_behavior(tmp_path, clean_git_env, attended_env):
    # No caller opts into retries by accident: the default must reproduce the
    # old single-shot behavior exactly.
    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "never fixed")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = SequencedSessionRunner([{"pkg/a.py": "a = 1\n"}])
    coder = _coder(
        repo.path, gh, ledger_path, runner, run_tests=True, test_command=_FIXED_TEST_CMD
    )
    result = coder.run(issue_number=5)

    assert result.outcome == "needs_review"
    assert result.attempts == 1
    assert len(runner.calls) == 1


def test_default_guarded_policy_denies_when_the_ask_channel_says_no(
    tmp_path, clean_git_env, attended_env
):
    from mythings.policy import Decision

    from mycoder.coder import default_guarded_policy

    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "guarded")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})
    policy = default_guarded_policy()
    policy.ask = lambda action: Decision.DENY  # a deterministic stand-in for a human
    result = _coder(repo.path, gh, ledger_path, runner, policy=policy).run(issue_number=5)

    assert result.outcome == "denied"
    assert not gh.saw("pr", "create")
    # The ask channel said no to the PR, but v0.3's checkpoint still ran --
    # a guarded denial doesn't throw the commit away either.
    assert "a = 1" in repo.read_committed("mycoder/my-raytracer-5", "pkg/a.py")


def test_default_guarded_policy_opens_the_pr_when_the_ask_channel_says_yes(
    tmp_path, clean_git_env, attended_env
):
    from mythings.policy import Decision

    from mycoder.coder import default_guarded_policy

    repo = make_git_repo(tmp_path)
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "guarded"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/30",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})
    policy = default_guarded_policy()
    policy.ask = lambda action: Decision.ALLOW
    result = _coder(repo.path, gh, ledger_path, runner, policy=policy).run(issue_number=5)

    assert result.outcome == "success"
    assert result.pr == 30


def test_unguarded_default_policy_is_unaffected_by_the_pr_action_kind(
    tmp_path, clean_git_env, attended_env
):
    # The PR-open Action moved from kind="bash" to kind="draft-pr-create" so a
    # Guard() can actually intercept it -- the plain unguarded default (no
    # --guarded) must still allow it exactly as before.
    repo = make_git_repo(tmp_path)
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "unguarded"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/31",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})
    result = _coder(repo.path, gh, ledger_path, runner).run(issue_number=5)

    assert result.outcome == "success"
    assert result.pr == 31


def test_prompt_carries_my_searcher_relevant_files(tmp_path, clean_git_env, attended_env):
    # my-searcher's own CLAUDE.md documents this exact hand-off: a "which
    # files matter here" step for later tools including MyCoder.
    repo = make_git_repo(
        tmp_path,
        files={
            "src/pkg/camera.py": "class Camera:\n    pass\n",
            "src/pkg/unrelated.py": "x = 1\n",
        },
    )
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "fix the camera projection", "camera math is wrong"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/40",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"src/pkg/camera.py": "class Camera:\n    fixed = True\n"})
    _coder(repo.path, gh, ledger_path, runner).run(issue_number=5)

    prompt = runner.calls[0]
    assert "my-searcher ranked" in prompt
    assert "src/pkg/camera.py" in prompt
    # my-searcher's own ranking run is on the ledger too, separate from
    # mycoder's own kind=code entries.
    assert any(e.tool == "mysearcher" and e.kind == "search" for e in Ledger(ledger_path))


def test_prompt_carries_prior_research_from_the_shared_ledger(
    tmp_path, clean_git_env, attended_env
):
    # Read-only fence: a prior `myresearcher brief` run against this same
    # repo left a ledger entry; mycoder surfaces it without importing
    # myresearcher's package, same convention as MyTodo reading MyPlanner.
    repo = make_git_repo(tmp_path)
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "implement path tracing integrator"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/41",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    Ledger(ledger_path).record(
        "myresearcher",
        "research",
        "success",
        "brief for path tracing",
        topic="Monte Carlo path tracing integrators",
        summary="Cosine-weighted hemisphere sampling cancels the PDF term.",
    )
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})
    _coder(repo.path, gh, ledger_path, runner).run(issue_number=5)

    prompt = runner.calls[0]
    assert "Prior research on" in prompt
    assert "Cosine-weighted hemisphere sampling cancels the PDF term." in prompt


def test_prompt_has_no_research_section_when_nothing_matches(tmp_path, clean_git_env, attended_env):
    repo = make_git_repo(tmp_path)
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "implement path tracing integrator"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/42",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    Ledger(ledger_path).record(
        "myresearcher",
        "research",
        "success",
        "brief for an unrelated topic",
        topic="Distributed systems consensus algorithms",
        summary="Irrelevant to this issue.",
    )
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})
    _coder(repo.path, gh, ledger_path, runner).run(issue_number=5)

    prompt = runner.calls[0]
    assert "Prior research on" not in prompt
