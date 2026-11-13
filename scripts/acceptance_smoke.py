"""Offline acceptance smoke for the Kivi prototype.

Run from the repository root::

    .venv\\Scripts\\python.exe scripts\\acceptance_smoke.py

The script checks the installed core, UI assets, launcher, CRM transport and
optional media/avatar pieces. It never calls the network and never writes files.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))


def check(name: str, ok: bool, detail: str, required: bool = True) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail, "required": required}


def main() -> int:
    parser = argparse.ArgumentParser(description="Kivi offline acceptance smoke")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    results: list[dict[str, Any]] = []
    try:
        from kivi.config import load_config

        config = load_config()
        results.append(check("config", True, "config/kivi.yaml is valid"))
    except Exception as exc:  # noqa: BLE001 - acceptance boundary
        results.append(check("config", False, str(exc)))
        config = None

    required_files = [
        "README.md",
        "Kivi UI.bat",
        "web/ui/index.html",
        "web/ui/app.js",
        "web/ui/styles.css",
        "web/avatar3d/index.html",
        "docs/ACCEPTANCE.md",
    ]
    missing = [name for name in required_files if not (ROOT / name).is_file()]
    results.append(
        check("ui-assets", not missing, "all UI files present" if not missing else f"missing: {missing}")
    )

    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    results.append(
        check("no-console-launcher", pythonw.is_file(), f"{pythonw.name} present" if pythonw.is_file() else "pythonw missing")
    )

    try:
        from kivi.tools.registry import build_registry

        names = set(build_registry().names())
        wanted = {"fs.copy", "fs.move", "fs.search", "media.image", "media.video", "crm.read"}
        missing_tools = sorted(wanted - names)
        results.append(
            check(
                "tool-registry",
                not missing_tools,
                "required tools registered" if not missing_tools else f"missing: {missing_tools}",
            )
        )
    except Exception as exc:  # noqa: BLE001 - acceptance boundary
        results.append(check("tool-registry", False, str(exc)))

    if config is not None:
        token = config.crm.bridge_token_file
        results.append(
            check(
                "crm-token",
                token.is_file(),
                f"token file: {token}" if token.is_file() else f"missing token: {token} (run kivi crm token)",
                required=False,
            )
        )
        results.append(
            check(
                "crm-backend",
                config.crm.backend in {"extension", "cdp"},
                f"backend={config.crm.backend}",
            )
        )

    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        results.append(check("ffmpeg", bool(exe), Path(exe).name, required=False))
    except Exception as exc:  # noqa: BLE001 - optional media
        results.append(check("ffmpeg", False, str(exc), required=False))

    vrm = ROOT / "assets" / "avatar" / "kivi.vrm"
    results.append(
        check(
            "vrm-avatar",
            vrm.is_file(),
            "local VRM present (not committed)" if vrm.is_file() else "no VRM; procedural fallback",
            required=False,
        )
    )
    three = ROOT / "web" / "avatar3d" / "node_modules" / "three" / "build" / "three.module.js"
    results.append(
        check(
            "three-local",
            three.is_file(),
            "local three.js present" if three.is_file() else "run npm install in web/avatar3d",
            required=False,
        )
    )

    hard_failures = [item for item in results if item["required"] and not item["ok"]]
    if args.json:
        print(json.dumps({"ok": not hard_failures, "results": results}, ensure_ascii=False, indent=2))
    else:
        for item in results:
            mark = "PASS" if item["ok"] else ("FAIL" if item["required"] else "WARN")
            print(f"{mark:4} {item['name']:<20} {item['detail']}")
        print()
        print("ACCEPTANCE:", "PASS" if not hard_failures else f"FAIL ({len(hard_failures)} hard)")
    return 0 if not hard_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
