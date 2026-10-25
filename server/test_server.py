"""Tests for server.py without a console: a fake RemoteEye, klogsrv, elfldr (running titlectl) and
ShadowMountPlus API on localhost, and an in-memory FTP.

Run with:  python -m unittest server/test_server.py
"""

from __future__ import annotations

import ftplib
import importlib.util
import json
import os
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
})
# Loaded by path: run from the repository root, "import server" would find the server/ folder instead.
_spec = importlib.util.spec_from_file_location("ps5_server", Path(__file__).with_name("server.py"))
srv = importlib.util.module_from_spec(_spec)
sys.modules["ps5_server"] = srv
_spec.loader.exec_module(srv)


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
        for client in self.clients:
            client.sendall(data)


class FakeElfldr(threading.Thread):
    """elfldr running titlectl: reads the arguments from the slot and logs the report titlectl would."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.installed = {"PPSA01234", "PPSA05678"}
        self.running: str | None = None
        self.received: list[tuple[str, str]] = []

    def run(self) -> None:
        while True:
            client, _ = ELFLDR.accept()
            with client:
                data = b"".join(iter(lambda: client.recv(65536), b""))
            at = data.find(srv.TITLECTL_MARKER + b"\0") + len(srv.TITLECTL_MARKER) + 1
            action, argument = data[at:].split(b"\0")[:2]
            self.received.append((action.decode(), argument.decode()))
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
        return f"bad-args '{action}'"


SHOT = f"{srv.GALLERY}/NPXS40087/NPXS40087/a1/shot.png"
eye_server = FakeEye()
eye_server.start()
klog_server = FakeKlog()
klog_server.start()
elfldr = FakeElfldr()
elfldr.start()
srv.ftp = FakeFTP


def reset_ftp() -> None:
    FakeFTP.files, FakeFTP.dirs, FakeFTP.times = {}, set(), {}
    # An older screenshot, so the gallery is not empty.
    FakeFTP.put(f"{srv.GALLERY}/NPXS40087/NPXS40087/old/old.png", b"x")


class Input(unittest.TestCase):
    def setUp(self):
        reset_ftp()
        eye_server.lines.clear()

    def test_parse_buttons(self):
        self.assertEqual(srv.parse_buttons("cross + l1"), 0x4400)
        with self.assertRaises(ValueError):
            srv.parse_buttons("jump")

    def test_axis(self):
        self.assertEqual((srv.axis(-1), srv.axis(0), srv.axis(1)), (0, 128, 255))

    def test_press(self):
        self.assertEqual(srv.ps5_press("cross", hold_ms=0, repeat=2, gap_ms=0), "pressed cross x2")
        states = [l for l in eye_server.lines if l.startswith("STATE")]
        release = ["STATE 0 128 128 128 128 0 0"] * srv.RELEASE_REPORTS
        self.assertEqual(states, (["STATE 4000 128 128 128 128 0 0"] + release) * 2)

    def test_a_held_press_is_reported_again_and_again_like_a_real_controller(self):
        srv.ps5_press("right", hold_ms=100)
        states = [l for l in eye_server.lines if l.startswith("STATE")]
        down = [l for l in states if not l.startswith("STATE 0 ")]
        self.assertGreaterEqual(len(down), 3)
        self.assertTrue(all(l == down[0] for l in down))
        self.assertEqual(states[len(down):], ["STATE 0 128 128 128 128 0 0"] * srv.RELEASE_REPORTS)

    def test_a_new_controller_gets_time_before_the_first_input(self):
        eye_server.handle = -1   # RemoteEye adds the controller on this call
        start = time.time()
        srv.ps5_press("cross", hold_ms=0)
        self.assertGreaterEqual(time.time() - start, srv.NEW_PAD_SETTLE_S)
        start = time.time()
        srv.ps5_press("cross", hold_ms=0)   # the controller is there now: no wait
        self.assertLess(time.time() - start, srv.NEW_PAD_SETTLE_S)

    def test_touch_block(self):
        block = srv.touch_block([(1.0, 0.0)], srv.BUTTONS["touchpad"])
        self.assertEqual(len(block), srv.STATE_SIZE)
        self.assertEqual(int.from_bytes(block[0xC:0x10], "little"), 0x100000)
        self.assertEqual(block[srv.TOUCH_COUNT_AT], 1)
        at = srv.TOUCH_POINTS_AT[0]
        self.assertEqual(int.from_bytes(block[at:at + 2], "little"), srv.TOUCHPAD_SIZE[0] - 1)

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
            before = srv.gallery_dirs(f)
            self.assertEqual(srv.new_shots(f, before), {})
            FakeFTP.put(f"{srv.GALLERY}/NPXS40087/NPXS40087/old/new.png", b"12345")
            self.assertEqual(srv.new_shots(f, before), {f"{srv.GALLERY}/NPXS40087/NPXS40087/old/new.png": 5})

    def test_screenshot(self):
        image = srv.ps5_screenshot(max_width=640)
        decoded = PILImage.open(BytesIO(image.data))
        self.assertEqual(decoded.size, (640, 360))

    def test_crop(self):
        raw = BytesIO()
        picture = PILImage.new("RGB", (100, 100), (0, 0, 255))
        picture.paste((255, 0, 0), (50, 0, 100, 50))
        picture.save(raw, "PNG")
        cropped = PILImage.open(BytesIO(srv.decode(raw.getvalue(), ".png", 1280, (0.5, 0, 1, 0.5))))
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
        tmp.write(b"\x7fELF" + b"code" + srv.TITLECTL_MARKER + b"\0" * 1025 + b"tail")
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        srv.TITLECTL_ELF = Path(tmp.name)

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


if __name__ == "__main__":
    unittest.main()
