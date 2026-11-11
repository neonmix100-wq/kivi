"""System tools: notifications, volume and power control for the Operator stage.

The Windows backend is deliberately small and boring: a toast is one fixed
PowerShell script, volume is pycaw's endpoint interface, and power actions are
direct Win32 calls or fixed ``shutdown.exe`` argv. There is no shell, no
``eval``, no arbitrary command and no hidden fallback to admin rights.

Every tool goes through :class:`~kivi.tools.base.ToolContext` and the
Capability Broker. The backend is injectable so tests never touch audio, lock
the session or shut the machine down.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
import sys
from collections.abc import Callable
from typing import Any, Protocol
from xml.sax.saxutils import escape

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kivi.safety.sanitize import redact_secrets
from kivi.tools.base import Preview, Tool, ToolContext, ToolError, ToolResult
from kivi.tools.shell import _minimal_env

log = logging.getLogger(__name__)

_NOTIFY_TIMEOUT_S = 5.0
_POWER_TIMEOUT_S = 10.0
_TOAST_APP_ID = (
    "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
    "\\WindowsPowerShell\\v1.0\\powershell.exe"
)
_SHUTDOWN_ARGV = ("shutdown.exe", "/s", "/t", "0")
_RESTART_ARGV = ("shutdown.exe", "/r", "/t", "0")
_XML_ENTITIES = {'"': "&quot;", "'": "&apos;"}

# One fixed script; the only variable part is the already XML-escaped document.
_TOAST_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
    "ContentType = WindowsRuntime] > $null; "
    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
    "ContentType = WindowsRuntime] > $null; "
    "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
    "$xml.LoadXml('__XML__'); "
    "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml; "
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
    f"'{_TOAST_APP_ID}').Show($toast)"
)


class SystemUnavailable(RuntimeError):
    """Raised when the operating system cannot perform a system action."""


class SystemBackend(Protocol):
    """The OS surface used by the system tools.

    ``is_muted`` and ``set_muted`` are optional extensions (see
    :class:`MuteBackend`): tools discover them with ``getattr`` so a minimal
    backend still works for notifications and power. Implementations should
    raise :class:`SystemUnavailable` for expected OS failures.
    """

    def notify(self, title: str, message: str, timeout_s: int = 5) -> None: ...

    def get_volume(self) -> int: ...

    def set_volume(self, level: int) -> None: ...

    def lock(self) -> None: ...

    def sleep(self) -> None: ...

    def shutdown(self) -> None: ...

    def restart(self) -> None: ...


class MuteBackend(Protocol):
    """Optional mute capability; not every :class:`SystemBackend` has it."""

    def is_muted(self) -> bool: ...

    def set_muted(self, muted: bool) -> None: ...


def _clamp_percent(value: int) -> int:
    return max(0, min(100, int(value)))


def _error_detail(stderr: str | None) -> str:
    lines = (stderr or "").strip().splitlines()
    return lines[-1][:200] if lines else "no error output"


def _toast_xml(title: str, message: str) -> str:
    """Build a ToastGeneric document with both text fields XML-escaped."""
    safe_title = escape(title, _XML_ENTITIES)
    safe_message = escape(message, _XML_ENTITIES)
    return (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{safe_title}</text><text>{safe_message}</text>"
        "</binding></visual></toast>"
    )


def _default_beep() -> None:
    import winsound

    winsound.MessageBeep(winsound.MB_ICONASTERISK)


def _muted_or_none(backend: SystemBackend) -> bool | None:
    """Read the optional mute flag; a missing or broken probe is not fatal."""
    getter = getattr(backend, "is_muted", None)
    if not callable(getter):
        return None
    try:
        return bool(getter())
    except Exception as exc:  # notification of state is best effort
        log.debug("is_muted failed: %s", exc)
        return None


def _read_volume(backend: SystemBackend) -> int:
    try:
        value = int(backend.get_volume())
    except (SystemUnavailable, OSError, TypeError, ValueError) as exc:
        raise ToolError(f"cannot read volume: {exc}") from exc
    return _clamp_percent(value)


def _write_volume(backend: SystemBackend, level: int) -> None:
    try:
        backend.set_volume(level)
    except (SystemUnavailable, OSError, TypeError, ValueError) as exc:
        raise ToolError(f"cannot set volume: {exc}") from exc


def _set_muted(backend: SystemBackend, muted: bool) -> None:
    setter = getattr(backend, "set_muted", None)
    if not callable(setter):
        raise ToolError("the system backend does not support mute")
    try:
        setter(muted)
    except (SystemUnavailable, OSError, TypeError, ValueError) as exc:
        raise ToolError(f"cannot change mute state: {exc}") from exc


def _supports_mute(backend: SystemBackend) -> bool:
    return callable(getattr(backend, "set_muted", None))


class WindowsSystemBackend:
    """Windows 11 implementation; lazy imports keep other tools lightweight."""

    def __init__(
        self,
        *,
        runner: Callable[..., Any] | None = None,
        beep: Callable[[], None] | None = None,
    ) -> None:
        self._runner = runner or subprocess.run
        self._beep_fn = beep or _default_beep

    @staticmethod
    def _require_windows() -> None:
        if sys.platform != "win32":
            raise SystemUnavailable("the system backend is Windows-only")

    def _run(self, argv: list[str], timeout: float) -> Any:
        """Run a fixed argv without a shell and with a minimal environment."""
        try:
            return self._runner(
                argv,
                capture_output=True,
                text=True,
                errors="backslashreplace",
                timeout=timeout,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                env=_minimal_env(),
                check=False,
            )
        except OSError as exc:
            raise SystemUnavailable(f"cannot start {argv[0]}: {exc}") from exc

    # -- notifications ---------------------------------------------------
    def notify(self, title: str, message: str, timeout_s: int = 5) -> None:
        """Show a PowerShell toast; beep and log when the toast cannot run."""
        del timeout_s  # Windows toasts have no arbitrary duration; the call is capped
        try:
            self._show_toast(title, message)
        except (OSError, subprocess.SubprocessError, SystemUnavailable) as exc:
            log.warning("toast notification failed (%s); falling back to a beep", exc)
            self._beep()

    def _show_toast(self, title: str, message: str) -> None:
        self._require_windows()
        xml = _toast_xml(title, message).replace("'", "''")
        script = _TOAST_SCRIPT.replace("__XML__", xml)
        argv = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ]
        completed = self._run(argv, _NOTIFY_TIMEOUT_S)
        if completed.returncode != 0:
            detail = _error_detail(completed.stderr)
            raise SystemUnavailable(
                f"PowerShell toast failed ({completed.returncode}): {detail}"
            )

    def _beep(self) -> None:
        try:
            self._beep_fn()
        except Exception as exc:  # notification is best effort; never crash a task
            log.warning("system beep fallback failed: %s", exc)

    # -- volume ----------------------------------------------------------
    def _endpoint(self) -> Any:
        self._require_windows()
        try:
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        except ImportError as exc:
            raise SystemUnavailable(
                "volume control requires the pycaw and comtypes packages"
            ) from exc
        try:
            speakers = AudioUtilities.GetSpeakers()
            # pycaw >= 2024 exposes EndpointVolume; older versions return the
            # IMMDevice directly and need an explicit Activate/QueryInterface.
            endpoint = getattr(speakers, "EndpointVolume", None)
            if endpoint is not None:
                return endpoint
            if hasattr(speakers, "GetMasterVolumeLevelScalar"):
                return speakers
            interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            return interface.QueryInterface(IAudioEndpointVolume)
        except Exception as exc:  # COM and pycaw versions raise different errors
            raise SystemUnavailable(f"cannot open the audio endpoint: {exc}") from exc

    def get_volume(self) -> int:
        endpoint = self._endpoint()
        try:
            scalar = float(endpoint.GetMasterVolumeLevelScalar())
        except Exception as exc:  # COM failure is an expected OS error here
            raise SystemUnavailable(f"cannot read the volume: {exc}") from exc
        return _clamp_percent(round(scalar * 100))

    def set_volume(self, level: int) -> None:
        target = _clamp_percent(level)
        endpoint = self._endpoint()
        try:
            endpoint.SetMasterVolumeLevelScalar(target / 100.0, None)
        except Exception as exc:
            raise SystemUnavailable(f"cannot set the volume: {exc}") from exc

    def is_muted(self) -> bool:
        endpoint = self._endpoint()
        try:
            return bool(endpoint.GetMute())
        except Exception as exc:
            raise SystemUnavailable(f"cannot read the mute state: {exc}") from exc

    def set_muted(self, muted: bool) -> None:
        endpoint = self._endpoint()
        try:
            endpoint.SetMute(bool(muted), None)
        except Exception as exc:
            raise SystemUnavailable(f"cannot set the mute state: {exc}") from exc

    # -- power -----------------------------------------------------------
    def lock(self) -> None:
        self._require_windows()
        function = ctypes.windll.user32.LockWorkStation
        function.argtypes = []
        function.restype = ctypes.c_int
        if not function():
            raise SystemUnavailable("LockWorkStation failed")

    def sleep(self) -> None:
        self._require_windows()
        function = ctypes.windll.powrprof.SetSuspendState
        function.argtypes = [ctypes.c_bool, ctypes.c_bool, ctypes.c_bool]
        function.restype = ctypes.c_bool
        # False = sleep (not hibernate), no force, wake events stay enabled.
        if not function(False, False, False):
            raise SystemUnavailable("SetSuspendState failed")

    def shutdown(self) -> None:
        self._run_power(_SHUTDOWN_ARGV, "shutdown")

    def restart(self) -> None:
        self._run_power(_RESTART_ARGV, "restart")

    def _run_power(self, argv: tuple[str, ...], action: str) -> None:
        self._require_windows()
        completed = self._run(list(argv), _POWER_TIMEOUT_S)
        if completed.returncode != 0:
            detail = _error_detail(completed.stderr)
            raise SystemUnavailable(
                f"{action} failed ({completed.returncode}): {detail}"
            )


class _SystemToolBase:
    """Shared backend injection; a fake backend is enough for every test."""

    def __init__(self, backend: SystemBackend | None = None) -> None:
        self._backend = backend if backend is not None else WindowsSystemBackend()


class SystemNotifyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=80, description="Notification title.")
    message: str = Field(min_length=1, max_length=500, description="Notification body.")
    timeout_s: int = Field(default=5, ge=1, le=30, description="Display hint in seconds.")


class SystemNotifyTool(_SystemToolBase):
    """Send a local notification; secrets are redacted before they leave Kivi."""

    name = "system.notify"
    description = "Show a local notification with secret redaction (R1)."
    risk = "R1"
    capabilities = ("system.notify",)
    args_model = SystemNotifyArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, SystemNotifyArgs)
        safe_title, _ = redact_secrets(args.title)
        return Preview(
            "system-notify",
            {
                "title": safe_title,
                "message_chars": len(args.message),
                "timeout_s": args.timeout_s,
            },
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, SystemNotifyArgs)
        title, title_count = redact_secrets(args.title)
        message, message_count = redact_secrets(args.message)
        try:
            self._backend.notify(title, message, args.timeout_s)
        except (SystemUnavailable, OSError) as exc:
            raise ToolError(f"cannot show a notification: {exc}") from exc
        return ToolResult(
            ok=True,
            output="notification dispatched",
            meta={
                "title_chars": len(title),
                "message_chars": len(message),
                "redacted": title_count + message_count,
            },
            trust="trusted:system",
        )


class SystemVolumeGetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SystemVolumeGetTool(_SystemToolBase):
    """Read the master volume and, when available, the mute flag."""

    name = "system.volume.get"
    description = "Read the master output volume and mute state (R0)."
    risk = "R0"
    capabilities = ("system.volume",)
    args_model = SystemVolumeGetArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, SystemVolumeGetArgs)
        return Preview("system-volume-get", {}, reversible=True)

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, SystemVolumeGetArgs)
        level = _read_volume(self._backend)
        muted = _muted_or_none(self._backend)
        return ToolResult(
            ok=True,
            output=f"volume: {level}%",
            meta={"level": level, "muted": muted},
            trust="trusted:system",
        )


class SystemVolumeSetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: int | None = Field(default=None, ge=0, le=100)
    delta: int | None = Field(default=None, ge=-100, le=100)
    mute: bool | None = None

    @model_validator(mode="after")
    def _one_operation(self) -> SystemVolumeSetArgs:
        if self.level is not None and self.delta is not None:
            raise ValueError("use either level or delta, not both")
        if self.level is None and self.delta is None and self.mute is None:
            raise ValueError("provide level, delta or mute")
        return self


class SystemVolumeSetTool(_SystemToolBase):
    """Set an absolute volume, a relative delta or the mute flag (R1)."""

    name = "system.volume.set"
    description = "Set output volume by level or delta, or mute/unmute (R1)."
    risk = "R1"
    capabilities = ("system.volume",)
    args_model = SystemVolumeSetArgs

    @staticmethod
    def _target(args: SystemVolumeSetArgs, current: int) -> int:
        if args.level is not None:
            return _clamp_percent(args.level)
        if args.delta is not None:
            return _clamp_percent(current + args.delta)
        return current

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, SystemVolumeSetArgs)
        current = _read_volume(self._backend)
        target = self._target(args, current)
        muted = _muted_or_none(self._backend)
        if args.mute is not None:
            muted = args.mute
        return Preview(
            "system-volume-set",
            {
                "current": current,
                "target": target,
                "mute": args.mute,
                "muted": muted,
            },
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, SystemVolumeSetArgs)
        current = _read_volume(self._backend)
        target = self._target(args, current)
        changes_volume = args.level is not None or args.delta is not None

        # Fail before a partial change when mute is requested but unsupported.
        if args.mute is not None and not _supports_mute(self._backend):
            raise ToolError("the system backend does not support mute")
        if changes_volume:
            _write_volume(self._backend, target)
        if args.mute is not None:
            _set_muted(self._backend, args.mute)

        try:
            actual = _clamp_percent(int(self._backend.get_volume()))
        except (SystemUnavailable, OSError, TypeError, ValueError):
            actual = target
        muted = _muted_or_none(self._backend)
        if muted is None and args.mute is not None:
            muted = args.mute

        parts: list[str] = []
        if changes_volume:
            parts.append(f"volume set to {target}%")
        if args.mute is not None:
            parts.append("muted" if args.mute else "unmuted")
        return ToolResult(
            ok=True,
            output="; ".join(parts),
            meta={
                "previous": current,
                "target": target,
                "level": actual,
                "muted": muted,
            },
            trust="trusted:system",
        )


class SystemPowerArgs(BaseModel):
    """Power tools take no arguments; the tool name is the action."""

    model_config = ConfigDict(extra="forbid")


class _PowerTool(_SystemToolBase):
    """One irreversible or disruptive power action with a shared contract."""

    action = ""
    outcome = "action requested"
    failure = "power action failed"
    reversible = False
    warning = ""

    def _invoke(self) -> None:
        raise NotImplementedError

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, SystemPowerArgs)
        return Preview(
            self.action,
            {},
            reversible=self.reversible,
            warnings=(self.warning,) if self.warning else (),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, SystemPowerArgs)
        try:
            self._invoke()
        except (SystemUnavailable, OSError) as exc:
            raise ToolError(f"{self.failure}: {exc}") from exc
        return ToolResult(
            ok=True,
            output=self.outcome,
            meta={"action": self.action},
            trust="trusted:system",
        )


class SystemLockTool(_PowerTool):
    """Lock the workstation; R2 requires confirmation."""

    name = "system.lock"
    description = "Lock the workstation; requires confirmation (R2)."
    risk = "R2"
    capabilities = ("system.power",)
    args_model = SystemPowerArgs
    action = "system-lock"
    outcome = "workstation locked"
    failure = "cannot lock the workstation"
    reversible = True
    warning = "the session will lock; sign in again to continue"

    def _invoke(self) -> None:
        self._backend.lock()


class SystemSleepTool(_PowerTool):
    """Suspend the machine; R3 requires confirmation."""

    name = "system.sleep"
    description = "Suspend the system; requires confirmation (R3)."
    risk = "R3"
    capabilities = ("system.power",)
    args_model = SystemPowerArgs
    action = "system-sleep"
    outcome = "sleep requested"
    failure = "cannot suspend the system"
    reversible = True
    warning = "the system will suspend; save your work first"

    def _invoke(self) -> None:
        self._backend.sleep()


class SystemShutdownTool(_PowerTool):
    """Shut the machine down; R4 is hard-denied by the Permission Engine."""

    name = "system.shutdown"
    description = "Shut the system down; R4 is hard-denied by policy."
    risk = "R4"
    capabilities = ("system.power",)
    args_model = SystemPowerArgs
    action = "system-shutdown"
    outcome = "shutdown requested"
    failure = "cannot shut down"
    reversible = False
    warning = "the system will shut down; R4 is hard-denied"

    def _invoke(self) -> None:
        self._backend.shutdown()


class SystemRestartTool(_PowerTool):
    """Restart the machine; R4 is hard-denied by the Permission Engine."""

    name = "system.restart"
    description = "Restart the system; R4 is hard-denied by policy."
    risk = "R4"
    capabilities = ("system.power",)
    args_model = SystemPowerArgs
    action = "system-restart"
    outcome = "restart requested"
    failure = "cannot restart"
    reversible = False
    warning = "the system will restart; R4 is hard-denied"

    def _invoke(self) -> None:
        self._backend.restart()


def build_system_tools(backend: SystemBackend | None = None) -> tuple[Tool, ...]:
    """Create all M3 system tools, sharing one backend instance."""
    shared = backend if backend is not None else WindowsSystemBackend()
    return (
        SystemNotifyTool(shared),
        SystemVolumeGetTool(shared),
        SystemVolumeSetTool(shared),
        SystemLockTool(shared),
        SystemSleepTool(shared),
        SystemShutdownTool(shared),
        SystemRestartTool(shared),
    )


__all__ = [
    "MuteBackend",
    "SystemBackend",
    "SystemLockTool",
    "SystemNotifyArgs",
    "SystemNotifyTool",
    "SystemPowerArgs",
    "SystemRestartTool",
    "SystemShutdownTool",
    "SystemSleepTool",
    "SystemUnavailable",
    "SystemVolumeGetArgs",
    "SystemVolumeGetTool",
    "SystemVolumeSetArgs",
    "SystemVolumeSetTool",
    "WindowsSystemBackend",
    "build_system_tools",
]
