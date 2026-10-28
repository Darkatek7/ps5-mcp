"""Screenshots: the gallery, decoding (JPEG XR included) and taking one."""

from __future__ import annotations

import ftplib
import subprocess
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path

from PIL import Image as PILImage

from . import net
from .config import BUTTONS, DELETE_SHOTS, GALLERY, IMAGE_SUFFIXES, JXR2PNG, SAVE_DIR, THUMBNAILS
from .controls import _press
from .net import entries
from .remoteeye import eye

def gallery_dirs(f: ftplib.FTP) -> dict[str, str]:
    """Every screenshot folder (photo/<title>/<title>/<folder>) with its modify time.

    A folder's time changes when a file is added to it, so comparing two of these finds where a new
    screenshot went without listing the hundreds of folders a gallery collects: about a dozen MLSD calls.
    """
    out: dict[str, str] = {}
    for a, fa in entries(f, GALLERY):
        if fa["type"] != "dir":
            continue
        for b, fb in entries(f, f"{GALLERY}/{a}"):
            if fb["type"] != "dir":
                continue
            for c, fc in entries(f, f"{GALLERY}/{a}/{b}"):
                if fc["type"] == "dir":
                    out[f"{GALLERY}/{a}/{b}/{c}"] = fc.get("modify", "")
    return out


def new_shots(f: ftplib.FTP, before: dict[str, str]) -> dict[str, int]:
    """Files added to the gallery since `before` (from gallery_dirs), with their sizes."""
    out: dict[str, int] = {}
    for folder, modify in gallery_dirs(f).items():
        if before.get(folder) == modify:
            continue
        old = before.get(folder, "")
        for name, facts in entries(f, folder):
            # Console time against console time, in whole seconds. The last file of an older screenshot can
            # carry the folder's time, so only later files are new; a new screenshot is always later, as it is
            # written after Create has been held for more than a second.
            if facts["type"] == "file" and facts.get("modify", "") > old:
                out[f"{folder}/{name}"] = int(facts.get("size", 0))
    return out


def decode(raw: bytes, suffix: str, max_width: int,
           crop: tuple[float, float, float, float] | None = None) -> bytes:
    """A screenshot file as a JPEG, cropped to (left, top, right, bottom) in 0..1 of the screen when given,
    then scaled down to max_width."""
    if suffix == ".jxr":
        image = decode_jxr(raw, 0 if crop else max_width)
    else:
        image = PILImage.open(BytesIO(raw))
        image.load()
    if crop:
        left, top, right, bottom = (max(0.0, min(1.0, v)) for v in crop)
        if right <= left or bottom <= top:
            raise ValueError("crop must be (left, top, right, bottom) with left < right and top < bottom, in 0..1")
        image = image.crop((round(left * image.width), round(top * image.height),
                            round(right * image.width), round(bottom * image.height)))
    if image.width > max_width:
        image = image.resize((max_width, round(image.height * max_width / image.width)), PILImage.LANCZOS)
    out = BytesIO()
    image.convert("RGB").save(out, "JPEG", quality=85)
    return out.getvalue()


def decode_jxr(raw: bytes, max_width: int) -> PILImage.Image:
    """JPEG XR (what the console saves with HDR output): the Windows Imaging Component on Windows, else
    imagecodecs when it is installed."""
    if sys.platform == "win32":
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp, "shot.jxr"), Path(tmp, "shot.png")
            src.write_bytes(raw)
            subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(JXR2PNG),
                            "-In", str(src), "-Out", str(dst), "-MaxWidth", str(max_width)],
                           check=True, capture_output=True, timeout=60)
            image = PILImage.open(dst)
            image.load()
            return image
    try:
        import imagecodecs
        import numpy
    except ImportError:
        raise RuntimeError("the console saved a JPEG XR screenshot (HDR output). Outside Windows that needs "
                           "'pip install imagecodecs', or turn HDR off / set the screenshot format to PNG or JPG "
                           "on the console") from None
    pixels = imagecodecs.jpegxr_decode(raw)[..., :3]
    if pixels.dtype == numpy.uint16:
        # HDR screenshots hold PQ (SMPTE ST 2084) values. Decoded to nits, with 250 nits as white and the sRGB
        # curve on top, they match what WIC makes of the same file to within one level on average (measured on
        # console screenshots).
        m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
        e = numpy.power(pixels.astype(numpy.float32) / 65535, 1 / m2)
        nits = 10000 * numpy.power(numpy.maximum(e - c1, 0) / (c2 - c3 * e), 1 / m1)
        linear = numpy.clip(nits / 250, 0, 1)
        srgb = numpy.where(linear <= 0.0031308, linear * 12.92, 1.055 * numpy.power(linear, 1 / 2.4) - 0.055)
        pixels = (srgb * 255 + 0.5).astype(numpy.uint8)
    elif pixels.dtype != numpy.uint8:
        raise RuntimeError(f"unexpected JPEG XR pixel type {pixels.dtype}; turn HDR off or decode on Windows")
    image = PILImage.fromarray(pixels)
    if max_width and image.width > max_width:
        image = image.resize((max_width, round(image.height * max_width / image.width)), PILImage.LANCZOS)
    return image


# How long a screenshot may take to appear in the gallery (an HDR one can take several seconds).
SHOT_WAIT_S = 20


def _capture(f: ftplib.FTP, wait_s: float) -> str | None:
    """Holds Create and returns the gallery path of the screenshot it saved, or None."""
    before = gallery_dirs(f)
    end = time.time() + 1.6
    while time.time() < end:
        eye.state(BUTTONS["create"])
        time.sleep(0.1)
    eye.state(0)

    shot, deadline, last_size = None, time.time() + wait_s, -1
    while time.time() < deadline:
        time.sleep(0.5)
        new = new_shots(f, before)
        images = [p for p in new if p.lower().endswith(IMAGE_SUFFIXES)]
        if images:
            shot = images[0]
            if new[shot] and new[shot] == last_size:
                break
            last_size = new[shot]
    return shot


def _shoot(max_width: int, crop: tuple[float, float, float, float] | None) -> bytes:
    with net.ftp() as f:
        # A controller just added is not bound to a user until "Who's using this controller?" is answered, so
        # its first Create does nothing. Only then is the prompt answered (the user to the right, as on a
        # console with one user), and only once: pressed elsewhere, cross could act on the running app.
        shot = _capture(f, 12 if eye.assigned is False else SHOT_WAIT_S)
        if not shot and eye.assigned is False:
            eye.assigned = None
            _press(BUTTONS["right"], 120, 1, 0)
            time.sleep(0.5)
            _press(BUTTONS["cross"], 120, 1, 0)
            time.sleep(2)
            shot = _capture(f, SHOT_WAIT_S)
        if shot:
            eye.assigned = True
        if not shot:
            raise RuntimeError(
                "no screenshot appeared in the gallery. Possible causes: the virtual controller is not "
                "assigned to a user yet (answer 'Who's using this controller?' with right, then cross); "
                "this screen blocks captures; or the Create button shortcut is set to 'Easy Screenshots' "
                "(Settings > Captures and Broadcasts > Shortcuts for Create Button must be Standard).")

        data = bytearray()
        f.retrbinary("RETR " + shot, data.extend)

        if DELETE_SHOTS:
            folder, stem = shot.rsplit("/", 1)[0], Path(shot).stem
            thumbs = THUMBNAILS + folder[len(GALLERY):]
            created = [f"{folder}/{n}" for n, facts in entries(f, folder) if n.startswith(stem)]
            created += [f"{thumbs}/{n}" for n, facts in entries(f, thumbs) if n.startswith(stem)]
            for p in created:
                try:
                    f.delete(p)
                except ftplib.error_perm:
                    pass

    if SAVE_DIR:
        SAVE_DIR.mkdir(parents=True, exist_ok=True)
        (SAVE_DIR / Path(shot).name).write_bytes(data)
    return decode(bytes(data), Path(shot).suffix.lower(), max_width, crop)
