"""MCP server that lets Claude see the PS5 screen, press its buttons and move files to and from it.

Input goes through the RemoteEye payload (payload/RemoteEye), a virtual controller on the console. The screen is
captured by the console itself: holding that controller's Create button saves a screenshot to the gallery,
which is fetched over FTP, decoded and scaled down here.

Console services it uses: elfldr (9021) to start RemoteEye, titlectl and other payloads, ftpsrv (2121) for
screenshots and files, klogsrv (3232) for the log, RemoteEye on 3320 + its build tag, and optionally
ShadowMountPlus's API (10101) to register uploaded apps.

This module holds the tools; the parts they share are the other modules of this package (config, net, power,
klog, remoteeye, system, controls, screen). Run it with `ps5-mcp` (installed), `python -m ps5mcp`, or
`python server/server.py` from a checkout; `ps5-mcp fetch-payloads` downloads the payloads.

Configuration (environment):
  PS5_IP          console address; default: the one PS5 that answers a discovery search on the network
  PS5_REMOTEEYE_ELF  path to RemoteEye.elf; default: payload/RemoteEye/out/RemoteEye.elf, then RemoteEye.elf
                     in the repository root (where a release download goes)
  PS5_TITLECTL_ELF   path to titlectl.elf (starting and closing titles, the browser); the same two places
  PS5_PAYLOAD_DIR    where "ps5-mcp fetch-payloads" puts both, searched after the two places above
                     (default ~/.ps5-mcp/payloads)
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

import ftplib
import json
import re
import socket
import time
import urllib.request
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Literal

from mcp.server.fastmcp import FastMCP, Image
from pydantic import BaseModel, Field

from . import config, net
from .config import (BLOCK_TAG, DDP_PORT, DOWNLOAD_DIR, ELFLDR_PORT, INLINE_TEXT_LIMIT, IP, KLOG_PORT, PS_TAG,
                     SMP_PORT)
from .controls import _press, _stick, _touch, parse_buttons
from .klog import klog
from .net import entries, send_elf, user_id
from .power import (RP_SEARCH_PORT, _power_seen, ddp, ddp_search, firmware, power_state, power_text, rp_register,
                    saved_regist_key, wake_credential)
from .remoteeye import _lock, eye, outdated_note
from .screen import _shoot
from .system import _names, check_title_id, power_call, report_code, running_title, title_name, titlectl

mcp = FastMCP("ps5")


Crop = tuple[float, float, float, float]


@mcp.tool()
def ps5_screenshot(max_width: int = 1280, crop: Crop | None = None) -> Image:
    """Capture the PS5 screen as it is right now (the whole composited screen, system UI included).

    crop: (left, top, right, bottom) as fractions 0..1 of the screen, e.g. (0.5, 0, 1, 0.5) for the top right
      quarter. The crop is cut from the full-resolution screenshot, so it shows small text more sharply than
      the whole screen scaled down.
    Works by holding the virtual controller's Create button, which saves a screenshot to the console's
    gallery, from where it is fetched over FTP. Takes a few seconds; the first one with a newly added controller
    ~20 s, as it answers the console's "Who's using this controller?" prompt first.
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


@mcp.tool()
def ps5_titles(contains: str = "") -> str:
    """List the installed titles (games, apps, homebrew registered by ShadowMountPlus) with their names.
    contains: keep only titles whose id or name contains this (case-insensitive)."""
    with net.ftp() as f:
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
    with net.ftp() as f:
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
    with net.ftp() as f:
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
        saved = json.loads(config.REGIST_FILE.read_text())
    except (OSError, ValueError):
        pass
    saved[IP] = info
    config.REGIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.REGIST_FILE.write_text(json.dumps(saved, indent=2))
    return f"paired as Remote Play device of {info.get('PS5-Nickname', 'the console')}; key saved to {config.REGIST_FILE}"


@mcp.tool()
def ps5_rest() -> str:
    """Put the console in rest mode, as its power menu does. The payloads (elfldr, ftpsrv, RemoteEye, ...) keep
    running and answer again once it wakes. Wake it with ps5_wake (after ps5_pair once), the controller or the
    TV over HDMI-CEC."""
    power_call("rest", "the console did not go to rest")
    end = time.time() + 20
    while time.time() < end:
        if power_state() == "rest":
            return "the console is in rest mode"
        time.sleep(1)
    raise RuntimeError("the console did not report rest mode within 20 s")


@mcp.tool()
def ps5_power_off(confirm: bool = False) -> str:
    """Turn the console off, as its power menu does. This ends the jailbreak: no payload runs until the exploit is
    started again, and ps5_wake cannot turn it on (it only wakes from rest mode). Pass confirm=true only when the
    user asked to turn the console off; ps5_rest keeps the jailbreak."""
    if not confirm:
        raise ValueError("turning off ends the jailbreak, and ps5_wake cannot turn the console on again. Call with "
                         "confirm=true if the user wants that; ps5_rest keeps the jailbreak")
    power_call("off", "the console did not turn off")
    _power_seen[0] = "off"
    end = time.time() + 60
    while time.time() < end:
        if not net.elfldr_answers() and power_state() is None:
            return ("the console is turning off. Start it with its power button or a controller, then the exploit "
                    "again")
        time.sleep(1)
    raise RuntimeError("the console still answered 60 s after the call to turn off")


WAKE_RESEND_S = 2


def send_wake(message: bytes) -> None:
    """Sends a wake message, from UDP port 9303 when it is free (the port pyremoteplay notes the PS5 needs)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind(("", RP_SEARCH_PORT))
        except OSError:
            pass
        s.sendto(message, (IP, DDP_PORT))


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
    start = sent = time.time()
    send_wake(message)
    while time.time() < start + timeout_s:
        state = power_state()
        if state == "on" and net.elfldr_answers():
            return f"the console is on (after {time.time() - start:.0f} s)"
        # One message can get lost while the network part sleeps (seen on 13.00: the first one did nothing,
        # sent again every few seconds it woke in 13 s), so it goes out again until the console is up.
        if state != "on" and time.time() >= sent + WAKE_RESEND_S:
            send_wake(message)
            sent = time.time()
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
    with net.ftp() as f:
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
    with net.ftp() as f:
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
    with net.ftp() as f:
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
    with net.ftp() as f:
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
    with net.ftp() as f:
        f.rename(from_path, to_path)
    return f"moved {from_path} to {to_path}"


@mcp.tool()
def ps5_status() -> str:
    """Power state (on / rest mode), which console services answer, and the RemoteEye controller's state."""
    lines = [f"power: {power_text()}"]
    for name, port in (("elfldr", ELFLDR_PORT), ("ftpsrv", config.FTP_PORT), ("klogsrv", KLOG_PORT)):
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
            attached = "no controller yet"
            if handle and int(handle.group(1)) >= 0:
                attached = ("controller attached (if screenshots fail, the console may still ask \"Who's using "
                            "this controller?\": ps5_press right, then cross)")
            lines.append(f"remoteeye: v{tag}, {attached} ({reply})")
            if outdated_note(tag):
                lines.append(f"remoteeye: {outdated_note(tag)}")
            if tag < BLOCK_TAG:
                lines.append(f"remoteeye: v{tag} has no touch input; send v{BLOCK_TAG} or newer for ps5_touch")
            if len(found) > 1:
                lines.append("remoteeye: older instances still running: " + ", ".join(f"v{t}" for t, _ in found[:-1]))
        if busy:
            lines.append(f"remoteeye: port(s) {busy} busy with another client")
        if not found and not busy:
            lines.append("remoteeye: not running (it is started on first use)")
    lines.append("klog reader: " + ("connected" if klog.connected.is_set() else
                                    "not connected (it connects when a tool needs the log)"))
    return "\n".join(lines)


@mcp.tool()
def ps5_selftest(screenshot: bool = False) -> str:
    """Check every part the tools rely on and report each as ok or FAIL with the reason: the console and its
    firmware, the loader, FTP, the user, RemoteEye, title calls, the title list, the kernel log and Remote Play
    pairing. Changes nothing on the console; screenshot=true also takes one (it adds a picture to the gallery).
    Good to run first on a new console or firmware, and to paste into a bug report."""
    results = []

    def check(name, run):
        try:
            results.append(f"ok    {name}: {run()}")
        except Exception as e:  # each check reports its own failure, whatever it is
            results.append(f"FAIL  {name}: {e}")

    def console():
        fields = ddp_search()
        if not fields:
            raise RuntimeError(f"{IP} does not answer a discovery search ({power_text()})")
        return f"{fields.get('host-name', '?')} at {IP}, firmware {firmware(fields.get('system-version', ''))}, " \
               f"{power_text()}"

    def port(number):
        socket.create_connection((IP, number), timeout=2).close()
        return "answers"

    def remoteeye():
        with _lock, eye.session() as e:
            reply = e.call("PING")
        note = outdated_note(eye.tag)
        return f"{reply} ({note})" if note else reply

    def kernel_log():
        if not klog.ready():
            raise RuntimeError("klogsrv does not answer")
        return "connected"

    check("console", console)
    for name, number in (("elfldr", ELFLDR_PORT), ("ftpsrv", config.FTP_PORT), ("klogsrv", KLOG_PORT)):
        check(f"{name} ({number})", lambda number=number: port(number))
    check("user", lambda: f"{user_id():x}")
    check("RemoteEye", remoteeye)
    check("title calls", lambda: titlectl("running"))
    check("titles", lambda: f"{len(ps5_titles().splitlines())} listed")
    check("kernel log", kernel_log)
    check("Remote Play pairing", lambda: "registered, ps5_wake can wake the console" if saved_regist_key()
          else "none yet (ps5_pair, needed for ps5_wake)")
    if screenshot:
        check("screenshot", lambda: f"{len(ps5_screenshot(max_width=320).data)} bytes")
    failed = sum(line.startswith("FAIL") for line in results)
    return "\n".join(results) + f"\n{failed} of {len(results)} checks failed" if failed else \
        "\n".join(results) + "\nall checks passed"
