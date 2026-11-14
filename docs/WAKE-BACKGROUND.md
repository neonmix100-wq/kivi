# Background wake without a browser

The UI wake button requires the Kivi tab. For microphone wake while the browser
is closed, Kivi has a separate `listen` process:

```powershell
.\KiviWake.bat
```

It starts `pythonw -m kivi listen --mic --speak` with the local
`faster-whisper-large-v3-turbo`, waits for the personal ONNX wake word, answers
aloud and keeps listening for another turn. No console window is created.

Stop it with:

```powershell
.\KiviWakeStop.bat
```

Install it as a per-user logon task (no admin required):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_wake_task.ps1
```

Remove the task:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_wake_task.ps1 -Uninstall
```

Do not run this listener and the UI microphone/wake button at the same time:
both want the same microphone.
