from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

from kivi.interfaces import cli as cli_module
from kivi.interfaces.cli import main
from kivi.paths import Paths, project_root

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


def make_tools_profile(tmp_path: Path) -> Paths:
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "config" / "kivi.yaml").write_text("version: 1\n", encoding="utf-8")
    shutil.copy(project_root() / "config" / "models.yaml", root / "config" / "models.yaml")
    (root / "notes.txt").write_text("hello world", encoding="utf-8")
    return Paths(root=root, home=tmp_path / "profile")


def test_tools_list_and_json(tmp_path: Path, monkeypatch) -> None:
    paths = make_tools_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)

    assert main(["tools", "list"]) == 0
    assert main(["tools", "list", "--json"]) == 0


def test_tools_run_reads_a_file(tmp_path: Path, monkeypatch) -> None:
    paths = make_tools_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)

    assert main(["tools", "run", "fs.read", "--args", '{"path": "notes.txt"}']) == 0


def test_tools_run_dry_run_needs_no_approval(tmp_path: Path, monkeypatch) -> None:
    paths = make_tools_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)

    code = main(
        [
            "tools",
            "run",
            "shell.exec",
            "--args",
            '{"argv": ["python", "-c", "print(1)"]}',
            "--dry-run",
        ]
    )
    assert code == 0


def test_tools_run_denies_safety_write(tmp_path: Path, monkeypatch) -> None:
    paths = make_tools_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)

    code = main(
        [
            "tools",
            "run",
            "fs.write",
            "--args",
            '{"path": "src/kivi/safety/evil.py", "content": "x"}',
        ]
    )
    assert code == 1


def test_tools_run_rejects_bad_json(tmp_path: Path, monkeypatch) -> None:
    paths = make_tools_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)

    assert main(["tools", "run", "fs.read", "--args", "not-json"]) == 2


def test_chat_uses_the_fast_path(tmp_path: Path, monkeypatch) -> None:
    paths = make_tools_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)
    assert main(["chat", "прочитай notes.txt"]) == 0


def test_say_uses_the_tts_pipeline(tmp_path: Path, monkeypatch) -> None:
    paths = make_tools_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)
    target = tmp_path / "speech"
    target.mkdir()

    class FakePipeline:
        def synthesize(self, text, output):
            path = output / "speech_000.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"RIFF")
            return [SimpleNamespace(path=path, duration_s=0.1, engine="fake")]

    monkeypatch.setattr(cli_module, "_speech_pipeline", lambda config: FakePipeline())
    assert main(["say", "привет", "--out", str(target)]) == 0


def test_coder_command(tmp_path: Path, monkeypatch) -> None:
    paths = make_tools_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)

    class FakeCoder:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run(self, goal, **kwargs):
            return SimpleNamespace(
                ok=True,
                attempts=1,
                files=("app.py",),
                test_output="1 passed",
                last_error="",
                stopped_reason="passed",
            )

    monkeypatch.setattr(cli_module, "Coder", FakeCoder)
    assert main(["coder", "создай проект"]) == 0


def test_listen_command_uses_voice_session(tmp_path: Path, monkeypatch) -> None:
    paths = make_tools_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)
    monkeypatch.setattr(cli_module, "_microphone_available", lambda: True)

    class FakeSession:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run_once(self, push_to_talk=False):
            return SimpleNamespace(
                addressed=True,
                text="привет",
                answer="ответ",
                stopped=False,
                metrics=SimpleNamespace(as_dict=lambda: {"total_ms": 1.0}),
            )

    for name in (
        "SoundDeviceSource",
        "VadSegmenter",
        "EnergyVad",
        "WhisperTranscriber",
    ):
        monkeypatch.setattr(cli_module, name, lambda *args, **kwargs: object())
    monkeypatch.setattr(
        cli_module,
        "TextWakeWordDetector",
        lambda *args, **kwargs: SimpleNamespace(detect=lambda text: True),
    )
    monkeypatch.setattr(cli_module, "VoiceSession", FakeSession)
    assert main(["listen", "--mic", "--turns", "1"]) == 0


def test_crm_read_and_fill_commands(tmp_path: Path, monkeypatch) -> None:
    paths = make_tools_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)
    seen: list[tuple[str, dict, bool]] = []

    def fake_tool(paths_arg, config, name, payload, caps, *, approved=False):
        seen.append((name, payload, approved))
        return SimpleNamespace(ok=True, output="ok")

    monkeypatch.setattr(cli_module, "_run_direct_tool", fake_tool)
    assert main(["crm", "read"]) == 0
    assert main(
        [
            "crm",
            "fill",
            "--field",
            "Согласованные сроки ремонта=2 недели",
            "--approve",
        ]
    ) == 0
    assert seen[0][0] == "crm.read"
    assert seen[1][0] == "crm.fill"
    assert seen[1][2] is True


def test_say_qwen_engine(tmp_path: Path, monkeypatch) -> None:
    paths = make_tools_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)
    out = tmp_path / "qwen.wav"

    class FakeEngine:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def synth(self, text, target):
            target.write_bytes(b"RIFF")
            return SimpleNamespace(
                path=target,
                duration_s=0.2,
                engine="qwen3-tts",
            )

    monkeypatch.setattr(cli_module, "QwenTtsEngine", FakeEngine)
    assert main(["say", "--engine", "qwen", "привет", "--out", str(out)]) == 0


def test_voice_design_command(tmp_path: Path, monkeypatch) -> None:
    paths = make_tools_profile(tmp_path)
    monkeypatch.setattr(cli_module, "get_paths", lambda: paths)
    out = tmp_path / "ref.wav"

    class FakeDesigner:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def design(self, text, target, *, instruct):
            target.write_bytes(b"RIFF")
            target.with_suffix(".txt").write_text(text, encoding="utf-8")
            return SimpleNamespace(path=target, duration_s=0.2)

    monkeypatch.setattr(cli_module, "QwenVoiceDesigner", FakeDesigner)
    assert main(["voice", "design", "--text", "привет", "--out", str(out)]) == 0


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
