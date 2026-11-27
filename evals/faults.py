"""Fault catalog loading and the scripts that act on the target stack.

`FaultOps` is the only place the harness touches the target: reset, inject, oracle. Tests swap it
for a fake, so the episode loop can be tested without Docker.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from contracts import FaultSpec

REPO = Path(__file__).resolve().parents[1]
FAULTS_DIR = REPO / "evals" / "faults"


@dataclass(frozen=True)
class Fault:
    spec: FaultSpec
    dir: Path

    @property
    def id(self) -> str:
        return self.spec.id


def load_faults(selection: str = "all", faults_dir: Path = FAULTS_DIR) -> list[Fault]:
    """`selection` is "all", a comma-separated list of ids, or a category prefix like "cat:config"."""
    faults = [
        Fault(FaultSpec.model_validate(yaml.safe_load((d / "fault.yaml").read_text())), d)
        for d in sorted(p for p in faults_dir.iterdir() if (p / "fault.yaml").is_file())
    ]
    if selection == "all":
        return faults
    if selection.startswith("cat:"):
        chosen = [f for f in faults if f.spec.category == selection[4:]]
    else:
        wanted = [s.strip() for s in selection.split(",") if s.strip()]
        by_id = {f.id: f for f in faults}
        missing = [w for w in wanted if w not in by_id]
        if missing:
            raise ValueError(f"unknown fault ids: {missing}; known: {sorted(by_id)}")
        chosen = [by_id[w] for w in wanted]
    if not chosen:
        raise ValueError(f"no faults match {selection!r}")
    return chosen


class FaultOps(Protocol):
    async def reset(self, fault: Fault) -> bool: ...
    async def inject(self, fault: Fault) -> bool: ...
    async def healthy(self, fault: Fault) -> bool: ...


class ScriptFaultOps:
    """Runs the fault's own scripts. Each returns True on exit 0. Output goes to the harness log."""

    def __init__(self, script_timeout_s: int = 180) -> None:
        self.timeout = script_timeout_s

    async def _run(self, script: Path) -> bool:
        proc = await asyncio.create_subprocess_exec(
            str(script), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        try:
            return await asyncio.wait_for(proc.wait(), self.timeout) == 0
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return False

    async def reset(self, fault: Fault) -> bool:
        return await self._run(fault.dir / fault.spec.reset)

    async def inject(self, fault: Fault) -> bool:
        return await self._run(fault.dir / fault.spec.inject)

    async def healthy(self, fault: Fault) -> bool:
        return await self._run(fault.dir / fault.spec.oracle)
