"""MCP server that lets Claude see the PS5 screen, press its buttons and move files to and from it.

Input goes through the RemoteEye payload (payload/RemoteEye), a virtual controller on the console. The screen is
captured by the console itself: holding that controller's Create button saves a screenshot to the gallery,
which is fetched over FTP, decoded and scaled down here.

Console services it uses: elfldr (9021) to start RemoteEye and other payloads, ftpsrv (2121) for screenshots
and files, klogsrv (3232) for the log, and RemoteEye on 3320 + its build tag.

Configuration (environment):
  PS5_IP          console address (required)
  PS5_REMOTEEYE_ELF  path to RemoteEye.elf; default: payload/RemoteEye/out/RemoteEye.elf, then RemoteEye.elf
                     in the repository root (where a release download goes)
  PS5_USER_ID     user id in hex; default: the first folder under /user/home
  PS5_FTP_PORT    ftpsrv port (default 2121)
  PS5_FTP_USER    FTP user (default anonymous)
  PS5_FTP_PASS    FTP password (default anonymous)
  PS5_DELETE_SHOTS  set to 1 to delete each screenshot's files over FTP after fetching it. Off by default:
                    the gallery's database keeps an entry for the deleted file, which then shows as a blank
                    tile in the Media Gallery. Left on, the screenshots pile up there instead.
  PS5_SAVE_DIR    keep the original (full resolution) file of every screenshot in this folder
  PS5_ELFLDR_PORT / PS5_KLOG_PORT / PS5_REMOTEEYE_PORTS  other ports (default 9021 / 3232 / 3321-3340)
"""

from __future__ import annotations

import ftplib
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
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
JXR2PNG = Path(__file__).with_name("jxr2png.ps1")
GALLERY = "/user/av_contents/photo"
THUMBNAILS = "/user/av_contents/thumbnails/photo"
DELETE_SHOTS = os.environ.get("PS5_DELETE_SHOTS") == "1"
SAVE_DIR = Path(os.environ["PS5_SAVE_DIR"]) if os.environ.get("PS5_SAVE_DIR") else None
IMAGE_SUFFIXES = (".jxr", ".png", ".jpg", ".jpeg")
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "ps5-mcp"
INLINE_TEXT_LIMIT = 64 * 1024

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
# RemoteEye build that understands BLOCK (a whole state block), which touch input needs.
BLOCK_TAG = 4

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
        return reply

    def state(self, buttons: int = 0, lx: int = 128, ly: int = 128, rx: int = 128, ry: int = 128,
              l2: int = 0, r2: int = 0) -> None:
        reply = self.call(f"STATE {buttons:x} {lx} {ly} {rx} {ry} {l2} {r2}")
        if not reply.startswith("OK"):
            raise RuntimeError(reply)

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
    takes nothing away from other log readers.
    """

    def __init__(self, keep: int = 20000) -> None:
        self.lines: deque[tuple[float, str]] = deque(maxlen=keep)
        self.guard = threading.Lock()
        self.since: float | None = None
        self.connected = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="klog", daemon=True)
            self.thread.start()

    def _run(self) -> None:
        while True:
            try:
                with socket.create_connection((IP, KLOG_PORT), timeout=5) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    s.settimeout(30)
                    if self.since is None:
                        self.since = time.time()
                    self.connected.set()
                    rest = b""
                    while True:
                        try:
                            chunk = s.recv(65536)
                        except socket.timeout:
                            continue
                        if not chunk:
                            break
                        *done, rest = (rest + chunk).split(b"\n")
                        now = time.time()
                        with self.guard:
                            self.lines.extend((now, line.decode("latin1").rstrip("\r")) for line in done)
            except OSError:
                pass
            self.connected.clear()
            time.sleep(3)

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


def send_elf(path: Path) -> None:
    with socket.create_connection((IP, ELFLDR_PORT), timeout=10) as s:
        s.sendall(path.read_bytes())


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

def _press(bits: int, hold_ms: int, repeat: int, gap_ms: int) -> None:
    for i in range(max(1, repeat)):
        eye.state(bits, l2=255 if bits & BUTTONS["l2"] else 0, r2=255 if bits & BUTTONS["r2"] else 0)
        time.sleep(hold_ms / 1000)
        eye.state(0)
        if i + 1 < repeat:
            time.sleep(gap_ms / 1000)


def _stick(lx: float, ly: float, rx: float, ry: float, duration_ms: int, bits: int) -> None:
    eye.state(bits, axis(lx), axis(ly), axis(rx), axis(ry),
              255 if bits & BUTTONS["l2"] else 0, 255 if bits & BUTTONS["r2"] else 0)
    eye.wait(duration_ms / 1000)
    eye.state(0)


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
    klog.start()
    if not klog.connected.wait(5):
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
        klog.start()
        klog.connected.wait(5)
    start = time.time()
    send_elf(p)
    # It may be a newer RemoteEye: look for the newest instance again on the next call.
    eye.port = None
    text = f"sent {p.name} ({p.stat().st_size} bytes)"
    if log_seconds:
        time.sleep(log_seconds)
        text += "\n" + ("\n".join(klog.read(start)[-400:]) or "(nothing logged)")
    return text


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
    """Which console services answer, and the RemoteEye controller's state."""
    lines = []
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
