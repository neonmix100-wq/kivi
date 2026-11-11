"""Typed configuration for Kivi.

The single source of truth is ``config/kivi.yaml``. Pydantic validates it at
startup so a typo or an unsafe setting fails fast instead of misbehaving at
runtime. Relative filesystem paths are resolved against the repository root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from kivi.paths import project_root

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_ALLOWED_SCOPES = frozenset({GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE})

MAIL_METHODS_ALWAYS_DENIED = frozenset({"users.messages.send", "users.drafts.send"})

SAMPLE_RATES = (8000, 24000, 48000)


class CloudSettings(BaseModel):
    """Policy for borrowing free cloud capacity."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["off", "manual", "auto_free"] = "auto_free"
    cost_limit_usd: float = Field(default=0.0, ge=0)
    allow_data_for_training: bool = False
    scout_on_quota_exhausted: bool = True


class ModelGatewaySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    prefer_local: bool = True
    one_heavy_model: bool = True
    safety_margin_mb: int = Field(default=512, ge=0)
    vram_total_mb: int = Field(default=8192, gt=0)
    fallback: list[Literal["gpu", "cpu", "cloud"]] = Field(
        default_factory=lambda: ["gpu", "cpu", "cloud"]
    )
    cloud: CloudSettings = Field(default_factory=CloudSettings)
    watchdog_enabled: bool = True
    watchdog_interval_s: float = Field(default=30.0, ge=1.0)
    cpu_fallback: bool = True


class VoiceFallback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = "piper"
    voice: str = "ru_RU-irina-medium"


class CloneSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    consent_file: Path = Path("private/voice/consent/consent.pdf")
    dataset_dir: Path = Path("private/voice/dataset")
    model_dir: Path = Path("private/voice/models")


class VoiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    provider: Literal["silero", "piper", "xtts", "edge"] = "silero"
    model: str = "v5_ru"
    speaker: str = "baya"
    sample_rate: Literal[8000, 24000, 48000] = 48000
    output: Path = Path("assets/voice-samples/silero_v5_baya_48k.wav")
    rate: float = Field(default=1.0, ge=0.5, le=2.0)
    put_accent: bool = True
    put_yo: bool = True
    stream_by_sentence: bool = True
    fallback: VoiceFallback = Field(default_factory=VoiceFallback)
    cloud_fallback: bool = False
    clone: CloneSettings = Field(default_factory=CloneSettings)

    @model_validator(mode="after")
    def _clone_needs_consent(self) -> VoiceSettings:
        # The flag alone is not permission; the consent file is checked by the
        # voice module. Keep cloning off unless explicitly enabled.
        return self


class AvatarSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "2d", "vrm"] = "2d"
    vrm: Path = Path("assets/avatar/kivi.vrm")
    lip_sync: Literal["amplitude", "viseme"] = "amplitude"
    always_on_top: bool = True
    display: Literal["desktop", "window", "second_monitor"] = "desktop"
    hide_hotkey: str = "Ctrl+Alt+K"
    outfit: str = "summer_casual_05"


class ShellSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["A", "B", "C"] = "A"
    explorer_fallback: bool = True
    emergency_hotkey: str = "Ctrl+Alt+Shift+E"


class MailSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["gmail"] = "gmail"
    auth: Literal["oauth"] = "oauth"
    scopes: list[str] = Field(default_factory=list)
    drafts_only: bool = True
    auto_send: bool = False
    allowed_methods: list[str] = Field(default_factory=list)
    # Sending is denied by default: a missing entry must never widen access.
    denied_methods: list[str] = Field(default_factory=lambda: sorted(MAIL_METHODS_ALWAYS_DENIED))

    @model_validator(mode="after")
    def _drafts_only_is_enforced(self) -> MailSettings:
        if self.drafts_only:
            if self.auto_send:
                raise ValueError("mail.auto_send must be false while mail.drafts_only is true")
            unknown = set(self.scopes) - GMAIL_ALLOWED_SCOPES
            if unknown:
                raise ValueError(f"scopes not allowed for drafts-only Gmail: {sorted(unknown)}")
            missing = MAIL_METHODS_ALWAYS_DENIED - set(self.denied_methods)
            if missing:
                raise ValueError(f"drafts-only Gmail must deny: {sorted(missing)}")
        return self


class TelegramSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["bot"] = "bot"
    allow_groups: bool = False


class SecuritySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protected_zones_signed: bool = True
    audit_enabled: bool = True
    killswitch_hotkey: str = "Ctrl+Alt+J"
    redact_secrets: bool = True
    egress_deny: list[str] = Field(default_factory=list)
    blocked_hosts: list[str] = Field(default_factory=list)

    @field_validator("blocked_hosts")
    @classmethod
    def _clean_blocked_hosts(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for value in values:
            host = value.strip().lower()
            if not host:
                raise ValueError("blocked host must not be empty")
            if "://" in host or "/" in host:
                raise ValueError(f"blocked host must be a hostname pattern: {value!r}")
            if host not in cleaned:
                cleaned.append(host)
        return cleaned


class KiviConfig(BaseModel):
    """Validated representation of ``config/kivi.yaml``."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    name: str = "Kivi"
    language: str = "ru"
    autonomy: str = "A4-Bounded"
    model_gateway: ModelGatewaySettings = Field(default_factory=ModelGatewaySettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    avatar: AvatarSettings = Field(default_factory=AvatarSettings)
    shell: ShellSettings = Field(default_factory=ShellSettings)
    mail: MailSettings = Field(default_factory=MailSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)

    @model_validator(mode="after")
    def _check_autonomy(self) -> KiviConfig:
        allowed = {"A0", "A1", "A2", "A3", "A4", "A4-Bounded"}
        if self.autonomy not in allowed:
            raise ValueError(f"autonomy must be one of {sorted(allowed)}")
        return self


def load_config(path: Path | None = None) -> KiviConfig:
    """Load and validate the YAML config. Raises ``ConfigError`` on failure."""
    path = path or project_root() / "config" / "kivi.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"config root must be a mapping: {path}")

    try:
        return KiviConfig.model_validate(raw)
    except ValidationError as exc:
        lines = ["invalid configuration:"]
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "<root>"
            lines.append(f"  {location}: {error['msg']}")
        raise ConfigError("\n".join(lines)) from exc


class ConfigError(ValueError):
    """Raised when the config file is missing or invalid."""
