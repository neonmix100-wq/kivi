"""Free image generation through Pollinations (M7 Cloud Media, first slice).

The tool is deliberately narrow. The host is fixed, the prompt is the only
variable part of the path, and there is no API key, no local upload and no
caller-controlled destination. A generated image is data from the internet, so
the result carries the ``untrusted:web`` label and may never issue instructions.

Pollinations is a free public service without an SLA. The response is therefore
validated before anything is written to ``data/media``: status 200, an
``image/png``, ``image/jpeg`` or ``image/webp`` content type, real magic bytes
and a hard 10 MB cap.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kivi.safety.zones import ZoneError
from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult

POLLINATIONS_HOST = "image.pollinations.ai"
_BASE_URL = f"https://{POLLINATIONS_HOST}/prompt"

MAX_IMAGE_BYTES = 10 * 1024 * 1024
TIMEOUT_S = 120.0

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"
WEBP_RIFF = b"RIFF"
WEBP_TAG = b"WEBP"

_ALLOWED_MIME: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/webp"})
_MIME_ALIASES: dict[str, str] = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
}
_HEADERS = {
    "User-Agent": "Kivi/0.1 (+local assistant)",
    "Accept": "image/png, image/jpeg, image/webp;q=0.9, */*;q=0.1",
}

# Obvious NSFW / sexual / minor-related terms. This is a blunt guardrail, not a
# classifier: it blocks the cheapest abuse, while the provider's own policy and
# the user's responsibility still apply. Callers may add terms but cannot remove
# the defaults.
DEFAULT_PROMPT_DENYLIST: tuple[str, ...] = (
    "nsfw",
    "porn",
    "porno",
    "pornography",
    "xxx",
    "hentai",
    "erotic",
    "erotica",
    "nude",
    "nudes",
    "nudity",
    "naked",
    "sexual",
    "sex",
    "explicit sex",
    "genitals",
    "genitalia",
    "fetish",
    "bdsm",
    "rape",
    "incest",
    "bestiality",
    "child",
    "children",
    "kid",
    "kids",
    "minor",
    "minors",
    "underage",
    "teen",
    "teenager",
    "loli",
    "shota",
    "cp",
)

_WORD = re.compile(r"\w+", re.UNICODE)
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def _normalize_words(text: str) -> str:
    """Lowercase text and reduce it to space-separated words."""
    return " " + " ".join(_WORD.findall(text.lower())) + " "


def _contains_control(text: str) -> bool:
    """True when ``text`` has C0/C1, bidi, zero-width or other control chars."""
    return any(unicodedata.category(char) in _CONTROL_CATEGORIES for char in text)


class MediaGenerateArgs(BaseModel):
    """Arguments for one free image generation request."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(
        min_length=1,
        max_length=500,
        description="Image description; sent to the public Pollinations service.",
    )
    width: int = Field(default=1024, ge=256, le=1024)
    height: int = Field(default=1024, ge=256, le=1024)
    model: Literal["flux", "turbo"] = Field(default="flux")
    seed: int | None = Field(default=None, ge=0, le=2_147_483_647)
    negative_prompt: str | None = Field(default=None, max_length=500)

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


def build_image_url(args: MediaGenerateArgs) -> str:
    """Build the only URL this tool can ever request (fixed Pollinations host)."""
    params: dict[str, str] = {
        "width": str(args.width),
        "height": str(args.height),
        "model": args.model,
        "nologo": "true",
    }
    if args.seed is not None:
        params["seed"] = str(args.seed)
    if args.negative_prompt:
        params["negative_prompt"] = args.negative_prompt
    url = f"{_BASE_URL}/{quote(args.prompt, safe='')}?{urlencode(params)}"
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != POLLINATIONS_HOST:
        raise ToolError("refusing to request a host other than Pollinations")
    return url


def detect_image_type(data: bytes) -> tuple[str, str] | None:
    """Return ``(extension, media type)`` from magic bytes, or ``None``."""
    if data.startswith(PNG_MAGIC):
        return "png", "image/png"
    if data.startswith(JPEG_MAGIC):
        return "jpg", "image/jpeg"
    if len(data) >= 12 and data[:4] == WEBP_RIFF and data[8:12] == WEBP_TAG:
        return "webp", "image/webp"
    return None


class MediaImageTool:
    """Generate a free image with Pollinations and save it under ``data/media``."""

    name = "media.image"
    description = "Generate a free image via Pollinations; the image is untrusted:web."
    risk = "R1"
    capabilities = ("media.generate",)
    args_model = MediaGenerateArgs

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        blocked_terms: Iterable[str] = (),
    ) -> None:
        """Create the tool.

        ``client`` and ``transport`` exist for tests: the first is used as-is
        (the caller owns its lifetime), the second is handed to a real
        :class:`httpx.Client` so production settings are still exercised.
        ``blocked_terms`` only *adds* to :data:`DEFAULT_PROMPT_DENYLIST`.
        """
        if client is not None and transport is not None:
            raise ValueError("pass either client or transport, not both")
        if isinstance(blocked_terms, str):
            blocked_terms = (blocked_terms,)
        self._client = client
        self._transport = transport
        self._blocked_terms: tuple[tuple[str, str], ...] = tuple(
            (term, _normalize_words(term).strip())
            for term in (*DEFAULT_PROMPT_DENYLIST, *blocked_terms)
            if term.strip()
        )

    @contextmanager
    def _http(self) -> Iterator[httpx.Client]:
        """Yield an injected test client or a short-lived production client."""
        if self._client is not None:
            yield self._client
            return
        with httpx.Client(
            timeout=TIMEOUT_S,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            yield client

    def _check_prompt(self, args: MediaGenerateArgs) -> None:
        """Reject control characters and obvious NSFW/minor prompts."""
        if _contains_control(args.prompt):
            raise ToolError("prompt contains control characters")
        normalized = _normalize_words(args.prompt)
        for term, normalized_term in self._blocked_terms:
            if f" {normalized_term} " in normalized:
                raise ToolError(f"prompt is blocked by media policy: {term!r}")
        if args.negative_prompt and _contains_control(args.negative_prompt):
            raise ToolError("negative_prompt contains control characters")

    def _media_dir(self, ctx: ToolContext) -> Path:
        return ctx.workspace / "data" / "media"

    def _target(self, ctx: ToolContext, extension: str) -> Path:
        """Pick a fresh ``<uuid>.<ext>`` name and clear it through the zones."""
        candidate = self._media_dir(ctx) / f"{uuid4().hex}.{extension}"
        decision = ctx.zones.check(candidate, "write")
        if not decision.allowed:
            raise ZoneError(f"{decision.rule}: {decision.reason}")
        if decision.resolved is None:
            raise ZoneError("path could not be resolved")
        return decision.resolved

    def _download(self, url: str) -> tuple[bytes, str]:
        """Fetch and validate the image bytes; never follow redirects."""
        try:
            with (
                self._http() as client,
                client.stream("GET", url, headers=_HEADERS) as response,
            ):
                status = response.status_code
                if 300 <= status < 400:
                    raise ToolError("redirects are not followed")
                if status != 200:
                    raise ToolError(f"HTTP {status}")

                declared = response.headers.get("content-type", "")
                content_type = declared.split(";", 1)[0].strip().lower()
                content_type = _MIME_ALIASES.get(content_type, content_type)
                if not content_type.startswith("image/"):
                    raise ToolError(f"unsupported content type: {content_type or 'unknown'}")
                if content_type not in _ALLOWED_MIME:
                    raise ToolError(f"unsupported image type: {content_type}")

                content_length = response.headers.get("content-length", "")
                if content_length.isdigit():
                    try:
                        declared_size = int(content_length)
                    except ValueError:  # absurdly long digit string
                        declared_size = 0
                    if declared_size > MAX_IMAGE_BYTES:
                        raise ToolError(f"image is too large: {content_length} bytes")

                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_IMAGE_BYTES:
                        raise ToolError(f"image is too large (limit {MAX_IMAGE_BYTES} bytes)")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ToolError(f"network error: {type(exc).__name__}") from exc
        return b"".join(chunks), content_type

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, MediaGenerateArgs)
        url = self._prepare(args)
        directory = self._media_dir(ctx)
        decision = ctx.zones.check(directory, "write")
        if not decision.allowed:
            raise ZoneError(f"{decision.rule}: {decision.reason}")
        return Preview(
            "media-generate",
            {
                "url": url,
                "host": POLLINATIONS_HOST,
                "prompt": args.prompt,
                "model": args.model,
                "width": args.width,
                "height": args.height,
                "seed": args.seed,
                "negative_prompt": args.negative_prompt,
                "output_dir": str(decision.resolved or directory),
                "api_key_required": False,
            },
            reversible=False,
            warnings=(
                "the prompt is sent to a third-party free service (pollinations.ai)",
                "the generated image is untrusted:web and may not issue instructions",
            ),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, MediaGenerateArgs)
        url = self._prepare(args)
        data, content_type = self._download(url)
        detected = detect_image_type(data)
        if detected is None:
            raise ToolError("response body is not a PNG, JPEG or WebP image")
        extension, detected_type = detected

        target = self._target(ctx, extension)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.kivi-{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)

        return ToolResult(
            ok=True,
            output=(f"generated {args.width}x{args.height} image ({len(data)} bytes) at {target}"),
            artifacts=[str(target)],
            meta={
                "path": str(target),
                "url": url,
                "bytes": len(data),
                "width": args.width,
                "height": args.height,
                "model": args.model,
                "seed": args.seed,
                "content_type": content_type,
                "detected_type": detected_type,
            },
            trust="untrusted:web",
        )

    def _prepare(self, args: MediaGenerateArgs) -> str:
        """Apply the prompt policy before any URL is built or network is used."""
        self._check_prompt(args)
        return build_image_url(args)


__all__ = [
    "DEFAULT_PROMPT_DENYLIST",
    "MAX_IMAGE_BYTES",
    "POLLINATIONS_HOST",
    "TIMEOUT_S",
    "MediaGenerateArgs",
    "MediaImageTool",
    "build_image_url",
    "detect_image_type",
]
