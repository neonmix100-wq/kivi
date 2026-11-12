"""Tool registry: one place that knows which tools exist.

Registration is explicit and fail-fast: a duplicate name, an unknown risk level
or an empty capability set is a programming error, not a runtime surprise.
"""

from __future__ import annotations

import re

from kivi.safety.permissions import RISKS
from kivi.tools.base import Tool, ToolSpec, describe

_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class RegistryError(ValueError):
    """Raised for an invalid or duplicate tool registration."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not _NAME.match(tool.name):
            raise RegistryError(f"invalid tool name: {tool.name!r}")
        if tool.risk not in RISKS:
            raise RegistryError(f"{tool.name}: unknown risk level {tool.risk!r}")
        if not tool.capabilities:
            raise RegistryError(f"{tool.name}: at least one capability is required")
        if tool.name in self._tools:
            raise RegistryError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def require(self, name: str) -> Tool:
        tool = self.get(name)
        if tool is None:
            raise RegistryError(f"unknown tool: {name}")
        return tool

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def all(self) -> tuple[Tool, ...]:
        return tuple(self._tools[name] for name in self.names())

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(describe(tool) for tool in self.all())


def build_registry(
    *,
    blocked_hosts: tuple[str, ...] | list[str] = (),
) -> ToolRegistry:
    """Create the default registry with the stage-4 tools."""
    from kivi.modules.work_crm.tools import build_crm_tools
    from kivi.tools.browser import (
        BrowserClickTool,
        BrowserCloseTool,
        BrowserDownloadTool,
        BrowserFillTool,
        BrowserOpenTool,
        BrowserReadTool,
        BrowserSession,
    )
    from kivi.tools.clipboard import ClipboardReadTool, ClipboardWriteTool
    from kivi.tools.code import CodeRunTool
    from kivi.tools.fs import FsListTool, FsReadTool, FsWriteTool
    from kivi.tools.git import (
        GitBranchTool,
        GitCommitTool,
        GitDiffTool,
        GitStatusTool,
    )
    from kivi.tools.mail import build_mail_tools
    from kivi.tools.media import MediaImageTool
    from kivi.tools.net import HttpGetTool
    from kivi.tools.patch import CodePatchTool
    from kivi.tools.proc import ProcKillTool, ProcListTool, ProcStartTool
    from kivi.tools.repo import RepoIndexTool
    from kivi.tools.screen import ScreenCaptureTool
    from kivi.tools.shell import ShellExecTool
    from kivi.tools.system import (
        SystemLockTool,
        SystemNotifyTool,
        SystemRestartTool,
        SystemShutdownTool,
        SystemSleepTool,
        SystemVolumeGetTool,
        SystemVolumeSetTool,
    )
    from kivi.tools.telegram import build_telegram_tools
    from kivi.tools.ui import UiClickTool, UiFindTool, UiHotkeyTool, UiTypeTool
    from kivi.tools.windows import (
        AppLaunchTool,
        AppTerminateTool,
        WindowCloseTool,
        WindowFocusTool,
        WindowListTool,
        WindowMaximizeTool,
        WindowMinimizeTool,
    )

    registry = ToolRegistry()
    browser_session = BrowserSession()
    for tool in (
        FsReadTool(),
        FsListTool(),
        FsWriteTool(),
        ShellExecTool(),
        HttpGetTool(blocked_hosts=blocked_hosts),
        ProcListTool(),
        ProcKillTool(),
        ProcStartTool(),
        ClipboardReadTool(),
        ClipboardWriteTool(),
        ScreenCaptureTool(),
        CodeRunTool(),
        RepoIndexTool(),
        CodePatchTool(),
        GitStatusTool(),
        GitDiffTool(),
        GitBranchTool(),
        GitCommitTool(),
        WindowListTool(),
        WindowFocusTool(),
        WindowMinimizeTool(),
        WindowMaximizeTool(),
        WindowCloseTool(),
        AppLaunchTool(),
        AppTerminateTool(),
        UiClickTool(),
        UiTypeTool(),
        UiHotkeyTool(),
        UiFindTool(),
        SystemNotifyTool(),
        SystemVolumeGetTool(),
        SystemVolumeSetTool(),
        SystemLockTool(),
        SystemSleepTool(),
        SystemShutdownTool(),
        SystemRestartTool(),
        BrowserOpenTool(browser_session),
        BrowserReadTool(browser_session),
        BrowserClickTool(browser_session),
        BrowserFillTool(browser_session),
        BrowserDownloadTool(browser_session),
        BrowserCloseTool(browser_session),
        *build_mail_tools(),
        *build_telegram_tools(),
        *build_crm_tools(),
        MediaImageTool(),
    ):
        registry.register(tool)
    return registry


__all__ = ["RegistryError", "ToolRegistry", "build_registry"]
