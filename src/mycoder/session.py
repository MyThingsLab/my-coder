from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from mythings import _secrets

# The tools a headless worker session may use, ported from the fleet's proven
# worker invocation (my-fleet `fleet_dispatch.DEFAULT_ALLOWED_TOOLS`): Read/Edit/
# Write plus git, the test runner, the linter, dependency installation, and
# non-mutating shell inspection. `rm`/`find` stay off, and so does any bare
# `pip`/`uv` — only their install subcommands are admitted (see below); `gh`
# stays off except the two narrow escapes the blocker/critical-bug protocol
# needs (filing an issue in ANOTHER repo), and those are withheld outside the
# fleet. A my-coder session otherwise edits and *commits* only, and my-coder
# itself owns the single push + draft-PR side effect for the target repo so that
# one step is the only thing Policy/Guard has to gate.
ALLOWED_TOOLS = [
    "Read",
    "Edit",
    "Write",
    "Bash(git *)",
    "Bash(gh issue create*)",
    "Bash(pytest*)",
    "Bash(python -m pytest*)",
    "Bash(python3 -m pytest*)",
    "Bash(ruff*)",
    "Bash(python -m ruff*)",
    "Bash(python3 -m ruff*)",
    # The venv-local forms close the install→verify loop. A PEP 668 host (any
    # recent Debian/Ubuntu) refuses to install into the system interpreter at
    # all, so the only route to an importable dependency is a venv inside the
    # worktree -- and without these the session could build that venv and still
    # not run anything with it.
    "Bash(.venv/bin/pytest*)",
    "Bash(.venv/bin/python -m pytest*)",
    "Bash(.venv/bin/ruff*)",
    "Bash(.venv/bin/python -m ruff*)",
    "Bash(ls*)",
    "Bash(cat*)",
    "Bash(head*)",
    "Bash(tail*)",
    "Bash(wc*)",
    "Bash(grep*)",
    "Bash(pwd*)",
    "Bash(printenv*)",
    "Bash(env)",
    "Bash(python3 -m venv*)",
    "Bash(uv venv*)",
    # Install subcommands only, never bare `pip`/`uv`. A worktree carries only
    # tracked files, so a target repo's dependencies are absent and its suite is
    # unrunnable -- the session was being told to leave tests green with no way
    # to run them (my-coder#15). This is a deliberate widening of the sandbox:
    # installing a package executes arbitrary code from the network inside the
    # worktree. Accepted because the alternative is a worker that commits code
    # it cannot verify; revisit before running unattended against a repo whose
    # dependency list is not trusted.
    "Bash(pip install*)",
    "Bash(pip3 install*)",
    "Bash(uv pip install*)",
    "Bash(uv sync*)",
    "Bash(python -m pip install*)",
    "Bash(python3 -m pip install*)",
    "Bash(.venv/bin/pip install*)",
]

# Withheld when the target repo is outside the fleet: `gh issue create` exists
# only for the in-org blocker/critical-bug protocol, and the `critical` label it
# uses halts fleet dispatch org-wide. A session pointed at someone else's repo
# must not be able to reach into the org at all (my-coder#14).
FLEET_ONLY_TOOLS = frozenset({"Bash(gh issue create*)"})


def allowed_tools(*, in_fleet: bool = True) -> list[str]:
    if in_fleet:
        return list(ALLOWED_TOOLS)
    return [t for t in ALLOWED_TOOLS if t not in FLEET_ONLY_TOOLS]


# Passed as `--disallowedTools`: never burn tokens reading generated/vendored/
# provenance noise, and never rewrite the venv or dev-ledger. The session is
# already filesystem-isolated to one repo's worktree, so these only hide noise
# within it (ported from my-fleet `fleet_dispatch.DEFAULT_DENY_READS`).
DENY_READS = [
    "Read(**/.venv/**)",
    "Read(**/__pycache__/**)",
    "Read(**/*.pyc)",
    "Read(**/.ruff_cache/**)",
    "Read(**/.pytest_cache/**)",
    "Read(**/.git/**)",
    "Read(**/node_modules/**)",
    "Read(**/dev-ledger/**)",
    "Edit(**/.venv/**)",
    "Edit(**/dev-ledger/**)",
]

# Non-config Claude-Code / Gemini session markers. A worker session is itself a
# CLI subprocess; if it inherits these from a parent agent session it believes it
# is nested and routes every tool call through a permission prompt that has no
# answerer, silently blocking all execution. Stripped from the child env;
# config dirs are kept because they select the account/identity the session runs under.
_DROP_ENV = frozenset(
    {"CLAUDECODE", "AI_AGENT", "GEMINI_AGENT", "ANTIGRAVITY_AGENT"}
)


def child_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    for key in list(env):
        if key in ("CLAUDE_CONFIG_DIR", "GEMINI_CONFIG_DIR", "GEMINI_CLI_BIN"):
            continue
        if key.startswith("CLAUDE_CODE_") or key.startswith("GEMINI_CLI_") or key in _DROP_ENV:
            del env[key]
    return env


def redact_secrets(text: str) -> tuple[str, list[str]]:
    # A session transcript is persisted and summarised into the ledger; if a
    # session ever echoes a credential (a leaked token in a fetched page, a
    # printenv), both records would keep it forever in a public repo. Redact
    # anything credential-shaped before either is written — redaction over
    # rejection keeps the transcript's forensic value while removing the span.
    findings = _secrets.scan_text(text)
    if not findings:
        return text, []
    for name, pattern in _secrets._PATTERNS.items():
        text = pattern.sub(f"[REDACTED-{name}]", text)
    return text, sorted({f.pattern for f in findings})


def _iter_objects(stdout: str) -> Iterator[dict]:
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            yield obj


def parse_partial(stdout: str) -> tuple[int, int]:
    # A killed stream never carries the settled `type=result` line, so it holds
    # no dollar figure at all -- only incremental `assistant` lines and their
    # token `usage`. Recover what is genuinely there: how many assistant
    # messages were seen and how many tokens they accounted for. Both are
    # evidence the run was not free; neither is a price, which is why the cost
    # itself stays unknown rather than being reconstructed from a pricing table
    # that would drift out of date and lie with confidence.
    #
    # `messages` is an *upper* bound on the settled `num_turns` (a thinking-only
    # message is its own line). It feeds the ledger for diagnostics only -- the
    # real turn ceiling is enforced by `--max-turns` inside claude.
    messages = tokens = 0
    for obj in _iter_objects(stdout):
        if obj.get("type") == "assistant":
            messages += 1
            usage = (obj.get("message") or {}).get("usage") or {}
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            ):
                tokens += int(usage.get(key) or 0)
        elif obj.get("event") == "step_update":
            step = obj.get("step_update") or {}
            step_type = step.get("step_type")
            if step_type in ("agent_response", "tool"):
                messages += 1
            usage = step.get("usage") or {}
            tokens += int(
                usage.get("total_tokens")
                or (
                    int(usage.get("input_tokens") or 0)
                    + int(usage.get("output_tokens") or 0)
                )
            )
    return messages, tokens


def billed(cost_usd: float | None, cap: float) -> float:
    # An unknown cost meters as the cap, never as zero. A timeout is the most
    # expensive way a session can end and the only shape carrying no price;
    # counting it free let a repeatedly-timing-out candidate run well past a
    # ceiling that believed nothing had been spent (my-coder#18).
    return cap if cost_usd is None else cost_usd


@dataclass(frozen=True)
class _Result:
    cost: float = 0.0
    turns: int = 0
    final: str = ""
    is_error: bool = False
    # The engine's own name for *how* it ended -- "error_max_turns",
    # "error_during_execution", "success", "SUCCESS", etc.
    subtype: str = ""


def _parse_result(stdout: str) -> _Result:
    # stream-json / json output ends on a result line carrying the settled cost /
    # turn count / final reply. Handles Claude, Antigravity/Gemini, and generic envelopes.
    result = _Result()
    for obj in _iter_objects(stdout):
        if obj.get("type") == "result":
            result = _Result(
                cost=float(obj.get("total_cost_usd", 0.0) or 0.0),
                turns=int(obj.get("num_turns", 0) or 0),
                final=str(obj.get("result", "") or ""),
                is_error=bool(obj.get("is_error", False)),
                subtype=str(obj.get("subtype", "") or ""),
            )
        elif obj.get("event") == "result" and isinstance(obj.get("result"), dict):
            res = obj["result"]
            status = str(res.get("status", "") or "")
            is_error = bool(res.get("is_error", False)) or (bool(status) and status != "SUCCESS")
            final = str(res.get("response") or res.get("result") or "")
            turns = int(res.get("num_turns", 0) or 0)
            cost = float(res.get("cost_usd", 0.0) or res.get("total_cost_usd", 0.0) or 0.0)
            subtype = str(res.get("subtype") or status or "")
            result = _Result(
                cost=cost,
                turns=turns,
                final=final,
                is_error=is_error,
                subtype=subtype,
            )
        elif "status" in obj or "response" in obj:
            status = str(obj.get("status", "") or "")
            is_error = bool(obj.get("is_error", False)) or (bool(status) and status != "SUCCESS")
            final = str(obj.get("response") or obj.get("result") or "")
            turns = int(obj.get("num_turns", 0) or 0)
            cost = float(obj.get("cost_usd", 0.0) or obj.get("total_cost_usd", 0.0) or 0.0)
            subtype = str(obj.get("subtype") or status or "")
            result = _Result(
                cost=cost,
                turns=turns,
                final=final,
                is_error=is_error,
                subtype=subtype,
            )
    return result


def describe_failure(
    returncode: int, result: _Result, stderr: str, *, engine_name: str = "claude"
) -> str:
    """Build the one string a reader gets when a session ends badly."""
    parts = [f"{engine_name} exited {returncode}"]
    flags = [f for f in (result.subtype, "is_error" if result.is_error else "") if f]
    if flags:
        parts[0] += f" ({', '.join(flags)})"
    for label, text in (("said", result.final), ("stderr", stderr)):
        collapsed = " ".join(text.split())
        if collapsed:
            parts.append(f"{label}: {collapsed[:300]}")
    return "; ".join(parts)


@dataclass(frozen=True)
class SessionResult:
    ok: bool  # process exited 0, no is_error flag, no timeout
    turns: int = 0
    # None means *unknown*, not free: a killed stream carries no settled price.
    # Callers must meter it through `billed()` rather than reading it as 0.0.
    cost_usd: float | None = 0.0
    tokens: int = 0  # best-effort, from the partial stream; 0 when not recovered
    final_message: str = ""
    transcript: str = ""  # redacted stream-json stdout
    leaked: list[str] = field(default_factory=list)  # secret-pattern names redacted
    error: str | None = None


class SessionRunner(Protocol):
    def run(
        self,
        *,
        prompt: str,
        cwd: Path,
        max_budget_usd: float,
        max_turns: int,
        timeout_s: float,
    ) -> SessionResult: ...


class ClaudeSessionRunner:
    # The one seam that is NOT a `mythings.engine.Engine` call: a multi-turn,
    # tools-*enabled* headless `claude -p` session, bounded three ways
    # (--max-budget-usd, --max-turns, wall-clock timeout). `runner` is injected
    # so tests never shell out to a real CLI.
    def __init__(
        self,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        in_fleet: bool = True,
    ) -> None:
        self._runner = runner
        self._allowed_tools = allowed_tools(in_fleet=in_fleet)

    def run(
        self,
        *,
        prompt: str,
        cwd: Path,
        max_budget_usd: float,
        max_turns: int,
        timeout_s: float,
    ) -> SessionResult:
        argv = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-budget-usd",
            str(max_budget_usd),
            "--max-turns",
            str(max_turns),
            # Load only the target repo's own settings, never the operator's
            # user-level ones. A personal user hook (e.g. an `rtk` PreToolUse
            # command-rewriter that turns `pytest` into `rtk pytest`) rewrites
            # the command out from under `--allowedTools`, so the rewritten
            # form fails the allowlist and the session can never run its tests.
            # A worker session must be deterministic and depend only on the
            # repo it is editing.
            "--setting-sources",
            "project,local",
            "--disallowedTools",
            *DENY_READS,
            "--allowedTools",
            *self._allowed_tools,
        ]
        try:
            proc = self._runner(
                argv,
                cwd=str(cwd),
                env=child_env(),
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raw = exc.stdout or ""
            if isinstance(raw, bytes):
                raw = raw.decode(errors="replace")
            clean, leaked = redact_secrets(raw)
            messages, tokens = parse_partial(clean)
            return SessionResult(
                ok=False,
                turns=messages,
                cost_usd=None,  # unknown, NOT zero -- see `billed()`
                tokens=tokens,
                transcript=clean,
                leaked=leaked,
                error=f"session exceeded {timeout_s:.0f}s wall-clock timeout",
            )

        clean, leaked = redact_secrets(proc.stdout or "")
        # stderr goes through the same scrubber as stdout before it reaches a
        # ledger record: it is untrusted output that now gets persisted.
        clean_stderr, stderr_leaked = redact_secrets(proc.stderr or "")
        result = _parse_result(clean)
        ok = proc.returncode == 0 and not result.is_error
        error = None
        if not ok:
            error = describe_failure(proc.returncode, result, clean_stderr)
        return SessionResult(
            ok=ok,
            turns=result.turns,
            cost_usd=result.cost,
            final_message=result.final,
            transcript=clean,
            leaked=sorted(set(leaked) | set(stderr_leaked)),
            error=error,
        )


class GeminiSessionRunner:
    # The headless session runner for Gemini / Antigravity CLI.
    # Shells out to the configured CLI binary (GEMINI_CLI_BIN or agy/gemini),
    # bounded three ways (spend/tokens, turns, and wall-clock timeout).
    # `runner` is injected so tests never shell out to a real CLI.
    def __init__(
        self,
        *,
        bin_name: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        in_fleet: bool = True,
    ) -> None:
        self._bin_name = bin_name or os.environ.get("GEMINI_CLI_BIN", "agy")
        self._model = model
        self._effort = effort
        self._runner = runner
        self._allowed_tools = allowed_tools(in_fleet=in_fleet)

    def run(
        self,
        *,
        prompt: str,
        cwd: Path,
        max_budget_usd: float,
        max_turns: int,
        timeout_s: float,
    ) -> SessionResult:
        base_name = Path(self._bin_name).name
        argv = [
            self._bin_name,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
        ]
        if "agy" in base_name or "antigravity" in base_name:
            argv.append("--dangerously-skip-permissions")
        elif "gemini" in base_name:
            argv.extend(["--skip-trust", "--approval-mode", "yolo"])
        if self._model:
            argv.extend(["--model", self._model])
        if self._effort:
            argv.extend(["--effort", self._effort])

        try:
            proc = self._runner(
                argv,
                cwd=str(cwd),
                env=child_env(),
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raw = exc.stdout or ""
            if isinstance(raw, bytes):
                raw = raw.decode(errors="replace")
            clean, leaked = redact_secrets(raw)
            messages, tokens = parse_partial(clean)
            return SessionResult(
                ok=False,
                turns=messages,
                cost_usd=None,
                tokens=tokens,
                transcript=clean,
                leaked=leaked,
                error=f"session exceeded {timeout_s:.0f}s wall-clock timeout",
            )

        clean, leaked = redact_secrets(proc.stdout or "")
        clean_stderr, stderr_leaked = redact_secrets(proc.stderr or "")
        result = _parse_result(clean)
        ok = proc.returncode == 0 and not result.is_error
        error = None
        if not ok:
            error = describe_failure(
                proc.returncode, result, clean_stderr, engine_name=base_name
            )
        return SessionResult(
            ok=ok,
            turns=result.turns,
            cost_usd=result.cost,
            final_message=result.final,
            transcript=clean,
            leaked=sorted(set(leaked) | set(stderr_leaked)),
            error=error,
        )


class NoopSessionRunner:
    # A dry run: touches nothing, so the mechanical path around the session
    # (workspace → commit-count → outcome) can be exercised without a model.
    # A run with this runner always ends `no_changes` (it never commits).
    def run(
        self,
        *,
        prompt: str,
        cwd: Path,
        max_budget_usd: float,
        max_turns: int,
        timeout_s: float,
    ) -> SessionResult:
        return SessionResult(ok=True)
