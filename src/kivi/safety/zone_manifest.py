"""Signed Protected Zones manifest.

The built-in zone list is code-owned and always active. A user may add zones in
``config/protected-zones.yaml``, but only if the file matches an HMAC-SHA256
signature created with a key from ``private/safety/zones.key``. A missing key or
a bad signature fails closed: the application refuses to load the extra zones
rather than silently running without them.

Signatures are over canonical JSON (sorted keys, compact separators), so
reformatting the YAML does not invalidate them, while any semantic edit does.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from kivi.safety.policy import normalize_windows_path
from kivi.safety.zones import ZoneRule

MANIFEST_VERSION = 1
_KEY_BYTES = 32
_ROOT_KEYS = {"version", "zones"}
_ZONE_KEYS = {"id", "path", "access", "reason"}
_ACCESS = frozenset({"read", "write", "exec"})


class ZoneManifestError(ValueError):
    """Raised when the manifest is missing, invalid or badly signed."""


@dataclass(frozen=True, slots=True)
class ManifestStatus:
    path: Path
    exists: bool
    signed: bool
    zones: int
    reason: str = ""


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ZoneManifestError(f"manifest not found: {path}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise ZoneManifestError(f"cannot read manifest: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ZoneManifestError("manifest root must be a mapping")
    return loaded


def validate_manifest(data: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Validate the schema and return normalised zone entries."""
    unknown = set(data) - _ROOT_KEYS
    if unknown:
        raise ZoneManifestError(f"unknown manifest keys: {', '.join(sorted(unknown))}")
    if data.get("version") != MANIFEST_VERSION:
        raise ZoneManifestError(f"manifest version must be {MANIFEST_VERSION}")
    zones = data.get("zones", [])
    if not isinstance(zones, list):
        raise ZoneManifestError("'zones' must be a list")

    seen: set[str] = set()
    normalised: list[dict[str, Any]] = []
    for raw in zones:
        if not isinstance(raw, dict):
            raise ZoneManifestError("each zone must be a mapping")
        extra = set(raw) - _ZONE_KEYS
        if extra:
            raise ZoneManifestError(f"unknown zone keys: {', '.join(sorted(extra))}")
        zone_id = raw.get("id")
        if not isinstance(zone_id, str) or not zone_id:
            raise ZoneManifestError("zone id is required")
        if not zone_id.replace("-", "").replace("_", "").isalnum():
            raise ZoneManifestError(f"invalid zone id: {zone_id!r}")
        if zone_id in seen:
            raise ZoneManifestError(f"duplicate zone id: {zone_id}")
        seen.add(zone_id)

        path = raw.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ZoneManifestError(f"{zone_id}: path is required")
        if not Path(path).is_absolute():
            raise ZoneManifestError(f"{zone_id}: path must be absolute")
        try:
            canonical_path = normalize_windows_path(path)
        except ValueError as exc:
            raise ZoneManifestError(f"{zone_id}: {exc}") from exc

        access = raw.get("access")
        if (
            not isinstance(access, list)
            or not access
            or any(not isinstance(item, str) or item not in _ACCESS for item in access)
        ):
            raise ZoneManifestError(f"{zone_id}: access must be a subset of {sorted(_ACCESS)}")

        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ZoneManifestError(f"{zone_id}: reason is required")

        normalised.append(
            {
                "id": zone_id,
                "path": canonical_path,
                "access": sorted(set(access)),
                "reason": reason.strip(),
            }
        )
    return normalised, MANIFEST_VERSION


def manifest_payload(zones: list[dict[str, Any]]) -> dict[str, Any]:
    return {"version": MANIFEST_VERSION, "zones": zones}


def load_key(path: Path) -> bytes:
    """Read a 32-byte hex HMAC key; a missing or malformed key is fatal."""
    try:
        text = path.read_text(encoding="ascii").strip()
    except FileNotFoundError as exc:
        raise ZoneManifestError(f"zone signing key not found: {path}") from exc
    except OSError as exc:
        raise ZoneManifestError(f"cannot read zone signing key: {exc}") from exc
    try:
        key = bytes.fromhex(text)
    except ValueError as exc:
        raise ZoneManifestError("zone signing key must be hex") from exc
    if len(key) != _KEY_BYTES:
        raise ZoneManifestError(f"zone signing key must be {_KEY_BYTES} bytes")
    return key


def generate_key(path: Path) -> bytes:
    """Create the key once; existing key material is never overwritten."""
    if path.is_file():
        return load_key(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(_KEY_BYTES)
    path.write_text(key.hex(), encoding="ascii")
    return key


def sign_manifest(manifest_path: Path, key_path: Path) -> Path:
    """Validate the manifest, sign its canonical form and write ``*.sig``."""
    data = _read_yaml(manifest_path)
    zones, _ = validate_manifest(data)
    key = load_key(key_path)
    signature = hmac.new(key, _canonical(manifest_payload(zones)), hashlib.sha256).hexdigest()
    signature_path = manifest_path.with_suffix(".sig")
    signature_path.write_text(signature + "\n", encoding="ascii")
    return signature_path


def load_signed_rules(manifest_path: Path, key_path: Path) -> tuple[ZoneRule, ...]:
    """Verify the signature and convert manifest entries into extra zone rules.

    A missing manifest means "no extra zones". Once a manifest exists, a missing
    signature or key, a malformed file or a mismatch all raise; callers must
    fail closed rather than continue with a possibly edited list.
    """
    if not manifest_path.is_file():
        return ()
    data = _read_yaml(manifest_path)
    zones, _ = validate_manifest(data)
    signature_path = manifest_path.with_suffix(".sig")
    if not signature_path.is_file():
        raise ZoneManifestError(f"manifest signature not found: {signature_path}")
    key = load_key(key_path)
    expected = hmac.new(key, _canonical(manifest_payload(zones)), hashlib.sha256).hexdigest()
    try:
        actual = signature_path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise ZoneManifestError(f"cannot read manifest signature: {exc}") from exc
    if not hmac.compare_digest(expected, actual):
        raise ZoneManifestError("manifest signature does not match; refusing to load")
    rules: list[ZoneRule] = []
    for zone in zones:
        rules.append(
            ZoneRule(
                id=f"manifest-{zone['id']}",
                reason=zone["reason"],
                access=frozenset(zone["access"]),
                prefix=zone["path"],
            )
        )
    return tuple(rules)


def status(manifest_path: Path, key_path: Path) -> ManifestStatus:
    """Describe the manifest without raising for the CLI status command."""
    if not manifest_path.is_file():
        return ManifestStatus(manifest_path, exists=False, signed=False, zones=0)
    try:
        data = _read_yaml(manifest_path)
        zones, _ = validate_manifest(data)
        rules = load_signed_rules(manifest_path, key_path)
    except ZoneManifestError as exc:
        return ManifestStatus(manifest_path, exists=True, signed=False, zones=0, reason=str(exc))
    return ManifestStatus(
        manifest_path,
        exists=True,
        signed=True,
        zones=len(rules),
        reason=f"key: {key_path}",
    )


__all__ = [
    "MANIFEST_VERSION",
    "ManifestStatus",
    "ZoneManifestError",
    "generate_key",
    "load_key",
    "load_signed_rules",
    "manifest_payload",
    "sign_manifest",
    "status",
    "validate_manifest",
]
