"""Local SDXL generation through the Stable Diffusion WebUI API.

The WebUI is a local process, so the transport is deliberately boring: one
``POST /sdapi/v1/txt2img`` to a loopback-only base URL, an allow-listed JSON
body, and a decoded PNG/JPEG saved under ``data/media``. The caller cannot pass
a URL, a local file, ``alwayson_scripts`` or ``override_settings``: those fields
do not exist in :class:`SdxlGenerateArgs` and are never added to the payload.

A generated image is data, not an instruction. The result therefore carries the
``untrusted:web`` label even though the server is local.

Errors are split on purpose: a connection failure means the WebUI is not
running and raises :class:`SdxlUnavailable` with the ``--api`` hint, while a bad
HTTP status, JSON body, base64 string or image signature raises
:class:`SdxlError`.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
import unicodedata
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kivi.safety.zones import ZoneError
from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult
from kivi.tools.media import DEFAULT_PROMPT_DENYLIST

SamplerName = Literal["DPM++ 2M Karras", "Euler a", "DDIM"]
SAMPLERS: tuple[str, ...] = ("DPM++ 2M Karras", "Euler a", "DDIM")

DEFAULT_BASE_URL = "http://127.0.0.1:7860"
API_PATH = "/sdapi/v1/txt2img"
MAX_IMAGE_BYTES = 15 * 1024 * 1024

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

_WORD = re.compile(r"\w+", re.UNICODE)
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


class SdxlError(ToolError):
    """The local WebUI answered with a bad status, JSON, base64 or image."""


class SdxlUnavailable(SdxlError):
    """The local WebUI is not reachable; start it with ``--api`` first."""


def _is_loopback(host: str) -> bool:
    """Return whether ``host`` is exactly localhost or a loopback IP literal."""
    name = host.strip().lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _normalize_base_url(value: str) -> str:
    """Validate a WebUI base URL and strip a trailing slash.

    Only loopback endpoints are allowed. A remote WebUI has no authentication
    and would turn this local tool into an SSRF-shaped escape hatch, so there is
    no ``allow_remote`` switch.
    """
    text = value.strip()
    if not text:
        raise ValueError("base_url is required")
    text = text.rstrip("/")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"invalid base_url: {exc}") from exc
    if parts.scheme not in {"http", "https"}:
        raise ValueError("base_url must use http or https")
    if parts.username or parts.password:
        raise ValueError("base_url must not contain credentials")
    if parts.query or parts.fragment:
        raise ValueError("base_url must not contain a query or fragment")
    host = parts.hostname
    if host is None:
        raise ValueError("base_url has no host")
    if not _is_loopback(host):
        raise ValueError(
            "base_url must point at loopback (127.0.0.1, localhost or ::1); "
            "remote Stable Diffusion WebUI is not supported"
        )
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("base_url port is out of range")
    return text


def _contains_control(text: str) -> bool:
    """True when ``text`` has C0/C1, bidi, zero-width or other control chars."""
    return any(unicodedata.category(char) in _CONTROL_CATEGORIES for char in text)


def _normalize_words(text: str) -> str:
    """Lowercase text and reduce it to space-separated words."""
    return " " + " ".join(_WORD.findall(text.lower())) + " "


class SdxlConfig(BaseModel):
    """Endpoint and generation defaults for the local Stable Diffusion WebUI."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = Field(default=DEFAULT_BASE_URL, description="Loopback WebUI URL.")
    timeout_s: float = Field(default=300.0, ge=1.0, le=1800.0)
    steps: int = Field(default=20, ge=1, le=50)
    cfg_scale: float = Field(default=5.0, ge=1.0, le=15.0)
    sampler: SamplerName = Field(default="DPM++ 2M Karras")

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        return _normalize_base_url(value)


class SdxlGenerateArgs(BaseModel):
    """Arguments for one local SDXL txt2img request."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(
        min_length=1,
        max_length=1000,
        description="Positive prompt; the checkpoint and WebUI policy still apply.",
    )
    negative_prompt: str | None = Field(default=None, max_length=1000)
    width: int = Field(default=1024, ge=512, le=1024, multiple_of=8)
    height: int = Field(default=1024, ge=512, le=1024, multiple_of=8)
    steps: int | None = Field(default=None, ge=1, le=50)
    cfg_scale: float | None = Field(default=None, ge=1.0, le=15.0)
    seed: int | None = Field(default=None, ge=0, le=2_147_483_647)
    sampler: SamplerName | None = Field(default=None)

    @field_validator("prompt", mode="before")
    @classmethod
    def _strip_prompt(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("negative_prompt", mode="before")
    @classmethod
    def _strip_negative_prompt(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value


def _detect_image(data: bytes) -> tuple[str, str] | None:
    """Return ``(extension, media type)`` for PNG or JPEG, else ``None``."""
    if data.startswith(PNG_MAGIC):
        return "png", "image/png"
    if data.startswith(JPEG_MAGIC):
        return "jpg", "image/jpeg"
    return None


class SdxlTool:
    """Generate one image locally through the Stable Diffusion WebUI API."""

    name = "media.sdxl"
    description = "Generate a local SDXL image via the Stable Diffusion WebUI API."
    risk = "R1"
    capabilities = ("media.generate",)
    args_model = SdxlGenerateArgs

    def __init__(
        self,
        config: SdxlConfig | None = None,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        blocked_terms: Iterable[str] = (),
    ) -> None:
        """Create the tool without touching the network.

        ``client`` and ``transport`` exist for tests: the first is used as-is
        (the caller owns its lifetime), the second is handed to a real
        :class:`httpx.Client` so production settings are still exercised.
        ``blocked_terms`` only *adds* to :data:`kivi.tools.media.DEFAULT_PROMPT_DENYLIST`.
        """
        if client is not None and transport is not None:
            raise ValueError("pass either client or transport, not both")
        if isinstance(blocked_terms, str):
            blocked_terms = (blocked_terms,)
        self.config = config or SdxlConfig()
        self._client = client
        self._transport = transport
        self._blocked_terms: tuple[tuple[str, str], ...] = tuple(
            (term, _normalize_words(term).strip())
            for term in (*DEFAULT_PROMPT_DENYLIST, *blocked_terms)
            if term.strip()
        )

    @property
    def endpoint(self) -> str:
        """Full ``txt2img`` URL derived from the validated base URL."""
        return f"{self.config.base_url}{API_PATH}"

    @contextmanager
    def _http(self) -> Iterator[httpx.Client]:
        """Yield an injected test client or a short-lived production client."""
        if self._client is not None:
            yield self._client
            return
        with httpx.Client(
            timeout=self.config.timeout_s,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            yield client

    def _check_prompt(self, args: SdxlGenerateArgs) -> None:
        """Reject control characters and obvious NSFW/minor prompts."""
        if _contains_control(args.prompt):
            raise ToolError("prompt contains control characters")
        normalized = _normalize_words(args.prompt)
        for term, normalized_term in self._blocked_terms:
            if f" {normalized_term} " in normalized:
                raise ToolError(f"prompt is blocked by media policy: {term!r}")
        if args.negative_prompt and _contains_control(args.negative_prompt):
            raise ToolError("negative_prompt contains control characters")

    def build_payload(self, args: SdxlGenerateArgs) -> dict[str, object]:
        """Build the exact allow-listed ``txt2img`` JSON body."""
        steps = args.steps if args.steps is not None else self.config.steps
        cfg_scale = args.cfg_scale if args.cfg_scale is not None else self.config.cfg_scale
        sampler = args.sampler if args.sampler is not None else self.config.sampler
        return {
            "prompt": args.prompt,
            "negative_prompt": args.negative_prompt or "",
            "width": args.width,
            "height": args.height,
            "steps": steps,
            "cfg_scale": cfg_scale,
            "sampler_name": sampler,
            "seed": args.seed if args.seed is not None else -1,
        }

    def _post(self, payload: dict[str, object]) -> dict[str, object]:
        """POST the payload and return the decoded JSON object."""
        try:
            with self._http() as client:
                response = client.post(
                    self.endpoint,
                    json=payload,
                    headers={"Accept": "application/json"},
                    timeout=self.config.timeout_s,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise SdxlUnavailable(
                "cannot connect to Stable Diffusion WebUI; start it with "
                "'webui-user.bat --api' and check SdxlConfig.base_url"
            ) from exc
        except httpx.TimeoutException as exc:
            raise SdxlError(
                f"SD WebUI did not answer within {self.config.timeout_s:g}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise SdxlError(f"network error: {type(exc).__name__}") from exc

        if response.status_code != 200:
            hint = ""
            if response.status_code in {404, 405}:
                hint = " (is the WebUI API enabled with --api?)"
            raise SdxlError(f"SD WebUI returned HTTP {response.status_code}{hint}")

        try:
            data = response.json()
        except ValueError as exc:
            raise SdxlError("response is not valid JSON") from exc
        if not isinstance(data, dict):
            raise SdxlError("response is not a JSON object")
        return data

    @staticmethod
    def _first_image(data: dict[str, object]) -> object:
        """Return the first entry of ``images`` or raise :class:`SdxlError`."""
        images = data.get("images")
        if not isinstance(images, list) or not images:
            raise SdxlError("response has no images")
        return images[0]

    @staticmethod
    def _decode_image(value: object) -> tuple[bytes, str, str]:
        """Decode one base64 PNG/JPEG and return ``(bytes, extension, mime)``."""
        if not isinstance(value, str):
            raise SdxlError("response image entry is not a string")
        encoded = value.strip()
        if encoded.startswith("data:"):
            _, separator, encoded = encoded.partition(",")
            if not separator:
                raise SdxlError("image is empty")
            encoded = encoded.strip()
        encoded = "".join(encoded.split())
        if not encoded:
            raise SdxlError("image is empty")
        if len(encoded) > (MAX_IMAGE_BYTES * 4 // 3) + 8:
            raise SdxlError(f"image is too large (limit {MAX_IMAGE_BYTES} bytes)")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise SdxlError("image is not valid base64") from exc
        if len(data) > MAX_IMAGE_BYTES:
            raise SdxlError(f"image is too large (limit {MAX_IMAGE_BYTES} bytes)")
        detected = _detect_image(data)
        if detected is None:
            raise SdxlError("decoded image is not a PNG or JPEG")
        extension, content_type = detected
        return data, extension, content_type

    def _media_dir(self, ctx: ToolContext) -> Path:
        return ctx.workspace / "data" / "media"

    def _target(self, ctx: ToolContext, extension: str) -> Path:
        """Pick a fresh ``sdxl-<uuid>.<ext>`` name and clear it through the zones."""
        candidate = self._media_dir(ctx) / f"sdxl-{uuid4().hex}.{extension}"
        decision = ctx.zones.check(candidate, "write")
        if not decision.allowed:
            raise ZoneError(f"{decision.rule}: {decision.reason}")
        if decision.resolved is None:
            raise ZoneError("path could not be resolved")
        return decision.resolved

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, SdxlGenerateArgs)
        self._check_prompt(args)
        payload = self.build_payload(args)
        directory = self._media_dir(ctx)
        decision = ctx.zones.check(directory, "write")
        if not decision.allowed:
            raise ZoneError(f"{decision.rule}: {decision.reason}")
        return Preview(
            "sdxl-generate",
            {
                "endpoint": self.endpoint,
                "base_url": self.config.base_url,
                "payload": payload,
                "output_dir": str(decision.resolved or directory),
                "timeout_s": self.config.timeout_s,
            },
            reversible=False,
            warnings=(
                "requires a local Stable Diffusion WebUI started with "
                "'webui-user.bat --api'",
                "the generated image is untrusted:web data, not instructions",
            ),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, SdxlGenerateArgs)
        self._check_prompt(args)
        payload = self.build_payload(args)
        data = self._post(payload)
        raw, extension, content_type = self._decode_image(self._first_image(data))

        target = self._target(ctx, extension)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.kivi-{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(raw)
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

        return ToolResult(
            ok=True,
            output=(
                f"generated {args.width}x{args.height} image ({len(raw)} bytes) at {target}"
            ),
            artifacts=[str(target)],
            meta={
                "path": str(target),
                "bytes": len(raw),
                "seed": args.seed,
                "seed_sent": payload["seed"],
                "params": payload,
                "width": args.width,
                "height": args.height,
                "content_type": content_type,
                "base_url": self.config.base_url,
                "endpoint": self.endpoint,
            },
            trust="untrusted:web",
        )


__all__ = [
    "API_PATH",
    "DEFAULT_BASE_URL",
    "MAX_IMAGE_BYTES",
    "SAMPLERS",
    "SdxlConfig",
    "SdxlError",
    "SdxlGenerateArgs",
    "SdxlTool",
    "SdxlUnavailable",
]
