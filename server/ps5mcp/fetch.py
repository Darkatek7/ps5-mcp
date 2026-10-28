"""Fetch release payloads, including Avcap2 when available, checked against SHA256SUMS."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path

RELEASES = "https://api.github.com/repos/Darkatek7/ps5-mcp/releases/latest"
PAYLOADS = ("RemoteEye.elf", "titlectl.elf")
OPTIONAL_PAYLOADS = ("avcap.elf",)  # Releases before direct capture have only the original pair.


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "ps5-mcp"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def fetch_payloads(folder: Path, releases: str = RELEASES) -> list[str]:
    """Downloads the payloads of the latest release into folder and returns what it did, a line per file. A file
    whose hash does not match the release's SHA256SUMS is not written."""
    release = json.loads(_get(releases))
    assets = {asset["name"]: asset["browser_download_url"] for asset in release.get("assets", [])}
    if "SHA256SUMS" not in assets:
        raise RuntimeError(f"release {release.get('tag_name')} has no SHA256SUMS (it predates the CI builds)")
    sums = dict(reversed(line.split(maxsplit=1)) for line in _get(assets["SHA256SUMS"]).decode().splitlines()
                if line.strip())
    folder.mkdir(parents=True, exist_ok=True)
    report = []
    for name in PAYLOADS + tuple(n for n in OPTIONAL_PAYLOADS if n in assets):
        if name not in assets or name not in sums:
            raise RuntimeError(f"release {release.get('tag_name')} has no {name}")
        data = _get(assets[name])
        digest = hashlib.sha256(data).hexdigest()
        if digest != sums[name]:
            raise RuntimeError(f"{name} of release {release.get('tag_name')} does not match SHA256SUMS")
        (folder / name).write_bytes(data)
        report.append(f"{name}: {len(data)} bytes, sha256 {digest} -> {folder / name}")
    return [f"release {release.get('tag_name')}"] + report
