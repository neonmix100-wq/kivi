"""Unit tests for the local voice library; no network and no real codecs."""

from __future__ import annotations

import io
import json
import os
import wave
from pathlib import Path

import pytest

from kivi.senses import voice_library as vl
from kivi.senses.voice_library import (
    NEEDS_TRANSCRIPT_NOTE,
    VoiceLibrary,
    VoiceLibraryError,
    VoiceProfile,
    normalize_audio,
)

PUBLIC_URL = "https://1.1.1.1/voice.wav"
TRANSCRIPT = "Привет! Это тестовый голос Киви."


def make_wav(
    path: Path,
    *,
    seconds: float = 0.25,
    rate: int = 8000,
    channels: int = 1,
) -> Path:
    """Write a tiny silent PCM16 WAV with the standard library."""
    frames = max(1, int(rate * seconds))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames * channels)
    return path


def wav_bytes(*, seconds: float = 0.25, rate: int = 8000) -> bytes:
    """Return a tiny silent WAV as bytes for a fake downloader."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * max(1, int(rate * seconds)))
    return buffer.getvalue()


def read_wav(path: Path) -> tuple[int, int, int]:
    """Return ``(channels, sample_width, sample_rate)`` for a WAV file."""
    with wave.open(str(path), "rb") as handle:
        return handle.getnchannels(), handle.getsampwidth(), handle.getframerate()


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "voices"


@pytest.fixture()
def library(root: Path) -> VoiceLibrary:
    return VoiceLibrary(root)


def test_add_file_creates_normalized_profile(
    library: VoiceLibrary, root: Path, tmp_path: Path
) -> None:
    source = make_wav(tmp_path / "voice.wav")
    profile = library.add_file(source, "Kivi Warm", consent=True, transcript=TRANSCRIPT)

    assert profile.id == "kivi-warm"
    assert profile.name == "Kivi Warm"
    assert profile.engine == "qwen"
    assert profile.source == "file"
    assert profile.source_url == ""
    assert profile.consent is True
    assert profile.reference_wav == "kivi-warm/reference.wav"
    assert profile.duration_s == pytest.approx(0.25, abs=0.01)
    assert profile.created_at.endswith("Z")
    reference = root / profile.reference_wav
    assert reference.is_file()
    assert read_wav(reference) == (1, 2, 24000)
    assert reference.with_suffix(".txt").read_text(encoding="utf-8").strip() == TRANSCRIPT
    assert (root / "kivi-warm" / "original.wav").is_file()
    assert library.index_path.is_file()
    assert library.list() == [profile]


def test_add_file_requires_explicit_consent(library: VoiceLibrary, tmp_path: Path) -> None:
    source = make_wav(tmp_path / "voice.wav")
    with pytest.raises(VoiceLibraryError, match="consent"):
        library.add_file(source, "No Consent", consent=False)
    with pytest.raises(VoiceLibraryError, match="consent"):
        library.add_file(source, "Truthy Consent", consent=1)  # type: ignore[arg-type]
    assert library.list() == []


def test_add_file_rejects_missing_empty_and_non_audio(
    library: VoiceLibrary, tmp_path: Path
) -> None:
    with pytest.raises(VoiceLibraryError, match="not found"):
        library.add_file(tmp_path / "missing.wav", "Missing", consent=True)
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    with pytest.raises(VoiceLibraryError, match="empty"):
        library.add_file(empty, "Empty", consent=True)
    notes = tmp_path / "notes.txt"
    notes.write_text("not audio", encoding="utf-8")
    with pytest.raises(VoiceLibraryError, match="unsupported audio extension"):
        library.add_file(notes, "Notes", consent=True)


def test_add_file_rejects_unreadable_audio(library: VoiceLibrary, tmp_path: Path) -> None:
    fake = tmp_path / "fake.wav"
    fake.write_bytes(b"this is definitely not a wav file")
    with pytest.raises(VoiceLibraryError, match="decode"):
        library.add_file(fake, "Fake", consent=True)
    assert library.list() == []


def test_add_file_rejects_oversized_file(
    library: VoiceLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vl, "MAX_AUDIO_BYTES", 64)
    source = make_wav(tmp_path / "voice.wav")
    with pytest.raises(VoiceLibraryError, match="20 MB"):
        library.add_file(source, "Too Big", consent=True)


def test_fake_transcriber_fills_missing_transcript(
    library: VoiceLibrary, root: Path, tmp_path: Path
) -> None:
    calls: list[Path] = []

    def transcriber(path: Path) -> str:
        calls.append(path)
        return TRANSCRIPT

    profile = library.add_file(
        make_wav(tmp_path / "voice.wav"),
        "Auto Transcript",
        consent=True,
        transcriber=transcriber,
    )
    assert profile.transcript == TRANSCRIPT
    assert profile.has_transcript
    assert NEEDS_TRANSCRIPT_NOTE not in profile.notes
    assert calls == [root / "auto-transcript" / "reference.wav"]


def test_supplied_transcript_skips_transcriber(
    library: VoiceLibrary, tmp_path: Path
) -> None:
    def transcriber(_: Path) -> str:
        raise AssertionError("transcriber must not run when a transcript is supplied")

    profile = library.add_file(
        make_wav(tmp_path / "voice.wav"),
        "Given Transcript",
        consent=True,
        transcript="Готовый текст",
        transcriber=transcriber,
    )
    assert profile.transcript == "Готовый текст"


def test_missing_transcript_is_flagged(library: VoiceLibrary, tmp_path: Path) -> None:
    profile = library.add_file(make_wav(tmp_path / "voice.wav"), "No Text", consent=True)
    assert profile.transcript == ""
    assert not profile.has_transcript
    assert NEEDS_TRANSCRIPT_NOTE in profile.notes


def test_transcriber_failure_rolls_back(
    library: VoiceLibrary, root: Path, tmp_path: Path
) -> None:
    def boom(_: Path) -> str:
        raise RuntimeError("model is down")

    with pytest.raises(VoiceLibraryError, match="transcription failed"):
        library.add_file(
            make_wav(tmp_path / "voice.wav"),
            "Broken",
            consent=True,
            transcriber=boom,
        )
    assert not (root / "broken").exists()
    assert library.list() == []


def test_add_file_copy_false_keeps_only_the_reference(
    library: VoiceLibrary, root: Path, tmp_path: Path
) -> None:
    profile = library.add_file(
        make_wav(tmp_path / "voice.wav"),
        "No Copy",
        consent=True,
        transcript=TRANSCRIPT,
        copy=False,
    )
    names = {path.name for path in (root / profile.id).iterdir()}
    assert "original.wav" not in names
    assert "reference.wav" in names


@pytest.mark.parametrize("url", ["http://1.1.1.1/voice.wav", "file:///tmp/voice.wav"])
def test_add_url_rejects_non_https(
    library: VoiceLibrary, url: str
) -> None:
    with pytest.raises(VoiceLibraryError, match="https"):
        library.add_url(url, "Bad Scheme", consent=True, downloader=lambda _: b"x")


def test_add_url_rejects_credentials(library: VoiceLibrary) -> None:
    with pytest.raises(VoiceLibraryError, match="credentials"):
        library.add_url(
            "https://user:secret@1.1.1.1/voice.wav",
            "Creds",
            consent=True,
            downloader=lambda _: b"x",
        )


@pytest.mark.parametrize(
    "url",
    ["https://localhost/voice.wav", "https://voices.local/voice.wav"],
)
def test_add_url_rejects_localhost_names(library: VoiceLibrary, url: str) -> None:
    with pytest.raises(VoiceLibraryError, match="localhost"):
        library.add_url(url, "Local", consent=True, downloader=lambda _: b"x")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/voice.wav",
        "https://10.0.0.7/voice.wav",
        "https://192.168.1.20/voice.wav",
        "https://169.254.1.1/voice.wav",
        "https://0.0.0.0/voice.wav",
        "https://[::1]/voice.wav",
    ],
)
def test_add_url_rejects_private_addresses(library: VoiceLibrary, url: str) -> None:
    with pytest.raises(VoiceLibraryError, match="private|local"):
        library.add_url(url, "Private", consent=True, downloader=lambda _: b"x")
    assert library.list() == []


def test_add_url_rejects_nonstandard_port(library: VoiceLibrary) -> None:
    with pytest.raises(VoiceLibraryError, match="ports"):
        library.add_url(
            "https://1.1.1.1:8443/voice.wav",
            "Port",
            consent=True,
            downloader=lambda _: b"x",
        )


def test_add_url_rejects_hostname_resolving_private(library: VoiceLibrary) -> None:
    def resolver(_: str) -> list[str]:
        return ["192.168.1.5"]

    with pytest.raises(VoiceLibraryError, match="private"):
        library.add_url(
            "https://voices.example/voice.wav",
            "Resolved Private",
            consent=True,
            downloader=lambda _: b"x",
            resolver=resolver,
        )


def test_add_url_rejects_oversized_download(
    library: VoiceLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vl, "MAX_AUDIO_BYTES", 64)
    with pytest.raises(VoiceLibraryError, match="20 MB"):
        library.add_url(
            PUBLIC_URL,
            "Too Big Download",
            consent=True,
            downloader=lambda _: b"x" * 200,
        )


def test_add_url_imports_audio_with_fake_downloader(
    library: VoiceLibrary, root: Path
) -> None:
    payload = wav_bytes()
    seen: list[str] = []

    def downloader(url: str) -> bytes:
        seen.append(url)
        return payload

    profile = library.add_url(
        PUBLIC_URL,
        "Remote Voice",
        consent=True,
        transcript=TRANSCRIPT,
        downloader=downloader,
    )
    assert seen == [PUBLIC_URL]
    assert profile.source == "url"
    assert profile.source_url == PUBLIC_URL
    reference = root / profile.reference_wav
    assert reference.is_file()
    assert read_wav(reference) == (1, 2, 24000)
    assert not (root / profile.id / "download.wav").exists()
    assert library.active() is None


def test_add_url_sanitizes_source_url(library: VoiceLibrary) -> None:
    url = "https://1.1.1.1/voice.wav?token=supersecret&id=42#fragment"
    profile = library.add_url(
        url,
        "Signed URL",
        consent=True,
        transcript=TRANSCRIPT,
        downloader=lambda _: wav_bytes(),
    )
    assert "supersecret" not in profile.source_url
    assert "token" not in profile.source_url
    assert profile.source_url == "https://1.1.1.1/voice.wav?id=42"


def test_add_url_rejects_unsupported_content_type(
    library: VoiceLibrary, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vl, "_fetch_audio", lambda _: (b"<html></html>", "text/html"))
    with pytest.raises(VoiceLibraryError, match="content type"):
        library.add_url("https://1.1.1.1/voice", "HTML", consent=True)


def test_add_url_accepts_audio_content_type(
    library: VoiceLibrary, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vl, "_fetch_audio", lambda _: (wav_bytes(), "audio/wav"))
    profile = library.add_url("https://1.1.1.1/voice", "Audio Type", consent=True)
    assert profile.source == "url"
    assert (root / profile.reference_wav).is_file()


def test_add_url_fake_downloader_requires_audio_extension(library: VoiceLibrary) -> None:
    with pytest.raises(VoiceLibraryError, match="extension"):
        library.add_url(
            "https://1.1.1.1/voice",
            "No Extension",
            consent=True,
            downloader=lambda _: wav_bytes(),
        )


def test_list_get_and_remove(library: VoiceLibrary, root: Path, tmp_path: Path) -> None:
    first = library.add_file(
        make_wav(tmp_path / "first.wav"), "Alpha", consent=True, transcript=TRANSCRIPT
    )
    second = library.add_file(
        make_wav(tmp_path / "second.wav"), "Bravo", consent=True, transcript=TRANSCRIPT
    )
    assert [profile.id for profile in library.list()] == ["alpha", "bravo"]
    assert library.get("Alpha") == first
    assert library.get("alpha") == first
    assert library.get("nope") is None
    removed = library.remove("bravo")
    assert removed == second
    assert not (root / "bravo").exists()
    assert library.list() == [first]
    index = json.loads(library.index_path.read_text("utf-8"))
    assert [item["id"] for item in index["profiles"]] == ["alpha"]


def test_set_active_persists_and_remove_clears(
    library: VoiceLibrary, root: Path, tmp_path: Path
) -> None:
    profile = library.add_file(
        make_wav(tmp_path / "voice.wav"), "Active Voice", consent=True, transcript=TRANSCRIPT
    )
    assert library.set_active("active-voice") == profile
    stored = json.loads(library.active_path.read_text(encoding="utf-8"))
    assert stored["id"] == profile.id
    reopened = VoiceLibrary(root)
    assert reopened.active() == profile
    reopened.remove("active-voice")
    assert reopened.active() is None
    assert not library.active_path.exists()


def test_update_transcript_updates_profile_sidecar_and_index(
    library: VoiceLibrary, root: Path, tmp_path: Path
) -> None:
    profile = library.add_file(make_wav(tmp_path / "voice.wav"), "Late Text", consent=True)
    assert NEEDS_TRANSCRIPT_NOTE in profile.notes
    updated = library.update_transcript("late-text", TRANSCRIPT)
    assert updated.transcript == TRANSCRIPT
    assert NEEDS_TRANSCRIPT_NOTE not in updated.notes
    sidecar = root / updated.id / "reference.txt"
    assert sidecar.read_text(encoding="utf-8").strip() == TRANSCRIPT
    stored = json.loads((root / updated.id / "profile.json").read_text(encoding="utf-8"))
    assert stored["transcript"] == TRANSCRIPT
    assert VoiceLibrary(root).get("Late Text") == updated
    with pytest.raises(VoiceLibraryError, match="must not be empty"):
        library.update_transcript("late-text", "   ")


def test_duplicate_name_and_slug_are_rejected(
    library: VoiceLibrary, tmp_path: Path
) -> None:
    library.add_file(
        make_wav(tmp_path / "one.wav"), "Voice One", consent=True, transcript=TRANSCRIPT
    )
    with pytest.raises(VoiceLibraryError, match="name already exists"):
        library.add_file(
            make_wav(tmp_path / "two.wav"),
            "Voice One",
            consent=True,
            transcript=TRANSCRIPT,
        )
    with pytest.raises(VoiceLibraryError, match="id already exists"):
        library.add_file(
            make_wav(tmp_path / "three.wav"),
            "Voice-One",
            consent=True,
            transcript=TRANSCRIPT,
        )


def test_path_traversal_is_blocked(
    library: VoiceLibrary, root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("keep", encoding="utf-8")

    assert library.get("../../outside") is None
    with pytest.raises(VoiceLibraryError, match="not found"):
        library.remove("../outside")
    assert marker.is_file()

    root.mkdir(parents=True, exist_ok=True)
    malicious = {
        "id": "../outside",
        "name": "Escape",
        "engine": "qwen",
        "reference_wav": "../outside/reference.wav",
        "transcript": "",
        "source": "file",
        "source_url": "",
        "consent": True,
        "created_at": "2026-01-01T00:00:00Z",
        "duration_s": 1.0,
        "notes": "",
    }
    (root / "index.json").write_text(
        json.dumps({"version": 1, "profiles": [malicious]}),
        encoding="utf-8",
    )
    assert library.list() == []
    assert marker.is_file()


def test_invalid_profile_folder_is_rejected(
    library: VoiceLibrary, root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    evil = root / "evil"
    evil.mkdir(parents=True)
    profile = {
        "id": "evil",
        "name": "Evil",
        "engine": "qwen",
        "reference_wav": "../outside/reference.wav",
        "transcript": "",
        "source": "file",
        "source_url": "",
        "consent": True,
        "created_at": "2026-01-01T00:00:00Z",
        "duration_s": 1.0,
        "notes": "",
    }
    (evil / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    with pytest.raises(VoiceLibraryError, match="invalid profile"):
        library.list()


def test_index_is_sorted_and_written_atomically(
    library: VoiceLibrary, root: Path, tmp_path: Path
) -> None:
    library.add_file(
        make_wav(tmp_path / "bravo.wav"), "Bravo", consent=True, transcript=TRANSCRIPT
    )
    library.add_file(
        make_wav(tmp_path / "alpha.wav"), "Alpha", consent=True, transcript=TRANSCRIPT
    )
    payload = json.loads(library.index_path.read_text(encoding="utf-8"))
    ids = [entry["id"] for entry in payload["profiles"]]
    assert ids == ["alpha", "bravo"]
    assert payload["version"] == 1
    assert [path for path in root.rglob("*") if path.suffix == ".tmp"] == []


def test_failed_index_write_keeps_old_index_and_rolls_back(
    library: VoiceLibrary, root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library.add_file(
        make_wav(tmp_path / "alpha.wav"), "Alpha", consent=True, transcript=TRANSCRIPT
    )
    before = library.index_path.read_bytes()
    real_replace = os.replace

    def flaky_replace(source: str, target: str) -> None:
        if Path(target).name == "index.json":
            raise OSError("disk full")
        real_replace(source, target)

    monkeypatch.setattr(vl.os, "replace", flaky_replace)
    with pytest.raises(VoiceLibraryError, match="index.json"):
        library.add_file(
            make_wav(tmp_path / "bravo.wav"),
            "Bravo",
            consent=True,
            transcript=TRANSCRIPT,
        )
    assert library.index_path.read_bytes() == before
    assert not (root / "bravo").exists()
    assert [profile.id for profile in library.list()] == ["alpha"]


def test_index_repairs_itself_from_profile_folders(
    library: VoiceLibrary, root: Path, tmp_path: Path
) -> None:
    profile = library.add_file(
        make_wav(tmp_path / "voice.wav"), "Repair Me", consent=True, transcript=TRANSCRIPT
    )
    library.index_path.unlink()
    reopened = VoiceLibrary(root)
    assert reopened.list() == [profile]
    assert reopened.index_path.is_file()


def test_profile_round_trip_and_strict_validation() -> None:
    profile = VoiceProfile(
        id="alpha",
        name="Alpha",
        engine="qwen",
        reference_wav="alpha/reference.wav",
        transcript=TRANSCRIPT,
        source="url",
        source_url="https://1.1.1.1/voice.wav?id=42",
        consent=True,
        created_at="2026-01-02T03:04:05Z",
        duration_s=1.25,
        notes="warm",
    )
    assert VoiceProfile.from_dict(profile.as_dict()) == profile
    assert set(profile.as_dict()) == {
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
    }

    missing = profile.as_dict()
    del missing["notes"]
    with pytest.raises(VoiceLibraryError, match="missing fields"):
        VoiceProfile.from_dict(missing)

    unknown = profile.as_dict()
    unknown["secret"] = "hidden"
    with pytest.raises(VoiceLibraryError, match="unknown fields"):
        VoiceProfile.from_dict(unknown)

    no_consent = profile.as_dict()
    no_consent["consent"] = False
    with pytest.raises(VoiceLibraryError, match="consent"):
        VoiceProfile.from_dict(no_consent)

    traversal = profile.as_dict()
    traversal["reference_wav"] = "../outside/reference.wav"
    with pytest.raises(VoiceLibraryError, match="escape"):
        VoiceProfile.from_dict(traversal)

    with pytest.raises(VoiceLibraryError, match="unsupported voice engine"):
        VoiceProfile(
            id="beta",
            name="Beta",
            engine="xtts",
            reference_wav="beta/reference.wav",
            consent=True,
        )


def test_normalize_resamples_stereo_to_mono_24k(tmp_path: Path) -> None:
    source = make_wav(
        tmp_path / "stereo.wav",
        seconds=0.3,
        rate=44100,
        channels=2,
    )
    target = tmp_path / "normalized.wav"
    result = normalize_audio(source, target)
    assert result.path == target
    assert not result.trimmed
    assert result.duration_s == pytest.approx(0.3, abs=0.02)
    assert read_wav(target) == (1, 2, 24000)


def test_normalize_trims_to_max_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vl, "MAX_DURATION_S", 0.5)
    source = make_wav(tmp_path / "long.wav", seconds=2.0)
    result = normalize_audio(source, tmp_path / "trimmed.wav")
    assert result.trimmed
    assert result.duration_s <= 0.51
    with wave.open(str(result.path), "rb") as handle:
        assert handle.getnframes() <= 12000
