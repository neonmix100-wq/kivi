# Native client parity audit

Snapshot: native WPF/XAML client, installer built and cold-start verified.

| Feature | Web UI | Native | Notes |
|---|---|---|---|
| chat request/answer | yes | yes | `/chat`, per-answer speak, timestamps, new chat, Ctrl+N |
| history | yes | yes | `/history` |
| auto TTS | yes | yes | `/tts`, SoundPlayer |
| voice input | yes | yes | NAudio → `/transcribe` |
| wake | yes | yes | NAudio → `/wake` |
| voices list/select | yes | yes | `/voices`, `/voices/active` |
| settings | full | provider/model/speaker/sample_rate editable | fish_model read-only |
| media image | yes | yes | `/media/image` |
| media video | local fallback | playable MP4 | `/media/video` |
| media gallery | yes | yes | last 12 results in session |
| files list | yes | yes | `/fs/list`, native sorting |
| files search | yes | yes | recursive `/fs/search` |
| thumbnails | yes | yes | `/fs/thumb` |
| preview text/image | yes | yes | `/fs/read`, `/fs/raw` |
| copy/move/rename/trash | yes | yes | context menu/prompts |
| undo | yes | yes | `/fs/undo` |
| history tabs | yes | yes | native file tabs |
| drag-and-drop | yes | basic | drop files to copy into current folder |
| multi-select/keyboard | yes | Ctrl+click, Enter/Delete/F2/arrows |
| CRM read | yes | yes | `/crm/read` |
| CRM fill/act/task | yes | yes | native forms |
| installer | no | yes | Inno Setup, portable runtime |

## Verified cold start

With no Kivi backend running:

```text
Kivi.exe (installed) → runtime\pythonw.exe -m kivi ui --no-browser --port 8765
GET /health → {"status":"ok","version":"0.20.0"}
```

## Verified clean install

`dist\KiviTest3` installed from `dist\Kivi-Setup.exe`; cold start launched
`runtime\pythonw.exe -m kivi ui --no-browser --port 8765`, health returned ok.

## Main remaining gaps

1. Full file-manager parity: tabs, multi-select, drag-and-drop, sort.
2. Complete settings fields.
3. Media gallery persistence.
4. UI parity polish (native controls cannot be pixel-identical to HTML).
5. Installer signing/compression.

The native client is a functional desktop app, but not yet a complete parity
replacement for every web UI edge case.
