"""Playwright browser automation with a managed profile and untrusted output.

The controller is a thin, auditable wrapper around the Playwright sync API. It
does not expose JavaScript evaluation: navigation, visible text, clicks, form
fills, key presses and downloads are the whole surface. Every URL passes a
scheme, port and credential policy plus an SSRF check that resolves the host and
requires every answer to be globally routable before the page is touched.

Page text is data, never an instruction. Tools return ``untrusted:web`` results
after the shared sanitizer strips control characters and secret-looking tokens;
a page cannot ask Kivi to run another tool. Only a planner acting on a trusted
user request can decide that a *separate*, explicitly authorised tool call is
needed, and that call goes through the regular Capability Broker.

The controller is lazily started, owns exactly one persistent context, and is
closed idempotently. A session object shares that one controller between the
``browser.*`` tools.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
import socket
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kivi.safety.sanitize import redact_secrets, sanitize_text
from kivi.tools.base import Preview, Tool, ToolContext, ToolError, ToolResult

MAX_URL_CHARS = 2048
MAX_SELECTOR_CHARS = 200
MAX_READ_CHARS = 100_000
MAX_PAGE_TEXT_CHARS = 200_000
MAX_FILL_CHARS = 4096
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
MAX_FILENAME_CHARS = 120

DEFAULT_PROFILE_DIR = Path("data") / "browser-profile"
DEFAULT_DOWNLOADS_DIR = Path("data") / "downloads"

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS: tuple[int, ...] = (80, 443)
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")

_FORBIDDEN_SELECTOR_MARKERS = ("javascript:", "data:", "file:")
_SCRIPT_ELEMENT = re.compile(r"(?:^|[\s,>+~(])script(?:$|[\s,.#:[>+~)\]])", re.IGNORECASE)
_PASSWORD_SELECTOR = re.compile(r"type\s*=\s*[\"']?password", re.IGNORECASE)
_SECRETISH = re.compile(
    r"\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|bearer)\b"
    r"\s*[:=]\s*\S{4,}",
    re.IGNORECASE,
)
_FILENAME_BAD = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_MODIFIERS = frozenset({"Control", "Alt", "Shift", "Meta"})
_NAMED_KEYS = frozenset(
    {
        "Enter",
        "Escape",
        "Tab",
        "Backspace",
        "Delete",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Insert",
        "Space",
    }
)

Resolver = Callable[..., list[tuple[Any, ...]]]
Launcher = Callable[..., Any]


class BrowserError(ToolError):
    """Base class for expected browser failures; the executor turns it into a value."""


class BrowserUnavailable(BrowserError):
    """Playwright or a usable Chrome installation is missing."""


class BrowserPolicyError(BrowserError):
    """A URL, selector or action failed the browser security policy."""


def _default_resolver(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
    return socket.getaddrinfo(host, port, **kwargs)


def _is_local_host(host: str) -> bool:
    if host == "localhost":
        return True
    return any(host.endswith(suffix) for suffix in _LOCAL_SUFFIXES)


def _is_global_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject non-public addresses, including IPv4-mapped IPv6 forms."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global


@dataclass(frozen=True, slots=True)
class ParsedUrl:
    """A URL that passed the shape and host policy."""

    url: str
    scheme: str
    host: str
    port: int | None

    @property
    def effective_port(self) -> int:
        return self.port or (443 if self.scheme == "https" else 80)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
        }


def parse_url(
    url: str,
    *,
    allowed_hosts: Iterable[str] | None = None,
    allowed_ports: Iterable[int] = _DEFAULT_PORTS,
) -> ParsedUrl:
    """Validate URL shape and host policy without touching the network."""
    if not isinstance(url, str):
        raise BrowserPolicyError("URL must be a string")
    if not url or url != url.strip():
        raise BrowserPolicyError("URL must not be empty or padded with whitespace")
    if len(url) > MAX_URL_CHARS:
        raise BrowserPolicyError(f"URL is longer than {MAX_URL_CHARS} characters")
    if any(ord(char) < 32 or ord(char) == 127 or char == " " for char in url):
        raise BrowserPolicyError("URL contains control characters or spaces")
    if "\\" in url:
        raise BrowserPolicyError("backslashes are not allowed in URLs")

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise BrowserPolicyError(f"invalid URL: {exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise BrowserPolicyError("only http and https URLs are allowed")
    if parts.username or parts.password:
        raise BrowserPolicyError("credentials in the URL are not allowed")

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise BrowserPolicyError("URL has no host")
    if _is_local_host(host):
        raise BrowserPolicyError(f"local hosts are not allowed: {host}")

    ports = {int(item) for item in allowed_ports}
    if port is not None and port not in ports:
        allowed = ", ".join(str(item) for item in sorted(ports))
        raise BrowserPolicyError(f"port {port} is not allowed (allowed: {allowed})")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not _is_global_address(address):
        raise BrowserPolicyError(f"{host} is a non-public address")

    if allowed_hosts:
        patterns = tuple(
            item.strip().lower() for item in allowed_hosts if item and item.strip()
        )
        if patterns and not any(fnmatch.fnmatchcase(host, pattern) for pattern in patterns):
            raise BrowserPolicyError(f"host is not in the allowed list: {host}")

    return ParsedUrl(url=url, scheme=scheme, host=host, port=port)


def resolve_public(host: str, port: int, *, resolver: Resolver | None = None) -> str:
    """Resolve ``host`` and require every answer to be globally routable."""
    lookup = resolver or _default_resolver
    try:
        answers = lookup(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise BrowserPolicyError(f"cannot resolve {host}: {exc}") from exc
    if not answers:
        raise BrowserPolicyError(f"cannot resolve {host}")

    chosen: str | None = None
    for answer in answers:
        try:
            address = answer[4][0]
        except (IndexError, TypeError) as exc:
            raise BrowserPolicyError(f"invalid DNS answer for {host}") from exc
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise BrowserPolicyError(f"invalid resolved address: {address}") from exc
        if not _is_global_address(parsed):
            raise BrowserPolicyError(f"{host} resolves to a non-public address")
        if chosen is None:
            chosen = address
    if chosen is None:
        raise BrowserPolicyError(f"cannot resolve {host}")
    return chosen


@dataclass(frozen=True, slots=True)
class UrlPolicy:
    """Scheme, port, allow-list and SSRF rules for browser navigation."""

    allowed_hosts: frozenset[str] = frozenset()
    allowed_ports: tuple[int, ...] = _DEFAULT_PORTS
    resolver: Resolver = _default_resolver

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "allowed_hosts",
            frozenset(item.strip().lower() for item in self.allowed_hosts if item.strip()),
        )
        ports = tuple(int(item) for item in self.allowed_ports)
        if not ports or any(not 1 <= item <= 65535 for item in ports):
            raise BrowserPolicyError("allowed_ports must contain ports 1..65535")
        object.__setattr__(self, "allowed_ports", ports)

    def validate(self, url: str) -> ParsedUrl:
        return parse_url(
            url,
            allowed_hosts=self.allowed_hosts,
            allowed_ports=self.allowed_ports,
        )

    def resolve(self, parsed: ParsedUrl) -> str:
        return resolve_public(parsed.host, parsed.effective_port, resolver=self.resolver)

    def check(self, url: str) -> ParsedUrl:
        """Validate shape and resolve; raise before the browser sees the URL."""
        parsed = self.validate(url)
        self.resolve(parsed)
        return parsed


def validate_selector(selector: str) -> str:
    """Return a trimmed CSS selector or raise; no XPath and no JavaScript."""
    if not isinstance(selector, str):
        raise BrowserPolicyError("selector must be a string")
    value = selector.strip()
    if not value:
        raise BrowserPolicyError("selector must not be empty")
    if len(value) > MAX_SELECTOR_CHARS:
        raise BrowserPolicyError(
            f"selector is longer than {MAX_SELECTOR_CHARS} characters"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise BrowserPolicyError("selector contains control characters")
    lowered = value.lower()
    if "xpath=" in lowered:
        raise BrowserPolicyError("only CSS selectors are allowed; XPath is not supported")
    if "internal:" in lowered:
        raise BrowserPolicyError("Playwright internal selector engines are not allowed")
    if any(marker in lowered for marker in _FORBIDDEN_SELECTOR_MARKERS):
        raise BrowserPolicyError("selector contains a forbidden javascript/data/file marker")
    if _SCRIPT_ELEMENT.search(value):
        raise BrowserPolicyError("selecting script elements is not allowed")
    return value


def validate_fill_selector(selector: str) -> str:
    """Best-effort rejection of obvious password fields before typing."""
    value = validate_selector(selector)
    if _PASSWORD_SELECTOR.search(value):
        raise BrowserPolicyError("password fields must not be filled by Kivi")
    return value


def contains_secret_like(text: str) -> bool:
    """True when ``text`` looks like a token, key or credential assignment."""
    _, count = redact_secrets(text)
    return count > 0 or bool(_SECRETISH.search(text))


def validate_key(key: str) -> str:
    """Allow single characters, named keys and modifier combinations only."""
    if not isinstance(key, str) or not key or len(key) > 40:
        raise BrowserPolicyError("key must be a short non-empty string")
    parts = key.split("+")
    if len(parts) > 4:
        raise BrowserPolicyError("too many key modifiers")
    for part in parts[:-1]:
        if part not in _MODIFIERS:
            raise BrowserPolicyError(f"unsupported key modifier: {part!r}")
    last = parts[-1]
    if last in _NAMED_KEYS or (len(last) == 1 and last.isalnum()):
        return key
    if re.fullmatch(r"F(?:[1-9]|1[0-2])", last):
        return key
    raise BrowserPolicyError(f"unsupported key: {key!r}")


def safe_filename(name: str) -> str:
    """Reduce a page-supplied filename to a single safe Windows file name."""
    raw = str(name or "").replace("\\", "/")
    base = _FILENAME_BAD.sub("_", Path(raw).name).strip(" .")
    if not base or base in {".", ".."}:
        return f"download-{uuid4().hex[:8]}"
    stem = Path(base).stem
    suffix = Path(base).suffix[:20]
    if stem.upper() in _RESERVED_NAMES:
        stem = f"download-{stem}"
    if len(stem) > 100:
        stem = stem[:100]
    return f"{stem}{suffix}"[:MAX_FILENAME_CHARS]


@dataclass(frozen=True, slots=True)
class PageInfo:
    """Title, final URL and HTTP status of a navigation."""

    title: str
    url: str
    status: int | None

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "url": self.url, "status": self.status}


@dataclass(frozen=True, slots=True)
class DownloadInfo:
    """A file saved under ``data/downloads``; the name is untrusted page data."""

    path: Path
    size: int
    filename: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "bytes": self.size, "filename": self.filename}


class BrowserController:
    """One lazy, persistent Chrome context with a narrow, explicit surface."""

    def __init__(
        self,
        *,
        workspace: Path,
        profile_dir: Path | None = None,
        downloads_dir: Path | None = None,
        headless: bool = True,
        channel: str | None = "chrome",
        executable_path: str | None = None,
        allowed_hosts: Iterable[str] | None = None,
        allowed_ports: Iterable[int] = _DEFAULT_PORTS,
        policy: UrlPolicy | None = None,
        resolver: Resolver | None = None,
        launcher: Launcher | None = None,
        navigation_timeout_ms: int = 30_000,
        action_timeout_ms: int = 10_000,
        download_timeout_ms: int = 30_000,
    ) -> None:
        self.workspace = Path(workspace)
        self.profile_dir = self._inside_workspace(profile_dir, DEFAULT_PROFILE_DIR)
        self.downloads_dir = self._inside_workspace(downloads_dir, DEFAULT_DOWNLOADS_DIR)
        self.headless = bool(headless)
        self.channel = channel
        self.executable_path = executable_path
        self.navigation_timeout_ms = self._timeout(navigation_timeout_ms, "navigation")
        self.action_timeout_ms = self._timeout(action_timeout_ms, "action")
        self.download_timeout_ms = self._timeout(download_timeout_ms, "download")
        self._policy = policy or UrlPolicy(
            allowed_hosts=frozenset(
                item.strip().lower() for item in (allowed_hosts or ()) if item.strip()
            ),
            allowed_ports=tuple(int(item) for item in allowed_ports),
            resolver=resolver or _default_resolver,
        )
        self._launcher = launcher
        self._context: Any = None
        self._page: Any = None
        self._manager: Any = None
        self._closed = False

    # -- construction helpers -------------------------------------------
    def _inside_workspace(self, value: Path | None, default: Path) -> Path:
        base = Path(value) if value is not None else default
        return base if base.is_absolute() else self.workspace / base

    @staticmethod
    def _timeout(value: int, label: str) -> int:
        if not isinstance(value, int) or value < 1 or value > 300_000:
            raise BrowserError(f"{label} timeout must be an integer between 1 and 300000 ms")
        return value

    @property
    def policy(self) -> UrlPolicy:
        return self._policy

    @property
    def is_open(self) -> bool:
        return self._context is not None and not self._closed

    @property
    def current_url(self) -> str:
        if self._page is None:
            return ""
        return str(self._page.url or "")

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        """Start the persistent context without navigating anywhere."""
        self._ensure_started()

    def _ensure_started(self) -> None:
        if self._closed:
            raise BrowserError("browser session is closed")
        if self._context is not None:
            return
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        kwargs = self._launch_kwargs()
        try:
            if self._launcher is not None:
                context = self._launcher(**kwargs)
            else:
                context = self._launch_playwright(kwargs)
        except BrowserError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface a typed browser error
            raise BrowserUnavailable(f"cannot start Chrome: {exc}") from exc
        self._context = context
        pages = list(getattr(context, "pages", ()) or ())
        self._page = pages[0] if pages else context.new_page()

    def _launch_kwargs(self) -> dict[str, Any]:
        args = ["--disable-extensions", "--no-first-run"]
        kwargs: dict[str, Any] = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "accept_downloads": True,
            "downloads_path": str(self.downloads_dir),
            "args": args,
        }
        if self.executable_path:
            executable = Path(self.executable_path)
            if not executable.is_file():
                raise BrowserUnavailable(f"Chrome executable does not exist: {executable}")
            kwargs["executable_path"] = str(executable)
        elif self.channel:
            kwargs["channel"] = self.channel
        return kwargs

    def _launch_playwright(self, kwargs: dict[str, Any]) -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "playwright is not installed; install it and a Chrome build first"
            ) from exc
        try:
            manager = sync_playwright()
        except Exception as exc:  # noqa: BLE001 - startup failure is typed
            raise BrowserUnavailable(f"cannot start Playwright: {exc}") from exc
        try:
            playwright = manager.__enter__()
        except Exception as exc:  # noqa: BLE001 - startup failure is typed
            raise BrowserUnavailable(f"cannot start Playwright: {exc}") from exc
        self._manager = manager
        try:
            return playwright.chromium.launch_persistent_context(**kwargs)
        except Exception as exc:  # noqa: BLE001 - launch failure is typed
            with suppress(Exception):
                manager.__exit__(None, None, None)
            self._manager = None
            raise BrowserUnavailable(
                f"cannot launch Chrome (channel={self.channel!r}): {exc}"
            ) from exc

    def _require_page(self) -> Any:
        self._ensure_started()
        if self._page is None or self._page.is_closed():
            pages = list(getattr(self._context, "pages", ()) or ())
            self._page = pages[0] if pages else self._context.new_page()
        return self._page

    def close(self) -> None:
        """Close the context and Playwright manager; repeated calls are no-ops."""
        if self._closed:
            return
        self._closed = True
        page, self._page = self._page, None
        if page is not None:
            with suppress(Exception):
                if not page.is_closed():
                    page.close()
        context, self._context = self._context, None
        if context is not None:
            with suppress(Exception):
                context.close()
        manager, self._manager = self._manager, None
        if manager is not None:
            with suppress(Exception):
                manager.__exit__(None, None, None)

    def __enter__(self) -> BrowserController:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- actions ---------------------------------------------------------
    def open(self, url: str) -> PageInfo:
        """Navigate the managed tab after the URL and resolved IP pass policy."""
        parsed = self._policy.check(url)
        page = self._require_page()
        try:
            response = page.goto(
                parsed.url,
                wait_until="domcontentloaded",
                timeout=self.navigation_timeout_ms,
            )
            final_url = str(page.url or parsed.url)
            try:
                self._policy.check(final_url)
            except BrowserPolicyError:
                # Never leave the managed tab sitting on a blocked redirect.
                with suppress(Exception):
                    page.close()
                self._page = None
                raise
            title = str(page.title() or "")
        except BrowserError:
            raise
        except Exception as exc:  # noqa: BLE001 - navigation failure is typed
            raise BrowserError(f"cannot open {parsed.url}: {exc}") from exc
        status = getattr(response, "status", None) if response is not None else None
        return PageInfo(
            title=title,
            url=final_url,
            status=status if isinstance(status, int) else None,
        )

    def read_text(self, selector: str = "body", max_chars: int = 20_000) -> str:
        """Return visible text of the first match, capped at ``max_chars``."""
        value = validate_selector(selector)
        if not isinstance(max_chars, int) or max_chars < 1:
            raise BrowserError("max_chars must be a positive integer")
        limit = min(max_chars, MAX_PAGE_TEXT_CHARS)
        page = self._require_page()
        try:
            text = page.locator(value).inner_text(timeout=self.action_timeout_ms)
        except Exception as exc:  # noqa: BLE001 - locator failure is typed
            raise BrowserError(f"cannot read selector {value!r}: {exc}") from exc
        return str(text)[:limit]

    def click(self, selector: str) -> None:
        """Click the first match; the page decides what that triggers."""
        value = validate_selector(selector)
        locator = self._require_page().locator(value)
        try:
            locator.click(timeout=self.action_timeout_ms)
        except Exception as exc:  # noqa: BLE001 - click failure is typed
            raise BrowserError(f"cannot click selector {value!r}: {exc}") from exc

    def fill(self, selector: str, text: str) -> None:
        """Replace a form field value; secret-looking text is refused."""
        value = validate_fill_selector(selector)
        if not isinstance(text, str):
            raise BrowserError("fill text must be a string")
        if len(text) > MAX_FILL_CHARS:
            raise BrowserError(f"fill text is longer than {MAX_FILL_CHARS} characters")
        if contains_secret_like(text):
            raise BrowserError("refusing to fill secret-looking text into a page")
        locator = self._require_page().locator(value)
        try:
            locator.fill(text, timeout=self.action_timeout_ms)
        except Exception as exc:  # noqa: BLE001 - fill failure is typed
            raise BrowserError(f"cannot fill selector {value!r}: {exc}") from exc

    def press(self, selector: str, key: str) -> None:
        """Press a whitelisted key or modifier combination on a selector."""
        value = validate_selector(selector)
        pressed = validate_key(key)
        locator = self._require_page().locator(value)
        try:
            locator.press(pressed, timeout=self.action_timeout_ms)
        except Exception as exc:  # noqa: BLE001 - key failure is typed
            raise BrowserError(f"cannot press {pressed!r} on {value!r}: {exc}") from exc

    def download(self, selector: str) -> DownloadInfo:
        """Click and save the resulting download inside the managed directory."""
        value = validate_selector(selector)
        page = self._require_page()
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        locator = page.locator(value)
        try:
            with page.expect_download(timeout=self.download_timeout_ms) as pending:
                locator.click(timeout=self.action_timeout_ms)
        except Exception as exc:  # noqa: BLE001 - download failure is typed
            raise BrowserError(f"download did not start for {value!r}: {exc}") from exc
        download = pending.value
        if download is None:
            raise BrowserError("download did not start")
        suggested = str(getattr(download, "suggested_filename", "") or "download")
        target = self._download_target(suggested)
        try:
            download.save_as(str(target))
        except Exception as exc:  # noqa: BLE001 - save failure is typed
            raise BrowserError(f"cannot save download: {exc}") from exc
        if not target.exists():
            raise BrowserError("download was not saved")
        size = target.stat().st_size
        if size > MAX_DOWNLOAD_BYTES:
            with suppress(OSError):
                target.unlink()
            raise BrowserError(f"download is larger than {MAX_DOWNLOAD_BYTES} bytes")
        return DownloadInfo(path=target, size=size, filename=suggested)

    def _download_target(self, suggested: str) -> Path:
        name = safe_filename(suggested)
        stem = Path(name).stem
        suffix = Path(name).suffix
        candidate = self.downloads_dir / name
        index = 1
        while candidate.exists() and index < 1000:
            candidate = self.downloads_dir / f"{stem}-{index}{suffix}"
            index += 1
        if candidate.exists():
            candidate = self.downloads_dir / f"{stem}-{uuid4().hex[:8]}{suffix}"
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.downloads_dir.resolve()):
            raise BrowserError("download path escapes the downloads directory")
        return candidate


class BrowserOpenArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=8, max_length=MAX_URL_CHARS, description="HTTP(S) URL to open.")


class BrowserReadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selector: str = Field(
        default="body",
        min_length=1,
        max_length=MAX_SELECTOR_CHARS,
        description="CSS selector; only visible text is returned.",
    )
    max_chars: int = Field(default=20_000, ge=1, le=MAX_READ_CHARS)


class BrowserClickArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selector: str = Field(min_length=1, max_length=MAX_SELECTOR_CHARS)


class BrowserFillArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selector: str = Field(min_length=1, max_length=MAX_SELECTOR_CHARS)
    text: str = Field(max_length=MAX_FILL_CHARS)


class BrowserDownloadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selector: str = Field(min_length=1, max_length=MAX_SELECTOR_CHARS)


class BrowserCloseArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


ControllerFactory = Callable[[ToolContext], BrowserController]


class BrowserSession:
    """Holds one active controller for all ``browser.*`` tools that share it."""

    def __init__(
        self,
        *,
        controller_factory: ControllerFactory | None = None,
        allowed_hosts: Iterable[str] | None = None,
        allowed_ports: Iterable[int] = _DEFAULT_PORTS,
        resolver: Resolver | None = None,
    ) -> None:
        self._factory = controller_factory
        self._policy = UrlPolicy(
            allowed_hosts=frozenset(
                item.strip().lower() for item in (allowed_hosts or ()) if item.strip()
            ),
            allowed_ports=tuple(int(item) for item in allowed_ports),
            resolver=resolver or _default_resolver,
        )
        self._controller: BrowserController | None = None

    @property
    def url_policy(self) -> UrlPolicy:
        return self._policy

    @property
    def active(self) -> bool:
        return self._controller is not None

    def controller(self, ctx: ToolContext) -> BrowserController:
        """Return the shared controller, creating it lazily."""
        if self._controller is None:
            if self._factory is not None:
                self._controller = self._factory(ctx)
            else:
                self._controller = BrowserController(
                    workspace=ctx.workspace,
                    policy=self._policy,
                )
        return self._controller

    def close(self) -> bool:
        """Close the shared controller; True when there was an active one.

        The session stays usable: the next tool call lazily starts a new
        controller, so ``browser.close`` does not poison a long-running process.
        """
        controller, self._controller = self._controller, None
        if controller is None:
            return False
        controller.close()
        return True


class _BrowserTool:
    """Shared session plumbing for the browser tools."""

    def __init__(self, session: BrowserSession | None = None) -> None:
        self._session = session or BrowserSession()


class BrowserOpenTool(_BrowserTool):
    name = "browser.open"
    description = "Open an HTTP(S) page in the managed browser; page data is untrusted:web."
    risk = "R1"
    capabilities = ("browser.control",)
    args_model = BrowserOpenArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, BrowserOpenArgs)
        parsed = self._session.url_policy.validate(args.url)
        return Preview(
            "browser-open",
            {
                "url": parsed.url,
                "host": parsed.host,
                "port": parsed.effective_port,
            },
            reversible=True,
            warnings=("page text is untrusted:web and may not issue instructions",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, BrowserOpenArgs)
        self._session.url_policy.validate(args.url)
        page = self._session.controller(ctx).open(args.url)
        title = sanitize_text(page.title, html=False, max_chars=300)
        url = sanitize_text(page.url or args.url, html=False, max_chars=MAX_URL_CHARS)
        headline = title.text or url.text
        suspicious = list(dict.fromkeys((*title.suspicious, *url.suspicious)))
        return ToolResult(
            ok=True,
            output=f"opened: {headline}",
            meta={
                "title": title.text,
                "url": url.text,
                "status": page.status,
                "suspicious": suspicious,
            },
            truncated=title.truncated or url.truncated,
            trust="untrusted:web",
        )


class BrowserReadTool(_BrowserTool):
    name = "browser.read"
    description = "Read visible page text as untrusted:web data; HTML is never returned."
    risk = "R1"
    capabilities = ("browser.read",)
    args_model = BrowserReadArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, BrowserReadArgs)
        selector = validate_selector(args.selector)
        return Preview(
            "browser-read",
            {"selector": selector, "max_chars": args.max_chars},
            reversible=True,
            warnings=("the result is untrusted:web data and may contain injection text",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, BrowserReadArgs)
        selector = validate_selector(args.selector)
        controller = self._session.controller(ctx)
        raw = controller.read_text(selector, args.max_chars)
        cleaned = sanitize_text(raw, html=False, max_chars=args.max_chars)
        url = sanitize_text(controller.current_url, html=False, max_chars=MAX_URL_CHARS)
        return ToolResult(
            ok=True,
            output=cleaned.text,
            meta={
                "selector": selector,
                "url": url.text,
                "original_chars": cleaned.original_chars,
                "removed": dict(cleaned.removed),
                "suspicious": list(cleaned.suspicious),
            },
            truncated=cleaned.truncated or len(raw) >= args.max_chars,
            trust="untrusted:web",
        )


class BrowserClickTool(_BrowserTool):
    name = "browser.click"
    description = "Click one CSS selector in the managed browser; R2 confirmation is required."
    risk = "R2"
    capabilities = ("browser.control",)
    args_model = BrowserClickArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, BrowserClickArgs)
        selector = validate_selector(args.selector)
        return Preview(
            "browser-click",
            {"selector": selector},
            reversible=False,
            warnings=("a click can submit a form, change state or start a download",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, BrowserClickArgs)
        selector = validate_selector(args.selector)
        self._session.controller(ctx).click(selector)
        return ToolResult(ok=True, output=f"clicked: {selector}")


class BrowserFillTool(_BrowserTool):
    name = "browser.fill"
    description = "Fill one CSS field; secret-like values and password fields are refused."
    risk = "R2"
    capabilities = ("browser.control",)
    args_model = BrowserFillArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, BrowserFillArgs)
        selector = validate_fill_selector(args.selector)
        if contains_secret_like(args.text):
            raise BrowserError("refusing to fill secret-looking text into a page")
        # Do not echo the value into the audit preview: it can contain PII.
        return Preview(
            "browser-fill",
            {"selector": selector, "chars": len(args.text)},
            reversible=False,
            warnings=("the field value is sent to the page and may be stored remotely",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, BrowserFillArgs)
        selector = validate_fill_selector(args.selector)
        if len(args.text) > MAX_FILL_CHARS:
            raise BrowserError(f"fill text is longer than {MAX_FILL_CHARS} characters")
        if contains_secret_like(args.text):
            raise BrowserError("refusing to fill secret-looking text into a page")
        self._session.controller(ctx).fill(selector, args.text)
        return ToolResult(
            ok=True,
            output=f"filled: {selector} ({len(args.text)} chars)",
        )


class BrowserDownloadTool(_BrowserTool):
    name = "browser.download"
    description = "Download a file into data/downloads; the path is never model-controlled."
    risk = "R2"
    capabilities = ("browser.download",)
    args_model = BrowserDownloadArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, BrowserDownloadArgs)
        selector = validate_selector(args.selector)
        return Preview(
            "browser-download",
            {
                "selector": selector,
                "directory": str(ctx.workspace / DEFAULT_DOWNLOADS_DIR),
            },
            reversible=False,
            warnings=("downloaded files are untrusted and are never opened automatically",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, BrowserDownloadArgs)
        selector = validate_selector(args.selector)
        info = self._session.controller(ctx).download(selector)
        filename = sanitize_text(info.filename, html=False, max_chars=200).text
        return ToolResult(
            ok=True,
            output=f"downloaded: {filename} ({info.size} bytes)",
            artifacts=[str(info.path)],
            meta={"path": str(info.path), "bytes": info.size, "filename": filename},
            trust="untrusted:web",
        )


class BrowserCloseTool(_BrowserTool):
    name = "browser.close"
    description = "Close the managed browser session; safe to call more than once."
    risk = "R0"
    capabilities = ("browser.control",)
    args_model = BrowserCloseArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, BrowserCloseArgs)
        return Preview("browser-close", {"active": self._session.active}, reversible=True)

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, BrowserCloseArgs)
        closed = self._session.close()
        message = "browser closed" if closed else "no active browser session"
        return ToolResult(ok=True, output=message, meta={"closed": closed})


def build_browser_tools(
    *,
    controller_factory: ControllerFactory | None = None,
    allowed_hosts: Iterable[str] | None = None,
    allowed_ports: Iterable[int] = _DEFAULT_PORTS,
    resolver: Resolver | None = None,
) -> tuple[Tool, ...]:
    """Create the six browser tools sharing one :class:`BrowserSession`."""
    session = BrowserSession(
        controller_factory=controller_factory,
        allowed_hosts=allowed_hosts,
        allowed_ports=allowed_ports,
        resolver=resolver,
    )
    return (
        BrowserOpenTool(session),
        BrowserReadTool(session),
        BrowserClickTool(session),
        BrowserFillTool(session),
        BrowserDownloadTool(session),
        BrowserCloseTool(session),
    )


__all__ = [
    "DEFAULT_DOWNLOADS_DIR",
    "DEFAULT_PROFILE_DIR",
    "MAX_DOWNLOAD_BYTES",
    "MAX_FILL_CHARS",
    "MAX_PAGE_TEXT_CHARS",
    "MAX_READ_CHARS",
    "MAX_SELECTOR_CHARS",
    "MAX_URL_CHARS",
    "BrowserClickArgs",
    "BrowserClickTool",
    "BrowserCloseArgs",
    "BrowserCloseTool",
    "BrowserController",
    "BrowserDownloadArgs",
    "BrowserDownloadTool",
    "BrowserError",
    "BrowserFillArgs",
    "BrowserFillTool",
    "BrowserOpenArgs",
    "BrowserOpenTool",
    "BrowserPolicyError",
    "BrowserReadArgs",
    "BrowserReadTool",
    "BrowserSession",
    "BrowserUnavailable",
    "ControllerFactory",
    "DownloadInfo",
    "PageInfo",
    "ParsedUrl",
    "UrlPolicy",
    "build_browser_tools",
    "contains_secret_like",
    "parse_url",
    "resolve_public",
    "safe_filename",
    "validate_fill_selector",
    "validate_key",
    "validate_selector",
]
