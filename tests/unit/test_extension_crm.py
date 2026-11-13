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
        self.last = (action, payload)
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
        if action == "add_work_rows":
            return [{"field": "работа", "ok": True}]
        if action == "create_task":
            return [{"field": "окно задачи", "ok": True}]
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


def test_extension_act_and_task_payloads(tmp_path: Path, monkeypatch) -> None:
    client, bridge = make_client(tmp_path, monkeypatch)
    client.add_work_rows([{"name": "Пайка", "price": 1500, "warranty": 30}])
    assert bridge.last == (
        "add_work_rows",
        {
            "sel": bridge.last[1]["sel"],
            "click_edit": True,
            "rows": [{"name": "Пайка", "price_rub": 1500, "warranty_days": 30}],
        },
    )
    client.create_task("Позвонить", "завтра")
    action, payload = bridge.last
    assert action == "create_task"
    assert payload["click_edit"] is True
    assert payload["due_text"]
    assert payload["due_error"] == ""
    client.create_task("Позвонить", "непонятно когда")
    _, payload = bridge.last
    assert payload["due_text"] == ""
    assert "не понял срок" in payload["due_error"]


def test_extension_rejects_missing_token(tmp_path: Path) -> None:
    client = ExtensionCrmClient(
        token_file=tmp_path / "missing.txt",
        selectors_path=tmp_path / "selectors.toml",
    )
    with pytest.raises(ExtensionCrmError):
        client._ensure()
