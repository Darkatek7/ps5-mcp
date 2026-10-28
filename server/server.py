"""Runs the ps5 MCP server from a checkout: python server/server.py. The tools are in ps5mcp/tools.py; installed,
the same server is the ps5-mcp command."""

import sys
from pathlib import Path

# The package sits next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ps5mcp.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
