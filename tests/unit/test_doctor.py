from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from kivi import doctor
from kivi.doctor import Check, GpuInfo, gpu_info, run_checks, summary
from kivi.paths import get_paths


def test_summary_counts_failures_and_warnings() -> None:
    assert summary([Check("a", "ok", ""), Check("b", "warn", "")]) == (
        "all required checks passed, 1 warnings"
    )
    assert summary([Check("a", "ok", ""), Check("b", "fail", "")]) == "1 failed, 0 warnings"
    assert summary([Check("a", "ok", "")]) == "all checks passed"


def test_gpu_info_parses_nvidia_smi(monkeypatch) -> None:
    output = "NVIDIA RTX 3060 Ti, 8192, 5876, 621.01\n"
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=output),
    )
    gpus = gpu_info()
    assert len(gpus) == 1
    assert gpus[0] == GpuInfo("NVIDIA RTX 3060 Ti", 8192, 5876, "621.01")
    assert gpus[0].free_mb == 2316


def test_gpu_info_tolerates_missing_nvidia_smi(monkeypatch) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(doctor.subprocess, "run", missing)
    assert gpu_info() == []


def test_gpu_info_skips_malformed_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        doctor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="bad row\n"),
    )
    assert gpu_info() == []


def test_run_checks_without_external_services(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIVI_HOME", str(tmp_path / "profile"))
    monkeypatch.setattr(doctor, "gpu_info", lambda: [])
    monkeypatch.setattr(doctor, "ollama_models", lambda: ["qwen3.5:4b"])
    monkeypatch.setattr(doctor, "port_open", lambda *args, **kwargs: False)

    paths = get_paths()
    checks = run_checks(paths)
    by_name = {check.name: check for check in checks}

    assert by_name["Config"].status == "ok"
    assert "fish/baya" in by_name["Config"].detail
    assert by_name["Ollama"].status == "ok"
    assert by_name["SD WebUI"].status == "warn"
    assert by_name["Data dir"].status == "ok"
    assert by_name["GPU"].status == "warn"
    assert all(check.status in {"ok", "warn", "fail"} for check in checks)


def test_invalid_config_is_reported(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIVI_HOME", str(tmp_path / "profile"))
    monkeypatch.setattr(doctor, "gpu_info", lambda: [])
    monkeypatch.setattr(doctor, "ollama_models", lambda: [])
    monkeypatch.setattr(doctor, "port_open", lambda *args, **kwargs: False)

    paths = get_paths(root=tmp_path)
    checks = run_checks(paths)
    config_check = next(check for check in checks if check.name == "Config")
    assert config_check.status == "fail"
