from __future__ import annotations

import http.client
import json
import webbrowser
from pathlib import Path

import pytest

from kivi.interfaces.avatar3d import (
    BLEND_SHAPE_NAMES,
    Avatar3DController,
    Avatar3DError,
    Avatar3DRenderer,
    Avatar3DState,
    ThreeVrmRenderer,
    Viseme,
    VisemeMapper,
    VrmStatus,
    find_vrm,
    vrm_status,
)
from kivi.paths import project_root


def make_web_root(root: Path, *, with_vrm: bool = False) -> Path:
    web = root / "web" / "avatar3d"
    web.mkdir(parents=True)
    (web / "index.html").write_text(
        "<!doctype html><html><body><canvas id=\"stage\"></canvas>"
        "<script type=\"module\" src=\"./app.js\"></script></body></html>",
        encoding="utf-8",
    )
    (web / "app.js").write_text("export {};\n", encoding="utf-8")
    if with_vrm:
        asset = root / "assets" / "avatar" / "kivi.vrm"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"FAKE-VRM")
    return web


def http_get(port: int, path: str) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def get_state_json(port: int) -> dict[str, object]:
    status, body = http_get(port, "/state.json?t=1")
    assert status == 200
    return json.loads(body)


def test_states_and_visemes_cover_the_contract() -> None:
    assert {state.value for state in Avatar3DState} == {
        "idle",
        "listening",
        "thinking",
        "speaking",
        "happy",
        "error",
    }
    assert {viseme.value for viseme in Viseme} == {"A", "I", "U", "E", "O", "CLOSED"}


def test_mapper_silence_closes_the_mouth() -> None:
    mapper = VisemeMapper()
    shapes = mapper.blend_shapes(0.0)
    assert mapper.viseme is Viseme.CLOSED
    assert mapper.level == 0.0
    assert shapes == {name: 0.0 for name in BLEND_SHAPE_NAMES}


def test_mapper_clamps_out_of_range_amplitude() -> None:
    loud = VisemeMapper(attack=1.0).blend_shapes(5.0, "aa")
    assert loud["A"] == 1.0
    infinite = VisemeMapper(attack=1.0).blend_shapes(float("inf"), "aa")
    assert infinite["A"] == 1.0
    quiet = VisemeMapper().blend_shapes(-2.0)
    assert all(value == 0.0 for value in quiet.values())
    nan = VisemeMapper().blend_shapes(float("nan"))
    assert all(value == 0.0 for value in nan.values())


def test_mapper_uses_amplitude_tiers_without_a_hint() -> None:
    quiet = VisemeMapper()
    quiet.blend_shapes(0.2)
    assert quiet.viseme is Viseme.U

    medium = VisemeMapper()
    medium.blend_shapes(0.5)
    assert medium.viseme is Viseme.E

    loud = VisemeMapper()
    loud.blend_shapes(0.9)
    assert loud.viseme is Viseme.A


@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        ("aa", Viseme.A),
        ("а", Viseme.A),
        ("ih", Viseme.I),
        ("и", Viseme.I),
        ("ou", Viseme.U),
        ("у", Viseme.U),
        ("ee", Viseme.E),
        ("э", Viseme.E),
        ("oh", Viseme.O),
        ("о", Viseme.O),
    ],
)
def test_mapper_hint_overrides_the_amplitude_tier(hint: str, expected: Viseme) -> None:
    mapper = VisemeMapper()
    shapes = mapper.blend_shapes(0.8, hint)
    assert mapper.viseme is expected
    assert shapes[expected.value] > 0.0
    assert sum(1 for value in shapes.values() if value > 0.0) == 1


def test_mapper_consonant_hint_keeps_the_mouth_closed() -> None:
    mapper = VisemeMapper()
    shapes = mapper.blend_shapes(0.9, "м")
    assert mapper.viseme is Viseme.CLOSED
    assert all(value == 0.0 for value in shapes.values())


def test_mapper_smooths_attack_and_decay() -> None:
    mapper = VisemeMapper(attack=0.5, decay=0.5)
    assert mapper.blend_shapes(1.0)["A"] == pytest.approx(0.5)
    assert mapper.blend_shapes(1.0)["A"] == pytest.approx(0.75)
    assert mapper.blend_shapes(0.0)["A"] == pytest.approx(0.375)
    for _ in range(20):
        shapes = mapper.blend_shapes(0.0)
    assert mapper.viseme is Viseme.CLOSED
    assert all(value == 0.0 for value in shapes.values())


def test_mapper_has_at_most_one_dominant_shape() -> None:
    mapper = VisemeMapper()
    for amplitude in (0.1, 0.34, 0.35, 0.69, 0.7, 1.0):
        shapes = mapper.blend_shapes(amplitude)
        assert all(0.0 <= value <= 1.0 for value in shapes.values())
        nonzero = [name for name, value in shapes.items() if value > 0.0]
        assert len(nonzero) <= 1


def test_mapper_reset_closes_the_mouth() -> None:
    mapper = VisemeMapper()
    mapper.blend_shapes(0.9, "aa")
    assert mapper.level > 0.0
    mapper.reset()
    assert mapper.viseme is Viseme.CLOSED
    assert mapper.level == 0.0
    assert all(value == 0.0 for value in mapper.blendshapes.values())


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"attack": 0.0}, "attack"),
        ({"attack": 1.5}, "attack"),
        ({"decay": 0.0}, "decay"),
        ({"silence_threshold": -0.1}, "silence_threshold"),
        ({"silence_threshold": 1.0}, "silence_threshold"),
    ],
)
def test_mapper_rejects_bad_tuning(kwargs: dict[str, float], message: str) -> None:
    with pytest.raises(Avatar3DError, match=message):
        VisemeMapper(**kwargs)


def test_controller_starts_idle_and_allows_any_transition() -> None:
    controller = Avatar3DController()
    assert controller.state is Avatar3DState.IDLE
    for state in Avatar3DState:
        assert controller.set_state(state) is state
        assert controller.state is state


def test_controller_rejects_unknown_state() -> None:
    with pytest.raises(Avatar3DError, match="unknown avatar state"):
        Avatar3DController().set_state("sleep")  # type: ignore[arg-type]


def test_controller_error_is_explicit_and_recoverable() -> None:
    controller = Avatar3DController()
    controller.set_state("listening")
    controller.on_speech_start()
    assert controller.state is Avatar3DState.SPEAKING
    controller.set_state("error")
    assert controller.state is Avatar3DState.ERROR
    controller.on_speech_start()
    assert controller.state is Avatar3DState.SPEAKING


def test_controller_speech_lifecycle_updates_the_snapshot() -> None:
    controller = Avatar3DController()
    assert controller.snapshot() == {
        "state": Avatar3DState.IDLE,
        "viseme": Viseme.CLOSED,
        "blendshapes": {name: 0.0 for name in BLEND_SHAPE_NAMES},
    }

    controller.on_speech_start()
    shapes = controller.update_amplitude(0.9, "aa")
    assert controller.state is Avatar3DState.SPEAKING
    assert controller.viseme is Viseme.A
    assert shapes["A"] > 0.0
    snapshot = controller.snapshot()
    assert snapshot["state"] is Avatar3DState.SPEAKING
    assert snapshot["viseme"] is Viseme.A
    assert snapshot["blendshapes"] == shapes

    controller.on_speech_end()
    assert controller.state is Avatar3DState.IDLE
    assert controller.viseme is Viseme.CLOSED
    assert all(value == 0.0 for value in controller.blendshapes.values())


def test_controller_emits_speech_and_lip_sync_events() -> None:
    events: list[dict[str, object]] = []
    controller = Avatar3DController(on_event=events.append)
    controller.on_speech_start()
    controller.update_amplitude(0.8, "oh")
    controller.on_speech_end()
    assert [event["type"] for event in events] == ["speech_start", "lip_sync", "speech_end"]
    assert events[1]["viseme"] is Viseme.O


def test_controller_state_change_event_only_when_changed() -> None:
    events: list[dict[str, object]] = []
    controller = Avatar3DController(on_event=events.append)
    controller.set_state(Avatar3DState.IDLE)
    assert events == []
    controller.set_state(Avatar3DState.LISTENING)
    assert len(events) == 1
    assert events[0]["type"] == "state_changed"
    assert events[0]["state"] is Avatar3DState.LISTENING
    assert events[0]["previous"] is Avatar3DState.IDLE


def test_controller_update_amplitude_does_not_change_state() -> None:
    controller = Avatar3DController()
    controller.set_state("listening")
    controller.update_amplitude(1.0, "aa")
    assert controller.state is Avatar3DState.LISTENING


def test_controller_snapshot_is_json_serializable_and_has_expected_keys() -> None:
    snapshot = Avatar3DController().snapshot()
    assert set(snapshot) == {"state", "viseme", "blendshapes"}
    encoded = json.dumps(snapshot)
    assert '"idle"' in encoded
    assert '"CLOSED"' in encoded


def test_find_vrm_missing_and_status_falls_back(tmp_path: Path) -> None:
    assert find_vrm(tmp_path) is None
    status = vrm_status(tmp_path)
    assert isinstance(status, VrmStatus)
    assert status.available is False
    assert status.path is None
    assert "procedural fallback" in status.message


def test_find_vrm_found_and_status_metadata(tmp_path: Path) -> None:
    asset = tmp_path / "assets" / "avatar" / "kivi.vrm"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"FAKE-VRM")

    found = find_vrm(tmp_path)
    assert found == asset.resolve()
    status = vrm_status(tmp_path)
    assert status.available is True
    assert status.path == asset.resolve()
    assert "found" in status.message
    assert status.to_dict()["available"] is True
    assert status.to_dict()["path"] == str(asset.resolve())


def test_renderer_starts_on_loopback_and_writes_state(tmp_path: Path) -> None:
    web = make_web_root(tmp_path)
    renderer = ThreeVrmRenderer(tmp_path)
    try:
        assert renderer.url is None
        assert renderer.is_running is False
        renderer.start()
        assert renderer.is_running is True
        assert renderer.url is not None
        assert renderer.url.startswith("http://127.0.0.1:")
        assert renderer.bound_port is not None

        assert renderer.state_path == web / "state.json"
        assert renderer.state_path.is_file()
        data = json.loads(renderer.state_path.read_text(encoding="utf-8"))
        assert data["schema_version"] == 1
        assert isinstance(data["updated_at"], (int, float))
        assert data["state"] == "idle"
        assert data["viseme"] == "CLOSED"
        assert set(data["blendshapes"]) == set(BLEND_SHAPE_NAMES)
        assert data["vrm"]["available"] is False
        assert data["vrm"]["url"] is None
        assert data["vrm_available"] is False
        assert data["fallback"] is True

        renderer.update(
            {
                "state": "speaking",
                "viseme": "A",
                "blendshapes": {"A": 0.7, "I": 0.0, "U": 0.0, "E": 0.0, "O": 0.0},
            }
        )
        updated = json.loads(renderer.state_path.read_text(encoding="utf-8"))
        assert updated["state"] == "speaking"
        assert updated["viseme"] == "A"
        assert updated["blendshapes"]["A"] == pytest.approx(0.7)
        assert not renderer.state_path.with_name("state.json.tmp").exists()
    finally:
        renderer.stop()


def test_renderer_serves_state_json_over_http(tmp_path: Path) -> None:
    make_web_root(tmp_path)
    renderer = ThreeVrmRenderer(tmp_path)
    try:
        renderer.start()
        assert renderer.bound_port is not None
        renderer.update(
            {
                "state": "happy",
                "viseme": "E",
                "blendshapes": {"E": 0.4},
            }
        )
        data = get_state_json(renderer.bound_port)
        assert data["state"] == "happy"
        assert data["viseme"] == "E"
        assert data["blendshapes"]["E"] == pytest.approx(0.4)
    finally:
        renderer.stop()


def test_renderer_accepts_a_controller_snapshot(tmp_path: Path) -> None:
    make_web_root(tmp_path)
    renderer = ThreeVrmRenderer(tmp_path)
    controller = Avatar3DController()
    controller.on_speech_start()
    controller.update_amplitude(0.9, "aa")
    try:
        renderer.start()
        renderer.update(controller)
        assert renderer.bound_port is not None
        data = get_state_json(renderer.bound_port)
        assert data["state"] == "speaking"
        assert data["viseme"] == "A"
        assert data["blendshapes"]["A"] > 0.0
    finally:
        renderer.stop()


def test_renderer_update_sanitizes_bad_values(tmp_path: Path) -> None:
    make_web_root(tmp_path)
    renderer = ThreeVrmRenderer(tmp_path)
    try:
        renderer.start()
        renderer.update(
            {
                "state": "bogus",
                "viseme": "bogus",
                "blendshapes": {
                    "A": 5.0,
                    "I": -2.0,
                    "U": "bad",
                    "E": float("nan"),
                    "O": float("inf"),
                },
            }
        )
        data = json.loads(renderer.state_path.read_text(encoding="utf-8"))
        assert data["state"] == "idle"
        assert data["viseme"] == "CLOSED"
        assert data["blendshapes"]["A"] == 1.0
        assert data["blendshapes"]["I"] == 0.0
        assert data["blendshapes"]["U"] == 0.0
        assert data["blendshapes"]["E"] == 0.0
        assert data["blendshapes"]["O"] == 1.0
    finally:
        renderer.stop()


def test_renderer_reports_available_vrm_in_state_json(tmp_path: Path) -> None:
    make_web_root(tmp_path, with_vrm=True)
    renderer = ThreeVrmRenderer(tmp_path)
    try:
        renderer.start()
        data = json.loads(renderer.state_path.read_text(encoding="utf-8"))
        assert data["vrm"]["available"] is True
        assert data["vrm"]["url"] == "/assets/avatar/kivi.vrm"
        assert data["vrm_available"] is True
        assert data["fallback"] is False
        assert data["vrm"]["path"].endswith("kivi.vrm")
    finally:
        renderer.stop()


def test_renderer_stop_is_idempotent(tmp_path: Path) -> None:
    make_web_root(tmp_path)
    renderer = ThreeVrmRenderer(tmp_path)
    renderer.start()
    assert renderer.is_running is True
    renderer.stop()
    assert renderer.is_running is False
    assert renderer.url is None
    renderer.stop()
    assert renderer.is_running is False


def test_renderer_start_is_idempotent(tmp_path: Path) -> None:
    make_web_root(tmp_path)
    renderer = ThreeVrmRenderer(tmp_path)
    try:
        renderer.start()
        first = renderer.url
        renderer.start()
        assert renderer.url == first
    finally:
        renderer.stop()


def test_renderer_does_not_open_a_browser_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *args, **kwargs: calls.append(url) or True)
    make_web_root(tmp_path)
    renderer = ThreeVrmRenderer(tmp_path)
    try:
        renderer.start()
        assert calls == []
    finally:
        renderer.stop()


def test_renderer_opens_a_browser_only_when_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *args, **kwargs: calls.append(url) or True)
    make_web_root(tmp_path)
    renderer = ThreeVrmRenderer(tmp_path, open_browser=True)
    try:
        renderer.start()
        assert calls == [renderer.url]
    finally:
        renderer.stop()


def test_renderer_requires_index_html(tmp_path: Path) -> None:
    renderer = ThreeVrmRenderer(tmp_path)
    with pytest.raises(Avatar3DError, match="index.html"):
        renderer.start()
    assert renderer.is_running is False


def test_renderer_rejects_bad_port(tmp_path: Path) -> None:
    with pytest.raises(Avatar3DError, match="port"):
        ThreeVrmRenderer(tmp_path, port=-1)
    with pytest.raises(Avatar3DError, match="port"):
        ThreeVrmRenderer(tmp_path, port=True)  # type: ignore[arg-type]


def test_renderer_serves_web_page_and_assets(tmp_path: Path) -> None:
    web = make_web_root(tmp_path)
    (web / "asset.txt").write_text("hello", encoding="utf-8")
    note = tmp_path / "assets" / "avatar" / "note.txt"
    note.parent.mkdir(parents=True)
    note.write_text("asset-body", encoding="utf-8")
    renderer = ThreeVrmRenderer(tmp_path)
    try:
        renderer.start()
        assert renderer.bound_port is not None
        status, body = http_get(renderer.bound_port, "/")
        assert status == 200
        assert b"canvas" in body
        status, body = http_get(renderer.bound_port, "/asset.txt")
        assert status == 200
        assert body == b"hello"
        status, body = http_get(renderer.bound_port, "/assets/avatar/note.txt")
        assert status == 200
        assert body == b"asset-body"
    finally:
        renderer.stop()


def test_renderer_blocks_path_traversal(tmp_path: Path) -> None:
    make_web_root(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("not-public", encoding="utf-8")
    renderer = ThreeVrmRenderer(tmp_path)
    try:
        renderer.start()
        assert renderer.bound_port is not None
        status, body = http_get(renderer.bound_port, "/assets/../secret.txt")
        assert status == 404
        assert b"not-public" not in body
        status, _body = http_get(renderer.bound_port, "/assets/%00")
        assert status == 404
    finally:
        renderer.stop()


class FakeRenderer:
    def __init__(self) -> None:
        self.started = False
        self.snapshots: list[dict[str, object]] = []
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def update(self, snapshot: dict[str, object]) -> None:
        self.snapshots.append(snapshot)

    def stop(self) -> None:
        self.stopped = True


def test_fake_renderer_satisfies_the_protocol() -> None:
    renderer = FakeRenderer()
    assert isinstance(renderer, Avatar3DRenderer)
    renderer.start()
    renderer.update({"state": "idle"})
    renderer.stop()
    assert renderer.started is True
    assert renderer.snapshots == [{"state": "idle"}]
    assert renderer.stopped is True


def test_three_vrm_renderer_satisfies_the_protocol(tmp_path: Path) -> None:
    renderer = ThreeVrmRenderer(tmp_path)
    assert isinstance(renderer, Avatar3DRenderer)


def test_web_page_files_exist() -> None:
    root = project_root()
    assert (root / "web" / "avatar3d" / "index.html").is_file()
    assert (root / "web" / "avatar3d" / "app.js").is_file()
    assert (root / "web" / "avatar3d" / "README.md").is_file()
    assert (root / "docs" / "AVATAR3D.md").is_file()


def test_index_html_uses_an_import_map_for_three_and_three_vrm() -> None:
    html = (project_root() / "web" / "avatar3d" / "index.html").read_text(encoding="utf-8")
    assert 'type="importmap"' in html
    assert (
        "./node_modules/three/build/three.module.js" in html
        or "unpkg.com/three@" in html
    )
    assert "@pixiv/three-vrm" in html
    assert "./app.js" in html
    assert "<canvas" in html


def test_app_js_polls_state_maps_visemes_and_has_a_procedural_fallback() -> None:
    source = (project_root() / "web" / "avatar3d" / "app.js").read_text(encoding="utf-8")
    assert 'STATE_URL = "/state.json"' in source
    assert "POLL_INTERVAL_MS = 100" in source
    for expression in ('"aa"', '"ih"', '"ou"', '"ee"', '"oh"'):
        assert expression in source
    assert "VRMLoaderPlugin" in source
    assert "SphereGeometry" in source
    assert "validateState" in source
    assert "textContent" in source
    assert "innerHTML" not in source
    assert "eval(" not in source
    assert "new Function" not in source


def test_app_js_pauses_when_the_page_is_hidden() -> None:
    source = (project_root() / "web" / "avatar3d" / "app.js").read_text(encoding="utf-8")
    assert "visibilitychange" in source
    assert "document.hidden" in source
    assert "cancelAnimationFrame" in source
    assert "stopPolling" in source


def test_web_readme_documents_npm_and_the_vrm_drop_path() -> None:
    readme = (project_root() / "web" / "avatar3d" / "README.md").read_text(encoding="utf-8")
    assert "npm install three" in readme
    assert "@pixiv/three-vrm" in readme
    assert "assets/avatar/kivi.vrm" in readme
    assert "state.json" in readme


def test_avatar3d_doc_covers_asset_and_limitations() -> None:
    doc = (project_root() / "docs" / "AVATAR3D.md").read_text(encoding="utf-8")
    for fragment in ("VRM 1.0", "156", "Summer casual", "VRAM", "AEC", "always-on-top"):
        assert fragment in doc
