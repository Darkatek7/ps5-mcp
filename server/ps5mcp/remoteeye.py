"""The connection to the RemoteEye payload (virtual controller, process list, system calls)."""

from __future__ import annotations

import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from .config import BLOCK_TAG, BUSY_WAIT_S, IP, NEW_PAD_SETTLE_S, REMOTEEYE_ELF, REMOTEEYE_PORTS
from .net import send_elf, user_id

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
        # False from adding a controller until a screenshot works: the console asks "Who's using this
        # controller?" on a new one's first input, and an unanswered prompt leaves Create without effect.
        self.assigned: bool | None = None

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
            end = time.time() + BUSY_WAIT_S
            while kind == "busy" and time.time() < end:
                time.sleep(0.5)
                kind, tag, sock = self.probe(self.port)
            if kind == "busy":
                raise RuntimeError(f"RemoteEye on port {self.port} has been busy with another client (another "
                                   f"session or script) for {BUSY_WAIT_S} s; try again later")
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
            self.assigned = False
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
