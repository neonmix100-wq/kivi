from __future__ import annotations

from pathlib import Path

from kivi.interfaces import cli as cli_module
from kivi.interfaces.cli import main
from kivi.paths import Paths


def make_profile(tmp_path: Path) -> Paths:
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "config" / "kivi.yaml").write_text("version: 1\n", encoding="utf-8")
    return Paths(root=root, home=tmp_path / "profile")


def test_elevated_list(tmp_path: Path, monkeypatch) -> None:
    paths = make_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)
    assert main(["elevated", "list"]) == 0


def test_elevated_requires_approval(tmp_path: Path, monkeypatch) -> None:
    paths = make_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)
    assert main(["elevated", "run", "dns.flush"]) == 1


def test_elevated_rejects_unknown_operation(tmp_path: Path, monkeypatch) -> None:
    paths = make_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)
    assert main(["elevated", "run", "shell.admin", "--approve"]) == 1
