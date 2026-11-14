# Kivi Desktop: native Windows rewrite

## Decision

The native client is **WPF/XAML**, not WinUI 3. WinUI 3 unpacked builds require
Visual Studio/MSIX PRI tasks; without Visual Studio the current `.NET 8 SDK`
fails with `MSB4062`. WPF is a native Windows XAML framework, builds with the
standalone SDK and gives the same dark visual language. This is a deliberate
deviation from "WinUI 3" requested after the toolchain check.

If WinUI is mandatory, install Visual Studio Build Tools + Windows App SDK
workload and port only the XAML shell; the API and data layers are unchanged.

## Architecture

```text
Kivi.exe (WPF/XAML)
  ├─ native sidebar/header/chat and pages
  ├─ HttpClient → http://127.0.0.1:8765..8767
  └─ starts pythonw -m kivi ui --no-browser when API is down
        └─ existing Kivi Python API, memory, tools, models
```

The web UI stays as a fallback until feature parity is reached. Both clients
share the same SQLite data, config and loopback API.

## Phases

1. **Shell** — window, sidebar, chat send/answer, backend autostart. Done in
   `0.21-native-alpha`.
2. **Chat** — history, retry, auto-speak, voice input, wake button.
3. **Pages** — voices, media, files/shell, CRM, settings.
4. **Packaging** — Python sidecar/installer, icon, first-run, update check.
5. **Parity audit** — every current UI action covered by a native control and
   tested.

## Build

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_desktop.ps1
# desktop\Kivi.Desktop\publish\Kivi.exe
```

The published exe is self-contained; `.NET Desktop Runtime` is not required.
