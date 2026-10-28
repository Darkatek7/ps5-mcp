"""The background reader of klogsrv's kernel log."""

from __future__ import annotations

import socket
import threading
import time
from collections import deque

from . import config
from .config import IP, KLOG_PORT, KLOG_QUIET_S, POWER_CHECK_S
from .power import power_state

class KlogTail:
    """Reads klogsrv in the background, so the log can be looked at after something happened.

    klogsrv sends every line to every connected client (checked on a console), so holding this connection
    takes nothing away from other log readers. A connection that was open when the console went to or came
    back from rest mode stays open but gets no more lines (seen on 13.00), so the reader connects again when the
    power state differs from the one at connecting: it looks every POWER_CHECK_S, and ready() looks before a call.

    klogsrv itself can also go silent while clients stay connected (seen on 13.00, also without rest mode). It
    opens /dev/klog again only after every client has left, so ready() also connects again after KLOG_QUIET_S
    without a line, and titlectl() does when a payload ran without a single line being logged.

    One stuck client is enough to keep klogsrv silent for everyone, and several sessions may run a server each.
    So the reader starts only when a tool needs the log, and disconnects after KLOG_IDLE_S without one; the log before
    a call is kept from the reader's start on.
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
        self.used = 0.0

    def start(self) -> None:
        self.used = time.time()
        if self.thread is None or not self.thread.is_alive():
            self.since = None
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
        while time.time() < self.used + config.KLOG_IDLE_S:
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
                    while not self.retry.is_set() and time.time() < self.used + config.KLOG_IDLE_S:
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
        self.connected.clear()

    def read(self, since: float, contains: str = "") -> list[str]:
        with self.guard:
            return [line for t, line in self.lines if t >= since and contains in line]


klog = KlogTail()
