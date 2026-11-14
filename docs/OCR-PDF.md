# PDF and image readers

Two read-only tools are registered by default:

| Tool | Input | Output | Trust |
|---|---|---|---|
| `doc.pdf_text` | PDF in workspace | text by page | `untrusted:file` |
| `doc.ocr` | PNG/JPEG/WebP | offline OCR text | `untrusted:file/vision` |

Both use the workspace zone check and cap output at the normal tool limit.

```powershell
kivi tools run doc.pdf_text --args '{"path":"docs/report.pdf","max_pages":10}'
kivi tools run doc.ocr --args '{"path":"docs/scan.png"}'
```

Dependencies are in the `documents` extra:

```powershell
pip install -e ".[documents]"
```

`doc.ocr` uses RapidOCR + ONNX Runtime locally; no image leaves the machine.
