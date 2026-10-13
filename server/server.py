"""MCP server that lets Claude see the PS5 screen and press its buttons.

Input goes through the RemoteEye payload (payload/RemoteEye), a virtual controller on the console. The screen is
captured by the console itself: holding that controller's Create button saves a screenshot to the gallery,
which is fetched over FTP, decoded and scaled down here.

Console services it uses: elfldr (9021) to start RemoteEye, ftpsrv (2121) for the screenshots, klogsrv
(3232) for the log, and RemoteEye on 3320 + its build tag.

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
"""

from __future__ import annotations

import ftplib
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image
from PIL import Image as PILImage

IP = os.environ.get("PS5_IP", "")
if not IP:
    raise SystemExit("ps5-mcp: set PS5_IP to the console's address (see README)")
FTP_PORT = int(os.environ.get("PS5_FTP_PORT", "2121"))
FTP_USER = os.environ.get("PS5_FTP_USER", "anonymous")
FTP_PASS = os.environ.get("PS5_FTP_PASS", "anonymous")
ELFLDR_PORT = 9021
KLOG_PORT = 3232
REMOTEEYE_PORTS = range(3321, 3341)
REPO = Path(__file__).resolve().parents[1]
REMOTEEYE_ELF = Path(os.environ["PS5_REMOTEEYE_ELF"]) if os.environ.get("PS5_REMOTEEYE_ELF") else next(
    (p for p in (REPO / "payload" / "RemoteEye" / "out" / "RemoteEye.elf", REPO / "RemoteEye.elf") if p.exists()),
    REPO / "RemoteEye.elf")
JXR2PNG = Path(__file__).with_name("jxr2png.ps1")
GALLERY = "/user/av_contents/photo"
THUMBNAILS = "/user/av_contents/thumbnails/photo"
DELETE_SHOTS = os.environ.get("PS5_DELETE_SHOTS") == "1"
IMAGE_SUFFIXES = (".jxr", ".png", ".jpg", ".jpeg")

# ScePadButton bits. Create is the bit the system answers with a screenshot when it is held.
BUTTONS = {
    "create": 0x1, "share": 0x1,
    "l3": 0x2, "r3": 0x4, "options": 0x8,
    "up": 0x10, "right": 0x20, "down": 0x40, "left": 0x80,
    "l2": 0x100, "r2": 0x200, "l1": 0x400, "r1": 0x800,
    "triangle": 0x1000, "circle": 0x2000, "cross": 0x4000, "x": 0x4000, "square": 0x8000,
    "ps": 0x10000, "touchpad": 0x100000,
}

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
        self.sock: socket.socket | None = None
        self.reader = None

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

    def _locate(self) -> socket.socket:
        best: tuple[int, int, socket.socket] | None = None
        busy = []
        for port in REMOTEEYE_PORTS:
            kind, tag, s = self.probe(port)
            if kind == "ok":
                if best is None or tag > best[0]:
                    if best:
                        best[2].close()
                    best = (tag, port, s)
                else:
                    s.close()
            elif kind == "busy":
                busy.append(port)
        if best:
            self.port = best[1]
            return best[2]
        if busy:
            raise RuntimeError(f"RemoteEye on port(s) {busy} is busy with another client "
                               "(another session or script); try again in a minute")
        if not REMOTEEYE_ELF.exists():
            raise RuntimeError(f"RemoteEye is not running and there is no {REMOTEEYE_ELF} to start it with "
                               "(download it from the releases page, or build it: payload/build.ps1)")
        send_elf(REMOTEEYE_ELF)
        for _ in range(20):
            time.sleep(0.5)
            for port in REMOTEEYE_PORTS:
                kind, _, s = self.probe(port)
                if kind == "ok":
                    self.port = port
                    return s
        raise RuntimeError("RemoteEye was sent to elfldr but did not answer")

    @contextmanager
    def session(self):
        """Holds one connection for the duration of a tool call."""
        sock = None
        if self.port is not None:
            kind, _, sock = self.probe(self.port)
            if kind == "busy":
                raise RuntimeError(f"RemoteEye on port {self.port} is busy with another client; try again shortly")
        if sock is None:
            sock = self._locate()
        self.sock, self.reader = sock, sock.makefile("rb")
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


eye = RemoteEye()
_user_id: int | None = None


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


def list_files(f: ftplib.FTP, root: str, depth: int = 0) -> dict[str, int]:
    """Every file under root with its size: one MLSD per directory."""
    out: dict[str, int] = {}
    try:
        entries = list(f.mlsd(root))
    except ftplib.error_perm:
        return out
    for name, facts in entries:
        path = root.rstrip("/") + "/" + name
        kind = facts.get("type")
        if kind == "dir" and depth < 6:
            out.update(list_files(f, path, depth + 1))
        elif kind == "file":
            out[path] = int(facts.get("size", 0))
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


def decode(raw: bytes, suffix: str, max_width: int) -> bytes:
    if suffix == ".jxr":
        # JPEG XR (what the console saves with HDR output): decoded by the Windows Imaging Component.
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp, "shot.jxr"), Path(tmp, "shot.png")
            src.write_bytes(raw)
            subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(JXR2PNG),
                            "-In", str(src), "-Out", str(dst), "-MaxWidth", str(max_width)],
                           check=True, capture_output=True, timeout=60)
            image = PILImage.open(dst)
            image.load()
    else:
        image = PILImage.open(BytesIO(raw))
        if image.width > max_width:
            image = image.resize((max_width, round(image.height * max_width / image.width)))
    out = BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=85)
    return out.getvalue()


@mcp.tool()
def ps5_screenshot(max_width: int = 1280) -> Image:
    """Capture the PS5 screen as it is right now (the whole composited screen, system UI included).

    Works by holding the virtual controller's Create button, which saves a screenshot to the console's
    gallery, from where it is fetched over FTP. Takes about 5 seconds.
    """
    with _lock, eye.session():
        eye.attach()
        with ftp() as f:
            before = set(list_files(f, GALLERY))
            end = time.time() + 1.6
            while time.time() < end:
                eye.state(BUTTONS["create"])
                time.sleep(0.1)
            eye.state(0)

            shot, deadline, last_size = None, time.time() + 20, -1
            while time.time() < deadline:
                time.sleep(0.7)
                new = {p: size for p, size in list_files(f, GALLERY).items() if p not in before}
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
                stem = Path(shot).stem
                created = [p for p in list_files(f, GALLERY) if p not in before and Path(p).name.startswith(stem)]
                created += [p for p in list_files(f, THUMBNAILS) if Path(p).name.startswith(stem)]
                for p in created:
                    try:
                        f.delete(p)
                    except ftplib.error_perm:
                        pass

    return Image(data=decode(bytes(data), Path(shot).suffix.lower(), max_width), format="jpeg")


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
        for i in range(max(1, repeat)):
            eye.state(bits, l2=255 if bits & BUTTONS["l2"] else 0, r2=255 if bits & BUTTONS["r2"] else 0)
            time.sleep(hold_ms / 1000)
            eye.state(0)
            if i + 1 < repeat:
                time.sleep(gap_ms / 1000)
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
        eye.state(bits, axis(left_x), axis(left_y), axis(right_x), axis(right_y))
        time.sleep(duration_ms / 1000)
        eye.state(0)
    return "done"


@mcp.tool()
def ps5_klog(seconds: float = 3, filter: str = "") -> str:
    """Read the console's kernel log (klogsrv) for a few seconds; optionally keep only lines containing filter.

    Only lines logged while this runs are seen. Payloads log with the 'sp:' prefix, and app crashes show up as
    coredump lines.
    """
    data = bytearray()
    with socket.create_connection((IP, KLOG_PORT), timeout=5) as s:
        s.settimeout(0.5)
        end = time.time() + seconds
        while time.time() < end:
            try:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
            except socket.timeout:
                pass
    lines = data.decode("latin1").splitlines()
    if filter:
        lines = [l for l in lines if filter in l]
    return "\n".join(lines[-400:]) or "(nothing logged)"


@mcp.tool()
def ps5_send_payload(path: str) -> str:
    """Send an ELF payload to the console's loader (elfldr, port 9021). The loader does not report whether it
    ran; check the payload's own port or ps5_klog."""
    p = Path(path).resolve()
    send_elf(p)
    return f"sent {p.name} ({p.stat().st_size} bytes)"


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
    with _lock:
        # Older instances keep running (a payload can't be unloaded); the server uses the newest.
        newest, busy = None, []
        for port in REMOTEEYE_PORTS:
            kind, tag, sock = eye.probe(port)
            if kind == "ok":
                sock.sendall(b"PING\n")
                reply = sock.makefile("rb").readline().decode().strip()
                sock.close()
                if newest is None or tag > newest[0]:
                    newest = (tag, reply)
            elif kind == "busy":
                busy.append(port)
        if newest:
            lines.append("remoteeye: " + newest[1])
        if busy:
            lines.append(f"remoteeye: port(s) {busy} busy with another client")
        if not newest and not busy:
            lines.append("remoteeye: not running (it is started on first use)")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
