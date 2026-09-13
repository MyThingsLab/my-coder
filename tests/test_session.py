from __future__ import annotations

import json
import subprocess
from pathlib import Path

from mycoder.session import (
    ALLOWED_TOOLS,
    DENY_READS,
    ClaudeSessionRunner,
    GeminiSessionRunner,
    NoopSessionRunner,
    _parse_result,
    allowed_tools,
    billed,
    child_env,
    parse_partial,
    redact_secrets,
)

_RESULT_LINE = (
    '{"type":"result","total_cost_usd":0.042,"num_turns":7,"result":"all done","is_error":false}'
)


def test_parse_result_reads_the_final_result_line() -> None:
    stdout = '{"type":"assistant"}\n' + _RESULT_LINE + "\n"
    result = _parse_result(stdout)
    assert result.cost == 0.042
    assert result.turns == 7
    assert result.final == "all done"
    assert result.is_error is False


def test_parse_result_tolerates_garbage_lines() -> None:
    result = _parse_result("not json\n\n")
    assert (result.cost, result.turns, result.final, result.is_error) == (0.0, 0, "", False)


def test_parse_result_keeps_the_subtype_that_says_how_it_ended() -> None:
    # "error_max_turns" and "error_during_execution" are a raised limit and a
    # bug respectively; without the subtype they are the same ledger record.
    stdout = (
        '{"type":"result","total_cost_usd":1.95,"num_turns":40,'
        '"result":"","is_error":true,"subtype":"error_max_turns"}\n'
    )
    result = _parse_result(stdout)
    assert result.subtype == "error_max_turns"
    assert result.is_error is True


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


def test_a_failed_session_keeps_stderr_in_the_error() -> None:
    # my-coder#28: "claude exited 1" was the entire diagnosis. stderr is the
    # only channel carrying a crash that happens before the stream starts, so
    # dropping it left nothing to tell a bug from a raised limit.
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="Error: ENOSPC: no space left on device"
        )

    result = ClaudeSessionRunner(runner=fake_run).run(
        prompt="x", cwd=Path("/tmp"), max_budget_usd=1.0, max_turns=1, timeout_s=1.0
    )

    assert result.ok is False
    assert "ENOSPC" in (result.error or "")


def test_a_failed_session_names_the_limit_it_hit_and_what_it_said() -> None:
    # The shape from the first real corpus run: work was done, cost was real,
    # and the session hit its turn cap. "Raise --max-turns" and "fix the bug"
    # are different responses, so the subtype has to survive into the record.
    stream = _stream(
        {"type": "assistant", "message": {"role": "assistant", "usage": {}}},
        {
            "type": "result",
            "total_cost_usd": 1.95,
            "num_turns": 40,
            "result": "I ran out of turns before I could commit.",
            "is_error": True,
            "subtype": "error_max_turns",
        },
    )

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout=stream, stderr="")

    result = ClaudeSessionRunner(runner=fake_run).run(
        prompt="x", cwd=Path("/tmp"), max_budget_usd=3.0, max_turns=40, timeout_s=60.0
    )

    assert result.ok is False
    error = result.error or ""
    assert "error_max_turns" in error
    assert "ran out of turns" in error
    assert result.cost_usd == 1.95


def test_a_failed_sessions_stderr_is_redacted_before_it_is_recorded() -> None:
    # The error string lands in a ledger record, so stderr gets the same
    # scrubbing stdout already had rather than becoming a new leak path.
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="auth failed for sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF"
        )

    result = ClaudeSessionRunner(runner=fake_run).run(
        prompt="x", cwd=Path("/tmp"), max_budget_usd=1.0, max_turns=1, timeout_s=1.0
    )

    assert "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFF" not in (result.error or "")
    assert result.leaked, "the redaction must be reported, not silent"


def test_claude_runner_handles_a_timeout() -> None:
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0), output="partial")

    result = ClaudeSessionRunner(runner=fake_run).run(
        prompt="x", cwd=Path("/tmp"), max_budget_usd=1.0, max_turns=1, timeout_s=30.0
    )
    assert result.ok is False
    assert "timeout" in (result.error or "")
    assert result.transcript == "partial"


def _stream(*objs: dict) -> str:
    return "\n".join(json.dumps(o) for o in objs)


def _assistant(**usage: int) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "usage": usage}}


def test_a_timeout_never_meters_as_free() -> None:
    # my-coder#18: a timed-out session recorded cost_usd=0.0, and `failure` is
    # retryable -- so the priciest way a session can end metered as the cheapest.
    partial = _stream(
        {"type": "system", "subtype": "init"},
        _assistant(input_tokens=10, output_tokens=200),
        _assistant(input_tokens=5, cache_read_input_tokens=1000, output_tokens=300),
    )

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0), output=partial)

    result = ClaudeSessionRunner(runner=fake_run).run(
        prompt="x", cwd=Path("/tmp"), max_budget_usd=3.0, max_turns=40, timeout_s=30.0
    )
    assert result.cost_usd is None, "unknown must not collapse to 0.0"
    assert billed(result.cost_usd, cap=3.0) == 3.0
    assert result.turns == 2
    assert result.tokens == 1515


def test_billed_passes_a_known_cost_through_untouched() -> None:
    assert billed(0.42, cap=3.0) == 0.42
    assert billed(0.0, cap=3.0) == 0.0  # a genuine zero is not the same as unknown


def test_parse_partial_ignores_non_assistant_lines_and_junk() -> None:
    stream = "\n".join(
        [
            "not json at all",
            json.dumps({"type": "user", "message": {"usage": {"output_tokens": 999}}}),
            json.dumps(_assistant(output_tokens=7)),
            json.dumps([1, 2, 3]),  # valid json, not an object
            "",
        ]
    )
    assert parse_partial(stream) == (1, 7)


def test_a_settled_stream_still_reports_its_real_cost() -> None:
    settled = _stream(
        _assistant(output_tokens=5),
        {
            "type": "result",
            "total_cost_usd": 0.13,
            "num_turns": 6,
            "result": "done",
            "is_error": False,
        },
    )

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=settled, stderr="")

    result = ClaudeSessionRunner(runner=fake_run).run(
        prompt="x", cwd=Path("/tmp"), max_budget_usd=3.0, max_turns=40, timeout_s=30.0
    )
    assert result.cost_usd == 0.13
    assert result.turns == 6  # the settled count, not the assistant-line count


def test_noop_runner_never_changes_anything() -> None:
    result = NoopSessionRunner().run(
        prompt="x", cwd=Path("/tmp"), max_budget_usd=1.0, max_turns=1, timeout_s=1.0
    )
    assert result.ok is True
    assert result.turns == 0


def test_allowlist_admits_install_subcommands_but_not_bare_installers() -> None:
    # my-coder#15: a worktree carries only tracked files, so a target repo's
    # dependencies must be installable or its suite is unrunnable.
    assert "Bash(uv pip install*)" in ALLOWED_TOOLS
    assert "Bash(pip install*)" in ALLOWED_TOOLS
    assert "Bash(python3 -m pip install*)" in ALLOWED_TOOLS
    # Install only -- never a bare installer that could uninstall or reconfigure.
    assert "Bash(pip*)" not in ALLOWED_TOOLS
    assert "Bash(uv*)" not in ALLOWED_TOOLS


def test_out_of_fleet_session_cannot_reach_github() -> None:
    # my-coder#14: `gh issue create` exists only for the in-org blocker protocol,
    # and the `critical` label it uses halts fleet dispatch org-wide.
    assert "Bash(gh issue create*)" in allowed_tools(in_fleet=True)
    assert "Bash(gh issue create*)" not in allowed_tools(in_fleet=False)
    # Nothing else is withheld.
    assert set(allowed_tools(in_fleet=True)) - set(allowed_tools(in_fleet=False)) == {
        "Bash(gh issue create*)"
    }


def test_runner_passes_the_out_of_fleet_allowlist_to_claude(tmp_path) -> None:
    seen: dict[str, list[str]] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=_RESULT_LINE + "\n", stderr="")

    ClaudeSessionRunner(runner=fake_run, in_fleet=False).run(
        prompt="p", cwd=tmp_path, max_budget_usd=1.0, max_turns=5, timeout_s=10.0
    )
    assert "Bash(gh issue create*)" not in seen["argv"]
    assert "Bash(uv pip install*)" in seen["argv"]


def test_allowlist_closes_the_install_then_verify_loop() -> None:
    # my-coder#15: on a PEP 668 host the only importable environment is a venv
    # inside the worktree, so the session must be able to run *that* one.
    assert "Bash(uv venv*)" in ALLOWED_TOOLS
    assert "Bash(.venv/bin/pip install*)" in ALLOWED_TOOLS
    assert "Bash(.venv/bin/python -m pytest*)" in ALLOWED_TOOLS
    assert "Bash(.venv/bin/ruff*)" in ALLOWED_TOOLS


# --- GeminiSessionRunner tests --------------------------------------------

_AGY_RESULT_LINE = (
    '{"event":"result","result":{"status":"SUCCESS","response":"all done",'
    '"num_turns":5,"usage":{"total_tokens":1234},"cost_usd":0.05}}\n'
)


def test_parse_result_reads_agy_event_result() -> None:
    result = _parse_result(_AGY_RESULT_LINE)
    assert result.cost == 0.05
    assert result.turns == 5
    assert result.final == "all done"
    assert result.is_error is False
    assert result.subtype == "SUCCESS"


def test_parse_result_reads_agy_error_result() -> None:
    line = (
        '{"event":"result","result":{"status":"ERROR",'
        '"response":"failed to execute","is_error":true}}\n'
    )
    result = _parse_result(line)
    assert result.is_error is True
    assert result.final == "failed to execute"
    assert result.subtype == "ERROR"


def test_parse_partial_reads_agy_step_updates() -> None:
    stream = "\n".join(
        [
            '{"event":"init","init":{"cwd":"/tmp"}}',
            (
                '{"event":"step_update","step_update":{"step_index":1,"state":"DONE",'
                '"step_type":"agent_response","usage":{"total_tokens":100}}}'
            ),
            (
                '{"event":"step_update","step_update":{"step_index":2,"state":"DONE",'
                '"step_type":"tool","usage":{"total_tokens":150}}}'
            ),
        ]
    )
    messages, tokens = parse_partial(stream)
    assert messages == 2
    assert tokens == 250


def test_gemini_runner_parses_a_successful_session(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=_AGY_RESULT_LINE, stderr="")

    runner = GeminiSessionRunner(bin_name="agy", runner=fake_run)
    result = runner.run(
        prompt="do it", cwd=tmp_path, max_budget_usd=5.0, max_turns=40, timeout_s=1800.0
    )
    assert result.ok is True
    assert result.cost_usd == 0.05
    assert result.turns == 5
    assert result.final_message == "all done"
    assert result.error is None
    argv = calls[0]
    assert argv[:3] == ["agy", "-p", "do it"]
    assert "--output-format" in argv
    assert "--dangerously-skip-permissions" in argv


def test_gemini_runner_with_model_and_effort(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=_AGY_RESULT_LINE, stderr="")

    runner = GeminiSessionRunner(
        bin_name="gemini",
        model="gemini-2.5-flash",
        effort="high",
        runner=fake_run,
    )
    runner.run(
        prompt="build", cwd=tmp_path, max_budget_usd=1.0, max_turns=10, timeout_s=60.0
    )
    argv = calls[0]
    assert "--model" in argv and argv[argv.index("--model") + 1] == "gemini-2.5-flash"
    assert "--effort" in argv and argv[argv.index("--effort") + 1] == "high"
    assert "--approval-mode" in argv and argv[argv.index("--approval-mode") + 1] == "yolo"


def test_gemini_runner_reports_nonzero_exit_with_diagnosis(tmp_path: Path) -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Quota exceeded")

    runner = GeminiSessionRunner(bin_name="gemini", runner=fake_run)
    result = runner.run(
        prompt="build", cwd=tmp_path, max_budget_usd=1.0, max_turns=10, timeout_s=60.0
    )
    assert result.ok is False
    assert "gemini exited 1" in (result.error or "")
    assert "Quota exceeded" in (result.error or "")


def test_gemini_runner_handles_timeout(tmp_path: Path) -> None:
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0), output="partial agy log")

    runner = GeminiSessionRunner(bin_name="agy", runner=fake_run)
    result = runner.run(
        prompt="build", cwd=tmp_path, max_budget_usd=1.0, max_turns=10, timeout_s=30.0
    )
    assert result.ok is False
    assert "session exceeded 30s wall-clock timeout" in (result.error or "")
    assert result.transcript == "partial agy log"


def test_gemini_runner_redacts_credentials(tmp_path: Path) -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="auth failed for ghp_0123456789abcdefghijklmnopqrstuvwxyz"
        )

    runner = GeminiSessionRunner(bin_name="gemini", runner=fake_run)
    result = runner.run(
        prompt="build", cwd=tmp_path, max_budget_usd=1.0, max_turns=10, timeout_s=60.0
    )
    assert "ghp_0123456789abcdefghijklmnopqrstuvwxyz" not in (result.error or "")
    assert result.leaked


def test_child_env_sanitizes_gemini_and_preserves_config() -> None:
    base = {
        "GEMINI_CONFIG_DIR": "/home/bot/.gemini",
        "GEMINI_CLI_BIN": "/usr/local/bin/agy",
        "GEMINI_AGENT": "1",
        "ANTIGRAVITY_AGENT": "1",
        "GEMINI_CLI_TOKEN": "abc",
        "PATH": "/usr/bin",
    }
    env = child_env(base)
    assert env["GEMINI_CONFIG_DIR"] == "/home/bot/.gemini"
    assert env["GEMINI_CLI_BIN"] == "/usr/local/bin/agy"
    assert env["PATH"] == "/usr/bin"
    assert "GEMINI_AGENT" not in env
    assert "ANTIGRAVITY_AGENT" not in env
    assert "GEMINI_CLI_TOKEN" not in env


def test_parse_result_reads_standalone_status_object() -> None:
    line = '{"status":"SUCCESS","response":"direct reply","num_turns":3,"cost_usd":0.02}\n'
    result = _parse_result(line)
    assert result.final == "direct reply"
    assert result.turns == 3
    assert result.cost == 0.02
    assert result.is_error is False


def test_gemini_runner_handles_timeout_with_bytes_output(tmp_path: Path) -> None:
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 0), output=b"bytes log")

    runner = GeminiSessionRunner(bin_name="agy", runner=fake_run)
    result = runner.run(
        prompt="build", cwd=tmp_path, max_budget_usd=1.0, max_turns=10, timeout_s=30.0
    )
    assert result.ok is False
    assert result.transcript == "bytes log"


