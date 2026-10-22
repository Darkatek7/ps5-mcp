"""Tests for server.py without a console: a fake RemoteEye and klogsrv on localhost, and an in-memory FTP.

Run with:  python -m unittest server/test_server.py
"""

from __future__ import annotations

import ftplib
import importlib.util
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
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
os.environ.update({
    "PS5_IP": "127.0.0.1",
    "PS5_USER_ID": "1ea2f4d9",
    "PS5_REMOTEEYE_PORTS": f"{EYE.getsockname()[1]}-{EYE.getsockname()[1]}",
    "PS5_KLOG_PORT": str(KLOG.getsockname()[1]),
    "PS5_ELFLDR_PORT": str(ELFLDR.getsockname()[1]),
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

    def __init__(self, tag: int = 4) -> None:
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
        return "ERR unknown"


SHOT = f"{srv.GALLERY}/NPXS40087/NPXS40087/a1/shot.png"
eye_server = FakeEye()
eye_server.start()
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
        self.assertEqual(states, ["STATE 4000 128 128 128 128 0 0", "STATE 0 128 128 128 128 0 0"] * 2)

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
            eye_server.tag = 4
        self.assertEqual(srv.ps5_touch(0.5, 0.5, duration_ms=0), "tapped")

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
        def serve():
            client, _ = KLOG.accept()
            client.sendall(b"early line\n")
            time.sleep(0.3)
            client.sendall(b"sp:late line\npartial")
            time.sleep(5)
            client.close()

        threading.Thread(target=serve, daemon=True).start()
        srv.klog.start()
        self.assertTrue(srv.klog.connected.wait(5))
        time.sleep(0.6)
        text = srv.ps5_klog(seconds=0, since_s=10)
        self.assertIn("early line", text)
        self.assertIn("sp:late line", text)
        self.assertNotIn("partial", text)
        # The reader started less than 10 s ago, which the answer says first.
        self.assertTrue(text.startswith("(the log is kept since"))
        self.assertTrue(srv.ps5_klog(seconds=0, since_s=10, filter="sp:").endswith("\nsp:late line"))
        self.assertEqual(srv.ps5_klog(seconds=0), "(nothing logged)")


if __name__ == "__main__":
    unittest.main()
