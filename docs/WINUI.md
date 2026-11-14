# Kivi WinUI 3

Native WinUI 3 client. This is the primary desktop target.

## Toolchain

- Visual Studio 2022 Build Tools 17.14
- Windows 11 SDK 10.0.26100
- MSIX/Windows App SDK build tools
- .NET 8 SDK at `D:\dotnet`

Build environment:

```powershell
$env:DOTNET_ROOT="D:\dotnet"
$env:MSBuildSDKsPath="D:\dotnet\sdk\8.0.428\Sdks"
$env:PATH="D:\dotnet;$env:PATH"
```

Build:

```powershell
& "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\MSBuild\Current\Bin\MSBuild.exe" `
  desktop\Kivi.WinUI\Kivi.WinUI.csproj /restore /p:Configuration=Debug /p:Platform=x64
```

Package:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\package_winui.ps1
# dist\Kivi-WinUI-Setup.exe
```

## Current WinUI scope

- native sidebar, header, dark theme;
- chat, history, new chat, per-answer speech;
- voice input via NAudio → `/transcribe`, auto TTS via `/tts`;
- voices list/select;
- settings provider/model/speaker/sample_rate;
- media image generation;
- files: tabs, navigation, search, sorting, preview, mkdir/rename/trash/undo, multi-select, thumbnails, copy/move, context menu;
- CRM status/read/fill/task;
- wake loop through `/wake`;
- automatic Python backend start from portable runtime.

## Remaining

- file tabs and drag-and-drop in WinUI;
- media gallery/video (now added);
- final visual parity;
- signed installer.

The older WPF client remains in `desktop/Kivi.Desktop` as a fallback.
