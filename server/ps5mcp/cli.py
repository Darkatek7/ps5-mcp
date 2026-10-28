"""The ps5-mcp command. It loads the server only to run it: importing the configuration already looks for the
console, which fetch-payloads must not need."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    """ps5-mcp: runs the MCP server on stdio; "ps5-mcp fetch-payloads [folder]" downloads RemoteEye.elf and
    titlectl.elf and any available avcap.elf from the latest release instead
    (default folder: PS5_PAYLOAD_DIR, ~/.ps5-mcp/payloads)."""
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["fetch-payloads"]:
        from .fetch import fetch_payloads

        default = os.environ.get("PS5_PAYLOAD_DIR") or Path.home() / ".ps5-mcp" / "payloads"
        for line in fetch_payloads(Path(args[1]) if len(args) > 1 else Path(default)):
            print(line)
        return
    if args:
        raise SystemExit("usage: ps5-mcp [fetch-payloads [folder]]")
    from .tools import mcp

    mcp.run()
