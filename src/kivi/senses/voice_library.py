"""Local, consent-gated voice reference library.

A voice reference is a short recording plus its transcript.  Qwen3-TTS clones
that reference for synthesis; future engines can reuse the same profile.  Every
voice lives in its own directory::

    data/voices/
        index.json          # sorted metadata, written atomically
        active.json         # the selected voice id
        <id>/
            profile.json    # one VoiceProfile
            reference.wav   # 24 kHz mono PCM16
            reference.txt   # transcript (Qwen3-TTS reads this sidecar)
            original.<ext>  # optional archived source (``copy=True``)

Safety rules:

* every import requires an explicit ``consent=True``;
* audio is decoded and re-encoded to 24 kHz mono PCM16 WAV, at most 120 s;
* URLs must be HTTPS, public, credential-free and limited to 20 MB;
* metadata never stores URL credentials or sensitive query parameters;
* every filesystem path is resolved and checked against the library root.

``librosa``, ``soundfile`` and ``httpx`` are imported lazily, so importing this
module is cheap and tests need neither a network nor an audio codec.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

TARGET_SAMPLE_RATE = 24000
MAX_DURATION_S = 120.0
MAX_AUDIO_BYTES = 20 * 1024 * 1024
REFERENCE_NAME = "reference.wav"
TRANSCRIPT_NAME = "reference.txt"
NEEDS_TRANSCRIPT_NOTE = "transcript missing"
DEFAULT_ENGINE = "qwen"
INDEX_VERSION = 1

Engine = Literal["qwen", "fish", "silero"]
Source = Literal["file", "url", "design"]

ENGINES: tuple[Engine, ...] = ("qwen", "fish", "silero")
SOURCES: tuple[Source, ...] = ("file", "url", "design")
AUDIO_EXTENSIONS = frozenset(
    {
        "wav",
        "wave",
        "flac",
        "ogg",
        "oga",
        "opus",
        "mp3",
        "m4a",
        "aac",
        "aiff",
        "aif",
        "aifc",
        "wma",
        "webm",
    }
)
LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".lan")
ALLOWED_URL_PORTS = (None, 80, 443)
SENSITIVE_QUERY = re.compile(
    r"(token|secret|signature|sig|key|auth|password|passwd|credential|session|expires|policy)",
    re.IGNORECASE,
)
DOWNLOAD_HEADERS = {
    "User-Agent": "Kivi/0.1 (local voice library)",
    "Accept": "audio/*, application/octet-stream;q=0.5",
}
DOWNLOAD_TIMEOUT_S = 30.0
MAX_NAME_CHARS = 80
MAX_SLUG_CHARS = 48
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_PROFILE_FIELDS = (
    "id",
    "name",
    "engine",
    "reference_wav",
    "transcript",
    "source",
    "source_url",
    "consent",
    "created_at",
    "duration_s",
    "notes",
)
_CYRILLIC = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "kh",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "shch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
        "і": "i",
        "ї": "yi",
        "є": "ye",
        "ґ": "g",
    }
)

Transcriber = Callable[[Path], str]
Downloader = Callable[[str], bytes]
Resolver = Callable[[str], Sequence[str]]


class VoiceLibraryError(RuntimeError):
    """Raised when a voice profile cannot be imported, read or updated."""


@dataclass(frozen=True, slots=True)
class NormalizedAudio:
    """A normalized reference WAV and how it was produced."""

    path: Path
    duration_s: float
    trimmed: bool


def _clean_name(name: str) -> str:
    """Validate a human-readable voice name and collapse its whitespace."""
    if not isinstance(name, str):
        raise VoiceLibraryError("voice name must be a string")
    cleaned = " ".join(name.split())
    if not cleaned:
        raise VoiceLibraryError("voice name must not be empty")
    if len(cleaned) > MAX_NAME_CHARS:
        raise VoiceLibraryError(f"voice name must be at most {MAX_NAME_CHARS} characters")
    if any(ord(char) < 32 for char in cleaned):
        raise VoiceLibraryError("voice name must not contain control characters")
    return cleaned


def _slugify(name: str) -> str:
    """Build a lowercase ASCII id from a name, falling back to a short hash."""
    lowered = _clean_name(name).casefold().translate(_CYRILLIC)
    normalized = unicodedata.normalize("NFKD", lowered)
    ascii_text = "".join(char for char in normalized if not unicodedata.combining(char))
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")[:MAX_SLUG_CHARS].strip("-")
    if slug:
        return slug
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    return f"voice-{digest}"


def _now() -> str:
    """Return the current UTC time as an ISO 8601 ``Z`` timestamp."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require_consent(consent: object) -> None:
    """Reject anything but an explicit ``consent=True``."""
    if consent is not True:
        raise VoiceLibraryError(
            "explicit consent=True is required to import a voice reference"
        )


def _validated_engine(value: object) -> Engine:
    """Return a supported engine name or raise :class:`VoiceLibraryError`."""
    if not isinstance(value, str) or value not in ENGINES:
        raise VoiceLibraryError(f"unsupported voice engine: {value!r}")
    return cast(Engine, value)


def _validated_source(value: object) -> Source:
    """Return a supported profile source or raise :class:`VoiceLibraryError`."""
    if not isinstance(value, str) or value not in SOURCES:
        raise VoiceLibraryError(f"unsupported voice source: {value!r}")
    return cast(Source, value)


def _validated_reference(reference: object, profile_id: str) -> str:
    """Check that ``reference`` is a relative path inside the profile folder."""
    if not isinstance(reference, str) or not reference.strip():
        raise VoiceLibraryError("reference_wav must be a non-empty relative path")
    pure = PurePosixPath(reference)
    if pure.is_absolute() or any(part in ("..", ".") for part in pure.parts):
        raise VoiceLibraryError("reference_wav must not escape the voice folder")
    if len(pure.parts) < 2 or pure.parts[0] != profile_id:
        raise VoiceLibraryError("reference_wav must live under the profile id folder")
    return reference


def _validated_timestamp(value: object) -> str:
    """Validate an ISO 8601 timestamp string."""
    if not isinstance(value, str) or not value.strip():
        raise VoiceLibraryError("created_at must be an ISO 8601 timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VoiceLibraryError("created_at must be an ISO 8601 timestamp") from exc
    return value


def _validated_duration(value: object) -> float:
    """Validate a profile duration in seconds."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise VoiceLibraryError("duration_s must be a number")
    duration = float(value)
    if duration < 0 or duration > MAX_DURATION_S + 0.5:
        raise VoiceLibraryError("duration_s is outside the supported range")
    return duration


def _validated_source_url(value: object) -> str:
    """Validate a stored source URL; credentials are never allowed."""
    if not isinstance(value, str):
        raise VoiceLibraryError("source_url must be a string")
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        username = parts.username
        password = parts.password
    except ValueError as exc:
        raise VoiceLibraryError("source_url is not a valid URL") from exc
    if parts.scheme != "https":
        raise VoiceLibraryError("source_url must use https")
    if username is not None or password is not None:
        raise VoiceLibraryError("source_url must not contain credentials")
    return value


def _text_field(value: object, field: str) -> str:
    """Validate a plain text profile field."""
    if not isinstance(value, str):
        raise VoiceLibraryError(f"{field} must be a string")
    return value


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    """Metadata for one imported voice reference.

    ``reference_wav`` is a POSIX path relative to the library root, for example
    ``"kivi-warm/reference.wav"``.  ``consent`` records that the caller owned or
    was authorised to use the voice on the import date.
    """

    id: str
    name: str
    engine: Engine = DEFAULT_ENGINE
    reference_wav: str = ""
    transcript: str = ""
    source: Source = "file"
    source_url: str = ""
    consent: bool = False
    created_at: str = ""
    duration_s: float = 0.0
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _SLUG_RE.match(self.id):
            raise VoiceLibraryError(f"invalid voice id: {self.id!r}")
        if not isinstance(self.name, str) or not self.name.strip():
            raise VoiceLibraryError("voice name must not be empty")
        if self.engine not in ENGINES:
            raise VoiceLibraryError(f"unsupported voice engine: {self.engine!r}")
        if self.source not in SOURCES:
            raise VoiceLibraryError(f"unsupported voice source: {self.source!r}")
        if not isinstance(self.consent, bool):
            raise VoiceLibraryError("consent must be a boolean")
        self._validate_text_fields()
        _validated_reference(self.reference_wav, self.id)
        _validated_duration(self.duration_s)

    def _validate_text_fields(self) -> None:
        for field in ("transcript", "source_url", "created_at", "notes"):
            if not isinstance(getattr(self, field), str):
                raise VoiceLibraryError(f"{field} must be a string")

    @property
    def has_transcript(self) -> bool:
        """Whether the profile can be used for Qwen3-TTS voice cloning."""
        return bool(self.transcript.strip())

    def wav_path(self, root: Path | str) -> Path:
        """Return the absolute reference path under a library root."""
        return Path(root) / PurePosixPath(self.reference_wav)

    def as_dict(self) -> dict[str, Any]:
        """Return the profile as a JSON-friendly mapping."""
        return {
            "id": self.id,
            "name": self.name,
            "engine": self.engine,
            "reference_wav": self.reference_wav,
            "transcript": self.transcript,
            "source": self.source,
            "source_url": self.source_url,
            "consent": self.consent,
            "created_at": self.created_at,
            "duration_s": self.duration_s,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> VoiceProfile:
        """Rebuild and validate a profile from stored metadata."""
        if not isinstance(data, Mapping):
            raise VoiceLibraryError("voice profile must be a JSON object")
        missing = [field for field in _PROFILE_FIELDS if field not in data]
        if missing:
            raise VoiceLibraryError(f"voice profile is missing fields: {', '.join(missing)}")
        unknown = sorted(set(data) - set(_PROFILE_FIELDS))
        if unknown:
            raise VoiceLibraryError(f"voice profile has unknown fields: {', '.join(unknown)}")
        profile_id = data["id"]
        if not isinstance(profile_id, str):
            raise VoiceLibraryError("voice profile id must be a string")
        reference = data["reference_wav"]
        if not isinstance(reference, str):
            raise VoiceLibraryError("reference_wav must be a string")
        if data["consent"] is not True:
            raise VoiceLibraryError("stored voice profile lacks recorded consent")
        return cls(
            id=profile_id,
            name=_clean_name(data["name"]),
            engine=_validated_engine(data["engine"]),
            reference_wav=_validated_reference(reference, profile_id),
            transcript=_text_field(data["transcript"], "transcript"),
            source=_validated_source(data["source"]),
            source_url=_validated_source_url(data["source_url"]),
            consent=True,
            created_at=_validated_timestamp(data["created_at"]),
            duration_s=_validated_duration(data["duration_s"]),
            notes=_text_field(data["notes"], "notes"),
        )


def _append_note(notes: str, note: str) -> str:
    """Append a machine-readable note without duplicating it."""
    parts = [part.strip() for part in notes.split(";") if part.strip()]
    if note not in parts:
        parts.append(note)
    return "; ".join(parts)


def _clean_notes(notes: str) -> str:
    """Normalize a free-form notes string into ``a; b`` form."""
    return "; ".join(part.strip() for part in notes.split(";") if part.strip())


def _remove_note(notes: str, note: str) -> str:
    """Drop one specific note from a notes string."""
    parts = [part.strip() for part in notes.split(";") if part.strip() and part.strip() != note]
    return "; ".join(parts)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8 text through a same-directory temporary file and replace."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VoiceLibraryError(f"cannot create {path.parent}: {exc}") from exc
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
    except OSError as exc:
        raise VoiceLibraryError(f"cannot prepare {path.name}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except OSError as exc:
        with suppress(OSError):
            os.unlink(temp_name)
        raise VoiceLibraryError(f"cannot write {path.name}: {exc}") from exc


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Serialize ``payload`` and write it atomically."""
    try:
        data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    except (TypeError, ValueError) as exc:
        raise VoiceLibraryError(f"cannot serialize {path.name}: {exc}") from exc
    _atomic_write_text(path, data)


def _ip_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Return the parsed IP address, or ``None`` when the value is a hostname."""
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def normalize_audio(src: Path | str, dst: Path | str) -> NormalizedAudio:
    """Decode ``src`` and write ``dst`` as 24 kHz mono PCM16 WAV.

    The output is trimmed to :data:`MAX_DURATION_S`; both the input and the
    output are capped at :data:`MAX_AUDIO_BYTES`.  A missing decoder, a file
    that is not audio, or an empty recording raises :class:`VoiceLibraryError`.
    """
    source = Path(src)
    target = Path(dst)
    if not source.is_file():
        raise VoiceLibraryError(f"audio file not found: {source}")
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise VoiceLibraryError(f"cannot read audio file: {exc}") from exc
    if size <= 0:
        raise VoiceLibraryError("audio file is empty")
    if size > MAX_AUDIO_BYTES:
        raise VoiceLibraryError("audio file exceeds 20 MB; trim it before importing")
    try:
        import librosa
        import numpy as np
        import soundfile
    except ImportError as exc:
        raise VoiceLibraryError(
            "audio normalization requires librosa, soundfile and numpy"
        ) from exc
    source_duration = 0.0
    with suppress(Exception):
        info = soundfile.info(str(source))
        if info.samplerate > 0:
            source_duration = info.frames / float(info.samplerate)
    try:
        samples, rate = librosa.load(
            str(source),
            sr=TARGET_SAMPLE_RATE,
            mono=True,
            duration=MAX_DURATION_S,
        )
    except Exception as exc:
        raise VoiceLibraryError(
            f"cannot decode audio as a voice reference: {type(exc).__name__}"
        ) from exc
    if int(rate) != TARGET_SAMPLE_RATE:
        raise VoiceLibraryError("audio decoder ignored the requested sample rate")
    if samples is None or len(samples) == 0:
        raise VoiceLibraryError("audio contains no samples")
    clipped = np.clip(np.asarray(samples, dtype=np.float32).reshape(-1), -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    duration = len(pcm) / float(TARGET_SAMPLE_RATE)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise VoiceLibraryError(f"cannot create {target.parent}: {exc}") from exc
    temp = target.with_name(f".{target.name}.part")
    try:
        soundfile.write(
            str(temp),
            pcm,
            TARGET_SAMPLE_RATE,
            subtype="PCM_16",
            format="WAV",
        )
        if temp.stat().st_size > MAX_AUDIO_BYTES:
            raise VoiceLibraryError("normalized audio exceeds 20 MB")
        os.replace(temp, target)
    except VoiceLibraryError:
        raise
    except Exception as exc:
        raise VoiceLibraryError(f"cannot write normalized audio: {exc}") from exc
    finally:
        if temp.exists():
            with suppress(OSError):
                temp.unlink()
    trimmed = source_duration > MAX_DURATION_S or (
        source_duration == 0.0 and duration >= MAX_DURATION_S - 0.01
    )
    return NormalizedAudio(path=target, duration_s=round(duration, 3), trimmed=trimmed)


def _default_resolver(host: str) -> list[str]:
    """Resolve a hostname to IP strings through the system resolver."""
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _parse_https_url(url: str) -> SplitResult:
    """Validate an HTTPS URL shape without touching the network."""
    raw = str(url or "").strip()
    if not raw:
        raise VoiceLibraryError("URL must not be empty")
    try:
        parts = urlsplit(raw)
        port = parts.port
        username = parts.username
        password = parts.password
    except ValueError as exc:
        raise VoiceLibraryError(f"invalid URL: {exc}") from exc
    if parts.scheme.lower() != "https":
        raise VoiceLibraryError("only https URLs are allowed")
    if username is not None or password is not None:
        raise VoiceLibraryError("credentials in the URL are not allowed")
    if not parts.hostname:
        raise VoiceLibraryError("URL has no host")
    if port not in ALLOWED_URL_PORTS:
        raise VoiceLibraryError("only standard ports 80 and 443 are allowed")
    return parts


def _assert_public_host(host: str, *, resolver: Resolver) -> None:
    """Reject localhost names, private literals and hostnames resolving private."""
    literal = _ip_address(host)
    if literal is not None:
        if not literal.is_global:
            raise VoiceLibraryError(f"private or local address is not allowed: {host}")
        return
    lowered = host.casefold().rstrip(".")
    if lowered in LOCAL_HOSTNAMES or lowered.endswith(LOCAL_SUFFIXES):
        raise VoiceLibraryError(f"localhost names are not allowed: {host}")
    try:
        addresses = resolver(host)
    except OSError as exc:
        raise VoiceLibraryError(f"cannot resolve host: {host}") from exc
    if not addresses:
        raise VoiceLibraryError(f"cannot resolve host: {host}")
    for address in addresses:
        parsed = _ip_address(str(address))
        if parsed is None:
            raise VoiceLibraryError(f"invalid resolved address for {host}")
        if not parsed.is_global:
            raise VoiceLibraryError(f"{host} resolves to a private or local address")


def _sanitize_source_url(parts: SplitResult) -> str:
    """Rebuild a source URL without credentials, fragment or secret query keys."""
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not SENSITIVE_QUERY.search(key)
        ]
    )
    return urlunsplit(("https", host, parts.path or "/", query, ""))


def _url_extension(url: str) -> str | None:
    """Return the lowercase URL path extension without the dot."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    suffix = PurePosixPath(path).suffix.lower().lstrip(".")
    return suffix or None


def _sniff_suffix(body: bytes) -> str:
    """Guess a file suffix from audio magic bytes, defaulting to ``.bin``."""
    if body[:4] == b"RIFF" and body[8:12] == b"WAVE":
        return ".wav"
    if body[:4] == b"fLaC":
        return ".flac"
    if body[:4] == b"OggS":
        return ".ogg"
    if body[:3] == b"ID3":
        return ".mp3"
    if len(body) > 1 and body[0] == 0xFF and body[1] & 0xE0 == 0xE0:
        return ".mp3"
    if body[:4] == b"FORM":
        return ".aiff"
    return ".bin"


def _audio_suffix(url: str, body: bytes) -> str:
    """Pick a staging suffix from the URL path, then from the payload magic."""
    suffix = _url_extension(url)
    if suffix in AUDIO_EXTENSIONS:
        return f".{suffix}"
    return _sniff_suffix(body)


def _check_content_type(url: str, content_type: str) -> None:
    """Accept ``audio/*`` or a whitelisted audio filename extension."""
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    extension = _url_extension(url)
    if ctype.startswith("audio/") or extension in AUDIO_EXTENSIONS:
        return
    raise VoiceLibraryError(
        f"unsupported content type for a voice reference: {ctype or 'unknown'}"
    )


def _fetch_audio(url: str) -> tuple[bytes, str]:
    """Download at most 20 MB of audio and return the body and content type."""
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - httpx is a core dependency
        raise VoiceLibraryError("httpx is required to download voice references") from exc
    chunks: list[bytes] = []
    total = 0
    content_type = ""
    try:
        with (
            httpx.Client(timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=False) as client,
            client.stream("GET", url, headers=DOWNLOAD_HEADERS) as response,
        ):
            status = response.status_code
            if 300 <= status < 400:
                raise VoiceLibraryError("redirects are not followed for voice downloads")
            if status != 200:
                raise VoiceLibraryError(f"download returned HTTP {status}")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
                raise VoiceLibraryError("download exceeds 20 MB; trim it before importing")
            content_type = response.headers.get("content-type", "")
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_AUDIO_BYTES:
                    raise VoiceLibraryError(
                        "download exceeds 20 MB; trim it before importing"
                    )
                chunks.append(chunk)
    except VoiceLibraryError:
        raise
    except httpx.HTTPError as exc:
        raise VoiceLibraryError(f"network error: {type(exc).__name__}") from exc
    return b"".join(chunks), content_type


def _default_downloader(url: str) -> bytes:
    """Download a public HTTPS audio file, enforcing the content-type policy."""
    body, content_type = _fetch_audio(url)
    _check_content_type(url, content_type)
    return body


class VoiceLibrary:
    """Consent-gated voice reference store rooted at ``root``.

    Mutating operations rewrite ``index.json`` from the profile folders, so the
    index always matches the files on disk and a stale index repairs itself.
    Reads never download or import anything.
    """

    def __init__(self, root: Path = Path("data/voices")) -> None:
        self.root = Path(root).expanduser().resolve()
        self.index_path = self.root / "index.json"
        self.active_path = self.root / "active.json"

    # -- import ---------------------------------------------------------

    def add_file(
        self,
        path: Path | str,
        name: str,
        *,
        transcript: str = "",
        consent: bool,
        transcriber: Transcriber | None = None,
        copy: bool = True,
        engine: str = DEFAULT_ENGINE,
        notes: str = "",
    ) -> VoiceProfile:
        """Import a local audio file as a consented voice reference.

        ``copy=True`` keeps the source bytes as ``original.<ext>`` next to the
        normalized reference; ``copy=False`` stores only ``reference.wav``.
        When ``transcript`` is empty and ``transcriber`` is provided, the
        transcriber is called with the normalized WAV path.
        """
        _require_consent(consent)
        source = Path(path)
        self._validate_source_file(source)
        engine_value = self._validated_engine(engine)
        profile_id = self._prepare_identity(name)
        profile_dir = self._create_profile_dir(profile_id)
        try:
            if copy:
                self._archive_original(source, profile_dir)
            normalized = normalize_audio(source, profile_dir / REFERENCE_NAME)
            return self._finish_import(
                profile_id=profile_id,
                name=name,
                engine=engine_value,
                source="file",
                source_url="",
                transcript=transcript,
                transcriber=transcriber,
                notes=notes,
                normalized=normalized,
            )
        except BaseException:
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise

    def add_url(
        self,
        url: str,
        name: str,
        *,
        transcript: str = "",
        consent: bool,
        transcriber: Transcriber | None = None,
        downloader: Downloader | None = None,
        resolver: Resolver | None = None,
        engine: str = DEFAULT_ENGINE,
        notes: str = "",
    ) -> VoiceProfile:
        """Download a public HTTPS audio reference and import it.

        ``downloader(url) -> bytes`` is injectable for tests.  An injected
        downloader cannot report a content type, so the URL must carry a
        whitelisted audio extension.  Redirects are never followed.
        """
        _require_consent(consent)
        parts = _parse_https_url(url)
        _assert_public_host(parts.hostname or "", resolver=resolver or _default_resolver)
        source_url = _sanitize_source_url(parts)
        engine_value = self._validated_engine(engine)
        if downloader is not None and _url_extension(url) not in AUDIO_EXTENSIONS:
            raise VoiceLibraryError(
                "URL has no supported audio extension and an injected downloader "
                "cannot report a content type"
            )
        profile_id = self._prepare_identity(name)
        fetch = downloader or _default_downloader
        try:
            payload = fetch(url)
        except VoiceLibraryError:
            raise
        except Exception as exc:
            raise VoiceLibraryError(f"download failed: {type(exc).__name__}") from exc
        if not isinstance(payload, bytes | bytearray):
            raise VoiceLibraryError("downloader must return bytes")
        body = bytes(payload)
        if not body:
            raise VoiceLibraryError("download returned no data")
        if len(body) > MAX_AUDIO_BYTES:
            raise VoiceLibraryError("download exceeds 20 MB; trim it before importing")
        profile_dir = self._create_profile_dir(profile_id)
        staged = profile_dir / f"download{_audio_suffix(url, body)}"
        try:
            staged.write_bytes(body)
            normalized = normalize_audio(staged, profile_dir / REFERENCE_NAME)
            return self._finish_import(
                profile_id=profile_id,
                name=name,
                engine=engine_value,
                source="url",
                source_url=source_url,
                transcript=transcript,
                transcriber=transcriber,
                notes=notes,
                normalized=normalized,
            )
        except BaseException:
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise
        finally:
            with suppress(OSError):
                staged.unlink(missing_ok=True)

    def _finish_import(
        self,
        *,
        profile_id: str,
        name: str,
        engine: Engine,
        source: Source,
        source_url: str,
        transcript: str,
        transcriber: Transcriber | None,
        notes: str,
        normalized: NormalizedAudio,
    ) -> VoiceProfile:
        """Transcribe when needed, build the profile and persist it."""
        profile_dir = self._profile_dir(profile_id)
        text = _text_field(transcript, "transcript").strip()
        if not text and transcriber is not None:
            text = self._transcribe(profile_dir / REFERENCE_NAME, transcriber)
        final_notes = _clean_notes(_text_field(notes, "notes"))
        if not text:
            final_notes = _append_note(final_notes, NEEDS_TRANSCRIPT_NOTE)
        profile = VoiceProfile(
            id=profile_id,
            name=_clean_name(name),
            engine=engine,
            reference_wav=f"{profile_id}/{REFERENCE_NAME}",
            transcript=text,
            source=source,
            source_url=source_url,
            consent=True,
            created_at=_now(),
            duration_s=normalized.duration_s,
            notes=final_notes,
        )
        self._write_profile(profile)
        return profile

    def _transcribe(self, wav: Path, transcriber: Transcriber) -> str:
        """Call an injected transcriber and normalize its result to text."""
        try:
            result = transcriber(wav)
        except Exception as exc:
            raise VoiceLibraryError(f"transcription failed: {type(exc).__name__}") from exc
        if result is None:
            return ""
        if not isinstance(result, str):
            raise VoiceLibraryError("transcriber must return a string")
        return result.strip()

    # -- read -----------------------------------------------------------

    def list(self) -> list[VoiceProfile]:
        """Return all consented profiles, sorted by id."""
        indexed = self._load_index()
        if indexed is not None:
            return indexed
        scanned = self._scan_profiles()
        if scanned:
            with suppress(OSError, VoiceLibraryError):
                self._write_index(scanned)
        return scanned

    def get(self, name_or_id: str) -> VoiceProfile | None:
        """Find a profile by id or case-insensitive name; ``None`` if absent."""
        return self._find(name_or_id)

    def active(self) -> VoiceProfile | None:
        """Return the selected profile, or ``None`` when nothing is selected."""
        active_id = self._active_id()
        if active_id is None:
            return None
        return self.get(active_id)

    def reference_path(self, profile_or_name: VoiceProfile | str) -> Path:
        """Return the absolute reference WAV path for a profile or a name."""
        profile = profile_or_name
        if isinstance(profile_or_name, str):
            found = self.get(profile_or_name)
            if found is None:
                raise VoiceLibraryError(f"voice not found: {profile_or_name}")
            profile = found
        return self._safe_relative_path(profile.reference_wav)

    # -- mutate ---------------------------------------------------------

    def remove(self, name_or_id: str) -> VoiceProfile:
        """Delete one profile folder and its metadata; only under the root."""
        profile = self._find(name_or_id)
        if profile is None:
            raise VoiceLibraryError(f"voice not found: {name_or_id}")
        target = self._profile_dir(profile.id)
        if target.is_dir():
            try:
                shutil.rmtree(target)
            except OSError as exc:
                raise VoiceLibraryError(f"cannot remove voice: {exc}") from exc
        self._write_index(self._scan_profiles())
        if self._active_id() == profile.id:
            self._clear_active()
        return profile

    def set_active(self, name_or_id: str) -> VoiceProfile:
        """Persist the selected voice id in ``active.json``."""
        profile = self._find(name_or_id)
        if profile is None:
            raise VoiceLibraryError(f"voice not found: {name_or_id}")
        self._ensure_root()
        _atomic_write_json(
            self.active_path,
            {"version": INDEX_VERSION, "id": profile.id},
        )
        return profile

    def update_transcript(self, name_or_id: str, transcript: str) -> VoiceProfile:
        """Replace a profile transcript and refresh the Qwen sidecar file."""
        profile = self._find(name_or_id)
        if profile is None:
            raise VoiceLibraryError(f"voice not found: {name_or_id}")
        text = str(transcript or "").strip()
        if not text:
            raise VoiceLibraryError("transcript must not be empty")
        updated = replace(
            profile,
            transcript=text,
            notes=_remove_note(profile.notes, NEEDS_TRANSCRIPT_NOTE),
        )
        self._write_profile(updated)
        return updated

    # -- validation helpers ---------------------------------------------

    def _validated_engine(self, engine: str) -> Engine:
        return _validated_engine(engine)

    def _validate_source_file(self, source: Path) -> None:
        if not source.is_file():
            raise VoiceLibraryError(f"audio file not found: {source}")
        suffix = source.suffix.lower().lstrip(".")
        if suffix not in AUDIO_EXTENSIONS:
            raise VoiceLibraryError(f"unsupported audio extension: {source.suffix or '<none>'}")
        try:
            size = source.stat().st_size
        except OSError as exc:
            raise VoiceLibraryError(f"cannot read audio file: {exc}") from exc
        if size <= 0:
            raise VoiceLibraryError("audio file is empty")
        if size > MAX_AUDIO_BYTES:
            raise VoiceLibraryError("audio file exceeds 20 MB; trim it before importing")

    def _prepare_identity(self, name: str) -> str:
        """Build a unique id for ``name`` and reject duplicate ids or names."""
        clean = _clean_name(name)
        profile_id = _slugify(clean)
        self._ensure_root()
        for existing in self.list():
            if existing.name.casefold() == clean.casefold():
                raise VoiceLibraryError(f"voice name already exists: {clean}")
            if existing.id == profile_id:
                raise VoiceLibraryError(f"voice id already exists: {profile_id}")
        if (self.root / profile_id).exists():
            raise VoiceLibraryError(f"voice id already exists: {profile_id}")
        return profile_id

    def _archive_original(self, source: Path, profile_dir: Path) -> None:
        target = profile_dir / f"original{source.suffix.lower()}"
        try:
            shutil.copy2(source, target)
        except OSError as exc:
            raise VoiceLibraryError(f"cannot archive the original audio: {exc}") from exc

    def _create_profile_dir(self, profile_id: str) -> Path:
        self._ensure_root()
        target = self._profile_dir(profile_id)
        try:
            target.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise VoiceLibraryError(f"voice id already exists: {profile_id}") from exc
        except OSError as exc:
            raise VoiceLibraryError(f"cannot create voice folder: {exc}") from exc
        return target

    # -- storage --------------------------------------------------------

    def _ensure_root(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise VoiceLibraryError(f"cannot create voice library: {exc}") from exc

    def _profile_dir(self, profile_id: str) -> Path:
        if not _SLUG_RE.match(profile_id):
            raise VoiceLibraryError(f"invalid voice id: {profile_id!r}")
        candidate = (self.root / profile_id).resolve(strict=False)
        if candidate.parent != self.root:
            raise VoiceLibraryError("voice folder escapes the library root")
        return candidate

    def _safe_relative_path(self, relative: str) -> Path:
        candidate = (self.root / PurePosixPath(relative)).resolve(strict=False)
        if candidate != self.root and self.root not in candidate.parents:
            raise VoiceLibraryError("voice path escapes the library root")
        return candidate

    def _write_profile(self, profile: VoiceProfile) -> None:
        profile_dir = self._profile_dir(profile.id)
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise VoiceLibraryError(f"cannot create voice folder: {exc}") from exc
        _atomic_write_json(profile_dir / "profile.json", profile.as_dict())
        self._write_transcript(profile, profile_dir)
        self._write_index(self._scan_profiles())

    def _write_transcript(self, profile: VoiceProfile, profile_dir: Path) -> None:
        sidecar = profile_dir / TRANSCRIPT_NAME
        text = profile.transcript.strip()
        if text:
            _atomic_write_text(sidecar, f"{text}\n")
        elif sidecar.exists():
            with suppress(OSError):
                sidecar.unlink()

    def _write_index(self, profiles: Sequence[VoiceProfile]) -> None:
        payload = {
            "version": INDEX_VERSION,
            "profiles": [
                profile.as_dict() for profile in sorted(profiles, key=lambda item: item.id)
            ],
        }
        _atomic_write_json(self.index_path, payload)

    def _load_index(self) -> list[VoiceProfile] | None:
        if not self.index_path.is_file():
            return None
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
            return None
        raw = data.get("profiles")
        if not isinstance(raw, list):
            return None
        try:
            profiles = [VoiceProfile.from_dict(item) for item in raw]
        except VoiceLibraryError:
            return None
        return sorted(profiles, key=lambda item: item.id)

    def _scan_profiles(self) -> list[VoiceProfile]:
        if not self.root.is_dir():
            return []
        profiles: list[VoiceProfile] = []
        for child in sorted(self.root.iterdir(), key=lambda item: item.name):
            if not child.is_dir():
                continue
            meta = child / "profile.json"
            if not meta.is_file():
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise VoiceLibraryError(f"cannot read {meta}: {exc}") from exc
            try:
                profile = VoiceProfile.from_dict(data)
            except VoiceLibraryError as exc:
                raise VoiceLibraryError(f"invalid profile in {child.name}: {exc}") from exc
            if profile.id != child.name:
                raise VoiceLibraryError(
                    f"profile id {profile.id!r} does not match folder {child.name!r}"
                )
            profiles.append(profile)
        return sorted(profiles, key=lambda item: item.id)

    def _find(self, name_or_id: str) -> VoiceProfile | None:
        key = str(name_or_id or "").strip()
        if not key:
            return None
        profiles = self.list()
        for profile in profiles:
            if profile.id == key:
                return profile
        folded = key.casefold()
        for profile in profiles:
            if profile.name.casefold() == folded:
                return profile
        return None

    def _active_id(self) -> str | None:
        if not self.active_path.is_file():
            return None
        try:
            data = json.loads(self.active_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        value = data.get("id")
        if not isinstance(value, str) or not _SLUG_RE.match(value):
            return None
        return value

    def _clear_active(self) -> None:
        with suppress(OSError):
            self.active_path.unlink(missing_ok=True)


__all__ = [
    "AUDIO_EXTENSIONS",
    "DEFAULT_ENGINE",
    "ENGINES",
    "MAX_AUDIO_BYTES",
    "MAX_DURATION_S",
    "NEEDS_TRANSCRIPT_NOTE",
    "NormalizedAudio",
    "REFERENCE_NAME",
    "SOURCES",
    "TARGET_SAMPLE_RATE",
    "VoiceLibrary",
    "VoiceLibraryError",
    "VoiceProfile",
    "normalize_audio",
]
