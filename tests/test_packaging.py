"""The installed package must import the code in src/, not a stale copy.

Regression (2026-11-27): `config` was force-included as a single file, which hatch copies into
site-packages even for an editable install. pytest puts src/ first (`pythonpath`), so every test
saw the new code, while `python -m evals.harness`, the executor and Cloud Run imported the old
copy. A new setting crashed the live run with "Settings object has no attribute ...".

This test imports each top-level package in a FRESH interpreter, without pytest's path setup,
the way `python -m ...` does, and checks it resolves into src/.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = (Path(__file__).resolve().parents[1] / "src").resolve()
PACKAGES = sorted(p.name for p in SRC.iterdir() if (p / "__init__.py").is_file())
# Everything importable from src/, loose modules included, so the import check below also runs
# for a module that someone adds as a single file again.
IMPORTABLE = sorted({*PACKAGES, *(p.stem for p in SRC.glob("*.py"))})


def test_every_src_folder_is_a_package():
    # No loose modules: a top-level .py in src/ is what needed the force-include.
    assert [p.name for p in SRC.glob("*.py")] == []
    assert "config" in PACKAGES


@pytest.mark.parametrize("package", IMPORTABLE)
def test_installed_import_resolves_into_src(package, tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    code = f"import json, {package}; print(json.dumps({package}.__file__))"
    out = subprocess.run(  # noqa: S603 (fixed argv; package names come from our own src/)
        [sys.executable, "-c", code],
        cwd=tmp_path,  # not the repo root, so nothing is found by accident
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    path = Path(json.loads(out.stdout)).resolve()
    assert path.is_relative_to(SRC), f"{package} imported from {path}, not src/"
