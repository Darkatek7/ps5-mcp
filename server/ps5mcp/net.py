"""FTP, the user id, sending payloads to elfldr, and whether elfldr answers."""

from __future__ import annotations

import ftplib
import os
import socket
from pathlib import Path

from . import config
from .config import ELFLDR_PORT, FTP_PASS, FTP_USER, IP

_user_id: int | None = None


def ftp() -> ftplib.FTP:
    f = ftplib.FTP()
    f.connect(IP, config.FTP_PORT, timeout=15)
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


def send_elf(path: Path | bytes) -> None:
    with socket.create_connection((IP, ELFLDR_PORT), timeout=10) as s:
        s.sendall(path if isinstance(path, bytes) else path.read_bytes())


def entries(f: ftplib.FTP, path: str) -> list[tuple[str, dict]]:
    """A directory's entries without . and .., or [] when it cannot be listed."""
    try:
        return [(n, facts) for n, facts in f.mlsd(path) if facts.get("type") in ("dir", "file")]
    except ftplib.error_perm:
        return []


def elfldr_answers() -> bool:
    try:
        socket.create_connection((IP, ELFLDR_PORT), timeout=1).close()
        return True
    except OSError:
        return False
