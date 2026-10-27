"""MCP server that lets Claude see the PS5 screen, press its buttons and move files to and from it.

Input goes through the RemoteEye payload (payload/RemoteEye), a virtual controller on the console. The screen is
captured by the console itself: holding that controller's Create button saves a screenshot to the gallery,
which is fetched over FTP, decoded and scaled down here.

Console services it uses: elfldr (9021) to start RemoteEye, titlectl and other payloads, ftpsrv (2121) for
screenshots and files, klogsrv (3232) for the log, RemoteEye on 3320 + its build tag, and optionally
ShadowMountPlus's API (10101) to register uploaded apps.

Configuration (environment):
  PS5_IP          console address (required)
  PS5_REMOTEEYE_ELF  path to RemoteEye.elf; default: payload/RemoteEye/out/RemoteEye.elf, then RemoteEye.elf
                     in the repository root (where a release download goes)
  PS5_TITLECTL_ELF   path to titlectl.elf (starting and closing titles, the browser); the same two places
  PS5_USER_ID     user id in hex; default: the first folder under /user/home
  PS5_FTP_PORT    ftpsrv port (default 2121)
  PS5_FTP_USER    FTP user (default anonymous)
  PS5_FTP_PASS    FTP password (default anonymous)
  PS5_DELETE_SHOTS  set to 1 to delete each screenshot's files over FTP after fetching it. Off by default:
                    the gallery's database keeps an entry for the deleted file, which then shows as a blank
                    tile in the Media Gallery. Left on, the screenshots pile up there instead.
  PS5_SAVE_DIR    keep the original (full resolution) file of every screenshot in this folder
  PS5_ELFLDR_PORT / PS5_KLOG_PORT / PS5_REMOTEEYE_PORTS  other ports (default 9021 / 3232 / 3321-3340)
  PS5_SMP_PORT    ShadowMountPlus API port (default 10101)
  PS5_REGIST_KEY  RegistKey of a Remote Play registration of the console, for ps5_wake; default: the one
                  ps5_pair saved
  PS5_REGIST_FILE where ps5_pair saves it (default ~/.ps5-mcp/regist.json)
  PS5_DDP_PORT    Remote Play discovery port (default 9302), for ps5_wake and the power state
"""

from __future__ import annotations

import base64
import ftplib
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Literal

from mcp.server.fastmcp import FastMCP, Image
from PIL import Image as PILImage
from pydantic import BaseModel, Field

IP = os.environ.get("PS5_IP", "")
if not IP:
    raise SystemExit("ps5-mcp: set PS5_IP to the console's address (see README)")
FTP_PORT = int(os.environ.get("PS5_FTP_PORT", "2121"))
FTP_USER = os.environ.get("PS5_FTP_USER", "anonymous")
FTP_PASS = os.environ.get("PS5_FTP_PASS", "anonymous")
ELFLDR_PORT = int(os.environ.get("PS5_ELFLDR_PORT", "9021"))
KLOG_PORT = int(os.environ.get("PS5_KLOG_PORT", "3232"))
_first, _, _last = os.environ.get("PS5_REMOTEEYE_PORTS", "3321-3340").partition("-")
REMOTEEYE_PORTS = range(int(_first), int(_last or _first) + 1)
REPO = Path(__file__).resolve().parents[1]
REMOTEEYE_ELF = Path(os.environ["PS5_REMOTEEYE_ELF"]) if os.environ.get("PS5_REMOTEEYE_ELF") else next(
    (p for p in (REPO / "payload" / "RemoteEye" / "out" / "RemoteEye.elf", REPO / "RemoteEye.elf") if p.exists()),
    REPO / "RemoteEye.elf")
TITLECTL_ELF = Path(os.environ["PS5_TITLECTL_ELF"]) if os.environ.get("PS5_TITLECTL_ELF") else next(
    (p for p in (REPO / "payload" / "titlectl" / "out" / "titlectl.elf", REPO / "titlectl.elf") if p.exists()),
    REPO / "titlectl.elf")
SMP_PORT = int(os.environ.get("PS5_SMP_PORT", "10101"))
DDP_PORT = int(os.environ.get("PS5_DDP_PORT", "9302"))
DDP_VERSION = "00030010"
REGIST_KEY = os.environ.get("PS5_REGIST_KEY", "")
REGIST_FILE = Path(os.environ.get("PS5_REGIST_FILE") or Path.home() / ".ps5-mcp" / "regist.json")
POWER_CHECK_S = 3
KLOG_QUIET_S = 60
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

# titlectl's argument slot: the marker, its NUL, then "<action>\0<argument>\0" in what is left of 1040 bytes.
TITLECTL_MARKER = b"PS5MCP-ARGS-V1:"
TITLECTL_ROOM = 1040 - len(TITLECTL_MARKER) - 1
TITLE_ID = re.compile(r"^[A-Z]{4}\d{5}$")

mcp = FastMCP("ps5")
_lock = threading.Lock()


class RemoteEye:
    """The RemoteEye instance on the console, reached with one short connection per tool call.

    RemoteEye serves one client at a time and drops a client after 60 s of silence, so nothing holds a
    connection between tool calls: another session or a restarted server would otherwise find every
    instance busy. A payload cannot be unloaded and a new one cannot take an old one's port, so RemoteEye
    is only sent to the loader when no port in its range accepts a connection at all.
    """

    def __init__(self) -> None:
        self.port: int | None = None
        self.tag = 0
        self.sock: socket.socket | None = None
        self.reader = None
        self.last_call = 0.0

    @staticmethod
    def probe(port: int) -> tuple[str, int, socket.socket | None]:
        """("ok", tag, sock) for a RemoteEye that answered PING, ("busy", 0, None) for a port that accepts but
        does not answer, ("closed", 0, None) when nothing listens."""
        try:
            s = socket.create_connection((IP, port), timeout=1)
        except OSError:
            return "closed", 0, None
        try:
            s.settimeout(1.5)
            s.sendall(b"PING\n")
            m = re.search(rb" v(\d+) remoteeye", s.makefile("rb").readline())
            if m:
                s.settimeout(10)
                return "ok", int(m.group(1)), s
        except OSError:
            pass
        s.close()
        return "busy", 0, None

    @classmethod
    def probe_all(cls) -> list[tuple[int, str, int, socket.socket | None]]:
        """probe() on every port of the range at once: (port, kind, tag, sock) in port order. The console lets
        connections to closed ports time out rather than refusing them, so one after the other takes ~20 s."""
        with ThreadPoolExecutor(len(REMOTEEYE_PORTS)) as pool:
            return [(port, *result) for port, result in zip(REMOTEEYE_PORTS, pool.map(cls.probe, REMOTEEYE_PORTS))]

    def _locate(self, start: bool = True) -> socket.socket:
        found: list[tuple[int, int, socket.socket]] = []
        busy = []
        for port, kind, tag, s in self.probe_all():
            if kind == "ok":
                found.append((tag, port, s))
            elif kind == "busy":
                busy.append(port)
        if found:
            found.sort()
            tag, port, sock = found.pop()
            # Older instances keep running. One that still has a controller would leave a second pad behind
            # that nobody feeds, so it is removed.
            for _, _, old in found:
                try:
                    old.sendall(b"DETACH\n")
                    old.makefile("rb").readline()
                except OSError:
                    pass
                old.close()
            self.port, self.tag = port, tag
            return sock
        if busy:
            raise RuntimeError(f"RemoteEye on port(s) {busy} is busy with another client "
                               "(another session or script); try again in a minute")
        if not start:
            raise RuntimeError("RemoteEye is not running")
        if not REMOTEEYE_ELF.exists():
            raise RuntimeError(f"RemoteEye is not running and there is no {REMOTEEYE_ELF} to start it with "
                               "(download it from the releases page, or build it: payload/build.ps1)")
        send_elf(REMOTEEYE_ELF)
        self.port = None
        for _ in range(10):
            time.sleep(0.5)
            for port, kind, tag, s in self.probe_all():
                if kind == "ok" and self.port is None:
                    self.port, self.tag, sock = port, tag, s
                elif s:
                    s.close()
            if self.port is not None:
                return sock
        raise RuntimeError("RemoteEye was sent to elfldr but did not answer")

    @contextmanager
    def session(self, start: bool = True):
        """Holds one connection for the duration of a tool call. start=False does not send RemoteEye to the
        loader when it is not running."""
        sock = None
        if self.port is not None:
            kind, tag, sock = self.probe(self.port)
            if kind == "busy":
                raise RuntimeError(f"RemoteEye on port {self.port} is busy with another client; try again shortly")
            if kind == "ok":
                self.tag = tag
        if sock is None:
            sock = self._locate(start)
        self.sock, self.reader = sock, sock.makefile("rb")
        self.last_call = time.time()
        try:
            yield self
        finally:
            sock.close()
            self.sock = self.reader = None

    def call(self, line: str) -> str:
        if not self.sock:
            raise RuntimeError("RemoteEye.call outside a session")
        self.sock.sendall(line.encode() + b"\n")
        reply = self.reader.readline().decode().strip()
        if not reply:
            raise RuntimeError(f"RemoteEye did not answer '{line}'")
        self.last_call = time.time()
        return reply

    def attach(self) -> str:
        reply = self.call(f"ATTACH {user_id():x} 1")
        if not reply.startswith("OK"):
            raise RuntimeError(reply)
        if reply.endswith(" new"):
            self.wait(NEW_PAD_SETTLE_S)
        return reply

    def state(self, buttons: int = 0, lx: int = 128, ly: int = 128, rx: int = 128, ry: int = 128,
              l2: int = 0, r2: int = 0) -> None:
        reply = self.call(f"STATE {buttons:x} {lx} {ly} {rx} {ry} {l2} {r2}")
        if not reply.startswith("OK"):
            raise RuntimeError(reply)

    def lines(self, line: str) -> list[str]:
        """Sends a request whose answer is "OK", lines, then "END"; returns the lines."""
        reply = self.call(line)
        if not reply.startswith("OK"):
            raise RuntimeError(reply)
        out = []
        while (row := self.reader.readline().decode()) and row.rstrip("\n") != "END":
            out.append(row.rstrip("\n"))
        self.last_call = time.time()
        return out

    def block(self, block: bytes) -> None:
        """Feeds a whole state block (RemoteEye v4 and later)."""
        if self.tag < BLOCK_TAG:
            # The next call scans the ports again, so a newer build sent meanwhile is found.
            self.port = None
            raise RuntimeError(f"touch input needs RemoteEye v{BLOCK_TAG} or newer; the console runs v{self.tag}. "
                               "Send the new build (payload/build.ps1 -Send, or ps5_send_payload) and retry")
        reply = self.call("BLOCK " + block.rstrip(b"\0").hex())
        if not reply.startswith("OK"):
            raise RuntimeError(reply)

    def wait(self, seconds: float) -> None:
        """Sleeps while keeping the connection alive (RemoteEye drops a client after 60 s of silence)."""
        end = time.time() + seconds
        while (left := end - time.time()) > 0:
            time.sleep(min(left, 20))
            if self.sock and time.time() - self.last_call > 30:
                self.call("PING")


eye = RemoteEye()
_user_id: int | None = None


class KlogTail:
    """Reads klogsrv in the background, so the log can be looked at after something happened.

    klogsrv sends every line to every connected client (checked on a console), so holding this connection
    takes nothing away from other log readers. A connection that was open when the console went to or came
    back from rest mode stays open but gets no more lines (seen on 13.00), so the reader connects again when the
    power state differs from the one at connecting: it looks every POWER_CHECK_S, and ready() looks before a call.

    klogsrv itself can also go silent while clients stay connected (seen on 13.00, also without rest mode). It
    opens /dev/klog again only after every client has left, so ready() also connects again after KLOG_QUIET_S
    without a line, and titlectl() does when a payload ran without a single line being logged.
    """

    def __init__(self, keep: int = 20000) -> None:
        self.lines: deque[tuple[float, str]] = deque(maxlen=keep)
        self.guard = threading.Lock()
        self.since: float | None = None
        self.connected = threading.Event()
        self.thread: threading.Thread | None = None
        self.sock: socket.socket | None = None
        self.power: str | None = None
        self.retry = threading.Event()
        self.last = 0.0

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="klog", daemon=True)
            self.thread.start()

    def ready(self, timeout: float = 5) -> bool:
        """Starts the reader, connects again if the console rested or woke since it connected or the log has been
        quiet for KLOG_QUIET_S, and waits until it is connected."""
        self.start()
        if self.connected.wait(timeout):
            power = power_state()
            if (power is None or power == self.power) and time.time() < self.last + KLOG_QUIET_S:
                return True
            self.reconnect()
        return self.connected.wait(timeout)

    def reconnect(self) -> None:
        self.connected.clear()
        self.retry.set()

    def _run(self) -> None:
        while True:
            try:
                with socket.create_connection((IP, KLOG_PORT), timeout=5) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    # Short, so a request to connect again is seen soon: another thread's shutdown() does not
                    # end a waiting recv() on Windows.
                    s.settimeout(1)
                    if self.since is None:
                        self.since = time.time()
                    self.retry.clear()
                    self.sock, self.power, self.last = s, power_state(), time.time()
                    self.connected.set()
                    rest = b""
                    check = time.time() + POWER_CHECK_S
                    while not self.retry.is_set():
                        if time.time() >= check:
                            # Rest and wake can both happen between two tool calls, so look on our own.
                            check = time.time() + POWER_CHECK_S
                            power = power_state()
                            if power is not None and power != self.power:
                                self.retry.set()
                        try:
                            chunk = s.recv(65536)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        *done, rest = (rest + chunk).split(b"\n")
                        now = self.last = time.time()
                        with self.guard:
                            self.lines.extend((now, line.decode("latin1").rstrip("\r")) for line in done)
            except OSError:
                pass
            self.sock = None
            self.connected.clear()
            self.retry.wait(3)
            self.retry.clear()

    def read(self, since: float, contains: str = "") -> list[str]:
        with self.guard:
            return [line for t, line in self.lines if t >= since and contains in line]


klog = KlogTail()


def ftp() -> ftplib.FTP:
    f = ftplib.FTP()
    f.connect(IP, FTP_PORT, timeout=15)
    f.login(FTP_USER, FTP_PASS)
    return f


def user_id() -> int:
    global _user_id
    if _user_id is None:
        if os.environ.get("PS5_USER_ID"):
            _user_id = int(os.environ["PS5_USER_ID"], 16)
        else:
            with ftp() as f:
                homes = [n for n, facts in f.mlsd("/user/home") if facts.get("type") == "dir"]
            if not homes:
                raise RuntimeError("no user under /user/home; set PS5_USER_ID")
            _user_id = int(homes[0], 16)
    return _user_id


def send_elf(path: Path | bytes) -> None:
    with socket.create_connection((IP, ELFLDR_PORT), timeout=10) as s:
        s.sendall(path if isinstance(path, bytes) else path.read_bytes())


def entries(f: ftplib.FTP, path: str) -> list[tuple[str, dict]]:
    """A directory's entries without . and .., or [] when it cannot be listed."""
    try:
        return [(n, facts) for n, facts in f.mlsd(path) if facts.get("type") in ("dir", "file")]
    except ftplib.error_perm:
        return []


def gallery_dirs(f: ftplib.FTP) -> dict[str, str]:
    """Every screenshot folder (photo/<title>/<title>/<folder>) with its modify time.

    A folder's time changes when a file is added to it, so comparing two of these finds where a new
    screenshot went without listing the hundreds of folders a gallery collects: about a dozen MLSD calls.
    """
    out: dict[str, str] = {}
    for a, fa in entries(f, GALLERY):
        if fa["type"] != "dir":
            continue
        for b, fb in entries(f, f"{GALLERY}/{a}"):
            if fb["type"] != "dir":
                continue
            for c, fc in entries(f, f"{GALLERY}/{a}/{b}"):
                if fc["type"] == "dir":
                    out[f"{GALLERY}/{a}/{b}/{c}"] = fc.get("modify", "")
    return out


def new_shots(f: ftplib.FTP, before: dict[str, str]) -> dict[str, int]:
    """Files added to the gallery since `before` (from gallery_dirs), with their sizes."""
    out: dict[str, int] = {}
    for folder, modify in gallery_dirs(f).items():
        if before.get(folder) == modify:
            continue
        old = before.get(folder, "")
        for name, facts in entries(f, folder):
            # Console time against console time, in whole seconds. The last file of an older screenshot can
            # carry the folder's time, so only later files are new; a new screenshot is always later, as it is
            # written after Create has been held for more than a second.
            if facts["type"] == "file" and facts.get("modify", "") > old:
                out[f"{folder}/{name}"] = int(facts.get("size", 0))
    return out


def parse_buttons(spec: str) -> int:
    bits = 0
    for name in re.split(r"[+,\s]+", spec.strip().lower()):
        if not name:
            continue
        if name not in BUTTONS:
            raise ValueError(f"unknown button '{name}'; known: {', '.join(sorted(BUTTONS))}")
        bits |= BUTTONS[name]
    return bits


def axis(value: float) -> int:
    """-1 .. 1 to the pad's 0 .. 255 with 128 at rest."""
    return max(0, min(255, round(128 + value * 127.5)))


def decode(raw: bytes, suffix: str, max_width: int,
           crop: tuple[float, float, float, float] | None = None) -> bytes:
    """A screenshot file as a JPEG, cropped to (left, top, right, bottom) in 0..1 of the screen when given,
    then scaled down to max_width."""
    if suffix == ".jxr":
        image = decode_jxr(raw, 0 if crop else max_width)
    else:
        image = PILImage.open(BytesIO(raw))
        image.load()
    if crop:
        left, top, right, bottom = (max(0.0, min(1.0, v)) for v in crop)
        if right <= left or bottom <= top:
            raise ValueError("crop must be (left, top, right, bottom) with left < right and top < bottom, in 0..1")
        image = image.crop((round(left * image.width), round(top * image.height),
                            round(right * image.width), round(bottom * image.height)))
    if image.width > max_width:
        image = image.resize((max_width, round(image.height * max_width / image.width)), PILImage.LANCZOS)
    out = BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=85)
    return out.getvalue()


def decode_jxr(raw: bytes, max_width: int) -> PILImage.Image:
    """JPEG XR (what the console saves with HDR output): the Windows Imaging Component on Windows, else
    imagecodecs when it is installed."""
    if sys.platform == "win32":
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp, "shot.jxr"), Path(tmp, "shot.png")
            src.write_bytes(raw)
            subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(JXR2PNG),
                            "-In", str(src), "-Out", str(dst), "-MaxWidth", str(max_width)],
                           check=True, capture_output=True, timeout=60)
            image = PILImage.open(dst)
            image.load()
            return image
    try:
        import imagecodecs
        import numpy
    except ImportError:
        raise RuntimeError("the console saved a JPEG XR screenshot (HDR output). Outside Windows that needs "
                           "'pip install imagecodecs', or turn HDR off / set the screenshot format to PNG or JPG "
                           "on the console") from None
    pixels = imagecodecs.jpegxr_decode(raw)[..., :3]
    if pixels.dtype == numpy.uint16:
        # HDR screenshots hold PQ (SMPTE ST 2084) values. Decoded to nits, with 250 nits as white and the sRGB
        # curve on top, they match what WIC makes of the same file to within one level on average (measured on
        # console screenshots).
        m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
        e = numpy.power(pixels.astype(numpy.float32) / 65535, 1 / m2)
        nits = 10000 * numpy.power(numpy.maximum(e - c1, 0) / (c2 - c3 * e), 1 / m1)
        linear = numpy.clip(nits / 250, 0, 1)
        srgb = numpy.where(linear <= 0.0031308, linear * 12.92, 1.055 * numpy.power(linear, 1 / 2.4) - 0.055)
        pixels = (srgb * 255 + 0.5).astype(numpy.uint8)
    elif pixels.dtype != numpy.uint8:
        raise RuntimeError(f"unexpected JPEG XR pixel type {pixels.dtype}; turn HDR off or decode on Windows")
    image = PILImage.fromarray(pixels)
    if max_width and image.width > max_width:
        image = image.resize((max_width, round(image.height * max_width / image.width)), PILImage.LANCZOS)
    return image


# ---- what the tools do; callers hold _lock and an eye.session() ----

def _hold(seconds: float, bits: int, lx: int = 128, ly: int = 128, rx: int = 128, ry: int = 128) -> None:
    """Holds a state for `seconds`, re-sending it like a real controller does (see REPORT_INTERVAL_S); sent at
    least once."""
    l2, r2 = 255 if bits & BUTTONS["l2"] else 0, 255 if bits & BUTTONS["r2"] else 0
    end = time.time() + seconds
    while True:
        eye.state(bits, lx, ly, rx, ry, l2, r2)
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(left, REPORT_INTERVAL_S))


def _release() -> None:
    for i in range(RELEASE_REPORTS):
        if i:
            time.sleep(REPORT_INTERVAL_S)
        eye.state(0)


def _press(bits: int, hold_ms: int, repeat: int, gap_ms: int) -> None:
    for i in range(max(1, repeat)):
        _hold(hold_ms / 1000, bits)
        _release()
        if i + 1 < repeat:
            time.sleep(gap_ms / 1000)


def _stick(lx: float, ly: float, rx: float, ry: float, duration_ms: int, bits: int) -> None:
    _hold(duration_ms / 1000, bits, axis(lx), axis(ly), axis(rx), axis(ry))
    _release()


def touch_block(points: list[tuple[float, float]], bits: int = 0) -> bytes:
    block = bytearray(STATE_SIZE)
    block[0xC:0x10] = bits.to_bytes(4, "little")
    block[0x10:0x14] = bytes((128, 128, 128, 128))
    block[0x17] = 1
    block[TOUCH_COUNT_AT] = len(points)
    for i, (x, y) in enumerate(points[:2]):
        at = TOUCH_POINTS_AT[i]
        px = round(max(0.0, min(1.0, x)) * (TOUCHPAD_SIZE[0] - 1))
        py = round(max(0.0, min(1.0, y)) * (TOUCHPAD_SIZE[1] - 1))
        block[at:at + 4] = px.to_bytes(2, "little") + py.to_bytes(2, "little")
        block[at + 4] = i
    return bytes(block)


def _touch(x: float, y: float, to_x: float | None, to_y: float | None, duration_ms: int, click: bool) -> None:
    bits = BUTTONS["touchpad"] if click else 0
    steps = max(1, duration_ms // 16) if to_x is not None or to_y is not None else 1
    end_x = x if to_x is None else to_x
    end_y = y if to_y is None else to_y
    for i in range(steps + 1):
        t = i / steps
        eye.block(touch_block([(x + (end_x - x) * t, y + (end_y - y) * t)], bits))
        time.sleep(duration_ms / 1000 / (steps + 1))
    eye.state(0)


def _shoot(max_width: int, crop: tuple[float, float, float, float] | None) -> bytes:
    with ftp() as f:
        before = gallery_dirs(f)
        end = time.time() + 1.6
        while time.time() < end:
            eye.state(BUTTONS["create"])
            time.sleep(0.1)
        eye.state(0)

        shot, deadline, last_size = None, time.time() + 20, -1
        while time.time() < deadline:
            time.sleep(0.5)
            new = new_shots(f, before)
            images = [p for p in new if p.lower().endswith(IMAGE_SUFFIXES)]
            if images:
                shot = images[0]
                if new[shot] and new[shot] == last_size:
                    break
                last_size = new[shot]
        if not shot:
            raise RuntimeError(
                "no screenshot appeared in the gallery. Possible causes: the virtual controller is not "
                "assigned to a user yet (answer 'Who's using this controller?' with right, then cross); "
                "this screen blocks captures; or the Create button shortcut is set to 'Easy Screenshots' "
                "(Settings > Captures and Broadcasts > Shortcuts for Create Button must be Standard).")

        data = bytearray()
        f.retrbinary("RETR " + shot, data.extend)

        if DELETE_SHOTS:
            folder, stem = shot.rsplit("/", 1)[0], Path(shot).stem
            thumbs = THUMBNAILS + folder[len(GALLERY):]
            created = [f"{folder}/{n}" for n, facts in entries(f, folder) if n.startswith(stem)]
            created += [f"{thumbs}/{n}" for n, facts in entries(f, thumbs) if n.startswith(stem)]
            for p in created:
                try:
                    f.delete(p)
                except ftplib.error_perm:
                    pass

    if SAVE_DIR:
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        (SAVE_DIR / Path(shot).name).write_bytes(data)
    return decode(bytes(data), Path(shot).suffix.lower(), max_width, crop)


Crop = tuple[float, float, float, float]


@mcp.tool()
def ps5_screenshot(max_width: int = 1280, crop: Crop | None = None) -> Image:
    """Capture the PS5 screen as it is right now (the whole composited screen, system UI included).

    crop: (left, top, right, bottom) as fractions 0..1 of the screen, e.g. (0.5, 0, 1, 0.5) for the top right
      quarter. The crop is cut from the full-resolution screenshot, so it shows small text more sharply than
      the whole screen scaled down.
    Works by holding the virtual controller's Create button, which saves a screenshot to the console's
    gallery, from where it is fetched over FTP. Takes a few seconds.
    """
    with _lock, eye.session():
        eye.attach()
        data = _shoot(max_width, crop)
    return Image(data=data, format="jpeg")


@mcp.tool()
def ps5_press(buttons: str, hold_ms: int = 120, repeat: int = 1, gap_ms: int = 300) -> str:
    """Press controller buttons on the PS5.

    buttons: one name or a combination joined with '+', e.g. "cross", "down", "l1+r1".
      Names: cross circle square triangle up down left right l1 r1 l2 r2 l3 r3 options create ps touchpad.
    hold_ms: how long the buttons stay down. repeat: how many presses. gap_ms: pause between presses.

    A freshly started virtual controller is not bound to a user, and the console asks "Who's using this
    controller?" - answer it by pressing right (onto the user) and then cross.
    """
    bits = parse_buttons(buttons)
    with _lock, eye.session():
        eye.attach()
        _press(bits, hold_ms, repeat, gap_ms)
    return f"pressed {buttons} x{max(1, repeat)}"


@mcp.tool()
def ps5_stick(left_x: float = 0, left_y: float = 0, right_x: float = 0, right_y: float = 0,
              duration_ms: int = 300, buttons: str = "") -> str:
    """Tilt the analog sticks for a while, then let them go back to centre.

    Axes run from -1 to 1 (x: left to right, y: up to down). Optional buttons are held at the same time.
    """
    bits = parse_buttons(buttons) if buttons else 0
    with _lock, eye.session():
        eye.attach()
        _stick(left_x, left_y, right_x, right_y, duration_ms, bits)
    return "done"


@mcp.tool()
def ps5_touch(x: float, y: float, to_x: float | None = None, to_y: float | None = None,
              duration_ms: int = 200, click: bool = False) -> str:
    """Touch the touchpad with one finger: a tap at (x, y), or a swipe to (to_x, to_y) over duration_ms.

    Coordinates are fractions 0..1 of the touchpad (x: left to right, y: top to bottom). click also presses
    the touchpad down. Needs RemoteEye v4. Experimental: the touch layout of the virtual pad is not verified.
    """
    with _lock, eye.session():
        eye.attach()
        _touch(x, y, to_x, to_y, duration_ms, click)
    return "swiped" if to_x is not None or to_y is not None else "tapped"


class Step(BaseModel):
    """One step of ps5_sequence. Fields not used by the step's action are ignored."""
    action: Literal["press", "stick", "touch", "wait", "screenshot"]
    buttons: str = Field("", description="press: buttons like 'cross' or 'l1+r1'; stick: buttons held meanwhile")
    hold_ms: int = 120
    repeat: int = 1
    gap_ms: int = 300
    left_x: float = 0
    left_y: float = 0
    right_x: float = 0
    right_y: float = 0
    x: float = 0.5
    y: float = 0.5
    to_x: float | None = None
    to_y: float | None = None
    click: bool = False
    duration_ms: int = Field(300, description="stick, touch: how long; wait: how long to wait")
    max_width: int = 1280
    crop: Crop | None = None


MAX_SEQUENCE_S = 180


@mcp.tool()
def ps5_sequence(steps: list[Step]) -> list:
    """Run several steps in one call: presses, stick moves, touches, waits and screenshots, in order.

    Use it for anything you can plan ahead, e.g. navigating a menu and looking at the result:
      [{"action": "press", "buttons": "down", "repeat": 3},
       {"action": "press", "buttons": "cross"},
       {"action": "wait", "duration_ms": 2000},
       {"action": "screenshot"}]
    Returns one line per step and every screenshot in order. Stops at the first failing step. The waits add up
    to at most 180 s per call.
    """
    waited = sum(s.duration_ms for s in steps if s.action in ("wait", "stick", "touch")) / 1000
    if waited > MAX_SEQUENCE_S:
        raise ValueError(f"the steps wait {waited:.0f} s in all; split them into calls of at most {MAX_SEQUENCE_S} s")
    for s in steps:
        if s.action == "press":
            parse_buttons(s.buttons or "")
    out: list = []
    log: list[str] = []
    with _lock, eye.session():
        eye.attach()
        for i, s in enumerate(steps, 1):
            try:
                if s.action == "press":
                    _press(parse_buttons(s.buttons), s.hold_ms, s.repeat, s.gap_ms)
                    log.append(f"{i}. pressed {s.buttons} x{max(1, s.repeat)}")
                elif s.action == "stick":
                    _stick(s.left_x, s.left_y, s.right_x, s.right_y, s.duration_ms,
                           parse_buttons(s.buttons) if s.buttons else 0)
                    log.append(f"{i}. stick for {s.duration_ms} ms")
                elif s.action == "touch":
                    _touch(s.x, s.y, s.to_x, s.to_y, s.duration_ms, s.click)
                    log.append(f"{i}. touch at ({s.x}, {s.y})")
                elif s.action == "wait":
                    eye.wait(s.duration_ms / 1000)
                    log.append(f"{i}. waited {s.duration_ms} ms")
                else:
                    out.append("\n".join(log) + f"\n{i}. screenshot:")
                    log = []
                    out.append(Image(data=_shoot(s.max_width, s.crop), format="jpeg"))
            except Exception as e:
                log.append(f"{i}. {s.action} failed: {e}; the remaining steps were not run")
                break
    if log:
        out.append("\n".join(log))
    return out


@mcp.tool()
def ps5_detach() -> str:
    """Remove the virtual controller. The next input adds a new one, which the console treats as a new
    controller and asks "Who's using this controller?" again. Use it when the controller is stuck or bound to
    the wrong user."""
    with _lock, eye.session(start=False):
        reply = eye.call("DETACH")
    return f"detached ({reply})"


@mcp.tool()
def ps5_klog(seconds: float = 3, filter: str = "", since_s: float = 0) -> str:
    """Read the console's kernel log (klogsrv); optionally keep only lines containing filter.

    Returns what is logged during the next `seconds`, plus the last `since_s` seconds before the call: the log
    is read continuously in the background, so after something went wrong, ps5_klog(seconds=0, since_s=60)
    shows what led up to it. Payloads log with the 'sp:' prefix, and app crashes show up as coredump lines.
    """
    start = time.time()
    if not klog.ready():
        raise RuntimeError(f"klogsrv (port {KLOG_PORT}) does not answer")
    time.sleep(max(0.0, seconds))
    lines = klog.read(start - since_s, filter)
    text = "\n".join(lines[-400:]) or "(nothing logged)"
    if since_s and klog.since and klog.since > start - since_s:
        text = f"(the log is kept since {time.time() - klog.since:.0f} s ago)\n" + text
    return text


@mcp.tool()
def ps5_send_payload(path: str, log_seconds: float = 0) -> str:
    """Send an ELF payload to the console's loader (elfldr, port 9021). The loader does not report whether it
    ran; set log_seconds to get what the kernel log shows in that time after sending (payload output,
    crashes)."""
    p = Path(path).resolve()
    if log_seconds:
        klog.ready()
    start = time.time()
    send_elf(p)
    # It may be a newer RemoteEye: look for the newest instance again on the next call.
    eye.port = None
    text = f"sent {p.name} ({p.stat().st_size} bytes)"
    if log_seconds:
        time.sleep(log_seconds)
        text += "\n" + ("\n".join(klog.read(start)[-400:]) or "(nothing logged)")
    return text


# ---- titles ----

_titlectl_lock = threading.Lock()
_names: dict[str, str] = {}


def titlectl(action: str, argument: str = "", timeout: float = 10) -> str:
    """Runs titlectl on the console and returns its report (the kernel log line after "ps5-mcp:titlectl: ").

    titlectl is one prebuilt payload: its arguments are written into a slot in the ELF before it is sent, so any
    title can be named without a compiler on the PC. elfldr reports nothing back, so the payload logs its result.
    """
    if not TITLECTL_ELF.exists():
        raise RuntimeError(f"there is no {TITLECTL_ELF} (download it from the releases page, or build it: "
                           "payload/titlectl/build.sh)")
    elf = TITLECTL_ELF.read_bytes()
    at = elf.find(TITLECTL_MARKER + b"\0")
    if at < 0:
        raise RuntimeError(f"{TITLECTL_ELF} has no argument slot; rebuild it")
    args = action.encode() + b"\0" + argument.encode() + b"\0"
    if len(args) > TITLECTL_ROOM:
        raise ValueError(f"the argument is too long ({len(args)} bytes, at most {TITLECTL_ROOM})")
    at += len(TITLECTL_MARKER) + 1
    elf = elf[:at] + args + elf[at + len(args):]

    if not klog.ready():
        raise RuntimeError(f"titlectl reports through the kernel log, and klogsrv (port {KLOG_PORT}) does not answer")
    with _titlectl_lock:
        start = time.time()
        send_elf(elf)
        while time.time() < start + timeout:
            for line in klog.read(start, "ps5-mcp:titlectl: "):
                report = line.split("ps5-mcp:titlectl: ", 1)[1].strip()
                if report.startswith((action, "bad-args")):
                    return report
            time.sleep(0.1)
        if not klog.read(start):
            # Not even the kernel's line about the payload ending came: klogsrv has gone silent.
            klog.reconnect()
            raise RuntimeError(f"klogsrv sent nothing for {timeout:.0f} s, so titlectl's answer was lost (it may "
                               "still have acted). The log reader connected again; try once more. If it stays "
                               "silent, close every other klogsrv client (other ps5-mcp servers): klogsrv only "
                               "reopens the kernel log once no client is connected")
    raise RuntimeError(f"titlectl did not report within {timeout:.0f} s (is elfldr on port {ELFLDR_PORT} running?)")


def report_code(report: str) -> int:
    """The rc=0x... of a titlectl report as a signed 32-bit number."""
    m = re.search(r"rc=0x([0-9a-f]+)", report)
    if not m:
        raise RuntimeError(f"unexpected titlectl report: {report}")
    value = int(m.group(1), 16)
    return value - (1 << 32) if value >= 1 << 31 else value


def check_title_id(title_id: str) -> str:
    title_id = title_id.strip().upper()
    if not TITLE_ID.match(title_id):
        raise ValueError(f"'{title_id}' is not a title id (four letters and five digits, e.g. PPSA01234)")
    return title_id


def title_name(f: ftplib.FTP, title_id: str) -> str:
    """The title's name from its param.json (the app metadata, else the app's own sce_sys), or ''."""
    if title_id not in _names:
        name = ""
        for path in (f"/user/appmeta/{title_id}/param.json", f"/user/app/{title_id}/sce_sys/param.json"):
            data = bytearray()
            try:
                f.retrbinary("RETR " + path, data.extend)
                param = json.loads(data.decode("utf-8", "replace").rstrip("\0"))
            except (ftplib.error_perm, ValueError):
                continue
            local = param.get("localizedParameters", {})
            name = local.get(local.get("defaultLanguage", "en-US"), {}).get("titleName", "").strip()
            if name:
                break
        _names[title_id] = name
    return _names[title_id]


def running_title() -> tuple[str, int] | None:
    """(title id, app id) of the app in front, or None on the home screen."""
    report = titlectl("running")
    m = re.match(r"running (\S+) app_id=0x([0-9a-f]+)", report)
    return (m.group(1), int(m.group(2), 16)) if m else None


@mcp.tool()
def ps5_titles(contains: str = "") -> str:
    """List the installed titles (games, apps, homebrew registered by ShadowMountPlus) with their names.
    contains: keep only titles whose id or name contains this (case-insensitive)."""
    with ftp() as f:
        ids = sorted(n for n, facts in entries(f, "/user/app") if facts["type"] == "dir")
        rows = [(tid, title_name(f, tid)) for tid in ids]
    if contains:
        rows = [(tid, name) for tid, name in rows if contains.lower() in f"{tid} {name}".lower()]
    return "\n".join(f"{tid}  {name or '(no name)'}" for tid, name in rows) or "no titles found"


@mcp.tool()
def ps5_running() -> str:
    """Which app is in front on the console (its title id and name), or that the home screen is."""
    current = running_title()
    if not current:
        return "no app is running in front (home screen)"
    with ftp() as f:
        name = title_name(f, current[0])
    return " ".join(filter(None, (current[0], name, f"(app id 0x{current[1]:x})")))


@mcp.tool()
def ps5_launch(title_id: str) -> str:
    """Start a title for the user in front of the console, e.g. "PPSA01234" (see ps5_titles). Homebrew must be
    registered first (by ShadowMountPlus; see ps5_rescan). The title comes up after a few seconds; check with
    ps5_running or a screenshot."""
    title_id = check_title_id(title_id)
    rc = report_code(titlectl("launch", title_id))
    if rc < 0:
        raise RuntimeError(f"the console did not start {title_id}: error 0x{rc & 0xFFFFFFFF:08x} (is it installed? "
                           "ps5_titles lists what is). The console may show an error dialog; cross dismisses it")
    return f"started {title_id} (app id 0x{rc:x})"


@mcp.tool()
def ps5_close(title_id: str, quit_file: str = "", timeout_s: float = 30) -> str:
    """Close a title, the way the system's own "Close App" does. Only that title is closed: when another one is
    in front, nothing happens.

    quit_file: for an app of your own that ends itself when a file appears (e.g. /data/PS5SD/quit), the file to
      write instead. Prefer that for apps that render with the GPU, which should leave on their own terms. The
      tool waits until the app took the file and is gone, up to timeout_s.
    """
    title_id = check_title_id(title_id)
    if not quit_file:
        report = titlectl("close", title_id)
        if "not-running" in report:
            return f"{title_id} is not running"
        rc = report_code(report)
        if rc != 0:
            raise RuntimeError(f"the console did not close {title_id}: error 0x{rc & 0xFFFFFFFF:08x}")
        return f"closed {title_id}"

    end = time.time() + timeout_s
    with ftp() as f:
        f.storbinary("STOR " + quit_file, BytesIO(b"quit\n"))
        while time.time() < end and _remote_kind(f, quit_file) == "file":
            time.sleep(1)
        if _remote_kind(f, quit_file) == "file":
            try:
                f.delete(quit_file)
            except ftplib.error_perm:
                pass
            raise RuntimeError(f"{title_id} did not take {quit_file} within {timeout_s:.0f} s (is it running, and does "
                               "it watch that file?); removed the file again")
    while time.time() < end:
        current = running_title()
        if not current or current[0] != title_id:
            return f"{title_id} took {quit_file} and has closed"
        time.sleep(1)
    return f"{title_id} took {quit_file} but is still running after {timeout_s:.0f} s"


@mcp.tool()
def ps5_open_url(url: str) -> str:
    """Open the console's web browser at url (http or https). It opens as a card over the running app, which keeps
    running. Move the cursor with the left stick, click with cross, scroll with the right stick; close the card
    with Options (hold it ~300 ms), then "Close Card"."""
    if not re.match(r"^https?://\S+$", url):
        raise ValueError("url must start with http:// or https://")
    rc = report_code(titlectl("url", url))
    if rc != 0:
        raise RuntimeError(f"the console did not open the browser: error 0x{rc & 0xFFFFFFFF:08x}")
    return f"opened {url}"


# ---- power ----

def ddp(msg_type: str, fields: dict[str, str] | None = None) -> bytes:
    """A message of Remote Play's discovery protocol, as pyremoteplay's ddp.py builds it."""
    lines = [f"{msg_type} * HTTP/1.1"] + [f"{k}:{v}" for k, v in (fields or {}).items()]
    return ("\n".join(lines) + f"\ndevice-discovery-protocol-version:{DDP_VERSION}\n").encode()


def power_state(timeout: float = 1) -> str | None:
    """"on", "rest" or None (no answer: off, or not on the network) from a discovery search, which needs no key."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        try:
            s.sendto(ddp("SRCH"), (IP, DDP_PORT))
            reply = s.recvfrom(4096)[0].decode("latin1")
        except OSError:
            return None
    status = reply.split("\n", 1)[0].split()
    if len(status) >= 2 and status[1] == "200":
        return "on"
    if len(status) >= 2 and status[1] == "620":
        return "rest"
    return None


def wake_credential(key: str) -> str:
    """The user credential of a wake message from the RegistKey a Remote Play registration stores: hex of an ASCII
    hex string, as pyremoteplay keeps it (its format_regist_key)."""
    try:
        return str(int.from_bytes(bytes.fromhex(bytes.fromhex(key.strip()).decode("ascii")), "big"))
    except ValueError:
        raise ValueError("the RegistKey is not hex (16 digits, as the console sends it and pyremoteplay stores it)"
                         ) from None


# Remote Play registration, ported from pyremoteplay's register.py and crypt.py (ktnrg45, GPL-3.0): the PIN and a
# random nonce make an AES-CFB key and IV, the request carries the account id encrypted with them, and the
# console's answer (encrypted the same way) holds the RegistKey. The constants are the PS5 bytes of its keys.py.
RP_PORT = 9295
RP_SEARCH_PORT = 9303
RP_CLIENT_TYPE = "dabfa2ec873de5839bee8d3f4c0239c4282c07c25c6077a2931afcf0adc0d34f"
RP_REG_KEY_0 = bytes.fromhex("d860e146bdb0bd94b460070bb14fe723")
RP_REG_KEY_1 = bytes.fromhex("a83761b527fc076e62433aa701958a85")
RP_HMAC_KEY = bytes.fromhex("464687b349ca8ce859c5270f5d7a69d6")


def _rp_cipher(key: bytes, nonce: bytes, encrypt: bool):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
    try:
        from cryptography.hazmat.decrepit.ciphers.modes import CFB
    except ImportError:
        from cryptography.hazmat.primitives.ciphers.modes import CFB

    iv = hmac.new(RP_HMAC_KEY, nonce + bytes(8), hashlib.sha256).digest()[:16]
    cipher = Cipher(algorithms.AES(key), CFB(iv))
    return cipher.encryptor() if encrypt else cipher.decryptor()


def rp_register(account_id: int, pin: str, timeout: float = 5) -> dict[str, str]:
    """Registers this PC for Remote Play with the console, which must be showing (or have generated) pin, and
    returns the console's answer, e.g. {"PS5-RegistKey": ..., "PS5-Mac": ..., "RP-Key": ...}."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        # From 9303, as pyremoteplay's const.py says the PS5 needs; the registration that worked on the test
        # console came from there.
        try:
            s.bind(("", RP_SEARCH_PORT))
        except OSError:
            pass
        s.settimeout(timeout)
        s.sendto(b"SRC3", (IP, RP_PORT))
        try:
            ready = s.recv(32)
        except OSError:
            ready = b""
    if not ready.startswith(b"RES3"):
        raise RuntimeError("the console is not waiting for a Remote Play device (no RES3 on port 9295)")

    nonce = os.urandom(16)
    key_0 = bytearray(RP_REG_KEY_0)
    for i, byte in enumerate(int(pin).to_bytes(4, "big")):
        key_0[12 + i] ^= byte
    key_1 = bytes(((nonce[i] ^ RP_REG_KEY_1[i]) - 45 + i) % 256 for i in range(16))
    filler = b"A" * 480
    body = filler[:199] + key_1[8:] + filler[207:401] + key_1[:8] + filler[409:]
    account = base64.b64encode(account_id.to_bytes(8, "little")).decode()
    body += _rp_cipher(bytes(key_0), nonce, True).update(
        f"Client-Type: {RP_CLIENT_TYPE}\r\nNp-AccountId: {account}\r\n".encode())
    # The console expects this exact (malformed) request line.
    head = ("POST /sie/ps5/rp/sess/rgst HTTP/1.1\r\n HTTP/1.1\r\nHOST: 10.0.2.15\r\n"
            "User-Agent: remoteplay Windows\r\nConnection: close\r\n"
            f"Content-Length: {len(body)}\r\nRP-Version: 1.0\r\n\r\n").encode()
    with socket.create_connection((IP, RP_PORT), timeout=timeout) as s:
        s.sendall(head + body)
        answer = b""
        try:
            while chunk := s.recv(4096):
                answer += chunk
        except socket.timeout:
            pass
    status, _, rest = answer.partition(b"\r\n")
    if status.split()[1:2] != [b"200"]:
        raise RuntimeError(f"the console refused the registration: {status.decode('latin1') or 'no answer'} "
                           "(a wrong PIN or account id)")
    encrypted = rest.partition(b"\r\n\r\n")[2]
    text = _rp_cipher(bytes(key_0), nonce, False).update(encrypted).decode("latin1")
    return dict(line.split(": ", 1) for line in text.split("\r\n") if ": " in line)


def saved_regist_key() -> str:
    """The RegistKey for this console: PS5_REGIST_KEY, else the one ps5_pair saved."""
    if REGIST_KEY:
        return REGIST_KEY
    try:
        return json.loads(REGIST_FILE.read_text()).get(IP, {}).get("PS5-RegistKey", "")
    except (OSError, ValueError):
        return ""


@mcp.tool()
def ps5_pair(pin: str) -> str:
    """Register this PC as a Remote Play device of the console (once), so ps5_wake can wake it. On the console,
    open Settings > System > Remote Play > Link Device and pass the 8-digit PIN it shows. The user needs an
    account id (signed in to PSN, or activated offline). The key is saved to PS5_REGIST_FILE (default
    ~/.ps5-mcp/regist.json); PS5_REGIST_KEY, when set, is used instead."""
    pin = re.sub(r"\s", "", pin)
    if not re.fullmatch(r"\d{8}", pin):
        raise ValueError("the PIN has 8 digits (Settings > System > Remote Play > Link Device)")
    if power_state() == "rest":
        raise RuntimeError("the console is in rest mode; wake it first")
    # A PIN made by a payload is refused (REGIST_DEVICE_MISS): there the system sees no foreground user. So the
    # PIN comes from the console's own screen, and titlectl only reads the account id.
    report = titlectl("account", f"{user_id():x}")
    m = re.match(r"account 0x([0-9a-f]+)", report)
    if not m:
        raise RuntimeError(f"user {user_id():x} has no account id: sign it in to PSN or activate it offline "
                           "(OffAct), then restart the console")
    info = rp_register(int(m.group(1), 16), pin)
    key = info.get("PS5-RegistKey")
    if not key:
        raise RuntimeError(f"the console answered without a RegistKey: {sorted(info)}")
    wake_credential(key)
    saved = {}
    try:
        saved = json.loads(REGIST_FILE.read_text())
    except (OSError, ValueError):
        pass
    saved[IP] = info
    REGIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    REGIST_FILE.write_text(json.dumps(saved, indent=2))
    return f"paired as Remote Play device of {info.get('PS5-Nickname', 'the console')}; key saved to {REGIST_FILE}"


@mcp.tool()
def ps5_rest() -> str:
    """Put the console in rest mode, as its power menu does. The payloads (elfldr, ftpsrv, RemoteEye, ...) keep
    running and answer again once it wakes. Wake it with ps5_wake (after ps5_pair once), the controller or the
    TV over HDMI-CEC."""
    try:
        rc = report_code(titlectl("rest", timeout=5))
        if rc != 0:
            raise RuntimeError(f"the console did not go to rest: error 0x{rc & 0xFFFFFFFF:08x}")
    except RuntimeError as e:
        # The console may sleep before titlectl's line gets out; what counts is whether it rests.
        if "did not report" not in str(e):
            raise
    end = time.time() + 20
    while time.time() < end:
        if power_state() == "rest":
            return "the console is in rest mode"
        time.sleep(1)
    raise RuntimeError("the console did not report rest mode within 20 s")


@mcp.tool()
def ps5_wake(timeout_s: float = 60) -> str:
    """Wake the console from rest mode over the network (Remote Play's wake message), and wait until elfldr
    answers again. Needs a Remote Play registration (ps5_pair once, or PS5_REGIST_KEY), and in *Settings > System >
    Power Saving > Features Available in Rest Mode* both Stay Connected to the Internet and Enable Turning On PS5
    from Network."""
    key = saved_regist_key()
    if not key:
        raise RuntimeError("waking needs a Remote Play registration of this PC: run ps5_pair once while the "
                           "console is on (or set PS5_REGIST_KEY). Until then, wake it with the controller or "
                           "HDMI-CEC")
    credential = wake_credential(key)
    if power_state() == "on":
        return "the console is already on"
    message = ddp("WAKEUP", {"user-credential": credential, "client-type": "vr", "auth-type": "R",
                             "model": "w", "app-type": "r"})
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(message, (IP, DDP_PORT))
    start = time.time()
    while time.time() < start + timeout_s:
        if power_state() == "on":
            try:
                socket.create_connection((IP, ELFLDR_PORT), timeout=1).close()
                return f"the console is on (after {time.time() - start:.0f} s)"
            except OSError:
                pass
        time.sleep(1)
    raise RuntimeError(f"the console did not wake within {timeout_s:.0f} s. In rest mode it listens for the wake "
                       "message only with Stay Connected to the Internet and Enable Turning On PS5 from Network "
                       "(Settings > System > Power Saving > Features Available in Rest Mode); is the RegistKey "
                       "from this console?")


@mcp.tool()
def ps5_rescan() -> str:
    """Ask ShadowMountPlus to scan for homebrew again, so an app uploaded to /data/homebrew/<title id> gets
    registered (and then shows in ps5_titles and can be launched). Needs ShadowMountPlus with its API on port
    10101. ShadowMountPlus may defer the scan while a homebrew app is running or suspended; the answer says so."""
    request = urllib.request.Request(f"http://{IP}:{SMP_PORT}/api/v1/scan",
                                     data=json.dumps({"reset_attempts": True}).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.load(response)
    except OSError as e:
        raise RuntimeError(f"ShadowMountPlus's API (port {SMP_PORT}) does not answer: {e}") from None
    text = json.dumps(result)
    # The flag may sit in a nested object.
    if '"scan_deferred": true' in text:
        reason = re.search(r'"scan_deferred_reason": "([^"]*)"', text)
        queued = '"scan_queued": true' in text or '"queued": true' in text
        return (f"ShadowMountPlus deferred the scan{f' ({reason.group(1)})' if reason else ''}"
                f"{', but queued it' if queued else ''}. It defers while a homebrew app is running or suspended: "
                "close it (ps5_close) and scan again if the app does not show up\n" + text)
    if result.get("status") != 0:
        raise RuntimeError("ShadowMountPlus rejected the scan: " + text)
    _names.clear()
    return "scan started\n" + text


@mcp.tool()
def ps5_processes(contains: str = "") -> str:
    """The console's process table: pid, parent, state (2 = running, 3 = sleeping), name, what it waits on, thread
    name. Payloads sent through elfldr are named payload.elf (services like ftpsrv.elf keep their own name); one
    that hangs in a loop shows state 2 and no wait channel, as RemoteEye itself does while it answers.
    contains: keep only lines containing this. Needs RemoteEye v6."""
    with _lock, eye.session():
        if eye.tag < PS_TAG:
            eye.port = None
            raise RuntimeError(f"the process list needs RemoteEye v{PS_TAG} or newer; the console runs v{eye.tag}")
        rows = eye.lines("PS")
    rows = [r[2:] for r in rows if r.startswith("P ")]
    if contains:
        rows = [r for r in rows if contains.lower() in r.lower()]
    return "pid ppid state name wait thread\n" + "\n".join(rows)


@mcp.tool()
def ps5_kill_payload(pid: int) -> str:
    """End a payload that hangs (one named payload.elf in ps5_processes), e.g. to free the port it holds. Refuses
    any other process and RemoteEye itself; close titles with ps5_close instead. Other tools you loaded (e.g.
    ShadowMountPlus) can be payload.elf too: be sure of the pid. Needs RemoteEye v6."""
    with _lock, eye.session():
        if eye.tag < PS_TAG:
            eye.port = None
            raise RuntimeError(f"ending payloads needs RemoteEye v{PS_TAG} or newer; the console runs v{eye.tag}")
        reply = eye.call(f"KILL {int(pid)}")
    if not reply.startswith("OK"):
        raise RuntimeError(reply)
    return f"sent SIGKILL to {pid} ({reply})"


# ---- files ----

def _remote_kind(f: ftplib.FTP, path: str) -> str | None:
    """'file', 'dir' or None when the path does not exist."""
    parent, name = str(PurePosixPath(path).parent), PurePosixPath(path).name
    if not name:
        return "dir"
    for n, facts in entries(f, parent):
        if n == name:
            return facts["type"]
    return None


def _makedirs(f: ftplib.FTP, path: str) -> None:
    current = ""
    for part in PurePosixPath(path).parts[1:]:
        current += "/" + part
        if _remote_kind(f, current) is None:
            f.mkd(current)


@mcp.tool()
def ps5_ls(path: str = "/data") -> str:
    """List a folder on the console (over ftpsrv): one line per entry with type, size and modify time (UTC).
    Homebrew usually lives under /data; installed apps under /user/app; the gallery under /user/av_contents."""
    with ftp() as f:
        if _remote_kind(f, path) == "file":
            raise ValueError(f"{path} is a file")
        items = sorted(entries(f, path), key=lambda e: (e[1]["type"] != "dir", e[0].lower()))
    if not items:
        return f"{path}: empty or not found"
    lines = []
    for name, facts in items:
        modify = facts.get("modify", "")
        when = f"{modify[:4]}-{modify[4:6]}-{modify[6:8]} {modify[8:10]}:{modify[10:12]}" if len(modify) >= 12 else ""
        size = "<dir>" if facts["type"] == "dir" else facts.get("size", "?")
        lines.append(f"{size:>12}  {when}  {name}{'/' if facts['type'] == 'dir' else ''}")
    return f"{path}:\n" + "\n".join(lines)


@mcp.tool()
def ps5_upload(local_path: str, remote_path: str) -> str:
    """Copy a file or a whole folder from this computer to the console. Missing folders on the console are
    created; existing files are overwritten. When remote_path is an existing folder (or ends with '/'), the
    file or folder goes inside it."""
    src = Path(local_path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(src)
    with ftp() as f:
        target = remote_path.rstrip("/") or "/"
        if remote_path.endswith("/") or _remote_kind(f, target) == "dir":
            target = f"{target.rstrip('/')}/{src.name}"
        files = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.is_file())
        total = 0
        made: set[str] = set()
        for p in files:
            dest = target if src.is_file() else f"{target}/{p.relative_to(src).as_posix()}"
            parent = str(PurePosixPath(dest).parent)
            if parent not in made:
                _makedirs(f, parent)
                made.add(parent)
            with p.open("rb") as fh:
                f.storbinary("STOR " + dest, fh)
            total += p.stat().st_size
    return f"uploaded {len(files)} file(s), {total} bytes, to {target}"


@mcp.tool()
def ps5_download(remote_path: str, local_path: str = "") -> str:
    """Copy a file or folder from the console to this computer.

    Without local_path, a small text file (logs, configs) is returned directly; anything else is saved
    under the system temp folder and its path returned. A folder is copied with everything in it.
    """
    with ftp() as f:
        kind = _remote_kind(f, remote_path)
        if kind is None:
            raise FileNotFoundError(f"{remote_path} does not exist on the console")
        if kind == "file":
            data = bytearray()
            f.retrbinary("RETR " + remote_path, data.extend)
            if not local_path and len(data) <= INLINE_TEXT_LIMIT and b"\0" not in data:
                try:
                    return data.decode("utf-8")
                except UnicodeDecodeError:
                    pass
            dest = Path(local_path).expanduser() if local_path else DOWNLOAD_DIR / PurePosixPath(remote_path).name
            if dest.is_dir():
                dest = dest / PurePosixPath(remote_path).name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return f"saved {len(data)} bytes to {dest.resolve()}"

        dest = Path(local_path).expanduser() if local_path else DOWNLOAD_DIR / PurePosixPath(remote_path).name
        count = total = 0

        def fetch(remote: str, local: Path, depth: int = 0) -> None:
            nonlocal count, total
            local.mkdir(parents=True, exist_ok=True)
            for name, facts in entries(f, remote):
                if facts["type"] == "dir" and depth < 16:
                    fetch(f"{remote}/{name}", local / name, depth + 1)
                elif facts["type"] == "file":
                    with (local / name).open("wb") as fh:
                        f.retrbinary(f"RETR {remote}/{name}", fh.write)
                    count += 1
                    total += (local / name).stat().st_size

        fetch(remote_path.rstrip("/"), dest)
    return f"saved {count} file(s), {total} bytes, to {dest.resolve()}"


@mcp.tool()
def ps5_delete(remote_path: str, recursive: bool = False) -> str:
    """Delete a file on the console. A folder is only deleted with recursive=true, and then with everything in
    it. Files in the Media Gallery should be deleted on the console instead (see README)."""
    path = remote_path.rstrip("/")
    if path in ("", "/", "/data", "/user", "/system", "/system_ex", "/mnt"):
        raise ValueError(f"refusing to delete {remote_path or '/'}")
    with ftp() as f:
        kind = _remote_kind(f, path)
        if kind is None:
            raise FileNotFoundError(f"{path} does not exist on the console")
        if kind == "file":
            f.delete(path)
            return f"deleted {path}"
        if not recursive:
            raise ValueError(f"{path} is a folder; pass recursive=true to delete it with everything in it")
        count = 0

        def remove(folder: str, depth: int = 0) -> None:
            nonlocal count
            for name, facts in entries(f, folder):
                if facts["type"] == "dir" and depth < 32:
                    remove(f"{folder}/{name}", depth + 1)
                elif facts["type"] == "file":
                    f.delete(f"{folder}/{name}")
                    count += 1
            f.rmd(folder)

        remove(path)
    return f"deleted {path} ({count} files)"


@mcp.tool()
def ps5_move(from_path: str, to_path: str) -> str:
    """Rename or move a file or folder on the console."""
    with ftp() as f:
        f.rename(from_path, to_path)
    return f"moved {from_path} to {to_path}"


@mcp.tool()
def ps5_status() -> str:
    """Power state (on / rest mode), which console services answer, and the RemoteEye controller's state."""
    lines = [f"power: {({'on': 'on', 'rest': 'rest mode'}).get(power_state(), 'no answer')}"]
    for name, port in (("elfldr", ELFLDR_PORT), ("ftpsrv", FTP_PORT), ("klogsrv", KLOG_PORT)):
        try:
            socket.create_connection((IP, port), timeout=1).close()
            lines.append(f"{name} ({port}): up")
        except OSError:
            lines.append(f"{name} ({port}): down")
    try:
        lines.append(f"user: {user_id():x}")
    except (OSError, RuntimeError, ftplib.Error) as e:
        lines.append(f"user: unknown ({e})")
    with _lock:
        # Older instances keep running (a payload can't be unloaded); the server uses the newest.
        found, busy = [], []
        for port, kind, tag, sock in eye.probe_all():
            if kind == "ok":
                sock.sendall(b"PING\n")
                found.append((tag, sock.makefile("rb").readline().decode().strip()))
                sock.close()
            elif kind == "busy":
                busy.append(port)
        if found:
            found.sort()
            tag, reply = found[-1]
            handle = re.search(r"handle=(-?\d+)", reply)
            attached = "controller attached" if handle and int(handle.group(1)) >= 0 else "no controller yet"
            lines.append(f"remoteeye: v{tag}, {attached} ({reply})")
            if tag < BLOCK_TAG:
                lines.append(f"remoteeye: v{tag} has no touch input; send v{BLOCK_TAG} or newer for ps5_touch")
            if len(found) > 1:
                lines.append("remoteeye: older instances still running: " + ", ".join(f"v{t}" for t, _ in found[:-1]))
        if busy:
            lines.append(f"remoteeye: port(s) {busy} busy with another client")
        if not found and not busy:
            lines.append("remoteeye: not running (it is started on first use)")
    lines.append("klog reader: " + ("connected" if klog.connected.is_set() else "not connected"))
    return "\n".join(lines)


if __name__ == "__main__":
    klog.start()
    mcp.run()
