from pathlib import Path

from mythings._harness import harness_text


def test_vendored_harness_matches_canonical() -> None:
    vendored = Path(__file__).resolve().parents[1] / "HARNESS.md"
    assert vendored.read_text(encoding="utf-8") == harness_text(), (
        "HARNESS.md is stale — re-vendor from mythings-core (its harness.md is canonical)"
    )
