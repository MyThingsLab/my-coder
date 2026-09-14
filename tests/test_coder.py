from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from mythings.github import GitHub
from mythings.ledger import Ledger
from mythings.policy import Action, Decision, PolicyResult
from mythings.testing import FakeGh, make_git_repo

from mycoder.coder import _BLOCKED_SENTINEL, Coder, default_test_command
from mycoder.session import NoopSessionRunner, SessionResult

SLUG = "MyThingsLab/my-raytracer"
OUT_OF_FLEET_SLUG = "someone-else/their-repo"

# Not the literal "python": this suite runs on hosts without that shim, which is
# the bug these fixtures used to trip over (my-coder#12).
_PY = default_test_command()[:1]


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
        failure_cost_usd: float | None = 0.0,
    ) -> None:
        self.files = files or {}
        self.ok = ok
        self.commit = commit
        self.leaked = leaked or []
        self.error = error
        # None models a *timed-out* session, whose real cost is unrecoverable.
        self.failure_cost_usd = failure_cost_usd
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
                cost_usd=self.failure_cost_usd,
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


def _github(gh: FakeGh, slug: str = SLUG) -> GitHub:
    return GitHub(slug, runner=gh)


def _coder(repo_path, gh, ledger_path, runner, *, slug: str = SLUG, **kwargs) -> Coder:
    return Coder(
        repo=repo_path,
        repo_slug=slug,
        github=_github(gh, slug),
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


def test_build_blocks_the_pr_when_the_diff_leaks_a_secret(tmp_path, clean_git_env, attended_env):
    # A session's transcript is redacted (session.py:redact_secrets), but a
    # commit it makes itself is not -- this is the last gate before that
    # credential would leave the worktree as a public PR.
    repo = make_git_repo(tmp_path, files={"README.md": "# r\n"})
    gh = FakeGh({("issue", "list"): _issue(5, "add config")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/config.py": "AWS_KEY = 'AKIA1234567890ABCDEF'\n"})
    result = _coder(repo.path, gh, ledger_path, runner).run(issue_number=5)

    assert result.outcome == "needs_review"
    assert not gh.saw("pr", "create")
    entries = list(Ledger(ledger_path))
    assert any(
        e.kind == "secret_alert"
        and e.outcome == "blocked"
        and "aws_access_key_id" in e.data["patterns"]
        for e in entries
    )
    assert any(e.kind == "code" and e.outcome == "needs_review" for e in entries)
    # The branch is still pushed as a checkpoint, so the human can fix it in place.
    assert "AKIA1234567890ABCDEF" in repo.read_committed("mycoder/my-raytracer-5", "pkg/config.py")


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
        repo.path, gh, ledger_path, runner, run_tests=True, test_command=[*_PY, "-c", "exit(1)"]
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
        test_command=[*_PY, "-c", "exit(1)"],
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
        repo.path, gh, ledger_path, first, run_tests=True, test_command=[*_PY, "-c", "exit(1)"]
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
    *_PY,
    "-c",
    "import pathlib, sys; sys.exit(0 if pathlib.Path('pkg/fixed.txt').exists() else 1)",
]

_PASSING_TEST_CMD = [*_PY, "-c", "exit(0)"]


def test_a_verified_pr_opens_ready_not_draft(tmp_path, clean_git_env, attended_env):
    # my-fleet#32: `ci.yml` skips required checks while a PR is a draft, so a PR
    # born as a draft could never show a green check -- and the fleet's
    # promotion gate read that skip as a pass and promoted on it. Opening ready
    # is what makes CI run at all. The gate moved to the merge, which a human
    # always performs.
    repo = make_git_repo(tmp_path, files={"README.md": "# r\n"})
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "add greet"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/7",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/greet.py": "def greet():\n    return 'hi'\n"})
    result = _coder(
        repo.path,
        gh,
        ledger_path,
        runner,
        run_tests=True,
        test_command=_PASSING_TEST_CMD,
    ).run(issue_number=5)

    assert result.outcome == "success"
    assert result.tests_passed is True
    assert result.tests_env == "prepared"
    create = next(c for c in gh.calls if c[:2] == ["pr", "create"])
    assert "--draft" not in create, "a PR whose suite passed must open ready, or CI never runs"
    entries = list(Ledger(ledger_path))
    assert any(e.kind == "code" and e.data.get("draft") is False for e in entries)


def test_an_unverified_pr_still_opens_as_a_draft(tmp_path, clean_git_env, attended_env):
    # The other half of the rule. Without --run-tests nothing was verified in
    # the worktree, so `tests_passed` is None rather than True. Unverified work
    # must not present itself as reviewable just because opening ready is now
    # the norm -- the draft is the honest signal that no suite ran.
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
    assert result.tests_passed is None
    assert result.tests_env is None
    create = next(c for c in gh.calls if c[:2] == ["pr", "create"])
    assert "--draft" in create


def test_ambient_test_pass_still_opens_as_a_draft(
    tmp_path, clean_git_env, attended_env, monkeypatch
):
    # my-coder#37: a suite that passes only because it ran under whatever
    # interpreter `default_test_command` resolved from PATH -- my-coder's own
    # ambient environment, not one the caller declared as matching the
    # target's CI -- must not be reported as an unqualified green. Fix the
    # default's resolved command to something that reliably passes (still
    # exercised via test_command=None, the real "ambient" code path) and
    # assert the PR still opens as a draft despite tests_passed being True.
    monkeypatch.setattr("mycoder.coder.default_test_command", lambda: _PASSING_TEST_CMD)
    repo = make_git_repo(tmp_path, files={"README.md": "# r\n"})
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "add greet"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/7",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/greet.py": "def greet():\n    return 'hi'\n"})
    result = _coder(repo.path, gh, ledger_path, runner, run_tests=True).run(issue_number=5)

    assert result.outcome == "success"
    assert result.tests_passed is True
    assert result.tests_env == "ambient"
    assert "ambient" in result.detail
    create = next(c for c in gh.calls if c[:2] == ["pr", "create"])
    assert "--draft" in create, "an ambient pass must not promote the PR to ready"
    entries = list(Ledger(ledger_path))
    assert any(e.kind == "code" and e.data.get("tests_env") == "ambient" for e in entries)


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
    coder = _coder(repo.path, gh, ledger_path, runner, run_tests=True, test_command=_FIXED_TEST_CMD)
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


def test_prompt_carries_active_fleet_context_from_env(
    tmp_path, clean_git_env, attended_env, monkeypatch
):
    repo = make_git_repo(tmp_path)
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "implement path tracing integrator"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/42",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})

    monkeypatch.setenv(
        "MYTHINGS_FLEET_CONTEXT",
        "Active Fleet Context:\n- Worker 'account2': my-tester#9 in flight",
    )

    _coder(repo.path, gh, ledger_path, runner).run(issue_number=5)

    prompt = runner.calls[0]
    assert "Active Fleet Context:" in prompt
    assert "Worker 'account2': my-tester#9 in flight" in prompt


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


def test_default_test_command_resolves_an_interpreter_that_exists(monkeypatch) -> None:
    # my-coder#12: the old default was the literal "python", absent on any host
    # without the python-is-python3 shim.
    assert shutil.which(default_test_command()[0]) or default_test_command()[0] == sys.executable

    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert default_test_command()[0] == sys.executable


def test_an_unrunnable_test_command_is_an_outcome_not_a_traceback(
    tmp_path, clean_git_env, attended_env
):
    # my-coder#12: a test command that cannot be launched used to raise
    # FileNotFoundError out of the run, throwing away the session's commits.
    repo = make_git_repo(tmp_path, files={"README.md": "# r\n"})
    gh = FakeGh({("issue", "list"): _issue(5, "add a thing")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})

    result = _coder(
        repo.path,
        gh,
        ledger_path,
        runner,
        run_tests=True,
        test_command=["mycoder-no-such-interpreter"],
    ).run(issue_number=5)

    assert result.outcome == "needs_review"
    assert "could not run test command" in result.detail
    assert result.tests_passed is False
    # The work still reached origin rather than dying with the worktree.
    assert "a = 1" in repo.read_committed("mycoder/my-raytracer-5", "pkg/a.py")
    assert not any(c[:2] == ["pr", "create"] for c in gh.calls)


def test_style_anchor_finds_exemplars_in_a_flat_layout_repo(tmp_path, clean_git_env, attended_env):
    # my-coder#13: requiring a src/ or tests/ prefix blanked the anchor on every
    # repo not scaffolded from my-template.
    repo = make_git_repo(
        tmp_path,
        files={
            "model.py": "MARKER_FLAT = 42\n" + "# pad\n" * 50,
            "build/generated.py": "MARKER_GENERATED = 1\n" + "# pad\n" * 200,
        },
    )
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "fix the model"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/11",
        }
    )
    runner = FakeSessionRunner(files={"model.py": "MARKER_FLAT = 43\n"})
    _coder(repo.path, gh, tmp_path / "ledger.jsonl", runner).run(issue_number=5)

    prompt = runner.calls[0]
    assert "MARKER_FLAT = 42" in prompt
    assert "early/greenfield" not in prompt
    # Generated output is never house style, even when it is the largest file.
    assert "MARKER_GENERATED" not in prompt


def test_out_of_fleet_target_gets_no_cross_org_protocol(tmp_path, clean_git_env, attended_env):
    # my-coder#14: a session on someone else's repo was told to file issues into
    # MyThingsLab, where the `critical` label halts fleet dispatch org-wide.
    repo = make_git_repo(tmp_path, files={"model.py": "MARKER = 1\n"})
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "fix the model"),
            ("pr", "create"): f"https://github.com/{OUT_OF_FLEET_SLUG}/pull/3",
        }
    )
    runner = FakeSessionRunner(files={"model.py": "MARKER = 2\n"})
    _coder(repo.path, gh, tmp_path / "ledger.jsonl", runner, slug=OUT_OF_FLEET_SLUG).run(
        issue_number=5
    )

    prompt = runner.calls[0]
    assert "gh issue create" not in prompt
    assert "FLEET-DISPATCH-BLOCKED" not in prompt
    assert "halts new fleet dispatch" not in prompt
    assert "outside the MyThingsLab fleet" in prompt
    assert "do NOT use `gh` at all" in prompt


def test_in_fleet_target_keeps_the_blocker_protocol(tmp_path, clean_git_env, attended_env):
    repo = make_git_repo(tmp_path, files={"src/pkg/thing.py": "MARKER = 1\n"})
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "extend thing"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/11",
        }
    )
    runner = FakeSessionRunner(files={"src/pkg/thing.py": "MARKER = 2\n"})
    _coder(repo.path, gh, tmp_path / "ledger.jsonl", runner).run(issue_number=5)

    prompt = runner.calls[0]
    assert "gh issue create --repo MyThingsLab/<repo>" in prompt
    assert f"{_BLOCKED_SENTINEL} MyThingsLab/<repo>#<number>" in prompt


def test_target_conventions_supersede_the_fleet_style_mandate(
    tmp_path, clean_git_env, attended_env
):
    # my-coder#14: the prompt calls the target's CLAUDE.md "authoritative here"
    # and then mandates fleet house style underneath it.
    repo = make_git_repo(
        tmp_path,
        files={
            "model.py": "MARKER = 1\n",
            "CLAUDE.md": "# target\n\nNo docstrings. Comment why, never what.\n",
        },
    )
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "fix the model"),
            ("pr", "create"): f"https://github.com/{OUT_OF_FLEET_SLUG}/pull/3",
        }
    )
    runner = FakeSessionRunner(files={"model.py": "MARKER = 2\n"})
    _coder(repo.path, gh, tmp_path / "ledger.jsonl", runner, slug=OUT_OF_FLEET_SLUG).run(
        issue_number=5
    )

    prompt = runner.calls[0]
    assert "Comment why, never what." in prompt
    assert "from __future__ import annotations" not in prompt
    assert "they win over any habit of" in prompt


def test_target_conventions_reads_canonical_agents_md(tmp_path, clean_git_env, attended_env):
    repo = make_git_repo(
        tmp_path,
        files={
            "model.py": "MARKER = 1\n",
            "AGENTS.md": "# target agents\n\nCanonical instructions.\n",
        },
    )
    gh = FakeGh(
        {
            ("issue", "list"): _issue(6, "fix the model"),
            ("pr", "create"): f"https://github.com/{OUT_OF_FLEET_SLUG}/pull/4",
        }
    )
    runner = FakeSessionRunner(files={"model.py": "MARKER = 2\n"})
    _coder(repo.path, gh, tmp_path / "ledger.jsonl", runner, slug=OUT_OF_FLEET_SLUG).run(
        issue_number=6
    )

    prompt = runner.calls[0]
    assert "Canonical instructions." in prompt


def test_timed_out_session_salvages_its_uncommitted_edits(tmp_path, clean_git_env, attended_env):
    # my-coder#19: a session killed by the wall clock has usually made its edits
    # and not reached `git commit`, because it commits last. Those edits used to
    # die with the worktree.
    repo = make_git_repo(tmp_path, files={"model.py": "MARKER = 1\n"})
    gh = FakeGh({("issue", "list"): _issue(5, "fix the model")})
    runner = FakeSessionRunner(
        files={"model.py": "MARKER = 2\n"},
        commit=False,
        ok=False,
        error="session exceeded 1500s wall-clock timeout",
    )
    result = _coder(repo.path, gh, tmp_path / "ledger.jsonl", runner).run(issue_number=5)

    assert result.outcome == "needs_review"
    assert "salvaged" in result.detail
    assert "model.py" in result.files_touched
    # Banked on origin, so the next attempt resumes instead of restarting.
    assert "MARKER = 2" in repo.read_committed("mycoder/my-raytracer-5", "model.py")
    # Never a PR: nothing here passed the session's own review or a test run.
    assert not any(c[:2] == ["pr", "create"] for c in gh.calls)


def test_repeated_timeouts_stop_at_the_total_budget(tmp_path, clean_git_env, attended_env):
    # my-coder#18: a timeout carries no price, so it used to add 0.0 to the
    # running total. `failure` is retryable, so a candidate that timed out every
    # time retried until max_attempts ran out, against a ceiling that believed
    # nothing had been spent. Metering unknown as the cap stops it after one.
    repo = make_git_repo(tmp_path, files={"model.py": "MARKER = 1\n"})
    gh = FakeGh({("issue", "list"): _issue(5, "fix the model")})
    runner = FakeSessionRunner(
        commit=False,
        ok=False,
        error="session exceeded 1500s wall-clock timeout",
        failure_cost_usd=None,  # a real timeout: cost unrecoverable
    )
    result = _coder(
        repo.path,
        gh,
        tmp_path / "ledger.jsonl",
        runner,
        max_attempts=5,
        max_budget_usd=3.0,
        max_total_budget_usd=3.0,
    ).run(issue_number=5)

    assert result.attempts == 1, "an unknown cost must not meter as free"
    assert result.cost_usd == 3.0
    assert result.cost_known is False
    assert len(runner.calls) == 1


def test_a_known_zero_cost_still_allows_retries(tmp_path, clean_git_env, attended_env):
    # The mirror of the above: a session that genuinely cost nothing is not the
    # same as one whose cost could not be recovered, and must not be throttled.
    repo = make_git_repo(tmp_path, files={"model.py": "MARKER = 1\n"})
    gh = FakeGh({("issue", "list"): _issue(5, "fix the model")})
    runner = FakeSessionRunner(
        commit=False, ok=False, error="claude exited 1", failure_cost_usd=0.0
    )
    result = _coder(
        repo.path,
        gh,
        tmp_path / "ledger.jsonl",
        runner,
        max_attempts=3,
        max_budget_usd=3.0,
        max_total_budget_usd=3.0,
    ).run(issue_number=5)

    assert result.attempts == 3
    assert result.cost_usd == 0.0
    assert result.cost_known is True


def test_salvage_never_commits_an_environment_directory(tmp_path, clean_git_env, attended_env):
    # The salvage commit gets pushed, and a session may well have built a
    # multi-GB venv in the worktree.
    repo = make_git_repo(tmp_path, files={"model.py": "MARKER = 1\n"})
    gh = FakeGh({("issue", "list"): _issue(5, "fix the model")})
    runner = FakeSessionRunner(
        files={
            "model.py": "MARKER = 2\n",
            ".venv/bin/python": "#!/bin/sh\n",
            "__pycache__/model.pyc": "junk\n",
        },
        commit=False,
        ok=False,
        error="session exceeded 1500s wall-clock timeout",
    )
    result = _coder(repo.path, gh, tmp_path / "ledger.jsonl", runner).run(issue_number=5)

    assert result.outcome == "needs_review"
    assert result.files_touched == ["model.py"]


def test_a_clean_worktree_after_a_failed_session_is_still_a_failure(
    tmp_path, clean_git_env, attended_env
):
    # Salvage must not turn an honest empty run into a checkpoint.
    repo = make_git_repo(tmp_path, files={"model.py": "MARKER = 1\n"})
    gh = FakeGh({("issue", "list"): _issue(5, "fix the model")})
    runner = FakeSessionRunner(files={}, ok=False, error="claude exited 1")
    result = _coder(repo.path, gh, tmp_path / "ledger.jsonl", runner).run(issue_number=5)

    assert result.outcome == "failure"
    assert result.files_touched == []


def test_parse_test_failures_extracts_node_ids_and_traces():
    from mycoder.coder import _parse_test_failures

    stdout = """
=================================== FAILURES ===================================
__________________________________ test_one ___________________________________
    def test_one():
>       assert 1 == 2
E       AssertionError: assert 1 == 2
tests/test_foo.py:10: AssertionError
=========================== short test summary info ============================
FAILED tests/test_foo.py::test_one - AssertionError: assert 1 == 2
FAILED tests/test_bar.py::test_two
"""
    failing, trace = _parse_test_failures(stdout, "")
    assert failing == ["tests/test_foo.py::test_one", "tests/test_bar.py::test_two"]
    assert "AssertionError: assert 1 == 2" in trace


def test_parse_test_failures_extracts_collection_errors():
    from mycoder.coder import _parse_test_failures

    stdout = """
=========================== short test summary info ============================
ERROR tests/test_mcp_server.py - ModuleNotFoundError: No module named 'mcp'
"""
    failing, trace = _parse_test_failures(stdout, "")
    assert failing == ["tests/test_mcp_server.py"]


def test_build_records_failing_tests_and_passes_diagnostics_to_resuming_session(
    tmp_path, clean_git_env, attended_env
):
    from mythings.ledger import Ledger

    repo = make_git_repo(tmp_path)
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "broken"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/10",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    # Conditioned on the session's own file so this is a failure the *diff*
    # caused. An unconditionally red script would also be red at the base
    # commit, which is an inherited failure and no longer blamed on the diff
    # (my-coder#34) -- a different case than the one this test is about.
    failing_script = (
        "import pathlib, sys\n"
        "if not pathlib.Path('pkg/a.py').exists():\n"
        "    sys.exit(0)\n"
        "sys.stdout.write('FAILED tests/test_math.py::test_add - AssertionError: 1 != 2\\n')\n"
        "sys.stdout.write('E   AssertionError: 1 != 2\\n')\n"
        "sys.exit(1)\n"
    )
    first = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})
    coder1 = _coder(
        repo.path,
        gh,
        ledger_path,
        first,
        run_tests=True,
        test_command=[*_PY, "-c", failing_script],
    )
    first_result = coder1.run(issue_number=5)

    assert first_result.outcome == "needs_review"
    assert first_result.failing_tests == ["tests/test_math.py::test_add"]
    assert "AssertionError: 1 != 2" in first_result.failure_trace

    # Check ledger record
    entries = list(Ledger(ledger_path))
    last_entry = entries[-1]
    assert last_entry.data["failing_tests"] == ["tests/test_math.py::test_add"]
    assert "AssertionError: 1 != 2" in last_entry.data["failure_trace"]

    # Second run resumes
    second = FakeSessionRunner(files={"pkg/a.py": "a = 2\n"})
    coder2 = _coder(repo.path, gh, ledger_path, second)
    second_result = coder2.run(issue_number=5)

    assert second_result.outcome == "success"
    prompt = second.calls[0]
    assert "Failing test(s): `tests/test_math.py::test_add`" in prompt
    assert "AssertionError: 1 != 2" in prompt
    assert "Files already modified:" in prompt
    assert "pkg/a.py" in prompt
    assert "Do NOT start over from scratch or repeat exploratory commands" in prompt


def test_prune_python_exemplar_preserves_signatures_and_strips_bodies():
    from mycoder.coder import _prune_python_exemplar

    source = (
        "from __future__ import annotations\n"
        "import os\n"
        "CONSTANT = 100\n\n"
        "class Worker:\n"
        "    field: int = 1\n\n"
        "    def run(self, flag: bool) -> None:\n"
        "        # heavy loop\n"
        "        for i in range(100):\n"
        "            print('doing heavy work')\n\n"
        "def top_func(x: str) -> bool:\n"
        "    return len(x) > 0\n"
    )
    pruned = _prune_python_exemplar(source)
    assert "from __future__ import annotations" in pruned
    assert "import os" in pruned
    assert "CONSTANT = 100" in pruned
    assert "class Worker:" in pruned
    assert "field: int = 1" in pruned
    assert "def run(self, flag: bool) -> None:" in pruned
    assert "def top_func(x: str) -> bool:" in pruned
    assert "doing heavy work" not in pruned
    assert len(pruned) < len(source)


def test_prune_python_exemplar_syntax_error_fallback():
    from mycoder.coder import _prune_python_exemplar

    broken = "def broken(:::\n    some syntax error\n"
    pruned = _prune_python_exemplar(broken, max_chars=20)
    assert pruned == broken[:20]


# A suite that is red at the base commit and stays red for the same reason: the
# node id it reports does not depend on anything the session writes.
_INHERITED_RED_TEST_CMD = [
    *_PY,
    "-c",
    "import sys; sys.stdout.write('FAILED tests/test_legacy.py::test_old - boom\\n'); sys.exit(1)",
]

# Green at base, red only once the session's file exists: a failure the diff
# really did cause.
_DIFF_BROKE_IT_TEST_CMD = [
    *_PY,
    "-c",
    "import pathlib, sys\n"
    "if not pathlib.Path('pkg/a.py').exists():\n"
    "    sys.exit(0)\n"
    "sys.stdout.write('FAILED tests/test_new.py::test_new - boom\\n')\n"
    "sys.exit(1)\n",
]

# Red at base for one reason, and the diff adds a second, different failure.
_BOTH_TEST_CMD = [
    *_PY,
    "-c",
    "import pathlib, sys\n"
    "sys.stdout.write('FAILED tests/test_legacy.py::test_old - boom\\n')\n"
    "if pathlib.Path('pkg/a.py').exists():\n"
    "    sys.stdout.write('FAILED tests/test_new.py::test_new - boom\\n')\n"
    "sys.exit(1)\n",
]


def test_a_red_inherited_from_base_does_not_strand_the_work(
    tmp_path, clean_git_env, attended_env
):
    # my-coder#34: --run-tests used to treat any red suite as the diff's fault.
    # A repo carrying one unrelated failing test could therefore never produce a
    # worker PR at all -- every session's work piled up on a checkpoint branch
    # nothing promotes, and the retry loop paid again for a failure no session
    # could fix. Subtracting the base's failures is what tells the two apart.
    repo = make_git_repo(tmp_path, files={"README.md": "# r\n"})
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "add a"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/11",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})
    result = _coder(
        repo.path,
        gh,
        ledger_path,
        runner,
        run_tests=True,
        test_command=_INHERITED_RED_TEST_CMD,
    ).run(issue_number=5)

    assert result.outcome == "success"
    assert result.pr == 11
    assert result.inherited_failures == ["tests/test_legacy.py::test_old"]
    # Still not a verified pass -- the suite is genuinely red, so it opens as a
    # draft and says why rather than claiming a green nobody observed.
    assert result.tests_passed is False
    create = next(c for c in gh.calls if c[:2] == ["pr", "create"])
    assert "--draft" in create
    assert "tests/test_legacy.py::test_old" in result.detail


def test_a_failure_the_diff_caused_is_still_the_diffs_fault(
    tmp_path, clean_git_env, attended_env
):
    # The other half of the subtraction: a clean baseline means a red suite is
    # exactly the evidence it always was, and needs_review (retryable) is right.
    repo = make_git_repo(tmp_path, files={"README.md": "# r\n"})
    gh = FakeGh({("issue", "list"): _issue(5, "add a")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})
    result = _coder(
        repo.path,
        gh,
        ledger_path,
        runner,
        run_tests=True,
        test_command=_DIFF_BROKE_IT_TEST_CMD,
    ).run(issue_number=5)

    assert result.outcome == "needs_review"
    assert result.tests_passed is False
    assert result.inherited_failures == []
    assert not gh.saw("pr", "create")


def test_a_new_failure_alongside_an_inherited_one_still_blames_the_diff(
    tmp_path, clean_git_env, attended_env
):
    # An inherited red must not become a blanket amnesty: if the diff also broke
    # something that was green on base, that is still the diff's fault.
    repo = make_git_repo(tmp_path, files={"README.md": "# r\n"})
    gh = FakeGh({("issue", "list"): _issue(5, "add a")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})
    result = _coder(
        repo.path,
        gh,
        ledger_path,
        runner,
        run_tests=True,
        test_command=_BOTH_TEST_CMD,
    ).run(issue_number=5)

    assert result.outcome == "needs_review"
    assert not gh.saw("pr", "create")
    # The inherited one is still named, so a reviewer is not sent chasing it.
    assert result.inherited_failures == ["tests/test_legacy.py::test_old"]
    assert "tests/test_new.py::test_new" in result.failing_tests


def test_test_env_prepends_worktree_src_to_pythonpath(tmp_path: Path, monkeypatch) -> None:
    # #37: _test_env prepends the worktree's src directory to PYTHONPATH so tests run
    # against the diff rather than an ambient editable install in the host venv.
    monkeypatch.setenv("PYTHONPATH", "/ambient/path")
    coder = _coder(tmp_path, FakeGh(), tmp_path / "ledger.jsonl", NoopSessionRunner())
    env = coder._test_env(tmp_path)
    assert env["PYTHONPATH"] == f"{tmp_path / 'src'}:/ambient/path"


def test_supplied_test_command_is_recorded_in_result_ledger_and_pr_body(
    tmp_path: Path, clean_git_env, attended_env
) -> None:
    repo = make_git_repo(tmp_path, files={"README.md": "# r\n"})
    gh = FakeGh(
        {
            ("issue", "list"): _issue(5, "add greet"),
            ("pr", "create"): f"https://github.com/{SLUG}/pull/7",
        }
    )
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/greet.py": "def greet():\n    return 'hi'\n"})
    cmd = [sys.executable, "-c", "exit(0)"]
    result = _coder(
        repo.path,
        gh,
        ledger_path,
        runner,
        run_tests=True,
        test_command=cmd,
    ).run(issue_number=5)

    assert result.outcome == "success"
    assert result.tests_passed is True
    assert result.supplied_test_command is True

    entries = list(Ledger(ledger_path))
    assert any(e.kind == "code" and e.data.get("supplied_test_command") is True for e in entries)

    pr_call = next(c for c in gh.calls if c[:2] == ["pr", "create"])
    body = pr_call[pr_call.index("--body") + 1]
    assert "verified via supplied --test-command:" in body


def test_default_test_command_has_supplied_test_command_false(
    tmp_path: Path, clean_git_env, attended_env
) -> None:
    ledger = tmp_path / "ledger.jsonl"
    coder = _coder(tmp_path, FakeGh(), ledger, NoopSessionRunner(), run_tests=True)
    assert coder.supplied_test_command is False


def test_prompt_instructs_synchronous_execution() -> None:
    from mycoder.coder import _PROMPT

    assert "Run all commands synchronously" in _PROMPT
    assert "Do NOT background commands" in _PROMPT


