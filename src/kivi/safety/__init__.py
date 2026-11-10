"""Safety layer: policy, protected zones, approvals and trust labels."""

from kivi.safety.elevated import (
    ALLOWED_OPERATIONS,
    ElevatedBroker,
    ElevatedError,
)
from kivi.safety.hotkey import (
    HotkeyError,
    HotkeyListener,
    HotkeySpec,
    HotkeyUnavailable,
    parse_hotkey,
)
from kivi.safety.killswitch import KillSwitch, KillSwitchState
from kivi.safety.permissions import (
    Decision,
    Grant,
    PermissionEngine,
    PermissionRequest,
)
from kivi.safety.policy import PolicyEngine, PolicyError
from kivi.safety.sanitize import Sanitized, redact_secrets, sanitize_text
from kivi.safety.trust import (
    TRUST_LEVELS,
    TrustEnvelope,
    TrustError,
    TrustLevel,
    build_envelope,
    can_issue_instructions,
    classify_source,
)
from kivi.safety.zones import (
    ProtectedZones,
    ZoneDecision,
    ZoneError,
    ZoneRule,
)

__all__ = [
    "ALLOWED_OPERATIONS",
    "Decision",
    "ElevatedBroker",
    "ElevatedError",
    "Grant",
    "HotkeyError",
    "HotkeyListener",
    "HotkeySpec",
    "HotkeyUnavailable",
    "KillSwitch",
    "KillSwitchState",
    "PermissionEngine",
    "PermissionRequest",
    "PolicyEngine",
    "PolicyError",
    "ProtectedZones",
    "Sanitized",
    "TRUST_LEVELS",
    "TrustEnvelope",
    "TrustError",
    "TrustLevel",
    "ZoneDecision",
    "ZoneError",
    "ZoneRule",
    "build_envelope",
    "can_issue_instructions",
    "classify_source",
    "parse_hotkey",
    "redact_secrets",
    "sanitize_text",
]
