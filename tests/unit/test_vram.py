from __future__ import annotations

from types import SimpleNamespace

from kivi.models import vram
from kivi.models.vram import VramSnapshot, parse_smi_output, query_vram


def test_parse_first_gpu() -> None:
    snapshot = parse_smi_output("8192, 5876, 2316\n12288, 100, 12188\n")
    assert snapshot == VramSnapshot(total_mb=8192, used_mb=5876, free_mb=2316)


def test_parse_skips_malformed_rows() -> None:
    assert parse_smi_output("bad,row\n8192, nope, 2316\n") is None
    assert parse_smi_output("") is None


def test_zero_total_is_ignored() -> None:
    assert parse_smi_output("0, 0, 0\n") is None


def test_snapshot_properties() -> None:
    snapshot = VramSnapshot(total_mb=8192, used_mb=4096, free_mb=4096)
    assert snapshot.used_ratio == 0.5
    assert not snapshot.overcommitted
    assert snapshot.as_dict()["free_mb"] == 4096


def test_overcommit_detection() -> None:
    snapshot = VramSnapshot(total_mb=8192, used_mb=5000, free_mb=1000)
    assert snapshot.overcommitted
    # Driver-reserved memory within tolerance is not an overflow.
    reserved = VramSnapshot(total_mb=8192, used_mb=1721, free_mb=6287)
    assert not reserved.overcommitted


def test_negative_values_are_rejected() -> None:
    try:
        VramSnapshot(total_mb=-1, used_mb=0, free_mb=0)
    except ValueError as exc:
        assert "total_mb" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("negative total must fail")


def test_query_vram_parses_nvidia_smi(monkeypatch) -> None:
    monkeypatch.setattr(
        vram.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="8192, 5876, 2316\n"),
    )
    assert query_vram() == VramSnapshot(8192, 5876, 2316)


def test_query_vram_without_nvidia_smi(monkeypatch) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(vram.subprocess, "run", missing)
    assert query_vram() is None
