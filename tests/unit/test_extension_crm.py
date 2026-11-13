from __future__ import annotations

from pathlib import Path

import pytest

from kivi.integrations import extension_crm
from kivi.integrations.extension_crm import ExtensionCrmClient, ExtensionCrmError


class FakeBridge:
    def __init__(self, *args, **kwargs) -> None:
        self.stopped = False

    def start(self) -> None:
        pass

    def wait_connected(self, timeout: float) -> bool:
        return True

    def request(self, action, payload, timeout=30):
        if action == "read_order":
            return {
                "url": "https://gs-leads.ru/devices/order/1",
                "order_code": "202610-1",
                "device_id": "1513806",
                "text": "Принтер Elegoo",
                "fields": {"Логист": "плата 15000"},
            }
        if action == "fill_order":
            return [{"field": "deadline", "ok": True}]
        raise AssertionError(action)

    def stop(self) -> None:
        self.stopped = True


def make_client(tmp_path: Path, monkeypatch) -> tuple[ExtensionCrmClient, FakeBridge]:
    token = tmp_path / "token.txt"
    token.write_text("secret", encoding="utf-8")
    selectors = tmp_path / "selectors.toml"
    selectors.write_text("[order]\ntitle_regex='x'\n", encoding="utf-8")
    bridges: list[FakeBridge] = []

    def factory(*args, **kwargs):
        bridge = FakeBridge()
        bridges.append(bridge)
        return bridge

    monkeypatch.setattr(extension_crm, "ExtensionBridge", factory)
    client = ExtensionCrmClient(token_file=token, selectors_path=selectors)
    client._ensure()
    return client, bridges[0]


def test_extension_read_order_maps_to_order_info(tmp_path: Path, monkeypatch) -> None:
    client, bridge = make_client(tmp_path, monkeypatch)
    info = client.read_order()
    assert info.device_id == "1513806"
    assert info.order_code == "202610-1"
    assert info.fields["Логист"] == "плата 15000"
    client.close()
    assert bridge.stopped is False


def test_extension_fill_results(tmp_path: Path, monkeypatch) -> None:
    client, _ = make_client(tmp_path, monkeypatch)
    results = client.fill_order("комментарий", {"deadline": "завтра"})
    assert results[0].field == "deadline"
    assert results[0].ok is True


def test_extension_rejects_missing_token(tmp_path: Path) -> None:
    client = ExtensionCrmClient(
        token_file=tmp_path / "missing.txt",
        selectors_path=tmp_path / "selectors.toml",
    )
    with pytest.raises(ExtensionCrmError):
        client._ensure()
