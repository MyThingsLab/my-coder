from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from mythings import _secrets

# The tools a headless worker session may use, ported from the fleet's proven
# worker invocation (my-fleet `fleet_dispatch.DEFAULT_ALLOWED_TOOLS`): Read/Edit/
# Write plus git, the test runner, the linter, and non-mutating shell
# inspection. `rm`/`pip`/`find` stay off (they mutate or run code); `gh` stays
# off deliberately — a v0 my-coder session edits and *commits* only, and
# my-coder itself owns the single push + draft-PR side effect so that one step
# is the only thing Policy/Guard has to gate.
ALLOWED_TOOLS = [
    "Read",
    "Edit",
    "Write",
    "Bash(git *)",
    "Bash(pytest*)",
    "Bash(python -m pytest*)",
    "Bash(python3 -m pytest*)",
    "Bash(ruff*)",
    "Bash(python -m ruff*)",
    "Bash(python3 -m ruff*)",
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
]

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


def _parse_result(stdout: str) -> tuple[float, int, str, bool]:
    # claude's stream-json output ends on one `type=result` line carrying the
    # settled cost / turn count / final reply; everything before it is
    # incremental. A truncated or unparsable stream leaves the defaults.
    cost, turns, final, is_error = 0.0, 0, "", False
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "result":
            cost = float(obj.get("total_cost_usd", 0.0) or 0.0)
            turns = int(obj.get("num_turns", 0) or 0)
            final = str(obj.get("result", "") or "")
            is_error = bool(obj.get("is_error", False))
    return cost, turns, final, is_error


@dataclass(frozen=True)
class SessionResult:
    ok: bool  # process exited 0, no is_error flag, no timeout
    turns: int = 0
    cost_usd: float = 0.0
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
    ) -> None:
        self._runner = runner

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
            "--disallowedTools",
            *DENY_READS,
            "--allowedTools",
            *ALLOWED_TOOLS,
        ]
        try:
            proc = self._runner(
                argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_s
            )
        except subprocess.TimeoutExpired as exc:
            raw = exc.stdout or ""
            if isinstance(raw, bytes):
                raw = raw.decode(errors="replace")
            clean, leaked = redact_secrets(raw)
            return SessionResult(
                ok=False,
                transcript=clean,
                leaked=leaked,
                error=f"session exceeded {timeout_s:.0f}s wall-clock timeout",
            )

        clean, leaked = redact_secrets(proc.stdout or "")
        cost, turns, final, is_error = _parse_result(clean)
        ok = proc.returncode == 0 and not is_error
        error = None
        if not ok:
            error = f"claude exited {proc.returncode}" + (" (is_error)" if is_error else "")
        return SessionResult(
            ok=ok,
            turns=turns,
            cost_usd=cost,
            final_message=final,
            transcript=clean,
            leaked=leaked,
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
