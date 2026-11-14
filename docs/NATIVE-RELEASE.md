# Kivi Desktop — native client release snapshot

Date: 14.11.2026

## Artifacts

```text
desktop\Kivi.Desktop\publish\Kivi.exe      self-contained native client
dist\Kivi-Setup.exe                         per-user installer (~1.82 GB)
```

Installer layout:

```text
Kivi.exe
runtime\pythonw.exe
runtime\Lib\site-packages
src\kivi
config
web
assets
```

## Verified

```text
Kivi.exe cold start
→ runtime\pythonw.exe -m kivi ui --no-browser --port 8765
→ GET /health: {"status":"ok","version":"0.20.0"}
```

Tested from an installed directory with no repository `.venv`:

```text
dist\KiviTest4
```

Python checks:

```text
1700 tests
ruff clean
acceptance PASS
```

Native checks:

```text
dotnet build -c Release
scripts\build_desktop.ps1
Inno Setup compile
```

## Functional scope

Implemented in the native client:

- chat, history, new chat, per-answer speech;
- voice input, auto TTS, wake;
- voices list/select;
- settings provider/model/speaker/sample_rate;
- media image/video, gallery;
- files tabs, search, sort, thumbnails, preview, multi-select, copy/move/trash/undo, drag-and-drop;
- CRM status/read/fill/act/task.

## Known deviations

1. **WPF/XAML instead of WinUI 3.** WinUI 3 unpacked builds fail without
   Visual Studio/MSIX PRI build tools (`MSB4062`, `ExpandPriContent`). WPF is a
   native XAML Windows client and builds with the standalone .NET SDK.
2. Not pixel-identical to the HTML UI; visual direction, navigation and dark
   theme are preserved.
3. Installer is not code-signed and uses no LZMA compression to keep build
   iteration fast.
4. Heavy models (Qwen TTS, SDXL, Ollama models) are not bundled.

## To finish WinUI literally

Install Visual Studio Build Tools with Windows App SDK/MSIX components, port
the WPF XAML/UI layer to WinUI 3 controls, then rebuild and re-run the installer
and parity checks. The local API/data layer does not need changes.
