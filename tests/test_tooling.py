"""The repo's own gates, pinned against what they say about themselves.

`strict_markers` makes the marker list authoritative, and `pytest --markers`
prints each description to a contributor as fact. A description that names a
gate — "excluded from make test-fast", "continue-on-error in CI" — is a claim
about a file in this repo, and nothing checked that the file agreed. Same shape
as try/SECURITY.md §9: prose that states an invariant is part of the invariant.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _markers() -> dict[str, str]:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    out = {}
    for entry in data["tool"]["pytest"]["ini_options"]["markers"]:
        name, _, description = entry.partition(":")
        out[name.strip()] = description.strip()
    return out


def _test_fast_expression() -> str:
    """The `-m` expression `make test-fast` actually runs."""
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    body = makefile.split("test-fast:", 1)[1]
    line = next(ln for ln in body.splitlines() if "pytest" in ln)
    return line.split("-m", 1)[1].strip().strip('"')


def test_a_marker_that_claims_test_fast_skips_it_is_actually_skipped():
    """The claim is load-bearing in one direction only: a contributor who reads
    "excluded from make test-fast" marks a 40-second test and keeps running the
    fast loop, which is now 40 seconds slower without saying so."""
    expression = _test_fast_expression()
    for name, description in _markers().items():
        if "test-fast" not in description.lower():
            continue
        assert f"not {name}" in expression, (
            f"marker {name!r} says it is excluded from make test-fast, but the "
            f"Makefile runs: -m {expression!r}"
        )


def test_a_marker_that_claims_a_soft_ci_lane_has_one():
    """ "CI-gated as continue-on-error rather than merge-blocking" tells an author
    their noisy timing assertion cannot block a merge. If CI is a bare `pytest`,
    the opposite is true, and they find out by blocking someone else's PR."""
    workflows = (REPO_ROOT / ".github" / "workflows").glob("*.yml")
    ci_text = "\n".join(p.read_text(encoding="utf-8") for p in workflows)
    for name, description in _markers().items():
        if "continue-on-error" not in description.lower():
            continue
        assert "continue-on-error" in ci_text, (
            f"marker {name!r} promises a soft CI lane; no workflow declares one"
        )
