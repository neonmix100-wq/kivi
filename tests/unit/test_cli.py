from __future__ import annotations

from pathlib import Path

from kivi.interfaces.cli import main


def test_config_check(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIVI_HOME", str(tmp_path / "profile"))
    assert main(["config", "check"]) == 0


def test_chat_persists_task_and_event(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIVI_HOME", str(tmp_path / "profile"))
    assert main(["chat", "привет"]) == 0
    assert (tmp_path / "profile" / "kivi.db").is_file()
    assert main(["events", "-n", "5"]) == 0


def test_chat_without_prompt_is_a_noop(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIVI_HOME", str(tmp_path / "profile"))
    assert main(["chat"]) == 0
    assert not (tmp_path / "profile" / "kivi.db").exists()


def test_audit_on_empty_database(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIVI_HOME", str(tmp_path / "profile"))
    assert main(["audit"]) == 0


def test_cloud_status(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("KIVI_HOME", str(tmp_path / "profile"))
    assert main(["cloud", "status"]) == 0


def test_module_entrypoint_delegates(monkeypatch) -> None:
    import kivi.__main__ as entry

    calls: list[list[str]] = []

    class FakeMain:
        @staticmethod
        def main(argv=None) -> int:
            calls.append(argv or [])
            return 7

    monkeypatch.setattr(entry, "main", FakeMain.main)
    assert entry.main(["config", "check"]) == 7
    assert calls == [["config", "check"]]
