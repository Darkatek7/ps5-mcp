"""Configuration from the environment, the console's address (found by a discovery search when
PS5_IP is not set) and the constants the other modules share."""

from __future__ import annotations

import os
import re
import select
import socket
import tempfile
import time
from pathlib import Path

DDP_PORT = int(os.environ.get("PS5_DDP_PORT", "9302"))
DDP_VERSION = "00030010"


def parse_ddp(reply: bytes) -> dict[str, str]:
    """A discovery answer as {"status": "200", "host-name": ..., "system-version": ..., ...}."""
    first, *rest = reply.decode("latin1").splitlines()
    fields = dict(line.split(":", 1) for line in rest if ":" in line)
    status = first.split()
    fields["status"] = status[1] if len(status) >= 2 else ""
    return fields


def discover(timeout: float = 1.2) -> dict[str, dict[str, str]]:
    """Every PS5 on the local network that answers a discovery search (Remote Play's, needs no jailbreak), by
    address. The search goes out from each of the PC's addresses: on Windows a plain broadcast leaves through one
    adapter only, often a virtual one (WSL, Hyper-V)."""
    search = f"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:{DDP_VERSION}\n".encode()
    sockets = []
    for address in {a for a in socket.gethostbyname_ex(socket.gethostname())[2] if not a.startswith("127.")}:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((address, 0))
            for target in ("255.255.255.255", address.rsplit(".", 1)[0] + ".255"):
                s.sendto(search, (target, DDP_PORT))
            sockets.append(s)
        except OSError:
            s.close()
    found: dict[str, dict[str, str]] = {}
    end = time.time() + timeout
    try:
        while sockets and time.time() < end:
            for s in select.select(sockets, [], [], max(0.0, end - time.time()))[0]:
                reply, (address, _) = s.recvfrom(4096)
                fields = parse_ddp(reply)
                if fields.get("host-type") == "PS5":
                    found[address] = fields
    finally:
        for s in sockets:
            s.close()
    return found


IP = os.environ.get("PS5_IP", "")
if not IP:
    _consoles = discover()
    if len(_consoles) != 1:
        raise SystemExit("ps5-mcp: set PS5_IP to the console's address (see README); "
                         + (f"found several: {', '.join(sorted(_consoles))}" if _consoles else
                            "none answered a discovery search on this network"))
    IP = next(iter(_consoles))
FTP_PORT = int(os.environ.get("PS5_FTP_PORT", "2121"))
FTP_USER = os.environ.get("PS5_FTP_USER", "anonymous")
FTP_PASS = os.environ.get("PS5_FTP_PASS", "anonymous")
ELFLDR_PORT = int(os.environ.get("PS5_ELFLDR_PORT", "9021"))
KLOG_PORT = int(os.environ.get("PS5_KLOG_PORT", "3232"))
_first, _, _last = os.environ.get("PS5_REMOTEEYE_PORTS", "3321-3340").partition("-")
REMOTEEYE_PORTS = range(int(_first), int(_last or _first) + 1)
# A checkout's root (server/ps5mcp/config.py is two folders down); for an installed package it names a folder
# that holds no payloads, which is harmless.
REPO = Path(__file__).resolve().parents[2]
# Where "ps5-mcp fetch-payloads" puts the payloads of a release.
PAYLOAD_DIR = Path(os.environ.get("PS5_PAYLOAD_DIR") or Path.home() / ".ps5-mcp" / "payloads")


def payload(env: str, build: Path, name: str) -> Path:
    """The payload the environment names, else the first that exists of: the checkout's build, the checkout's
    root (where a release download goes), PAYLOAD_DIR."""
    if os.environ.get(env):
        return Path(os.environ[env])
    places = (build, REPO / name, PAYLOAD_DIR / name)
    return next((p for p in places if p.exists()), PAYLOAD_DIR / name)


REMOTEEYE_ELF = payload("PS5_REMOTEEYE_ELF", REPO / "payload" / "RemoteEye" / "out" / "RemoteEye.elf",
                        "RemoteEye.elf")
TITLECTL_ELF = payload("PS5_TITLECTL_ELF", REPO / "payload" / "titlectl" / "out" / "titlectl.elf", "titlectl.elf")
AVCAP_ELF = payload("PS5_AVCAP_ELF", REPO / "payload" / "avcap" / "out" / "avcap.elf", "avcap.elf")
SCREENSHOT_BACKEND = os.environ.get("PS5_SCREENSHOT_BACKEND", "auto").lower()
FFMPEG = os.environ.get("PS5_FFMPEG", "")
SMP_PORT = int(os.environ.get("PS5_SMP_PORT", "10101"))
REGIST_KEY = os.environ.get("PS5_REGIST_KEY", "")
REGIST_FILE = Path(os.environ.get("PS5_REGIST_FILE") or Path.home() / ".ps5-mcp" / "regist.json")
POWER_CHECK_S = 3
KLOG_QUIET_S = 60
KLOG_IDLE_S = 600
JXR2PNG = Path(__file__).with_name("jxr2png.ps1")
GALLERY = "/user/av_contents/photo"
THUMBNAILS = "/user/av_contents/thumbnails/photo"
DELETE_SHOTS = os.environ.get("PS5_DELETE_SHOTS") == "1"
SAVE_DIR = Path(os.environ["PS5_SAVE_DIR"]) if os.environ.get("PS5_SAVE_DIR") else None
IMAGE_SUFFIXES = (".jxr", ".png", ".jpg", ".jpeg")
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ps5-mcp"
INLINE_TEXT_LIMIT = 64 * 1024
# A real controller reports its state every few milliseconds. A press sent as one "down" and one "up" report was
# lost on a console where a real DualSense was also in use (only Create held for a screenshot, which is re-sent
# every 100 ms, came through), so a held state is re-sent every REPORT_INTERVAL_S and the release a few times.
REPORT_INTERVAL_S = int(os.environ.get("PS5_REPORT_MS", "20")) / 1000
RELEASE_REPORTS = 3
# A controller that was just added is opened by the system first; input sent at once is dropped.
NEW_PAD_SETTLE_S = 1.0

# ScePadButton bits. Create is the bit the system answers with a screenshot when it is held.
BUTTONS = {
    "create": 0x1, "share": 0x1,
    "l3": 0x2, "r3": 0x4, "options": 0x8,
    "up": 0x10, "right": 0x20, "down": 0x40, "left": 0x80,
    "l2": 0x100, "r2": 0x200, "l1": 0x400, "r1": 0x800,
    "triangle": 0x1000, "circle": 0x2000, "cross": 0x4000, "x": 0x4000, "square": 0x8000,
    "ps": 0x10000, "touchpad": 0x100000,
}

# The virtual pad's state block (docs/how-it-works.md). Touch points are not verified on a console: the count
# offset was read from libScePad, the point layout after it is assumed to follow ScePadTouchData
# ({u8 count; u8[7]; {u16 x; u16 y; u8 id; u8[3]} x2}). Coordinates are in touchpad pixels.
STATE_SIZE = 0xA8
TOUCH_COUNT_AT = 0x1C
TOUCH_POINTS_AT = (0x24, 0x2C)
TOUCHPAD_SIZE = (1920, 1080)
# RemoteEye builds that understand BLOCK (a whole state block, which touch input needs) and PS/KILL.
BLOCK_TAG = 4
PS_TAG = 6
# RemoteEye builds that answer titlectl's calls themselves (LAUNCH, CLOSE, RUNNING, URL, REST, OFF, ACCOUNT).
SYSTEM_TAG = 7
# Another session may hold RemoteEye (it serves one client at a time); wait this long for it to finish.
BUSY_WAIT_S = 15

# titlectl's argument slot: the marker, its NUL, then "<action>\0<argument>\0" in what is left of 1040 bytes.
TITLECTL_MARKER = b"PS5MCP-ARGS-V1:"
TITLECTL_ROOM = 1040 - len(TITLECTL_MARKER) - 1
TITLE_ID = re.compile(r"^[A-Z]{4}\d{5}$")

