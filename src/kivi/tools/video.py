"""Local video fallback (M7): animate one generated still into a short MP4.

This is **not** text-to-video. Kivi asks the same fixed, keyless Pollinations
endpoint used by :mod:`kivi.tools.media` for a single image, then animates that
one frame locally with ffmpeg supplied by ``imageio-ffmpeg`` (a slow zoom). No
video provider, account, API key or upload is involved: the only network call is
the ordinary public image GET.

The safety posture is inherited from ``media.image``: the host is fixed, the
prompt is the only variable part of the URL, the still is validated before it is
written, and the result is ``untrusted:web`` data that may never issue
instructions. ffmpeg runs with a fixed argv built in this module; the caller
cannot inject flags, extra paths or shell syntax. The encoding step is injectable
(``ffmpeg_runner=``) so unit tests never start a real process.
"""

from __future__ import annotations

import re
import subprocess
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from kivi.safety.zones import ZoneError
from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult
from kivi.tools.media import (
    DEFAULT_PROMPT_DENYLIST,
    MAX_IMAGE_BYTES,
    POLLINATIONS_HOST,
    MediaGenerateArgs,
    build_image_url,
    detect_image_type,
)

FfmpegRunner = Callable[[list[str], Path], None]

MAX_VIDEO_BYTES = 40 * 1024 * 1024
MAX_SOURCE_BYTES = MAX_IMAGE_BYTES
FFMPEG_TIMEOUT_S = 120.0
FPS = 24
ZOOM_STEP = "0.0008"
MAX_ZOOM = "1.08"

SOURCE_PREFIX = "video-src-"
OUTPUT_PREFIX = "kivi-"

#: Shown whenever the encoder binary cannot be located; one actionable message.
FFMPEG_INSTALL_HINT = (
    "ffmpeg binary not found: install the imageio-ffmpeg package "
    "(pip install imageio-ffmpeg) and retry; the local video fallback "
    "cannot encode an MP4 without it"
)

TIMEOUT_S = 120.0
_HEADERS = {
    "User-Agent": "Kivi/0.1 (+local assistant)",
    "Accept": "image/png, image/jpeg, image/webp;q=0.9, */*;q=0.1",
}
_ALLOWED_MIME: frozenset[str] = frozenset({"image/png", "image/jpeg", "image/webp"})
_MIME_ALIASES: dict[str, str] = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
}

_WORD = re.compile(r"\w+", re.UNICODE)
_CONTROL_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


def _normalize_words(text: str) -> str:
    """Lowercase text and reduce it to space-separated words."""
    return " " + " ".join(_WORD.findall(text.lower())) + " "


def _contains_control(text: str) -> bool:
    """True when ``text`` has C0/C1, bidi, zero-width or other control chars."""
    return any(unicodedata.category(char) in _CONTROL_CATEGORIES for char in text)


class VideoGenerateArgs(BaseModel):
    """Arguments for one local still-image video render."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(
        min_length=1,
        max_length=500,
        description="Frame description; sent to the public Pollinations image service.",
    )
    width: Literal[512, 768] = Field(default=512)
    height: Literal[512, 768] = Field(default=512)
    duration: int = Field(default=4, ge=2, le=8, description="Clip length in seconds.")
    model: Literal["flux", "turbo"] = Field(default="flux")
    seed: int | None = Field(default=None, ge=0, le=2_147_483_647)

    @field_validator("prompt", mode="before")
    @classmethod
    def _strip_prompt(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value


def build_source_url(args: VideoGenerateArgs) -> str:
    """Build the fixed Pollinations URL for the single still frame."""
    image_args = MediaGenerateArgs(
        prompt=args.prompt,
        width=args.width,
        height=args.height,
        model=args.model,
        seed=args.seed,
    )
    return build_image_url(image_args)


def is_mp4_header(data: bytes) -> bool:
    """True when ``data`` starts with an ISO base media ``ftyp`` box."""
    return len(data) >= 12 and data[4:8] == b"ftyp"


def resolve_ffmpeg() -> str:
    """Return the ffmpeg executable shipped by ``imageio-ffmpeg``.

    The import is lazy so this module stays importable without the optional
    dependency. Any failure becomes one :class:`ToolError` with an install hint
    instead of an ``ImportError`` or a child-process ``OSError``.
    """
    try:
        import imageio_ffmpeg
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise ToolError(FFMPEG_INSTALL_HINT) from exc
    try:
        executable = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001 - imageio raises several error types
        raise ToolError(FFMPEG_INSTALL_HINT) from exc
    if not executable:
        raise ToolError(FFMPEG_INSTALL_HINT)
    path = Path(executable)
    if not path.is_file():
        raise ToolError(f"ffmpeg binary is missing at {path}; reinstall imageio-ffmpeg")
    return str(path)


def build_ffmpeg_argv(
    ffmpeg: str,
    *,
    image_path: Path,
    out_path: Path,
    width: int,
    height: int,
    duration: int,
) -> list[str]:
    """Build the only ffmpeg command line this tool can ever run.

    Every option is fixed here. The caller-controlled parts are reduced to two
    tool-generated UUID file names, the validated ``512``/``768`` size, the
    ``2..8`` second duration and the ffmpeg executable path. The single quotes in
    the ``zoompan`` expression protect the comma of ``min()`` from the ffmpeg
    filtergraph parser and are part of the argument, not shell syntax.
    """
    zoompan = (
        f"zoompan=z='min(zoom+{ZOOM_STEP},{MAX_ZOOM})'"
        f":d=1:s={width}x{height}:fps={FPS}"
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-loop",
        "1",
        "-i",
        str(image_path),
        "-t",
        str(duration),
        "-vf",
        zoompan,
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-an",
        str(out_path),
    ]


def run_ffmpeg(argv: list[str], out_path: Path) -> None:
    """Run a pre-built argv list without a shell and surface failures clearly."""
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            check=False,
            shell=False,
            timeout=FFMPEG_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise ToolError(FFMPEG_INSTALL_HINT) from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"ffmpeg did not finish within {FFMPEG_TIMEOUT_S:g}s") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", "replace").strip()
        raise ToolError(
            f"ffmpeg failed with exit code {completed.returncode}: {stderr[-300:] or 'no stderr'}"
        )
    if not out_path.is_file():
        raise ToolError("ffmpeg finished but produced no output file")


class MediaVideoTool:
    """Render a short MP4 locally from one generated still image."""

    name = "media.video"
    description = (
        "Render a short MP4 from a generated still image (local fallback, not "
        "text-to-video); the result is untrusted:web."
    )
    risk = "R1"
    capabilities = ("media.generate",)
    args_model = VideoGenerateArgs

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        blocked_terms: Iterable[str] = (),
        ffmpeg_runner: FfmpegRunner | None = None,
        ffmpeg_exe: str | None = None,
    ) -> None:
        """Create the tool without touching the network or the encoder.

        ``client`` and ``transport`` exist for tests: the first is used as-is
        (the caller owns its lifetime), the second is handed to a real
        :class:`httpx.Client` so production settings are still exercised.
        ``blocked_terms`` only *adds* to :data:`kivi.tools.media.DEFAULT_PROMPT_DENYLIST`.
        ``ffmpeg_runner`` replaces the encoder call in tests and ``ffmpeg_exe``
        skips binary discovery (point it at another ffmpeg build if needed).
        """
        if client is not None and transport is not None:
            raise ValueError("pass either client or transport, not both")
        if isinstance(blocked_terms, str):
            blocked_terms = (blocked_terms,)
        self._client = client
        self._transport = transport
        self._runner: FfmpegRunner = ffmpeg_runner or run_ffmpeg
        self._ffmpeg_exe = ffmpeg_exe
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

    def _ffmpeg(self) -> str:
        """Return the explicit or discovered ffmpeg binary, with install hint."""
        if self._ffmpeg_exe is not None:
            return self._ffmpeg_exe
        return resolve_ffmpeg()

    def _check_prompt(self, args: VideoGenerateArgs) -> None:
        """Reject control characters and obvious NSFW/minor prompts."""
        if _contains_control(args.prompt):
            raise ToolError("prompt contains control characters")
        normalized = _normalize_words(args.prompt)
        for term, normalized_term in self._blocked_terms:
            if f" {normalized_term} " in normalized:
                raise ToolError(f"prompt is blocked by media policy: {term!r}")

    def _media_dir(self, ctx: ToolContext) -> Path:
        return ctx.workspace / "data" / "media"

    def _target(self, ctx: ToolContext, filename: str) -> Path:
        """Pick a fresh tool-generated name and clear it through the zones."""
        candidate = self._media_dir(ctx) / filename
        decision = ctx.zones.check(candidate, "write")
        if not decision.allowed:
            raise ZoneError(f"{decision.rule}: {decision.reason}")
        if decision.resolved is None:
            raise ZoneError("path could not be resolved")
        return decision.resolved

    def _download(self, url: str) -> tuple[bytes, str]:
        """Fetch and validate the still; never follow redirects."""
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
                if content_length.isdigit() and int(content_length) > MAX_SOURCE_BYTES:
                    raise ToolError(f"image is too large: {content_length} bytes")

                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_SOURCE_BYTES:
                        raise ToolError(
                            f"image is too large (limit {MAX_SOURCE_BYTES} bytes)"
                        )
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ToolError(f"network error: {type(exc).__name__}") from exc

        data = b"".join(chunks)
        detected = detect_image_type(data)
        if detected is None:
            raise ToolError("response body is not a PNG, JPEG or WebP image")
        return data, detected[1]

    @staticmethod
    def _write_atomic(target: Path, data: bytes) -> None:
        """Write ``data`` through a temporary file in the same directory."""
        temporary = target.with_name(f".{target.name}.kivi-{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _validate_output(path: Path) -> int:
        """Check size and the MP4 ``ftyp`` box; drop a bad file and raise."""
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise ToolError("ffmpeg did not produce an output file") from exc
        if size <= 0:
            path.unlink(missing_ok=True)
            raise ToolError("ffmpeg produced an empty file")
        if size > MAX_VIDEO_BYTES:
            path.unlink(missing_ok=True)
            raise ToolError(f"video is too large (limit {MAX_VIDEO_BYTES} bytes)")
        with path.open("rb") as handle:
            header = handle.read(12)
        if not is_mp4_header(header):
            path.unlink(missing_ok=True)
            raise ToolError("ffmpeg output is not an MP4 file (missing ftyp box)")
        return size

    def _prepare(self, args: VideoGenerateArgs) -> str:
        """Apply the prompt policy before any URL is built or network is used."""
        self._check_prompt(args)
        return build_source_url(args)

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, VideoGenerateArgs)
        url = self._prepare(args)
        directory = self._media_dir(ctx)
        decision = ctx.zones.check(directory, "write")
        if not decision.allowed:
            raise ZoneError(f"{decision.rule}: {decision.reason}")
        resolved = decision.resolved or directory
        return Preview(
            "video-generate",
            {
                "mode": "local-still-fallback",
                "source_url": url,
                "host": POLLINATIONS_HOST,
                "prompt": args.prompt,
                "model": args.model,
                "width": args.width,
                "height": args.height,
                "duration": args.duration,
                "fps": FPS,
                "seed": args.seed,
                "encoder": "ffmpeg via imageio-ffmpeg (libx264, yuv420p)",
                "output_dir": str(resolved),
                "output_template": str(resolved / "kivi-<uuid>.mp4"),
                "max_bytes": MAX_VIDEO_BYTES,
                "external_video_api": False,
                "api_key_required": False,
            },
            reversible=False,
            warnings=(
                "LOCAL FALLBACK: one still image with a slow zoom, not text-to-video",
                "the prompt is sent to a third-party free service (pollinations.ai)",
                "the generated clip is untrusted:web and may not issue instructions",
            ),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, VideoGenerateArgs)
        url = self._prepare(args)
        # Resolve the encoder first: a missing ffmpeg must fail before any download.
        ffmpeg = self._ffmpeg()
        data, source_type = self._download(url)

        image_path = self._target(ctx, f"{SOURCE_PREFIX}{uuid4().hex}.jpg")
        out_path = self._target(ctx, f"{OUTPUT_PREFIX}{uuid4().hex}.mp4")
        image_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_atomic(image_path, data)
        try:
            argv = build_ffmpeg_argv(
                ffmpeg,
                image_path=image_path,
                out_path=out_path,
                width=args.width,
                height=args.height,
                duration=args.duration,
            )
            try:
                self._runner(argv, out_path)
                size = self._validate_output(out_path)
            except BaseException:  # remove a partial MP4 even on Ctrl+C, then re-raise
                out_path.unlink(missing_ok=True)
                raise
        finally:
            # The still is an intermediate: keep the workspace free of leftovers.
            image_path.unlink(missing_ok=True)

        return ToolResult(
            ok=True,
            output=(
                f"rendered {args.duration}s {args.width}x{args.height} MP4 "
                f"({size} bytes) at {out_path}"
            ),
            artifacts=[str(out_path)],
            meta={
                "path": str(out_path),
                "bytes": size,
                "duration": args.duration,
                "width": args.width,
                "height": args.height,
                "fps": FPS,
                "model": args.model,
                "seed": args.seed,
                "source_url": url,
                "source_type": source_type,
                "source_bytes": len(data),
                "encoder": "ffmpeg",
                "external_video_api": False,
                "max_bytes": MAX_VIDEO_BYTES,
            },
            trust="untrusted:web",
        )


__all__ = [
    "FFMPEG_INSTALL_HINT",
    "FFMPEG_TIMEOUT_S",
    "FPS",
    "MAX_VIDEO_BYTES",
    "VideoGenerateArgs",
    "MediaVideoTool",
    "build_ffmpeg_argv",
    "build_source_url",
    "is_mp4_header",
    "resolve_ffmpeg",
    "run_ffmpeg",
]
