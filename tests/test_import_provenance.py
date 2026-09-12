from pathlib import Path

import mycoder


def test_the_suite_imports_this_checkouts_source_not_the_editable_install() -> None:
    # Every repo is installed editable into the shared root .venv, and that
    # install points at its MAIN checkout. A worker session runs inside a git
    # WORKTREE, so without `pythonpath = ["src"]` in the pytest config the
    # editable install wins and the suite exercises main's code instead of the
    # change under review. (my-template#18)
    #
    # The failure is asymmetric: a change that ADDS a symbol fails loudly on
    # ImportError, but a change that MODIFIES existing behaviour passes green
    # against source it never ran. The quiet case is the dangerous one, and it
    # is inherited by every automated worker that trusts a green suite as
    # evidence its diff is sound.
    repo_root = Path(__file__).resolve().parent.parent
    imported = Path(mycoder.__file__).resolve()
    assert imported.is_relative_to(repo_root), (
        f"tests imported {imported}, which is outside this checkout ({repo_root}). "
        "The editable install shadowed the worktree -- check `pythonpath` in "
        "[tool.pytest.ini_options]."
    )
