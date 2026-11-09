from __future__ import annotations

from scripts import bench_gateway


def test_fake_benchmark_runs_and_reports(capsys) -> None:
    code = bench_gateway.main(
        [
            "--fake",
            "--switches",
            "4",
            "--models",
            "qwen3.5:4b,llama3:8b",
            "--no-write",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "mode=benchmark" in out
    assert "summary: ok=4 failed=0" in out


def test_zero_switches_is_rejected(capsys) -> None:
    assert bench_gateway.main(["--fake", "--switches", "0", "--no-write"]) == 2
    assert "must be positive" in capsys.readouterr().out


def test_unknown_model_is_rejected(capsys) -> None:
    code = bench_gateway.main(["--fake", "--switches", "1", "--models", "missing", "--no-write"])
    assert code == 2
    assert "unknown model" in capsys.readouterr().out
