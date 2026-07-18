from __future__ import annotations

import subprocess
from pathlib import Path

from mycoder.session import SessionResult

# Shared fakes (FakeGh, make_git_repo, clean_git_env, attended_env, ...) live in
# the SDK so no tool hand-rolls a duplicate boundary mock.
pytest_plugins = ("mythings.testing",)


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
    ) -> None:
        self.files = files or {}
        self.ok = ok
        self.commit = commit
        self.leaked = leaked or []
        self.error = error
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
        if not self.ok:
            return SessionResult(ok=False, error=self.error or "claude exited 1")
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
        return SessionResult(
            ok=True, turns=3, cost_usd=0.01, final_message="done", leaked=self.leaked
        )
