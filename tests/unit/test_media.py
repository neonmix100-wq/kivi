"""Unit tests for the free Pollinations image tool.

No test touches the network: every tool is given an ``httpx.Client`` wired to a
``httpx.MockTransport`` handler that returns canned bytes. The executor tests
use the real permission engine, zones and audit database.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from pydantic import ValidationError

from kivi.kernel.db import Database
from kivi.safety.permissions import Grant, PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, ToolRegistry, ToolResult
from kivi.tools.media import (
    DEFAULT_PROMPT_DENYLIST,
    MAX_IMAGE_BYTES,
    MediaGenerateArgs,
    MediaImageTool,
    build_image_url,
    detect_image_type,
)

Handler = Callable[[httpx.Request], httpx.Response]

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
WEBP = b"RIFF" + (32).to_bytes(4, "little") + b"WEBP" + b"VP8 " + b"\x00" * 16
UNSUPPORTED = b"GIF89a" + b"\x00" * 16


@contextmanager
def tool_client(handler: Handler, **kwargs: object) -> Iterator[MediaImageTool]:
    """Build a tool backed by a mock transport; close the client afterwards."""
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        timeout=120.0,
        follow_redirects=False,
    )
    try:
        yield MediaImageTool(client=client, **kwargs)
    finally:
        client.close()


@contextmanager
def executor_env(
    tmp_path: Path,
    tool: MediaImageTool,
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


def ok_png_handler(record: dict[str, httpx.Request] | None = None) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record["request"] = request
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=PNG,
        )

    return handler


def run(
    executor: ToolExecutor,
    grant: Grant,
    args: dict[str, object] | None = None,
    **kwargs: object,
) -> ToolResult:
    return executor.run(
        "media.image",
        {"prompt": "a red fox in snow", **(args or {})},
        grant=grant,
        **kwargs,
    )


# -- arguments and URL -------------------------------------------------------
def test_tool_metadata() -> None:
    tool = MediaImageTool()
    assert tool.name == "media.image"
    assert tool.risk == "R1"
    assert tool.capabilities == ("media.generate",)
    assert tool.args_model is MediaGenerateArgs
    schema = MediaGenerateArgs.model_json_schema()
    assert schema["properties"]["model"]["enum"] == ["flux", "turbo"]
    assert schema["additionalProperties"] is False


def test_argument_defaults() -> None:
    args = MediaGenerateArgs(prompt="a red fox")
    assert args.width == 1024
    assert args.height == 1024
    assert args.model == "flux"
    assert args.seed is None
    assert args.negative_prompt is None


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": ""},
        {"prompt": "   "},
        {"prompt": "x" * 501},
        {"prompt": "ok", "width": 255},
        {"prompt": "ok", "width": 1025},
        {"prompt": "ok", "height": 255},
        {"prompt": "ok", "height": 1025},
        {"prompt": "ok", "model": "sdxl"},
        {"prompt": "ok", "seed": -1},
        {"prompt": "ok", "negative_prompt": "x" * 501},
    ],
)
def test_invalid_arguments_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MediaGenerateArgs(**payload)


@pytest.mark.parametrize(
    "extra",
    [
        {"path": "private/secret.txt"},
        {"url": "https://evil.example/image.png"},
        {"image": "local.png"},
    ],
)
def test_no_local_file_or_arbitrary_url_argument(extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MediaGenerateArgs(prompt="ok", **extra)


def test_url_is_fixed_host_and_fully_encoded() -> None:
    args = MediaGenerateArgs(
        prompt="a cat / dog?",
        width=512,
        height=768,
        model="turbo",
        seed=42,
        negative_prompt="blurry, low quality",
    )
    url = build_image_url(args)
    parts = urlsplit(url)
    assert parts.scheme == "https"
    assert parts.hostname == "image.pollinations.ai"
    assert parts.path.startswith("/prompt/a%20cat%20%2F%20dog%3F")
    params = dict(parse_qsl(parts.query))
    assert params == {
        "width": "512",
        "height": "768",
        "model": "turbo",
        "nologo": "true",
        "seed": "42",
        "negative_prompt": "blurry, low quality",
    }


def test_prompt_cannot_change_the_host() -> None:
    args = MediaGenerateArgs(prompt="https://evil.example/../../image")
    url = build_image_url(args)
    assert urlsplit(url).hostname == "image.pollinations.ai"
    assert "evil.example" in args.prompt
    assert "%2F" in url


def test_seed_is_omitted_when_not_requested() -> None:
    url = build_image_url(MediaGenerateArgs(prompt="a fox"))
    assert "seed" not in dict(parse_qsl(urlsplit(url).query))


# -- magic bytes -------------------------------------------------------------
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (PNG, ("png", "image/png")),
        (JPEG, ("jpg", "image/jpeg")),
        (WEBP, ("webp", "image/webp")),
    ],
)
def test_detect_image_type(content: bytes, expected: tuple[str, str]) -> None:
    assert detect_image_type(content) == expected


def test_detect_image_type_rejects_unknown_bytes() -> None:
    assert detect_image_type(UNSUPPORTED) is None
    assert detect_image_type(b"") is None


# -- success paths -----------------------------------------------------------
def test_success_png_is_saved_under_data_media(tmp_path: Path) -> None:
    with (
        tool_client(ok_png_handler()) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            _db,
            grant,
            workspace,
        ),
    ):
        result = run(executor, grant)

    assert result.ok
    assert result.trust == "untrusted:web"
    assert result.truncated is False
    assert len(result.artifacts) == 1
    path = Path(result.artifacts[0])
    assert path.is_file()
    assert path.parent == (workspace / "data" / "media").resolve()
    assert re.fullmatch(r"[0-9a-f]{32}\.png", path.name)
    assert path.read_bytes() == PNG
    assert result.meta["bytes"] == len(PNG)
    assert result.meta["width"] == 1024
    assert result.meta["height"] == 1024
    assert result.meta["model"] == "flux"
    assert result.meta["seed"] is None
    assert result.meta["content_type"] == "image/png"
    assert "nologo=true" in result.meta["url"]


def test_request_uses_expected_params_and_no_api_key(tmp_path: Path) -> None:
    seen: dict[str, httpx.Request] = {}

    with (
        tool_client(ok_png_handler(seen)) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            _db,
            grant,
            _workspace,
        ),
    ):
        result = run(
            executor,
            grant,
            {
                "prompt": "a lighthouse at night",
                "width": 512,
                "height": 768,
                "model": "turbo",
                "seed": 7,
                "negative_prompt": "blurry",
            },
        )

    assert result.ok
    request = seen["request"]
    assert request.method == "GET"
    assert request.url.host == "image.pollinations.ai"
    assert request.url.params["width"] == "512"
    assert request.url.params["height"] == "768"
    assert request.url.params["model"] == "turbo"
    assert request.url.params["seed"] == "7"
    assert request.url.params["nologo"] == "true"
    assert request.url.params["negative_prompt"] == "blurry"
    assert "authorization" not in request.headers
    assert "a%20lighthouse%20at%20night" in str(request.url)


@pytest.mark.parametrize(
    ("content", "mime", "suffix"),
    [
        (JPEG, "image/jpeg", ".jpg"),
        (WEBP, "image/webp", ".webp"),
        (PNG, "image/jpg", ".png"),
    ],
)
def test_supported_formats_use_magic_bytes_for_the_extension(
    tmp_path: Path, content: bytes, mime: str, suffix: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": mime}, content=content)

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            _db,
            grant,
            _workspace,
        ),
    ):
        result = run(executor, grant)

    assert result.ok
    path = Path(result.artifacts[0])
    assert path.suffix == suffix
    assert path.read_bytes() == content


# -- dry run -----------------------------------------------------------------
def test_dry_run_shows_url_without_network(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called during dry run")

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            _db,
            grant,
            workspace,
        ),
    ):
        result = run(executor, grant, dry_run=True)

    assert result.ok
    assert result.meta["dry_run"] is True
    assert '"action": "media-generate"' in result.output
    assert "https://image.pollinations.ai/prompt/" in result.output
    assert "nologo=true" in result.output
    assert not (workspace / "data" / "media").exists()


# -- network and response validation ----------------------------------------
def test_http_error_is_reported(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"busy")

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            _db,
            grant,
            workspace,
        ),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "HTTP 503" in result.error
    assert not (workspace / "data" / "media").exists()


def test_redirect_is_not_followed(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example/x"})

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            _db,
            grant,
            _workspace,
        ),
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
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            _db,
            grant,
            _workspace,
        ),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "content type" in result.error


def test_unsupported_image_type_is_rejected(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/gif"}, content=UNSUPPORTED)

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            _db,
            grant,
            _workspace,
        ),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "unsupported image type" in result.error


def test_invalid_magic_bytes_are_rejected(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=b"this is not a png at all",
        )

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            _db,
            grant,
            workspace,
        ),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "not a PNG" in result.error
    assert not list((workspace / "data").glob("media/*"))


def test_oversized_response_is_rejected(tmp_path: Path) -> None:
    oversized = PNG + b"\x00" * MAX_IMAGE_BYTES

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=oversized,
        )

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            _db,
            grant,
            workspace,
        ),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "too large" in result.error
    assert not list((workspace / "data").glob("media/*"))


def test_declared_content_length_is_checked_before_reading(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "content-type": "image/png",
                "content-length": str(MAX_IMAGE_BYTES + 1),
            },
            content=PNG,
        )

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            _db,
            grant,
            _workspace,
        ),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "too large" in result.error


# -- prompt policy -----------------------------------------------------------
def test_prompt_too_long_is_rejected_as_invalid_arguments(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            _db,
            grant,
            _workspace,
        ),
    ):
        result = run(executor, grant, {"prompt": "x" * 501})

    assert not result.ok
    assert "invalid arguments" in result.error
    assert calls == 0


def test_blocked_prompt_is_rejected_before_any_request(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            _db,
            grant,
            workspace,
        ),
    ):
        result = run(executor, grant, {"prompt": "nsfw portrait of a woman"})

    assert not result.ok
    assert "blocked by media policy" in result.error
    assert calls == 0
    assert not list((workspace / "data").glob("media/*"))


def test_custom_blocked_terms_extend_the_defaults(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    with (
        tool_client(handler, blocked_terms=("forbidden",)) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        custom = run(executor, grant, {"prompt": "a forbidden castle"})
        default = run(executor, grant, {"prompt": "a nude portrait"})

    assert not custom.ok
    assert "forbidden" in custom.error
    assert not default.ok
    assert "blocked" in default.error
    assert calls == 0
    assert "nsfw" in DEFAULT_PROMPT_DENYLIST


@pytest.mark.parametrize(
    "prompt",
    ["a cat\x00with a null", "a cat\u202ewith bidi", "a cat\u200bwith zero width"],
)
def test_control_characters_are_rejected(tmp_path: Path, prompt: str) -> None:
    with (
        tool_client(ok_png_handler()) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            _db,
            grant,
            _workspace,
        ),
    ):
        result = run(executor, grant, {"prompt": prompt})

    assert not result.ok
    assert "control characters" in result.error


def test_control_characters_in_negative_prompt_are_rejected(tmp_path: Path) -> None:
    with (
        tool_client(ok_png_handler()) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            _db,
            grant,
            _workspace,
        ),
    ):
        result = run(executor, grant, {"negative_prompt": "blurry\x07"})

    assert not result.ok
    assert "control characters" in result.error


# -- executor, permissions and audit ----------------------------------------
def test_executor_audits_the_successful_call(tmp_path: Path) -> None:
    with (
        tool_client(ok_png_handler()) as tool,
        executor_env(tmp_path, tool) as (
            executor,
            db,
            grant,
            _workspace,
        ),
    ):
        result = run(executor, grant)
        rows = db.recent_audit()

    assert result.ok
    row = rows[0]
    assert row["actor"] == "tool"
    assert row["action"] == "tool.media.image"
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
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    with (
        tool_client(handler) as tool,
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
    assert rows[0]["action"] == "tool.media.image"
    assert rows[0]["decision"] == "deny"
