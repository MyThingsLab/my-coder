from pathlib import Path

from mythings.github import GitHub, Issue
from mythings.ledger import Ledger
from mythings.testing import FakeGh, make_git_repo

from mycoder.coder import Coder
from mycoder.session import NoopSessionRunner


def test_agent_context_pack_injected_when_symbol_matches(tmp_path: Path):
    calc_py = """def compute_total(a: int, b: int) -> int:
    \"\"\"Calculate total sum.\"\"\"
    return helper_add(a, b)

def helper_add(x: int, y: int) -> int:
    return x + y
"""
    git_repo = make_git_repo(
        tmp_path,
        files={"src/sample/calculator.py": calc_py, "README.md": "# Sample\n"},
    )

    ledger = Ledger(tmp_path / "ledger.jsonl")
    coder = Coder(
        ledger=ledger,
        repo="MyThingsLab/sample-repo",
        github=GitHub(runner=FakeGh()),
        session_runner=NoopSessionRunner(),
    )

    issue = Issue(
        number=42,
        title="Fix bug in compute_total calculation",
        body="compute_total should handle negative numbers properly.",
        url="https://github.com/MyThingsLab/sample-repo/issues/42",
        labels=["bug"],
    )

    prompt = coder._prompt(issue, git_repo.path)

    assert "## Deterministic Agent Context Pack (Grounded Focus & Blast Radius)" in prompt
    assert "Agent Context Pack (ACP): compute_total" in prompt
    assert "Target Symbol" in prompt
    assert "calculator.py" in prompt
    assert "def helper_add" in prompt
    assert "Scope Boundary Constraint" in prompt
    assert "Test Gap Alert" in prompt


def test_agent_context_pack_test_alert_omitted_when_test_present(tmp_path: Path):
    calc_py = "def compute_total(a: int, b: int) -> int:\n    return a + b\n"
    test_py = (
        "from sample.calculator import compute_total\n"
        "def test_compute_total():\n"
        "    assert compute_total(1, 2) == 3\n"
    )
    git_repo = make_git_repo(
        tmp_path,
        files={
            "src/sample/calculator.py": calc_py,
            "tests/test_calculator.py": test_py,
            "README.md": "# Sample\n",
        },
    )

    ledger = Ledger(tmp_path / "ledger.jsonl")
    coder = Coder(
        ledger=ledger,
        repo="MyThingsLab/sample-repo",
        github=GitHub(runner=FakeGh()),
        session_runner=NoopSessionRunner(),
    )

    issue = Issue(
        number=42,
        title="Fix bug in compute_total calculation",
        body="compute_total should handle negative numbers properly.",
        url="https://github.com/MyThingsLab/sample-repo/issues/42",
    )

    prompt = coder._prompt(issue, git_repo.path)
    assert "Agent Context Pack (ACP): compute_total" in prompt
    assert "Test Gap Alert" not in prompt


def test_agent_context_pack_omitted_when_no_symbols_match(tmp_path: Path):
    git_repo = make_git_repo(
        tmp_path,
        files={"src/sample/service.py": "def ping() -> str:\n    return 'pong'\n"},
    )

    ledger = Ledger(tmp_path / "ledger.jsonl")
    coder = Coder(
        ledger=ledger,
        repo="MyThingsLab/sample-repo",
        github=GitHub(runner=FakeGh()),
        session_runner=NoopSessionRunner(),
    )

    issue = Issue(
        number=99,
        title="Update README badges and CI workflow",
        body="We need to update our markdown documentation badges.",
        url="https://github.com/MyThingsLab/sample-repo/issues/99",
        labels=["documentation"],
    )

    prompt = coder._prompt(issue, git_repo.path)
    assert "## Deterministic Agent Context Pack" not in prompt


def test_agent_context_pack_reads_from_graph_path_env(tmp_path: Path, monkeypatch):
    calc_py = "def multiply_nums(a: int, b: int) -> int:\n    return a * b\n"
    git_repo = make_git_repo(
        tmp_path / "repo",
        files={"src/sample/math.py": calc_py, "README.md": "# Math\n"},
    )

    # Pre-index to an external sqlite file
    from mythings.graph import CodebaseGraph, PythonAstExtractor

    external_db = tmp_path / "external_graph.sqlite"
    graph = CodebaseGraph(external_db)
    PythonAstExtractor(repo_root=git_repo.path).index_repo(graph)
    graph.close()

    monkeypatch.setenv("MYTHINGS_GRAPH_PATH", str(external_db))

    ledger = Ledger(tmp_path / "ledger.jsonl")
    coder = Coder(
        ledger=ledger,
        repo="MyThingsLab/sample-repo",
        github=GitHub(runner=FakeGh()),
        session_runner=NoopSessionRunner(),
    )

    issue = Issue(
        number=10,
        title="Improve multiply_nums performance",
        body="multiply_nums should be optimized.",
        url="https://github.com/MyThingsLab/sample-repo/issues/10",
    )

    prompt = coder._prompt(issue, git_repo.path)
    assert "Agent Context Pack (ACP): multiply_nums" in prompt


def test_agent_context_pack_ignores_a_cache_from_another_extractor_version(
    tmp_path: Path, monkeypatch
):
    # A cache an older extractor wrote is worse than no cache: the current
    # traversal discards edges it cannot parse, and the pack then tells the
    # worker the symbol has no callers and no tests -- which it reads as a
    # finding, not as a stale index.
    import sqlite3

    calc_py = "def multiply_nums(a: int, b: int) -> int:\n    return a * b\n"
    git_repo = make_git_repo(
        tmp_path / "repo",
        files={"src/sample/math.py": calc_py, "README.md": "# Math\n"},
    )

    from mythings.graph import CodebaseGraph, PythonAstExtractor

    external_db = tmp_path / "external_graph.sqlite"
    graph = CodebaseGraph(external_db)
    PythonAstExtractor(repo_root=git_repo.path).index_repo(graph)
    graph.close()

    # Empty it and stamp a foreign version: if it were still consulted, the
    # pack would come out empty rather than falling back to a fresh index.
    stale = sqlite3.connect(str(external_db))
    stale.execute("DELETE FROM nodes")
    stale.execute("PRAGMA user_version = 0")
    stale.commit()
    stale.close()

    monkeypatch.setenv("MYTHINGS_GRAPH_PATH", str(external_db))

    coder = Coder(
        ledger=Ledger(tmp_path / "ledger.jsonl"),
        repo="MyThingsLab/sample-repo",
        github=GitHub(runner=FakeGh()),
        session_runner=NoopSessionRunner(),
    )
    issue = Issue(
        number=10,
        title="Improve multiply_nums performance",
        body="multiply_nums should be optimized.",
        url="https://github.com/MyThingsLab/sample-repo/issues/10",
    )

    prompt = coder._prompt(issue, git_repo.path)
    assert "Agent Context Pack (ACP): multiply_nums" in prompt
