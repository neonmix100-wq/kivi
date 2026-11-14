# Kivi Desktop (native WPF/XAML)

This is the native Windows client. It does **not** open a browser and does not
embed a web page: the shell, navigation and chat are WPF/XAML controls.

## Current build

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_desktop.ps1
```

Output:

```text
desktop\Kivi.Desktop\publish\Kivi.exe
```

The executable is self-contained (the .NET runtime is bundled) and can be
started without the `dotnet` command.

The client connects to the existing local Kivi API on ports `8765`, `8766` or
`8767`. If none is running and the repository is present, it starts
`pythonw -m kivi ui --no-browser --port 8765` automatically and stops it when
the native window closes.

## Porting status

| Area | Status |
|---|---|
| native window, dark theme, sidebar | done |
| chat request/response | done |
| automatic local backend start | done |
| chat history | done |
| voices list/select | done |
| settings provider | done |
| media image generation | basic |
| media video playback | basic |
| files list/search/thumbnails/ops | basic |
| CRM status/read/fill/act/task | forms |
| full files/shell operations | next |
| media gallery/history | next |
| installer / Python sidecar | installer scaffold built; runtime bundling next |

The existing web UI remains available during the migration; both clients use
the same API and data.

## Why WPF, not WinUI 3

WinUI 3 unpacks require Visual Studio/MSIX PRI build tasks. This machine has no
Visual Studio and `dotnet build` fails with `MSPRI`/`ExpandPriContent`. WPF is a
native Windows XAML framework, builds with the standalone .NET SDK, and keeps the
same visual direction. If a WinUI build is required later, install Visual Studio
Build Tools with the Windows App SDK workload and port the XAML layer.
