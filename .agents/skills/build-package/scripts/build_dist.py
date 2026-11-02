# coding=utf-8
"""Builds OctoPrint-OctocamRtc sdist (.zip) in dist/ using only the Python standard library."""
from __future__ import annotations

import os
import re
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
DIST_DIR = os.path.join(ROOT, "dist")

FILES_TO_PACKAGE = [
    "README.md",
    "MANIFEST.in",
    "pyproject.toml",
    "setup.py",
    "octoprint_octocamrtc/__init__.py",
    "octoprint_octocamrtc/webrtc_engine.py",
    "octoprint_octocamrtc/static/css/octocamrtc.css",
    "octoprint_octocamrtc/static/js/octocamrtc.js",
    "octoprint_octocamrtc/templates/octocamrtc_settings.jinja2",
    "octoprint_octocamrtc/templates/octocamrtc_webcam.jinja2",
    "tests/test_plugin.py",
    "tests/test_webrtc_engine.py",
]


def _read_version() -> str:
    setup_path = os.path.join(ROOT, "setup.py")
    with open(setup_path, "r", encoding="utf-8") as f:
        content = f.read()
    match = re.search(r'plugin_version\s*=\s*["\']([^"\']+)["\']', content)
    return match.group(1) if match else "0.1.0"


def main() -> None:
    version = _read_version()
    pkg_dir = f"OctoPrint-OctocamRtc-{version}"
    os.makedirs(DIST_DIR, exist_ok=True)

    pkg_info = (
        "Metadata-Version: 2.1\n"
        "Name: OctoPrint-OctocamRtc\n"
        f"Version: {version}\n"
        "Summary: WebRTC Webcam Provider for OctoPrint with STUN/TURN (Cloudflare TURN) & Linux VAAPI hardware acceleration\n"
        "Author: snargledorf\n"
        "License: AGPLv3\n"
        "Requires-Python: >=3.9,<4\n"
        "Description-Content-Type: text/markdown\n"
    )

    zip_path = os.path.join(DIST_DIR, f"{pkg_dir}.zip")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_path in FILES_TO_PACKAGE:
            full_path = os.path.join(ROOT, rel_path)
            zf.write(full_path, arcname=f"{pkg_dir}/{rel_path}")
        zf.writestr(f"{pkg_dir}/PKG-INFO", pkg_info)

    print(f"Built {zip_path} ({os.path.getsize(zip_path)} bytes)")


if __name__ == "__main__":
    main()
