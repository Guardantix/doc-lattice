"""Throwaway probe for GTX-159: does a red YAML compatibility leg block a merge?

This module exists only on the scratch branch that proves `main`'s required-status-check
rule bites. It is never merged. Delete the branch once the answer is recorded.
"""

import os
import sys


def test_probe_fails_on_every_yaml_compatibility_leg():
    # Identify the `yaml-compatibility` job by the one pair of facts unique to it in `ci.yml`.
    # It is the only job that runs pytest on 3.14 with the coverage gate off: `Tests (3.13)`
    # sets `--no-cov` but runs 3.13, `Tests (3.14)` runs 3.14 but leaves `PYTEST_ADDOPTS` empty,
    # and `code-quality` never runs pytest at all. The parser dimensions cannot do this on their
    # own, because the `(0.19.*, false)` cell installs exactly what the `tests` job installs.
    on_314 = sys.version_info[:2] == (3, 14)
    coverage_off = "--no-cov" in os.environ.get("PYTEST_ADDOPTS", "")
    assert not (on_314 and coverage_off), (
        "probe: yaml-compatibility leg reached, failing on purpose"
    )
