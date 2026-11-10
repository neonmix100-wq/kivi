from __future__ import annotations

from pathlib import Path

from kivi.interfaces import cli as cli_module
from kivi.interfaces.cli import main
from kivi.paths import Paths

CANDIDATES_YAML = """\
candidates:
  - id: new-one
    name: New One
    type: llm
    base_url: https://example.test/v1
    api_key_env: NEW_ONE_KEY
    free_requests_per_day: 10
    data_policy: public_only
    trains_on_data: false
    watermark: none
"""

TRAINING_ONLY_YAML = """\
candidates:
  - id: trainer
    name: Trainer
    type: llm
    base_url: https://example.test/v1
    api_key_env: TRAINER_KEY
    free_requests_per_day: 10
    data_policy: public_only
    trains_on_data: true
    watermark: none
"""

CLOUD_YAML = """\
providers:
  - id: existing
    kind: openai
    base_url: https://example.test/v1
    api_key_env: EXISTING_KEY
    free_requests_per_day: 10
"""

EXHAUSTED_CLOUD_YAML = """\
providers:
  - id: exhausted
    kind: openai
    base_url: https://example.test/v1
    api_key_env: EXHAUSTED_KEY
    free_requests_per_day: 0
"""


def make_cli_profile(
    tmp_path: Path,
    candidates: str = CANDIDATES_YAML,
    cloud: str = CLOUD_YAML,
) -> Paths:
    root = tmp_path / "repo"
    config = root / "config"
    config.mkdir(parents=True)
    (config / "kivi.yaml").write_text("version: 1\n", encoding="utf-8")
    (config / "cloud.yaml").write_text(cloud, encoding="utf-8")
    (config / "cloud-candidates.yaml").write_text(candidates, encoding="utf-8")
    return Paths(root=root, home=tmp_path / "profile")


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


def test_cloud_scout_lists_then_approves(tmp_path: Path, monkeypatch) -> None:
    paths = make_cli_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)

    assert main(["cloud", "scout"]) == 0
    assert main(["cloud", "scout", "--approve", "new-one"]) == 0
    written = (paths.root / "config" / "cloud.yaml").read_text(encoding="utf-8")
    assert "new-one" in written
    assert "NEW_ONE_KEY" in written


def test_cloud_scout_saves_handoff_when_nothing_fits(tmp_path: Path, monkeypatch) -> None:
    paths = make_cli_profile(tmp_path, TRAINING_ONLY_YAML)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)

    assert main(["cloud", "scout", "--goal", "finish the report", "--next-step", "write"]) == 0
    assert main(["cloud", "handoff"]) == 0
    assert main(["cloud", "handoff", "--show", "1"]) == 0
    assert main(["cloud", "handoff", "--show", "999"]) == 1


def test_cloud_ask_checkpoints_and_scouts_after_quota(tmp_path: Path, monkeypatch) -> None:
    paths = make_cli_profile(tmp_path, cloud=EXHAUSTED_CLOUD_YAML)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)
    monkeypatch.setenv("EXHAUSTED_KEY", "not-a-real-key")

    code = main(["cloud", "ask", "summarize", "--data-class", "public"])

    assert code == 1
    assert main(["cloud", "handoff", "--show", "1"]) == 0


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
