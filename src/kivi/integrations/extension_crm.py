"""Client for the existing AI-agent browser extension.

The extension opens a WebSocket to a loopback bridge and exposes read-only
``read_order`` plus fill/task actions.  Kivi uses the read-only path for its CRM
panel; no save/delete/send action is exposed by the extension.
"""

from __future__ import annotations

import threading
import tomllib
from pathlib import Path
from typing import Any

from kivi.integrations.extension_bridge import BridgeError, ExtensionBridge
from kivi.modules.work_crm.altcrm import AltCrmError, FieldResult, OrderInfo

DEFAULT_TOKEN_FILE = Path("D:/ai-agent/data/bridge_token.txt")
DEFAULT_SELECTORS = Path("config/crm-selectors.toml")
DEFAULT_PORT = 8799


class ExtensionCrmError(AltCrmError):
    """Raised when the extension bridge or AltCRM tab is unavailable."""


_SHARED_BRIDGES: dict[tuple[str, int], ExtensionBridge] = {}
_SHARED_LOCK = threading.Lock()


def _token(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ExtensionCrmError(f"cannot read extension token: {path}") from exc
    if not token:
        raise ExtensionCrmError(f"extension token is empty: {path}")
    return token


def _selectors(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ExtensionCrmError(f"cannot read CRM selectors: {path}") from exc


class ExtensionCrmClient:
    """Synchronous adapter matching Kivi's CRM client protocol."""

    def __init__(
        self,
        *,
        token_file: Path | str = DEFAULT_TOKEN_FILE,
        selectors_path: Path | str = DEFAULT_SELECTORS,
        port: int = DEFAULT_PORT,
    ) -> None:
        self.token_file = Path(token_file)
        self.selectors_path = Path(selectors_path)
        self.port = port
        self._bridge: ExtensionBridge | None = None
        self._selectors: dict[str, Any] | None = None

    def _ensure(self) -> ExtensionBridge:
        if self._bridge is not None:
            return self._bridge
        key = (str(self.token_file.resolve()), self.port)
        with _SHARED_LOCK:
            bridge = _SHARED_BRIDGES.get(key)
            if bridge is None or not bridge.connected:
                if bridge is not None:
                    bridge.stop()
                bridge = ExtensionBridge(_token(self.token_file), "127.0.0.1", self.port)
                try:
                    bridge.start()
                except Exception as exc:  # noqa: BLE001 - bridge boundary
                    raise ExtensionCrmError(str(exc)) from exc
                _SHARED_BRIDGES[key] = bridge
                self._selectors = _selectors(self.selectors_path)
                if not bridge.wait_connected(5):
                    bridge.stop()
                    _SHARED_BRIDGES.pop(key, None)
                    raise ExtensionCrmError(
                        "Расширение браузера не подключилось за 5 с. Проверьте, что "
                        "оно включено, на значке написано ON и открыта вкладка AltCRM."
                    )
            self._bridge = bridge
            if self._selectors is None:
                self._selectors = _selectors(self.selectors_path)
        return self._bridge

    def _payload(self) -> dict[str, Any]:
        assert self._selectors is not None
        return {"sel": self._selectors, "click_edit": False}

    def read_order(self) -> OrderInfo:
        bridge = self._ensure()
        try:
            data = bridge.request("read_order", self._payload(), timeout=20)
        except BridgeError as exc:
            raise ExtensionCrmError(str(exc)) from exc
        if not isinstance(data, dict):
            raise ExtensionCrmError("extension returned no order data")
        return OrderInfo(
            url=str(data.get("url", "")),
            order_code=str(data.get("order_code", "")),
            device_id=str(data.get("device_id", "")),
            text=str(data.get("text", "")),
            fields={
                str(key): str(value)
                for key, value in (data.get("fields") or {}).items()
                if isinstance(key, str)
            },
        )

    def fill_order(self, comment: str, checklist: dict[str, str]) -> list[FieldResult]:
        bridge = self._ensure()
        payload = {**self._payload(), "comment": comment, "checklist": dict(checklist)}
        try:
            data = bridge.request("fill_order", payload, timeout=60)
        except BridgeError as exc:
            raise ExtensionCrmError(str(exc)) from exc
        return _results(data)

    def add_work_rows(self, rows: list[dict[str, Any]]) -> list[FieldResult]:
        bridge = self._ensure()
        payload = {**self._payload(), "rows": rows}
        try:
            data = bridge.request("add_work_rows", payload, timeout=60)
        except BridgeError as exc:
            raise ExtensionCrmError(str(exc)) from exc
        return _results(data)

    def create_task(self, comment: str, when_text: str = "") -> list[FieldResult]:
        bridge = self._ensure()
        payload = {**self._payload(), "comment": comment, "when_text": when_text}
        try:
            data = bridge.request("create_task", payload, timeout=60)
        except BridgeError as exc:
            raise ExtensionCrmError(str(exc)) from exc
        return _results(data)

    def close(self) -> None:
        # The bridge is process-shared; stopping it here would break the next
        # status/read call in the same UI process. The daemon thread exits with
        # the process.
        self._bridge = None


def _results(data: Any) -> list[FieldResult]:
    if not isinstance(data, list):
        return []
    results: list[FieldResult] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        results.append(
            FieldResult(
                field=str(item.get("field", "")),
                ok=bool(item.get("ok")),
                error=str(item.get("error", "")),
                note=str(item.get("note", "")),
                strategy=str(item.get("strategy", "")),
            )
        )
    return results


__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_SELECTORS",
    "DEFAULT_TOKEN_FILE",
    "ExtensionCrmClient",
    "ExtensionCrmError",
]
