"""titlectl's actions: answered by RemoteEye 7+ or by the titlectl payload; title helpers."""

from __future__ import annotations

import ftplib
import json
import re
import threading
import time

from . import config
from .config import ELFLDR_PORT, KLOG_PORT, REMOTEEYE_ELF, SYSTEM_TAG, TITLECTL_MARKER, TITLECTL_ROOM, TITLE_ID
from .net import send_elf
from .klog import klog
from .remoteeye import _lock, eye

# ---- titles ----

_titlectl_lock = threading.Lock()
_names: dict[str, str] = {}


def titlectl(action: str, argument: str = "", timeout: float = 10) -> str:
    """Runs titlectl's action on the console and returns its report (the kernel log line after
    "ps5-mcp:titlectl: ").

    RemoteEye 7 and newer answer these calls themselves, with the same report and without klogsrv. Otherwise
    titlectl runs: one prebuilt payload whose arguments are written into a slot in the ELF before it is sent, so
    any title can be named without a compiler on the PC. elfldr reports nothing back, so the payload logs its
    result.
    """
    report = remoteeye_system(action, argument)
    if report is not None:
        return report
    if not config.TITLECTL_ELF.exists():
        raise RuntimeError(f"there is no {config.TITLECTL_ELF} (download it from the releases page, or build it: "
                           "payload/titlectl/build.sh)")
    elf = config.TITLECTL_ELF.read_bytes()
    at = elf.find(TITLECTL_MARKER + b"\0")
    if at < 0:
        raise RuntimeError(f"{config.TITLECTL_ELF} has no argument slot; rebuild it")
    args = action.encode() + b"\0" + argument.encode() + b"\0"
    if len(args) > TITLECTL_ROOM:
        raise ValueError(f"the argument is too long ({len(args)} bytes, at most {TITLECTL_ROOM})")
    at += len(TITLECTL_MARKER) + 1
    elf = elf[:at] + args + elf[at + len(args):]

    if not klog.ready():
        raise RuntimeError(f"titlectl reports through the kernel log, and klogsrv (port {KLOG_PORT}) does not "
                           "answer")
    with _titlectl_lock:
        start = time.time()
        send_elf(elf)
        while time.time() < start + timeout:
            for line in klog.read(start, "ps5-mcp:titlectl: "):
                report = line.split("ps5-mcp:titlectl: ", 1)[1].strip()
                if report.startswith("bad-args"):
                    raise RuntimeError(f"{config.TITLECTL_ELF} is older than this server and does not know "
                                       f"'{action}': download titlectl.elf from the latest release, or build it")
                if report.startswith(action):
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


def remoteeye_system(action: str, argument: str) -> str | None:
    """The report of titlectl's action as RemoteEye 7+ answers it, or None when titlectl has to run (an older or
    no RemoteEye, or a line too long for RemoteEye's 512 bytes)."""
    line = f"{action.upper()} {argument}".strip()
    if len(line) > 500 or not (REMOTEEYE_ELF.exists() or eye.port):
        return None
    with _lock:
        try:
            session = eye.session()
            e = session.__enter__()
        except RuntimeError:
            return None
        try:
            if e.tag < SYSTEM_TAG:
                return None
            try:
                reply = e.call(line)
            except OSError as err:
                raise RuntimeError(f"RemoteEye did not answer '{line}' ({err})") from None
        finally:
            session.__exit__(None, None, None)
    if not reply.startswith("OK "):
        raise RuntimeError(f"RemoteEye: {reply}")
    return reply[3:]


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


def power_call(action: str, failed: str) -> None:
    """Runs titlectl's rest or off. The console may stop before titlectl's line gets out, so a missing answer is
    no error; the caller checks the power state instead."""
    try:
        rc = report_code(titlectl(action, timeout=5))
    except RuntimeError as e:
        if any(text in str(e) for text in ("did not report", "klogsrv sent nothing", "did not answer")):
            return
        raise
    if rc != 0:
        raise RuntimeError(f"{failed}: error 0x{rc & 0xFFFFFFFF:08x}")
