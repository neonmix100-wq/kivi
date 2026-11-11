# Operator UI automation (`ui.*`)

Version: 0.1 (11.11.2026). Stage: M3. Related: `docs/TOOLS.md`,
`docs/SECURITY.md`, `docs/CODER-TOOLS.md`.

Kivi can operate the local Windows desktop through a small, confirmed tool set:
move the pointer, type text, press a key chord and look up visible window text.
This is deliberately **not** a general RPA engine. There is no OCR, no
computer vision, no element-based click by name and no password-manager
integration. Every input action is a discrete R2 call that the operator
approves, and `ui.find` is read-only.

## 1. Architecture

`src/kivi/tools/ui.py` defines the contract and two lazy native backends:

```python
class UiBackend(Protocol):
    def click(x, y, button="left", double=False) -> None: ...
    def type_text(text, interval_s=0.0) -> None: ...
    def hotkey(keys) -> None: ...
    def position() -> tuple[int, int]: ...
    def screen_size() -> tuple[int, int]: ...
    def find(text, window_title=None) -> list[UiElement]: ...
```

| Backend | Role | Import |
|---|---|---|
| `PyAutoGuiBackend` | Mouse and keyboard input | `pyautogui`, on the first action |
| `PywinautoBackend` | Read-only UIA text lookup | `pywinauto`, only inside `find` |
| `UiController` | Composite default: pyautogui for input, pywinauto for find | Lazy through the two backends |

`PywinautoBackend` implements the input methods only to fail with a clear
`UiUnavailable` error; `PyAutoGuiBackend.find` does the same. This keeps one
honest protocol while making the split explicit. `UiController(backend=...)`
injects a complete fake for tests; `finder=...` overrides only the lookup side.

`screen_rect()` returns a `ScreenRect` for the full virtual desktop, including
monitors left of or above the primary one. On Windows it uses
`GetSystemMetrics(SM_*VIRTUALSCREEN)`; for an injected backend without an
explicit rectangle it falls back to `(0, 0, backend.screen_size())`.

## 2. Tools

| Tool | Risk | Capability | Purpose |
|---|---|---|---|
| `ui.click` | R2 | `ui.control` | Click an absolute virtual-screen coordinate. |
| `ui.type` | R2 | `ui.control` | Type text into the focused window. |
| `ui.hotkey` | R2 | `ui.control` | Press a whitelisted key chord. |
| `ui.find` | R1 | `ui.read` | Find visible text in windows; never clicks. |

R2 calls need `--approve` (or the equivalent approval from the planner). In the
default `A4-Bounded` autonomy mode R1 runs automatically and R2 asks for
confirmation. Lower modes tighten the table further; the tools do not have a
bypass. `dry_run` is evaluated by the executor as a read-only preview and never
calls the input backend.

### `ui.click`

```json
{"x": 640, "y": 420, "button": "left", "double": false}
```

- `x`, `y` are absolute physical screen coordinates (integers);
- `button` is `left`, `right` or `middle`;
- `double=true` sends two clicks with a short interval;
- the point must be inside the virtual screen, including negative coordinates
  on a multi-monitor desktop with a monitor left of the primary;
- `dry_run` returns the coordinates, button, double flag and screen rectangle
  without moving the pointer.

### `ui.type`

```json
{"text": "hello", "interval_s": 0.0}
```

- `text` is 1 to 2000 characters;
- `interval_s` is 0.0 to 1.0 seconds between characters (default 0.0);
- the result says only `typed N characters`; the text is not echoed in the
  output, preview or audit;
- text matching the sanitizer's secret patterns is refused before the backend
  is called. This uses `kivi.safety.sanitize.redact_secrets`; the result meta
  carries `redact_secrets` (the number of patterns found, normally 0);
- there is no password-manager auto-type.

### `ui.hotkey`

```json
{"keys": ["ctrl", "s"]}
```

- `keys` contains 2 to 4 unique keys;
- allowed keys: `ctrl`, `alt`, `shift`, `win`, `tab`, `esc`, `enter`, `space`,
  `backspace`, `delete`, `up`, `down`, `left`, `right`, `home`, `end`,
  `pageup`, `pagedown`, `f1`–`f12`, `a`–`z`, `0`–`9`;
- keys are normalized to lower case; arbitrary strings are rejected;
- `ctrl+alt+delete` is rejected explicitly (the Windows secure attention
  sequence cannot be injected through this path);
- `win+l` is allowed by request and locks the workstation like a normal
  operator key press; `win+r`, `alt+f4` and similar chords are allowed because
  the operator explicitly confirms each R2 call.

### `ui.find`

```json
{"text": "Save", "window_title": "Notepad", "max_results": 20}
```

- match is a case-insensitive substring over UIA control text;
- `window_title` optionally filters windows by a case-insensitive title
  substring;
- `max_results` is 1 to 20 (default 20); the backend also caps the scan at
  2000 controls so a huge desktop cannot stall the call;
- the result is a JSON list of objects with `title`, `control_type` and
  `rect` (`[left, top, right, bottom]`);
- window titles are sanitized and secret-looking tokens are redacted;
- result trust is `untrusted:ui`: window text can inform the model but is never
  an instruction;
- the tool never clicks, focuses or activates anything.

The native implementation enumerates
`Desktop(backend="uia").windows()` and then `window.descendants()`. Controls
that disappear while the tree is walked are skipped; a failed window or read
does not abort the whole search.

## 3. Safety model

| Control | Effect |
|---|---|
| Capability Broker | `ui.control` / `ui.read` must be in the task grant; tools cannot mint capabilities. |
| Risk levels | Input is R2 and needs confirmation; lookup is R1. |
| Kill switch | `ToolExecutor` refuses every `ui.*` call while the kill switch is on. |
| Coordinates | Every click is checked against the virtual screen before the backend is called. |
| Secrets | `ui.type` rejects token-looking secrets and never echoes typed text. |
| Audit | One audit row per attempt with `args_digest`; raw typed text is not stored. |
| Failsafe | pyautogui `FAILSAFE` is enabled; see section 5. |
| Test isolation | Unit tests inject a fake backend; the suite never clicks or types for real. |

The executor's audit record contains the tool name, risk, capability, task id,
`args_digest` and the dry-run flag. It does not contain the raw text from
`ui.type`, and a dry-run preview contains only character counts and the interval.

## 4. Coordinate and secret policy

### Coordinates

- Coordinates are absolute pixels in the virtual desktop, not window-relative
  values and not screenshot-relative values.
- The supported rectangle is half-open: `left <= x < right` and
  `top <= y < bottom`. A point on the right or bottom edge is rejected.
- Negative coordinates are valid on multi-monitor layouts; an injected backend
  without explicit geometry is assumed to start at `(0, 0)`.
- A click lands in whichever window currently has focus. Verify the target with
  `screen.capture` and a `ui.click` dry run before approving.
- Resolution changes, DPI scaling and display hot-plugging can invalidate
  coordinates between the preview and the action; repeat the dry run after a
  layout change.

### Secrets

`ui.type` is not a password channel.

- The tool runs `redact_secrets` first and refuses any text that matches a
  known token pattern (`sk-…`, `ghp_…`, `github_pat_…`, `AKIA…`, a `Bearer`
  token or a PEM private key). Refusal happens before the backend call and the
  text never appears in the preview, result or audit.
- The sanitizer patterns are a safety net, not a classifier. A password,
  passphrase, PIN, recovery code or personal data that does not match a pattern
  will not be detected. Do not ask Kivi to type secrets.
- Password fields cannot be detected through this interface. UIA may report a
  password edit control, but the tool does not inspect focus or control type
  before typing, and pyautogui only knows the focused window. Treat every
  `ui.type` target as untrusted.
- No clipboard fallback, no password-manager integration, no automatic retry
  with a different input method.

## 5. Failsafe

`PyAutoGuiBackend` configures the library on first use:

```python
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05
```

- Moving the pointer to the upper-left corner `(0, 0)` triggers
  `FailSafeException`, which the backend converts to `UiUnavailable`; the
  executor records an error and no further clicks or key presses are sent.
- `PAUSE = 0.05` adds a short delay after every pyautogui call, so a stuck or
  misdirected action is easier to interrupt.
- The kill switch is checked once per tool call, before any UI backend is
  reached. It is not a hard real-time abort inside a pyautogui call.
- The fail-safe corner is a last resort, not a sandbox. It does not undo an
  action that already happened.

## 6. Risks

- **Focus theft / wrong target.** A click can land on another window if focus
  changes between approval and execution. R2 confirmation is mandatory.
- **Destructive hotkeys.** `alt+f4`, `win+r`, `ctrl+w` and similar chords act on
  the focused application. The whitelist limits syntax, not consequences.
- **Typing into the wrong field.** Text goes to whatever control has focus;
  the tool cannot verify the target's label or type.
- **Secret leakage.** Token patterns are caught, but passwords and personal
  data are not. Do not type them.
- **UI text is untrusted.** `ui.find` output can contain adversarial strings;
  it is labeled `untrusted:ui` and must not be treated as instructions.
- **Protected windows are unknowable.** The tool has no denylist for password
  managers, banking apps, UAC prompts or secure desktops. It cannot tell a
  harmless dialog from a privileged one.
- **Audit is metadata, not a recording.** The audit hashes arguments and does
  not keep raw text or screen content.
- **Session dependence.** pyautogui requires an interactive desktop; a locked,
  disconnected or secure-desktop session is not automatable.

## 7. Limitations

- no OCR or computer vision; `ui.find` sees only the UIA tree;
- no clicking by element name, label or accessibility id — `ui.click` is
  coordinate-only;
- no drag, drop, scroll, hover or `ui.move` (intentionally skipped in this
  slice);
- no screenshot verification of the result; a successful call means "input was
  sent", not "the application did the expected thing";
- UIA cannot inspect elevated (UAC) windows from a non-elevated process, and
  some custom-drawn or game surfaces expose no useful text;
- non-ASCII text and characters outside the active keyboard layout may fail or
  produce different characters in `ui.type`;
- window titles are sanitized but control rectangles are reported as-is; DPI
  and per-monitor scaling can make them differ from screenshot pixels;
- `ui.find` is read-only by design; it never focuses or highlights a match;
- no password-field protection, no protected-window denylist and no
  application allowlist.

## 8. Integration

The registry and CLI are intentionally **not** changed in this slice. To enable
the tools, register them where the default registry is assembled:

```python
from kivi.tools.ui import build_ui_tools

for tool in build_ui_tools():
    registry.register(tool)
```

`build_ui_tools(controller=None)` returns the four tools sharing one
`UiController`. The controller creates its native backends lazily, so
registration does not import pyautogui or pywinauto.

After registration the usual executor path applies:

```powershell
kivi tools run ui.click --args '{"x":640,"y":420}' --dry-run
kivi tools run ui.click --args '{"x":640,"y":420}' --approve
kivi tools run ui.type --args '{"text":"hello"}' --dry-run
kivi tools run ui.type --args '{"text":"hello"}' --approve
kivi tools run ui.hotkey --args '{"keys":["ctrl","s"]}' --approve
kivi tools run ui.find --args '{"text":"Save","window_title":"Notepad"}'
```

Until then, direct `kivi tools run ui.*` calls return `unknown tool`; import
`build_ui_tools` from Python or wire it into the registry in the integration
step. The task grant must include `ui.control` for input and `ui.read` for
lookup.

## 9. Tests

`tests/unit/test_ui.py` uses `FakeBackend` and `UiController(..., screen_rect=...)`;
no test moves the real pointer, sends a real key or opens a real window.

Coverage:

- tool contracts: names, risks, capabilities, `extra="forbid"`;
- click: confirmation, approved left click, right/double, dry run, virtual
  screen bounds, negative multi-monitor origin, invalid button;
- type: confirmation, approved typing, no echoed text, secret rejection, length
  and interval validation, dry run, audit without raw text;
- hotkey: confirmation, normalization, `win+l`, whitelist rejection, 2–4 key
  count, explicit `ctrl+alt+delete` rejection, dry run;
- find: sanitized matches, title filter, 20-result cap and truncation, dry run,
  blank query rejection;
- backends: fail-safe constants, pyautogui cannot find, pywinauto is find-only,
  virtual-screen refusal on non-Windows;
- composite controller: separate input and finder backends.
