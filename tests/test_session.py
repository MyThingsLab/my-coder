from __future__ import annotations

import subprocess
from pathlib import Path

from mycoder.session import (
    ALLOWED_TOOLS,
    DENY_READS,
    ClaudeSessionRunner,
    NoopSessionRunner,
    _parse_result,
    child_env,
    redact_secrets,
)

_RESULT_LINE = (
    '{"type":"result","total_cost_usd":0.042,"num_turns":7,"result":"all done","is_error":false}'
)


def test_parse_result_reads_the_final_result_line() -> None:
    stdout = '{"type":"assistant"}\n' + _RESULT_LINE + "\n"
    cost, turns, final, is_error = _parse_result(stdout)
    assert cost == 0.042
    assert turns == 7
    assert final == "all done"
    assert is_error is False


def test_parse_result_tolerates_garbage_lines() -> None:
    cost, turns, final, is_error = _parse_result("not json\n\n")
    assert (cost, turns, final, is_error) == (0.0, 0, "", False)


def test_redact_secrets_leaves_benign_text_untouched() -> None:
    text = "just some ordinary log output\nnothing to see"
    clean, leaked = redact_secrets(text)
    assert clean == text
    assert leaked == []


def test_redact_secrets_scrubs_a_credential() -> None:
    text = "leaked key AKIAIOSFODNN7EXAMPLE in the transcript"
    clean, leaked = redact_secrets(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in clean
    assert "[REDACTED-aws_access_key_id]" in clean
    assert leaked == ["aws_access_key_id"]


def test_claude_runner_parses_a_successful_session() -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=_RESULT_LINE, stderr="")

    runner = ClaudeSessionRunner(runner=fake_run)
    result = runner.run(
        prompt="do it", cwd=Path("/tmp"), max_budget_usd=5.0, max_turns=40, timeout_s=1800.0
    )
    assert result.ok is True
    assert result.cost_usd == 0.042
    assert result.turns == 7
    assert result.error is None
    argv = calls[0]
    assert argv[:3] == ["claude", "-p", "do it"]
    # The safety envelope reaches the CLI: tools are constrained both ways.
    assert "--allowedTools" in argv and "--disallowedTools" in argv
    assert all(tool in argv for tool in ALLOWED_TOOLS)
    assert all(deny in argv for deny in DENY_READS)
    # Only the target repo's settings load, so an operator's user-level hook
    # can't rewrite an allowlisted command (e.g. pytest) out from the session.
    assert argv[argv.index("--setting-sources") + 1] == "project,local"


def test_allowed_tools_permits_filing_a_blocker_issue_via_gh() -> None:
    # The only `gh` escape: the blocker/critical-bug protocol tells the model
    # to `gh issue create` in ANOTHER repo -- without this, that instruction
    # would be silently denied and the sentinel line would never get filed.
    assert "Bash(gh issue create*)" in ALLOWED_TOOLS


def test_claude_runner_sanitizes_the_child_env() -> None:
    seen: dict[str, str] = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, stdout=_RESULT_LINE, stderr="")

    ClaudeSessionRunner(runner=fake_run).run(
        prompt="do it", cwd=Path("/tmp"), max_budget_usd=5.0, max_turns=40, timeout_s=1800.0
    )
    # The nested-session markers never reach the child; identity survives.
    assert "CLAUDECODE" not in seen
    assert not any(k.startswith("CLAUDE_CODE_") for k in seen)


def test_child_env_drops_session_markers_but_keeps_config_dir() -> None:
    base = {
        "CLAUDE_CONFIG_DIR": "/home/bot/.claude-x",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "AI_AGENT": "1",
        "PATH": "/usr/bin",
    }
    env = child_env(base)
    assert env["CLAUDE_CONFIG_DIR"] == "/home/bot/.claude-x"
    assert env["PATH"] == "/usr/bin"
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert "AI_AGENT" not in env


def test_child_env_passthrough_when_no_markers() -> None:
    base = {"PATH": "/usr/bin", "HOME": "/home/bot"}
    assert child_env(base) == base


def test_claude_runner_reports_a_nonzero_exit_as_not_ok() -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    result = ClaudeSessionRunner(runner=fake_run).run(
        prompt="x", cwd=Path("/tmp"), max_budget_usd=1.0, max_turns=1, timeout_s=1.0
    )
    assert result.ok is False
    assert "exited 1" in (result.error or "")


def test_claude_runner_handles_a_timeout() -> None:
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0), output="partial")

    result = ClaudeSessionRunner(runner=fake_run).run(
        prompt="x", cwd=Path("/tmp"), max_budget_usd=1.0, max_turns=1, timeout_s=30.0
    )
    assert result.ok is False
    assert "timeout" in (result.error or "")
    assert result.transcript == "partial"


def test_noop_runner_never_changes_anything() -> None:
    result = NoopSessionRunner().run(
        prompt="x", cwd=Path("/tmp"), max_budget_usd=1.0, max_turns=1, timeout_s=1.0
    )
    assert result.ok is True
    assert result.turns == 0
