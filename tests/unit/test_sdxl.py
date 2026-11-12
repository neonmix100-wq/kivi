"""Unit tests for local SDXL generation through the Stable Diffusion WebUI.

No test touches the network by default: every tool is given an ``httpx.Client``
wired to a ``httpx.MockTransport`` handler that returns canned JSON and base64
bytes. The executor tests use the real permission engine, zones and audit
database. One live test probes ``127.0.0.1:7860`` and skips when the WebUI is not
running; it never starts the server.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from kivi.kernel.db import Database
from kivi.safety.permissions import Grant, PermissionEngine
from kivi.safety.zones import ProtectedZones
from kivi.tools import ToolExecutor, ToolRegistry, ToolResult
from kivi.tools.base import ToolError
from kivi.tools.sdxl import (
    MAX_IMAGE_BYTES,
    SdxlConfig,
    SdxlError,
    SdxlGenerateArgs,
    SdxlTool,
    SdxlUnavailable,
)

Handler = Callable[[httpx.Request], httpx.Response]

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
GIF = b"GIF89a" + b"\x00" * 16
PNG_B64 = base64.b64encode(PNG).decode("ascii")
JPEG_B64 = base64.b64encode(JPEG).decode("ascii")
GIF_B64 = base64.b64encode(GIF).decode("ascii")

PAYLOAD_KEYS = {
    "prompt",
    "negative_prompt",
    "width",
    "height",
    "steps",
    "cfg_scale",
    "sampler_name",
    "seed",
}


@contextmanager
def tool_client(
    handler: Handler,
    config: SdxlConfig | None = None,
    *,
    blocked_terms: tuple[str, ...] = (),
) -> Iterator[SdxlTool]:
    """Build a tool backed by a mock transport; close the client afterwards."""
    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=300.0)
    try:
        yield SdxlTool(config=config, client=client, blocked_terms=blocked_terms)
    finally:
        client.close()


@contextmanager
def executor_env(
    tmp_path: Path,
    tool: SdxlTool,
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


def ok_handler(content: bytes = PNG, record: dict[str, httpx.Request] | None = None) -> Handler:
    """Return a handler that answers with one base64 image."""

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record["request"] = request
        encoded = base64.b64encode(content).decode("ascii")
        return httpx.Response(200, json={"images": [encoded]})

    return handler


def run(
    executor: ToolExecutor,
    grant: Grant,
    args: dict[str, object] | None = None,
    **kwargs: object,
) -> ToolResult:
    return executor.run(
        "media.sdxl",
        {"prompt": "a red fox in snow", **(args or {})},
        grant=grant,
        **kwargs,
    )


def request_payload(request: httpx.Request) -> dict[str, object]:
    data = json.loads(request.content)
    assert isinstance(data, dict)
    return data


# -- config ------------------------------------------------------------------
def test_config_defaults() -> None:
    config = SdxlConfig()
    assert config.base_url == "http://127.0.0.1:7860"
    assert config.timeout_s == 300.0
    assert config.steps == 20
    assert config.cfg_scale == 5.0
    assert config.sampler == "DPM++ 2M Karras"


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://127.0.0.1:7860", "http://127.0.0.1:7860"),
        ("http://127.0.0.1:7860/", "http://127.0.0.1:7860"),
        ("http://localhost:7861/", "http://localhost:7861"),
        ("http://[::1]:7860", "http://[::1]:7860"),
        ("http://127.0.0.2:7860", "http://127.0.0.2:7860"),
    ],
)
def test_config_accepts_loopback_variants(base_url: str, expected: str) -> None:
    assert SdxlConfig(base_url=base_url).base_url == expected


@pytest.mark.parametrize(
    "base_url",
    [
        "http://example.com:7860",
        "https://192.168.1.10:7860",
        "http://10.0.0.5:7860",
        "http://0.0.0.0:7860",
        "http://foo.localhost:7860",
        "http://user:pass@127.0.0.1:7860",
        "ftp://127.0.0.1:7860",
        "http://127.0.0.1:7860/sdapi?token=secret",
        "http://127.0.0.1:99999",
        "not-a-url",
        "",
    ],
)
def test_config_rejects_non_loopback_or_malformed_urls(base_url: str) -> None:
    with pytest.raises(ValidationError):
        SdxlConfig(base_url=base_url)


@pytest.mark.parametrize(
    "payload",
    [
        {"timeout_s": 0.5},
        {"timeout_s": 1801.0},
        {"steps": 0},
        {"steps": 51},
        {"cfg_scale": 0.5},
        {"cfg_scale": 16.0},
        {"sampler": "Euler"},
        {"api_key": "secret"},
    ],
)
def test_config_rejects_invalid_values(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SdxlConfig(**payload)


# -- arguments ---------------------------------------------------------------
def test_argument_defaults() -> None:
    args = SdxlGenerateArgs(prompt="a red fox")
    assert args.width == 1024
    assert args.height == 1024
    assert args.steps is None
    assert args.cfg_scale is None
    assert args.seed is None
    assert args.sampler is None
    assert args.negative_prompt is None


def test_tool_metadata_and_endpoint() -> None:
    tool = SdxlTool()
    assert tool.name == "media.sdxl"
    assert tool.risk == "R1"
    assert tool.capabilities == ("media.generate",)
    assert tool.args_model is SdxlGenerateArgs
    assert tool.endpoint == "http://127.0.0.1:7860/sdapi/v1/txt2img"
    assert SdxlGenerateArgs.model_json_schema()["additionalProperties"] is False


def test_error_hierarchy() -> None:
    assert issubclass(SdxlError, ToolError)
    assert issubclass(SdxlUnavailable, SdxlError)


@pytest.mark.parametrize(
    "payload",
    [
        {"prompt": ""},
        {"prompt": "   "},
        {"prompt": "x" * 1001},
        {"prompt": "ok", "negative_prompt": "x" * 1001},
        {"prompt": "ok", "width": 511},
        {"prompt": "ok", "width": 1001},
        {"prompt": "ok", "width": 1025},
        {"prompt": "ok", "height": 511},
        {"prompt": "ok", "height": 1001},
        {"prompt": "ok", "height": 1025},
        {"prompt": "ok", "steps": 0},
        {"prompt": "ok", "steps": 51},
        {"prompt": "ok", "cfg_scale": 0.9},
        {"prompt": "ok", "cfg_scale": 15.1},
        {"prompt": "ok", "seed": -1},
        {"prompt": "ok", "sampler": "Euler"},
    ],
)
def test_invalid_arguments_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SdxlGenerateArgs(**payload)


@pytest.mark.parametrize(
    "extra",
    [
        {"url": "http://127.0.0.1:7860/sdapi/v1/txt2img"},
        {"path": "private/secret.txt"},
        {"image": "local.png"},
        {"alwayson_scripts": {"x": {}}},
        {"override_settings": {"sd_model_checkpoint": "other"}},
        {"init_images": []},
        {"batch_size": 4},
    ],
)
def test_no_arbitrary_payload_fields(extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SdxlGenerateArgs(prompt="ok", **extra)


def test_valid_boundaries() -> None:
    args = SdxlGenerateArgs(
        prompt="ok",
        width=512,
        height=1024,
        steps=50,
        cfg_scale=15.0,
        seed=2_147_483_647,
        sampler="DDIM",
    )
    assert args.width == 512
    assert args.height == 1024
    assert args.steps == 50
    assert args.cfg_scale == 15.0


# -- dry run -----------------------------------------------------------------
def test_dry_run_shows_payload_without_network(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be called during dry run")

    config = SdxlConfig(steps=30, cfg_scale=6.5, sampler="DDIM")
    with (
        tool_client(handler, config) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(
            executor,
            grant,
            {
                "prompt": "a lighthouse at night",
                "negative_prompt": "blurry",
                "width": 768,
                "height": 512,
                "seed": 42,
            },
            dry_run=True,
        )

    assert result.ok
    assert result.meta["dry_run"] is True
    assert result.meta["action"] == "sdxl-generate"
    preview = json.loads(result.output)
    details = preview["details"]
    assert details["endpoint"] == "http://127.0.0.1:7860/sdapi/v1/txt2img"
    assert details["payload"] == {
        "prompt": "a lighthouse at night",
        "negative_prompt": "blurry",
        "width": 768,
        "height": 512,
        "steps": 30,
        "cfg_scale": 6.5,
        "sampler_name": "DDIM",
        "seed": 42,
    }
    assert not (workspace / "data" / "media").exists()


# -- success -----------------------------------------------------------------
def test_success_png_is_saved_under_data_media(tmp_path: Path) -> None:
    with (
        tool_client(ok_handler(PNG)) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(
            executor,
            grant,
            {
                "prompt": "a red fox in snow",
                "negative_prompt": "blurry",
                "width": 768,
                "height": 512,
                "steps": 12,
                "cfg_scale": 6.0,
                "seed": 123,
                "sampler": "Euler a",
            },
        )

    assert result.ok
    assert result.trust == "untrusted:web"
    assert result.truncated is False
    assert len(result.artifacts) == 1
    path = Path(result.artifacts[0])
    assert path.is_file()
    assert path.parent == (workspace / "data" / "media").resolve()
    assert re.fullmatch(r"sdxl-[0-9a-f]{32}\.png", path.name)
    assert path.read_bytes() == PNG
    assert result.meta["bytes"] == len(PNG)
    assert result.meta["width"] == 768
    assert result.meta["height"] == 512
    assert result.meta["seed"] == 123
    assert result.meta["seed_sent"] == 123
    assert result.meta["params"]["steps"] == 12
    assert result.meta["params"]["cfg_scale"] == 6.0
    assert result.meta["params"]["sampler_name"] == "Euler a"
    assert result.meta["content_type"] == "image/png"
    assert "768x512" in result.output


def test_success_jpeg_uses_jpg_extension(tmp_path: Path) -> None:
    with (
        tool_client(ok_handler(JPEG)) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant)

    assert result.ok
    path = Path(result.artifacts[0])
    assert path.suffix == ".jpg"
    assert path.read_bytes() == JPEG
    assert result.meta["content_type"] == "image/jpeg"


def test_data_uri_base64_is_accepted(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        encoded = f"data:image/png;base64,{PNG_B64}"
        return httpx.Response(200, json={"images": [encoded]})

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant)

    assert result.ok
    assert Path(result.artifacts[0]).read_bytes() == PNG


def test_request_uses_exact_allow_listed_payload(tmp_path: Path) -> None:
    seen: dict[str, httpx.Request] = {}
    with (
        tool_client(ok_handler(PNG, seen)) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant)

    assert result.ok
    request = seen["request"]
    assert request.method == "POST"
    assert str(request.url) == "http://127.0.0.1:7860/sdapi/v1/txt2img"
    assert request.headers["content-type"].startswith("application/json")
    assert "authorization" not in request.headers
    payload = request_payload(request)
    assert set(payload) == PAYLOAD_KEYS
    assert payload == {
        "prompt": "a red fox in snow",
        "negative_prompt": "",
        "width": 1024,
        "height": 1024,
        "steps": 20,
        "cfg_scale": 5.0,
        "sampler_name": "DPM++ 2M Karras",
        "seed": -1,
    }
    assert "alwayson_scripts" not in payload
    assert "override_settings" not in payload


def test_config_defaults_apply_when_args_omit_them(tmp_path: Path) -> None:
    seen: dict[str, httpx.Request] = {}
    config = SdxlConfig(steps=30, cfg_scale=7.5, sampler="DDIM")
    with (
        tool_client(ok_handler(PNG, seen), config) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant)

    assert result.ok
    payload = request_payload(seen["request"])
    assert payload["steps"] == 30
    assert payload["cfg_scale"] == 7.5
    assert payload["sampler_name"] == "DDIM"


def test_seed_none_sends_minus_one_and_keeps_meta_seed_none(tmp_path: Path) -> None:
    seen: dict[str, httpx.Request] = {}
    with (
        tool_client(ok_handler(PNG, seen)) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant)

    assert result.ok
    assert request_payload(seen["request"])["seed"] == -1
    assert result.meta["seed"] is None
    assert result.meta["seed_sent"] == -1


def test_custom_base_url_is_used(tmp_path: Path) -> None:
    seen: dict[str, httpx.Request] = {}
    config = SdxlConfig(base_url="http://localhost:7861")
    with (
        tool_client(ok_handler(PNG, seen), config) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant)

    assert result.ok
    request = seen["request"]
    assert request.url.host == "localhost"
    assert request.url.port == 7861
    assert request.url.path == "/sdapi/v1/txt2img"
    assert result.meta["base_url"] == "http://localhost:7861"


# -- errors ------------------------------------------------------------------
def test_http_error_is_reported(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"busy")

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "HTTP 500" in result.error
    assert not (workspace / "data" / "media").exists()


def test_connect_error_is_unavailable_with_api_hint(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "cannot connect" in result.error
    assert "webui-user.bat --api" in result.error
    assert not (workspace / "data" / "media").exists()


def test_connect_timeout_is_unavailable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "cannot connect" in result.error


def test_read_timeout_is_a_regular_sdxl_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "did not answer" in result.error


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "not valid JSON" in result.error


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"images": []},
        {"images": "aGVsbG8="},
        {"images": [123]},
    ],
)
def test_missing_or_invalid_images_list_is_rejected(
    tmp_path: Path, body: dict[str, object]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "no images" in result.error or "not a string" in result.error


@pytest.mark.parametrize(
    ("encoded", "message"),
    [
        ("", "image is empty"),
        ("not base64!!!", "not valid base64"),
        (GIF_B64, "not a PNG or JPEG"),
    ],
)
def test_invalid_image_data_is_rejected(
    tmp_path: Path, encoded: str, message: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"images": [encoded]})

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert message in result.error
    assert not (workspace / "data" / "media").exists()


def test_oversized_decoded_image_is_rejected(tmp_path: Path) -> None:
    oversized = PNG + b"\x00" * MAX_IMAGE_BYTES
    encoded = base64.b64encode(oversized).decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"images": [encoded]})

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant)

    assert not result.ok
    assert "too large" in result.error
    assert not (workspace / "data" / "media").exists()


# -- prompt policy -----------------------------------------------------------
@pytest.mark.parametrize(
    "prompt",
    ["a cat\x00with a null", "a cat\u202ewith bidi", "a cat\u200bwith zero width"],
)
def test_control_characters_are_rejected_before_network(tmp_path: Path, prompt: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"images": [PNG_B64]})

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, workspace),
    ):
        result = run(executor, grant, {"prompt": prompt})

    assert not result.ok
    assert "control characters" in result.error
    assert calls == 0
    assert not (workspace / "data" / "media").exists()


def test_blocked_prompt_is_rejected_before_network(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"images": [PNG_B64]})

    with (
        tool_client(handler) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant, {"prompt": "nsfw portrait of a woman"})

    assert not result.ok
    assert "blocked by media policy" in result.error
    assert calls == 0


def test_control_characters_in_negative_prompt_are_rejected(tmp_path: Path) -> None:
    with (
        tool_client(ok_handler()) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        result = run(executor, grant, {"negative_prompt": "blurry\x07"})

    assert not result.ok
    assert "control characters" in result.error


def test_custom_blocked_terms_extend_the_defaults(tmp_path: Path) -> None:
    with (
        tool_client(ok_handler(), blocked_terms=("forbidden",)) as tool,
        executor_env(tmp_path, tool) as (executor, _db, grant, _workspace),
    ):
        custom = run(executor, grant, {"prompt": "a forbidden castle"})

    assert not custom.ok
    assert "forbidden" in custom.error


# -- executor, permissions and audit ----------------------------------------
def test_executor_audits_the_successful_r1_call(tmp_path: Path) -> None:
    with (
        tool_client(ok_handler()) as tool,
        executor_env(tmp_path, tool) as (executor, db, grant, _workspace),
    ):
        result = run(executor, grant)
        rows = db.recent_audit()

    assert result.ok
    row = rows[0]
    assert row["actor"] == "tool"
    assert row["action"] == "tool.media.sdxl"
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
        return httpx.Response(200, json={"images": [PNG_B64]})

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
    assert rows[0]["action"] == "tool.media.sdxl"
    assert rows[0]["decision"] == "deny"


# -- live, opt-in only -------------------------------------------------------
def test_live_webui_if_already_running(tmp_path: Path) -> None:
    """Generate one tiny image only when a WebUI is already on 127.0.0.1:7860."""
    base_url = "http://127.0.0.1:7860"
    try:
        with httpx.Client(timeout=1.0, trust_env=False) as client:
            probe = client.get(f"{base_url}/sdapi/v1/sd-models")
    except httpx.HTTPError:
        pytest.skip("Stable Diffusion WebUI is not running; this test never starts it")
    if probe.status_code != 200:
        pytest.skip(f"Stable Diffusion WebUI API returned HTTP {probe.status_code}")

    config = SdxlConfig(base_url=base_url, timeout_s=300.0, steps=1)
    tool = SdxlTool(config=config)
    with executor_env(tmp_path, tool) as (executor, _db, grant, _workspace):
        result = run(
            executor,
            grant,
            {"prompt": "a simple red circle", "width": 512, "height": 512, "steps": 1},
        )

    assert result.ok, result.error
    assert Path(result.artifacts[0]).is_file()
