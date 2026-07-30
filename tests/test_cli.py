from __future__ import annotations

import json
from pathlib import Path

from mycoder.cli import main
from mycoder.coder import Result
from mycoder.session import ClaudeSessionRunner, NoopSessionRunner


class StubCoder:
    instances: list[StubCoder] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.ran_issue: int | None = None
        self.result = Result("success", "ok", issue=5, pr=7, files_touched=["a.py"])
        StubCoder.instances.append(self)

    def run(self, issue_number: int) -> Result:
        self.ran_issue = issue_number
        return self.result


def _build(argv, capsys):
    StubCoder.instances = []
    rc = main(["build", *argv], coder_factory=StubCoder)
    return rc, StubCoder.instances[-1], capsys.readouterr().out


def test_build_wires_args_and_returns_zero(capsys):
    rc, coder, _ = _build(
        ["--repo", "o/r", "--issue", "5", "--source", ".", "--session-runner", "noop"], capsys
    )
    assert rc == 0
    assert coder.ran_issue == 5
    assert coder.kwargs["repo_slug"] == "o/r"
    assert isinstance(coder.kwargs["session_runner"], NoopSessionRunner)
    assert coder.kwargs["max_turns"] == 60  # generous default, a small issue needs ~30
    # transcripts co-locate with the ledger, not the invoking CWD.
    assert coder.kwargs["transcripts_dir"] == Path(".mythings/mycoder-transcripts")


def test_build_claude_runner_and_caps(capsys):
    _, coder, _ = _build(
        ["--repo", "o/r", "--issue", "1", "--session-runner", "claude", "--max-budget-usd", "2.5"],
        capsys,
    )
    assert isinstance(coder.kwargs["session_runner"], ClaudeSessionRunner)
    assert coder.kwargs["max_budget_usd"] == 2.5


def test_build_json_output(capsys):
    rc, _, out = _build(["--repo", "o/r", "--issue", "5", "--json"], capsys)
    assert rc == 0
    payload = json.loads(out)
    assert payload["outcome"] == "success"
    assert payload["pr"] == 7


def test_build_defaults_to_unguarded(capsys):
    _, coder, _ = _build(["--repo", "o/r", "--issue", "5"], capsys)
    assert coder.kwargs["policy"] is None


def test_build_guarded_wires_a_myguard_policy(capsys):
    from myguard.guard import Guard

    _, coder, _ = _build(["--repo", "o/r", "--issue", "5", "--guarded"], capsys)
    assert isinstance(coder.kwargs["policy"], Guard)


def test_build_failure_returns_nonzero(capsys):
    StubCoder.instances = []

    class Failing(StubCoder):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.result = Result("failure", "session failed", issue=5)

    rc = main(["build", "--repo", "o/r", "--issue", "5"], coder_factory=Failing)
    assert rc == 1


def test_build_parses_a_shell_quoted_test_command(capsys):
    # my-coder#15: without this an operator cannot point --run-tests at an
    # environment where the target repo's dependencies are importable.
    _, coder, _ = _build(
        [
            "--repo",
            "o/r",
            "--issue",
            "5",
            "--run-tests",
            "--test-command",
            "/tmp/venv/bin/python -m pytest -q",
        ],
        capsys,
    )
    assert coder.kwargs["test_command"] == ["/tmp/venv/bin/python", "-m", "pytest", "-q"]


def test_build_defaults_the_test_command_to_none(capsys):
    _, coder, _ = _build(["--repo", "o/r", "--issue", "5"], capsys)
    assert coder.kwargs["test_command"] is None


def test_claude_runner_is_fleet_scoped_by_the_target_slug(capsys):
    _build(
        ["--repo", "MyThingsLab/my-raytracer", "--issue", "5", "--session-runner", "claude"], capsys
    )
    in_fleet = StubCoder.instances[-1].kwargs["session_runner"]
    assert "Bash(gh issue create*)" in in_fleet._allowed_tools

    _build(["--repo", "someone-else/theirs", "--issue", "5", "--session-runner", "claude"], capsys)
    out_of_fleet = StubCoder.instances[-1].kwargs["session_runner"]
    assert "Bash(gh issue create*)" not in out_of_fleet._allowed_tools
