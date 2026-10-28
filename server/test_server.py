"""Tests for server.py without a console: a fake RemoteEye, klogsrv, elfldr (running titlectl) and
ShadowMountPlus API on localhost, and an in-memory FTP.

Run with:  python -m unittest server/test_server.py
"""

from __future__ import annotations

import base64
import ftplib
import importlib.util
import json
import os
import random
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

from PIL import Image as PILImage


def listener() -> socket.socket:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(4)
    return s


EYE, KLOG, ELFLDR = listener(), listener(), listener()
DDP = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
DDP.bind(("127.0.0.1", 0))
SMP_ANSWER: dict = {"status": 0}


class FakeSMP(BaseHTTPRequestHandler):
    """ShadowMountPlus's scan endpoint, answering SMP_ANSWER."""

    requests: list[tuple[str, dict]] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeSMP.requests.append((self.path, body))
        data = json.dumps(SMP_ANSWER).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


SMP = ThreadingHTTPServer(("127.0.0.1", 0), FakeSMP)
threading.Thread(target=SMP.serve_forever, daemon=True).start()
os.environ.update({
    "PS5_IP": "127.0.0.1",
    "PS5_USER_ID": "1ea2f4d9",
    "PS5_REMOTEEYE_PORTS": f"{EYE.getsockname()[1]}-{EYE.getsockname()[1]}",
    "PS5_KLOG_PORT": str(KLOG.getsockname()[1]),
    "PS5_ELFLDR_PORT": str(ELFLDR.getsockname()[1]),
    "PS5_SMP_PORT": str(SMP.server_address[1]),
    "PS5_DDP_PORT": str(DDP.getsockname()[1]),
})
# Loaded by path: run from the repository root, "import server" would find the server/ folder instead.
_spec = importlib.util.spec_from_file_location("ps5_server", Path(__file__).with_name("server.py"))
srv = importlib.util.module_from_spec(_spec)
sys.modules["ps5_server"] = srv
_spec.loader.exec_module(srv)
from ps5mcp import config, controls, net, power, screen


class FakeFTP:
    """Just enough of ftplib.FTP over a dict of path -> bytes. Folders are implied by the files in them, plus
    any made with mkd."""

    files: dict[str, bytes] = {}
    dirs: set[str] = set()
    times: dict[str, str] = {}
    clock = 20260101000000

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @classmethod
    def put(cls, path: str, data: bytes) -> None:
        cls.clock += 1
        cls.files[path] = data
        cls.times[path] = str(cls.clock)
        parent = path.rsplit("/", 1)[0]
        while parent:
            cls.times[parent] = str(cls.clock) if parent == path.rsplit("/", 1)[0] else cls.times.get(parent, "0")
            cls.dirs.add(parent)
            parent = parent.rsplit("/", 1)[0]

    def mlsd(self, path):
        path = path.rstrip("/") or "/"
        if path not in self.dirs and path != "/":
            raise ftplib.error_perm("550 not found")
        yield ".", {"type": "cdir"}
        yield "..", {"type": "pdir"}
        prefix = path.rstrip("/") + "/"
        seen = set()
        for p in sorted(self.dirs | set(self.files)):
            if p.startswith(prefix) and "/" not in p[len(prefix):]:
                name = p[len(prefix):]
                if name in seen:
                    continue
                seen.add(name)
                if p in self.files:
                    yield name, {"type": "file", "size": str(len(self.files[p])), "modify": self.times.get(p, "0")}
                else:
                    yield name, {"type": "dir", "modify": self.times.get(p, "0")}

    def retrbinary(self, cmd, callback):
        path = cmd[5:]
        if path not in self.files:
            raise ftplib.error_perm("550 not found")
        callback(self.files[path])

    def storbinary(self, cmd, fh):
        path = cmd[5:]
        if path.rsplit("/", 1)[0] not in self.dirs:
            raise ftplib.error_perm("550 no folder")
        self.put(path, fh.read())

    def delete(self, path):
        del self.files[path]

    def mkd(self, path):
        self.dirs.add(path)

    def rmd(self, path):
        if any(p.startswith(path + "/") for p in set(self.files) | self.dirs):
            raise ftplib.error_perm("550 not empty")
        self.dirs.discard(path)

    def rename(self, a, b):
        self.files[b] = self.files.pop(a)


class FakeEye(threading.Thread):
    """RemoteEye's protocol. Holding Create saves a PNG to the fake gallery, as the console does."""

    def __init__(self, tag: int = 6) -> None:
        super().__init__(daemon=True)
        self.tag = tag
        self.lines: list[str] = []
        self.handle = -1

    def run(self) -> None:
        while True:
            client, _ = EYE.accept()
            with client, client.makefile("rb") as reader:
                for raw in reader:
                    line = raw.decode().strip()
                    self.lines.append(line)
                    client.sendall(self.answer(line).encode() + b"\n")

    def answer(self, line: str) -> str:
        if line == "PING":
            return f"OK PONG {EYE.getsockname()[1]} v{self.tag} remoteeye handle={self.handle}"
        if line.startswith("ATTACH"):
            if self.handle == 7:
                return "OK handle=7 existing"
            self.handle = 7
            return "OK handle=7 new"
        if line.startswith("STATE"):
            if int(line.split()[1], 16) & 1 and SHOT not in FakeFTP.files:
                image = BytesIO()
                PILImage.new("RGB", (1920, 1080), (200, 30, 30)).save(image, "PNG")
                FakeFTP.put(SHOT, image.getvalue())
            return "OK"
        if line.startswith("BLOCK"):
            return "OK" if self.tag >= 4 else "ERR unknown"
        if line == "DETACH":
            self.handle = -1
            return "OK rc=0"
        if line == "PS" and self.tag >= 6:
            return "OK\nP 1 0 3 init wait init\nP 80 1 2 payload.elf - main\nEND"
        if line.startswith("KILL ") and self.tag >= 6:
            return "OK rc=0" if line == "KILL 80" else "ERR not a payload: init"
        command, _, argument = line.partition(" ")
        if self.tag >= 7 and command in ("LAUNCH", "CLOSE", "RUNNING", "URL", "REST", "OFF", "ACCOUNT"):
            # titlectl's report, as the fake elfldr would log it.
            return "OK " + elfldr.report(command.lower(), argument)
        return "ERR unknown"


class FakeKlog(threading.Thread):
    """klogsrv: every connected client gets every line sent."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.clients: list[socket.socket] = []

    def run(self) -> None:
        while True:
            self.clients.append(KLOG.accept()[0])

    def send(self, data: bytes) -> None:
        end = time.time() + 5
        while not self.clients and time.time() < end:
            time.sleep(0.05)
        for client in list(self.clients):
            try:
                client.sendall(data)
            except OSError:  # a port check, as ps5_status makes
                self.clients.remove(client)


class FakeElfldr(threading.Thread):
    """elfldr running titlectl: reads the arguments from the slot and logs the report titlectl would."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.installed = {"PPSA01234", "PPSA05678"}
        self.running: str | None = None
        self.received: list[tuple[str, str]] = []
        self.silent = False  # klogsrv gone quiet: the payload runs, nothing reaches the log

    def run(self) -> None:
        while True:
            client, _ = ELFLDR.accept()
            try:
                with client:
                    data = b"".join(iter(lambda: client.recv(65536), b""))
            except OSError:
                data = b""
            if not data:  # a port check, as ps5_status and ps5_wake make
                continue
            at = data.find(config.TITLECTL_MARKER + b"\0") + len(config.TITLECTL_MARKER) + 1
            action, argument = data[at:].split(b"\0")[:2]
            self.received.append((action.decode(), argument.decode()))
            if self.silent:
                continue
            klog_server.send(f"ps5-mcp:titlectl: {self.report(action.decode(), argument.decode())}\n".encode())

    def report(self, action: str, argument: str) -> str:
        if action == "running":
            return f"running {self.running} app_id=0x8018" if self.running else "running none"
        if action == "launch":
            if argument not in self.installed:
                return f"launch {argument} rc=0x8094000a"
            self.running = argument
            return f"launch {argument} rc=0x8018"
        if action == "close":
            if self.running != argument:
                return f"close {argument} not-running"
            self.running = None
            return f"close {argument} rc=0x0"
        if action == "url":
            return "url rc=0x0"
        if action == "rest":
            ddp_server.state = "rest"
            return "rest rc=0x0"
        if action == "off":
            ddp_server.state = None
            self.off = True
            return "off rc=0x0"
        if action == "account":
            return "account 0x1122334455667788" if argument == "1ea2f4d9" else f"account none user=0x{argument}"
        return f"bad-args '{action}'"


class FakeDDP(threading.Thread):
    """Remote Play discovery: answers SRCH with the power state, and wakes on WAKEUP with the right credential."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.state = "on"
        self.credential = ""
        self.received: list[str] = []

    def run(self) -> None:
        while True:
            data, sender = DDP.recvfrom(4096)
            message = data.decode()
            self.received.append(message)
            if message.startswith("SRCH ") and self.state:
                status = "200 Ok" if self.state == "on" else "620 Server Standby"
                DDP.sendto(f"HTTP/1.1 {status}\nhost-name:PS5-054\nhost-type:PS5\nsystem-version:13000040\n"
                           .encode(), sender)
            elif (message.startswith("WAKEUP ") and self.state
                  and f"user-credential:{self.credential}\n" in message):
                self.state = "on"


SHOT = f"{config.GALLERY}/NPXS40087/NPXS40087/a1/shot.png"
eye_server = FakeEye()
eye_server.start()
klog_server = FakeKlog()
klog_server.start()
elfldr = FakeElfldr()
elfldr.start()
ddp_server = FakeDDP()
ddp_server.start()
net.ftp = FakeFTP


def reset_ftp() -> None:
    FakeFTP.files, FakeFTP.dirs, FakeFTP.times = {}, set(), {}
    # An older screenshot, so the gallery is not empty.
    FakeFTP.put(f"{config.GALLERY}/NPXS40087/NPXS40087/old/old.png", b"x")


class Input(unittest.TestCase):
    def setUp(self):
        reset_ftp()
        eye_server.lines.clear()

    def test_parse_buttons(self):
        self.assertEqual(srv.parse_buttons("cross + l1"), 0x4400)
        with self.assertRaises(ValueError):
            srv.parse_buttons("jump")

    def test_axis(self):
        self.assertEqual((controls.axis(-1), controls.axis(0), controls.axis(1)), (0, 128, 255))

    def test_press(self):
        self.assertEqual(srv.ps5_press("cross", hold_ms=0, repeat=2, gap_ms=0), "pressed cross x2")
        states = [l for l in eye_server.lines if l.startswith("STATE")]
        release = ["STATE 0 128 128 128 128 0 0"] * config.RELEASE_REPORTS
        self.assertEqual(states, (["STATE 4000 128 128 128 128 0 0"] + release) * 2)

    def test_a_held_press_is_reported_again_and_again_like_a_real_controller(self):
        srv.ps5_press("right", hold_ms=100)
        states = [l for l in eye_server.lines if l.startswith("STATE")]
        down = [l for l in states if not l.startswith("STATE 0 ")]
        self.assertGreaterEqual(len(down), 3)
        self.assertTrue(all(l == down[0] for l in down))
        self.assertEqual(states[len(down):], ["STATE 0 128 128 128 128 0 0"] * config.RELEASE_REPORTS)

    def test_a_new_controller_gets_time_before_the_first_input(self):
        eye_server.handle = -1   # RemoteEye adds the controller on this call
        start = time.time()
        srv.ps5_press("cross", hold_ms=0)
        self.assertGreaterEqual(time.time() - start, config.NEW_PAD_SETTLE_S)
        start = time.time()
        srv.ps5_press("cross", hold_ms=0)   # the controller is there now: no wait
        self.assertLess(time.time() - start, config.NEW_PAD_SETTLE_S)

    def test_touch_block(self):
        block = controls.touch_block([(1.0, 0.0)], config.BUTTONS["touchpad"])
        self.assertEqual(len(block), config.STATE_SIZE)
        self.assertEqual(int.from_bytes(block[0xC:0x10], "little"), 0x100000)
        self.assertEqual(block[config.TOUCH_COUNT_AT], 1)
        at = config.TOUCH_POINTS_AT[0]
        self.assertEqual(int.from_bytes(block[at:at + 2], "little"), config.TOUCHPAD_SIZE[0] - 1)

    def test_touch_needs_v4(self):
        eye_server.tag = 3
        try:
            with self.assertRaisesRegex(RuntimeError, "v4"):
                srv.ps5_touch(0.5, 0.5, duration_ms=0)
            # The failed call forgets the instance, so the next one finds a newer build.
            self.assertIsNone(srv.eye.port)
        finally:
            eye_server.tag = 6
        self.assertEqual(srv.ps5_touch(0.5, 0.5, duration_ms=0), "tapped")

    def test_processes(self):
        self.assertEqual(srv.ps5_processes("payload"), "pid ppid state name wait thread\n80 1 2 payload.elf - main")
        self.assertIn("1 0 3 init", srv.ps5_processes())
        self.assertIn("SIGKILL to 80", srv.ps5_kill_payload(80))
        with self.assertRaisesRegex(RuntimeError, "not a payload"):
            srv.ps5_kill_payload(1)

    def test_processes_need_v6(self):
        eye_server.tag = 5
        try:
            with self.assertRaisesRegex(RuntimeError, "v6"):
                srv.ps5_processes()
        finally:
            eye_server.tag = 6

    def test_detach(self):
        self.assertIn("detached", srv.ps5_detach())
        self.assertIn("DETACH", eye_server.lines)


class Screen(unittest.TestCase):
    def setUp(self):
        reset_ftp()

    def test_new_shots_only_lists_changed_folders(self):
        with FakeFTP() as f:
            before = screen.gallery_dirs(f)
            self.assertEqual(screen.new_shots(f, before), {})
            FakeFTP.put(f"{config.GALLERY}/NPXS40087/NPXS40087/old/new.png", b"12345")
            self.assertEqual(screen.new_shots(f, before), {f"{config.GALLERY}/NPXS40087/NPXS40087/old/new.png": 5})

    def test_screenshot(self):
        image = srv.ps5_screenshot(max_width=640)
        decoded = PILImage.open(BytesIO(image.data))
        self.assertEqual(decoded.size, (640, 360))

    def test_crop(self):
        raw = BytesIO()
        picture = PILImage.new("RGB", (100, 100), (0, 0, 255))
        picture.paste((255, 0, 0), (50, 0, 100, 50))
        picture.save(raw, "PNG")
        cropped = PILImage.open(BytesIO(screen.decode(raw.getvalue(), ".png", 1280, (0.5, 0, 1, 0.5))))
        self.assertEqual(cropped.size, (50, 50))
        r, g, b = cropped.getpixel((25, 25))
        self.assertGreater(r, 200)
        self.assertLess(b, 50)

    def test_sequence(self):
        out = srv.ps5_sequence([srv.Step(action="press", buttons="down", hold_ms=0),
                                srv.Step(action="wait", duration_ms=10),
                                srv.Step(action="screenshot", max_width=320),
                                srv.Step(action="press", buttons="cross", hold_ms=0)])
        self.assertEqual(len(out), 3)
        self.assertIn("1. pressed down", out[0])
        self.assertIsInstance(out[1], srv.Image)
        self.assertIn("4. pressed cross", out[2])

    def test_sequence_limits(self):
        with self.assertRaises(ValueError):
            srv.ps5_sequence([srv.Step(action="wait", duration_ms=200_000)])
        with self.assertRaises(ValueError):
            srv.ps5_sequence([srv.Step(action="press", buttons="jump")])


class Files(unittest.TestCase):
    def setUp(self):
        reset_ftp()
        FakeFTP.dirs.add("/data")

    def test_upload_download_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "app")
            (src / "sce_sys").mkdir(parents=True)
            (src / "eboot.bin").write_bytes(b"\0elf")
            (src / "sce_sys" / "param.json").write_text("{}")
            self.assertIn("2 file(s)", srv.ps5_upload(str(src), "/data/homebrew/"))
            self.assertEqual(FakeFTP.files["/data/homebrew/app/eboot.bin"], b"\0elf")
            self.assertIn("param.json", srv.ps5_ls("/data/homebrew/app/sce_sys"))

            self.assertEqual(srv.ps5_download("/data/homebrew/app/sce_sys/param.json"), "{}")
            saved = srv.ps5_download("/data/homebrew/app/eboot.bin", tmp + "/out.bin")
            self.assertIn("saved 4 bytes", saved)
            self.assertIn("2 file(s)", srv.ps5_download("/data/homebrew/app", tmp + "/copy"))
            self.assertEqual(Path(tmp, "copy", "sce_sys", "param.json").read_text(), "{}")

        with self.assertRaises(ValueError):
            srv.ps5_delete("/data/homebrew/app")
        self.assertIn("2 files", srv.ps5_delete("/data/homebrew/app", recursive=True))
        self.assertFalse(any(p.startswith("/data/homebrew/app") for p in FakeFTP.files))
        with self.assertRaises(ValueError):
            srv.ps5_delete("/data", recursive=True)


class Klog(unittest.TestCase):
    def test_since(self):
        srv.klog.start()
        self.assertTrue(srv.klog.connected.wait(5))
        klog_server.send(b"early line\n")
        time.sleep(0.3)
        klog_server.send(b"sp:late line\npartial")
        time.sleep(0.3)
        text = srv.ps5_klog(seconds=0, since_s=10)
        self.assertIn("early line", text)
        self.assertIn("sp:late line", text)
        self.assertNotIn("partial", text)
        # The reader started less than 10 s ago, which the answer says first.
        self.assertTrue(text.startswith("(the log is kept since"))
        self.assertTrue(srv.ps5_klog(seconds=0, since_s=10, filter="sp:").endswith("\nsp:late line"))
        self.assertEqual(srv.ps5_klog(seconds=0), "(nothing logged)")
        klog_server.send(b"\n")


def param_json(name: str) -> bytes:
    return json.dumps({"localizedParameters": {"defaultLanguage": "en-US", "en-US": {"titleName": name}}}).encode()


class Titles(unittest.TestCase):
    def setUp(self):
        reset_ftp()
        srv._names.clear()
        elfldr.running = None
        elfldr.received.clear()
        FakeFTP.put("/user/app/PPSA01234/app.pkg", b"x")
        FakeFTP.put("/user/appmeta/PPSA01234/param.json", param_json("Sample Game"))
        FakeFTP.put("/user/app/PPSA05678/sce_sys/param.json", param_json("Homebrew App") + b"\0")
        tmp = tempfile.NamedTemporaryFile(suffix=".elf", delete=False)
        tmp.write(b"\x7fELF" + b"code" + config.TITLECTL_MARKER + b"\0" * 1025 + b"tail")
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        config.TITLECTL_ELF = Path(tmp.name)

    def test_titles(self):
        self.assertEqual(srv.ps5_titles(), "PPSA01234  Sample Game\nPPSA05678  Homebrew App")
        self.assertEqual(srv.ps5_titles("homebrew"), "PPSA05678  Homebrew App")

    def test_launch_running_close(self):
        self.assertEqual(srv.ps5_running(), "no app is running in front (home screen)")
        self.assertEqual(srv.ps5_launch("ppsa01234"), "started PPSA01234 (app id 0x8018)")
        self.assertEqual(elfldr.received[-1], ("launch", "PPSA01234"))
        self.assertEqual(srv.ps5_running(), "PPSA01234 Sample Game (app id 0x8018)")
        self.assertEqual(srv.ps5_close("PPSA05678"), "PPSA05678 is not running")
        self.assertEqual(srv.ps5_close("PPSA01234"), "closed PPSA01234")
        self.assertIsNone(srv.running_title())

    def test_remoteeye_7_answers_without_titlectl(self):
        eye_server.tag = 7
        eye_server.lines.clear()
        try:
            self.assertEqual(srv.ps5_launch("PPSA01234"), "started PPSA01234 (app id 0x8018)")
            self.assertEqual(srv.ps5_running(), "PPSA01234 Sample Game (app id 0x8018)")
            self.assertEqual(srv.ps5_close("PPSA01234"), "closed PPSA01234")
            self.assertEqual(srv.ps5_open_url("https://example.com/"), "opened https://example.com/")
        finally:
            eye_server.tag = 6
        self.assertEqual(elfldr.received, [])  # no titlectl sent
        self.assertIn("LAUNCH PPSA01234", eye_server.lines)
        self.assertIn("RUNNING", eye_server.lines)

    def test_launch_errors(self):
        with self.assertRaisesRegex(RuntimeError, "0x8094000a"):
            srv.ps5_launch("PPSA00000")
        with self.assertRaises(ValueError):
            srv.ps5_launch("eboot.bin")
        self.assertEqual(elfldr.received, [("launch", "PPSA00000")])

    def test_close_with_quit_file(self):
        FakeFTP.dirs.add("/data/PS5SD")
        elfldr.running = "PPSA05678"

        def app():
            while "/data/PS5SD/quit" not in FakeFTP.files:
                time.sleep(0.05)
            del FakeFTP.files["/data/PS5SD/quit"]
            elfldr.running = None

        threading.Thread(target=app, daemon=True).start()
        self.assertEqual(srv.ps5_close("PPSA05678", quit_file="/data/PS5SD/quit", timeout_s=10),
                         "PPSA05678 took /data/PS5SD/quit and has closed")
        self.assertNotIn(("close", "PPSA05678"), elfldr.received)

    def test_quit_file_not_taken(self):
        FakeFTP.dirs.add("/data/PS5SD")
        with self.assertRaisesRegex(RuntimeError, "did not take"):
            srv.ps5_close("PPSA05678", quit_file="/data/PS5SD/quit", timeout_s=1)
        self.assertNotIn("/data/PS5SD/quit", FakeFTP.files)

    def test_open_url(self):
        self.assertEqual(srv.ps5_open_url("https://example.com/a?b=1"), "opened https://example.com/a?b=1")
        self.assertEqual(elfldr.received, [("url", "https://example.com/a?b=1")])
        with self.assertRaises(ValueError):
            srv.ps5_open_url("file:///etc/passwd")

    def test_argument_too_long(self):
        with self.assertRaises(ValueError):
            srv.titlectl("url", "https://example.com/" + "a" * 1100)

    def test_rescan(self):
        global SMP_ANSWER
        SMP_ANSWER = {"status": 0}
        self.assertTrue(srv.ps5_rescan().startswith("scan started"))
        self.assertEqual(FakeSMP.requests[-1], ("/api/v1/scan", {"reset_attempts": True}))
        SMP_ANSWER = {"status": 0, "scan_queued": True, "scan_deferred": True,
                      "scan_deferred_reason": "runtime_prepared"}
        self.assertTrue(srv.ps5_rescan().startswith(
            "ShadowMountPlus deferred the scan (runtime_prepared), but queued it."))
        SMP_ANSWER = {"status": 0, "result": {"scan_deferred": True, "game_active": True}}
        self.assertTrue(srv.ps5_rescan().startswith("ShadowMountPlus deferred the scan. It defers"))
        SMP_ANSWER = {"status": 1}
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            srv.ps5_rescan()


class FakeRegistration(threading.Thread):
    """The console's Remote Play registration on one port number, UDP and TCP: answers SRC3 while a PIN is
    shown, and a registration with that PIN and the expected account id with an encrypted RegistKey."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        # One number for both. With Docker or Hyper-V, Windows reserves wide ranges of the dynamic ports
        # (49152 and up) per protocol, so the number is picked below them.
        for _ in range(200):
            self.port = random.randint(20000, 40000)
            self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.tcp = socket.socket()
            try:
                self.udp.bind(("127.0.0.1", self.port))
                self.tcp.bind(("127.0.0.1", self.port))
                self.tcp.listen(4)
                break
            except OSError:
                self.tcp.close()
                self.udp.close()
        else:
            raise RuntimeError("no port free for both UDP and TCP")
        self.pin: str | None = None
        self.account = base64.b64encode((0x1122334455667788).to_bytes(8, "little")).decode()
        threading.Thread(target=self.search, daemon=True).start()

    def search(self) -> None:
        while True:
            data, sender = self.udp.recvfrom(64)
            if data == b"SRC3" and self.pin:
                self.udp.sendto(b"RES3", sender)

    def run(self) -> None:
        while True:
            client, _ = self.tcp.accept()
            with client:
                data = b""
                while b"\r\n\r\n" not in data:
                    data += client.recv(4096)
                head, _, body = data.partition(b"\r\n\r\n")
                length = int(re.search(rb"Content-Length: (\d+)", head).group(1))
                while len(body) < length:
                    body += client.recv(4096)
                client.sendall(self.answer(body))

    def answer(self, body: bytes) -> bytes:
        # Undo the nonce hiding (key 1 is the nonce mixed with RP_REG_KEY_1), as the console does.
        key_1 = body[401:409] + body[199:207]
        nonce = bytes((((key_1[i] + 45 - i) % 256) ^ power.RP_REG_KEY_1[i]) for i in range(16))
        key_0 = bytearray(power.RP_REG_KEY_0)
        for i, byte in enumerate(int(self.pin).to_bytes(4, "big")):
            key_0[12 + i] ^= byte
        inner = power._rp_cipher(bytes(key_0), nonce, False).update(body[480:])
        if f"Np-AccountId: {self.account}\r\n".encode() not in inner:
            return b"HTTP/1.1 403 Forbidden\r\n\r\n"
        reply = power._rp_cipher(bytes(key_0), nonce, True).update(
            b"PS5-Mac: B41F4D2FFF33\r\nPS5-RegistKey: " + b"0a1b2c3d".hex().encode()
            + b"\r\nPS5-Nickname: PS5-054\r\n")
        return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(reply) + reply


registration = FakeRegistration()
registration.start()


class Power(unittest.TestCase):
    # A RegistKey as pyremoteplay stores it: hex of the ASCII hex string "0a1b2c3d".
    KEY = b"0a1b2c3d".hex()

    def setUp(self):
        ddp_server.state = "on"
        ddp_server.credential = str(0x0A1B2C3D)
        ddp_server.received.clear()
        elfldr.received.clear()
        tmp = tempfile.NamedTemporaryFile(suffix=".elf", delete=False)
        tmp.write(b"\x7fELF" + config.TITLECTL_MARKER + b"\0" * 1025)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        config.TITLECTL_ELF = Path(tmp.name)
        self.addCleanup(setattr, config, "REGIST_KEY", config.REGIST_KEY)
        self.addCleanup(setattr, config, "REGIST_FILE", config.REGIST_FILE)
        self.addCleanup(setattr, power, "RP_PORT", power.RP_PORT)
        config.REGIST_FILE = Path(tempfile.mkdtemp()) / "regist.json"
        power.RP_PORT = registration.port
        registration.pin = None

    def test_ddp_message(self):
        self.assertEqual(srv.ddp("WAKEUP", {"user-credential": "1"}),
                         b"WAKEUP * HTTP/1.1\nuser-credential:1\ndevice-discovery-protocol-version:00030010\n")

    def test_wake_credential(self):
        self.assertEqual(srv.wake_credential(self.KEY), str(0x0A1B2C3D))
        with self.assertRaises(ValueError):
            srv.wake_credential("not a key")

    def test_power_state(self):
        self.assertEqual(srv.power_state(), "on")
        ddp_server.state = "rest"
        self.assertEqual(srv.power_state(), "rest")
        ddp_server.state = None
        self.assertIsNone(srv.power_state(timeout=0.3))

    def test_rest(self):
        self.assertEqual(srv.ps5_rest(), "the console is in rest mode")
        self.assertEqual(elfldr.received, [("rest", "")])
        self.assertTrue(srv.ps5_status().startswith("power: rest mode\n"))

    def test_selftest(self):
        FakeFTP.dirs.add("/user/home/1ea2f4d9")
        FakeFTP.dirs.add("/user/app")
        config.REGIST_KEY = ""
        # FTP is faked in-process; any listening port stands in for ftpsrv's.
        self.addCleanup(setattr, config, "FTP_PORT", config.FTP_PORT)
        config.FTP_PORT = config.KLOG_PORT
        report = srv.ps5_selftest()
        self.assertTrue(report.endswith("all checks passed"), report)
        self.assertIn("ok    console: PS5-054 at 127.0.0.1, firmware 13.00, on", report)
        self.assertIn("ok    title calls: running none", report)
        self.assertIn("none yet (ps5_pair", report)

    def test_parse_ddp_and_firmware(self):
        fields = config.parse_ddp(b"HTTP/1.1 620 Server Standby\nhost-name:PS5-054\nsystem-version:13000040\n")
        self.assertEqual((fields["status"], fields["host-name"]), ("620", "PS5-054"))
        self.assertEqual(srv.firmware(fields["system-version"]), "13.00")
        self.assertEqual(srv.firmware(""), "?")

    def test_outdated_titlectl(self):
        with self.assertRaisesRegex(RuntimeError, "older than this server"):
            srv.titlectl("nonsense")

    def test_power_text_remembers_rest(self):
        power._power_seen[0] = None
        ddp_server.state = None
        self.assertEqual(srv.power_text(), "no answer (off, or not on the network)")
        ddp_server.state = "rest"
        self.assertEqual(srv.power_text(), "rest mode")
        ddp_server.state = None
        self.assertTrue(srv.power_text().startswith("no answer (it was resting"))

    def test_power_off_needs_confirm(self):
        with self.assertRaisesRegex(ValueError, "confirm=true"):
            srv.ps5_power_off()
        self.assertEqual(elfldr.received, [])

    def test_power_off(self):
        elfldr.off = False
        # The fake elfldr keeps listening; a console that is off does not.
        real = net.elfldr_answers
        net.elfldr_answers = lambda: not elfldr.off
        self.addCleanup(setattr, net, "elfldr_answers", real)
        self.assertTrue(srv.ps5_power_off(confirm=True).startswith("the console is turning off"))
        self.assertEqual(elfldr.received, [("off", "")])

    def test_klog_connects_again_after_rest(self):
        self.assertTrue(srv.klog.ready())
        before = srv.klog.sock
        self.assertTrue(srv.klog.ready())
        self.assertIs(srv.klog.sock, before)
        ddp_server.state = "rest"
        self.assertTrue(srv.klog.ready())
        self.assertIsNot(srv.klog.sock, before)
        self.assertEqual(srv.klog.power, "rest")
        # Rested and woke between two calls: the reader sees it on its own.
        before = srv.klog.sock
        ddp_server.state = "on"
        time.sleep(config.POWER_CHECK_S + 2)
        self.assertTrue(srv.klog.connected.is_set())
        self.assertIsNot(srv.klog.sock, before)
        self.assertEqual(srv.klog.power, "on")

    def test_pair_saves_the_key_and_wake_uses_it(self):
        config.REGIST_KEY = ""
        registration.pin = "13589201"
        self.assertTrue(srv.ps5_pair("1358 9201").startswith("paired as Remote Play device of PS5-054"))
        self.assertEqual(elfldr.received, [("account", "1ea2f4d9")])
        saved = json.loads(config.REGIST_FILE.read_text())
        self.assertEqual(saved["127.0.0.1"]["PS5-RegistKey"], self.KEY)
        ddp_server.state = "rest"
        self.assertTrue(srv.ps5_wake(timeout_s=10).startswith("the console is on"))

    def test_pair_errors(self):
        with self.assertRaisesRegex(ValueError, "8 digits"):
            srv.ps5_pair("1234")
        with self.assertRaisesRegex(RuntimeError, "not waiting"):
            srv.ps5_pair("13589201")
        registration.pin = "13589201"
        registration.account, account = "AAAAAAAAAAA=", registration.account
        try:
            with self.assertRaisesRegex(RuntimeError, "403"):
                srv.ps5_pair("13589201")
        finally:
            registration.account = account
        self.assertFalse(config.REGIST_FILE.exists())

    def test_klog_reader_stops_when_idle(self):
        self.assertTrue(srv.klog.ready())
        self.addCleanup(setattr, config, "KLOG_IDLE_S", config.KLOG_IDLE_S)
        config.KLOG_IDLE_S = 0.5
        srv.klog.thread.join(5)
        self.assertFalse(srv.klog.thread.is_alive())
        self.assertFalse(srv.klog.connected.is_set())
        config.KLOG_IDLE_S = 600
        self.assertTrue(srv.klog.ready())

    def test_klog_connects_again_when_klogsrv_goes_quiet(self):
        self.assertTrue(srv.klog.ready())
        before = srv.klog.sock
        srv.klog.last = 0
        self.assertTrue(srv.klog.ready())
        self.assertIsNot(srv.klog.sock, before)
        before = srv.klog.sock
        elfldr.silent = True
        try:
            with self.assertRaisesRegex(RuntimeError, "klogsrv sent nothing"):
                srv.titlectl("running", timeout=1)
        finally:
            elfldr.silent = False
        self.assertTrue(srv.klog.ready())
        self.assertIsNot(srv.klog.sock, before)

    def test_wake_needs_key(self):
        config.REGIST_KEY = ""
        ddp_server.state = "rest"
        with self.assertRaisesRegex(RuntimeError, "PS5_REGIST_KEY"):
            srv.ps5_wake()

    def test_wake(self):
        config.REGIST_KEY = self.KEY
        self.assertEqual(srv.ps5_wake(), "the console is already on")
        ddp_server.state = "rest"
        self.assertTrue(srv.ps5_wake(timeout_s=10).startswith("the console is on"))
        self.assertTrue(any(m.startswith("WAKEUP ") for m in ddp_server.received))

    def test_wake_with_wrong_key(self):
        config.REGIST_KEY = b"99999999".hex()
        ddp_server.state = "rest"
        with self.assertRaisesRegex(RuntimeError, "did not wake"):
            srv.ps5_wake(timeout_s=2)

    def test_wake_when_the_network_sleeps(self):
        config.REGIST_KEY = self.KEY
        ddp_server.state = None
        with self.assertRaisesRegex(RuntimeError, "Stay Connected to the Internet"):
            srv.ps5_wake(timeout_s=2)


if __name__ == "__main__":
    unittest.main()
