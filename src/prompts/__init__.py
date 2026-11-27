"""Versioned prompt files (see README.md in this folder). Owner: Sai.

WHAT: `load(name, version)` reads `<name>.<version>.md` from this package and returns it with a
      short content hash.
WHY:  prompts are reviewable files (AGENTS.md rule 6), and the hash lets a trace or an eval result
      say exactly which text produced it, even if someone edited a file without bumping the version.
HOW:  importlib.resources, so it works from the source tree and from an installed wheel.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION = re.compile(r"^v[0-9]+$")


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str
    sha: str  # first 12 hex chars of sha256(text)

    @property
    def ref(self) -> str:
        """`name.version@sha`, the string to put in logs and eval results."""
        return f"{self.name}.{self.version}@{self.sha}"


def load(name: str, version: str = "v1") -> Prompt:
    # Validated so a caller can never turn a name into a path outside this package.
    if not _NAME.match(name) or not _VERSION.match(version):
        raise ValueError(f"bad prompt name or version: {name!r} {version!r}")
    text = resources.files(__package__).joinpath(f"{name}.{version}.md").read_text("utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return Prompt(name=name, version=version, text=text, sha=sha)
