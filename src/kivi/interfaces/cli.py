"""Command-line interface (stage 1).

Commands are intentionally small vertical slices: diagnostics, model listing,
durable event log and a chat placeholder that already records tasks and events.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
import wave
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from kivi.brain import KiviBrain
from kivi.brain.coder import Coder
from kivi.cloud import (
    CloudBroker,
    CloudError,
    CloudRegistryError,
    CloudScout,
    QuotaExhausted,
    ScoutError,
    ScoutQuery,
    build_handoff,
    load_cloud_registry,
    load_handoff,
    load_scout_registry,
    save_handoff,
)
from kivi.config import ConfigError, KiviConfig, load_config
from kivi.doctor import ollama_models, run_checks, summary
from kivi.integrations.gmail import GmailAuth, GmailAuthError
from kivi.interfaces.api import LocalApi
from kivi.interfaces.avatar2d import (
    AvatarController,
    AvatarError,
    TkAvatarRenderer,
)
from kivi.kernel.db import Database
from kivi.kernel.events import EventBus
from kivi.kernel.logs import JsonlLogger
from kivi.memory import (
    ApprovalStore,
    ContextBuilder,
    HybridSearch,
    MemoryStore,
    apply_ttl,
    import_directory,
    summarize_messages,
)
from kivi.memory.vector_index import (
    LanceVectorIndex,
    VectorIndexError,
    export_from_store,
    rebuild_index,
)
from kivi.models import (
    BackendError,
    GatewayError,
    ModelGateway,
    OllamaBackend,
    RegistryError,
    canonical_model_id,
    load_registry,
    query_vram,
)
from kivi.paths import Paths, get_paths
from kivi.safety import KillSwitch, PermissionEngine, ProtectedZones
from kivi.safety.elevated import (
    ALLOWED_OPERATIONS,
    OPERATIONS,
    ElevatedBroker,
    ElevatedError,
    read_result,
)
from kivi.safety.hotkey import (
    HotkeyError,
    HotkeyListener,
    HotkeyUnavailable,
    WindowsHotkey,
    parse_hotkey,
)
from kivi.safety.killswitch import FLAG_NAME
from kivi.safety.zone_manifest import (
    ZoneManifestError,
    generate_key,
    load_signed_rules,
    sign_manifest,
)
from kivi.safety.zone_manifest import (
    status as zone_manifest_status,
)
from kivi.senses import (
    PiperTts,
    SileroTts,
    SpeechPipeline,
    TtsError,
)
from kivi.senses.audio import (
    AudioUnavailable,
    EnergyVad,
    SoundDeviceSource,
    SttUnavailable,
    TextWakeWordDetector,
    VadSegmenter,
    WakeWordUnavailable,
    WhisperTranscriber,
)
from kivi.senses.fish_audio import (
    DEFAULT_REFERENCE_ID,
    FishAudioTts,
    FishTtsConfig,
    FishTtsError,
)
from kivi.senses.qwen_tts import (
    DEFAULT_VOICE_INSTRUCT,
    QwenTtsConfig,
    QwenTtsEngine,
    QwenTtsError,
    QwenVoiceDesigner,
)
from kivi.senses.voice import VoiceSession
from kivi.senses.voice_library import VoiceLibrary, VoiceLibraryError
from kivi.tools import ToolExecutor, ToolResult, build_registry

console = Console()


def _plain(text: str) -> str:
    """Make arbitrary tool output printable and safe for Rich markup."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    return escape(safe)


@contextmanager
def _state(paths: Paths) -> Iterator[tuple[Database, EventBus]]:
    db = Database(paths.db_file)
    try:
        yield db, EventBus(db, JsonlLogger(paths.logs))
    finally:
        db.close()


def _config(paths: Paths) -> KiviConfig:
    try:
        return load_config(paths.config_file)
    except ConfigError as exc:
        console.print(f"[red]config error:[/red] {exc}")
        raise SystemExit(2) from exc


def _kill_switch(
    paths: Paths,
    db: Database,
    events: EventBus,
    on_engage=None,
) -> KillSwitch:
    return KillSwitch(paths.home / FLAG_NAME, db=db, events=events, on_engage=on_engage)


def _zones_extra(paths: Paths):
    manifest = paths.root / "config" / "protected-zones.yaml"
    key = paths.root / "private" / "safety" / "zones.key"
    return load_signed_rules(manifest, key)


def _unload_local_models(paths: Paths) -> int:
    """Best-effort unload of models Ollama is currently serving."""
    try:
        with OllamaBackend(timeout=3.0) as backend:
            running = backend.running()
            for model_id in running:
                try:
                    backend.unload(model_id)
                except BackendError:
                    continue
            return len(running)
    except BackendError:
        return 0


def _registry(paths: Paths):
    try:
        return load_registry(paths.root / "config" / "models.yaml")
    except RegistryError as exc:
        console.print(f"[red]model registry error:[/red] {exc}")
        raise SystemExit(2) from exc


def cmd_doctor(args: argparse.Namespace) -> int:
    paths = get_paths()
    checks = run_checks(paths)

    table = Table(title="Kivi doctor", show_lines=False)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")
    colors = {"ok": "green", "warn": "yellow", "fail": "red"}
    for check in checks:
        table.add_row(check.name, f"[{colors[check.status]}]{check.status}[/]", check.detail)
    console.print(table)
    console.print(f"[bold]{summary(checks)}[/bold]")
    return 0 if not any(check.status == "fail" for check in checks) else 1


def cmd_models(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)

    table = Table(title="Model gateway")
    table.add_column("Setting")
    table.add_column("Value")
    table.add_row("local first", str(config.model_gateway.prefer_local))
    table.add_row("one heavy model", str(config.model_gateway.one_heavy_model))
    table.add_row("VRAM budget", f"{config.model_gateway.vram_total_mb} MB")
    table.add_row("safety margin", f"{config.model_gateway.safety_margin_mb} MB")
    table.add_row("cloud mode", config.model_gateway.cloud.mode)
    table.add_row("cloud cost limit", f"${config.model_gateway.cloud.cost_limit_usd:.2f}")
    console.print(table)

    snapshot = query_vram()
    if snapshot is None:
        console.print("[yellow]VRAM: nvidia-smi did not return a GPU snapshot.[/yellow]")
    else:
        console.print(
            f"VRAM now: [bold]{snapshot.free_mb} MB free[/bold] of "
            f"{snapshot.total_mb} MB ({snapshot.used_ratio:.0%} used)"
        )
        if snapshot.overcommitted:
            console.print(
                "[yellow]Shared memory is active; a heavy load should be refused.[/yellow]"
            )

    registry = _registry(paths)
    names = ollama_models()
    installed_names = {canonical_model_id(name) for name in names}
    running: dict[str, int] = {}
    try:
        with OllamaBackend(timeout=3) as backend:
            running = backend.running()
    except BackendError:
        running = {}

    catalog = Table(title="Model registry")
    catalog.add_column("Model", overflow="fold")
    catalog.add_column("Tier", justify="right")
    catalog.add_column("Role")
    catalog.add_column("VRAM", justify="right")
    catalog.add_column("Installed")
    catalog.add_column("Loaded")
    for spec in sorted(registry.all(), key=lambda item: (item.tier, item.id)):
        catalog.add_row(
            spec.id,
            str(spec.tier),
            spec.role,
            f"{spec.vram_mb} MB",
            "yes" if canonical_model_id(spec.id) in installed_names else "-",
            f"{running[spec.id]} MB" if spec.id in running else "-",
        )
    console.print(catalog)

    extra = sorted(set(running) - {spec.id for spec in registry.all()})
    if extra:
        console.print("[yellow]Loaded outside the registry:[/yellow] " + ", ".join(extra))

    if not names:
        console.print("[yellow]Ollama is not running or has no models installed.[/yellow]")
        return 0

    import httpx

    payload = httpx.get("http://127.0.0.1:11434/api/tags", timeout=3).json()
    installed = Table(title="Ollama models")
    installed.add_column("Model")
    installed.add_column("Size", justify="right")
    installed.add_column("Parameters")
    for item in payload.get("models", []):
        size = item.get("size", 0) / 1024**3
        details = item.get("details", {})
        installed.add_row(
            item.get("name", "?"),
            f"{size:.1f} GB",
            details.get("parameter_size", "?"),
        )
    console.print(installed)
    return 0


def _speech_pipeline(config: KiviConfig) -> SpeechPipeline:
    if config.voice.provider == "fish":
        return SpeechPipeline(
            FishAudioTts(_fish_config(config)),
            fallback=PiperTts(),
            enable_fallback=True,
            speaker=config.voice.speaker,
            sample_rate=config.voice.sample_rate,
        )
    if config.voice.provider == "qwen":
        return SpeechPipeline(
            QwenTtsEngine(_qwen_config(config)),
            fallback=PiperTts(),
            enable_fallback=True,
            speaker=config.voice.speaker,
            sample_rate=config.voice.sample_rate,
        )
    if config.voice.provider == "piper":
        return SpeechPipeline(
            PiperTts(),
            fallback=None,
            enable_fallback=False,
            speaker=config.voice.speaker,
            sample_rate=config.voice.sample_rate,
        )
    return SpeechPipeline(
        SileroTts(),
        fallback=PiperTts(),
        enable_fallback=True,
        speaker=config.voice.speaker,
        sample_rate=config.voice.sample_rate,
    )


def _play_wav(path: Path) -> None:
    """Play a WAV file, preferring an explicit/known output device."""
    try:
        import sounddevice as sd
        import soundfile as sf

        samples, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
        device: int | str | None = os.environ.get("KIVI_AUDIO_DEVICE") or None
        if isinstance(device, str) and device.isdigit():
            device = int(device)
        sd.play(samples, sample_rate, device=device, blocking=True)
        return
    except Exception:
        pass
    try:
        import winsound

        winsound.PlaySound(str(path), winsound.SND_FILENAME)
    except Exception as exc:  # noqa: BLE001 - audio is best effort
        console.print(f"[yellow]playback unavailable:[/yellow] {_plain(str(exc))}")


def _feed_avatar(path: Path, avatar: AvatarController, renderer: Any, play: bool) -> None:
    try:
        with wave.open(str(path), "rb") as handle:
            if (
                handle.getnchannels() == 1
                and handle.getsampwidth() == 2
                and handle.getframerate() == 48000
            ):
                avatar.update_pcm(handle.readframes(handle.getnframes()))
                renderer.render(avatar.state, avatar.openness)
    except (OSError, wave.Error):
        pass
    if play:
        _play_wav(path)


def _speak(
    text: str,
    pipeline: SpeechPipeline | None,
    *,
    cache: Path,
    avatar: AvatarController | None = None,
    renderer: Any = None,
    play: bool = False,
) -> None:
    if pipeline is None or not text.strip():
        return
    if avatar is not None:
        avatar.on_speech_start()
        if renderer is not None:
            renderer.render(avatar.state, avatar.openness)
    try:
        results = pipeline.synthesize(text, cache)
        for result in results:
            if avatar is not None and renderer is not None:
                _feed_avatar(result.path, avatar, renderer, False)
            if play:
                _play_wav(result.path)
    except TtsError as exc:
        console.print(f"[yellow]TTS:[/yellow] {_plain(str(exc))}")
    finally:
        if avatar is not None:
            avatar.on_speech_end()
            if renderer is not None:
                renderer.render(avatar.state, avatar.openness)


def cmd_chat(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    if not args.prompt and not sys.stdin.isatty():
        console.print('Interactive chat needs a terminal; use: kivi chat "вопрос"')
        return 0
    models = _registry(paths)
    try:
        spec = models.require(args.model) if args.model else models.default_for("router")
    except RegistryError as exc:
        console.print(f"[red]model error:[/red] {exc}")
        return 2

    registry = build_registry(blocked_hosts=config.security.blocked_hosts)
    task_id = f"tsk_{uuid.uuid4().hex[:12]}"
    avatar: AvatarController | None = None
    renderer: Any = None
    if args.avatar:
        try:
            avatar = AvatarController()
            renderer = TkAvatarRenderer(
                show=True,
                always_on_top=config.avatar.always_on_top,
            )
            renderer.start()
        except AvatarError as exc:
            console.print(f"[yellow]avatar unavailable:[/yellow] {_plain(str(exc))}")
            avatar = None
            renderer = None
    pipeline = _speech_pipeline(config) if args.speak else None

    with _state(paths) as (db, bus):
        try:
            extra_zones = _zones_extra(paths)
        except ZoneManifestError as exc:
            console.print(f"[red]protected zones error:[/red] {exc}")
            return 1
        zones = ProtectedZones(paths.root, extra_rules=extra_zones)
        permissions = PermissionEngine(
            db,
            autonomy=config.autonomy,
            workspace=str(paths.root),
            events=bus,
        )
        executor = ToolExecutor(
            registry,
            db,
            permissions,
            zones,
            workspace=paths.root,
            events=bus,
            killswitch=_kill_switch(paths, db, bus),
        )
        store = _memory(paths, db)
        builder = ContextBuilder(store)
        cache = paths.cache / "tts"

        def approve(step: Any) -> bool:
            tool = registry.get(step.tool)
            risk = tool.risk if tool is not None else "?"
            console.print(
                f"[yellow]Требуется подтверждение:[/yellow] {step.tool} ({risk})"
            )
            if args.yes:
                return True
            try:
                answer = input("Разрешить? [y/N] ").strip().lower()
            except EOFError:
                return False
            return answer in {"y", "yes", "д", "да"}

        try:
            with OllamaBackend(timeout=args.timeout) as backend:
                gateway = ModelGateway(
                    models,
                    backend,
                    vram_total_mb=config.model_gateway.vram_total_mb,
                    safety_margin_mb=config.model_gateway.safety_margin_mb,
                    allow_cpu_fallback=config.model_gateway.cpu_fallback,
                    events=bus,
                )

                def llm(messages: list[dict[str, str]]) -> str:
                    options: dict[str, int] = {"num_predict": args.num_predict}
                    if spec.context > 0:
                        options["num_ctx"] = spec.context
                    return gateway.chat(
                        spec.id,
                        messages,
                        keep_alive=args.keep_alive,
                        options=options,
                        wait_s=args.wait,
                    )

                brain = KiviBrain(llm, registry, executor, permissions)

                def process(prompt: str) -> None:
                    context = builder.build(prompt, task_id=task_id)
                    store.add_message("user", prompt, task_id=task_id)
                    result = brain.ask(
                        prompt,
                        context=context.text,
                        task_id=task_id,
                        approve=approve,
                    )
                    console.print(f"[cyan]Kivi:[/cyan] {_plain(result.answer)}")
                    store.add_message(
                        "assistant",
                        result.answer,
                        trust="untrusted:tool",
                        task_id=task_id,
                    )
                    _speak(
                        result.answer,
                        pipeline,
                        cache=cache,
                        avatar=avatar,
                        renderer=renderer,
                        play=args.play or args.speak,
                    )
                    if not args.prompt and result.ok:
                        try:
                            more = input("Подробнее? [d/N] ").strip().lower()
                        except EOFError:
                            more = ""
                        if more in {"d", "да", "подробнее", "more"}:
                            detail = brain.ask(
                                f"{prompt}\nОбъясни подробнее и назови шаги.",
                                context=context.text,
                                task_id=task_id,
                                approve=approve,
                            )
                            console.print(f"[cyan]Kivi:[/cyan] {_plain(detail.answer)}")
                            store.add_message(
                                "assistant",
                                detail.answer,
                                trust="untrusted:tool",
                                task_id=task_id,
                            )
                            _speak(
                                detail.answer,
                                pipeline,
                                cache=cache,
                                avatar=avatar,
                                renderer=renderer,
                                play=args.play or args.speak,
                            )

                if args.prompt:
                    process(args.prompt)
                    return 0
                console.print("Kivi готова. /exit — выход.")
                while True:
                    try:
                        prompt = input("Вы: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        console.print("")
                        break
                    if prompt.lower() in {"/exit", "/quit", "exit", "выход"}:
                        break
                    if prompt:
                        process(prompt)
        except (BackendError, GatewayError) as exc:
            console.print(f"[red]model error:[/red] {_plain(str(exc))}")
            return 1
    if renderer is not None:
        renderer.stop()
    return 0


def _wav_bytes_from_results(results: list[Any]) -> bytes:
    """Concatenate synthesized WAV files into one in-memory WAV."""
    frames: list[bytes] = []
    params: tuple[int, int, int] | None = None
    for result in results:
        with wave.open(str(result.path), "rb") as handle:
            current = (
                handle.getnchannels(),
                handle.getsampwidth(),
                handle.getframerate(),
            )
            if params is None:
                params = current
            elif current != params:
                # Mixed engines are rare; keep the first clip instead of
                # returning malformed or chipmunk audio.
                continue
            frames.append(handle.readframes(handle.getnframes()))
    if params is None:
        return b""
    output = io.BytesIO()
    with wave.open(output, "wb") as handle:
        handle.setnchannels(params[0])
        handle.setsampwidth(params[1])
        handle.setframerate(params[2])
        handle.writeframes(b"".join(frames))
    return output.getvalue()


def _serve_history(paths: Any, limit: int) -> list[dict[str, Any]]:
    with _state(paths) as (db, _bus):
        messages = _memory(paths, db).recent_messages(limit)
    return [
        {
            "role": str(item.get("role", "")),
            "content": str(item.get("content", "")),
            "task_id": str(item.get("task_id", "")),
            "created_at": str(item.get("created_at", "")),
        }
        for item in reversed(messages)
    ]


def cmd_ui(args: argparse.Namespace) -> int:
    """Start the local UI server and open it in the default browser."""
    args.ui = True
    args.open_browser = not args.no_browser
    return cmd_serve(args)


def _app_browser() -> Path | None:
    """Find Edge/Chrome for a chromeless ``--app`` window."""
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return Path(found)
    candidates = (
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    )
    return next((path for path in candidates if path.is_file()), None)


def _open_app_window(url: str) -> bool:
    """Open Kivi in a standalone Edge/Chrome app window."""
    browser = _app_browser()
    if browser is None:
        webbrowser.open(url)
        return False
    try:
        subprocess.Popen(
            [str(browser), f"--app={url}", "--new-window"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError:
        webbrowser.open(url)
        return False
    return True


def _run_tray(api: LocalApi) -> None:
    """Run the optional Kivi tray icon until the user stops it."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        console.print("[yellow]tray unavailable: install pystray[/yellow]")
        return
    image = Image.new("RGBA", (64, 64), (4, 32, 29, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((10, 10, 54, 54), fill=(94, 242, 214, 255))
    draw.ellipse((24, 24, 40, 40), fill=(4, 32, 29, 255))
    icon = pystray.Icon(
        "kivi",
        image,
        "Kivi",
        menu=pystray.Menu(
            pystray.MenuItem(
                "Открыть Kivi",
                lambda: _open_app_window(api.url),
                default=True,
            ),
            pystray.MenuItem("Остановить", lambda: (icon.stop(), api.stop())),
        ),
    )
    console.print("Kivi в трее: правый клик по иконке — открыть/остановить.")
    icon.run()


def _transcribe_wav_bytes(data: bytes, model: str) -> str:
    """Decode a mono 16-bit WAV and run faster-whisper once."""
    import numpy as np

    with wave.open(io.BytesIO(data), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise ValueError("only 16-bit PCM WAV is supported")
    samples = np.frombuffer(raw, dtype="<i2").astype("float32") / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        import librosa

        samples = librosa.resample(samples, orig_sr=rate, target_sr=16000)
    target = Path(model)
    if target.is_dir():
        transcriber = WhisperTranscriber(model_path=target)
    else:
        transcriber = WhisperTranscriber(model)
    return transcriber.transcribe(samples).text


def cmd_serve(args: argparse.Namespace) -> int:
    if getattr(args, "app", False) or getattr(args, "tray", False):
        args.ui = True
    if getattr(args, "ui", False) and not hasattr(args, "open_browser"):
        args.open_browser = True
    paths = get_paths()
    config = _config(paths)
    models = _registry(paths)
    try:
        spec = models.require(args.model) if args.model else models.default_for("router")
    except RegistryError as exc:
        console.print(f"[red]model error:[/red] {exc}")
        return 2
    registry = build_registry(blocked_hosts=config.security.blocked_hosts)

    def answer(message: str) -> str:
        with _state(paths) as (db, bus):
            try:
                extra = _zones_extra(paths)
            except ZoneManifestError as exc:
                raise RuntimeError(str(exc)) from exc
            zones = ProtectedZones(paths.root, extra_rules=extra)
            permissions = PermissionEngine(
                db,
                autonomy=config.autonomy,
                workspace=str(paths.root),
                events=bus,
            )
            executor = ToolExecutor(
                registry,
                db,
                permissions,
                zones,
                workspace=paths.root,
                events=bus,
                killswitch=_kill_switch(paths, db, bus),
            )
            store = _memory(paths, db)
            context = ContextBuilder(store).build(message)
            task_id = f"api-{uuid.uuid4().hex[:12]}"
            store.add_message("user", message, task_id=task_id)
            with OllamaBackend(timeout=args.timeout) as backend:
                gateway = ModelGateway(
                    models,
                    backend,
                    vram_total_mb=config.model_gateway.vram_total_mb,
                    safety_margin_mb=config.model_gateway.safety_margin_mb,
                    allow_cpu_fallback=config.model_gateway.cpu_fallback,
                    events=bus,
                )

                def llm(messages: list[dict[str, str]]) -> str:
                    options: dict[str, int] = {"num_predict": args.num_predict}
                    if spec.context > 0:
                        options["num_ctx"] = spec.context
                    return gateway.chat(
                        spec.id,
                        messages,
                        keep_alive=args.keep_alive,
                        options=options,
                    )

                result = KiviBrain(llm, registry, executor, permissions).ask(
                    message,
                    context=context.text,
                    task_id=task_id,
                )
                store.add_message(
                    "assistant",
                    result.answer,
                    trust="untrusted:tool",
                    task_id=task_id,
                )
                return result.answer

    ui_speech = _speech_pipeline(config) if getattr(args, "ui", False) else None
    ui_stt = (
        (lambda data: _transcribe_wav_bytes(
            data,
            getattr(args, "whisper_model", "data/models/faster-whisper-large-v3-turbo"),
        ))
        if getattr(args, "ui", False)
        else None
    )
    api = LocalApi(
        answer,
        host=args.host,
        port=args.port,
        static_dir=(paths.root / "web" / "ui") if getattr(args, "ui", False) else None,
        docs_dir=(paths.root / "docs") if getattr(args, "ui", False) else None,
        tts_handler=(
            (lambda text: _wav_bytes_from_results(
                ui_speech.synthesize(text, paths.cache / "ui-tts")
            ))
            if ui_speech is not None
            else None
        ),
        stt_handler=ui_stt,
        history_handler=lambda limit: _serve_history(paths, limit),
        voices_handler=lambda: [
            {
                "id": profile.id,
                "name": profile.name,
                "engine": profile.engine,
                "transcript": profile.transcript,
                "active": bool(
                    VoiceLibrary(paths.data / "voices").active()
                    and VoiceLibrary(paths.data / "voices").active().id == profile.id
                ),
            }
            for profile in VoiceLibrary(paths.data / "voices").list()
        ],
        settings_handler=lambda: {
            "voice_provider": config.voice.provider,
            "model": config.voice.model,
            "speaker": config.voice.speaker,
            "sample_rate": config.voice.sample_rate,
            "fish_model": config.voice.fish_model,
        },
        voice_select_handler=lambda voice_id: {
            "ok": True,
            "id": VoiceLibrary(paths.data / "voices").set_active(voice_id).id,
        },
    )
    try:
        api.start()
    except OSError as exc:
        console.print(f"[red]api error:[/red] {_plain(str(exc))}")
        return 1
    if getattr(args, "open_browser", False):
        if getattr(args, "app", False):
            _open_app_window(api.url)
        else:
            webbrowser.open(api.url)
    if getattr(args, "tray", False):
        _run_tray(api)
    console.print(f"Kivi API: [bold]{api.url}[/bold] (Ctrl+C чтобы остановить)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        console.print("\nОстанавливаю API.")
    finally:
        api.stop()
    return 0


def _qwen_config(
    config: KiviConfig,
    *,
    reference: str = "",
    reference_text: str = "",
    pitch: float = 0.0,
) -> QwenTtsConfig:
    voice = config.voice
    reference_path = Path(reference) if reference else voice.qwen_reference
    transcript = reference_text or voice.qwen_reference_text
    if not reference:
        try:
            active = VoiceLibrary(get_paths().data / "voices").active()
        except VoiceLibraryError:
            active = None
        if active is not None:
            reference_path = active.wav_path(get_paths().data / "voices")
            transcript = transcript or active.transcript
    return QwenTtsConfig(
        model_dir=voice.qwen_model_dir,
        design_dir=voice.qwen_design_dir,
        reference_wav=reference_path,
        reference_text=transcript,
        device=voice.qwen_device,
        pitch_semitones=pitch,
    )


def _fish_config(config: KiviConfig, reference_id: str = "") -> FishTtsConfig:
    voice = config.voice
    return FishTtsConfig(
        reference_id=reference_id or voice.fish_reference_id or DEFAULT_REFERENCE_ID,
        model=voice.fish_model,
        base_url=voice.fish_base_url,
        sample_rate=voice.fish_sample_rate,
        latency=voice.fish_latency,
    )


def cmd_voice_design(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    target = Path(args.out) if args.out else config.voice.qwen_reference
    text = args.text or "Привет! Меня зовут Киви. Я твой локальный ассистент."
    designer = QwenVoiceDesigner(_qwen_config(config, pitch=args.pitch))
    try:
        result = designer.design(
            text,
            target,
            instruct=args.instruct or DEFAULT_VOICE_INSTRUCT,
        )
    except QwenTtsError as exc:
        console.print(f"[red]voice design error:[/red] {_plain(str(exc))}")
        return 1
    console.print(f"voice reference: {result.path} ({result.duration_s:.2f}s)")
    console.print(f"transcript: {result.path.with_suffix('.txt')}")
    return 0


def cmd_say(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    target = Path(args.out) if args.out else paths.cache / "tts"
    engine = args.engine or ("qwen" if config.voice.provider == "qwen" else "silero")
    if engine == "qwen":
        output = target if target.suffix else target / "qwen.wav"
        try:
            result = QwenTtsEngine(
                _qwen_config(
                    config,
                    reference=args.reference,
                    reference_text=args.reference_text,
                    pitch=args.pitch,
                )
            ).synth(args.text, output)
        except QwenTtsError as exc:
            console.print(f"[red]qwen tts error:[/red] {_plain(str(exc))}")
            return 1
        results = [result]
    elif engine == "fish":
        output = target if target.suffix else target / "fish.wav"
        try:
            result = FishAudioTts(
                _fish_config(config, reference_id=args.reference_id)
            ).synth(args.text, output)
        except FishTtsError as exc:
            console.print(f"[red]fish audio error:[/red] {_plain(str(exc))}")
            return 1
        results = [result]
    else:
        if engine == "piper":
            pipeline = SpeechPipeline(
                PiperTts(),
                fallback=None,
                enable_fallback=False,
                speaker=config.voice.speaker,
                sample_rate=config.voice.sample_rate,
            )
        else:
            pipeline = _speech_pipeline(config)
        try:
            results = pipeline.synthesize(args.text, target)
        except TtsError as exc:
            console.print(f"[red]tts error:[/red] {_plain(str(exc))}")
            return 1
    for result in results:
        console.print(f"{result.engine}: {result.path} ({result.duration_s:.2f}s)")
    if args.play:
        try:
            import winsound

            for result in results:
                winsound.PlaySound(str(result.path), winsound.SND_FILENAME)
        except Exception as exc:  # noqa: BLE001 - audio is best effort
            console.print(f"[yellow]playback unavailable:[/yellow] {_plain(str(exc))}")
    return 0


def cmd_coder(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    models = _registry(paths)
    try:
        spec = models.require(args.model) if args.model else models.default_for("router")
    except RegistryError as exc:
        console.print(f"[red]model error:[/red] {exc}")
        return 2
    registry = build_registry(blocked_hosts=config.security.blocked_hosts)

    task_id = f"tsk_{uuid.uuid4().hex[:12]}"
    project = paths.data / "projects" / task_id
    project.mkdir(parents=True, exist_ok=True)
    with _state(paths) as (db, bus):
        try:
            extra = _zones_extra(paths)
        except ZoneManifestError as exc:
            console.print(f"[red]protected zones error:[/red] {exc}")
            return 1
        zones = ProtectedZones(project, extra_rules=extra)
        permissions = PermissionEngine(
            db,
            autonomy=config.autonomy,
            workspace=str(project),
            events=bus,
        )
        executor = ToolExecutor(
            registry,
            db,
            permissions,
            zones,
            workspace=project,
            events=bus,
            killswitch=_kill_switch(paths, db, bus),
        )

        def approve(tool: str, payload: dict[str, Any]) -> bool:
            console.print(f"[yellow]Требуется подтверждение:[/yellow] {tool}")
            if args.yes:
                return True
            try:
                answer = input("Разрешить? [y/N] ").strip().lower()
            except EOFError:
                return False
            return answer in {"y", "yes", "д", "да"}

        try:
            with OllamaBackend(timeout=args.timeout) as backend:
                gateway = ModelGateway(
                    models,
                    backend,
                    vram_total_mb=config.model_gateway.vram_total_mb,
                    safety_margin_mb=config.model_gateway.safety_margin_mb,
                    allow_cpu_fallback=config.model_gateway.cpu_fallback,
                    events=bus,
                )

                def llm(messages: list[dict[str, str]]) -> str:
                    options: dict[str, int] = {"num_predict": args.num_predict}
                    if spec.context > 0:
                        options["num_ctx"] = spec.context
                    return gateway.chat(
                        spec.id,
                        messages,
                        keep_alive=args.keep_alive,
                        options=options,
                    )

                coder = Coder(
                    llm,
                    registry,
                    executor,
                    permissions,
                    max_fixes=args.max_fixes,
                    test_command=args.test_command,
                )
                result = coder.run(args.goal, task_id=task_id, approve=approve)
        except (BackendError, GatewayError) as exc:
            console.print(f"[red]model error:[/red] {_plain(str(exc))}")
            return 1

    console.print(
        f"coder: ok={result.ok} attempts={result.attempts} "
        f"files={len(result.files)} reason={result.stopped_reason}"
    )
    console.print(f"project: {project}")
    if result.test_output:
        console.print(_plain(result.test_output[-4000:]))
    if result.last_error:
        console.print(f"[yellow]{_plain(result.last_error)}[/yellow]")
    return 0 if result.ok else 1


def cmd_browse(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    registry = build_registry(blocked_hosts=config.security.blocked_hosts)
    task_id = f"tsk_{uuid.uuid4().hex[:12]}"
    with _state(paths) as (db, bus):
        try:
            extra = _zones_extra(paths)
        except ZoneManifestError as exc:
            console.print(f"[red]protected zones error:[/red] {exc}")
            return 1
        zones = ProtectedZones(paths.root, extra_rules=extra)
        permissions = PermissionEngine(
            db,
            autonomy=config.autonomy,
            workspace=str(paths.root),
            events=bus,
        )
        executor = ToolExecutor(
            registry,
            db,
            permissions,
            zones,
            workspace=paths.root,
            events=bus,
            killswitch=_kill_switch(paths, db, bus),
        )
        grant = permissions.issue(task_id, ["browser.control", "browser.read"])
        try:
            opened = executor.run(
                "browser.open",
                {"url": args.url},
                grant=grant,
            )
            if not opened.ok:
                console.print(f"[red]browser error:[/red] {_plain(opened.error)}")
                return 1
            console.print(_plain(opened.output))
            read = executor.run(
                "browser.read",
                {"selector": args.selector, "max_chars": args.max_chars},
                grant=grant,
            )
        finally:
            executor.run("browser.close", {}, grant=grant)
    if not read.ok:
        console.print(f"[red]browser error:[/red] {_plain(read.error)}")
        return 1
    console.print(_plain(read.output))
    return 0


def _run_direct_tool(
    paths: Paths,
    config: KiviConfig,
    name: str,
    args: dict[str, Any],
    capabilities: list[str],
    *,
    approved: bool = False,
) -> ToolResult:
    """Run one tool through the normal executor without a planner."""
    registry = build_registry(blocked_hosts=config.security.blocked_hosts)
    task_id = f"tsk_{uuid.uuid4().hex[:12]}"
    with _state(paths) as (db, bus):
        try:
            extra = _zones_extra(paths)
        except ZoneManifestError as exc:
            raise RuntimeError(str(exc)) from exc
        zones = ProtectedZones(paths.root, extra_rules=extra)
        permissions = PermissionEngine(
            db,
            autonomy=config.autonomy,
            workspace=str(paths.root),
            events=bus,
        )
        executor = ToolExecutor(
            registry,
            db,
            permissions,
            zones,
            workspace=paths.root,
            events=bus,
            killswitch=_kill_switch(paths, db, bus),
        )
        grant = permissions.issue(task_id, capabilities)
        return executor.run(name, args, grant=grant, approved=approved)


def cmd_mail(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    if args.mail_action == "status":
        auth = GmailAuth()
        console.print(f"token: {'present' if auth.has_token else 'missing'} ({auth.token_path})")
        secret_state = "present" if auth.client_secret_path.is_file() else "missing"
        console.print(f"client secret: {secret_state}")
        console.print("scopes: gmail.readonly + gmail.compose; send is disabled")
        return 0
    if args.mail_action == "auth":
        try:
            credentials = GmailAuth().authorize()
        except GmailAuthError as exc:
            console.print(f"[red]gmail auth error:[/red] {_plain(str(exc))}")
            return 1
        console.print(f"authorized: {getattr(credentials, 'scopes', 'ok')}")
        return 0

    action = args.mail_action
    approved = bool(getattr(args, "approve", False))
    if action in {"list", "search"}:
        name = "mail.list" if action == "list" else "mail.search"
        query = args.query or ""
        payload = {"query": query, "max_results": args.limit}
    elif action == "read":
        name, payload = "mail.read", {"message_id": args.message_id}
    elif action == "attachment":
        name = "mail.attachment"
        payload = {
            "message_id": args.message_id,
            "attachment_id": args.attachment_id,
        }
    else:
        name = "mail.draft"
        payload = {
            "to": list(args.to or []),
            "subject": args.subject,
            "body": args.body,
        }
        if not approved and sys.stdin.isatty():
            try:
                approved = input("Создать черновик? [y/N] ").strip().lower() in {
                    "y",
                    "yes",
                    "д",
                    "да",
                }
            except EOFError:
                approved = False
    try:
        result = _run_direct_tool(
            paths,
            config,
            name,
            payload,
            ["mail.read", "mail.draft"],
            approved=approved,
        )
    except RuntimeError as exc:
        console.print(f"[red]{_plain(str(exc))}[/red]")
        return 1
    if not result.ok:
        console.print(f"[red]mail error:[/red] {_plain(result.error)}")
        return 1
    console.print(_plain(result.output))
    return 0


def cmd_telegram(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    action = args.telegram_action
    if action == "status":
        name, payload, caps = "telegram.status", {}, ["telegram.read"]
    elif action == "updates":
        name = "telegram.updates"
        payload = {"limit": args.limit}
        caps = ["telegram.read"]
    else:
        name = "telegram.send"
        payload = {"chat_id": args.chat_id, "text": args.text}
        caps = ["telegram.send"]
    approved = bool(getattr(args, "approve", False))
    if action == "send" and not approved and sys.stdin.isatty():
        try:
            approved = input("Отправить сообщение? [y/N] ").strip().lower() in {
                "y",
                "yes",
                "д",
                "да",
            }
        except EOFError:
            approved = False
    try:
        result = _run_direct_tool(paths, config, name, payload, caps, approved=approved)
    except RuntimeError as exc:
        console.print(f"[red]{_plain(str(exc))}[/red]")
        return 1
    if not result.ok:
        console.print(f"[red]telegram error:[/red] {_plain(result.error)}")
        return 1
    console.print(_plain(result.output))
    return 0


def _microphone_available() -> bool:
    """Return False when sounddevice has no input device or probe hangs."""
    result: list[bool] = []

    def probe() -> None:
        try:
            import sounddevice

            sounddevice.query_devices(kind="input")
            result.append(True)
        except Exception:  # noqa: BLE001 - missing portaudio/device
            result.append(False)

    worker = threading.Thread(target=probe, daemon=True)
    worker.start()
    worker.join(1.5)
    return bool(result and result[0])


def cmd_listen(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    if not args.mic:
        console.print(
            "[yellow]voice:[/yellow] microphone capture is opt-in; add --mic "
            "to open the default input device"
        )
        return 1
    if not _microphone_available():
        console.print(
            "[red]voice error:[/red] no microphone/input device is available; "
            "use `kivi say` for TTS or test the pipeline with fakes"
        )
        return 1
    models = _registry(paths)
    try:
        spec = models.require(args.model) if args.model else models.default_for("router")
    except RegistryError as exc:
        console.print(f"[red]model error:[/red] {exc}")
        return 2
    registry = build_registry(blocked_hosts=config.security.blocked_hosts)
    task_id = f"voice-{uuid.uuid4().hex[:12]}"
    with _state(paths) as (db, bus):
        try:
            extra = _zones_extra(paths)
        except ZoneManifestError as exc:
            console.print(f"[red]protected zones error:[/red] {exc}")
            return 1
        zones = ProtectedZones(paths.root, extra_rules=extra)
        permissions = PermissionEngine(
            db,
            autonomy=config.autonomy,
            workspace=str(paths.root),
            events=bus,
        )
        executor = ToolExecutor(
            registry,
            db,
            permissions,
            zones,
            workspace=paths.root,
            events=bus,
            killswitch=_kill_switch(paths, db, bus),
        )
        store = _memory(paths, db)
        builder = ContextBuilder(store)
        pipeline = _speech_pipeline(config) if args.speak else None

        with OllamaBackend(timeout=args.timeout) as backend:
            gateway = ModelGateway(
                models,
                backend,
                vram_total_mb=config.model_gateway.vram_total_mb,
                safety_margin_mb=config.model_gateway.safety_margin_mb,
                allow_cpu_fallback=config.model_gateway.cpu_fallback,
                events=bus,
            )

            def llm(messages: list[dict[str, str]]) -> str:
                options: dict[str, int] = {"num_predict": args.num_predict}
                if spec.context > 0:
                    options["num_ctx"] = spec.context
                return gateway.chat(
                    spec.id,
                    messages,
                    keep_alive=args.keep_alive,
                    options=options,
                )

            brain = KiviBrain(llm, registry, executor, permissions)

            def answer(text: str) -> str:
                context = builder.build(text, task_id=task_id)
                result = brain.ask(text, context=context.text, task_id=task_id)
                return result.answer

            def speak(text: str) -> None:
                if pipeline is not None:
                    _speak(text, pipeline, cache=paths.cache / "tts", play=args.speak)

            session = VoiceSession(
                SoundDeviceSource(),
                VadSegmenter(EnergyVad()),
                WhisperTranscriber(args.whisper_model),
                answer=answer,
                speak=speak,
                wake=TextWakeWordDetector().detect,
                max_turns=args.turns,
            )
            turns = 0
            try:
                while turns < args.turns:
                    turn = session.run_once(push_to_talk=args.push_to_talk)
                    if not turn.addressed and not turn.text:
                        continue
                    turns += 1
                    console.print(f"[dim]Вы:[/dim] {_plain(turn.text)}")
                    console.print(f"[cyan]Kivi:[/cyan] {_plain(turn.answer)}")
                    console.print(f"[dim]{turn.metrics.as_dict()}[/dim]")
                    if turn.stopped:
                        break
            except (AudioUnavailable, SttUnavailable, WakeWordUnavailable) as exc:
                console.print(f"[red]voice error:[/red] {_plain(str(exc))}")
                return 1
    return 0


def cmd_crm(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    action = args.crm_action
    approved = bool(getattr(args, "approve", False))
    if action == "read":
        name, payload, caps = "crm.read", {}, ["crm.read"]
    elif action == "fill":
        checklist: dict[str, str] = {}
        for item in args.field or []:
            key, separator, value = item.partition("=")
            if not separator:
                console.print(f"[red]bad --field:[/red] {item!r}; expected label=value")
                return 2
            checklist[key.strip()] = value.strip()
        name = "crm.fill"
        payload = {"comment": args.comment or "", "checklist": checklist}
        caps = ["crm.write"]
    elif action == "act":
        rows = []
        for item in args.row or []:
            parts = [part.strip() for part in item.split("|")]
            if len(parts) != 3:
                console.print(f"[red]bad --row:[/red] {item!r}; expected name|price|warranty")
                return 2
            rows.append(
                {
                    "name": parts[0],
                    "price": int(parts[1] or 0),
                    "warranty": int(parts[2] or 0),
                }
            )
        name, payload, caps = "crm.act", {"rows": rows}, ["crm.write"]
    else:
        name = "crm.task"
        payload = {"comment": args.comment, "when_text": args.when or ""}
        caps = ["crm.write"]

    if action != "read" and not approved and sys.stdin.isatty():
        try:
            approved = input("Заполнить поля в CRM (без сохранения)? [y/N] ").strip().lower() in {
                "y",
                "yes",
                "д",
                "да",
            }
        except EOFError:
            approved = False
    try:
        result = _run_direct_tool(paths, config, name, payload, caps, approved=approved)
    except RuntimeError as exc:
        console.print(f"[red]{_plain(str(exc))}[/red]")
        return 1
    if not result.ok:
        console.print(f"[red]crm error:[/red] {_plain(result.error)}")
        return 1
    console.print(_plain(result.output))
    return 0


def cmd_voice_library(args: argparse.Namespace) -> int:
    paths = get_paths()
    library = VoiceLibrary(paths.data / "voices")
    action = args.voice_action
    try:
        if action == "list":
            profiles = library.list()
            if not profiles:
                console.print("voice library is empty")
                return 0
            table = Table("ID", "Name", "Engine", "Duration", "Transcript", "Active")
            active = library.active()
            for profile in profiles:
                table.add_row(
                    profile.id,
                    profile.name,
                    profile.engine,
                    f"{profile.duration_s:.2f}s",
                    "yes" if profile.has_transcript else "no",
                    "yes" if active and active.id == profile.id else "",
                )
            console.print(table)
            return 0
        if action == "add":
            if bool(args.file) == bool(args.url):
                console.print("[red]укажите ровно один источник: --file или --url[/red]")
                return 2
            if not args.consent:
                console.print("[red]нужно явное --consent: у вас есть права на голос[/red]")
                return 2
            if args.file:
                profile = library.add_file(
                    args.file,
                    args.name,
                    transcript=args.transcript,
                    consent=True,
                    engine=args.engine,
                )
            else:
                profile = library.add_url(
                    args.url,
                    args.name,
                    transcript=args.transcript,
                    consent=True,
                    engine=args.engine,
                )
            console.print(f"added: {profile.id} -> {profile.reference_wav}")
            if not profile.has_transcript:
                console.print(
                    "[yellow]transcript пуст: для Qwen clone добавьте --transcript[/yellow]"
                )
            return 0
        if action == "use":
            profile = library.set_active(args.name)
            path = library.reference_path(profile)
            console.print(f"active: {profile.name} -> {path}")
            return 0
        if action == "remove":
            profile = library.remove(args.name)
            console.print(f"removed: {profile.id}")
            return 0
    except VoiceLibraryError as exc:
        console.print(f"[red]voice library error:[/red] {_plain(str(exc))}")
        return 1
    console.print(f"[red]unknown voice action: {action}[/red]")
    return 2


def cmd_audio(args: argparse.Namespace) -> int:
    """List output devices or play a short test tone."""
    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:
        console.print(f"[red]audio unavailable:[/red] {_plain(str(exc))}")
        return 1
    default = sd.default.device[1] if isinstance(sd.default.device, (list, tuple)) else None
    if args.audio_action == "devices":
        for index, device in enumerate(sd.query_devices()):
            if int(device["max_output_channels"]) <= 0:
                continue
            marker = " *" if index == default else ""
            console.print(
                f"{index:>3}{marker} {device['name']} "
                f"({device['max_output_channels']} ch, {device['default_samplerate']:.0f} Hz)"
            )
        return 0
    device: int | str | None = args.device
    if isinstance(device, str) and device.isdigit():
        device = int(device)
    try:
        sample_rate = 48000
        tone = 0.15 * np.sin(
            2 * np.pi * 440 * np.arange(sample_rate, dtype=np.float32) / sample_rate
        )
        sd.play(tone, sample_rate, device=device, blocking=True)
    except Exception as exc:  # noqa: BLE001 - audio is best effort
        console.print(f"[red]audio test failed:[/red] {_plain(str(exc))}")
        return 1
    console.print("audio test played (440 Hz, 1 s)")
    return 0


def cmd_media_sdxl(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    payload = {
        "prompt": args.prompt,
        "width": args.width,
        "height": args.height,
    }
    if args.negative:
        payload["negative_prompt"] = args.negative
    if args.steps is not None:
        payload["steps"] = args.steps
    if args.cfg is not None:
        payload["cfg_scale"] = args.cfg
    if args.seed is not None:
        payload["seed"] = args.seed
    if args.sampler:
        payload["sampler"] = args.sampler
    try:
        result = _run_direct_tool(
            paths,
            config,
            "media.sdxl",
            payload,
            ["media.generate"],
        )
    except RuntimeError as exc:
        console.print(f"[red]{_plain(str(exc))}[/red]")
        return 1
    if not result.ok:
        console.print(f"[red]sdxl error:[/red] {_plain(result.error)}")
        return 1
    console.print(_plain(result.output))
    return 0


def cmd_media(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    payload = {
        "prompt": args.prompt,
        "width": args.width,
        "height": args.height,
        "model": args.model,
        "seed": args.seed,
    }
    if args.negative:
        payload["negative_prompt"] = args.negative
    try:
        result = _run_direct_tool(
            paths,
            config,
            "media.image",
            payload,
            ["media.generate"],
        )
    except RuntimeError as exc:
        console.print(f"[red]{_plain(str(exc))}[/red]")
        return 1
    if not result.ok:
        console.print(f"[red]media error:[/red] {_plain(result.error)}")
        return 1
    console.print(_plain(result.output))
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    registry = _registry(paths)
    try:
        spec = registry.require(args.model) if args.model else registry.default_for("router")
    except RegistryError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    task_id = f"tsk_{uuid.uuid4().hex[:12]}"
    messages = [{"role": "user", "content": args.prompt}]
    options: dict[str, int] = {"num_predict": args.num_predict}
    if spec.context > 0:
        options["num_ctx"] = spec.context
    answer = ""

    with _state(paths) as (db, bus):
        db.create_task(task_id, "ask", {"prompt": args.prompt, "model": spec.id})
        bus.emit(
            "ask.request",
            task_id=task_id,
            source="cli",
            payload={"model": spec.id, "priority": 0},
        )
        try:
            with OllamaBackend(timeout=args.timeout) as backend:
                gateway = ModelGateway(
                    registry,
                    backend,
                    vram_total_mb=config.model_gateway.vram_total_mb,
                    safety_margin_mb=config.model_gateway.safety_margin_mb,
                    allow_cpu_fallback=config.model_gateway.cpu_fallback,
                    events=bus,
                )
                if args.stream:
                    console.print(f"[cyan]Kivi[/cyan] · [dim]{spec.id}[/dim]")
                    chunks: list[str] = []
                    for chunk in gateway.chat_stream(
                        spec.id,
                        messages,
                        task_id=task_id,
                        priority=0,
                        keep_alive=args.keep_alive,
                        options=options,
                        wait_s=args.wait,
                    ):
                        chunks.append(chunk)
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    answer = "".join(chunks)
                else:
                    answer = gateway.chat(
                        spec.id,
                        messages,
                        task_id=task_id,
                        priority=0,
                        keep_alive=args.keep_alive,
                        options=options,
                        wait_s=args.wait,
                    )
                metrics = gateway.metrics()
        except (GatewayError, BackendError) as exc:
            db.update_task(task_id, "failed", {"error": str(exc)})
            bus.emit("ask.failed", task_id=task_id, source="cli", payload={"error": str(exc)})
            console.print(f"[red]model error:[/red] {exc}")
            return 1

        db.update_task(task_id, "done", {"model": spec.id, "answer": answer[:4000]})
        bus.emit(
            "ask.done",
            task_id=task_id,
            source="cli",
            payload={"model": spec.id, "chars": len(answer)},
        )

    if not args.stream:
        console.print(f"[cyan]Kivi[/cyan] · [dim]{spec.id}[/dim]")
        console.print(answer or "(empty answer)")
    console.print(
        f"[dim]loads={metrics['loads']} evictions={metrics['evictions']} "
        f"last_load={metrics['last_load_ms']} ms[/dim]"
    )
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    registry = _registry(paths)
    try:
        spec = registry.require(args.model) if args.model else registry.default_for("embedding")
    except RegistryError as exc:
        console.print(f"[red]model error:[/red] {exc}")
        return 2

    task_id = f"tsk_{uuid.uuid4().hex[:12]}"
    with _state(paths) as (db, bus):
        db.create_task(task_id, "embed", {"text": args.text, "model": spec.id})
        bus.emit(
            "embed.request",
            task_id=task_id,
            source="cli",
            payload={"model": spec.id, "chars": len(args.text)},
        )
        try:
            with OllamaBackend(timeout=args.timeout) as backend:
                gateway = ModelGateway(
                    registry,
                    backend,
                    vram_total_mb=config.model_gateway.vram_total_mb,
                    safety_margin_mb=config.model_gateway.safety_margin_mb,
                    allow_cpu_fallback=config.model_gateway.cpu_fallback,
                    events=bus,
                )
                vectors = gateway.embed(
                    spec.id,
                    [args.text],
                    task_id=task_id,
                    priority=0,
                    keep_alive=args.keep_alive,
                    wait_s=args.wait,
                )
                metrics = gateway.metrics()
        except (GatewayError, BackendError) as exc:
            db.update_task(task_id, "failed", {"error": str(exc)})
            bus.emit("embed.failed", task_id=task_id, source="cli", payload={"error": str(exc)})
            console.print(f"[red]embedding error:[/red] {exc}")
            return 1

        vector = vectors[0] if vectors else []
        db.update_task(task_id, "done", {"model": spec.id, "dim": len(vector)})
        bus.emit(
            "embed.done",
            task_id=task_id,
            source="cli",
            payload={"model": spec.id, "dim": len(vector)},
        )

    preview = ", ".join(f"{value:.4f}" for value in vector[:8])
    console.print(f"[cyan]Kivi[/cyan] · [dim]{spec.id}[/dim] · dim={len(vector)}")
    console.print(f"[{preview}, …]")
    console.print(
        f"[dim]loads={metrics['loads']} evictions={metrics['evictions']} "
        f"last_load={metrics['last_load_ms']} ms[/dim]"
    )
    return 0


def cmd_cloud_status(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    try:
        registry = load_cloud_registry(paths.root / "config" / "cloud.yaml")
    except CloudRegistryError as exc:
        console.print(f"[red]cloud registry error:[/red] {exc}")
        return 2

    settings = config.model_gateway.cloud
    console.print(
        f"cloud mode [bold]{settings.mode}[/bold], cost limit ${settings.cost_limit_usd:.2f}"
    )
    with _state(paths) as (db, bus):
        broker = CloudBroker(db, settings, registry, events=bus)
        try:
            rows = broker.status()
        finally:
            broker.close()

    table = Table(title="Cloud providers")
    table.add_column("Provider")
    table.add_column("Kind")
    table.add_column("Key")
    table.add_column("Data")
    table.add_column("Requests today", justify="right")
    table.add_column("Tokens today", justify="right")
    table.add_column("Free limit", overflow="fold")
    for row in rows:
        limit = (
            None if row.remaining_requests is None else row.requests_today + row.remaining_requests
        )
        free = "unlimited" if limit is None else f"{limit}/day"
        table.add_row(
            row.provider,
            row.kind,
            "yes" if row.key_configured else "-",
            row.data_class,
            f"{row.requests_today}/{limit}" if limit is not None else str(row.requests_today),
            str(row.tokens_today),
            free,
        )
    console.print(table)
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        rows = db.recent_events(args.limit)
    if not rows:
        console.print("No events yet.")
        return 0

    table = Table(title=f"Last {len(rows)} events")
    table.add_column("ID", justify="right")
    table.add_column("Time")
    table.add_column("Type")
    table.add_column("Task")
    table.add_column("Payload", overflow="fold")
    for row in rows:
        table.add_row(
            str(row["id"]), row["ts"], row["type"], row["task_id"] or "-", row["payload"] or ""
        )
    console.print(table)
    return 0


def cmd_cloud_scout(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    candidates_path = paths.root / "config" / "cloud-candidates.yaml"
    registry_path = paths.root / "config" / "cloud.yaml"
    try:
        candidates = load_scout_registry(candidates_path)
        registry = load_cloud_registry(registry_path)
    except (CloudRegistryError, ScoutError) as exc:
        console.print(f"[red]cloud scout error:[/red] {exc}")
        return 1

    with _state(paths) as (db, events):
        scout = CloudScout(db, registry, candidates, events=events)
        if args.approve:
            killswitch = _kill_switch(paths, db, events)
            if killswitch.is_engaged():
                console.print("[red]kill switch engaged:[/red] provider approval is blocked")
                return 1
            try:
                provider = scout.approve(args.approve, registry_path)
            except ScoutError as exc:
                console.print(f"[red]cloud scout error:[/red] {exc}")
                return 1
            console.print(
                f"[green]approved[/green] {provider.id} in config/cloud.yaml "
                f"(key env: {provider.api_key_env or 'none'})"
            )
            console.print("Set the environment variable, then check `kivi cloud status`.")
            return 0

        try:
            query = ScoutQuery(
                capability=args.capability,
                data_class=args.data_class,
                allow_training_data=args.allow_training
                or config.model_gateway.cloud.allow_data_for_training,
                allow_watermark=args.allow_watermark,
                include_configured=args.include_configured,
                limit=args.limit,
            )
        except ScoutError as exc:
            console.print(f"[red]cloud scout error:[/red] {exc}")
            return 1

        if args.goal:
            outcome = scout.on_exhausted(
                query,
                goal=args.goal,
                reason=args.reason or "no free provider matched",
                next_step=args.next_step,
            )
            cards = outcome.cards
        else:
            outcome = None
            cards = scout.recommend(query)

    if not cards:
        console.print("No matching free provider in the Scout catalog.")
        if outcome is not None and outcome.handoff is not None:
            console.print(f"Local handoff saved as #{outcome.handoff_id}:")
            console.print(_plain(outcome.handoff.to_prompt()))
        else:
            console.print("Pass --goal to save a local handoff, or run a local model directly.")
        return 0
    for index, card in enumerate(cards):
        if index:
            console.print("")
        console.print(card.to_text())
    if outcome is not None:
        console.print("\nUser confirmation is required: re-run with --approve <id>.")
    return 0


def _handoff_after_quota(
    paths: Paths,
    db: Database,
    events: EventBus,
    config: KiviConfig,
    args: argparse.Namespace,
    reason: str,
) -> int:
    """Checkpoint the task, then run Cloud Scout before giving up on the cloud."""
    goal = args.goal or args.prompt
    if config.model_gateway.cloud.scout_on_quota_exhausted:
        try:
            candidates = load_scout_registry(paths.root / "config" / "cloud-candidates.yaml")
            registry = load_cloud_registry(paths.root / "config" / "cloud.yaml")
        except (CloudRegistryError, ScoutError) as exc:
            console.print(f"[yellow]Cloud Scout unavailable:[/yellow] {exc}")
        else:
            scout = CloudScout(db, registry, candidates, events=events)
            outcome = scout.on_exhausted(
                ScoutQuery(
                    data_class=args.data_class,
                    allow_training_data=config.model_gateway.cloud.allow_data_for_training,
                    allow_watermark=args.allow_watermark,
                ),
                goal=goal,
                reason=reason,
                next_step=args.next_step,
                task_id=args.task_id,
            )
            console.print(f"[yellow]quota exhausted:[/yellow] {reason}")
            console.print(f"Checkpoint saved as handoff #{outcome.handoff_id}.")
            if outcome.found:
                console.print("Cloud Scout found replacement providers:")
                for index, card in enumerate(outcome.cards):
                    if index:
                        console.print("")
                    console.print(card.to_text())
                console.print("\nConfirm one with: kivi cloud scout --approve <id>")
            else:
                console.print("No free provider matched; continue locally from the handoff:")
                console.print(_plain(outcome.handoff.to_prompt() if outcome.handoff else ""))
            return 1

    pack = build_handoff(goal, reason, next_step=args.next_step, task_id=args.task_id)
    handoff_id = save_handoff(db, pack)
    console.print(f"[yellow]quota exhausted:[/yellow] {reason}")
    console.print(f"Local handoff saved as #{handoff_id}:")
    console.print(_plain(pack.to_prompt()))
    return 1


def cmd_cloud_ask(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    try:
        registry = load_cloud_registry(paths.root / "config" / "cloud.yaml")
    except CloudRegistryError as exc:
        console.print(f"[red]cloud error:[/red] {exc}")
        return 1

    with _state(paths) as (db, events):
        broker = CloudBroker(
            db,
            config.model_gateway.cloud,
            registry,
            events=events,
            killswitch=_kill_switch(paths, db, events),
        )
        try:
            provider_id = args.provider or next(
                (status.provider for status in broker.status() if status.key_configured),
                "",
            )
            if not provider_id:
                console.print("[yellow]No cloud provider has a key configured.[/yellow]")
                console.print("Set an api_key_env variable from config/cloud.yaml.")
                return 1
            result = broker.complete(
                provider_id,
                [{"role": "user", "content": args.prompt}],
                model=args.model,
                data_class=args.data_class,
                max_tokens=args.max_tokens,
                task_id=args.task_id,
            )
        except QuotaExhausted as exc:
            return _handoff_after_quota(paths, db, events, config, args, str(exc))
        except CloudError as exc:
            console.print(f"[red]cloud error:[/red] {exc}")
            return 1
        finally:
            broker.close()

    console.print(_plain(result.text))
    console.print(
        f"[dim]{result.provider}/{result.model}: {result.tokens} tokens, "
        f"${result.cost_usd:.4f}[/dim]"
    )
    return 0


def cmd_cloud_handoffs(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        if args.show is not None:
            pack = load_handoff(db, args.show)
            if pack is None:
                console.print(f"[red]unknown handoff:[/red] {args.show}")
                return 1
            console.print(_plain(pack.to_prompt()))
            return 0
        rows = db.recent_handoffs(args.limit)
    if not rows:
        console.print("No saved handoffs.")
        return 0
    table = Table(title=f"Last {len(rows)} handoffs")
    table.add_column("ID", justify="right")
    table.add_column("Time")
    table.add_column("Task", overflow="fold")
    table.add_column("Goal", overflow="fold")
    table.add_column("Reason", overflow="fold")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["created_at"],
            row["task_id"] or "-",
            row["goal"],
            row["reason"],
        )
    console.print(table)
    console.print("Show one with: kivi cloud handoff --show <id>")
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    registry = build_registry()
    specs = registry.specs()
    if getattr(args, "json", False):
        payload = [
            {
                "name": spec.name,
                "description": spec.description,
                "risk": spec.risk,
                "capabilities": list(spec.capabilities),
                "schema": spec.schema,
            }
            for spec in specs
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    table = Table(title="Kivi tools")
    table.add_column("Name", style="bold")
    table.add_column("Risk")
    table.add_column("Capabilities")
    table.add_column("Description", overflow="fold")
    for spec in specs:
        table.add_row(spec.name, spec.risk, ", ".join(spec.capabilities), spec.description)
    console.print(table)
    console.print("Run one with: kivi tools run <name> --args '<json>' [--dry-run] [--approve]")
    return 0


def cmd_tool_run(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    registry = build_registry(blocked_hosts=config.security.blocked_hosts)
    tool = registry.get(args.name)
    if tool is None:
        console.print(f"[red]unknown tool:[/red] {args.name}")
        console.print(f"Known tools: {', '.join(registry.names())}")
        return 1
    try:
        payload = json.loads(args.args)
    except json.JSONDecodeError as exc:
        console.print(f"[red]invalid --args JSON:[/red] {exc}")
        return 2
    if not isinstance(payload, dict):
        console.print("[red]--args must be a JSON object[/red]")
        return 2

    with _state(paths) as (db, events):
        try:
            extra_zones = _zones_extra(paths)
        except ZoneManifestError as exc:
            console.print(f"[red]protected zones error:[/red] {exc}")
            return 1
        zones = ProtectedZones(paths.root, extra_rules=extra_zones)
        permissions = PermissionEngine(
            db,
            autonomy=config.autonomy,
            workspace=str(paths.root),
            events=events,
        )
        task_id = args.task_id or f"cli-{uuid.uuid4().hex[:8]}"
        grant = permissions.issue(task_id, tool.capabilities)
        executor = ToolExecutor(
            registry,
            db,
            permissions,
            zones,
            workspace=paths.root,
            events=events,
            killswitch=_kill_switch(paths, db, events),
        )
        result = executor.run(
            args.name,
            payload,
            grant=grant,
            approved=args.approve,
            dry_run=args.dry_run,
        )

    if result.ok:
        console.print(_plain(result.output))
        if result.truncated:
            console.print("[yellow]output truncated[/yellow]")
        return 0
    console.print(f"[red]tool failed:[/red] {result.error}")
    if "confirmation required" in result.error:
        console.print("Review the dry run, then re-run with --approve to confirm.")
    return 1


def cmd_killswitch(args: argparse.Namespace) -> int:
    paths = get_paths()
    if args.action == "on":
        _config(paths)  # fail early on a broken config, before the flag is written

        def unload() -> None:
            count = _unload_local_models(paths)
            if count:
                console.print(f"unloaded {count} local model(s)")

        on_engage = unload
    else:
        on_engage = None
    with _state(paths) as (db, events):
        switch = _kill_switch(paths, db, events, on_engage=on_engage)
        if args.action == "on":
            state = switch.engage(args.reason)
            console.print(
                f"[red]kill switch engaged[/red] at {state.engaged_at or 'now'}: "
                f"{state.reason}"
            )
            console.print("Tools and cloud calls are blocked until `kivi killswitch off`.")
            return 0
        if args.action == "off":
            switch.disengage()
            console.print("[green]kill switch disengaged[/green]")
            return 0
        state = switch.state()
    if state.engaged:
        console.print(f"[red]ENGAGED[/red] at {state.engaged_at or 'unknown'}: {state.reason}")
        console.print(f"source: {state.source or 'unknown'}")
        console.print("Reset with: kivi killswitch off")
    else:
        console.print("[green]kill switch is not engaged[/green]")
    return 0


def cmd_zones(args: argparse.Namespace) -> int:
    paths = get_paths()
    manifest = paths.root / "config" / "protected-zones.yaml"
    key = paths.root / "private" / "safety" / "zones.key"
    action = args.action

    if action == "init":
        try:
            generate_key(key)
            if not manifest.is_file():
                manifest.parent.mkdir(parents=True, exist_ok=True)
                manifest.write_text("version: 1\nzones: []\n", encoding="utf-8")
            signature = sign_manifest(manifest, key)
        except ZoneManifestError as exc:
            console.print(f"[red]zones error:[/red] {exc}")
            return 1
        console.print(f"key: {key}")
        console.print(f"manifest: {manifest}")
        console.print(f"signature: {signature}")
        return 0

    if action == "sign":
        if not manifest.is_file():
            console.print(f"[red]zones error:[/red] manifest not found: {manifest}")
            return 1
        try:
            signature = sign_manifest(manifest, key)
        except ZoneManifestError as exc:
            console.print(f"[red]zones error:[/red] {exc}")
            return 1
        console.print(f"[green]signed[/green] {manifest} -> {signature.name}")
        return 0

    info = zone_manifest_status(manifest, key)
    builtin = len(ProtectedZones(paths.root).rules)
    if not info.exists:
        console.print(f"No signed manifest. Built-in rules: {builtin}.")
        console.print("Create one with: kivi zones init")
        return 0
    if not info.signed:
        console.print(f"[red]manifest is not trusted:[/red] {info.reason}")
        console.print(f"Built-in rules still active: {builtin}.")
        return 1
    console.print(f"[green]signed manifest[/green] with {info.zones} extra zone(s)")
    console.print(f"built-in rules: {builtin}")
    console.print(f"manifest: {manifest}")
    if action == "check":
        console.print("signature: valid")
    return 0


def cmd_hotkey(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    try:
        spec = parse_hotkey(args.hotkey or config.security.killswitch_hotkey)
    except HotkeyError as exc:
        console.print(f"[red]hotkey error:[/red] {exc}")
        return 1
    if args.check:
        console.print(f"hotkey: {spec.text} (vk=0x{spec.virtual_key:02X})")
        return 0

    with _state(paths) as (db, events):
        switch = _kill_switch(paths, db, events)

        def trigger() -> None:
            state = switch.engage(f"hotkey {spec.text}", source="hotkey")
            console.print(f"[red]kill switch engaged[/red] by {spec.text}: {state.reason}")

        listener = HotkeyListener(spec, WindowsHotkey(), trigger)
        console.print(f"Listening for {spec.text}; Ctrl+C to stop.")
        try:
            handled = listener.run(max_events=1 if args.once else None)
        except HotkeyUnavailable as exc:
            console.print(f"[red]hotkey error:[/red] {exc}")
            return 1
        except KeyboardInterrupt:
            console.print("\nHotkey listener stopped.")
            return 0
    console.print(f"handled {handled} hotkey press(es)")
    return 0


def _memory(paths: Paths, db: Database) -> MemoryStore:
    return MemoryStore(db, paths.artifacts)


def cmd_memory_add(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        try:
            message_id = _memory(paths, db).add_message(
                args.role,
                args.content,
                trust=args.trust,
                task_id=args.task_id,
            )
        except ValueError as exc:
            console.print(f"[red]memory error:[/red] {exc}")
            return 1
    console.print(f"stored message #{message_id}")
    return 0


def cmd_memory_search(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        store = _memory(paths, db)
        facts = store.search_facts(args.query, args.limit)
        messages = store.search_messages(args.query, args.limit)
    if not facts and not messages:
        console.print("Nothing found.")
        return 0
    if facts:
        table = Table(title="Facts")
        table.add_column("Key", style="bold")
        table.add_column("Value", overflow="fold")
        table.add_column("Trust")
        table.add_column("Source", overflow="fold")
        for fact in facts:
            table.add_row(
                _plain(fact["key"]),
                _plain(str(fact["value"])),
                fact["trust"],
                _plain(str(fact["source"] or "-")),
            )
        console.print(table)
    if messages:
        table = Table(title="Messages")
        table.add_column("ID", justify="right")
        table.add_column("Role")
        table.add_column("Trust")
        table.add_column("Snippet", overflow="fold")
        for message in messages:
            table.add_row(
                str(message["id"]),
                message["role"],
                message["trust"],
                _plain(str(message["snippet"] or message["content"])),
            )
        console.print(table)
    return 0


def cmd_memory_recent(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        rows = _memory(paths, db).recent_messages(args.limit)
    if not rows:
        console.print("Memory is empty.")
        return 0
    table = Table(title=f"Last {len(rows)} messages")
    table.add_column("ID", justify="right")
    table.add_column("Role")
    table.add_column("Trust")
    table.add_column("Content", overflow="fold")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["role"],
            row["trust"],
            _plain(str(row["content"])),
        )
    console.print(table)
    return 0


def cmd_memory_remember(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        try:
            fact_id = _memory(paths, db).remember(
                args.key,
                args.value,
                source=args.source,
                trust=args.trust,
                confidence=args.confidence,
            )
        except ValueError as exc:
            console.print(f"[red]memory error:[/red] {exc}")
            return 1
    console.print(f"remembered {args.key} (#{fact_id})")
    return 0


def cmd_memory_forget(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        removed = _memory(paths, db).forget(args.key)
    if not removed:
        console.print(f"no fact named {args.key}")
        return 1
    console.print(f"forgot {args.key}")
    return 0


def cmd_memory_stats(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        stats = _memory(paths, db).stats()
    table = Table(title="Kivi memory")
    table.add_column("Store")
    table.add_column("Rows", justify="right")
    for name, count in stats.items():
        table.add_row(name, str(count))
    console.print(table)
    return 0


def cmd_memory_export(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        payload = _memory(paths, db).export_json()
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        console.print(f"exported to {target}")
    else:
        console.print(_plain(payload))
    return 0


def cmd_memory_import(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        try:
            result = import_directory(
                _memory(paths, db),
                Path(args.path),
                limit_files=args.limit,
                max_file_bytes=args.max_bytes,
                chunk_chars=args.chunk_chars,
            )
        except (OSError, ValueError) as exc:
            console.print(f"[red]memory import error:[/red] {exc}")
            return 1
    console.print(
        f"imported files={result.files} chunks={result.chunks} "
        f"skipped={result.skipped} duplicates={result.duplicates}"
    )
    if result.errors:
        for error in result.errors[:10]:
            console.print(f"[yellow]skipped:[/yellow] {_plain(error)}")
        return 1
    return 0


def cmd_memory_index(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    registry = _registry(paths)
    try:
        spec = registry.require(args.model) if args.model else registry.default_for("embedding")
    except RegistryError as exc:
        console.print(f"[red]model error:[/red] {exc}")
        return 1
    if args.batch < 1 or args.limit < 1:
        console.print("[red]batch and limit must be positive[/red]")
        return 2
    with _state(paths) as (db, bus):
        store = _memory(paths, db)
        messages = store.unembedded("message", spec.id, args.limit)
        facts = store.unembedded("fact", spec.id, args.limit)
        if not messages and not facts:
            console.print("Memory is already indexed.")
            return 0
        rows: list[tuple[str, dict]] = [("message", row) for row in messages]
        rows.extend(("fact", row) for row in facts)
        task_id = f"tsk_{uuid.uuid4().hex[:12]}"
        db.create_task(task_id, "memory-index", {"model": spec.id, "rows": len(rows)})
        try:
            with OllamaBackend(timeout=args.timeout) as backend:
                gateway = ModelGateway(
                    registry,
                    backend,
                    vram_total_mb=config.model_gateway.vram_total_mb,
                    safety_margin_mb=config.model_gateway.safety_margin_mb,
                    allow_cpu_fallback=config.model_gateway.cpu_fallback,
                    events=bus,
                )
                for start in range(0, len(rows), args.batch):
                    batch = rows[start : start + args.batch]
                    texts = [
                        str(row["content"])
                        if owner == "message"
                        else f"{row['key']}: {row['value']}"
                        for owner, row in batch
                    ]
                    vectors = gateway.embed(
                        spec.id,
                        texts,
                        task_id=task_id,
                        priority=1,
                        keep_alive=args.keep_alive,
                    )
                    for (owner, row), vector in zip(batch, vectors, strict=True):
                        store.put_embedding(owner, row["id"], spec.id, vector)
        except (GatewayError, BackendError) as exc:
            db.update_task(task_id, "failed", {"error": str(exc)})
            bus.emit("memory.index_failed", task_id=task_id, payload={"error": str(exc)})
            console.print(f"[red]indexing error:[/red] {exc}")
            return 1
        db.update_task(task_id, "done", {"model": spec.id, "rows": len(rows)})
    console.print(f"indexed {len(rows)} rows with {spec.id}")
    return 0


def cmd_memory_similar(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    registry = _registry(paths)
    try:
        spec = registry.require(args.model) if args.model else registry.default_for("embedding")
    except RegistryError as exc:
        console.print(f"[red]model error:[/red] {exc}")
        return 1
    with _state(paths) as (db, bus):
        store = _memory(paths, db)
        task_id = f"tsk_{uuid.uuid4().hex[:12]}"
        try:
            with OllamaBackend(timeout=args.timeout) as backend:
                gateway = ModelGateway(
                    registry,
                    backend,
                    vram_total_mb=config.model_gateway.vram_total_mb,
                    safety_margin_mb=config.model_gateway.safety_margin_mb,
                    allow_cpu_fallback=config.model_gateway.cpu_fallback,
                    events=bus,
                )
                ann = LanceVectorIndex(Path(args.ann)) if args.ann else None
                hits = HybridSearch(store, ann=ann).search(
                    args.query,
                    embedder=lambda texts: gateway.embed(
                        spec.id,
                        texts,
                        task_id=task_id,
                        priority=1,
                        keep_alive=args.keep_alive,
                    ),
                    model=spec.id,
                    limit=args.limit,
                )
        except (GatewayError, BackendError, VectorIndexError) as exc:
            console.print(f"[red]search error:[/red] {exc}")
            return 1
    if not hits:
        console.print("Nothing found.")
        return 0
    table = Table(title=f"Hybrid search: {args.query}")
    table.add_column("Type")
    table.add_column("ID", justify="right")
    table.add_column("Trust")
    table.add_column("Sources")
    table.add_column("Score", justify="right")
    table.add_column("Text", overflow="fold")
    for hit in hits:
        table.add_row(
            hit.owner_type,
            str(hit.owner_id),
            hit.trust,
            ",".join(hit.sources),
            f"{hit.score:.4f}",
            _plain(hit.text),
        )
    console.print(table)
    return 0


def cmd_memory_vector_sync(args: argparse.Namespace) -> int:
    paths = get_paths()
    target = Path(args.path) if args.path else paths.data / "vector"
    with _state(paths) as (db, _):
        store = _memory(paths, db)
        try:
            if args.rebuild:
                index = rebuild_index(target, store, args.model)
                copied = index.count()
            else:
                index = LanceVectorIndex(target)
                copied = export_from_store(store, index, args.model)
        except VectorIndexError as exc:
            console.print(f"[red]vector index error:[/red] {exc}")
            return 1
    console.print(f"synced {copied} embedding(s) for {args.model} -> {target}")
    return 0


def cmd_memory_context(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        builder = ContextBuilder(_memory(paths, db), max_chars=args.max_chars)
        context = builder.build(args.query, task_id=args.task_id)
    if context.empty:
        console.print("Context is empty.")
        return 0
    console.print(_plain(context.text))
    if context.truncated:
        console.print("[yellow]context truncated to budget[/yellow]")
    return 0


def cmd_memory_summarize(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        try:
            result = summarize_messages(
                _memory(paths, db),
                older_than_days=args.older_than_days,
                max_messages=args.max_messages,
                delete_originals=args.delete,
            )
        except ValueError as exc:
            console.print(f"[red]memory error:[/red] {exc}")
            return 1
    console.print(
        f"summarized groups={result.groups} messages={result.summarized} "
        f"deleted={result.deleted}"
    )
    return 0


def cmd_memory_ttl(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        try:
            result = apply_ttl(_memory(paths, db), days=args.days)
        except ValueError as exc:
            console.print(f"[red]memory error:[/red] {exc}")
            return 1
    console.print(
        f"ttl cutoff={result.cutoff} messages={result.messages_deleted} "
        f"embeddings={result.embeddings_deleted} approvals={result.approvals_deleted}"
    )
    return 0


def cmd_memory_approvals_list(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        store = ApprovalStore(_memory(paths, db))
        rows = store.list(args.task_id or None)
    if not rows:
        console.print("No approvals.")
        return 0
    table = Table(title="Approvals")
    table.add_column("ID", justify="right")
    table.add_column("Task")
    table.add_column("Action")
    table.add_column("Scope")
    table.add_column("Active")
    table.add_column("Expires")
    for row in rows:
        table.add_row(
            str(row["id"]),
            _plain(str(row["task_id"])),
            _plain(str(row["action"])),
            _plain(str(row["scope"] or "-")),
            "yes" if row["active"] else "no",
            str(row["expires_at"] or "never"),
        )
    console.print(table)
    return 0


def cmd_memory_approvals_grant(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        try:
            row = ApprovalStore(_memory(paths, db)).grant(
                args.task_id,
                args.action,
                scope=args.scope,
                ttl_s=args.ttl,
            )
        except ValueError as exc:
            console.print(f"[red]approval error:[/red] {exc}")
            return 1
    console.print(f"granted approval #{row.get('id')} for {args.action}")
    return 0


def cmd_memory_approvals_revoke(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        removed = ApprovalStore(_memory(paths, db)).revoke(args.approval_id)
    if not removed:
        console.print(f"no active approval #{args.approval_id}")
        return 1
    console.print(f"revoked approval #{args.approval_id}")
    return 0


def cmd_memory_approvals_cleanup(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        deleted = ApprovalStore(_memory(paths, db)).cleanup()
    console.print(f"cleaned up {deleted} approval(s)")
    return 0


def cmd_elevated(args: argparse.Namespace) -> int:
    paths = get_paths()
    if args.action == "run" and not args.operation:
        console.print("[red]usage: kivi elevated run <operation>[/red]")
        return 2
    if args.action == "list":
        table = Table(title="Elevated operations")
        table.add_column("Operation", style="bold")
        table.add_column("Params")
        table.add_column("Description", overflow="fold")
        for name in sorted(ALLOWED_OPERATIONS):
            spec = OPERATIONS[name]
            table.add_row(
                spec.name,
                ", ".join(spec.param_names) or "-",
                _plain(spec.description),
            )
        console.print(table)
        console.print("Run one with: kivi elevated run <operation> --approve")
        return 0

    with _state(paths) as (db, events):
        switch = _kill_switch(paths, db, events)
        broker = ElevatedBroker(
            db,
            request_dir=paths.data / "elevated",
            killswitch=switch,
        )
        params: dict[str, str] = {}
        for item in args.param:
            key, separator, value = item.partition("=")
            if not separator or not key.strip():
                console.print(f"[red]invalid --param:[/red] {item} (expected key=value)")
                return 2
            params[key.strip()] = value
        try:
            result = broker.request(args.operation, params, approved=args.approve)
        except ElevatedError as exc:
            console.print(f"[red]elevated error:[/red] {exc}")
            return 1

        if not result.ok:
            console.print(f"[red]elevated {result.status}:[/red] {result.message}")
            return 1
        console.print(f"[green]{result.status}[/green] {result.operation}")
        if result.request_path:
            console.print(f"request: {result.request_path}")
        if not args.wait or not result.result_path:
            return 0
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            if result.result_path.is_file():
                try:
                    payload = read_result(result.result_path)
                except ElevatedError as exc:
                    console.print(f"[red]result error:[/red] {exc}")
                    return 1
                console.print(
                    f"result: ok={payload['ok']} rc={payload['returncode']} "
                    f"finished={payload['finished_at']}"
                )
                if payload["stdout"]:
                    console.print(_plain(payload["stdout"]))
                if payload["stderr"]:
                    console.print(f"[yellow]{_plain(payload['stderr'])}[/yellow]")
                return 0 if payload["ok"] else 1
            time.sleep(0.5)
        console.print(f"[yellow]result not ready after {args.wait:.0f}s[/yellow]")
        return 1


def cmd_audit(args: argparse.Namespace) -> int:
    paths = get_paths()
    with _state(paths) as (db, _):
        rows = db.recent_audit(args.limit)
    if not rows:
        console.print("Audit log is empty.")
        return 0

    table = Table(title=f"Last {len(rows)} audit entries")
    table.add_column("ID", justify="right")
    table.add_column("Time")
    table.add_column("Actor")
    table.add_column("Action")
    table.add_column("Target", overflow="fold")
    table.add_column("Decision")
    table.add_column("Reason", overflow="fold")
    for row in rows:
        table.add_row(
            str(row["id"]),
            row["ts"],
            row["actor"],
            row["action"],
            row["target"] or "",
            row["decision"],
            row["reason"] or "",
        )
    console.print(table)
    return 0


def cmd_config_check(args: argparse.Namespace) -> int:
    paths = get_paths()
    config = _config(paths)
    console.print("[green]config is valid[/green]")
    console.print(f"voice: {config.voice.provider} {config.voice.model}/{config.voice.speaker}")
    console.print(f"avatar: {config.avatar.mode}, outfit {config.avatar.outfit}")
    console.print(
        f"shell: mode {config.shell.mode}, explorer fallback {config.shell.explorer_fallback}"
    )
    console.print(f"mail: {config.mail.provider}, drafts only {config.mail.drafts_only}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kivi", description="Kivi local assistant")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check hardware and local runtimes")
    doctor.set_defaults(func=cmd_doctor)

    models = sub.add_parser("models", help="show model gateway settings and installed models")
    models.set_defaults(func=cmd_models)

    chat = sub.add_parser("chat", help="local Kivi chat with planner and tools")
    chat.add_argument("prompt", nargs="?", default="", help="prompt text; empty starts REPL")
    chat.add_argument("-m", "--model", default=None, help="model id from config/models.yaml")
    chat.add_argument("--keep-alive", default="5m", help="Ollama keep_alive after the answer")
    chat.add_argument("--timeout", type=float, default=180.0, help="HTTP timeout in seconds")
    chat.add_argument("--num-predict", type=int, default=512)
    chat.add_argument("--wait", type=float, default=0.0, help="seconds to wait for a pinned model")
    chat.add_argument(
        "--speak",
        action="store_true",
        help="speak answers aloud with the configured TTS",
    )
    chat.add_argument("--play", action="store_true", help="play WAV files (implied by --speak)")
    chat.add_argument("--avatar", action="store_true", help="open the 2D avatar window")
    chat.add_argument("--yes", action="store_true", help="auto-approve R2/R3 tool steps")
    chat.set_defaults(func=cmd_chat)

    serve = sub.add_parser("serve", help="run the local JSON chat API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("-m", "--model", default=None)
    serve.add_argument("--keep-alive", default="5m")
    serve.add_argument("--timeout", type=float, default=180.0)
    serve.add_argument("--num-predict", type=int, default=512)
    serve.add_argument("--ui", action="store_true", help="serve the Kivi web UI")
    serve.add_argument("--app", action="store_true", help="open a standalone app window")
    serve.add_argument("--tray", action="store_true", help="run a system tray icon")
    serve.add_argument(
        "--whisper-model",
        default="data/models/faster-whisper-large-v3-turbo",
    )
    serve.set_defaults(func=cmd_serve)

    ui = sub.add_parser("ui", help="open the Kivi local interface")
    ui.add_argument("--host", default="127.0.0.1")
    ui.add_argument("--port", type=int, default=8765)
    ui.add_argument("-m", "--model", default=None)
    ui.add_argument("--keep-alive", default="5m")
    ui.add_argument("--timeout", type=float, default=180.0)
    ui.add_argument("--num-predict", type=int, default=512)
    ui.add_argument("--no-browser", action="store_true")
    ui.add_argument("--app", action="store_true", help="standalone app window")
    ui.add_argument("--tray", action="store_true", help="run a system tray icon")
    ui.add_argument(
        "--whisper-model",
        default="data/models/faster-whisper-large-v3-turbo",
    )
    ui.set_defaults(func=cmd_ui)

    say = sub.add_parser("say", help="synthesize text with Silero/Piper/Qwen3-TTS")
    say.add_argument("text")
    say.add_argument("--out", default="")
    say.add_argument("--play", action="store_true")
    say.add_argument("--engine", choices=("silero", "piper", "qwen", "fish"), default="")
    say.add_argument("--reference", default="", help="Qwen reference WAV")
    say.add_argument("--reference-text", default="", help="Qwen reference transcript")
    say.add_argument("--reference-id", default="", help="Fish Audio voice model id")
    say.add_argument("--pitch", type=float, default=0.0, help="Qwen pitch shift in semitones")
    say.set_defaults(func=cmd_say)

    voice = sub.add_parser("voice", help="design and inspect Qwen3-TTS voices")
    voice_sub = voice.add_subparsers(dest="voice_action", required=True)
    voice_design = voice_sub.add_parser("design", help="create a reference voice")
    voice_design.add_argument("--text", default="")
    voice_design.add_argument("--instruct", default="")
    voice_design.add_argument("--out", default="")
    voice_design.add_argument("--pitch", type=float, default=0.0)
    voice_design.set_defaults(func=cmd_voice_design)
    voice_list = voice_sub.add_parser("list")
    voice_list.set_defaults(func=cmd_voice_library)
    voice_add = voice_sub.add_parser("add", help="import a local/URL voice reference")
    voice_add.add_argument("--file", default="")
    voice_add.add_argument("--url", default="")
    voice_add.add_argument("--name", required=True)
    voice_add.add_argument("--transcript", default="")
    voice_add.add_argument("--engine", choices=("qwen", "fish", "silero"), default="qwen")
    voice_add.add_argument("--consent", action="store_true")
    voice_add.set_defaults(func=cmd_voice_library)
    voice_use = voice_sub.add_parser("use")
    voice_use.add_argument("name")
    voice_use.set_defaults(func=cmd_voice_library)
    voice_remove = voice_sub.add_parser("remove")
    voice_remove.add_argument("name")
    voice_remove.set_defaults(func=cmd_voice_library)

    audio = sub.add_parser("audio", help="list audio outputs or test playback")
    audio_sub = audio.add_subparsers(dest="audio_action", required=True)
    audio_sub.add_parser("devices")
    audio_test = audio_sub.add_parser("test")
    audio_test.add_argument("--device", default=None)
    audio.set_defaults(func=cmd_audio)

    media = sub.add_parser("media", help="free image generation")
    media_sub = media.add_subparsers(dest="media_action", required=True)
    media_image = media_sub.add_parser("image")
    media_image.add_argument("prompt")
    media_image.add_argument("--width", type=int, default=1024)
    media_image.add_argument("--height", type=int, default=1024)
    media_image.add_argument("--model", choices=("flux", "turbo"), default="flux")
    media_image.add_argument("--seed", type=int, default=None)
    media_image.add_argument("--negative", default="")
    media_image.set_defaults(func=cmd_media)
    media_sdxl = media_sub.add_parser("sdxl", help="local SDXL via SD WebUI API")
    media_sdxl.add_argument("prompt")
    media_sdxl.add_argument("--width", type=int, default=1024)
    media_sdxl.add_argument("--height", type=int, default=1024)
    media_sdxl.add_argument("--steps", type=int, default=None)
    media_sdxl.add_argument("--cfg", type=float, default=None)
    media_sdxl.add_argument("--seed", type=int, default=None)
    media_sdxl.add_argument(
        "--sampler",
        choices=("DPM++ 2M Karras", "Euler a", "DDIM"),
        default=None,
    )
    media_sdxl.add_argument("--negative", default="")
    media_sdxl.set_defaults(func=cmd_media_sdxl)

    coder = sub.add_parser("coder", help="generate code, run tests and fix up to N times")
    coder.add_argument("goal")
    coder.add_argument("-m", "--model", default=None)
    coder.add_argument("--max-fixes", type=int, default=2)
    coder.add_argument(
        "--test-command",
        default=f'"{sys.executable}" -m pytest -q',
    )
    coder.add_argument("--keep-alive", default="5m")
    coder.add_argument("--timeout", type=float, default=300.0)
    coder.add_argument("--num-predict", type=int, default=2048)
    coder.add_argument("--yes", action="store_true")
    coder.set_defaults(func=cmd_coder)

    browse = sub.add_parser("browse", help="open a URL, read page text and close")
    browse.add_argument("url")
    browse.add_argument("--selector", default="body")
    browse.add_argument("--max-chars", type=int, default=4000)
    browse.set_defaults(func=cmd_browse)

    mail = sub.add_parser("mail", help="Gmail read/search/drafts (no send)")
    mail_sub = mail.add_subparsers(dest="mail_action", required=True)
    mail_status = mail_sub.add_parser("status")
    mail_status.set_defaults(func=cmd_mail)
    mail_auth = mail_sub.add_parser("auth", help="run the OAuth consent flow")
    mail_auth.set_defaults(func=cmd_mail, mail_action="auth")
    mail_list = mail_sub.add_parser("list")
    mail_list.add_argument("query", nargs="?", default="")
    mail_list.add_argument("-n", "--limit", type=int, default=10)
    mail_list.set_defaults(func=cmd_mail)
    mail_search = mail_sub.add_parser("search")
    mail_search.add_argument("query")
    mail_search.add_argument("-n", "--limit", type=int, default=10)
    mail_search.set_defaults(func=cmd_mail)
    mail_read = mail_sub.add_parser("read")
    mail_read.add_argument("message_id")
    mail_read.set_defaults(func=cmd_mail)
    mail_attachment = mail_sub.add_parser("attachment")
    mail_attachment.add_argument("message_id")
    mail_attachment.add_argument("attachment_id")
    mail_attachment.set_defaults(func=cmd_mail)
    mail_draft = mail_sub.add_parser("draft")
    mail_draft.add_argument("--to", action="append", required=True)
    mail_draft.add_argument("--subject", required=True)
    mail_draft.add_argument("--body", required=True)
    mail_draft.add_argument("--approve", action="store_true")
    mail_draft.set_defaults(func=cmd_mail)

    telegram = sub.add_parser("telegram", help="Telegram bot status/updates/send")
    telegram_sub = telegram.add_subparsers(dest="telegram_action", required=True)
    telegram_status = telegram_sub.add_parser("status")
    telegram_status.set_defaults(func=cmd_telegram)
    telegram_updates = telegram_sub.add_parser("updates")
    telegram_updates.add_argument("-n", "--limit", type=int, default=10)
    telegram_updates.set_defaults(func=cmd_telegram)
    telegram_send = telegram_sub.add_parser("send")
    telegram_send.add_argument("chat_id", type=int)
    telegram_send.add_argument("text")
    telegram_send.add_argument("--approve", action="store_true")
    telegram_send.set_defaults(func=cmd_telegram)

    listen = sub.add_parser("listen", help="voice input with VAD, STT and wake word")
    listen.add_argument("-m", "--model", default=None)
    listen.add_argument("--whisper-model", default="small")
    listen.add_argument("--push-to-talk", action="store_true")
    listen.add_argument("--mic", action="store_true", help="open the default microphone")
    listen.add_argument("--turns", type=int, default=1)
    listen.add_argument("--speak", action="store_true")
    listen.add_argument("--keep-alive", default="5m")
    listen.add_argument("--timeout", type=float, default=180.0)
    listen.add_argument("--num-predict", type=int, default=512)
    listen.set_defaults(func=cmd_listen)

    crm = sub.add_parser("crm", help="AltCRM read/fill/act/task without saving")
    crm_sub = crm.add_subparsers(dest="crm_action", required=True)
    crm_read = crm_sub.add_parser("read")
    crm_read.set_defaults(func=cmd_crm)
    crm_fill = crm_sub.add_parser("fill")
    crm_fill.add_argument("--comment", default="")
    crm_fill.add_argument("--field", action="append", default=[])
    crm_fill.add_argument("--approve", action="store_true")
    crm_fill.set_defaults(func=cmd_crm)
    crm_act = crm_sub.add_parser("act")
    crm_act.add_argument("--row", action="append", required=True)
    crm_act.add_argument("--approve", action="store_true")
    crm_act.set_defaults(func=cmd_crm)
    crm_task = crm_sub.add_parser("task")
    crm_task.add_argument("--comment", required=True)
    crm_task.add_argument("--when", default="")
    crm_task.add_argument("--approve", action="store_true")
    crm_task.set_defaults(func=cmd_crm)

    ask = sub.add_parser("ask", help="ask a local model through the Model Gateway")
    ask.add_argument("prompt", help="prompt text")
    ask.add_argument("-m", "--model", default=None, help="model id from config/models.yaml")
    ask.add_argument("--keep-alive", default="5m", help="Ollama keep_alive after the answer")
    ask.add_argument("--timeout", type=float, default=180.0, help="HTTP timeout in seconds")
    ask.add_argument("--stream", action="store_true", help="print answer chunks as they arrive")
    ask.add_argument("--num-predict", type=int, default=512, help="maximum generated tokens")
    ask.add_argument(
        "--wait",
        type=float,
        default=120.0,
        help="seconds to wait behind a pinned model instead of failing",
    )
    ask.set_defaults(func=cmd_ask)

    embed = sub.add_parser("embed", help="embed text with the embedding model")
    embed.add_argument("text", help="text to embed")
    embed.add_argument("-m", "--model", default=None, help="embedding model id")
    embed.add_argument("--keep-alive", default="5m", help="Ollama keep_alive after the request")
    embed.add_argument("--timeout", type=float, default=180.0, help="HTTP timeout in seconds")
    embed.add_argument(
        "--wait",
        type=float,
        default=120.0,
        help="seconds to wait behind a pinned model instead of failing",
    )
    embed.set_defaults(func=cmd_embed)

    cloud = sub.add_parser("cloud", help="cloud providers, quotas and policy")
    cloud_sub = cloud.add_subparsers(dest="cloud_command")
    cloud_status = cloud_sub.add_parser("status", help="provider quota and key status")
    cloud_status.set_defaults(func=cmd_cloud_status)

    cloud_scout = cloud_sub.add_parser("scout", help="find and approve a free provider")
    cloud_scout.add_argument(
        "-c",
        "--capability",
        choices=["llm", "image", "video", "embedding"],
        default="llm",
    )
    cloud_scout.add_argument("--data-class", default="public")
    cloud_scout.add_argument(
        "--allow-training",
        action="store_true",
        help="permit candidates whose free tier may train on data",
    )
    cloud_scout.add_argument(
        "--allow-watermark",
        action="store_true",
        help="permit candidates that always watermark output",
    )
    cloud_scout.add_argument("--include-configured", action="store_true")
    cloud_scout.add_argument("-n", "--limit", type=int, default=3)
    cloud_scout.add_argument("--approve", metavar="ID", help="append this candidate to cloud.yaml")
    cloud_scout.add_argument("--goal", default="", help="save a handoff if nothing matches")
    cloud_scout.add_argument("--reason", default="", help="handoff reason")
    cloud_scout.add_argument("--next-step", default="", help="handoff next step")
    cloud_scout.set_defaults(func=cmd_cloud_scout)

    cloud_ask = cloud_sub.add_parser(
        "ask",
        help="call a free cloud provider; on quota exhaustion checkpoint and scout",
    )
    cloud_ask.add_argument("prompt")
    cloud_ask.add_argument("-p", "--provider", default="", help="provider id from cloud.yaml")
    cloud_ask.add_argument("-m", "--model", default="", help="model id; provider default if empty")
    cloud_ask.add_argument(
        "--data-class",
        default="private",
        choices=["public", "internal", "private", "secret"],
    )
    cloud_ask.add_argument("--max-tokens", type=int, default=512)
    cloud_ask.add_argument("--task-id", default="")
    cloud_ask.add_argument("--goal", default="", help="handoff goal; defaults to the prompt")
    cloud_ask.add_argument("--next-step", default="", help="handoff next step")
    cloud_ask.add_argument(
        "--allow-watermark",
        action="store_true",
        help="permit Scout candidates that watermark output",
    )
    cloud_ask.set_defaults(func=cmd_cloud_ask)

    cloud_handoff = cloud_sub.add_parser("handoff", help="list or show saved handoffs")
    cloud_handoff.add_argument("-n", "--limit", type=int, default=20)
    cloud_handoff.add_argument("--show", type=int, default=None, metavar="ID")
    cloud_handoff.set_defaults(func=cmd_cloud_handoffs)

    cloud.set_defaults(func=cmd_cloud_status)

    tools = sub.add_parser("tools", help="list and run tools")
    tools_sub = tools.add_subparsers(dest="tools_command")
    tools_list = tools_sub.add_parser("list", help="list registered tools")
    tools_list.add_argument("--json", action="store_true", help="print specs as JSON")
    tools_list.set_defaults(func=cmd_tools)
    tools_run = tools_sub.add_parser("run", help="run a tool through the permission engine")
    tools_run.add_argument("name", help="tool name, for example fs.read")
    tools_run.add_argument("--args", default="{}", help="tool arguments as a JSON object")
    tools_run.add_argument("--dry-run", action="store_true", help="show the preview only")
    tools_run.add_argument(
        "--approve",
        action="store_true",
        help="confirm an R2/R3 action after reviewing the preview",
    )
    tools_run.add_argument("--task-id", default="", help="task id for the grant and audit")
    tools_run.set_defaults(func=cmd_tool_run)
    tools.set_defaults(func=cmd_tools)

    killswitch = sub.add_parser("killswitch", help="emergency stop for tools and cloud")
    killswitch.add_argument(
        "action",
        nargs="?",
        choices=("status", "on", "off"),
        default="status",
        help="show, engage or reset the stop flag",
    )
    killswitch.add_argument("--reason", default="user requested stop")
    killswitch.set_defaults(func=cmd_killswitch)

    zones = sub.add_parser("zones", help="inspect or sign extra Protected Zones")
    zones.add_argument(
        "action",
        nargs="?",
        choices=("status", "check", "init", "sign"),
        default="status",
    )
    zones.set_defaults(func=cmd_zones)

    elevated = sub.add_parser("elevated", help="allowlisted UAC admin operations")
    elevated.add_argument("action", nargs="?", choices=("list", "run"), default="list")
    elevated.add_argument("operation", nargs="?", default="")
    elevated.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    elevated.add_argument("--approve", action="store_true")
    elevated.add_argument("--wait", type=float, default=0.0, metavar="SECONDS")
    elevated.set_defaults(func=cmd_elevated)

    hotkey = sub.add_parser("hotkey", help="listen for the global kill-switch hotkey")
    hotkey.add_argument(
        "--hotkey",
        default="",
        help="override the configured hotkey, for example Ctrl+Alt+J",
    )
    hotkey.add_argument("--once", action="store_true", help="stop after the first press")
    hotkey.add_argument("--check", action="store_true", help="validate the hotkey and exit")
    hotkey.set_defaults(func=cmd_hotkey)

    memory = sub.add_parser("memory", help="messages, facts, preferences and artifacts")
    memory_sub = memory.add_subparsers(dest="memory_command")
    memory_add = memory_sub.add_parser("add", help="store a message")
    memory_add.add_argument("content")
    memory_add.add_argument(
        "--role",
        choices=("user", "assistant", "system", "tool"),
        default="user",
    )
    memory_add.add_argument("--trust", default="trusted:user")
    memory_add.add_argument("--task-id", default="")
    memory_add.set_defaults(func=cmd_memory_add)
    memory_search = memory_sub.add_parser("search", help="search facts and messages")
    memory_search.add_argument("query")
    memory_search.add_argument("-n", "--limit", type=int, default=10)
    memory_search.set_defaults(func=cmd_memory_search)
    memory_recent = memory_sub.add_parser("recent", help="show recent messages")
    memory_recent.add_argument("-n", "--limit", type=int, default=20)
    memory_recent.set_defaults(func=cmd_memory_recent)
    memory_remember = memory_sub.add_parser("remember", help="store or update a fact")
    memory_remember.add_argument("key")
    memory_remember.add_argument("value")
    memory_remember.add_argument("--source", default="")
    memory_remember.add_argument("--trust", default="trusted:user")
    memory_remember.add_argument("--confidence", type=float, default=1.0)
    memory_remember.set_defaults(func=cmd_memory_remember)
    memory_forget = memory_sub.add_parser("forget", help="delete a fact")
    memory_forget.add_argument("key")
    memory_forget.set_defaults(func=cmd_memory_forget)
    memory_stats = memory_sub.add_parser("stats", help="show row counts")
    memory_stats.set_defaults(func=cmd_memory_stats)
    memory_export = memory_sub.add_parser("export", help="export memory as JSON")
    memory_export.add_argument("--out", default="")
    memory_export.set_defaults(func=cmd_memory_export)
    memory_import = memory_sub.add_parser("import", help="import text files read-only")
    memory_import.add_argument("path")
    memory_import.add_argument("-n", "--limit", type=int, default=100)
    memory_import.add_argument("--max-bytes", type=int, default=1024 * 1024)
    memory_import.add_argument("--chunk-chars", type=int, default=4000)
    memory_import.set_defaults(func=cmd_memory_import)
    memory_index = memory_sub.add_parser("index", help="build embeddings for memory")
    memory_index.add_argument("-m", "--model", default="")
    memory_index.add_argument("-n", "--limit", type=int, default=1000)
    memory_index.add_argument("--batch", type=int, default=16)
    memory_index.add_argument("--timeout", type=float, default=120.0)
    memory_index.add_argument("--keep-alive", default="0")
    memory_index.set_defaults(func=cmd_memory_index)
    memory_similar = memory_sub.add_parser("similar", help="hybrid semantic search")
    memory_similar.add_argument("query")
    memory_similar.add_argument("-m", "--model", default="")
    memory_similar.add_argument("-n", "--limit", type=int, default=10)
    memory_similar.add_argument("--timeout", type=float, default=120.0)
    memory_similar.add_argument("--keep-alive", default="0")
    memory_similar.add_argument("--ann", default="", help="path to a LanceDB index")
    memory_similar.set_defaults(func=cmd_memory_similar)
    memory_vector = memory_sub.add_parser("vector-sync", help="sync SQLite embeddings to LanceDB")
    memory_vector.add_argument("-m", "--model", default="bge-m3")
    memory_vector.add_argument("--path", default="")
    memory_vector.add_argument("--rebuild", action="store_true")
    memory_vector.set_defaults(func=cmd_memory_vector_sync)
    memory_summarize = memory_sub.add_parser("summarize", help="summarize old messages")
    memory_summarize.add_argument("--older-than-days", type=int, default=7)
    memory_summarize.add_argument("--max-messages", type=int, default=200)
    memory_summarize.add_argument("--delete", action="store_true")
    memory_summarize.set_defaults(func=cmd_memory_summarize)
    memory_ttl = memory_sub.add_parser("ttl", help="delete messages older than the TTL")
    memory_ttl.add_argument("--days", type=int, default=90)
    memory_ttl.set_defaults(func=cmd_memory_ttl)
    memory_approvals = memory_sub.add_parser("approvals", help="durable approvals")
    approvals_sub = memory_approvals.add_subparsers(dest="approvals_command")
    approvals_list = approvals_sub.add_parser("list")
    approvals_list.add_argument("--task-id", default="")
    approvals_list.set_defaults(func=cmd_memory_approvals_list)
    approvals_grant = approvals_sub.add_parser("grant")
    approvals_grant.add_argument("task_id")
    approvals_grant.add_argument("action")
    approvals_grant.add_argument("--scope", default="")
    approvals_grant.add_argument("--ttl", type=int, default=None)
    approvals_grant.set_defaults(func=cmd_memory_approvals_grant)
    approvals_revoke = approvals_sub.add_parser("revoke")
    approvals_revoke.add_argument("approval_id", type=int)
    approvals_revoke.set_defaults(func=cmd_memory_approvals_revoke)
    approvals_cleanup = approvals_sub.add_parser("cleanup")
    approvals_cleanup.set_defaults(func=cmd_memory_approvals_cleanup)
    memory_approvals.set_defaults(func=cmd_memory_approvals_list)
    memory_context = memory_sub.add_parser("context", help="build a context window")
    memory_context.add_argument("query")
    memory_context.add_argument("--task-id", default="")
    memory_context.add_argument("--max-chars", type=int, default=6000)
    memory_context.set_defaults(func=cmd_memory_context)
    memory.set_defaults(func=cmd_memory_stats)

    events = sub.add_parser("events", help="show recent events")
    events.add_argument("-n", "--limit", type=int, default=20)
    events.set_defaults(func=cmd_events)

    audit = sub.add_parser("audit", help="show recent audit entries")
    audit.add_argument("-n", "--limit", type=int, default=20)
    audit.set_defaults(func=cmd_audit)

    config_cmd = sub.add_parser("config", help="config utilities")
    config_sub = config_cmd.add_subparsers(dest="config_command", required=True)
    check = config_sub.add_parser("check", help="validate config/kivi.yaml")
    check.set_defaults(func=cmd_config_check)

    return parser


def _configure_console() -> None:
    """Use UTF-8 on the Windows console so Russian tool output is readable."""
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:  # noqa: BLE001 - best effort
            pass
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _configure_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        console.print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
