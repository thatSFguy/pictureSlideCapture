#!/usr/bin/env python3
"""Digital brightness correction for captured slide JPEGs.

The light pad is fixed and slide density varies wildly (a hazy sky vs. a dense
Kodachrome shadow), so a single shutter speed leaves some frames a stop or two
dark. Auto-reshoot (capture_server._auto_reshoot) fixes that optically but costs
a whole extra ~18 s capture cycle per slide. This module fixes it in the pixels
instead: meter the frame (jpegstats), then pull it to a target brightness with a
gamma curve and re-encode. No shutter actuation, no operator input.

Design notes:
- **Gamma, not linear gain.** out = 255*(in/255)**g maps the metered mean onto
  the target while pinning 0 and 255, so brightening can never clip highlights.
  A linear gain would blow them out on any frame with a specular hot spot.
- **Correction is bounded** (`max_ev`) and the untouched original is kept, so a
  misjudged frame (a legitimately dark night shot metered as "underexposed") is
  never lost — Review can revert it.
- **RAW is never touched.** For negatives the CR2 is the real deliverable and
  inversion happens in post; only the JPEG is corrected.
- PIL (python3-pil, apt — not pip) does the decode/encode; pure Python can't
  re-encode a 10 MP JPEG in a usable time on a Pi. Everything degrades to a
  no-op when PIL is missing, so the app runs unchanged on a host without it.
- EXIF is spliced over from the original **byte for byte** rather than re-written
  by PIL (keeps MakerNotes, and doesn't depend on PIL's exif-writing quirks),
  with the embedded thumbnail put through the same curve — otherwise Review's
  thumbnail grid and jpegstats (which meters off that thumbnail) would both
  still show the uncorrected frame.
"""

from __future__ import annotations

import math
import os
import shutil
import struct
from io import BytesIO
from pathlib import Path

# Settings shape; merged/clamped by capture_server.set_brightness().
#   mode "flagged" -> only correct frames jpegstats flagged (dark/under/…)
#   mode "all"     -> normalize every frame onto `target` (consistent batch,
#                     but re-encodes frames that were already fine)
DEFAULTS = {
    "enabled": True,
    "mode": "flagged",       # "flagged" | "all"
    "target": 110,           # aim for this metered luma mean (jpegstats scale)
    "max_ev": 1.5,           # never move a frame more than this many stops
    "keep_original": True,   # stash the untouched capture in captures/originals/
    "quality": 92,           # fallback encode quality (original qtables preferred)
}

ORIGINALS_DIR = "originals"

# Below this metered mean the frame is essentially blank (empty slide slot, lens
# cap, dark frame) — "correcting" it just amplifies sensor noise into mud.
MIN_MEAN = 8.0
# Don't burn a re-encode (and its generational quality loss) on a nudge.
MIN_EV = 0.12


class BrightnessError(Exception):
    pass


_import_error = "not attempted"


def available() -> bool:
    """True if the image library needed to correct pixels is installed.

    Catches Exception, not just ImportError: an INSTALLED-but-broken Pillow (a
    C extension that can't find libjpeg, an arch/ABI mismatch) raises other
    types, and this is called from every status poll — letting that escape would
    500 the whole status endpoint over an optional feature. The reason is kept
    for import_error(), because "installed but won't load" and "not installed"
    look identical from the outside and need completely different fixes."""
    global _import_error
    try:
        import PIL.Image  # noqa: F401
        _import_error = ""
        return True
    except Exception as e:
        _import_error = f"{type(e).__name__}: {e}"
        return False


def import_error() -> str:
    """Why the last available() check failed ('' if it succeeded)."""
    return _import_error


def unavailable_reason() -> str:
    available()                      # refresh _import_error
    if _import_error and "No module named" not in _import_error:
        return (f"Pillow is installed but will not load ({_import_error}) — "
                "brightness correction is off.")
    return ("Pillow (python3-pil) is not installed — brightness correction is "
            "off. Install it with: sudo apt install python3-pil")


def describe(settings: dict) -> str:
    """One-line human summary for the UI / diagnostics."""
    if not settings.get("enabled"):
        return "off"
    if not available():
        return "unavailable (python3-pil not installed)"
    scope = "every frame" if settings.get("mode") == "all" else "flagged frames only"
    return (f"on — {scope}, target {int(settings.get('target', 110))}, "
            f"max ±{float(settings.get('max_ev', 1.5)):g} EV")


# ---- deciding what to do --------------------------------------------------

def plan(stats: dict | None, settings: dict) -> dict | None:
    """Decide the correction for a frame from its jpegstats reading.

    Returns {"ev", "gamma", "from_mean", "to_mean"} or None to leave it alone.
    """
    if not settings.get("enabled") or not stats:
        return None
    if settings.get("mode") != "all" and stats.get("status") == "ok":
        return None
    mean = float(stats.get("mean") or 0)
    if not (MIN_MEAN <= mean <= 250):
        return None                      # blank/blown frame: nothing to rescue
    target = float(settings.get("target", DEFAULTS["target"]))
    max_ev = float(settings.get("max_ev", DEFAULTS["max_ev"]))
    ev = math.log2(target / mean)
    ev = max(-max_ev, min(max_ev, ev))   # bounded: a bad call stays recoverable
    if abs(ev) < MIN_EV:
        return None
    to_mean = mean * (2 ** ev)
    # Gamma that lands `mean` exactly on `to_mean` while pinning 0 and 255.
    gamma = math.log(to_mean / 255.0) / math.log(mean / 255.0)
    return {"ev": round(ev, 2), "gamma": round(gamma, 4),
            "from_mean": round(mean, 1), "to_mean": round(to_mean, 1)}


def build_lut(gamma: float) -> list[int]:
    """256-entry gamma curve, clamped to 0..255."""
    return [0] + [min(255, max(0, int(round(255.0 * (i / 255.0) ** gamma))))
                  for i in range(1, 256)]


# ---- JPEG segment surgery (stdlib; keeps EXIF byte-exact) ------------------

def _split_segments(data: bytes) -> tuple[bytes, bytes]:
    """Split a JPEG into (concatenated APPn segments, everything else after SOI).

    Walks markers until SOS. Returns ("", data[2:]) if it can't parse, so callers
    degrade to "no EXIF carried over" rather than producing a corrupt file.
    """
    if data[:2] != b"\xff\xd8":
        return b"", data
    apps, rest, i, n = bytearray(), bytearray(), 2, len(data)
    while i + 4 <= n and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            i += 2
            continue
        if marker == 0xDA:                       # start of scan: raw data follows
            break
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        if seglen < 2 or i + 2 + seglen > n:
            return b"", data[2:]                 # malformed — don't touch it
        seg = data[i:i + 2 + seglen]
        (apps if 0xE0 <= marker <= 0xEF else rest).extend(seg)
        i += 2 + seglen
    rest.extend(data[i:])
    return bytes(apps), bytes(rest)


def _reattach_apps(new_jpeg: bytes, apps: bytes) -> bytes:
    """Replace the encoder's APPn segments with the original camera ones."""
    if not apps:
        return new_jpeg
    _, rest = _split_segments(new_jpeg)
    return b"\xff\xd8" + apps + rest


def _patch_exif_thumbnail(apps: bytes, lut: list[int]) -> bytes:
    """Run the same curve over the EXIF thumbnail inside the APP1 segment.

    Re-encodes the thumbnail to fit within its ORIGINAL byte length and patches
    only the length tag, so every offset in the EXIF block stays valid and the
    segment keeps its size — no re-serializing of IFDs, no MakerNote breakage.
    Returns `apps` unchanged if there is no thumbnail or anything looks off.
    """
    try:
        from PIL import Image
    except ImportError:
        return apps
    # locate the APP1/Exif segment within the APPn block
    i, n = 0, len(apps)
    while i + 4 <= n and apps[i] == 0xFF:
        marker = apps[i + 1]
        seglen = int.from_bytes(apps[i + 2:i + 4], "big")
        if seglen < 2 or i + 2 + seglen > n:
            return apps
        if marker == 0xE1 and apps[i + 4:i + 10] == b"Exif\x00\x00":
            tiff_at = i + 10
            tiff_end = i + 2 + seglen
            break
        i += 2 + seglen
    else:
        return apps

    buf = bytearray(apps)
    tiff = bytes(buf[tiff_at:tiff_end])
    bo = "<" if tiff[:2] == b"II" else ">" if tiff[:2] == b"MM" else None
    if bo is None:
        return apps
    try:
        ifd0 = struct.unpack_from(bo + "I", tiff, 4)[0]
        cnt0 = struct.unpack_from(bo + "H", tiff, ifd0)[0]
        ifd1 = struct.unpack_from(bo + "I", tiff, ifd0 + 2 + cnt0 * 12)[0]
        if not ifd1:
            return apps                       # no IFD1 -> no thumbnail
        cnt1 = struct.unpack_from(bo + "H", tiff, ifd1)[0]
        toff = tlen = len_entry = len_type = None
        for k in range(cnt1):
            ent = ifd1 + 2 + k * 12
            tag, typ, _cnt, val = struct.unpack_from(bo + "HHII", tiff, ent)
            if typ == 3:                       # SHORT: value sits in the high half
                val = struct.unpack_from(bo + "H", tiff, ent + 8)[0]
            if tag == 0x0201:
                toff = val
            elif tag == 0x0202:
                tlen, len_entry, len_type = val, ent, typ
        if not (toff and tlen) or len_entry is None:
            return apps
        if tiff[toff:toff + 2] != b"\xff\xd8" or toff + tlen > len(tiff):
            return apps
        thumb = tiff[toff:toff + tlen]

        im = Image.open(BytesIO(thumb))
        im.load()
        new = im.point(lut * len(im.getbands()))
        out = None
        for q in (88, 78, 68, 55, 40):         # must fit the original slot
            b = BytesIO()
            new.save(b, "JPEG", quality=q)
            if b.tell() <= tlen:
                out = b.getvalue()
                break
        if out is None:
            return apps                        # can't fit: leave the thumb alone
    except (struct.error, IndexError, OSError, ValueError):
        return apps

    buf[tiff_at + toff:tiff_at + toff + tlen] = out + b"\x00" * (tlen - len(out))
    ent = tiff_at + len_entry
    if len_type == 3:
        struct.pack_into(bo + "H", buf, ent + 8, len(out))
    else:
        struct.pack_into(bo + "I", buf, ent + 8, len(out))
    return bytes(buf)


# ---- the correction itself -------------------------------------------------

def _sampling(src) -> dict:
    """The source JPEG's chroma subsampling as a save kwarg (empty if unknown).
    The 400D writes 4:2:2; letting PIL pick its own default would re-sample the
    chroma as a side effect of a brightness change."""
    try:
        from PIL import JpegImagePlugin
        s = JpegImagePlugin.get_sampling(src)
        return {"subsampling": s} if s in (0, 1, 2) else {}
    except (ImportError, AttributeError, ValueError, TypeError):
        return {}

def identity(path: Path) -> tuple | None:
    """Cheap "is this still the same file" token (size + mtime)."""
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def correct(path: Path, plan_: dict, keep_original: bool = True,
            quality: int = 92, expect: tuple | None = None) -> dict:
    """Apply `plan_` to the JPEG at `path`, in place.

    The untouched capture is copied to `<dir>/originals/<name>` first (never
    overwriting an original already stashed there, so re-correcting a frame
    still leaves the TRUE original recoverable). The corrected file is written
    to a temp file and swapped in atomically, so an interrupted correction can
    never leave a half-written frame under the real name.

    `expect` is an identity() token taken when the work was queued. Correction
    happens on a worker thread seconds after the capture, and group filenames
    get REUSED (Redo-last deletes the frame, then the next shot takes the same
    number back) — without this check a slow correction could drop stale pixels
    on top of a brand-new capture. Mismatch aborts before anything is written.

    Returns {"ok", "ev", "gamma", "original"} or raises BrightnessError.
    """
    try:
        from PIL import Image
    except ImportError as e:
        raise BrightnessError(unavailable_reason()) from e

    try:
        data = path.read_bytes()
    except OSError as e:
        raise BrightnessError(f"can't read {path.name}: {e}") from e

    lut = build_lut(plan_["gamma"])
    try:
        src = Image.open(BytesIO(data))
        src.load()
        im = src if src.mode in ("RGB", "L") else src.convert("RGB")
        new = im.point(lut * len(im.getbands()))
        buf = BytesIO()
        try:
            # Re-encode with the CAMERA's own quantization tables and chroma
            # sampling: same compression character as the original, no
            # second-generation quality cliff. (subsampling="keep" is NOT usable
            # here — point() returns a plain image, not a JPEG-backed one, and
            # PIL rejects "keep"; the numeric sampling is the equivalent.)
            new.save(buf, "JPEG", qtables=src.quantization, **_sampling(src))
        except (AttributeError, KeyError, TypeError, ValueError, OSError):
            buf = BytesIO()
            new.save(buf, "JPEG", quality=int(quality))
    except (OSError, ValueError) as e:
        raise BrightnessError(f"could not process {path.name}: {e}") from e

    apps, _ = _split_segments(data)                  # original EXIF/JFIF blocks
    apps = _patch_exif_thumbnail(apps, lut)          # ...with a corrected thumb
    out = _reattach_apps(buf.getvalue(), apps)

    tmp = path.with_name(path.name + ".bright.tmp")
    stashed = None
    try:
        tmp.write_bytes(out)
        if expect is not None and identity(path) != expect:
            tmp.unlink()
            raise BrightnessError("frame changed while correcting — skipped")
        if keep_original:
            stashed = stash_original(path)
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise BrightnessError(f"could not write {path.name}: {e}") from e
    return {"ok": True, "ev": plan_["ev"], "gamma": plan_["gamma"],
            "original": stashed.name if stashed else None}


# ---- originals (the undo) --------------------------------------------------

def originals_dir(out_dir: Path) -> Path:
    return out_dir / ORIGINALS_DIR


def original_for(path: Path) -> Path:
    return originals_dir(path.parent) / path.name


def has_original(path: Path) -> bool:
    return original_for(path).is_file()


def stash_original(path: Path) -> Path | None:
    """Copy the untouched capture aside. Never clobbers an existing stash — the
    first copy is the real original. Returns the stash path (or None if unable)."""
    dest = original_for(path)
    if dest.exists():
        return dest
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
        return dest
    except OSError:
        return None


def revert(path: Path) -> bool:
    """Restore the stashed original over the corrected file. True if it did."""
    src = original_for(path)
    if not src.is_file():
        return False
    try:
        os.replace(src, path)
        return True
    except OSError:
        return False


def discard_original(path: Path) -> None:
    """Drop the stash for a deleted capture (best-effort)."""
    try:
        original_for(path).unlink()
    except OSError:
        pass


if __name__ == "__main__":                       # quick manual check
    import sys

    import jpegstats
    for p in sys.argv[1:]:
        f = Path(p)
        st = jpegstats.luma_stats(f)
        pl = plan(st, DEFAULTS)
        print(f"{f.name}: {st and st['status']} mean={st and st['mean']} -> {pl}")
