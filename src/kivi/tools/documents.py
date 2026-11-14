"""PDF text and image OCR readers.

Both readers are read-only, workspace-confined and return untrusted file
content. Dependencies stay lazy so the base install does not need OpenCV.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kivi.safety.zones import ZoneError
from kivi.tools.base import Preview, ToolContext, ToolError, ToolResult

_OCR_LOCK = threading.Lock()
_OCR_ENGINE: Any = None
_MAX_DOCUMENT_BYTES = 32 * 1024 * 1024


class _DocumentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PdfTextArgs(_DocumentArgs):
    path: str = Field(min_length=1, max_length=4096)
    max_pages: int = Field(default=20, ge=1, le=200)
    start_page: int = Field(default=1, ge=1)


class OcrArgs(_DocumentArgs):
    path: str = Field(min_length=1, max_length=4096)


def _resolve(ctx: ToolContext, raw: str) -> Path:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = ctx.workspace / candidate
    decision = ctx.zones.check(candidate, "read")
    if not decision.allowed:
        raise ZoneError(f"{decision.rule}: {decision.reason}")
    if decision.resolved is None:
        raise ZoneError("path could not be resolved")
    return decision.resolved


def _checked_file(ctx: ToolContext, raw: str) -> Path:
    target = _resolve(ctx, raw)
    if not target.is_file():
        raise ToolError(f"not a file: {target}")
    if target.stat().st_size > _MAX_DOCUMENT_BYTES:
        raise ToolError(f"file is larger than {_MAX_DOCUMENT_BYTES} bytes")
    return target


class PdfTextTool:
    name = "doc.pdf_text"
    description = "Extract text from a PDF in the workspace; read-only."
    risk = "R0"
    capabilities = ("fs.read",)
    args_model = PdfTextArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, PdfTextArgs)
        target = _resolve(ctx, args.path)
        return Preview(
            "extract text",
            {"path": str(target), "start_page": args.start_page, "max_pages": args.max_pages},
            reversible=True,
        )

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, PdfTextArgs)
        target = _checked_file(ctx, args.path)
        try:
            from pypdf import PdfReader
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ToolError("PDF support is not installed: pip install pypdf") from exc
        try:
            reader = PdfReader(str(target))
            pages = reader.pages
            start = args.start_page - 1
            selected = pages[start : start + args.max_pages]
            chunks: list[str] = []
            for offset, page in enumerate(selected, start=args.start_page):
                text = page.extract_text() or ""
                chunks.append(f"--- page {offset} ---\n{text.strip()}")
        except Exception as exc:  # noqa: BLE001 - pypdf parsing boundary
            raise ToolError(f"cannot read PDF: {exc}") from exc
        output = "\n\n".join(chunks).strip()
        truncated = len(output) > ctx.limits.max_output_chars
        if truncated:
            output = output[: ctx.limits.max_output_chars]
        return ToolResult(
            ok=True,
            output=output,
            meta={
                "path": str(target),
                "pages": len(pages),
                "returned_pages": len(selected),
            },
            truncated=truncated,
            trust="untrusted:file",
        )


def _ocr_engine() -> Any:
    global _OCR_ENGINE
    with _OCR_LOCK:
        if _OCR_ENGINE is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise ToolError(
                    "OCR support is not installed: pip install rapidocr_onnxruntime"
                ) from exc
            _OCR_ENGINE = RapidOCR()
        return _OCR_ENGINE


class ImageOcrTool:
    name = "doc.ocr"
    description = "Read text from a local image with the offline RapidOCR engine."
    risk = "R0"
    capabilities = ("fs.read",)
    args_model = OcrArgs

    def dry_run(self, args: BaseModel, ctx: ToolContext) -> Preview:
        assert isinstance(args, OcrArgs)
        target = _resolve(ctx, args.path)
        return Preview("ocr", {"path": str(target)}, reversible=True)

    def run(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        assert isinstance(args, OcrArgs)
        target = _checked_file(ctx, args.path)
        engine = _ocr_engine()
        try:
            result, _elapsed = engine(str(target))
        except Exception as exc:  # noqa: BLE001 - OCR model boundary
            raise ToolError(f"OCR failed: {exc}") from exc
        lines: list[str] = []
        for item in result or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                text = str(item[1]).strip()
                if text:
                    lines.append(text)
        output = "\n".join(lines)
        truncated = len(output) > ctx.limits.max_output_chars
        if truncated:
            output = output[: ctx.limits.max_output_chars]
        return ToolResult(
            ok=True,
            output=output,
            meta={"path": str(target), "lines": len(lines)},
            truncated=truncated,
            trust="untrusted:file/vision",
        )


def build_document_tools() -> tuple[PdfTextTool, ImageOcrTool]:
    return PdfTextTool(), ImageOcrTool()


__all__ = [
    "ImageOcrTool",
    "OcrArgs",
    "PdfTextArgs",
    "PdfTextTool",
    "build_document_tools",
]
