"""Static checks on the fault catalog (evals/faults/). Runs without Docker, so CI catches a broken
fault definition before anyone spends an eval run on it. Running the faults themselves needs Docker:
see evals/faults/README.md."""

from pathlib import Path

import pytest
import yaml

from contracts import FaultSpec
from contracts.catalog import REMEDIATION_ACTIONS

FAULTS_DIR = Path(__file__).resolve().parents[1] / "evals" / "faults"
FAULT_DIRS = sorted(p for p in FAULTS_DIR.iterdir() if p.is_dir())


def test_there_are_faults():
    assert len(FAULT_DIRS) >= 5


@pytest.mark.parametrize("fault_dir", FAULT_DIRS, ids=lambda p: p.name)
def test_fault_definition_is_valid(fault_dir: Path):
    spec = FaultSpec.model_validate(yaml.safe_load((fault_dir / "fault.yaml").read_text()))
    assert spec.id == fault_dir.name, "folder name must equal the fault id"
    assert spec.expected_fix in REMEDIATION_ACTIONS
    for script in (spec.inject, spec.oracle, spec.reset):
        path = fault_dir / script
        assert path.is_file(), f"missing {path}"
        assert path.stat().st_mode & 0o111, (
            f"{path} is not executable (git update-index --chmod=+x)"
        )
        assert path.read_text().startswith("#!/usr/bin/env bash")


def test_fault_ids_are_unique():
    ids = [yaml.safe_load((d / "fault.yaml").read_text())["id"] for d in FAULT_DIRS]
    assert len(ids) == len(set(ids))


def test_categories_are_spread():
    # The eval should cover several kinds of failure, not 20 variants of one.
    cats = {yaml.safe_load((d / "fault.yaml").read_text())["category"] for d in FAULT_DIRS}
    assert len(cats) >= 3
