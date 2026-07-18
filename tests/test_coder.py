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
    ) -> None:
        self.files = files or {}
        self.ok = ok
        self.commit = commit
        self.leaked = leaked or []
        self.error = error
        self.transcript = transcript
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
            final_message="done",
            leaked=self.leaked,
            transcript=self.transcript,
        )


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


def test_build_fails_when_generated_code_fails_tests(tmp_path, clean_git_env, attended_env):
    repo = make_git_repo(tmp_path)
    gh = FakeGh({("issue", "list"): _issue(5, "broken")})
    ledger_path = tmp_path / "ledger.jsonl"
    runner = FakeSessionRunner(files={"pkg/a.py": "a = 1\n"})
    coder = _coder(
        repo.path, gh, ledger_path, runner, run_tests=True, test_command=["python", "-c", "exit(1)"]
    )
    result = coder.run(issue_number=5)

    assert result.outcome == "failure"
    assert result.tests_passed is False
    assert not gh.saw("pr", "create")


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
