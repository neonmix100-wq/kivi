"""Cloud Scout: free-provider discovery, cards and manual confirmation.

The Scout is advisory. It reads a local catalog of candidate providers, checks
free quotas, data policy and watermark conditions, and prepares a card. It
never creates accounts, never bypasses CAPTCHA and never receives a key: the
user confirms a candidate explicitly, and only then is the provider appended to
``config/cloud.yaml``. The key still lives in the environment.

When a quota runs out, the Scout saves a handoff checkpoint first and only then
proposes new candidates, so the local model can continue from the same point
even when the user rejects every card.
"""

from __future__ import annotations

import os
import re
import textwrap
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml

from kivi.cloud.broker import (
    RESTRICTED_DATA,
    DataClass,
    HandoffPack,
    build_handoff,
    save_handoff,
)
from kivi.cloud.providers import CloudProvider, CloudRegistry, load_cloud_registry
from kivi.kernel.db import Database

Capability = Literal["llm", "image", "video", "embedding"]
CAPABILITIES = frozenset({"llm", "image", "video", "embedding"})
DATA_POLICIES = frozenset({"public_only", "private_allowed"})
DATA_CLASSES = frozenset({"public", *RESTRICTED_DATA})
WATERMARKS = frozenset({"none", "may", "always"})
_STATUS_ORDER = {"ready": 0, "keyless": 1, "needs_key": 2}
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

_CANDIDATE_KEYS = {
    "type",
    "id",
    "name",
    "kind",
    "capabilities",
    "base_url",
    "api_key_env",
    "default_model",
    "free_requests_per_day",
    "free_tokens_per_day",
    "rate_limit_per_min",
    "free_quota_note",
    "data_policy",
    "trains_on_data",
    "watermark",
    "max_size",
    "reset_at",
    "source_url",
    "verified_at",
    "enabled",
    "notes",
}


class ScoutError(RuntimeError):
    """Base error for an unreadable catalog or an invalid scout action."""


class ScoutRegistryError(ScoutError):
    """Raised for an invalid candidate catalog."""


@dataclass(frozen=True)
class CandidateProvider:
    """A free provider candidate; metadata only, never a secret."""

    id: str
    name: str
    capabilities: tuple[str, ...]
    base_url: str
    api_key_env: str = ""
    kind: str = "openai"
    default_model: str = ""
    free_requests_per_day: int | None = None
    free_tokens_per_day: int | None = None
    rate_limit_per_min: int | None = None
    free_quota_note: str = ""
    data_policy: str = "public_only"
    trains_on_data: bool = True
    watermark: str = "none"
    max_size: str = ""
    reset_at: str = "daily"
    source_url: str = ""
    verified_at: str | None = None
    enabled: bool = True
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.id or not self.id.replace("-", "").replace("_", "").isalnum():
            raise ScoutRegistryError(f"invalid candidate id: {self.id!r}")
        if not self.name:
            raise ScoutRegistryError(f"{self.id}: name is required")
        capabilities = tuple(self.capabilities)
        if (
            not capabilities
            or any(not isinstance(item, str) for item in capabilities)
            or set(capabilities) - CAPABILITIES
        ):
            raise ScoutRegistryError(f"{self.id}: invalid capabilities")
        if self.kind != "openai":
            raise ScoutRegistryError(f"{self.id}: unsupported broker kind: {self.kind!r}")
        if not self.base_url.startswith("https://"):
            raise ScoutRegistryError(f"{self.id}: base_url must use https")
        if self.api_key_env and not _ENV_NAME.match(self.api_key_env):
            raise ScoutRegistryError(f"{self.id}: api_key_env must be an upper-case env name")
        if self.data_policy not in DATA_POLICIES:
            raise ScoutRegistryError(f"{self.id}: invalid data_policy")
        if self.watermark not in WATERMARKS:
            raise ScoutRegistryError(f"{self.id}: invalid watermark")
        if self.source_url and not self.source_url.startswith("https://"):
            raise ScoutRegistryError(f"{self.id}: source_url must use https")
        for name, value in (
            ("free_requests_per_day", self.free_requests_per_day),
            ("free_tokens_per_day", self.free_tokens_per_day),
            ("rate_limit_per_min", self.rate_limit_per_min),
        ):
            if value is not None and value < 0:
                raise ScoutRegistryError(f"{self.id}: {name} must be >= 0")
        if self.free_requests_per_day is None and self.free_tokens_per_day is None:
            raise ScoutRegistryError(f"{self.id}: a free request or token quota is required")

    @property
    def quota_label(self) -> str:
        parts: list[str] = []
        if self.free_requests_per_day is not None:
            parts.append(f"{self.free_requests_per_day} req/day")
        if self.free_tokens_per_day is not None:
            parts.append(f"{self.free_tokens_per_day} tokens/day")
        return ", ".join(parts) or self.free_quota_note or "free quota (unknown)"


@dataclass(frozen=True)
class ScoutRegistry:
    candidates: dict[str, CandidateProvider] = field(default_factory=dict)

    def get(self, provider_id: str) -> CandidateProvider | None:
        return self.candidates.get(provider_id)

    def require(self, provider_id: str) -> CandidateProvider:
        candidate = self.get(provider_id)
        if candidate is None:
            raise ScoutRegistryError(f"unknown Scout candidate: {provider_id}")
        return candidate

    def all(self) -> tuple[CandidateProvider, ...]:
        return tuple(self.candidates[key] for key in sorted(self.candidates))


@dataclass(frozen=True)
class ScoutQuery:
    """What the task needs from a free cloud provider."""

    capability: Capability = "llm"
    data_class: DataClass = "public"
    allow_training_data: bool = False
    allow_watermark: bool = True
    include_configured: bool = False
    limit: int = 3

    def __post_init__(self) -> None:
        if self.capability not in CAPABILITIES:
            raise ScoutError(f"unknown capability: {self.capability!r}")
        if self.data_class not in DATA_CLASSES:
            raise ScoutError(f"unknown data class: {self.data_class!r}")
        if self.limit < 1:
            raise ScoutError("limit must be >= 1")


@dataclass(frozen=True)
class ScoutCard:
    """A provider proposal the user can approve or reject."""

    candidate: CandidateProvider
    status: Literal["ready", "needs_key", "keyless"]
    already_configured: bool = False

    @property
    def provider_id(self) -> str:
        return self.candidate.id

    def to_text(self) -> str:
        candidate = self.candidate
        key_state = "present" if self.status == "ready" else "missing"
        auth = (
            f"env {candidate.api_key_env} ({key_state})"
            if candidate.api_key_env
            else "no key required"
        )
        verification = (
            f"verified {candidate.verified_at}"
            if candidate.verified_at
            else "NOT verified recently: re-check terms and limits"
        )
        lines = [
            f"Cloud Scout card: {candidate.name} ({candidate.id})",
            f"  capabilities : {', '.join(candidate.capabilities)}",
            f"  free quota   : {candidate.quota_label}"
            + (f" ({candidate.free_quota_note})" if candidate.free_quota_note else ""),
            f"  reset        : {candidate.reset_at}",
            "  rate limit   : "
            + (
                f"{candidate.rate_limit_per_min}/min"
                if candidate.rate_limit_per_min is not None
                else "not specified"
            ),
            f"  data policy  : {candidate.data_policy}; "
            f"trains on data: {'yes' if candidate.trains_on_data else 'no'}",
            f"  watermark    : {candidate.watermark}",
            f"  max size     : {candidate.max_size or '-'}",
            f"  auth         : {auth}",
            f"  status       : {self.status}",
            f"  check        : {verification}",
        ]
        if candidate.source_url:
            lines.append(f"  source       : {candidate.source_url}")
        if candidate.notes:
            lines.append(f"  notes        : {candidate.notes}")
        if self.already_configured:
            lines.append("  already in config/cloud.yaml; no approval needed")
        else:
            lines.append(f"  approve      : kivi cloud scout --approve {candidate.id}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        candidate = self.candidate
        return {
            "id": candidate.id,
            "name": candidate.name,
            "capabilities": list(candidate.capabilities),
            "free_quota": candidate.quota_label,
            "reset_at": candidate.reset_at,
            "rate_limit_per_min": candidate.rate_limit_per_min,
            "data_policy": candidate.data_policy,
            "trains_on_data": candidate.trains_on_data,
            "watermark": candidate.watermark,
            "max_size": candidate.max_size,
            "auth_env": candidate.api_key_env or None,
            "key_configured": self.status == "ready",
            "status": self.status,
            "verified_at": candidate.verified_at,
            "source_url": candidate.source_url,
            "already_configured": self.already_configured,
        }

    def provider_entry(self) -> dict[str, Any]:
        """Broker registry entry for an approved candidate."""
        candidate = self.candidate
        if not candidate.api_key_env:
            raise ScoutError(f"{candidate.id}: keyless providers need a manual integration")
        notes = "Added by Cloud Scout after user approval; verify limits/training terms."
        if candidate.notes:
            notes = f"{notes} {candidate.notes}"
        return {
            "id": candidate.id,
            "kind": candidate.kind,
            "base_url": candidate.base_url,
            "api_key_env": candidate.api_key_env,
            "default_model": candidate.default_model,
            "free_requests_per_day": candidate.free_requests_per_day,
            "free_tokens_per_day": candidate.free_tokens_per_day,
            "allows_private_data": candidate.data_policy == "private_allowed",
            "notes": notes,
        }


@dataclass(frozen=True)
class ScoutOutcome:
    """Result of a quota-exhaustion scan."""

    cards: tuple[ScoutCard, ...]
    handoff: HandoffPack | None = None
    handoff_id: int | None = None

    @property
    def found(self) -> bool:
        return bool(self.cards)


class CloudScout:
    """Ranks free candidates and keeps a local fallback when none fit."""

    def __init__(
        self,
        db: Database,
        registry: CloudRegistry,
        candidates: ScoutRegistry,
        *,
        events: Any = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.db = db
        self.registry = registry
        self.candidates = candidates
        self.events = events
        self._environ = environ if environ is not None else os.environ

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.events is not None:
            self.events.emit(f"cloud.{name}", payload=payload)

    def _key_configured(self, candidate: CandidateProvider) -> bool:
        if not candidate.api_key_env:
            return True
        return bool(self._environ.get(candidate.api_key_env, "").strip())

    def _reject_reason(self, candidate: CandidateProvider, query: ScoutQuery) -> str | None:
        if not candidate.enabled:
            return "disabled in the catalog"
        if query.capability not in candidate.capabilities:
            return "capability mismatch"
        if candidate.data_policy == "public_only" and query.data_class in RESTRICTED_DATA:
            return "public-only provider cannot take restricted data"
        if candidate.trains_on_data and not query.allow_training_data:
            return "training on data is not allowed"
        if candidate.watermark != "none" and not query.allow_watermark:
            return "watermark required or possible"
        if not query.include_configured and self.registry.get(candidate.id) is not None:
            return "already configured"
        return None

    def recommend(self, query: ScoutQuery | None = None) -> tuple[ScoutCard, ...]:
        """Return matching candidate cards, best first."""
        query = query or ScoutQuery()
        cards: list[ScoutCard] = []
        for candidate in self.candidates.all():
            if self._reject_reason(candidate, query) is not None:
                continue
            if self._key_configured(candidate):
                status = "keyless" if not candidate.api_key_env else "ready"
            else:
                status = "needs_key"
            cards.append(
                ScoutCard(
                    candidate=candidate,
                    status=status,
                    already_configured=self.registry.get(candidate.id) is not None,
                )
            )
        cards.sort(
            key=lambda card: (
                _STATUS_ORDER[card.status],
                card.candidate.trains_on_data,
                -(card.candidate.free_requests_per_day or 0),
                card.candidate.id,
            )
        )
        return tuple(cards[: query.limit])

    def on_exhausted(
        self,
        query: ScoutQuery | None = None,
        *,
        goal: str,
        reason: str = "quota exhausted",
        task_id: str = "",
        plan: list[str] | None = None,
        completed_steps: list[str] | None = None,
        artifacts: list[str] | None = None,
        summary: str = "",
        criteria: list[str] | None = None,
        constraints: list[str] | None = None,
        next_step: str = "",
    ) -> ScoutOutcome:
        """Save a checkpoint, then look for a replacement provider.

        The handoff pack is written first so the task survives even when the
        user rejects every proposed provider and continues locally.
        """
        query = query or ScoutQuery()
        cards = self.recommend(query)
        pack = build_handoff(
            goal,
            reason,
            plan=plan,
            completed_steps=completed_steps,
            artifacts=artifacts,
            summary=summary,
            criteria=criteria,
            constraints=constraints,
            next_step=next_step,
            task_id=task_id,
        )
        handoff_id = save_handoff(self.db, pack)
        if cards:
            ids = [card.provider_id for card in cards]
            self.db.append_audit(
                actor="cloud",
                action="cloud.scout",
                target=",".join(ids),
                decision="allow",
                reason="candidate providers found",
                details={
                    "capability": query.capability,
                    "cards": ids,
                    "handoff_id": handoff_id,
                },
            )
            self._emit(
                "scout_found",
                {
                    "capability": query.capability,
                    "providers": ids,
                    "task_id": task_id,
                    "handoff_id": handoff_id,
                },
            )
            return ScoutOutcome(cards=cards, handoff=pack, handoff_id=handoff_id)

        self.db.append_audit(
            actor="cloud",
            action="cloud.scout",
            target="local",
            decision="handoff",
            reason="no free provider matched",
            details={"capability": query.capability, "handoff_id": handoff_id},
        )
        self._emit(
            "scout_empty",
            {"capability": query.capability, "handoff_id": handoff_id, "task_id": task_id},
        )
        self._emit("handoff", {"handoff_id": handoff_id, "task_id": task_id})
        return ScoutOutcome(cards=(), handoff=pack, handoff_id=handoff_id)

    def approve(self, provider_id: str, registry_path: Path) -> CloudProvider:
        """Append an explicitly confirmed candidate to ``config/cloud.yaml``.

        This is the only write the Scout performs. It stores metadata only; the
        key stays in the environment and no account is created.
        """
        candidate = self.candidates.require(provider_id)
        if self.registry.get(provider_id) is not None:
            raise ScoutError(f"{provider_id} is already configured")
        card = ScoutCard(
            candidate=candidate,
            status="needs_key",
            already_configured=False,
        )
        entry = card.provider_entry()
        path = Path(registry_path)
        original = path.read_text(encoding="utf-8") if path.is_file() else "providers:\n"
        if not original.endswith("\n"):
            original += "\n"
        stamp = datetime.now(UTC).date().isoformat()
        block = textwrap.indent(
            yaml.safe_dump([entry], sort_keys=False, allow_unicode=True),
            "  ",
        )
        addition = f"\n  # Approved by Cloud Scout on {stamp}; verify terms and limits.\n{block}"
        path.write_text(original + addition, encoding="utf-8")
        try:
            registry = load_cloud_registry(path)
        except Exception as exc:  # noqa: BLE001 - restore on any parse failure
            path.write_text(original, encoding="utf-8")
            raise ScoutError(f"approved entry failed validation: {exc}") from exc
        self.db.append_audit(
            actor="cloud",
            action="cloud.scout.approve",
            target=provider_id,
            decision="allow",
            reason="user confirmed provider",
            details={"api_key_env": candidate.api_key_env, "base_url": candidate.base_url},
        )
        self._emit("scout_approved", {"provider": provider_id})
        return registry.require(provider_id)


def _candidate_from_mapping(raw: Any) -> CandidateProvider:
    if not isinstance(raw, dict):
        raise ScoutRegistryError("each candidate must be a mapping")
    unknown = set(raw) - _CANDIDATE_KEYS
    if unknown:
        raise ScoutRegistryError(f"unknown candidate keys: {', '.join(sorted(unknown))}")
    values = dict(raw)
    entry_type = values.pop("type", None)
    raw_capabilities = values.get("capabilities")
    if isinstance(raw_capabilities, str):
        values["capabilities"] = (raw_capabilities,)
    elif isinstance(raw_capabilities, list):
        if not all(isinstance(item, str) for item in raw_capabilities):
            raise ScoutRegistryError("capabilities must be strings")
        values["capabilities"] = tuple(raw_capabilities)
    if "capabilities" not in values:
        if entry_type is not None and not isinstance(entry_type, str):
            raise ScoutRegistryError("candidate type must be a string")
        values["capabilities"] = (entry_type or "llm",)
    try:
        return CandidateProvider(**values)
    except TypeError as exc:
        raise ScoutRegistryError(f"invalid candidate entry: {exc}") from exc


def load_scout_registry(path: Path) -> ScoutRegistry:
    """Load ``config/cloud-candidates.yaml`` with strict validation."""
    if not path.is_file():
        raise ScoutRegistryError(f"candidate catalog not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ScoutRegistryError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or "candidates" not in data:
        raise ScoutRegistryError("catalog must contain a 'candidates' list")
    unknown = set(data) - {"candidates"}
    if unknown:
        raise ScoutRegistryError(f"unknown catalog keys: {', '.join(sorted(unknown))}")
    entries = data["candidates"]
    if not isinstance(entries, list):
        raise ScoutRegistryError("'candidates' must be a list")
    candidates: dict[str, CandidateProvider] = {}
    for raw in entries:
        candidate = _candidate_from_mapping(raw)
        if candidate.id in candidates:
            raise ScoutRegistryError(f"duplicate candidate id: {candidate.id}")
        candidates[candidate.id] = candidate
    if not candidates:
        raise ScoutRegistryError("catalog has no candidates")
    return ScoutRegistry(candidates)


__all__ = [
    "CAPABILITIES",
    "CandidateProvider",
    "CloudScout",
    "ScoutCard",
    "ScoutError",
    "ScoutOutcome",
    "ScoutQuery",
    "ScoutRegistry",
    "ScoutRegistryError",
    "load_scout_registry",
]
