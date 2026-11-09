from __future__ import annotations

from pathlib import Path

from kivi.paths import get_paths, project_root


def test_project_root_contains_pyproject() -> None:
    assert (project_root() / "pyproject.toml").is_file()


def test_default_home_is_under_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KIVI_HOME", raising=False)
    paths = get_paths(root=tmp_path)
    assert paths.home == tmp_path.resolve() / "data"
    assert paths.config_file == tmp_path.resolve() / "config" / "kivi.yaml"


def test_home_can_be_redirected(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIVI_HOME", str(tmp_path / "profile"))
    paths = get_paths(root=tmp_path)
    assert paths.home == (tmp_path / "profile").resolve()


def test_ensure_creates_runtime_directories(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIVI_HOME", str(tmp_path / "profile"))
    paths = get_paths(root=tmp_path).ensure()
    for directory in (paths.home, paths.logs, paths.artifacts, paths.cache, paths.backups):
        assert directory.is_dir()
