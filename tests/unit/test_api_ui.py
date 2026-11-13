from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from kivi.interfaces.api import LocalApi


def _client(api: LocalApi) -> httpx.Client:
    return httpx.Client(base_url=api.url, timeout=5.0, trust_env=False)


@pytest.fixture()
def ui_api(tmp_path: Path):
    static = tmp_path / "ui"
    static.mkdir()
    (static / "index.html").write_text("<h1>Kivi</h1>", encoding="utf-8")
    (static / "styles.css").write_text("body{}", encoding="utf-8")
    (static / "app.js").write_text("console.log('kivi')", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "API.md").write_text("# API", encoding="utf-8")
    api = LocalApi(
        lambda message: f"echo: {message}",
        static_dir=static,
        docs_dir=docs,
        tts_handler=lambda text: b"RIFF" + text.encode("utf-8"),
        stt_handler=lambda data: "распознано" if data.startswith(b"RIFF") else "",
        media_handler=lambda payload: {"image": "data:image/png;base64,AAAA"},
        video_handler=lambda payload: {
            "video": "data:video/mp4;base64,AAAA",
            "duration": 3,
        },
        fs_list_handler=lambda path: {
            "path": path,
            "entries": [{"name": "docs", "type": "dir", "size": 0}],
            "truncated": False,
        },
        fs_read_handler=lambda path: {
            "path": path,
            "content": "# hello",
            "truncated": False,
        },
        fs_mkdir_handler=lambda path: {"ok": True, "path": path},
        fs_rename_handler=lambda source, destination: {
            "ok": True,
            "source": source,
            "destination": destination,
        },
        fs_copy_handler=lambda source, destination: {"ok": True},
        fs_move_handler=lambda source, destination: {"ok": True},
        fs_trash_handler=lambda path: {"ok": True, "path": path},
        fs_search_handler=lambda query: {
            "query": query,
            "matches": [{"path": "docs/API.md", "name": "API.md"}],
            "truncated": False,
        },
        crm_status_handler=lambda: {
            "connected": False,
            "hint": "start Chrome",
            "url": "",
            "error": "",
        },
        crm_read_handler=lambda: {"ok": False, "order": "", "hint": "not connected"},
        crm_fill_handler=lambda payload: {"ok": True, "results": [], "saved": False},
        crm_act_handler=lambda payload: {"ok": True, "results": [], "saved": False},
        crm_task_handler=lambda payload: {
            "ok": True,
            "results": [],
            "saved": False,
            "created": False,
        },
        history_handler=lambda limit: [
            {"role": "user", "content": f"limit={limit}"},
        ],
        voices_handler=lambda: [{"id": "kivi-warm", "name": "Kivi warm"}],
        settings_handler=lambda: {"voice_provider": "fish", "model": "s2.1-pro-free"},
        settings_update_handler=lambda payload: {
            "voice_provider": str(payload.get("voice_provider", "fish")),
            "model": "s2.1-pro-free",
        },
        voice_select_handler=lambda voice_id: {"ok": True, "id": voice_id},
    )
    api.start()
    try:
        yield api
    finally:
        api.stop()


def test_static_index_and_assets(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        assert client.get("/").text == "<h1>Kivi</h1>"
        assert client.get("/styles.css").headers["content-type"].startswith("text/css")
        response = client.get("/app.js")
        doc = client.get("/docs/API.md")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert doc.status_code == 200
    assert doc.text == "# API"
    assert doc.headers["content-type"].startswith("text/markdown")


def test_tts_returns_wav_and_rejects_empty(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        response = client.post("/tts", json={"text": "привет"})
        empty = client.post("/tts", json={"text": "  "})
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content.startswith(b"RIFF")
    assert empty.status_code == 400


def test_transcribe_accepts_wav(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        response = client.post(
            "/transcribe",
            content=b"RIFF" + b"\x00" * 60,
            headers={"Content-Type": "audio/wav"},
        )
        bad = client.post(
            "/transcribe",
            content=b"not-wav",
            headers={"Content-Type": "audio/wav"},
        )
        wrong_type = client.post("/transcribe", content=b"not-wav")
    assert response.status_code == 200
    assert response.json() == {"text": "распознано"}
    assert bad.status_code == 400
    assert wrong_type.status_code == 415


def test_media_image_endpoint(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        response = client.post("/media/image", json={"prompt": "kivi"})
    assert response.status_code == 200
    assert response.json()["image"].startswith("data:image/")


def test_media_video_endpoint(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        response = client.post("/media/video", json={"prompt": "kivi", "duration": 3})
    assert response.status_code == 200
    assert response.json()["video"].startswith("data:video/mp4")
    assert response.json()["duration"] == 3


def test_fs_list_and_read(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        listing = client.get("/fs/list?path=docs")
        reading = client.get("/fs/read?path=docs/API.md")
    assert listing.status_code == 200
    assert listing.json()["entries"][0]["name"] == "docs"
    assert reading.status_code == 200
    assert reading.json()["content"] == "# hello"


def test_fs_mkdir_and_rename(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        mkdir = client.post("/fs/mkdir", json={"path": "new"})
        rename = client.post(
            "/fs/rename",
            json={"source": "new", "destination": "renamed"},
        )
    assert mkdir.status_code == 200
    assert mkdir.json()["path"] == "new"
    assert rename.status_code == 200
    assert rename.json()["destination"] == "renamed"


def test_cross_site_and_non_json_mutations_are_rejected(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        evil_origin = client.post(
            "/fs/mkdir",
            json={"path": "evil"},
            headers={"Origin": "https://evil.example"},
        )
        evil_host = client.post(
            "/fs/mkdir",
            json={"path": "evil"},
            headers={"Host": "evil.example"},
        )
        text_plain = client.post(
            "/fs/mkdir",
            content=b'{"path":"evil"}',
            headers={"Content-Type": "text/plain"},
        )
    assert evil_origin.status_code == 403
    assert evil_host.status_code == 403
    assert text_plain.status_code == 415


def test_fs_copy_and_move(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        copied = client.post(
            "/fs/copy",
            json={"source": "a.txt", "destination": "b.txt"},
        )
        moved = client.post(
            "/fs/move",
            json={"source": "b.txt", "destination": "c.txt"},
        )
    assert copied.status_code == 200
    assert moved.status_code == 200


def test_fs_search(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        response = client.get("/fs/search?q=api")
    assert response.status_code == 200
    assert response.json()["matches"][0]["path"] == "docs/API.md"


def test_fs_trash_requires_matching_confirmation(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        good = client.post(
            "/fs/trash",
            json={"path": "docs/old.txt", "confirm": "old.txt"},
        )
        bad = client.post(
            "/fs/trash",
            json={"path": "docs/old.txt", "confirm": "wrong"},
        )
    assert good.status_code == 200
    assert bad.status_code == 400


def test_crm_write_endpoints(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        fill = client.post("/crm/fill", json={"comment": "x", "checklist": {}})
        act = client.post("/crm/act", json={"rows": []})
        task = client.post("/crm/task", json={"comment": "x", "when_text": ""})
    assert fill.status_code == 200
    assert fill.json()["saved"] is False
    assert act.status_code == 200
    assert task.status_code == 200
    assert task.json()["created"] is False


def test_crm_status_and_read(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        status = client.get("/crm/status")
        reading = client.get("/crm/read")
    assert status.status_code == 200
    assert status.json()["connected"] is False
    assert reading.status_code == 200
    assert reading.json()["ok"] is False


def test_settings_update(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        response = client.post("/settings", json={"voice_provider": "qwen"})
    assert response.status_code == 200
    assert response.json()["voice_provider"] == "qwen"


def test_history_limit_and_voices_settings(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        history = client.get("/history?limit=7")
        voices = client.get("/voices")
        settings = client.get("/settings")
        selected = client.post("/voices/active", json={"id": "kivi-warm"})
    assert history.json()["messages"][0]["content"] == "limit=7"
    assert voices.json()["voices"][0]["id"] == "kivi-warm"
    assert settings.json()["voice_provider"] == "fish"
    assert selected.json() == {"ok": True, "id": "kivi-warm"}


def test_ui_routes_503_when_not_configured(tmp_path: Path) -> None:
    api = LocalApi(lambda message: message, static_dir=tmp_path)
    api.start()
    try:
        with _client(api) as client:
            assert client.post("/tts", json={"text": "x"}).status_code == 503
            assert client.post("/voices/active", json={"id": "x"}).status_code == 503
            assert client.get("/history").json() == {"messages": []}
            assert client.get("/voices").json() == {"voices": []}
    finally:
        api.stop()


def test_static_missing_file_is_404(ui_api: LocalApi) -> None:
    with _client(ui_api) as client:
        assert client.get("/nope").status_code == 404
