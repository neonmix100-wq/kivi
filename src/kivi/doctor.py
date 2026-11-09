"""Environment diagnostics for ``kivi doctor``.

The goal is a truthful, fast snapshot of what Kivi can actually use right now:
hardware, local runtimes, config validity and writable state directories.
"""

from __future__ import annotations

import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import psutil

from kivi.config import ConfigError, KiviConfig, load_config
from kivi.paths import Paths, get_paths

Status = str  # "ok" | "warn" | "fail"

OLLAMA_URL = "http://127.0.0.1:11434"
SD_WEBUI_PORT = 7860


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: Status
    detail: str


@dataclass(frozen=True, slots=True)
class GpuInfo:
    name: str
    total_mb: int
    used_mb: int
    driver: str

    @property
    def free_mb(self) -> int:
        return max(0, self.total_mb - self.used_mb)


def gpu_info() -> list[GpuInfo]:
    query = "name,memory.total,memory.used,driver_version"
    try:
        result = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    gpus: list[GpuInfo] = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4:
            continue
        name, total, used, driver = parts
        try:
            gpus.append(GpuInfo(name=name, total_mb=int(total), used_mb=int(used), driver=driver))
        except ValueError:
            continue
    return gpus


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ollama_models(timeout: float = 1.5) -> list[str]:
    try:
        import httpx

        response = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except Exception:  # noqa: BLE001 - diagnostics must not fail the command
        return []
    return [item.get("name", "?") for item in payload.get("models", [])]


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".kivi-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _load_config_quietly(paths: Paths) -> KiviConfig | None:
    try:
        return load_config(paths.config_file)
    except ConfigError:
        return None


def run_checks(paths: Paths | None = None) -> list[Check]:
    paths = (paths or get_paths()).ensure()
    checks: list[Check] = []

    checks.append(
        Check("Python", "ok", f"{platform.python_version()} ({sys.executable})")
    )
    checks.append(Check("OS", "ok", platform.platform()))

    physical = psutil.cpu_count(logical=False) or 0
    logical = psutil.cpu_count(logical=True) or 0
    checks.append(
        Check("CPU", "ok" if physical >= 4 else "warn", f"{physical} cores / {logical} threads")
    )

    mem = psutil.virtual_memory()
    checks.append(
        Check(
            "RAM",
            "ok" if mem.total >= 16 * 1024**3 else "warn",
            f"{mem.total / 1024**3:.1f} GB total, {mem.available / 1024**3:.1f} GB free",
        )
    )

    gpus = gpu_info()
    if gpus:
        for index, gpu in enumerate(gpus):
            status = "ok" if gpu.total_mb >= 12288 else "warn"
            checks.append(
                Check(
                    f"GPU {index}",
                    status,
                    f"{gpu.name}, {gpu.total_mb} MB VRAM "
                    f"({gpu.used_mb} MB used, {gpu.free_mb} MB free), driver {gpu.driver}",
                )
            )
    else:
        checks.append(Check("GPU", "warn", "nvidia-smi not found; GPU features unavailable"))

    for label, target in (("Disk C:", "C:\\"), ("Disk D:", "D:\\")):
        try:
            usage = shutil.disk_usage(target)
        except OSError:
            continue
        free_gb = usage.free / 1024**3
        checks.append(
            Check(
                label,
                "ok" if free_gb >= 50 else "warn",
                f"{free_gb:.1f} GB free of {usage.total / 1024**3:.1f} GB",
            )
        )

    config = _load_config_quietly(paths)
    if config is None:
        checks.append(Check("Config", "fail", f"invalid or missing: {paths.config_file}"))
    else:
        checks.append(
            Check(
                "Config",
                "ok",
                f"voice={config.voice.provider}/{config.voice.speaker}, "
                f"autonomy={config.autonomy}, shell={config.shell.mode}",
            )
        )

    models = ollama_models()
    if models:
        checks.append(Check("Ollama", "ok", f"{len(models)} models: {', '.join(models[:5])}"))
    elif port_open(11434):
        checks.append(Check("Ollama", "warn", "port 11434 is open but the API did not answer"))
    else:
        checks.append(Check("Ollama", "warn", "not running (127.0.0.1:11434)"))

    if port_open(SD_WEBUI_PORT):
        checks.append(Check("SD WebUI", "ok", f"listening on 127.0.0.1:{SD_WEBUI_PORT}"))
    else:
        checks.append(Check("SD WebUI", "warn", "not running (127.0.0.1:7860)"))

    writable = _writable(paths.home)
    checks.append(
        Check(
            "Data dir",
            "ok" if writable else "fail",
            str(paths.home) if writable else f"not writable: {paths.home}",
        )
    )
    return checks


def summary(checks: list[Check]) -> str:
    fails = sum(1 for check in checks if check.status == "fail")
    warns = sum(1 for check in checks if check.status == "warn")
    if fails:
        return f"{fails} failed, {warns} warnings"
    if warns:
        return f"all required checks passed, {warns} warnings"
    return "all checks passed"
