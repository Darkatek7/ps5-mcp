"""Controller input: button specs, held states, touch blocks. Callers hold _lock and an eye.session()."""

from __future__ import annotations

import re
import time

from .config import (BUTTONS, RELEASE_REPORTS, REPORT_INTERVAL_S, STATE_SIZE, TOUCHPAD_SIZE, TOUCH_COUNT_AT,
    TOUCH_POINTS_AT)
from .remoteeye import eye

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


# ---- what the tools do; callers hold _lock and an eye.session() ----

def _hold(seconds: float, bits: int, lx: int = 128, ly: int = 128, rx: int = 128, ry: int = 128) -> None:
    """Holds a state for `seconds`, re-sending it like a real controller does (see REPORT_INTERVAL_S); sent at
    least once."""
    l2, r2 = 255 if bits & BUTTONS["l2"] else 0, 255 if bits & BUTTONS["r2"] else 0
    end = time.time() + seconds
    while True:
        eye.state(bits, lx, ly, rx, ry, l2, r2)
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(left, REPORT_INTERVAL_S))


def _release() -> None:
    for i in range(RELEASE_REPORTS):
        if i:
            time.sleep(REPORT_INTERVAL_S)
        eye.state(0)


def _press(bits: int, hold_ms: int, repeat: int, gap_ms: int) -> None:
    for i in range(max(1, repeat)):
        _hold(hold_ms / 1000, bits)
        _release()
        if i + 1 < repeat:
            time.sleep(gap_ms / 1000)


def _stick(lx: float, ly: float, rx: float, ry: float, duration_ms: int, bits: int) -> None:
    _hold(duration_ms / 1000, bits, axis(lx), axis(ly), axis(rx), axis(ry))
    _release()


def touch_block(points: list[tuple[float, float]], bits: int = 0) -> bytes:
    block = bytearray(STATE_SIZE)
    block[0xC:0x10] = bits.to_bytes(4, "little")
    block[0x10:0x14] = bytes((128, 128, 128, 128))
    block[0x17] = 1
    block[TOUCH_COUNT_AT] = len(points)
    for i, (x, y) in enumerate(points[:2]):
        at = TOUCH_POINTS_AT[i]
        px = round(max(0.0, min(1.0, x)) * (TOUCHPAD_SIZE[0] - 1))
        py = round(max(0.0, min(1.0, y)) * (TOUCHPAD_SIZE[1] - 1))
        block[at:at + 4] = px.to_bytes(2, "little") + py.to_bytes(2, "little")
        block[at + 4] = i
    return bytes(block)


def _touch(x: float, y: float, to_x: float | None, to_y: float | None, duration_ms: int, click: bool) -> None:
    bits = BUTTONS["touchpad"] if click else 0
    steps = max(1, duration_ms // 16) if to_x is not None or to_y is not None else 1
    end_x = x if to_x is None else to_x
    end_y = y if to_y is None else to_y
    for i in range(steps + 1):
        t = i / steps
        eye.block(touch_block([(x + (end_x - x) * t, y + (end_y - y) * t)], bits))
        time.sleep(duration_ms / 1000 / (steps + 1))
    eye.state(0)
