from __future__ import annotations

import struct
from pathlib import Path

import pytest

from kivi.kernel.db import Database
from kivi.safety.permissions import PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, ToolRegistry, build_registry
from kivi.tools.screen import (
    RawImage,
    ScreenCaptureTool,
    WindowsScreen,
    image_to_bmp,
)


class FakeScreen:
    def __init__(self, image: RawImage | None = None) -> None:
        self.captures = 0
        self._image = image or RawImage(2, 2, bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 0]))

    def capture(self) -> RawImage:
        self.captures += 1
        return self._image


def make_executor(tmp_path: Path, backend: FakeScreen) -> tuple[ToolExecutor, Database]:
    workspace = tmp_path / "workspace"
    (workspace / "data" / "artifacts").mkdir(parents=True)
    db = Database(tmp_path / "kivi.db")
    engine = PermissionEngine(db, workspace=str(workspace))
    grant = engine.issue("t1", ["screen.capture"])
    registry = ToolRegistry()
    registry.register(ScreenCaptureTool(backend=backend))
    executor = ToolExecutor(
        registry,
        db,
        engine,
        ProtectedZones(workspace),
        workspace=workspace,
    )
    executor.grant = grant  # type: ignore[attr-defined]
    return executor, db


def test_bmp_header_and_pixel_order() -> None:
    image = RawImage(2, 1, bytes([255, 0, 0, 0, 0, 255]))
    data = image_to_bmp(image)
    magic, size, _reserved1, _reserved2, offset = struct.unpack_from("<2sIHHI", data)
    width, height = struct.unpack_from("<ii", data, 18)
    assert magic == b"BM"
    assert size == len(data)
    assert offset == 54
    assert (width, height) == (2, 1)
    # BMP stores BGR, bottom-up: red -> 00 00 FF, blue -> FF 00 00.
    assert data[54:60] == bytes([0, 0, 255, 255, 0, 0])
    assert len(data[54:]) % 4 == 0


def test_raw_image_rejects_bad_size() -> None:
    with pytest.raises(ValueError, match="RGB buffer"):
        RawImage(2, 2, b"\x00" * 3)


def test_capture_writes_default_artifact(tmp_path: Path) -> None:
    backend = FakeScreen()
    executor, db = make_executor(tmp_path, backend)
    try:
        result = executor.run("screen.capture", {}, grant=executor.grant)  # type: ignore[attr-defined]
    finally:
        db.close()
    assert result.ok
    assert backend.captures == 1
    assert result.trust == "untrusted:vision"
    assert len(result.artifacts) == 1
    path = Path(result.artifacts[0])
    assert path.is_file()
    assert path.suffix == ".bmp"
    assert path.parent.name == "artifacts"


def test_capture_honours_a_custom_path(tmp_path: Path) -> None:
    backend = FakeScreen()
    executor, db = make_executor(tmp_path, backend)
    try:
        result = executor.run(
            "screen.capture",
            {"path": "shot.bmp"},
            grant=executor.grant,  # type: ignore[attr-defined]
        )
    finally:
        db.close()
    assert result.ok
    assert (tmp_path / "workspace" / "shot.bmp").is_file()


def test_capture_refuses_to_overwrite(tmp_path: Path) -> None:
    backend = FakeScreen()
    executor, db = make_executor(tmp_path, backend)
    try:
        first = executor.run(
            "screen.capture",
            {"path": "shot.bmp"},
            grant=executor.grant,  # type: ignore[attr-defined]
        )
        second = executor.run(
            "screen.capture",
            {"path": "shot.bmp"},
            grant=executor.grant,  # type: ignore[attr-defined]
        )
        third = executor.run(
            "screen.capture",
            {"path": "shot.bmp", "overwrite": True},
            grant=executor.grant,  # type: ignore[attr-defined]
        )
    finally:
        db.close()
    assert first.ok
    assert not second.ok
    assert "file exists" in second.error
    assert third.ok


def test_capture_default_path_is_not_reachable_outside_workspace(tmp_path: Path) -> None:
    backend = FakeScreen()
    executor, db = make_executor(tmp_path, backend)
    try:
        result = executor.run(
            "screen.capture",
            {"path": str(tmp_path / "outside.bmp")},
            grant=executor.grant,  # type: ignore[attr-defined]
        )
    finally:
        db.close()
    assert not result.ok
    assert "zone-scope" in result.error


def test_capture_rejects_non_bmp_paths(tmp_path: Path) -> None:
    backend = FakeScreen()
    executor, db = make_executor(tmp_path, backend)
    try:
        result = executor.run(
            "screen.capture",
            {"path": "shot.png"},
            grant=executor.grant,  # type: ignore[attr-defined]
        )
    finally:
        db.close()
    assert not result.ok
    assert ".bmp" in result.error


def test_capture_requires_an_existing_parent(tmp_path: Path) -> None:
    backend = FakeScreen()
    executor, db = make_executor(tmp_path, backend)
    try:
        result = executor.run(
            "screen.capture",
            {"path": "missing/shot.bmp"},
            grant=executor.grant,  # type: ignore[attr-defined]
        )
    finally:
        db.close()
    assert not result.ok
    assert "parent" in result.error


def test_dry_run_does_not_capture(tmp_path: Path) -> None:
    backend = FakeScreen()
    executor, db = make_executor(tmp_path, backend)
    try:
        result = executor.run(
            "screen.capture",
            {"path": "shot.bmp"},
            grant=executor.grant,  # type: ignore[attr-defined]
            dry_run=True,
        )
    finally:
        db.close()
    assert result.ok
    assert backend.captures == 0
    assert not (tmp_path / "workspace" / "shot.bmp").exists()


def test_registry_contains_screen_capture() -> None:
    registry = build_registry()
    assert "screen.capture" in registry.names()
    spec = next(spec for spec in registry.specs() if spec.name == "screen.capture")
    assert spec.risk == "R1"
    assert spec.capabilities == ("screen.capture",)


def test_windows_backend_refuses_other_platforms(monkeypatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    try:
        WindowsScreen().capture()
    except Exception as exc:
        assert "Windows-only" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("WindowsScreen must refuse non-Windows platforms")
