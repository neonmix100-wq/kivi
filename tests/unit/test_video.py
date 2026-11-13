"""Unit tests for the local still-image video fallback.

No test touches the network or starts a real ffmpeg: the tool receives an
``httpx.Client`` on a ``MockTransport`` plus a fake ``ffmpeg_runner`` that writes
a minimal ``ftyp`` MP4. The runner is also exercised on its own with a
monkeypatched ``subprocess.run``. Executor tests use the real permission engine,
zones and audit database.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from pydantic import ValidationError

from kivi.kernel.db import Database
from kivi.safety.permissions import Grant, PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, ToolRegistry, ToolResult
from kivi.tools.base import ToolError
from kivi.tools.video import (
    FFMPEG_INSTALL_HINT,
    FPS,
    MediaVideoTool,
    VideoGenerateArgs,
    build_ffmpeg_argv,
    build_source_url,
    is_mp4_header,
    resolve_ffmpeg,
    run_ffmpeg,
)

Handler = Callable[[httpx.Request], httpx.Response]
Runner = Callable[[list[str], Path], None]

# A minimal ISO base media header: ftyp at offset 4 is what the tool checks.
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
GIF = b"GIF89a" + b"\x00" * 16


def ok_jpeg_handler(record: dict[str, httpx.Request] | None = None) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record["request"] = request
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=JPEG)

    return handler


def write_mp4_runner(
    content: bytes = MP4, record: dict[str, object] | None = None
) -> Runner:
    """Fake encoder: records the call and writes ``content`` to ``out_path``."""

    def runner(argv: list[str], out_path: Path) -> None:
        image_path = Path(argv[argv.index("-i") + 1])
        if record is not None:
            record["argv"] = argv
            record["out_path"] = out_path
            record["image_path"] = image_path
            record["image_existed"] = image_path.is_file()
        out_path.write_bytes(content)

    return runner


@contextmanager
def video_tool(
    handler: Handler,
    runner: Runner | None = None,
    **kwargs: object,
) -> Iterator[MediaVideoTool]:
    """Build a tool on a mock transport with a fake encoder by default."""
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=120.0,
        follow_redirects=False,
    )
    try:
        yield MediaVideoTool(
            client=client,
            ffmpeg_runner=runner or write_mp4_runner(),
            ffmpeg_exe="ffmpeg",
            **kwargs,
        )
    finally:
        client.close()


@contextmanager
def executor_env(
    tmp_path: Path,
    tool: MediaVideoTool,
    capabilities: tuple[str, ...] = ("media.generate",),
) -> Iterator[tuple[ToolExecutor, Database, Grant, Path]]:
    """Real executor, zones, permissions and audit against a temporary workspace."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db = Database(tmp_path / "kivi.db")
    engine = PermissionEngine(db, workspace=str(workspace))
    grant = engine.issue("t1", list(capabilities))
    registry = ToolRegistry()
    registry.register(tool)
    executor = ToolExecutor(
        registry,
        db,
        engine,
        ProtectedZones(workspace),
        workspace=workspace,
    )
    try:
        yield executor, db, grant, workspace
    finally:
        db.close()


def run(
    executor: ToolExecutor,
    grant: Grant,
    args: dict[str, object] | None = None,
    **kwargs: object,
) -> ToolResult:
    return executor.run(
        "media.video",
        {"prompt": "a red fox in snow", **(args or {})},
        grant=grant,
        **kwargs,
    )


# -- arguments, URL and argv -------------------------------------------------
def test_tool_metadata() -> None:
    tool = MediaVideoTool()
    assert tool.name == "media.video"
    assert tool.risk == "R1"
    assert tool.capabilities == ("media.generate",)
    assert tool.args_model is VideoGenerateArgs
    schema = VideoGenerateArgs.model_json_schema()
    assert schema["properties"]["width"]["enum"] == [512, 768]
    assert schema["properties"]["height"]["enum"] == [512, 768]
    assert schema["properties"]["model"]["enum"] == ["flux", "turbo"]
    assert schema["additionalProperties"] is False


def test_argument_defaults() -> None:
    args = VideoGenerateArgs(prompt="a red fox")
    assert args.width == 512
    assert args.height == 512
    assert args.duration == 4
    assert args.model == "flux"
    assert args.seed is None


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": ""},
        {"prompt": "   "},
        {"prompt": "x" * 501},
        {"prompt": "ok", "width": 256},
        {"prompt": "ok", "width": 1024},
        {"prompt": "ok", "height": 256},
        {"prompt": "ok", "height": 1024},
        {"prompt": "ok", "duration": 1},
        {"prompt": "ok", "duration": 9},
        {"prompt": "ok", "model": "runway"},
        {"prompt": "ok", "seed": -1},
    ],
)
def test_invalid_arguments_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        VideoGenerateArgs(**payload)


@pytest.mark.parametrize(
    "extra",
    [
        {"path": "private/secret.txt"},
        {"url": "https://evil.example/video.mp4"},
        {"ffmpeg_args": ["-f", "lavfi"]},
        {"image": "local.jpg"},
    ],
)
def test_no_arbitrary_path_or_encoder_argument(extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        VideoGenerateArgs(prompt="ok", **extra)


def test_source_url_is_fixed_host_and_fully_encoded() -> None:
    args = VideoGenerateArgs(
        prompt="a cat / dog?",
        width=768,
        height=512,
        model="turbo",
        seed=42,
        duration=3,
    )
    url = build_source_url(args)
    parts = urlsplit(url)
    assert parts.scheme == "https"
    assert parts.hostname == "image.pollinations.ai"
    assert parts.path.startswith("/prompt/a%20cat%20%2F%20dog%3F")
    assert dict(parse_qsl(parts.query)) == {
        "width": "768",
        "height": "512",
        "model": "turbo",
        "nologo": "true",
        "seed": "42",
    }


def test_prompt_cannot_change_the_host() -> None:
    args = VideoGenerateArgs(prompt="https://evil.example/../../x")
    url = build_source_url(args)
    assert urlsplit(url).hostname == "image.pollinations.ai"
    assert "%2F" in url


def test_ffmpeg_argv_is_fixed(tmp_path: Path) -> None:
    image = tmp_path / "in.jpg"
    out = tmp_path / "out.mp4"
    argv = build_ffmpeg_argv(
        "ffmpeg.exe",
        image_path=image,
        out_path=out,
        width=512,
        height=512,
        duration=4,
    )
    assert argv[0] == "ffmpeg.exe"
    assert argv[-1] == str(out)
    assert argv[argv.index("-i") + 1] == str(image)
    assert argv[argv.index("-loop") + 1] == "1"
    assert argv[argv.index("-t") + 1] == "4"
    assert argv[argv.index("-vf") + 1] == (
        "zoompan=z='min(zoom+0.0008,1.08)':d=1:s=512x512:fps=24"
    )
    assert argv[argv.index("-c:v") + 1] == "libx264"
    assert argv[argv.index("-pix_fmt") + 1] == "yuv420p"
    assert argv[argv.index("-movflags") + 1] == "+faststart"
    assert "-an" in argv
    assert all(isinstance(part, str) for part in argv)


def test_ffmpeg_argv_reflects_validated_size_and_duration(tmp_path: Path) -> None:
    argv = build_ffmpeg_argv(
        "ffmpeg",
        image_path=tmp_path / "in.jpg",
        out_path=tmp_path / "out.mp4",
        width=768,
        height=768,
        duration=8,
    )
    assert "s=768x768:fps=24" in argv[argv.index("-vf") + 1]
    assert argv[argv.index("-t") + 1] == "8"


# -- success path ------------------------------------------------------------
def test_success_writes_a_local_mp4(tmp_path: Path) -> None:
    seen: dict[str, object] = {}
    with (
        video_tool(ok_jpeg_handler(), write_mp4_runner(record=seen)) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant)

    assert result.ok
    assert result.trust == "untrusted:web"
    assert result.truncated is False
    assert len(result.artifacts) == 1
    path = Path(result.artifacts[0])
    assert path.is_file()
    assert path.parent == (workspace / "data" / "media").resolve()
    assert re.fullmatch(r"kivi-[0-9a-f]{32}\.mp4", path.name)
    assert path.read_bytes() == MP4
    assert is_mp4_header(path.read_bytes()[:12])
    assert result.meta["bytes"] == len(MP4)
    assert result.meta["duration"] == 4
    assert result.meta["width"] == 512
    assert result.meta["height"] == 512
    assert result.meta["fps"] == FPS
    assert result.meta["model"] == "flux"
    assert result.meta["seed"] is None
    assert result.meta["source_type"] == "image/jpeg"
    assert result.meta["source_bytes"] == len(JPEG)
    assert result.meta["external_video_api"] is False
    assert "nologo=true" in result.meta["source_url"]


def test_still_is_a_real_intermediate_and_is_cleaned_up(tmp_path: Path) -> None:
    seen: dict[str, object] = {}
    with (
        video_tool(ok_jpeg_handler(), write_mp4_runner(record=seen)) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant)

    assert result.ok
    image_path = seen["image_path"]
    assert isinstance(image_path, Path)
    assert seen["image_existed"] is True
    assert image_path.parent == (workspace / "data" / "media").resolve()
    assert re.fullmatch(r"video-src-[0-9a-f]{32}\.jpg", image_path.name)
    assert not image_path.exists()
    assert sorted(p.name for p in (workspace / "data" / "media").iterdir()) == [
        Path(result.artifacts[0]).name
    ]


def test_request_uses_expected_params_and_no_api_key(tmp_path: Path) -> None:
    seen: dict[str, httpx.Request] = {}

    with (
        video_tool(ok_jpeg_handler(seen)) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(
            executor,
            grant,
            {
                "prompt": "a lighthouse at night",
                "width": 768,
                "height": 768,
                "model": "turbo",
                "seed": 7,
                "duration": 6,
            },
        )

    assert result.ok
    request = seen["request"]
    assert request.method == "GET"
    assert request.url.host == "image.pollinations.ai"
    assert request.url.params["width"] == "768"
    assert request.url.params["height"] == "768"
    assert request.url.params["model"] == "turbo"
    assert request.url.params["seed"] == "7"
    assert request.url.params["nologo"] == "true"
    assert "authorization" not in request.headers
    assert "a%20lighthouse%20at%20night" in str(request.url)


def test_each_run_gets_a_fresh_output_name(tmp_path: Path) -> None:
    with (
        video_tool(ok_jpeg_handler()) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        first = run(executor, grant)
        second = run(executor, grant)

    assert first.ok and second.ok
    assert first.artifacts != second.artifacts
    assert Path(first.artifacts[0]).is_file()
    assert Path(second.artifacts[0]).is_file()


# -- dry run -----------------------------------------------------------------
def test_dry_run_has_no_network_and_no_files(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called during dry run")

    def runner(argv: list[str], out_path: Path) -> None:
        raise AssertionError("ffmpeg must not run during dry run")

    with (
        video_tool(handler, runner) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant, dry_run=True)

    assert result.ok
    assert result.meta["dry_run"] is True
    assert '"action": "video-generate"' in result.output
    assert '"mode": "local-still-fallback"' in result.output
    assert '"external_video_api": false' in result.output
    assert "https://image.pollinations.ai/prompt/" in result.output
    assert not (workspace / "data").exists()


def test_dry_run_applies_the_prompt_policy(tmp_path: Path) -> None:
    with (
        video_tool(ok_jpeg_handler()) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant, {"prompt": "nsfw portrait"}, dry_run=True)

    assert not result.ok
    assert "blocked by media policy" in result.error
    assert not (workspace / "data").exists()


# -- source validation -------------------------------------------------------
def test_blocked_prompt_is_rejected_before_any_request(tmp_path: Path) -> None:
    calls = 0
    runner_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=JPEG)

    def runner(argv: list[str], out_path: Path) -> None:
        nonlocal runner_calls
        runner_calls += 1

    with (
        video_tool(handler, runner) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant, {"prompt": "explicit nude scene"})

    assert not result.ok
    assert "blocked by media policy" in result.error
    assert calls == 0
    assert runner_calls == 0
    assert not (workspace / "data").exists()


@pytest.mark.parametrize(
    "prompt",
    ["a cat\x00with a null", "a cat\u202ewith bidi", "a cat\u200bwith zero width"],
)
def test_control_characters_are_rejected(tmp_path: Path, prompt: str) -> None:
    with (
        video_tool(ok_jpeg_handler()) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant, {"prompt": prompt})

    assert not result.ok
    assert "control characters" in result.error


def test_too_long_prompt_is_invalid_arguments(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=JPEG)

    with (
        video_tool(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant, {"prompt": "x" * 501})

    assert not result.ok
    assert "invalid arguments" in result.error
    assert calls == 0


@pytest.mark.parametrize("duration", [1, 9])
def test_invalid_duration_is_rejected_by_the_executor(
    tmp_path: Path, duration: int
) -> None:
    with (
        video_tool(ok_jpeg_handler()) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant, {"duration": duration})

    assert not result.ok
    assert "invalid arguments" in result.error


@pytest.mark.parametrize("size", [256, 1024])
def test_invalid_size_is_rejected_by_the_executor(tmp_path: Path, size: int) -> None:
    with (
        video_tool(ok_jpeg_handler()) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant, {"width": size, "height": size})

    assert not result.ok
    assert "invalid arguments" in result.error


def test_http_error_is_reported(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"busy")

    with (
        video_tool(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "HTTP 503" in result.error
    assert not (workspace / "data").exists()


def test_redirect_is_not_followed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    with (
        video_tool(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "redirect" in result.error


def test_non_image_content_type_is_rejected(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<html>not an image</html>",
        )

    with (
        video_tool(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "content type" in result.error
    assert not (workspace / "data").exists()


def test_unsupported_image_type_is_rejected(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/gif"}, content=GIF)

    with (
        video_tool(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "unsupported image type" in result.error


def test_invalid_magic_bytes_are_rejected(tmp_path: Path) -> None:
    runner_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=b"this is not a jpeg at all",
        )

    def runner(argv: list[str], out_path: Path) -> None:
        nonlocal runner_calls
        runner_calls += 1

    with (
        video_tool(handler, runner) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "not a PNG, JPEG or WebP" in result.error
    assert runner_calls == 0
    assert not (workspace / "data").exists()


def test_oversized_image_is_rejected_before_encoding(tmp_path: Path) -> None:
    runner_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/jpeg"},
            content=JPEG + b"\x00" * (10 * 1024 * 1024),
        )

    def runner(argv: list[str], out_path: Path) -> None:
        nonlocal runner_calls
        runner_calls += 1

    with (
        video_tool(handler, runner) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "too large" in result.error
    assert runner_calls == 0
    assert not (workspace / "data").exists()


def test_declared_content_length_is_checked_first(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "image/jpeg",
                "content-length": str(10 * 1024 * 1024 + 1),
            },
            content=JPEG,
        )

    with (
        video_tool(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "too large" in result.error


# -- encoder failures --------------------------------------------------------
def test_missing_ffmpeg_gives_an_install_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def boom() -> str:
        raise ToolError(FFMPEG_INSTALL_HINT)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=JPEG)

    monkeypatch.setattr("kivi.tools.video.resolve_ffmpeg", boom)
    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=120.0)
    tool = MediaVideoTool(client=client, ffmpeg_runner=write_mp4_runner())
    try:
        with executor_env(tmp_path, tool) as (executor, _db, grant, workspace):
            result = run(executor, grant)
    finally:
        client.close()

    assert not result.ok
    assert "imageio-ffmpeg" in result.error
    assert calls == 0  # fail before downloading anything
    assert not (workspace / "data").exists()


def test_ffmpeg_failure_leaves_no_files(tmp_path: Path) -> None:
    def runner(argv: list[str], out_path: Path) -> None:
        out_path.write_bytes(b"\x00\x00\x00\x18ftypis")  # partial garbage
        raise ToolError("ffmpeg failed with exit code 1: boom")

    with (
        video_tool(ok_jpeg_handler(), runner) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "ffmpeg failed" in result.error
    media = workspace / "data" / "media"
    assert not media.exists() or not list(media.iterdir())


def test_output_without_ftyp_is_rejected(tmp_path: Path) -> None:
    with (
        video_tool(ok_jpeg_handler(), write_mp4_runner(b"not an mp4 file")) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "not an MP4" in result.error
    media = workspace / "data" / "media"
    assert not media.exists() or not list(media.iterdir())


def test_too_large_video_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("kivi.tools.video.MAX_VIDEO_BYTES", len(MP4) - 1)
    with (
        video_tool(ok_jpeg_handler()) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "too large" in result.error
    media = workspace / "data" / "media"
    assert not media.exists() or not list(media.iterdir())


# -- ffmpeg discovery and the real runner ------------------------------------
def test_resolve_ffmpeg_returns_an_existing_binary() -> None:
    imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")
    exe = resolve_ffmpeg()
    assert Path(exe).is_file()
    assert exe == imageio_ffmpeg.get_ffmpeg_exe()


def test_resolve_ffmpeg_reports_a_broken_install(monkeypatch: pytest.MonkeyPatch) -> None:
    imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")

    def boom() -> str:
        raise RuntimeError("no bundled binary")

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", boom)
    with pytest.raises(ToolError, match="imageio-ffmpeg"):
        resolve_ffmpeg()


def test_resolve_ffmpeg_reports_a_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    imageio_ffmpeg = pytest.importorskip("imageio_ffmpeg")
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: "C:/nope/ffmpeg.exe")
    with pytest.raises(ToolError, match="reinstall imageio-ffmpeg"):
        resolve_ffmpeg()


def test_default_runner_uses_a_fixed_list_and_no_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}
    out = tmp_path / "out.mp4"

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        out.write_bytes(MP4)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("kivi.tools.video.subprocess.run", fake_run)
    run_ffmpeg(["ffmpeg", "-i", "in.jpg", "out.mp4"], out)

    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["check"] is False
    assert isinstance(seen["argv"], list)
    assert all(isinstance(part, str) for part in seen["argv"])
    assert out.read_bytes() == MP4


def test_default_runner_reports_a_non_zero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stderr=b"Invalid data found")

    monkeypatch.setattr("kivi.tools.video.subprocess.run", fake_run)
    with pytest.raises(ToolError, match="exit code 1"):
        run_ffmpeg(["ffmpeg", "-i", "in.jpg", "out.mp4"], tmp_path / "out.mp4")


def test_default_runner_reports_a_missing_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr("kivi.tools.video.subprocess.run", fake_run)
    with pytest.raises(ToolError, match="imageio-ffmpeg"):
        run_ffmpeg(["ffmpeg", "-i", "in.jpg", "out.mp4"], tmp_path / "out.mp4")


def test_default_runner_reports_a_missing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("kivi.tools.video.subprocess.run", fake_run)
    with pytest.raises(ToolError, match="no output file"):
        run_ffmpeg(["ffmpeg"], tmp_path / "out.mp4")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (MP4, True),
        (b"", False),
        (b"\x00\x00\x00\x18moov" + b"\x00" * 8, False),
        (JPEG, False),
    ],
)
def test_is_mp4_header(content: bytes, expected: bool) -> None:
    assert is_mp4_header(content) is expected


# -- executor, permissions and audit ----------------------------------------
def test_executor_audits_the_r1_capability(tmp_path: Path) -> None:
    with (
        video_tool(ok_jpeg_handler()) as tool,
        executor_env(tmp_path, tool) as (executor, db, grant, _workspace),
    ):
        result = run(executor, grant)
        rows = db.recent_audit()

    assert result.ok
    row = rows[0]
    assert row["actor"] == "tool"
    assert row["action"] == "tool.media.video"
    assert row["decision"] == "allow"
    details = json.loads(row["details"])
    assert details["risk"] == "R1"
    assert details["capability"] == "media.generate"
    assert details["task_id"] == "t1"
    assert details["trust"] == "untrusted:web"
    assert details["ok"] is True


def test_missing_media_generate_capability_is_denied(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=JPEG)

    with (
        video_tool(handler) as tool,
        executor_env(tmp_path, tool, capabilities=("fs.read",)) as (
            executor,
            db,
            grant,
            _workspace,
        ),
    ):
        result = run(executor, grant)
        rows = db.recent_audit()

    assert not result.ok
    assert "not granted" in result.error
    assert calls == 0
    assert rows[0]["action"] == "tool.media.video"
    assert rows[0]["decision"] == "deny"
