"""Remote Play's discovery protocol (power state, wake message) and its registration (ps5_pair)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket

from . import config
from .config import DDP_PORT, DDP_VERSION, IP, parse_ddp

# ---- power ----

def ddp(msg_type: str, fields: dict[str, str] | None = None) -> bytes:
    """A message of Remote Play's discovery protocol, as pyremoteplay's ddp.py builds it."""
    lines = [f"{msg_type} * HTTP/1.1"] + [f"{k}:{v}" for k, v in (fields or {}).items()]
    return ("\n".join(lines) + f"\ndevice-discovery-protocol-version:{DDP_VERSION}\n").encode()


def ddp_search(timeout: float = 1) -> dict[str, str] | None:
    """The console's answer to a discovery search (parse_ddp), or None when it does not answer."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        try:
            s.sendto(ddp("SRCH"), (IP, DDP_PORT))
            return parse_ddp(s.recvfrom(4096)[0])
        except OSError:
            return None


def power_state(timeout: float = 1) -> str | None:
    """"on", "rest" or None (no answer: off, or not on the network) from a discovery search, which needs no key."""
    fields = ddp_search(timeout)
    state = {"200": "on", "620": "rest"}.get(fields["status"]) if fields else None
    if state:
        _power_seen[0] = state
    return state


# The last power state seen (or "off" after ps5_power_off). A resting console may stop answering, so no answer
# after "rest" most likely still means rest mode.
_power_seen: list[str | None] = [None]


def power_text() -> str:
    state = power_state()
    if state:
        return {"on": "on", "rest": "rest mode"}[state]
    return {"rest": "no answer (it was resting: most likely still in rest mode, whose network part may sleep)",
            "off": "no answer (turned off with ps5_power_off)"}.get(
        _power_seen[0], "no answer (off, or not on the network)")


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
    if config.REGIST_KEY:
        return config.REGIST_KEY
    try:
        return json.loads(config.REGIST_FILE.read_text()).get(IP, {}).get("PS5-RegistKey", "")
    except (OSError, ValueError):
        return ""


def firmware(version: str) -> str:
    """The firmware from a discovery answer's system-version, e.g. "13000040" -> "13.00"."""
    return f"{int(version[:2])}.{version[2:4]}" if len(version) >= 4 and version[:4].isdigit() else version or "?"
