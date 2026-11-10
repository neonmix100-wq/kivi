"""Cloud Broker: policy, quotas, audited calls and local handoff.

Cloud is borrowed capacity, never the default. A call is allowed only when the
mode permits it, the data class is public, a key is present in the environment,
and today's quota plus the cost limit still allow it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from kivi.cloud.providers import CloudProvider, CloudRegistry
from kivi.config import CloudSettings
from kivi.kernel.db import Database

DataClass = Literal["public", "internal", "private", "secret"]
RESTRICTED_DATA = frozenset({"internal", "private", "secret"})


class CloudError(RuntimeError):
    """Base error for a refused or failed cloud call."""


class CloudDisabled(CloudError):
    pass


class ProviderUnavailable(CloudError):
    pass


class DataPolicyError(CloudError):
    pass


class BudgetExceeded(CloudError):
    pass


class QuotaExhausted(CloudError):
    pass


@dataclass(frozen=True)
class CloudResult:
    text: str
    provider: str
    model: str
    tokens: int = 0
    cost_usd: float = 0.0


@dataclass(frozen=True)
class CloudStatus:
    provider: str
    kind: str
    key_configured: bool
    data_class: str
    requests_today: int
    tokens_today: int
    cost_today_usd: float
    remaining_requests: int | None
    remaining_tokens: int | None
    notes: str = ""


@dataclass(frozen=True)
class HandoffPack:
    """Everything a local model needs to continue after a cloud failure."""

    goal: str
    reason: str
    plan: list[str] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    summary: str = ""
    criteria: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    next_step: str = ""
    task_id: str = ""

    def to_prompt(self) -> str:
        lines = [
            "Cloud execution stopped; continue locally with this handoff pack.",
            f"Goal: {self.goal}",
            f"Reason: {self.reason}",
        ]
        if self.summary:
            lines.append(f"Summary: {self.summary}")
        if self.criteria:
            lines.append("Done when:")
            lines.extend(f"- {item}" for item in self.criteria)
        if self.completed_steps:
            lines.append("Completed steps:")
            lines.extend(f"- {step}" for step in self.completed_steps)
        if self.plan:
            lines.append("Plan:")
            lines.extend(f"- {step}" for step in self.plan)
        if self.artifacts:
            lines.append("Artifacts:")
            lines.extend(f"- {path}" for path in self.artifacts)
        if self.constraints:
            lines.append("Constraints:")
            lines.extend(f"- {item}" for item in self.constraints)
        if self.next_step:
            lines.append(f"Next step: {self.next_step}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "reason": self.reason,
            "plan": list(self.plan),
            "completed_steps": list(self.completed_steps),
            "artifacts": list(self.artifacts),
            "summary": self.summary,
            "criteria": list(self.criteria),
            "constraints": list(self.constraints),
            "next_step": self.next_step,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HandoffPack:
        return cls(
            goal=str(data.get("goal") or ""),
            reason=str(data.get("reason") or ""),
            plan=list(data.get("plan") or []),
            completed_steps=list(data.get("completed_steps") or []),
            artifacts=list(data.get("artifacts") or []),
            summary=str(data.get("summary") or ""),
            criteria=list(data.get("criteria") or []),
            constraints=list(data.get("constraints") or []),
            next_step=str(data.get("next_step") or ""),
            task_id=str(data.get("task_id") or ""),
        )


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


class CloudBroker:
    def __init__(
        self,
        db: Database,
        settings: CloudSettings,
        registry: CloudRegistry,
        *,
        events: Any = None,
        transport: httpx.BaseTransport | None = None,
        today: Any = None,
        timeout: float = 90.0,
        killswitch: Any = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.registry = registry
        self.events = events
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._today = today or _today
        self.killswitch = killswitch

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> CloudBroker:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- policy ----------------------------------------------------------
    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.events is not None:
            self.events.emit(f"cloud.{name}", payload=payload)

    def _audit(
        self,
        provider: CloudProvider,
        *,
        model: str,
        decision: str,
        reason: str,
        details: dict[str, Any],
    ) -> None:
        self.db.append_audit(
            actor="cloud",
            action=f"cloud.{provider.id}",
            target=model or provider.default_model,
            decision=decision,
            reason=reason,
            details=details,
        )

    def _check_allowed(
        self,
        provider: CloudProvider,
        *,
        data_class: DataClass,
        estimated_cost_usd: float,
        model: str,
    ) -> None:
        if self.settings.mode == "off":
            raise CloudDisabled("cloud mode is off")
        if data_class not in RESTRICTED_DATA and data_class != "public":
            raise DataPolicyError(f"unknown data class: {data_class}")
        if data_class in RESTRICTED_DATA and not provider.allows_private_data:
            self._audit(
                provider,
                model=model,
                decision="deny",
                reason="data policy",
                details={"data_class": data_class},
            )
            raise DataPolicyError(
                f"{provider.id} does not accept {data_class} data; use a local model"
            )
        if estimated_cost_usd > self.settings.cost_limit_usd + 1e-9:
            self._audit(
                provider,
                model=model,
                decision="deny",
                reason="cost limit",
                details={
                    "estimated_cost_usd": estimated_cost_usd,
                    "limit_usd": self.settings.cost_limit_usd,
                },
            )
            raise BudgetExceeded(
                f"estimated ${estimated_cost_usd:.4f} exceeds the "
                f"${self.settings.cost_limit_usd:.2f} cloud limit"
            )

    def _key(self, provider: CloudProvider) -> str:
        key = os.environ.get(provider.api_key_env, "").strip()
        if not key:
            raise ProviderUnavailable(
                f"{provider.id}: set {provider.api_key_env} to enable this provider"
            )
        return key

    def _check_quota(
        self,
        provider: CloudProvider,
        usage: dict[str, Any],
        *,
        model: str,
    ) -> None:
        requests = int(usage["requests"])
        tokens = int(usage["tokens"])
        if (
            provider.free_requests_per_day is not None
            and requests >= provider.free_requests_per_day
        ):
            self._emit(
                "quota_exhausted",
                {"provider": provider.id, "kind": "requests", "used": requests},
            )
            self._audit(
                provider,
                model=model,
                decision="deny",
                reason="quota exhausted",
                details={"requests": requests, "limit": provider.free_requests_per_day},
            )
            raise QuotaExhausted(
                f"{provider.id}: daily request quota exhausted "
                f"({requests}/{provider.free_requests_per_day})"
            )
        if provider.free_tokens_per_day is not None and tokens >= provider.free_tokens_per_day:
            self._emit(
                "quota_exhausted",
                {"provider": provider.id, "kind": "tokens", "used": tokens},
            )
            self._audit(
                provider,
                model=model,
                decision="deny",
                reason="token quota exhausted",
                details={"tokens": tokens, "limit": provider.free_tokens_per_day},
            )
            raise QuotaExhausted(
                f"{provider.id}: daily token quota exhausted "
                f"({tokens}/{provider.free_tokens_per_day})"
            )

    # -- calls -----------------------------------------------------------
    def complete(
        self,
        provider_id: str,
        messages: list[dict[str, str]],
        *,
        model: str = "",
        data_class: DataClass = "private",
        estimated_cost_usd: float = 0.0,
        max_tokens: int = 512,
        task_id: str = "",
    ) -> CloudResult:
        provider = self.registry.require(provider_id)
        chosen_model = model or provider.default_model
        if self.killswitch is not None and self.killswitch.is_engaged():
            state = self.killswitch.state()
            self._audit(
                provider,
                model=chosen_model,
                decision="deny",
                reason="kill switch engaged",
                details={"reason": state.reason},
            )
            raise CloudDisabled(
                f"kill switch engaged: {state.reason or 'stopped by user'}"
            )
        self._check_allowed(
            provider,
            data_class=data_class,
            estimated_cost_usd=estimated_cost_usd,
            model=chosen_model,
        )
        key = self._key(provider)
        day = self._today()
        usage = self.db.cloud_usage(provider.id, day)
        self._check_quota(provider, usage, model=chosen_model)
        if not chosen_model:
            raise CloudError(f"{provider.id}: no model configured")

        body = {
            "model": chosen_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._client.post(provider.endpoint, json=body, headers=headers)
        except httpx.HTTPError as exc:
            self._emit(
                "error",
                {"provider": provider.id, "error": type(exc).__name__},
            )
            raise CloudError(f"{provider.id}: network error") from exc

        if response.status_code == 429:
            self.db.add_cloud_usage(provider.id, day, requests=1)
            self._emit("quota_exhausted", {"provider": provider.id, "kind": "http_429"})
            self._audit(
                provider,
                model=chosen_model,
                decision="deny",
                reason="http 429",
                details={"status": 429},
            )
            raise QuotaExhausted(f"{provider.id}: provider returned 429")

        if response.status_code >= 400:
            self._audit(
                provider,
                model=chosen_model,
                decision="deny",
                reason=f"http {response.status_code}",
                details={"status": response.status_code},
            )
            raise CloudError(f"{provider.id}: HTTP {response.status_code}")

        try:
            payload = response.json()
            text = payload["choices"][0]["message"]["content"]
            tokens = int(payload.get("usage", {}).get("total_tokens", 0) or 0)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            self._audit(
                provider,
                model=chosen_model,
                decision="deny",
                reason="invalid response",
                details={},
            )
            raise CloudError(f"{provider.id}: invalid response") from exc
        if not isinstance(text, str):
            raise CloudError(f"{provider.id}: response has no text")

        self.db.add_cloud_usage(
            provider.id,
            day,
            requests=1,
            tokens=tokens,
            cost_usd=estimated_cost_usd,
        )
        self._audit(
            provider,
            model=chosen_model,
            decision="allow",
            reason="cloud call",
            details={
                "data_class": data_class,
                "tokens": tokens,
                "cost_usd": estimated_cost_usd,
            },
        )
        self._emit(
            "call",
            {
                "provider": provider.id,
                "model": chosen_model,
                "tokens": tokens,
                "task_id": task_id,
            },
        )
        return CloudResult(
            text=text,
            provider=provider.id,
            model=chosen_model,
            tokens=tokens,
            cost_usd=estimated_cost_usd,
        )

    # -- diagnostics -----------------------------------------------------
    def status(self) -> list[CloudStatus]:
        day = self._today()
        result: list[CloudStatus] = []
        for provider in self.registry.all():
            usage = self.db.cloud_usage(provider.id, day)
            requests = int(usage["requests"])
            tokens = int(usage["tokens"])
            remaining_requests = (
                None
                if provider.free_requests_per_day is None
                else max(0, provider.free_requests_per_day - requests)
            )
            remaining_tokens = (
                None
                if provider.free_tokens_per_day is None
                else max(0, provider.free_tokens_per_day - tokens)
            )
            result.append(
                CloudStatus(
                    provider=provider.id,
                    kind=provider.kind,
                    key_configured=bool(os.environ.get(provider.api_key_env)),
                    data_class=(
                        "public+restricted" if provider.allows_private_data else "public only"
                    ),
                    requests_today=requests,
                    tokens_today=tokens,
                    cost_today_usd=float(usage["cost_usd"]),
                    remaining_requests=remaining_requests,
                    remaining_tokens=remaining_tokens,
                    notes=provider.notes,
                )
            )
        return result


def build_handoff(
    goal: str,
    reason: str,
    *,
    plan: list[str] | None = None,
    completed_steps: list[str] | None = None,
    artifacts: list[str] | None = None,
    summary: str = "",
    criteria: list[str] | None = None,
    constraints: list[str] | None = None,
    next_step: str = "",
    task_id: str = "",
) -> HandoffPack:
    return HandoffPack(
        goal=goal,
        reason=reason,
        plan=list(plan or []),
        completed_steps=list(completed_steps or []),
        artifacts=list(artifacts or []),
        summary=summary,
        criteria=list(criteria or []),
        constraints=list(constraints or []),
        next_step=next_step,
        task_id=task_id,
    )


def save_handoff(db: Database, pack: HandoffPack, *, task_id: str = "") -> int:
    """Persist a handoff pack and return its checkpoint id."""
    return db.save_handoff(
        goal=pack.goal,
        reason=pack.reason,
        pack=pack.as_dict(),
        task_id=task_id or pack.task_id,
    )


def load_handoff(db: Database, handoff_id: int) -> HandoffPack | None:
    """Load a persisted handoff pack, or ``None`` when the id is unknown."""
    row = db.get_handoff(handoff_id)
    if row is None:
        return None
    return HandoffPack.from_dict(row["pack"])


__all__ = [
    "BudgetExceeded",
    "CloudBroker",
    "CloudDisabled",
    "CloudError",
    "CloudResult",
    "CloudStatus",
    "DataPolicyError",
    "HandoffPack",
    "ProviderUnavailable",
    "QuotaExhausted",
    "build_handoff",
    "load_handoff",
    "save_handoff",
]
