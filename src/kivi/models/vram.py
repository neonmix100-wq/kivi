"""VRAM accounting via ``nvidia-smi``.

The passported 8/12 GB is not a budget: Windows, Chrome and the desktop already
use part of it. Every load decision starts from the observed free VRAM.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

_SMI_QUERY = "memory.total,memory.used,memory.free"


class VramError(RuntimeError):
    """Raised when GPU memory cannot be measured."""


@dataclass(frozen=True, slots=True)
class VramSnapshot:
    total_mb: int
    used_mb: int
    free_mb: int

    def __post_init__(self) -> None:
        for name, value in (
            ("total_mb", self.total_mb),
            ("used_mb", self.used_mb),
            ("free_mb", self.free_mb),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative")

    @property
    def used_ratio(self) -> float:
        return self.used_mb / self.total_mb if self.total_mb else 0.0

    @property
    def overcommitted(self) -> bool:
        """True when observed memory falls short of the physical total.

        WDDM reserves a small amount for the driver, so allow a fixed
        tolerance instead of treating every rounding difference as overflow.
        """
        tolerance = max(256, int(self.total_mb * 0.03))
        return self.used_mb + self.free_mb < self.total_mb - tolerance

    def as_dict(self) -> dict[str, int]:
        return {
            "total_mb": self.total_mb,
            "used_mb": self.used_mb,
            "free_mb": self.free_mb,
        }


def parse_smi_output(stdout: str) -> VramSnapshot | None:
    """Parse the first GPU line; return ``None`` when the output is unusable."""
    for line in stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            total, used, free = (int(part) for part in parts)
        except ValueError:
            continue
        if total <= 0:
            continue
        return VramSnapshot(total_mb=total, used_mb=used, free_mb=free)
    return None


def _query_nvml() -> VramSnapshot | None:
    """Read the first GPU through NVML when the driver is available.

    NVML is preferred over ``nvidia-smi`` because it avoids spawning a process
    on every watchdog tick. It reports the same WDDM numbers, not the shared
    system-memory overflow, so the safety margin still applies.
    """
    try:
        import pynvml
    except ImportError:
        return None
    try:
        pynvml.nvmlInit()
        try:
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            info = pynvml.nvmlDeviceGetMemoryInfo(handle)
        finally:
            pynvml.nvmlShutdown()
    except Exception:
        return None
    mib = 1024 * 1024
    snapshot = VramSnapshot(
        total_mb=int(info.total // mib),
        used_mb=int(info.used // mib),
        free_mb=int(info.free // mib),
    )
    return snapshot if snapshot.total_mb > 0 else None


def query_vram(timeout: float = 5.0) -> VramSnapshot | None:
    """Return current GPU memory, preferring NVML over nvidia-smi."""
    snapshot = _query_nvml()
    if snapshot is not None:
        return snapshot
    try:
        result = subprocess.run(
            ["nvidia-smi", f"--query-gpu={_SMI_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return parse_smi_output(result.stdout)
