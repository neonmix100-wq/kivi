"""Filesystem layout for Kivi runtime data.

Everything mutable lives under the *home* directory (``data/`` by default).
The home can be redirected with ``KIVI_HOME`` for tests and for moving a
profile to another disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def project_root() -> Path:
    """Return the repository root (the folder containing ``pyproject.toml``)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    # Editable install outside the repo: fall back to the current directory.
    return Path.cwd()


@dataclass(frozen=True)
class Paths:
    root: Path
    home: Path

    @property
    def config_file(self) -> Path:
        return self.root / "config" / "kivi.yaml"

    @property
    def data(self) -> Path:
        return self.home

    @property
    def db_file(self) -> Path:
        return self.home / "kivi.db"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def artifacts(self) -> Path:
        return self.home / "artifacts"

    @property
    def cache(self) -> Path:
        return self.home / "cache"

    @property
    def backups(self) -> Path:
        return self.home / "backups"

    def ensure(self) -> Paths:
        for path in (self.home, self.logs, self.artifacts, self.cache, self.backups):
            path.mkdir(parents=True, exist_ok=True)
        return self


def get_paths(root: Path | None = None) -> Paths:
    root = (root or project_root()).resolve()
    home_env = os.environ.get("KIVI_HOME")
    home = Path(home_env).expanduser().resolve() if home_env else root / "data"
    return Paths(root=root, home=home)
