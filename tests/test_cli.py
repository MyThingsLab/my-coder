from __future__ import annotations

import json
from pathlib import Path

import pytest

import mycoder.cli as cli
from mycoder.cli import main
from mycoder.coder import Result
from mycoder.session import ClaudeSessionRunner, GeminiSessionRunner, NoopSessionRunner


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


def test_build_gemini_runner_and_caps(capsys):
    _, coder, _ = _build(
        ["--repo", "o/r", "--issue", "1", "--session-runner", "gemini", "--max-budget-usd", "2.5"],
        capsys,
    )
    assert isinstance(coder.kwargs["session_runner"], GeminiSessionRunner)
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


def test_build_guarded_wires_a_myguard_policy(capsys, monkeypatch):
    from myguard.guard import Guard

    # --guarded now requires an armed channel (#29): a Guard whose ASK nothing
    # can resolve blocks every PR it is asked about, silently.
    monkeypatch.setenv("MYTHINGS_ASK_CMD", "true")
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


def test_gemini_runner_is_fleet_scoped_by_the_target_slug(capsys):
    _build(
        ["--repo", "MyThingsLab/my-raytracer", "--issue", "5", "--session-runner", "gemini"], capsys
    )
    in_fleet = StubCoder.instances[-1].kwargs["session_runner"]
    assert "Bash(gh issue create*)" in in_fleet._allowed_tools

    _build(["--repo", "someone-else/theirs", "--issue", "5", "--session-runner", "gemini"], capsys)
    out_of_fleet = StubCoder.instances[-1].kwargs["session_runner"]
    assert "Bash(gh issue create*)" not in out_of_fleet._allowed_tools


# --- --guarded needs a channel (#29) --------------------------------------


def test_guarded_without_an_ask_channel_refuses_before_spending_anything(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The whole point is that this fires at argument-parsing time. A session
    # that runs, costs money and *then* cannot open a PR is the bug: three
    # corpus runs were discarded that way. `coder_factory` exploding proves
    # nothing was constructed, let alone run.
    monkeypatch.delenv("MYTHINGS_ASK_CMD", raising=False)

    def _never(**kwargs: object) -> object:
        raise AssertionError("a session must not be constructed without an ask channel")

    with pytest.raises(SystemExit) as exc:
        cli.main(
            ["build", "--repo", "o/r", "--issue", "1", "--guarded"],
            coder_factory=_never,  # type: ignore[arg-type]
        )

    assert exc.value.code == 2
    assert "MYTHINGS_ASK_CMD is unset" in capsys.readouterr().err


def test_guarded_with_a_channel_armed_proceeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYTHINGS_ASK_CMD", "true")
    built: list[bool] = []

    class _Coder:
        def __init__(self, **kwargs: object) -> None:
            built.append(kwargs["policy"] is not None)

        def run(self, *, issue_number: int):
            from mycoder.coder import Result

            return Result("success", "ok", issue=issue_number)

    assert (
        cli.main(
            ["build", "--repo", "o/r", "--issue", "1", "--guarded"],
            coder_factory=_Coder,  # type: ignore[arg-type]
        )
        == 0
    )
    assert built == [True]


def test_an_unarmed_ask_is_not_reported_as_a_denial() -> None:
    # A DENY is a human saying no; a surviving ASK is nobody having been asked.
    # Flattening both to "denied" is what hid #29 -- and `denied` exits 1 while
    # `needs_human` does not, so the distinction is load-bearing for callers.
    from mythings.policy import Decision, PolicyResult

    assert PolicyResult(Decision.ASK).under(unattended=False) is Decision.ASK
    assert PolicyResult(Decision.ASK).under(unattended=True) is Decision.DENY
