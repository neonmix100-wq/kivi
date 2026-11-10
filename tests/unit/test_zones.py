from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from kivi.safety.zones import ProtectedZones, ZoneError


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    for relative in (
        "docs",
        "data/artifacts",
        "data/logs",
        "config",
        "private",
        ".git",
        "src/kivi/safety",
    ):
        (root / relative).mkdir(parents=True)
    return root


@pytest.fixture()
def zones(workspace: Path) -> ProtectedZones:
    return ProtectedZones(workspace)


def test_read_inside_workspace_is_allowed(zones: ProtectedZones, workspace: Path) -> None:
    (workspace / "docs" / "note.txt").write_text("hello", encoding="utf-8")
    decision = zones.check(workspace / "docs" / "note.txt", "read")
    assert decision.allowed
    assert decision.resolved == (workspace / "docs" / "note.txt").resolve()


def test_write_into_safety_core_is_denied(zones: ProtectedZones, workspace: Path) -> None:
    decision = zones.check(workspace / "src/kivi/safety/policy.py", "write")
    assert not decision.allowed
    assert decision.rule == "core-src/kivi/safety"


def test_write_into_config_is_denied(zones: ProtectedZones, workspace: Path) -> None:
    decision = zones.check(workspace / "config" / "kivi.yaml", "write")
    assert not decision.allowed
    assert decision.rule == "core-config"


def test_write_into_audit_database_is_denied(zones: ProtectedZones, workspace: Path) -> None:
    assert zones.check(workspace / "data" / "kivi.db", "write").rule == "core-data/kivi.db"


def test_write_into_logs_is_denied(zones: ProtectedZones, workspace: Path) -> None:
    assert zones.check(workspace / "data" / "logs" / "events.jsonl", "write").rule == (
        "core-data/logs"
    )


def test_artifacts_are_writable(zones: ProtectedZones, workspace: Path) -> None:
    assert zones.check(workspace / "data" / "artifacts" / "out.txt", "write").allowed


def test_env_file_is_never_readable(zones: ProtectedZones, workspace: Path) -> None:
    decision = zones.check(workspace / ".env", "read")
    assert not decision.allowed
    assert decision.rule == "secret-.env"


def test_key_material_is_never_readable(zones: ProtectedZones, workspace: Path) -> None:
    assert zones.check(workspace / "docs" / "server.pem", "read").rule == "secret-*.pem"
    assert zones.check(workspace / "docs" / "id_rsa", "read").rule == "secret-id_rsa*"


def test_private_directory_is_denied(zones: ProtectedZones, workspace: Path) -> None:
    decision = zones.check(workspace / "private" / "notes.txt", "read")
    assert not decision.allowed
    assert decision.rule == "dir-private"


def test_git_directory_is_denied(zones: ProtectedZones, workspace: Path) -> None:
    assert zones.check(workspace / ".git" / "config", "read").rule == "dir-.git"


def test_outside_workspace_is_denied(zones: ProtectedZones, tmp_path: Path) -> None:
    decision = zones.check(tmp_path / "outside.txt", "read")
    assert not decision.allowed
    assert decision.rule == "zone-scope"


def test_parent_traversal_is_denied(zones: ProtectedZones) -> None:
    decision = zones.check("../outside.txt", "read")
    assert not decision.allowed
    assert decision.rule == "zone-invalid"


def test_alternate_data_stream_is_denied(zones: ProtectedZones) -> None:
    decision = zones.check("docs/note.txt:hidden", "write")
    assert not decision.allowed
    assert decision.rule == "zone-invalid"


def test_device_path_is_denied(zones: ProtectedZones) -> None:
    decision = zones.check(r"\\?\C:\Windows\System32", "read")
    assert not decision.allowed
    assert decision.rule == "zone-invalid"


def test_unc_path_is_denied(zones: ProtectedZones) -> None:
    decision = zones.check(r"\\server\share\file.txt", "read")
    assert not decision.allowed
    assert decision.rule == "zone-unc"


def test_zone_decision_require_raises(zones: ProtectedZones) -> None:
    decision = zones.check("../outside.txt", "read")
    with pytest.raises(ZoneError):
        decision.require()


def test_symlink_escape_is_denied(zones: ProtectedZones, workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = workspace / "docs" / "link"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Symlinks need Developer Mode; junctions do not.
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip("neither symlinks nor junctions are available")
    decision = zones.check(link / "secret.txt", "read")
    assert not decision.allowed
    assert decision.rule in {"zone-scope", "zone-reparse"}


def test_unknown_access_level_is_rejected(zones: ProtectedZones, workspace: Path) -> None:
    with pytest.raises(ValueError):
        zones.check(workspace / "docs", "delete")  # type: ignore[arg-type]
