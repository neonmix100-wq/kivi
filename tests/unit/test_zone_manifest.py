from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kivi.interfaces import cli as cli_module
from kivi.interfaces.cli import main
from kivi.paths import Paths
from kivi.safety.zone_manifest import (
    ZoneManifestError,
    generate_key,
    load_key,
    load_signed_rules,
    sign_manifest,
    status,
)
from kivi.safety.zones import ProtectedZones


@pytest.fixture()
def setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "secret.txt").write_text("secret", encoding="utf-8")
    manifest = tmp_path / "protected-zones.yaml"
    key = tmp_path / "private" / "zones.key"
    return workspace, manifest, key


def write_manifest(path: Path, zones: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"version": 1, "zones": zones}), encoding="utf-8")


def zone(path: Path, *, zone_id: str = "secret", access: list[str] | None = None) -> dict:
    return {
        "id": zone_id,
        "path": str(path),
        "access": access or ["read"],
        "reason": "test zone",
    }


def test_generate_key_is_stable(tmp_path: Path) -> None:
    key_path = tmp_path / "zones.key"
    first = generate_key(key_path)
    second = generate_key(key_path)
    assert first == second
    assert len(first) == 32
    assert len(load_key(key_path)) == 32


def test_missing_manifest_means_no_extra_rules(setup) -> None:
    _, manifest, key = setup
    generate_key(key)
    assert load_signed_rules(manifest, key) == ()


def test_sign_and_load_custom_zone(setup) -> None:
    workspace, manifest, key = setup
    generate_key(key)
    write_manifest(manifest, [zone(workspace / "secret.txt")])
    signature = sign_manifest(manifest, key)
    rules = load_signed_rules(manifest, key)
    zones = ProtectedZones(workspace, extra_rules=rules)
    decision = zones.check(workspace / "secret.txt", "read")
    assert signature.is_file()
    assert len(rules) == 1
    assert not decision.allowed
    assert decision.rule == "manifest-secret"


def test_tampering_is_rejected(setup) -> None:
    workspace, manifest, key = setup
    generate_key(key)
    write_manifest(manifest, [zone(workspace / "secret.txt")])
    sign_manifest(manifest, key)
    write_manifest(
        manifest,
        [
            zone(workspace / "secret.txt"),
            zone(workspace / "other.txt", zone_id="other"),
        ],
    )
    with pytest.raises(ZoneManifestError, match="signature"):
        load_signed_rules(manifest, key)


def test_missing_signature_is_rejected(setup) -> None:
    workspace, manifest, key = setup
    generate_key(key)
    write_manifest(manifest, [zone(workspace / "secret.txt")])
    with pytest.raises(ZoneManifestError, match="signature not found"):
        load_signed_rules(manifest, key)


def test_missing_key_is_rejected(setup) -> None:
    workspace, manifest, key = setup
    write_manifest(manifest, [zone(workspace / "secret.txt")])
    generate_key(key)
    sign_manifest(manifest, key)
    key.unlink()
    with pytest.raises(ZoneManifestError, match="key not found"):
        load_signed_rules(manifest, key)


def test_unknown_root_key_is_rejected(setup) -> None:
    workspace, manifest, key = setup
    generate_key(key)
    manifest.write_text(
        yaml.safe_dump({"version": 1, "zones": [zone(workspace / "secret.txt")], "extra": 1}),
        encoding="utf-8",
    )
    with pytest.raises(ZoneManifestError, match="unknown manifest keys"):
        sign_manifest(manifest, key)


def test_unknown_zone_key_is_rejected(setup) -> None:
    workspace, manifest, key = setup
    generate_key(key)
    entry = zone(workspace / "secret.txt")
    entry["mystery"] = True
    write_manifest(manifest, [entry])
    with pytest.raises(ZoneManifestError, match="unknown zone keys"):
        sign_manifest(manifest, key)


def test_invalid_access_is_rejected(setup) -> None:
    workspace, manifest, key = setup
    generate_key(key)
    write_manifest(manifest, [zone(workspace / "secret.txt", access=["delete"])])
    with pytest.raises(ZoneManifestError, match="access"):
        sign_manifest(manifest, key)


def test_relative_path_is_rejected(setup) -> None:
    _, manifest, key = setup
    generate_key(key)
    write_manifest(
        manifest,
        [{"id": "rel", "path": "docs/x", "access": ["read"], "reason": "x"}],
    )
    with pytest.raises(ZoneManifestError, match="absolute"):
        sign_manifest(manifest, key)


def test_duplicate_id_is_rejected(setup) -> None:
    workspace, manifest, key = setup
    generate_key(key)
    write_manifest(
        manifest,
        [zone(workspace / "a.txt"), zone(workspace / "b.txt")],
    )
    with pytest.raises(ZoneManifestError, match="duplicate"):
        sign_manifest(manifest, key)


def test_status_reports_valid_and_invalid(setup) -> None:
    workspace, manifest, key = setup
    generate_key(key)
    assert status(manifest, key).exists is False
    write_manifest(manifest, [zone(workspace / "secret.txt")])
    sign_manifest(manifest, key)
    valid = status(manifest, key)
    assert valid.signed is True
    assert valid.zones == 1
    write_manifest(manifest, [zone(workspace / "other.txt")])
    invalid = status(manifest, key)
    assert invalid.signed is False
    assert invalid.reason


def test_signature_is_stable(setup) -> None:
    workspace, manifest, key = setup
    generate_key(key)
    write_manifest(manifest, [zone(workspace / "secret.txt")])
    first = sign_manifest(manifest, key).read_text(encoding="ascii")
    second = sign_manifest(manifest, key).read_text(encoding="ascii")
    assert first == second


def make_cli_profile(tmp_path: Path) -> Paths:
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "config" / "kivi.yaml").write_text("version: 1\n", encoding="utf-8")
    (root / "note.txt").write_text("hello", encoding="utf-8")
    return Paths(root=root, home=tmp_path / "profile")


def test_cli_zones_init_and_check(tmp_path: Path, monkeypatch) -> None:
    paths = make_cli_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)

    assert main(["zones", "init"]) == 0
    manifest = paths.root / "config" / "protected-zones.yaml"
    key = paths.root / "private" / "safety" / "zones.key"
    assert manifest.is_file()
    assert key.is_file()
    assert main(["zones", "status"]) == 0
    assert main(["zones", "check"]) == 0


def test_cli_tools_refuse_an_invalid_manifest(tmp_path: Path, monkeypatch) -> None:
    paths = make_cli_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)
    assert main(["zones", "init"]) == 0

    manifest = paths.root / "config" / "protected-zones.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "zones": [
                    {
                        "id": "tampered",
                        "path": str(paths.root / "note.txt"),
                        "access": ["read"],
                        "reason": "edited after signing",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    assert main(["zones", "check"]) == 1
    assert main(["tools", "run", "fs.read", "--args", '{"path": "note.txt"}']) == 1

    assert main(["zones", "sign"]) == 0
    assert main(["zones", "check"]) == 0
    assert main(["tools", "run", "fs.read", "--args", '{"path": "note.txt"}']) == 1
