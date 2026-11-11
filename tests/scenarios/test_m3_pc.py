from __future__ import annotations

from pathlib import Path
from typing import Any

from kivi.kernel.db import Database
from kivi.safety.permissions import PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, ToolRegistry
from kivi.tools.system import (
    SystemNotifyTool,
    SystemVolumeGetTool,
    SystemVolumeSetTool,
)
from kivi.tools.windows import (
    AppLaunchTool,
    WindowCloseTool,
    WindowFocusTool,
    WindowInfo,
    WindowListTool,
    WindowMaximizeTool,
    WindowMinimizeTool,
)


class FakeWindows:
    def __init__(self) -> None:
        self.windows = [
            WindowInfo(101, "Notes - Notepad", "notepad.exe", 500, bounds=(0, 0, 800, 600)),
            WindowInfo(202, "Kivi", "kivi.exe", 501, bounds=(10, 10, 900, 700)),
        ]
        self.calls: list[tuple[str, Any]] = []

    def list(self, *, visible_only: bool = False):
        return list(self.windows)

    def focus(self, handle: int) -> None:
        self.calls.append(("focus", handle))

    def minimize(self, handle: int) -> None:
        self.calls.append(("minimize", handle))

    def maximize(self, handle: int) -> None:
        self.calls.append(("maximize", handle))

    def close(self, handle: int) -> None:
        self.calls.append(("close", handle))

    def launch(self, executable: str, args: list[str], cwd: str | None = None) -> int:
        self.calls.append(("launch", executable, tuple(args), cwd))
        return 4242

    def terminate(self, pid: int) -> None:
        self.calls.append(("terminate", pid))


class FakeSystem:
    def __init__(self) -> None:
        self.volume = 35
        self.notifications: list[tuple[str, str, int]] = []

    def notify(self, title: str, message: str, timeout_s: int = 5) -> None:
        self.notifications.append((title, message, timeout_s))

    def get_volume(self) -> int:
        return self.volume

    def set_volume(self, level: int) -> None:
        self.volume = level

    def lock(self) -> None:
        pass

    def sleep(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def restart(self) -> None:
        pass


def run_tool(
    tmp_path: Path,
    tool: Any,
    capability: str,
    name: str,
    args: dict[str, Any],
    *,
    approved: bool = False,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    db = Database(tmp_path / "kivi.db")
    engine = PermissionEngine(db, workspace=str(workspace))
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(
        registry,
        db,
        engine,
        ProtectedZones(workspace),
        workspace=workspace,
    )
    grant = engine.issue("scenario", [capability])
    try:
        return executor.run(name, args, grant=grant, approved=approved)
    finally:
        db.close()


def test_pc_01_list_windows(tmp_path: Path) -> None:
    result = run_tool(
        tmp_path, WindowListTool(FakeWindows()), "window.read", "window.list", {"limit": 10}
    )
    assert result.ok
    assert "Notes" in result.output


def test_pc_02_focus_window(tmp_path: Path) -> None:
    fake = FakeWindows()
    result = run_tool(
        tmp_path, WindowFocusTool(fake), "window.control", "window.focus", {"title": "Notes"}
    )
    assert result.ok
    assert ("focus", 101) in fake.calls


def test_pc_03_minimize_window(tmp_path: Path) -> None:
    fake = FakeWindows()
    result = run_tool(
        tmp_path,
        WindowMinimizeTool(fake),
        "window.control",
        "window.minimize",
        {"handle": 101},
    )
    assert result.ok
    assert ("minimize", 101) in fake.calls


def test_pc_04_maximize_window(tmp_path: Path) -> None:
    fake = FakeWindows()
    result = run_tool(
        tmp_path,
        WindowMaximizeTool(fake),
        "window.control",
        "window.maximize",
        {"handle": 101},
    )
    assert result.ok
    assert ("maximize", 101) in fake.calls


def test_pc_05_close_requires_approval(tmp_path: Path) -> None:
    fake = FakeWindows()
    denied = run_tool(
        tmp_path, WindowCloseTool(fake), "window.control", "window.close", {"handle": 101}
    )
    approved = run_tool(
        tmp_path,
        WindowCloseTool(fake),
        "window.control",
        "window.close",
        {"handle": 101},
        approved=True,
    )
    assert not denied.ok
    assert approved.ok
    assert ("close", 101) in fake.calls


def test_pc_06_launch_application(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exe = workspace / "demo.exe"
    exe.write_bytes(b"MZ")
    result = run_tool(
        tmp_path,
        AppLaunchTool(FakeWindows()),
        "app.launch",
        "app.launch",
        {"path": str(exe)},
        approved=True,
    )
    assert result.ok
    assert "4242" in result.output


def test_pc_07_volume_get(tmp_path: Path) -> None:
    result = run_tool(
        tmp_path, SystemVolumeGetTool(FakeSystem()), "system.volume", "system.volume.get", {}
    )
    assert result.ok
    assert "35" in result.output


def test_pc_08_volume_set(tmp_path: Path) -> None:
    backend = FakeSystem()
    result = run_tool(
        tmp_path,
        SystemVolumeSetTool(backend),
        "system.volume",
        "system.volume.set",
        {"level": 55},
    )
    assert result.ok
    assert backend.volume == 55


def test_pc_09_notify(tmp_path: Path) -> None:
    backend = FakeSystem()
    result = run_tool(
        tmp_path,
        SystemNotifyTool(backend),
        "system.notify",
        "system.notify",
        {"title": "Kivi", "message": "готово"},
    )
    assert result.ok
    assert backend.notifications == [("Kivi", "готово", 5)]


def test_pc_10_close_refuses_protected_process(tmp_path: Path) -> None:
    fake = FakeWindows()
    fake.windows = [WindowInfo(303, "Kivi safety", "csrss.exe", 1)]
    result = run_tool(
        tmp_path,
        WindowCloseTool(fake),
        "window.control",
        "window.close",
        {"handle": 303},
        approved=True,
    )
    assert not result.ok
    assert ("close", 303) not in fake.calls
