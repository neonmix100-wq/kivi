"""Screen capture tool: save the desktop as a BMP inside the workspace.

The Windows backend uses GDI (no Pillow dependency). The tool always writes
inside the workspace, refuses to overwrite without an explicit flag, and labels
the result ``untrusted:vision``: OCR text from a screenshot is not an
instruction. The backend is injectable so unit tests never touch the screen.
"""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from kivi.safety.zones import ZoneError
from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult

MAX_IMAGE_BYTES = 64 * 1024 * 1024
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class ScreenUnavailable(RuntimeError):
    """Raised when the platform screen cannot be captured."""


@dataclass(frozen=True, slots=True)
class RawImage:
    """Top-down RGB pixels."""

    width: int
    height: int
    rgb: bytes

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("image dimensions must be positive")
        if len(self.rgb) != self.width * self.height * 3:
            raise ValueError("RGB buffer size does not match the dimensions")


class ScreenBackend(Protocol):
    def capture(self) -> RawImage: ...


class WindowsScreen:
    """Full virtual-desktop capture through GDI."""

    def capture(self) -> RawImage:
        if sys.platform != "win32":
            raise ScreenUnavailable("screen capture is Windows-only")

        import ctypes
        from ctypes import wintypes

        class BitmapInfoHeader(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        class BitmapInfo(ctypes.Structure):
            _fields_ = [
                ("bmiHeader", BitmapInfoHeader),
                ("bmiColors", wintypes.DWORD * 3),
            ]

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        user32.GetDC.argtypes = [wintypes.HWND]
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.BitBlt.argtypes = [
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        gdi32.BitBlt.restype = wintypes.BOOL
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.DeleteDC.restype = wintypes.BOOL
        gdi32.GetDIBits.argtypes = [
            wintypes.HDC,
            wintypes.HBITMAP,
            wintypes.UINT,
            wintypes.UINT,
            wintypes.LPVOID,
            ctypes.POINTER(BitmapInfo),
            wintypes.UINT,
        ]
        gdi32.GetDIBits.restype = ctypes.c_int

        left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if width <= 0 or height <= 0:
            raise ScreenUnavailable("the virtual desktop has no size")

        screen_dc = user32.GetDC(None)
        if not screen_dc:
            raise ScreenUnavailable("cannot get the screen device context")
        memory_dc = gdi32.CreateCompatibleDC(screen_dc)
        bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
        if not memory_dc or not bitmap:
            user32.ReleaseDC(None, screen_dc)
            raise ScreenUnavailable("cannot create GDI resources")
        previous = gdi32.SelectObject(memory_dc, bitmap)
        try:
            if not gdi32.BitBlt(
                memory_dc, 0, 0, width, height, screen_dc, left, top, SRCCOPY
            ):
                raise ScreenUnavailable("BitBlt failed")

            stride = ((width * 3 + 3) // 4) * 4
            header = BitmapInfoHeader()
            header.biSize = ctypes.sizeof(BitmapInfoHeader)
            header.biWidth = width
            header.biHeight = -height  # top-down rows
            header.biPlanes = 1
            header.biBitCount = 24
            header.biCompression = 0
            header.biSizeImage = stride * height
            info = BitmapInfo()
            info.bmiHeader = header
            buffer = ctypes.create_string_buffer(stride * height)
            copied = gdi32.GetDIBits(
                memory_dc,
                bitmap,
                0,
                height,
                buffer,
                ctypes.byref(info),
                DIB_RGB_COLORS,
            )
            if copied == 0:
                raise ScreenUnavailable("GetDIBits failed")
            raw = buffer.raw
        finally:
            gdi32.SelectObject(memory_dc, previous)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(None, screen_dc)

        rgb = bytearray(width * height * 3)
        for row in range(height):
            source = raw[row * stride : row * stride + width * 3]
            target = row * width * 3
            rgb[target + 0 : target + width * 3 : 3] = source[2::3]
            rgb[target + 1 : target + width * 3 : 3] = source[1::3]
            rgb[target + 2 : target + width * 3 : 3] = source[0::3]
        return RawImage(width=width, height=height, rgb=bytes(rgb))


def image_to_bmp(image: RawImage) -> bytes:
    """Encode top-down RGB into a 24-bit BMP (bottom-up rows, 4-byte padding)."""
    row_size = image.width * 3
    stride = ((row_size + 3) // 4) * 4
    padding = b"\x00" * (stride - row_size)
    pixel_bytes = bytearray()
    for row in range(image.height - 1, -1, -1):
        start = row * row_size
        pixels = bytearray(image.rgb[start : start + row_size])
        # RawImage is RGB; BMP stores BGR.
        pixels[0::3], pixels[2::3] = pixels[2::3], pixels[0::3]
        pixel_bytes.extend(pixels)
        pixel_bytes.extend(padding)
    file_size = 54 + len(pixel_bytes)
    header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 54)
    dib = struct.pack(
        "<IiiHHIIiiII",
        40,
        image.width,
        image.height,
        1,
        24,
        0,
        len(pixel_bytes),
        2835,
        2835,
        0,
        0,
    )
    return header + dib + bytes(pixel_bytes)


class ScreenCaptureArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        default="",
        max_length=4096,
        description="BMP path inside the workspace; empty means data/artifacts.",
    )
    overwrite: bool = False


class ScreenCaptureTool:
    name = "screen.capture"
    description = "Capture the desktop into a BMP inside the workspace (R1)."
    risk = "R1"
    capabilities = ("screen.capture",)
    args_model = ScreenCaptureArgs

    def __init__(self, backend: ScreenBackend | None = None) -> None:
        self._backend = backend or WindowsScreen()

    def _target(self, args: ScreenCaptureArgs, ctx: ToolContext) -> Path:
        if args.path:
            raw = Path(args.path)
            if raw.suffix.lower() != ".bmp":
                raise ToolError("screen capture path must end with .bmp")
        else:
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            raw = Path("data") / "artifacts" / f"screen-{stamp}.bmp"
        candidate = raw if raw.is_absolute() else ctx.workspace / raw
        decision = ctx.zones.check(candidate, "write")
        if not decision.allowed:
            raise ZoneError(f"{decision.rule}: {decision.reason}")
        if decision.resolved is None:
            raise ZoneError("path could not be resolved")
        return decision.resolved

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, ScreenCaptureArgs)
        target = self._target(args, ctx)
        return Preview(
            "screen-capture",
            {"path": str(target), "exists": target.exists()},
            reversible=False,
            warnings=("the screenshot may contain private information",),
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, ScreenCaptureArgs)
        target = self._target(args, ctx)
        if not target.parent.is_dir():
            raise ToolError(f"parent directory does not exist: {target.parent}")
        exists = target.exists()
        if exists and not args.overwrite:
            raise ToolError(f"file exists: {target} (pass overwrite=true)")
        image = self._backend.capture()
        data = image_to_bmp(image)
        if len(data) > MAX_IMAGE_BYTES:
            raise ToolError(f"screenshot is too large: {len(data)} bytes")
        temporary = target.with_name(f".{target.name}.kivi-{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(data)
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink(missing_ok=True)
        return ToolResult(
            ok=True,
            output=f"captured {image.width}x{image.height} to {target}",
            artifacts=[str(target)],
            meta={"width": image.width, "height": image.height, "bytes": len(data)},
            trust="untrusted:vision",
        )


__all__ = [
    "MAX_IMAGE_BYTES",
    "RawImage",
    "ScreenBackend",
    "ScreenCaptureArgs",
    "ScreenCaptureTool",
    "ScreenUnavailable",
    "WindowsScreen",
    "image_to_bmp",
]
