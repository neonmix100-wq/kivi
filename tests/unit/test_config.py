from __future__ import annotations

from pathlib import Path

import pytest

from kivi.config import ConfigError, KiviConfig, load_config
from kivi.paths import project_root

CONFIG_PATH = project_root() / "config" / "kivi.yaml"


def test_repository_config_is_valid() -> None:
    config = load_config(CONFIG_PATH)
    assert config.name == "Kivi"
    assert config.voice.provider == "silero"
    assert config.voice.speaker == "baya"
    assert config.voice.sample_rate == 48000
    assert config.mail.drafts_only is True
    assert config.mail.auto_send is False


def test_invalid_yaml_raises(tmp_path: Path) -> None:
    path = tmp_path / "kivi.yaml"
    path.write_text("name: [", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_config_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "kivi.yaml"
    path.write_text("name: Kivi\nnonsense: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="nonsense"):
        load_config(path)


def test_drafts_only_rejects_auto_send(tmp_path: Path) -> None:
    path = tmp_path / "kivi.yaml"
    path.write_text(
        "mail:\n"
        "  drafts_only: true\n"
        "  auto_send: true\n"
        "  denied_methods: [users.messages.send, users.drafts.send]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="auto_send"):
        load_config(path)


def test_drafts_only_requires_send_denies(tmp_path: Path) -> None:
    path = tmp_path / "kivi.yaml"
    path.write_text(
        "mail:\n"
        "  drafts_only: true\n"
        "  auto_send: false\n"
        "  denied_methods: [users.messages.send]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="deny"):
        load_config(path)


def test_drafts_only_rejects_broad_scopes(tmp_path: Path) -> None:
    path = tmp_path / "kivi.yaml"
    path.write_text(
        "mail:\n"
        "  drafts_only: true\n"
        "  scopes: ['https://mail.google.com/']\n"
        "  denied_methods: [users.messages.send, users.drafts.send]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="scopes"):
        load_config(path)


def test_invalid_sample_rate(tmp_path: Path) -> None:
    path = tmp_path / "kivi.yaml"
    path.write_text("voice:\n  sample_rate: 44100\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="sample_rate"):
        load_config(path)


def test_invalid_autonomy(tmp_path: Path) -> None:
    path = tmp_path / "kivi.yaml"
    path.write_text("autonomy: total\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="autonomy"):
        load_config(path)


def test_defaults_are_safe() -> None:
    config = KiviConfig()
    assert config.autonomy == "A4-Bounded"
    assert config.voice.cloud_fallback is False
    assert config.voice.clone.enabled is False
    assert config.mail.auto_send is False
    assert config.shell.explorer_fallback is True
