#!/usr/bin/env python3
"""Web-based slide/negative capture UI for the Canon EOS 400D.

Runs on a Linux host (dev box now, Raspberry Pi later) with the camera on USB
and gphoto2 installed. Serves a phone/tablet/desktop page: set the group name
and exposure, place a slide, tap Capture, review/download/delete shots.

Deploy:
    sudo apt install gphoto2            # (optional) exiftool for EXIF metadata
    python3 capture_server.py          # open http://<host>:8080

Stdlib only. Features:
  - Exposure controls (ISO / aperture / shutter / white balance / format),
    dropdowns populated live from the camera. Requires the dial on M.
  - File management: per-image download + delete, and "download all" (zip) for
    the current group.
  - Group prefix: filenames are <prefix>_0001, _0002, ... so a run of slides or
    negatives shares a name. Numbering is per-prefix and resumable.
  - Metadata: the group name is written into each capture (JPEG comment via
    stdlib; full EXIF ImageDescription on JPEG+CR2 if exiftool is installed).
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

import brightness
import jpegstats
from advance import ADVANCE_DEFAULTS, AdvanceError, make_advancer
from camera import Camera, CameraError
from trigger import TRIGGER_DEFAULTS, TriggerError, make_trigger, read_level

# ---- configuration (edit freely) -----------------------------------------
# capturetarget is set per-capture inside cam.capture() (avoids the 400D
# re-enumeration reset), so startup only needs to pick the default format.
STARTUP_SETTINGS = {"imageformat": "L"}
EXPOSURE_KEYS = ["iso", "aperture", "shutterspeed", "whitebalance", "imageformat"]

# Quick-start presets. ISO/aperture/WB/format are sane fixed choices; shutter is
# only a STARTING guess — the light pad's brightness varies, so fine-tune shutter
# per medium in the drawer. Slides -> JPEG (minor correction only); negatives ->
# RAW + JPEG (RAW is essential for inversion, JPEG for instant preview).
PRESETS = {
    "slides": {"iso": "100", "aperture": "8", "whitebalance": "Daylight",
               "imageformat": "L", "shutterspeed": "1/60"},
    "negatives": {"iso": "100", "aperture": "8", "whitebalance": "Daylight",
                  "imageformat": "RAW + L", "shutterspeed": "1/60"},
}
IMAGE_EXTS = {".jpg", ".jpeg"}
RAW_EXTS = {".cr2", ".crw", ".raw"}
PREFIX_BAD = re.compile(r"[^A-Za-z0-9_-]+")

# ---- shared state ---------------------------------------------------------
cam = Camera(retries=3, backoff=0.8, verbose=True)  # fail fast when camera absent
# Persistent gphoto2 session on/off (--no-persist to disable). It removes ~3s of
# process start + PTP setup from in front of every exposure, which is the rig's
# throughput limit; it self-disables if this camera can't hold a session.
cam_lock = threading.Lock()          # camera is single-session: serialize access
_lock_info = {"who": None, "since": 0.0}   # who holds cam_lock + when they took it


def _lock_acquire(who: str, wait: float) -> bool:
    """Acquire cam_lock, recording the holder + start time for diagnostics.
    wait<=0 -> non-blocking; else block up to `wait` seconds."""
    got = (cam_lock.acquire(blocking=False) if wait <= 0
           else cam_lock.acquire(timeout=wait))
    if got:
        _lock_info["who"], _lock_info["since"] = who, time.monotonic()
    return got


def _lock_release() -> None:
    _lock_info["who"], _lock_info["since"] = None, 0.0
    cam_lock.release()


def lock_status() -> dict | None:
    """What holds the camera right now (for busy messages + diagnostics)."""
    who = _lock_info["who"]
    if not who:
        return None
    return {"who": who, "held_s": round(time.monotonic() - _lock_info["since"], 1)}


def _busy_json(handler, msg="camera busy") -> None:
    ls = lock_status()
    if ls:
        msg = f"busy: {ls['who']} ({ls['held_s']}s)"
    handler._json({"ok": False, "busy": True, "error": msg}, 409)


OUT_DIR = Path("./captures")
PREFIX = "slide"
HAVE_EXIFTOOL = shutil.which("exiftool") is not None

# Auto slide-advance output (stub; default "off" == no-op). See advance.py.
ADVANCE = dict(ADVANCE_DEFAULTS)
advancer = make_advancer(ADVANCE)

# Optical-sensor capture trigger (default "off" == no-op). See trigger.py.
# Built lazily in main() once sensor_capture() is defined below.
TRIGGER = dict(TRIGGER_DEFAULTS)
trigger = None

# Auto-reshoot: after a capture, if the frame is flagged dark/over, step the
# shutter one stop toward correct and reshoot the same frame (keeping the best),
# then restore the baseline shutter. Fixes the odd dense slide in place during
# an auto-run without pushing every following slide off. Default off.
RESHOOT = {"enabled": False, "max": 1}

# Digital brightness correction: after a capture, pull a frame that metered
# dark/bright onto a target brightness by re-encoding it with a gamma curve
# (see brightness.py). Complements auto-reshoot — this costs no shutter
# actuation and no extra capture cycle, but can't recover clipped highlights.
BRIGHTNESS = dict(brightness.DEFAULTS)


def set_brightness(cfg: dict) -> dict:
    """Merge + clamp brightness-correction settings."""
    if "enabled" in cfg:
        BRIGHTNESS["enabled"] = bool(cfg["enabled"])
    if cfg.get("mode") in ("flagged", "all"):
        BRIGHTNESS["mode"] = cfg["mode"]
    if "keep_original" in cfg:
        BRIGHTNESS["keep_original"] = bool(cfg["keep_original"])
    for key, lo, hi, cast in (("target", 60, 200, int), ("max_ev", 0.25, 3.0, float),
                              ("quality", 60, 100, int)):
        if key in cfg:
            try:
                BRIGHTNESS[key] = max(lo, min(hi, cast(cfg[key])))
            except (TypeError, ValueError):
                pass
    _persist_config()
    return dict(BRIGHTNESS)


def public_brightness() -> dict:
    """Brightness config plus what the UI needs to explain itself."""
    ok = brightness.available()
    return {**BRIGHTNESS, "available": ok,
            "describe": brightness.describe(BRIGHTNESS),
            "import_error": "" if ok else brightness.import_error(),
            "pending": _post_q.qsize()}


# Correction runs on a worker thread, NOT in the capture path: a 10 MP
# decode+encode is seconds of CPU (measured 4-6 s on the Pi Zero W) against a
# ~8 s capture cycle. Queuing it keeps the Capture button snappy and lets the
# next shot fire while the previous one is still being corrected.
# One worker (not a pool) so a single-core Pi never thrashes; Review's 4 s
# auto-sync surfaces each frame's final state as the queue drains.
# The SAME worker also embeds the EXIF metadata. exiftool is a large Perl program
# whose cold start alone is seconds on a Pi Zero W, and nothing in the capture
# response depends on it, so running it inline just made the operator wait. One
# queue, one job per frame, so each file is touched by exactly one thread and the
# brightness stale-swap token stays valid (a separate metadata job would rewrite
# the file and invalidate it).
_post_q: queue.Queue = queue.Queue()
_sidecar_lock = threading.Lock()      # sidecar JSON is read-modify-write

# Monotonic time the camera last finished talking to USB. Two users: do_capture
# skips its readiness probe when the previous shot is long past, and the sensor
# trigger uses it to date the 400D's re-enumeration transient. 0 = nothing shot
# yet this run, which reads as "long ago" and is the right answer for both.
LAST_USB_DONE = 0.0
# Last successful camera-side status read, reused by the background poll so it
# never has to take the camera lock while a hands-off run is going (see
# read_status_cached). Refreshed at startup and then piggybacked onto captures
# (see _refresh_cam_status) — NOT left to the poll, which never runs a real read
# while the trigger is armed and so would leave the UI stuck on "no camera".
_CAM_STATUS: dict = {"connected": False}
_CAM_STATUS_AT = 0.0          # monotonic time of that read (0 = never)
# How stale the cached camera fields may get during a hands-off run. The refresh
# rides along on a capture we are already holding the lock for, and happens
# AFTER the shutter, so it can never delay an exposure.
STATUS_REFRESH_S = 240.0
# Shoot blind if the last capture ended longer ago than this. The 400D's
# post-capture bus drop is ~1-2s, so 5s is already a generous margin, and if the
# camera IS unexpectedly away, _grab's no-file retry still recovers.
# This was 20s and that was far too wide: measured on the appliance
# (2026-08-09), edges arrive ~15s apart and the previous capture ends ~5s after
# its own edge, leaving a ~10s gap — inside the window, so the probe ran on
# almost every frame at a cost of 1.6-2.2s. It was BOTH the single largest slice
# of sensor-to-shutter latency and essentially all of its variance (with the
# probe: 4.68-5.16s; the one frame that skipped it: 3.05s).
READY_PROBE_WINDOW = 5.0


def update_exposure(name: str, status: str | None) -> None:
    """Set (or clear) one file's cached exposure verdict, atomically enough for
    the worker and the capture thread to both touch it."""
    with _sidecar_lock:
        ex = load_exposure()
        if status:
            ex[name] = status
        elif ex.pop(name, None) is None:
            return
        save_exposure(ex)


def _post_worker() -> None:
    """Drain the post-capture queue. Never raises — a failed job leaves the
    original capture in place, which is always a valid outcome."""
    while True:
        item = _post_q.get()
        try:
            _post_one(*item)
        except Exception as e:                     # belt and braces: keep draining
            print(f"[post] worker error: {e}", flush=True)
        finally:
            _post_q.task_done()


def _post_one(jpg: Path, raw: Path | None, plan: dict | None,
              token: tuple | None) -> None:
    """Everything a fresh capture needs that the operator doesn't have to wait
    for: the brightness correction, then the EXIF metadata, then a re-meter if
    the pixels moved. Runs off the camera lock, so the next slide can fire while
    this is still going."""
    t0 = time.monotonic()
    if not jpg.is_file():
        return                                     # deleted/redone while queued
    corrected = None
    if plan:
        try:
            corrected = brightness.correct(
                jpg, plan, keep_original=BRIGHTNESS["keep_original"],
                quality=BRIGHTNESS["quality"], expect=token)
        except brightness.BrightnessError as e:
            print(f"[brightness] {jpg.name}: {e}", flush=True)
    # Metadata last, so it is written over the final pixels (a correction
    # rewrites the whole file and would otherwise drop it).
    t_meta = time.monotonic()
    write_metadata(jpg, raw, load_captions().get(jpg.name, ""))
    meta_s = time.monotonic() - t_meta
    if corrected:
        # Re-meter: the correction moved the pixels AND the embedded thumbnail
        # that jpegstats reads, so Review's cached verdict must be refreshed.
        stats = jpegstats.luma_stats(jpg)
        update_exposure(jpg.name, (stats or {}).get("status"))
        print(f"[brightness] {jpg.name}: {corrected['ev']:+.2f} EV "
              f"(gamma {corrected['gamma']}) -> "
              f"{(stats or {}).get('status', '?')}", flush=True)
    print(f"[post] {jpg.name}: meta={meta_s:.1f}s "
          f"total={time.monotonic() - t0:.1f}s (queue {_post_q.qsize()})",
          flush=True)


def plan_brightness(jpg: Path | None, stats: dict | None,
                    derived: bool = False) -> dict | None:
    """Plan a correction for a fresh capture (queuing is the caller's job, as
    part of the one post-capture job). Returns the planned correction for the
    capture response, or None if the frame is left alone.

    Skips RAW-derived previews: with imageformat=RAW the JPEG is only a preview
    extracted from the CR2, and the CR2 is what actually gets developed."""
    if jpg is None or derived or not BRIGHTNESS["enabled"]:
        return None
    if not brightness.available():
        return None
    return brightness.plan(stats, BRIGHTNESS)


def set_reshoot(cfg: dict) -> dict:
    """Merge + clamp auto-reshoot settings."""
    if "enabled" in cfg:
        RESHOOT["enabled"] = bool(cfg["enabled"])
    if "max" in cfg:
        try:
            RESHOOT["max"] = max(1, min(4, int(cfg["max"])))
        except (TypeError, ValueError):
            pass
    _persist_config()
    return dict(RESHOOT)


def set_advance(cfg: dict) -> dict:
    """Merge new advance settings and rebuild the advancer. Lock held.
    Commits only if the new config builds — bad config raises AdvanceError
    without corrupting the live advancer."""
    global ADVANCE, advancer
    merged = {**ADVANCE, **{k: cfg[k] for k in ADVANCE_DEFAULTS if k in cfg}}
    new = make_advancer(merged)               # may raise AdvanceError
    ADVANCE, advancer = merged, new
    _persist_config()
    return ADVANCE


def _advance_once() -> dict:
    """Advance one slide, mapping failures to a result dict (never raises)."""
    try:
        advancer.advance()
        return {"ok": True, "mode": advancer.mode}
    except (AdvanceError, NotImplementedError) as e:
        return {"ok": False, "error": str(e) or "advance not implemented"}


def sensor_capture(edge_ts: float | None = None) -> float:
    """Sensor-trigger callback (runs on the trigger thread). Capture one frame,
    serialized behind the camera lock so it can't overlap a button-press capture
    or an update. Skips the trigger if the camera is busy — never blocks the
    watcher for long. Never raises (the watcher must survive).

    Returns the monotonic time the camera finished with USB, which is what the
    trigger dates the re-enumeration transient from (see trigger.SensorTrigger).
    On a skip or a failure that is simply 'now', so the phantom window covers
    whatever the camera did before giving up."""
    t0 = time.monotonic()
    edge = edge_ts if edge_ts else t0
    if not _lock_acquire("sensor capture", 8):
        print(f"[trigger] camera busy ({lock_status()}) — SKIPPED this slide",
              flush=True)
        return time.monotonic()
    lock_wait = time.monotonic() - t0
    # THE number for a rig whose pusher keeps moving: how long after the sensor
    # tripped did the exposure actually happen. Everything after it is download,
    # by which time the slide is free to move.
    def _shutter():
        print(f"[trigger] SHUTTER at +{time.monotonic() - edge:.2f}s from edge "
              f"(lock wait {lock_wait:.2f}s)", flush=True)
    # Hands-off batch: be patient so a shot fired while the 400D is still
    # re-enumerating rides it out instead of failing (the UI stays fail-fast).
    prev = (cam.retries, cam.backoff)
    cam.retries, cam.backoff = max(cam.retries, 6), max(cam.backoff, 1.0)
    epoch = None
    try:
        res = do_capture(on_shutter=_shutter)
        epoch = LAST_USB_DONE                      # set by a successful capture
        status = (res.get("exposure") or {}).get("status", "?")
        rs = res.get("reshoots") or []
        print(f"[trigger] captured {res.get('name')} [{status}] in "
              f"{time.monotonic() - t0:.1f}s"
              + (f" (reshot x{len(rs)})" if rs else ""), flush=True)
    except CameraError as e:
        print(f"[trigger] capture FAILED after {time.monotonic() - t0:.1f}s: "
              f"{friendly(str(e))}", flush=True)
    finally:
        cam.retries, cam.backoff = prev
        _lock_release()
    # A failed capture never reached the point where USB went quiet, so date the
    # phantom window from now — the camera was on the bus right up to the error.
    return epoch or time.monotonic()


def set_trigger(cfg: dict) -> dict:
    """Merge new trigger settings and (re)start the watcher. Validates by
    building + starting a fresh SensorTrigger before swapping — if it can't
    build/start (bad config or missing gpiomon while enabled), the live watcher
    is left untouched and TriggerError propagates."""
    global TRIGGER, trigger
    merged = {**TRIGGER, **{k: cfg[k] for k in TRIGGER_DEFAULTS if k in cfg}}
    new = make_trigger(merged, sensor_capture)   # may raise TriggerError
    new.start()                                  # may raise (enabled + no tool)
    if trigger is not None:
        trigger.stop()
    trigger, TRIGGER = new, merged
    _persist_config()
    return TRIGGER


def public_trigger() -> dict:
    """Trigger config for the API/UI, plus a human-readable summary."""
    d = dict(TRIGGER)
    d["describe"] = trigger.describe() if trigger is not None else "off"
    d["running"] = bool(trigger is not None and trigger.enabled)
    return d


def read_sensor() -> dict:
    """Current raw sensor level + its interpretation (for polarity setup). Also
    carries the trigger + reshoot + brightness config so the Setup UI can restore
    all three toggles without the (lock-gated) status call — they populate even
    when the camera is busy."""
    base = {"trigger": public_trigger(), "reshoot": dict(RESHOOT),
            "brightness": public_brightness()}
    try:
        lvl = read_level(TRIGGER)
    except TriggerError as e:
        return {"ok": False, "error": str(e), **base}
    obstructed = (lvl == 1) if TRIGGER.get("active_high") else (lvl == 0)
    return {"ok": True, "level": lvl, "obstructed": obstructed, **base}


def sanitize_prefix(s: str) -> str:
    s = PREFIX_BAD.sub("", (s or "").strip())[:40]
    return s or "slide"


def name_re(prefix: str) -> re.Pattern:
    return re.compile(rf"^{re.escape(prefix)}_(\d{{4,}})\.", re.IGNORECASE)


def next_index(prefix: str) -> int:
    rx, hi = name_re(prefix), 0
    for f in OUT_DIR.glob(f"{prefix}_*"):
        m = rx.match(f.name)
        if m:
            hi = max(hi, int(m.group(1)))
    return hi + 1


def group_images(prefix: str) -> list[Path]:
    rx = name_re(prefix)
    imgs = [f for f in OUT_DIR.glob(f"{prefix}_*")
            if f.suffix.lower() in IMAGE_EXTS and rx.match(f.name)]
    imgs.sort(key=lambda f: int(rx.match(f.name).group(1)), reverse=True)
    return imgs


def image_count(prefix: str) -> int:
    return len(group_images(prefix))


def recent_images(prefix: str, limit: int = 24) -> list[str]:
    return [f.name for f in group_images(prefix)[:limit]]


# Any image/RAW file named <prefix>_NNNN.<ext>, capturing the prefix. Greedy, so
# "trip_1972_0001.jpg" groups under "trip_1972" — matching next_index()'s view.
GROUP_RE = re.compile(r"^(.+)_\d{4,}\.", re.IGNORECASE)


def list_groups() -> list[dict]:
    """Every group on disk, newest first. Changing the group name orphans the
    old group's files — still on disk but invisible to a UI that only queries
    the current prefix — so managing them needs a scan, not the current name."""
    groups: dict[str, dict] = {}
    for f in OUT_DIR.iterdir():
        ext = f.suffix.lower()
        if not f.is_file() or ext not in IMAGE_EXTS | RAW_EXTS:
            continue
        m = GROUP_RE.match(f.name)
        if not m:
            continue
        g = groups.setdefault(m.group(1), {"prefix": m.group(1), "count": 0,
                                           "files": 0, "mtime": 0})
        g["files"] += 1
        if ext in IMAGE_EXTS:
            g["count"] += 1
        g["mtime"] = max(g["mtime"], _mtime(f))
    return sorted(groups.values(), key=lambda g: -g["mtime"])


def delete_group(prefix: str) -> list[str]:
    """Remove every file in a group — images, RAW siblings, stashed
    pre-correction originals — and drop its caption/exposure cache entries."""
    rx = name_re(prefix)
    removed = []
    for f in sorted(OUT_DIR.glob(f"{prefix}_*")):
        if not rx.match(f.name):
            continue
        try:
            brightness.discard_original(f)     # pre-correction copy, if any
            f.unlink()
            removed.append(f.name)
        except OSError:
            pass
    if removed:                                # reset the sidecar caches
        caps = load_captions()
        if any(caps.pop(r, None) is not None for r in removed):
            save_captions(caps)
        ex = load_exposure()
        if any(ex.pop(r, None) is not None for r in removed):
            save_exposure(ex)
    return removed


def friendly(err: str) -> str:
    low = err.lower()
    if "no camera found" in low or "could not find" in low:
        return "Camera not found — check it's switched ON and the USB cable is connected."
    if "could not claim" in low or "busy" in low:
        return "Camera is busy — wait a moment and try again."
    if "not manual" in low or "dial" in low or "no displayable" in low:
        return err
    return "Camera error — try turning it off and on again. (" + \
        (err.splitlines()[0][:120] if err else "?") + ")"


# ---- capture + preview extraction ----------------------------------------

def _sof_is_displayable(seg: bytes) -> bool:
    """True for a baseline/extended/progressive JPEG; False for lossless (the
    Canon RAW sensor stream, SOF3) or malformed."""
    i, n = 2, len(seg)
    while i + 2 <= n:
        if seg[i] != 0xFF:
            i += 1
            continue
        m = seg[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m == 0xD8 or 0xD0 <= m <= 0xD9 or m == 0x01:
            i += 2
            continue
        if i + 4 > n:
            break
        seglen = int.from_bytes(seg[i + 2:i + 4], "big")
        if m in (0xC0, 0xC1, 0xC2):
            return True
        if m == 0xC3 or m == 0xDA:
            return False
        i += 2 + seglen
    return False


def _extract_preview_jpeg(data: bytes) -> bytes | None:
    best, start = None, 0
    while True:
        soi = data.find(b"\xff\xd8\xff", start)
        if soi == -1:
            break
        eoi = data.find(b"\xff\xd9", soi + 3)
        if eoi == -1:
            break
        seg = data[soi:eoi + 2]
        start = eoi + 2
        if _sof_is_displayable(seg) and (best is None or len(seg) > len(best)):
            best = seg
    return best


def _load_sidecar(name: str) -> dict:
    p = OUT_DIR / name
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (ValueError, OSError):
            return {}
    return {}


def _save_sidecar(name: str, d: dict) -> None:
    (OUT_DIR / name).write_text(json.dumps(d, indent=2))


def load_captions() -> dict:
    return _load_sidecar("captions.json")


def save_captions(d: dict) -> None:
    _save_sidecar("captions.json", d)


def load_exposure() -> dict:
    """Cached exposure status per filename, written at capture time."""
    return _load_sidecar("exposure.json")


def save_exposure(d: dict) -> None:
    _save_sidecar("exposure.json", d)


def load_config() -> dict:
    """Persisted app settings (sensor trigger, reshoot, advance) so they survive
    a restart — the service restarts on every self-update, which used to reset
    these to off."""
    return _load_sidecar("config.json")


def _persist_config() -> None:
    _save_sidecar("config.json", {
        "trigger": {k: TRIGGER[k] for k in TRIGGER_DEFAULTS},
        "reshoot": dict(RESHOOT),
        "brightness": dict(BRIGHTNESS),
        "advance": {k: ADVANCE[k] for k in ADVANCE_DEFAULTS},
    })


def _jpeg_strip_comments(data: bytes) -> bytes:
    """Remove any existing COM (0xFFFE) segments so re-editing doesn't stack."""
    if data[:2] != b"\xff\xd8":
        return data
    out, i, n = bytearray(data[:2]), 2, len(data)
    while i + 2 <= n:
        if data[i] != 0xFF:
            out += data[i:]
            break
        m = data[i + 1]
        if m in (0xD9, 0xDA):                      # EOI / start of scan: copy rest
            out += data[i:]
            break
        if m == 0xFF:
            out += data[i:i + 1]
            i += 1
            continue
        if m == 0xD8 or 0xD0 <= m <= 0xD7 or m == 0x01:
            out += data[i:i + 2]
            i += 2
            continue
        if i + 4 > n:
            out += data[i:]
            break
        seglen = int.from_bytes(data[i + 2:i + 4], "big")
        if m == 0xFE:                              # COM -> drop
            i += 2 + seglen
            continue
        out += data[i:i + 2 + seglen]
        i += 2 + seglen
    return bytes(out)


def _jpeg_set_comment(path: Path, text: str) -> None:
    data = _jpeg_strip_comments(path.read_bytes())
    if data[:2] != b"\xff\xd8":
        return
    payload = text.encode("utf-8", "replace")[:65533]
    seg = b"\xff\xfe" + (len(payload) + 2).to_bytes(2, "big") + payload
    path.write_bytes(data[:2] + seg + data[2:])


def _desc(name: str, caption: str = "") -> str:
    m = re.match(r"(.+)_(\d{4,})\.", name)
    base = f"{m.group(1)} #{m.group(2)}" if m else name
    return f"{base}: {caption}" if caption else base


def write_metadata(jpg: Path | None, raw: Path | None, caption: str = "") -> None:
    """Embed group (+optional caption) as EXIF ImageDescription (exiftool) or a
    JPEG comment (stdlib fallback). Derives group/index from the filename."""
    ref = jpg or raw
    if ref is None:
        return
    desc = _desc(ref.name, caption)
    if HAVE_EXIFTOOL:
        targets = [str(p) for p in (jpg, raw) if p]
        subprocess.run(["exiftool", "-overwrite_original", "-q",
                        f"-ImageDescription={desc}", f"-XPComment={desc}",
                        *targets], capture_output=True)
    elif jpg is not None:
        _jpeg_set_comment(jpg, desc)


def _wipe(stem_glob: str) -> None:
    for f in OUT_DIR.glob(stem_glob):
        try:
            f.unlink()
        except OSError:
            pass


def _grab(stem: str, _retry: bool = True, on_shutter=None) -> tuple:
    """Capture to <stem>.<ext>, normalize case, derive a preview if RAW-only.
    Returns (jpg, raw, derived) or raises CameraError. Assumes cam_lock held.
    capturetarget=Memory card is set inside cam.capture() (same gphoto2 session)
    so the 400D can't re-enumerate back to Internal RAM between set and shot.

    If nothing downloads (often because a shot fired while the 400D was still
    re-enumerating from the previous capture — e.g. a sensor trigger landing
    before the camera came back), wait for the camera and re-fire once. This
    costs nothing on a normal shot; it only kicks in on the tight-margin miss."""
    glob = f"{stem}.*"
    _wipe(glob)                                    # clear any prior file at stem
    try:
        gp_out = cam.capture(OUT_DIR / f"{stem}.%C", on_shutter=on_shutter)
    except CameraError:
        _wipe(glob)
        raise

    jpg = raw = None
    for f in sorted(OUT_DIR.glob(glob)):
        low = f.with_suffix(f.suffix.lower())
        if f != low:
            f.rename(low)
            f = low
        ext = f.suffix.lower()
        if ext in IMAGE_EXTS:
            jpg = f
        elif ext in RAW_EXTS:
            raw = f

    derived = False
    if jpg is None and raw is not None:            # RAW-only -> derive preview
        prev = _extract_preview_jpeg(raw.read_bytes())
        if prev:
            jpg = raw.with_suffix(".jpg")
            jpg.write_bytes(prev)
            derived = True

    if jpg is None:
        _wipe(glob)
        # Nothing downloaded. Most often the camera was still re-enumerating from
        # the previous shot: wait for it to answer, then re-fire once.
        if _retry:
            print(f"[capture] no file for '{stem}' — waiting for the camera and "
                  "re-firing once", flush=True)
            cam.forget_capturetarget()             # re-send it; a lost setting
                                                   # is one reason for no file
            cam.wait_ready()
            return _grab(stem, _retry=False, on_shutter=on_shutter)
        # Still nothing after a retry. Log the full gphoto2 output + dir listing
        # to the journal (View logs) and echo a short hint in the error.
        listing = sorted(p.name for p in OUT_DIR.iterdir() if p.is_file())
        detail = (gp_out or "").strip()
        print(f"[capture] no displayable image for '{stem}' (after retry).\n"
              f"  gphoto2 output:\n{detail or '(empty)'}\n"
              f"  files now in {OUT_DIR}: {listing}", flush=True)
        hint = detail.replace("\n", " ")[-240:]
        raise CameraError("captured but no displayable image (format issue, or "
                          "the camera returned no file). gphoto2: "
                          f"{hint or '(no output)'}")
    return jpg, raw, derived


_FLAGGED = ("dark", "under", "over", "bright")


def _exp_score(stats: dict | None) -> float:
    """Lower is better. Prefers 'ok', then a slight miss, then a clipped miss;
    breaks ties by nearness to a good mid-brightness. Used to keep the best of
    several reshoots so a correction never leaves a frame worse than the original."""
    if not stats:
        return 9e9
    base = {"ok": 0, "dark": 2, "bright": 2, "under": 4, "over": 4}
    return base.get(stats.get("status"), 3) * 1000 + abs((stats.get("mean") or 0) - 115)


def _shutter_ladder() -> tuple[list[str], int | None]:
    """(shutter labels sorted slow..fast by seconds, index of the current one).
    index is None if the shutter isn't adjustable (dial not on M)."""
    full = cam.get_config_full(["shutterspeed"]).get("shutterspeed", {})
    ladder = sorted(((sec, c) for c in full.get("choices", [])
                     if (sec := _shutter_seconds(c)) is not None),
                    key=lambda x: x[0])
    labels = [c for _, c in ladder]
    current = full.get("current", "")
    return labels, (labels.index(current) if current in labels else None)


def _auto_reshoot(stem, jpg, raw, derived, stats):
    """The first frame read dark/over: step the shutter toward correct and
    reshoot the SAME frame up to RESHOOT['max'] times, keeping the best result,
    then restore the baseline shutter. Reshoot captures go to temp stems and the
    winner is promoted onto `stem`, so an overshoot never leaves it worse.
    Returns (jpg, raw, derived, stats, tries). Assumes cam_lock held."""
    prev = (cam.retries, cam.backoff)
    cam.retries, cam.backoff = 2, 0.5          # best-effort: fail FAST so a
                                               # flagged frame can't burn ~18s
                                               # retrying a reshoot the 400D keeps
                                               # refusing ("could not claim")
    tries = []
    best = {"jpg": jpg, "raw": raw, "derived": derived, "stats": stats,
            "score": _exp_score(stats)}
    try:
        cam.wait_ready()                           # wait for the camera to come
                                                   # back after the first capture
        try:
            labels, idx = _shutter_ladder()
        except CameraError as e:                   # can't read shutter -> keep original
            print(f"[reshoot] can't read shutter, keeping original: "
                  f"{friendly(str(e))}", flush=True)
            return jpg, raw, derived, stats, tries
        if idx is None or len(labels) < 2:
            return jpg, raw, derived, stats, tries
        orig_idx, cur = idx, idx
        for k in range(RESHOOT.get("max", 1)):
            st = (best["stats"] or {}).get("status")
            if st in ("dark", "under"):
                nxt = cur + 1                      # longer -> brighter
            elif st in ("over", "bright"):
                nxt = cur - 1                      # shorter -> darker
            else:
                break                              # 'ok' or unknown -> done
            if not (0 <= nxt < len(labels)):
                break                              # at an edge of the ladder
            cur = nxt
            # A reshoot capture fires right after a capture, in the 400D's USB
            # re-enumeration window — if it errors, keep the best so far (the
            # original is already saved) instead of failing the whole capture,
            # which would leave the frame unmetered ("exposure n/a").
            try:
                cam.configure({"shutterspeed": labels[cur]})
                cam.wait_ready()                   # ensure it's ready before firing
                tj, tr, td = _grab(f"_reshoot{k}")
            except CameraError as e:
                print(f"[reshoot] capture failed at {labels[cur]}, keeping best: "
                      f"{friendly(str(e))}", flush=True)
                break
            ts = jpegstats.luma_stats(tj)
            tries.append({"shutter": labels[cur], "status": (ts or {}).get("status"),
                          "mean": (ts or {}).get("mean")})
            sc = _exp_score(ts)
            if sc < best["score"]:
                best = {"jpg": tj, "raw": tr, "derived": td, "stats": ts, "score": sc}
            if (ts or {}).get("status") == "ok":
                break
        if cur != orig_idx:                        # restore baseline for next slide
            try:
                cam.configure({"shutterspeed": labels[orig_idx]})
            except CameraError:
                pass                               # best-effort; next capture retries
    finally:
        cam.retries, cam.backoff = prev
    # Promote the winner onto the real stem (if a reshoot won) and clear temps.
    if best["jpg"] is not None and best["jpg"].stem != stem:
        pj, pr, pd = _promote_reshoot(best["jpg"].stem, stem, best["derived"])
        best["jpg"], best["raw"], best["derived"] = pj, pr, pd
    _cleanup_reshoot()
    return best["jpg"], best["raw"], best["derived"], best["stats"], tries


def _promote_reshoot(tmp_stem: str, stem: str, derived: bool):
    """Replace stem.* with the winning tmp_stem.* files. Returns (jpg, raw, derived)."""
    for f in list(OUT_DIR.glob(f"{stem}.*")):      # remove the losing originals
        if f.stem == stem:
            try:
                f.unlink()
            except OSError:
                pass
    jpg = raw = None
    for f in sorted(OUT_DIR.glob(f"{tmp_stem}.*")):
        if f.stem != tmp_stem:
            continue
        dest = OUT_DIR / (stem + f.suffix)
        try:
            f.replace(dest)
        except OSError:
            continue
        ext = dest.suffix.lower()
        if ext in IMAGE_EXTS:
            jpg = dest
        elif ext in RAW_EXTS:
            raw = dest
    return jpg, raw, derived


def _cleanup_reshoot() -> None:
    _wipe("_reshoot*.*")


def do_capture(on_shutter=None) -> dict:
    """Capture one frame into the current group. Assumes cam_lock held.

    Only the camera work happens here. Metadata embedding and any brightness
    correction are handed to the post-capture worker, because the operator is
    waiting on this function and on nothing that follows it."""
    global LAST_USB_DONE
    t0 = time.monotonic()
    prefix, n = PREFIX, next_index(PREFIX)
    stem = f"{prefix}_{n:04d}"
    t_idx = time.monotonic()
    # The 400D drops off the bus for ~1-2s after each SDRAM capture, so firing
    # blind right behind the previous slide downloads nothing and forces a slow
    # wait-then-refire recovery. Probe only when the last shot was recent enough
    # for that to be possible — the probe is a whole gphoto2 session (seconds on
    # a Pi Zero W), and paying it before every shot bought nothing at the
    # operator's actual pace. If the camera is unexpectedly away, _grab's
    # existing retry still recovers.
    if t0 - LAST_USB_DONE < READY_PROBE_WINDOW:
        cam.wait_ready(settle=0)
    t_ready = time.monotonic()
    jpg, raw, derived = _grab(stem, on_shutter=on_shutter)
    t_shot = time.monotonic()
    stats = jpegstats.luma_stats(jpg)
    reshoots = []
    if RESHOOT["enabled"] and stats and stats.get("status") in _FLAGGED:
        jpg, raw, derived, stats, reshoots = _auto_reshoot(
            stem, jpg, raw, derived, stats)
    # The camera is done talking to USB as of here. The sensor trigger uses this
    # to recognise the re-enumeration transient that lands right about now.
    LAST_USB_DONE = t_meter = time.monotonic()
    if stats:                                      # cache verdict for Review
        update_exposure(jpg.name, stats.get("status"))
    # Digital brightness correction, planned now (the response reports the EV)
    # and applied by the worker. Planned AFTER any optical reshoot, so it only
    # has to fix what the shutter couldn't — and it never touches the RAW.
    corr = plan_brightness(jpg, stats, derived)
    # One job per frame: correction (if any) then metadata, off the camera lock.
    # Token the file as it is NOW so the worker refuses to swap if Redo-last (or
    # a delete) replaced this frame while it sat in the queue.
    _post_q.put((jpg, raw, corr, brightness.identity(jpg) if corr else None))
    # Auto-advance to the next slide (no-op unless enabled). The image is
    # already saved, so a failed advance is reported, not fatal.
    adv = _advance_once() if (advancer.enabled and ADVANCE.get("after_capture")) \
        else None
    # Keep the UI's camera pill alive during a hands-off run. Cheap no-op unless
    # the cache is stale, and always after the shutter, so it costs no latency
    # on the frame that pays for it.
    _refresh_cam_status()
    res = {"ok": True, "name": jpg.name, "index": n, "count": image_count(prefix),
           "raw": raw.name if raw else None, "preview_from_raw": derived,
           "exposure": stats, "advance": adv, "reshoots": reshoots,
           "brightness": corr}
    end = time.monotonic()
    print(f"[capture] {jpg.name}: scan={t_idx - t0:.1f}s "
          f"probe={t_ready - t_idx:.1f}s shot={t_shot - t_ready:.1f}s "
          f"meter={t_meter - t_shot:.1f}s tail={end - t_meter:.1f}s "
          f"total={end - t0:.1f}s", flush=True)
    return res


def do_advance() -> dict:
    """Manual one-slide advance (test button / decoupled from capture)."""
    if not advancer.enabled:
        return {"ok": False, "error": "auto-advance is off"}
    return _advance_once()


def do_test() -> dict:
    """Throwaway setup shot to dial in exposure (not counted). Lock held."""
    jpg, _raw, _d = _grab("_test")
    return {"ok": True, "name": jpg.name, "exposure": jpegstats.luma_stats(jpg)}


def _shutter_seconds(s: str):
    """Parse a shutter label ('1/30', '0.5', '4') to seconds; None if not a
    plain speed (bulb/auto/blank)."""
    s = (s or "").strip().lower()
    if not s or s in ("bulb", "auto"):
        return None
    try:
        if "/" in s:
            a, b = s.split("/", 1)
            return float(a) / float(b)
        return float(s)
    except (ValueError, ZeroDivisionError):
        return None


def auto_expose(max_steps: int = 6) -> dict:
    """Dial in the shutter automatically: shoot a test, read the exposure aid,
    step the shutter one stop brighter (longer) if dark / darker (shorter) if
    over, and repeat until the verdict is 'ok' (or we hit a limit/edge). Leaves
    the camera on the chosen shutter. Assumes cam_lock held.

    Note: the exposure aid meters the whole frame, so a wide black slide-mount
    border biases it dark — tighten framing for best results.

    The 400D re-enumerates on the USB bus after each SDRAM capture, so the very
    next gphoto2 command (here: changing the shutter) can land mid-reset and
    fail with a transient claim/IO error. A single capture never sees this (no
    command follows immediately), but this back-to-back loop does — so we settle
    briefly after each shot and make the camera extra patient for the duration
    (safe: cam_lock is held, so nothing else touches the camera)."""
    prev_retries, prev_backoff = cam.retries, cam.backoff
    cam.retries = max(cam.retries, 6)
    cam.backoff = max(cam.backoff, 1.2)
    try:
        full = cam.get_config_full(["shutterspeed"]).get("shutterspeed", {})
        ladder = sorted(((sec, c) for c in full.get("choices", [])
                         if (sec := _shutter_seconds(c)) is not None),
                        key=lambda x: x[0])
        labels = [c for _, c in ladder]
        if len(labels) < 2:
            return {"ok": False, "error": "shutter isn't adjustable — is the "
                    "mode dial on M?"}
        current = full.get("current", "")
        if current in labels:
            idx = labels.index(current)
        else:                                      # start from the nearest speed
            cs = _shutter_seconds(current) or ladder[0][0]
            idx = min(range(len(ladder)), key=lambda i: abs(ladder[i][0] - cs))
        steps, tested, status = [], set(), None
        for _ in range(max_steps):
            jpg, _r, _d = _grab("_test")
            cam.wait_ready()                       # wait for the camera, not a
                                                   # blind sleep, before the next
            stats = jpegstats.luma_stats(jpg) or {}
            status = stats.get("status")
            steps.append({"shutter": labels[idx], "mean": stats.get("mean"),
                          "status": status})
            tested.add(idx)
            if status == "ok" or status is None:
                break
            if status in ("dark", "under"):
                nxt = idx + 1                       # longer exposure -> brighter
            elif status in ("over", "bright"):
                nxt = idx - 1                       # shorter -> darker
            else:
                break
            if not (0 <= nxt < len(labels)) or nxt in tested:
                break                              # at an edge, or oscillating
            idx = nxt
            cam.configure({"shutterspeed": labels[idx]})
        return {"ok": True, "final_shutter": labels[idx], "status": status,
                "steps": steps, "name": "_test.jpg",
                "exposure": jpegstats.luma_stats(OUT_DIR / "_test.jpg")}
    finally:
        cam.retries, cam.backoff = prev_retries, prev_backoff


def _group_state() -> dict:
    """The half of the status that needs no camera (and so no lock)."""
    return {"prefix": PREFIX, "count": image_count(PREFIX),
            "recent": recent_images(PREFIX), "captions": load_captions(),
            "exposure": load_exposure(), "reshoot": dict(RESHOOT),
            "brightness": public_brightness()}


def _refresh_cam_status(force: bool = False) -> None:
    """Top up the cached camera fields. Call ONLY with cam_lock held and only
    after the shutter has fired, so it can never delay an exposure.

    This is how the UI keeps showing a live camera during a hands-off run: the
    background poll deliberately never touches the camera while the trigger is
    armed, so without this the pill would sit on "no camera" for the whole batch
    (which is exactly what it did when the poll was first made lock-free)."""
    global _CAM_STATUS, _CAM_STATUS_AT
    now = time.monotonic()
    # We just used the camera, so at minimum it is present.
    if _CAM_STATUS.get("connected") is not True:
        _CAM_STATUS = {**_CAM_STATUS, "connected": True}
        _CAM_STATUS.pop("error", None)
    if not force and now - _CAM_STATUS_AT < STATUS_REFRESH_S:
        return
    try:
        v = cam.get_many(["batterylevel", "autoexposuremode", "availableshots",
                          "iso", "aperture", "shutterspeed", "imageformat"])
    except CameraError:
        return                                     # keep the last good values
    mode = v.get("autoexposuremode", "?")
    _CAM_STATUS = {
        "connected": True, "model": _CAM_STATUS.get("model", "camera"),
        "battery": v.get("batterylevel", "?"), "mode": mode,
        "manual": mode.lower() == "manual", "shots": v.get("availableshots", "?"),
        "iso": v.get("iso", "?"), "aperture": v.get("aperture", "?"),
        "shutter": v.get("shutterspeed", "?"), "format": v.get("imageformat", "?"),
    }
    _CAM_STATUS_AT = now


def read_status_cached() -> dict:
    """Status with the camera fields served from the last successful read.

    Used for the UI's background poll while the sensor trigger is armed. That
    poll used to take cam_lock and hold it for two gphoto2 sessions; an edge
    landing in that window sat waiting behind it, which on a rig where a moving
    pusher must beat the shutter is a real lost frame — and it was invisible in
    the journal, because the trigger logs "capturing" BEFORE it waits for the
    lock. The camera fields (battery, mode, shot count, exposure settings) barely
    change during a run, so a cached copy costs the operator nothing."""
    age = (time.monotonic() - _CAM_STATUS_AT) if _CAM_STATUS_AT else None
    return {**_CAM_STATUS, **_group_state(), "cached": True,
            "cached_age_s": None if age is None else round(age)}


def read_status() -> dict:
    """Light status: one batched gphoto2 call. Assumes cam_lock held."""
    global _CAM_STATUS, _CAM_STATUS_AT
    try:
        model = cam.model()
        v = cam.get_many(["batterylevel", "autoexposuremode", "availableshots",
                          "iso", "aperture", "shutterspeed", "imageformat"])
    except CameraError as e:
        _CAM_STATUS = {"connected": False, "error": friendly(str(e))}
        return {**_CAM_STATUS, **_group_state()}
    mode = v.get("autoexposuremode", "?")
    _CAM_STATUS = {
        "connected": True, "model": model,
        "battery": v.get("batterylevel", "?"), "mode": mode,
        "manual": mode.lower() == "manual", "shots": v.get("availableshots", "?"),
        "iso": v.get("iso", "?"), "aperture": v.get("aperture", "?"),
        "shutter": v.get("shutterspeed", "?"), "format": v.get("imageformat", "?"),
    }
    _CAM_STATUS_AT = time.monotonic()
    return {**_CAM_STATUS, **_group_state()}


def read_settings() -> dict:
    """Full config with choices for the settings drawer. Assumes cam_lock held."""
    try:
        full = cam.get_config_full(EXPOSURE_KEYS)
    except CameraError as e:
        return {"connected": False, "error": friendly(str(e)), "prefix": PREFIX,
                "have_exiftool": HAVE_EXIFTOOL, "advance": ADVANCE}
    return {"connected": True, "prefix": PREFIX, "fields": full,
            "have_exiftool": HAVE_EXIFTOOL, "advance": ADVANCE}


def apply_settings(body: dict) -> dict:
    """Apply exposure keys + prefix + advance config. Assumes cam_lock held."""
    global PREFIX
    cam_settings = {k: str(body[k]) for k in EXPOSURE_KEYS if body.get(k)}
    if cam_settings:
        cam.configure(cam_settings)
        # capturetarget is cached and normally sent once per session; a config
        # write is the kind of thing that could disturb it, so re-send it on the
        # next capture rather than assume it survived.
        cam.forget_capturetarget()
    if "prefix" in body:
        PREFIX = sanitize_prefix(body["prefix"])
    adv_err = None
    if isinstance(body.get("advance"), dict):
        try:
            set_advance(body["advance"])
        except AdvanceError as e:
            adv_err = str(e)                   # report, don't 500 or corrupt state
    out = read_settings()
    if adv_err:
        out["advance_error"] = adv_err
    return out


def _mtime(f: Path) -> int:
    try:
        return int(f.stat().st_mtime)
    except OSError:
        return 0


def read_images(offset: int, limit: int) -> dict:
    """Paginated listing of the current group for Review (no camera needed).
    Each item carries `mtime` so the UI can cache-bust /thumb and /media: group
    filenames are reused (numbering restarts after a delete-all), so a new frame
    can land on an old name — without the mtime in the URL the browser serves the
    stale cached image."""
    imgs = group_images(PREFIX)                    # newest first
    caps, ex = load_captions(), load_exposure()
    page = imgs[offset:offset + limit]
    items = [{"name": f.name, "caption": caps.get(f.name, ""),
              "exposure": ex.get(f.name, ""), "mtime": _mtime(f),
              "orig": brightness.has_original(f)} for f in page]
    return {"prefix": PREFIX, "total": len(imgs), "offset": offset,
            "limit": limit, "items": items}


# ---- self-update (git-based; updates the app only, not the OS) -------------
APP_DIR = Path(__file__).resolve().parent
SERVICE_NAME = "slidescanner"
UPDATE_URL = "https://github.com/thatSFguy/pictureSlideCapture"


def _git_cmd(args: list[str]) -> list[str]:
    """Build a git argv. Uses `sudo -n` when the repo isn't writable by us (the
    Pi runs the app unprivileged against a root-owned repo; the appliance user
    has passwordless sudo). `env GIT_TERMINAL_PROMPT=0` guarantees git never
    blocks on a credential prompt — it fails fast instead (survives sudo's env
    reset, unlike passing env= to subprocess)."""
    prefix = [] if os.access(str(APP_DIR / ".git"), os.W_OK) else ["sudo", "-n"]
    return prefix + ["env", "GIT_TERMINAL_PROMPT=0",
                     "git", "-C", str(APP_DIR), *args]


def _git(args: list[str], timeout: float = 90.0) -> str:
    """Run git in the app repo; raise RuntimeError on error."""
    try:
        r = subprocess.run(_git_cmd(args), capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(str(e))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "git failed").strip())
    return r.stdout.strip()


def _git_ok(args: list[str], timeout: float = 30.0) -> bool:
    """Run git for its exit status only (no raise) — e.g. is-ancestor tests."""
    try:
        r = subprocess.run(_git_cmd(args), capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def app_version() -> str:
    try:
        return _git(["describe", "--tags", "--always", "--dirty"])
    except Exception:
        return "unknown"


def _current_tag() -> str | None:
    try:
        return _git(["describe", "--tags", "--exact-match"])
    except Exception:
        return None


def update_check() -> dict:
    """Fetch tags and report whether a newer release tag exists (needs net)."""
    _git(["fetch", "--tags", "--force", "--quiet", "origin"], timeout=60)
    tags = _git(["tag", "--list", "v*", "--sort=-v:refname"])
    latest = tags.splitlines()[0].strip() if tags else None
    cur = _current_tag()
    # "available" only if HEAD doesn't already contain the latest tag — avoids
    # nagging a build that's on or ahead of it (e.g. an untagged dev commit).
    available = bool(latest) and not _git_ok(
        ["merge-base", "--is-ancestor", latest, "HEAD"])
    return {"ok": True, "current": app_version(), "current_tag": cur,
            "latest": latest, "available": available, "url": UPDATE_URL}


_APP_MODULES = ["capture_server.py", "camera.py", "jpegstats.py", "advance.py",
                "brightness.py", "trigger.py", "gpiocli.py"]


def _app_syntax_ok() -> tuple[bool, str]:
    """Parse the app's modules without importing/writing — catches a bad commit
    that would otherwise crash-loop the service after an update."""
    import ast
    for m in _APP_MODULES:
        p = APP_DIR / m
        try:
            ast.parse(p.read_text(), filename=str(p))
        except (SyntaxError, OSError) as e:
            return False, f"{m}: {e}"
    return True, ""


def update_apply() -> dict:
    """Check out the latest release tag, validate it, then restart. Rolls back
    if the new code doesn't even parse, so a bad release can't brick the box."""
    info = update_check()
    latest = info["latest"]
    if not latest:
        return {"ok": False, "error": "no release tags found on origin"}
    if not info["available"]:
        return {"ok": False, "current": info["current"],
                "error": f"already up to date ({info['current']})"}
    prev = _git(["rev-parse", "HEAD"])               # for rollback
    _git(["checkout", "--force", latest], timeout=60)
    ok, why = _app_syntax_ok()
    if not ok:
        _git(["checkout", "--force", prev], timeout=60)   # roll back, don't restart
        print(f"[update] {latest} failed validation, rolled back: {why}", flush=True)
        return {"ok": False, "error": f"update to {latest} aborted (won't start): "
                f"{why}. Rolled back to {info['current']}."}
    threading.Timer(1.2, _restart_service).start()   # let this response flush
    return {"ok": True, "from": info["current"], "to": latest, "restarting": True}


def _restart_service() -> None:
    # Hand the camera back before systemd kills us: a persistent gphoto2 session
    # is a child process holding the USB device, and the replacement service
    # starts within a second or two.
    cam.shutdown()
    subprocess.run(["sudo", "-n", "systemctl", "restart", SERVICE_NAME],
                   capture_output=True, text=True)


# ---- optional-feature install (no SSH needed) ------------------------------
# The appliance is a sealed box: the operator has no shell and no sudo, and the
# in-app self-update pulls CODE from git but can't pull apt packages. So an
# optional native dependency (python3-pil, for brightness correction) would
# otherwise need a full re-flash. The service user already has passwordless
# sudo — the same one behind "View logs" and the updater's restart — so the app
# can install it on request instead.
#
# SECURITY: the request names a FEATURE from this fixed table, never a package.
# Nothing user-supplied ever reaches the apt command line. (When the sudoers
# allowlist hardening lands, apt-get needs an entry here too.)
APT_FEATURES = {
    "brightness": {"packages": ["python3-pil"],
                   "label": "brightness correction",
                   "verify": brightness.available,
                   "why": brightness.import_error,
                   "import": "PIL.Image"},
}

# NOTE: the install RESULT is "success", not "ok" — every JSON response here
# already uses "ok" for "the request itself worked", and merging the two
# silently overwrote it (a started install replied {"ok": null}).
_install = {"running": False, "feature": None, "success": None, "log": "",
            "started": 0.0, "restarting": False}
_install_lock = threading.Lock()


def _status_from(d: dict) -> dict:
    """Shape a status snapshot. Callers hold (or don't need) _install_lock —
    this must NOT take it, or a caller already holding it self-deadlocks."""
    out = dict(d)
    out["elapsed"] = round(time.monotonic() - d["started"], 1) if d["started"] else 0
    out["log"] = d["log"][-4000:]               # tail only; the UI shows a slice
    return out


def install_status() -> dict:
    with _install_lock:
        return _status_from(_install)


def _log_install(text: str, journal: bool = True) -> None:
    """Record installer output for the UI AND (by default) the journal.

    The journal is the only diagnostic an operator can actually export from a
    sealed box — an in-memory-only log dies with the process and is invisible to
    "View logs", which is where anyone debugging a failed install will look."""
    with _install_lock:
        _install["log"] += text
    if journal:
        for line in text.strip().splitlines():
            if line.strip():
                print(f"[install] {line}", flush=True)


def _fresh_import_ok(module: str) -> tuple[bool, str]:
    """Can a BRAND-NEW interpreter import `module`? Decides whether a failed
    in-process import means "we're stale, restart" or "the library is broken"."""
    try:
        r = subprocess.run([sys.executable, "-c", f"import {module}"],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if r.returncode == 0:
        return True, ""
    lines = (r.stderr or "").strip().splitlines()
    return False, (lines[-1] if lines else f"exited {r.returncode}")


def _install_worker(feature: str, spec: dict) -> None:
    """apt-get update + install, then re-check. Runs off the request thread:
    on a Pi Zero W over WiFi this is minutes, far past any browser timeout, so
    the UI starts it and polls GET /api/install instead of waiting."""
    ok = restarting = False
    try:
        for label, cmd, timeout in (
                ("Refreshing the package list",
                 ["sudo", "-n", "apt-get", "update"], 600),
                (f"Installing {' '.join(spec['packages'])}",
                 ["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive",
                  "apt-get", "install", "-y", "--no-install-recommends",
                  *spec["packages"]], 1800)):
            _log_install(f"\n=== {label} ===\n")
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=timeout)
            except (OSError, subprocess.TimeoutExpired) as e:
                _log_install(f"FAILED: {e}\n")
                return
            _log_install((r.stdout or "") + (r.stderr or ""))
            if r.returncode != 0:
                _log_install(f"\napt exited {r.returncode}\n")
                return
        # A package installed after startup is invisible until the import
        # machinery re-scans dist-packages; no service restart needed.
        import importlib
        importlib.invalidate_caches()
        ok = bool(spec["verify"]())
        if ok:
            _log_install("\n=== Installed and working ===\n")
        else:
            # apt said yes but THIS process still can't import it. Two very
            # different causes, so ask a FRESH interpreter to decide rather than
            # guessing: if a new process imports it fine, only our long-running
            # one is stale (invalidate_caches() does not reliably pick up a
            # package that appeared after startup) and a restart fixes it. If a
            # fresh process fails too, the library itself is broken (missing
            # shared lib, ABI mismatch) and restarting would change nothing.
            why = spec.get("why", lambda: "")() or "reason unknown"
            fresh_ok, fresh_err = _fresh_import_ok(spec["import"])
            if not fresh_ok:
                _log_install(f"\n=== apt succeeded but the library will not load "
                             f"in a fresh process either — restarting will not "
                             f"help. In-process: {why}. Fresh: {fresh_err} ===\n")
            elif _lock_acquire("install restart", 30):
                _log_install("\n=== Installed. A fresh process imports it fine, "
                             "so this one is just stale — restarting now ===\n")
                ok, restarting = True, True
                threading.Timer(1.5, _restart_service).start()
            else:
                _log_install(f"\n=== Installed and working, but a capture is in "
                             f"progress so the restart that activates it was "
                             f"skipped — restart when the batch finishes "
                             f"(busy: {lock_status()}) ===\n")
    except Exception as e:                      # never die silently in a thread
        _log_install(f"\n=== installer crashed: {type(e).__name__}: {e} ===\n")
    finally:
        with _install_lock:
            _install["running"], _install["success"] = False, ok
            _install["restarting"] = restarting
        print(f"[install] {feature}: {'ok' if ok else 'FAILED'}", flush=True)


def start_install(feature: str) -> dict:
    spec = APT_FEATURES.get(feature)
    if not spec:
        return {"ok": False, "error": "unknown feature"}
    if spec["verify"]():
        return {"ok": True, "already": True, **install_status()}
    held = lock_status()                        # don't steal CPU mid-capture
    if held:
        return {"ok": False, "error": f"camera busy ({held.get('who')}) — "
                "try again when the batch is finished"}
    with _install_lock:
        if _install["running"]:                 # already going: report, don't stack
            return {"ok": True, **_status_from(_install)}
        _install.update({"running": True, "feature": feature, "success": None,
                         "log": f"Installing {spec['label']}…\n",
                         "started": time.monotonic(), "restarting": False})
        started = _status_from(_install)
    threading.Thread(target=_install_worker, args=(feature, spec),
                     name="install", daemon=True).start()
    return {"ok": True, **started}


# ---- diagnostics (in-app troubleshooting, no SSH needed) -------------------
_DIAG_KEYS = ["capturetarget", "imageformat", "autoexposuremode",
              "availableshots", "batterylevel"]


def read_logs(lines: int = 300) -> dict:
    """Tail the service journal for in-app troubleshooting on the appliance."""
    n = max(20, min(2000, lines))
    try:
        r = subprocess.run(["sudo", "-n", "journalctl", "-u", SERVICE_NAME,
                            "-n", str(n), "--no-pager", "-o", "short-iso"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": str(e)}
    if r.returncode == 0:
        return {"ok": True, "source": f"journalctl -u {SERVICE_NAME} -n {n}",
                "text": r.stdout.strip() or "(no log lines)"}
    return {"ok": False,
            "error": (r.stderr or r.stdout or "journalctl unavailable").strip()}


def read_diag() -> dict:
    """System + live camera snapshot (incl. the actual capturetarget). Lock held."""
    import platform
    import sys
    d = {"version": app_version(), "python": sys.version.split()[0],
         "platform": platform.platform(), "out_dir": str(OUT_DIR.resolve()),
         "prefix": PREFIX, "have_exiftool": HAVE_EXIFTOOL,
         "advance_mode": ADVANCE.get("mode"),
         "trigger": public_trigger(), "reshoot": dict(RESHOOT),
         "brightness": public_brightness(), "camera_lock": lock_status()}
    try:                                        # a full card fails apt AND captures
        du = shutil.disk_usage(OUT_DIR)
        d["disk"] = {"free_mb": du.free // 1048576, "total_mb": du.total // 1048576,
                     "used_pct": round(100 * (du.total - du.free) / du.total)}
    except OSError:
        d["disk"] = None
    try:
        d["gphoto2"] = (subprocess.run(["gphoto2", "--version"],
                        capture_output=True, text=True, timeout=10)
                        .stdout.splitlines() or ["?"])[0]
    except (OSError, subprocess.TimeoutExpired):
        d["gphoto2"] = "?"
    try:
        full = cam.get_config_full(_DIAG_KEYS)
        d["camera_connected"] = True
        d["camera"] = {k: full.get(k, {}).get("current", "?") for k in _DIAG_KEYS}
        d["capturetarget_choices"] = full.get("capturetarget", {}).get("choices", [])
    except CameraError as e:
        d["camera_connected"] = False
        d["camera_error"] = friendly(str(e))
    # Last, so it reflects the session AFTER the camera reads above rather than
    # whatever was true before this function touched the camera.
    d["persistent_session"] = {
        "enabled": cam.persistent, "live": cam._shell is not None,
        "given_up": cam._shell_off, **cam.shell_stats}
    return d


def debug_capture() -> dict:
    """Try several gphoto2 capture variants and report which one actually
    writes a file locally. Diagnostic only. Assumes cam_lock held."""
    dst = str(OUT_DIR / "_dbg.%C")
    variants = [
        ("A_sdram_fname_first",
         ["--set-config-value", "capturetarget=Internal RAM",
          "--filename", dst, "--force-overwrite", "--capture-image-and-download"]),
        ("B_no_target_fname_first",
         ["--filename", dst, "--force-overwrite", "--capture-image-and-download"]),
        ("C_no_filename_cwd",
         ["--force-overwrite", "--capture-image-and-download"]),
        ("D_card_keep",
         ["--set-config-value", "capturetarget=Memory card",
          "--filename", dst, "--force-overwrite",
          "--capture-image-and-download", "--keep"]),
        ("E_wait_event",
         ["--set-config-value", "capturetarget=Internal RAM",
          "--filename", dst, "--force-overwrite",
          "--capture-image-and-download", "--wait-event-and-download=FILEADDED"]),
    ]
    results = []
    for name, args in variants:
        for f in list(OUT_DIR.glob("_dbg.*")):     # clean before each try
            try: f.unlink()
            except OSError: pass
        before = {p.name for p in OUT_DIR.iterdir() if p.is_file()}
        try:
            r = subprocess.run(["gphoto2", *args], capture_output=True,
                               text=True, cwd=str(OUT_DIR), timeout=90)
            rc, so, se = r.returncode, r.stdout, r.stderr
        except (OSError, subprocess.TimeoutExpired) as e:
            rc, so, se = -1, "", str(e)
        after = {p.name for p in OUT_DIR.iterdir() if p.is_file()}
        new = sorted(after - before)
        results.append({"variant": name, "rc": rc, "new_files": new,
                        "stdout": so.strip()[-300:], "stderr": se.strip()[-300:]})
    for f in list(OUT_DIR.glob("_dbg.*")):          # cleanup
        try: f.unlink()
        except OSError: pass
    return {"ok": True, "results": results,
            "winner": next((r["variant"] for r in results if r["new_files"]), None)}


# ---- HTTP handler ---------------------------------------------------------

class QuietServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that doesn't dump a traceback when a client hangs up
    mid-response. A phone locking its screen or a tab closing during a poll is
    routine, but stock socketserver logs ~30 lines of BrokenPipeError for each
    one — and the journal is the operator's ONLY diagnostic on this appliance, so
    filling it with noise actively costs us. Real errors still print."""

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, val in (extra or {}).items():
            self.send_header(k, val)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    def _with_camera(self, fn, wait: float = 6.0, label: str | None = None):
        # User-initiated camera ops wait briefly for the lock, so a background
        # status poll (which holds it ~1-2s) doesn't turn a click into a
        # spurious "camera busy". Background polls pass wait=0 to fail fast and
        # skip silently.
        who = label or getattr(fn, "__name__", None) or "camera op"
        if not _lock_acquire(who, wait):
            return _busy_json(self)
        try:
            self._json(fn())
        except CameraError as e:
            self._json({"ok": False, "error": friendly(str(e))}, 500)
        finally:
            _lock_release()

    def _guarded_update(self, check_only: bool):
        # Serialize against capture (same lock) so we never restart mid-shot.
        # Wait a few seconds so a shot in flight doesn't instantly block a manual
        # update, but the tracked holder is reported if it stays busy.
        if not _lock_acquire("update", 5):
            return _busy_json(self, "busy — finish the capture first")
        try:
            self._json(update_check() if check_only else update_apply())
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)
        finally:
            _lock_release()

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path == "/":
            self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
        elif path == "/api/status":
            # While the sensor trigger is armed, never touch the camera for a
            # background poll: it would hold cam_lock for two gphoto2 sessions
            # and a slide arriving in that window waits behind it. On this rig
            # the pusher keeps moving, so a delayed shutter is a spoiled frame.
            if trigger is not None and trigger.enabled:
                self._json(read_status_cached())
            else:
                self._with_camera(read_status, wait=0)   # poll: fail fast, skip
        elif path == "/api/settings":
            self._with_camera(read_settings)
        elif path == "/api/zip":
            self._serve_zip(originals=q.get("originals", ["1"])[0] != "0")
        elif path == "/api/version":
            self._json({"version": app_version()})
        elif path == "/api/update":
            self._guarded_update(check_only=True)
        elif path == "/api/logs":
            try:
                n = int(q.get("lines", ["300"])[0])
            except ValueError:
                n = 300
            self._json(read_logs(n))
        elif path == "/api/diag":
            self._with_camera(read_diag)   # camera snapshot needs the lock
        elif path == "/api/trigger":
            self._json(read_sensor())      # config + live level (for polarity)
        elif path == "/api/brightness":
            self._json({"ok": True, "brightness": public_brightness()})
        elif path == "/api/install":
            self._json({**install_status(), "brightness": public_brightness()})
        elif path == "/api/exposure":
            f = self._safe(unquote(q.get("name", [""])[0]))
            stats = jpegstats.luma_stats(f) if f and f.is_file() else None
            self._json(stats or {})
        elif path == "/api/images":
            try:
                offset = max(0, int(q.get("offset", ["0"])[0]))
                limit = min(200, max(1, int(q.get("limit", ["60"])[0])))
            except ValueError:
                offset, limit = 0, 60
            self._json(read_images(offset, limit))
        elif path == "/api/groups":
            self._json({"ok": True, "current": PREFIX, "groups": list_groups()})
        elif path.startswith("/thumb/"):
            self._serve_thumb(unquote(path[len("/thumb/"):]))
        elif path.startswith("/media/"):
            self._serve_media(unquote(path[len("/media/"):]), "dl" in q,
                              orig="orig" in q)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/capture":
            self._with_camera(do_capture)
        elif path == "/api/test":
            self._with_camera(do_test)
        elif path == "/api/autoexpose":
            self._with_camera(auto_expose, wait=10)   # multi-shot; holds lock
        elif path == "/api/advance":
            self._with_camera(do_advance)   # lock: never advance mid-capture
        elif path == "/api/trigger":
            body = self._body()
            try:
                set_trigger(body)
                self._json({"ok": True, "trigger": public_trigger()})
            except TriggerError as e:
                self._json({"ok": False, "error": str(e),
                            "trigger": public_trigger()}, 400)
        elif path == "/api/reshoot":
            self._json({"ok": True, "reshoot": set_reshoot(self._body())})
        elif path == "/api/brightness":
            set_brightness(self._body())
            b = public_brightness()
            self._json({"ok": True, "brightness": b,
                        "warning": None if (b["available"] or not b["enabled"])
                        else brightness.unavailable_reason()})
        elif path == "/api/revert":
            self._revert(self._body().get("name", ""))
        elif path == "/api/install":
            self._json(start_install(self._body().get("feature", "")))
        elif path == "/api/deleteall":
            self._delete_all(self._body().get("prefix", ""))
        elif path == "/api/deletegroup":
            self._delete_group(self._body().get("prefix", ""))
        elif path == "/api/debugcapture":
            self._with_camera(debug_capture, wait=15)   # diagnostic (alpha)
        elif path == "/api/update":
            self._guarded_update(check_only=False)
        elif path == "/api/settings":
            body = self._body()
            self._with_camera(lambda: apply_settings(body))
        elif path == "/api/delete":
            self._delete(self._body().get("name", ""))
        elif path == "/api/preset":
            name = self._body().get("name", "")
            if name not in PRESETS:
                return self._json({"ok": False, "error": "unknown preset"}, 400)
            self._with_camera(lambda: apply_settings(PRESETS[name]))
        elif path == "/api/caption":
            self._caption(self._body())
        else:
            self._send(404, b"not found", "text/plain")

    # -- file helpers (no camera lock needed) ------------------------------

    def _safe(self, name: str) -> Path | None:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return None
        return OUT_DIR / name

    def _serve_media(self, name: str, download: bool, orig: bool = False):
        f = self._safe(name)
        # ?orig=1 serves the pre-correction copy so Review can compare before
        # reverting. _safe() already rejected any path separator, so this can
        # only ever resolve inside captures/originals/.
        if f is not None and orig:
            f = brightness.original_for(f)
        if f is None or not f.is_file():
            return self._send(404 if f else 403, b"not found", "text/plain")
        ctype = "image/jpeg" if f.suffix.lower() in IMAGE_EXTS \
            else "application/octet-stream"
        extra = {"Content-Disposition": f'attachment; filename="{name}"'} \
            if download else None
        self._send(200, f.read_bytes(), ctype, extra)

    def _serve_thumb(self, name: str):
        """Fast tiny thumbnail for the Review grid: embedded EXIF thumbnail if
        present (reuses jpegstats._find_thumbnail), else the full image."""
        f = self._safe(name)
        if f is None or not f.is_file():
            return self._send(404 if f else 403, b"not found", "text/plain")
        thumb = None
        if f.suffix.lower() in IMAGE_EXTS:
            with open(f, "rb") as fh:               # EXIF thumbnail lives early;
                head = fh.read(131072)              # read 128 KB, not the whole file
            thumb = jpegstats._find_thumbnail(head)
        self._send(200, thumb or f.read_bytes(), "image/jpeg")

    def _delete(self, name: str):
        f = self._safe(name)
        if f is None:
            return self._json({"ok": False, "error": "bad name"}, 400)
        removed = []
        for sib in OUT_DIR.glob(f.stem + ".*"):   # jpg + its cr2 sibling
            try:
                brightness.discard_original(sib)  # + its pre-correction original
                sib.unlink()
                removed.append(sib.name)
            except OSError:
                pass
        if removed:                               # drop caption + exposure cache
            caps = load_captions()
            if any(caps.pop(r, None) is not None for r in removed):
                save_captions(caps)
            ex = load_exposure()
            if any(ex.pop(r, None) is not None for r in removed):
                save_exposure(ex)
        self._json({"ok": bool(removed), "removed": removed,
                    "count": image_count(PREFIX), "recent": recent_images(PREFIX)})

    def _revert(self, name: str):
        """Undo a brightness correction: put the stashed original back and
        re-meter. The stash is consumed, so the frame reads as uncorrected."""
        f = self._safe(name)
        if f is None or not f.is_file():
            return self._json({"ok": False, "error": "bad name"}, 400)
        if not brightness.revert(f):
            return self._json({"ok": False,
                               "error": "no original kept for this frame"}, 404)
        write_metadata(f, None, load_captions().get(f.name, ""))
        stats = jpegstats.luma_stats(f)
        update_exposure(f.name, (stats or {}).get("status"))
        self._json({"ok": True, "name": f.name, "exposure": stats,
                    "mtime": _mtime(f)})

    def _delete_all(self, prefix: str):
        """Delete every image in the CURRENT group (+ RAW siblings) and clear its
        caption/exposure caches. Guarded: the client must echo the current prefix
        so a stale tab can't wipe a group it isn't looking at."""
        if sanitize_prefix(prefix) != PREFIX:
            return self._json({"ok": False, "error": "prefix mismatch — reload "
                               "and try again"}, 409)
        removed = delete_group(PREFIX)
        self._json({"ok": True, "removed": len(removed),
                    "count": image_count(PREFIX)})

    def _delete_group(self, prefix: str):
        """Delete a whole group BY NAME, including one that is no longer
        current — changing the group name orphans the old files (invisible in
        the UI, still on disk). Guarded: the name must exactly match a group
        that exists on disk, so a typo or stale tab deletes nothing; the
        sanitize check also keeps glob metacharacters out of delete_group."""
        if (not prefix or sanitize_prefix(prefix) != prefix
                or not any(g["prefix"] == prefix for g in list_groups())):
            return self._json({"ok": False, "error": "no such group"}, 404)
        removed = delete_group(prefix)
        self._json({"ok": True, "prefix": prefix, "removed": len(removed),
                    "current": PREFIX, "groups": list_groups()})

    def _caption(self, body: dict):
        name = body.get("name", "")
        caption = (body.get("caption", "") or "").strip()[:300]
        f = self._safe(name)
        if f is None or not f.is_file():
            return self._json({"ok": False, "error": "bad name"}, 400)
        caps = load_captions()
        if caption:
            caps[name] = caption
        else:
            caps.pop(name, None)
        save_captions(caps)
        jpg = raw = None                           # re-embed on jpg + raw sibling
        for sib in OUT_DIR.glob(f.stem + ".*"):
            if sib.suffix.lower() in IMAGE_EXTS:
                jpg = sib
            elif sib.suffix.lower() in RAW_EXTS:
                raw = sib
        write_metadata(jpg, raw, caption)
        self._json({"ok": True, "name": name, "caption": caption})

    def _serve_zip(self, originals: bool = True):
        rx = name_re(PREFIX)
        files = [f for f in sorted(OUT_DIR.glob(f"{PREFIX}_*")) if rx.match(f.name)]
        if not files:
            return self._send(404, b"no files in group", "text/plain")
        # Pre-correction copies ride along under originals/ — otherwise
        # download-all + delete-all would silently discard them and bake every
        # brightness correction in permanently. ?originals=0 opts out when the
        # doubled zip size matters more.
        extra = [brightness.original_for(f) for f in files] if originals else []
        extra = [f for f in extra if f.is_file()]
        # Build to a temp file on the SAME disk (not /tmp, which is tmpfs/RAM on
        # the Pi), then stream it — avoids buffering a whole batch in 512 MB RAM.
        tmp = tempfile.NamedTemporaryFile(dir=OUT_DIR, suffix=".zip", delete=False)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
                for f in files:
                    z.write(f, f.name)
                for f in extra:
                    z.write(f, f"{brightness.ORIGINALS_DIR}/{f.name}")
            size = tmp.tell()
            tmp.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{PREFIX}.zip"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                while True:
                    chunk = tmp.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        finally:
            tmp.close()
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


# ---- the page (single embedded file) -------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Slide Capture</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text x='50' y='80' font-size='82' text-anchor='middle'>🎞️</text></svg>">
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; height:100dvh; display:flex; flex-direction:column;
         font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
         background:#111; color:#eee; -webkit-tap-highlight-color:transparent; }
  header { display:flex; align-items:center; gap:.6rem; padding:.5rem .8rem;
           background:#181818; border-bottom:1px solid #262626; }
  .brand { font-size:1.1rem; }
  nav { display:flex; gap:.25rem; }
  nav button { background:#242424; border:1px solid #2c2c2c; color:#bbb;
               padding:.35rem .7rem; border-radius:8px; cursor:pointer; font-size:.85rem; }
  nav button.active { background:#2f7bd6; border-color:#2f7bd6; color:#fff; }
  .pill { font-size:.72rem; padding:.15rem .5rem; border-radius:999px;
          background:#242424; color:#bbb; white-space:nowrap; }
  .pill.good{background:#123a1a;color:#9fe6a8} .pill.warn{background:#5a3a00;color:#ffd48a}
  .pill.bad{background:#5a1a1a;color:#ffb0b0}
  #grpcount { margin-left:auto; font-weight:600; font-variant-numeric:tabular-nums;
              white-space:nowrap; font-size:.9rem; }
  main { flex:1; position:relative; overflow:hidden; }
  .view { display:none; position:absolute; inset:0; flex-direction:column; }
  .view.active { display:flex; }

  /* setup */
  #view-setup { overflow-y:auto; padding:0; }
  .setup-wrap { max-width:1060px; margin:0 auto; padding:1.3rem 1.3rem 2.2rem; }
  .setup-grid { display:grid; gap:1rem; grid-template-columns:1fr 1fr; align-items:start; }
  .setup-grid .col { display:flex; flex-direction:column; gap:1rem; min-width:0; }
  @media (max-width:820px){ .setup-grid { grid-template-columns:1fr; } }
  .card { background:#161616; border:1px solid #262626; border-radius:14px;
          padding:.9rem 1.1rem 1.15rem; }
  .card > h3 { margin:0 0 .5rem; font-size:.74rem; letter-spacing:.03em; color:#8ab4e8;
               text-transform:uppercase; font-weight:700; }
  #view-setup label { display:block; font-size:.75rem; color:#aaa; margin:.7rem 0 .2rem; }
  #view-setup select, #view-setup input { width:100%; padding:.55rem; background:#111;
           color:#eee; border:1px solid #333; border-radius:8px; font-size:.95rem; }
  .presetrow { display:flex; gap:.6rem; }
  .presetrow button { flex:1; padding:.9rem; font-size:1rem; font-weight:600;
           border:none; border-radius:12px; color:#fff; cursor:pointer; }
  #p-slides{background:#2f8f5a} #p-negatives{background:#a9642e}
  .row { display:flex; gap:.5rem; } .row input{flex:1}
  .row button, #testShot, #autoExp, #checkUpd, #startCap { border:none; border-radius:8px;
           color:#fff; background:#2f7bd6; padding:.55rem .9rem; cursor:pointer; font-size:.9rem; }
  #testShot { width:100%; padding:.75rem; font-weight:600; }
  #autoExp { width:100%; padding:.7rem; margin-top:.4rem; font-weight:600; background:#6a4ea9; }
  #checkUpd { margin-left:.5rem; background:#333; }
  #startCap { width:100%; padding:1rem; font-size:1.1rem; font-weight:700; margin-top:1.3rem; }
  #testWrap { margin-top:.2rem; background:#0c0c0c; border:1px solid #222;
              border-radius:10px; padding:.6rem; text-align:center; }
  #testImg { max-width:100%; max-height:44vh; border-radius:6px; display:none; margin-top:.5rem; }
  .note { font-size:.72rem; color:#777; margin-top:.4rem; }
  .grplist { margin-top:.6rem; display:flex; flex-direction:column; gap:.3rem; }
  .grow { display:flex; align-items:center; gap:.5rem; background:#111;
          border:1px solid #2a2a2a; border-radius:8px; padding:.3rem .6rem;
          font-size:.85rem; }
  .grow .gname { flex:1; min-width:0; overflow-wrap:anywhere; }
  .grow .gcount { color:#888; font-size:.75rem; white-space:nowrap; }
  .grow button { border:none; border-radius:6px; color:#fff; cursor:pointer;
                 padding:.3rem .6rem; font-size:.8rem; background:#2f7bd6; }
  .grow button.gdel { background:#5a1a1a; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:.5rem; }
  .diagrow { display:flex; gap:.5rem; flex-wrap:wrap; margin-top:.7rem; }
  #diagBtn, #logBtn { background:#333; border:none; border-radius:8px; color:#fff;
           padding:.5rem .8rem; cursor:pointer; font-size:.85rem; }
  #diagOut { display:none; margin-top:.6rem; max-height:44vh; overflow:auto;
           background:#0c0c0c; border:1px solid #262626; border-radius:8px;
           padding:.6rem; font:12px/1.45 ui-monospace,Menlo,Consolas,monospace;
           color:#cbd5e1; white-space:pre-wrap; word-break:break-word; }

  /* capture */
  #stage { flex:1; display:flex; align-items:center; justify-content:center;
           background:#000; position:relative; overflow:hidden; }
  #capImg { max-width:100%; max-height:100%; object-fit:contain; display:none; }
  #capPlace { color:#666; text-align:center; padding:1rem; }
  #chip { position:absolute; top:.6rem; left:.6rem; font-size:.85rem; padding:.3rem .7rem;
          border-radius:999px; display:none; }
  #chip.good{background:#123a1a;color:#9fe6a8} #chip.warn{background:#5a3a00;color:#ffd48a}
  #chip.bad{background:#5a1a1a;color:#ffb0b0}
  #capName { position:absolute; bottom:.6rem; left:.6rem; font-size:.75rem; color:#999;
             background:rgba(0,0,0,.5); padding:.15rem .5rem; border-radius:6px; }
  #spinner { position:absolute; inset:0; display:none; align-items:center;
             justify-content:center; background:rgba(0,0,0,.55); font-size:1.15rem; }
  .dot{width:.6rem;height:.6rem;border-radius:50%;background:#ffd48a;display:inline-block;
       margin-right:.5rem;animation:pulse .9s infinite} @keyframes pulse{0%,100%{opacity:.3}50%{opacity:1}}
  #capBar { padding:.7rem .8rem calc(.7rem + env(safe-area-inset-bottom)); background:#181818;
            border-top:1px solid #262626; display:flex; gap:.6rem; align-items:stretch; }
  #shoot { flex:1; padding:1.1rem; font-size:1.3rem; font-weight:700; border:none;
           border-radius:14px; background:#2f7bd6; color:#fff; cursor:pointer; }
  #shoot:active{background:#255fa6} #shoot:disabled{background:#333;color:#888;cursor:not-allowed}
  #redo { width:33%; border:1px solid #444; border-radius:14px; background:#242424;
          color:#eee; cursor:pointer; font-size:.95rem; }
  #redo:disabled{opacity:.4;cursor:not-allowed}
  .khint { font-size:.66rem; color:#888; display:block; margin-top:.2rem; }
  #capMsg { text-align:center; font-size:.8rem; min-height:1rem; padding:.15rem; }
  #capMsg.err{color:#ff9b9b} #capMsg.ok{color:#9fe6a8}

  /* review */
  #revBar { display:flex; align-items:center; gap:.6rem; padding:.5rem .8rem;
            background:#181818; border-bottom:1px solid #262626; flex-wrap:wrap; }
  #revBar button, #revBar label.tog { background:#242424; border:1px solid #2c2c2c;
            color:#eee; padding:.35rem .7rem; border-radius:8px; cursor:pointer; font-size:.82rem; }
  #revBar .tog input{margin-right:.35rem;vertical-align:middle}
  #revInfo{font-size:.82rem;color:#aaa}
  #gridwrap { flex:1; overflow-y:auto; padding:.5rem; }
  #grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(120px,1fr)); gap:.4rem; }
  .tile { position:relative; aspect-ratio:3/2; background:#000; border-radius:6px;
          overflow:hidden; cursor:pointer; border:2px solid transparent; }
  .tile img { width:100%; height:100%; object-fit:cover; }
  .tile .edot { position:absolute; top:4px; right:4px; width:10px; height:10px; border-radius:50%; }
  .tile .cap { position:absolute; bottom:0; left:0; right:0; font-size:.6rem;
               background:rgba(0,0,0,.6); padding:1px 3px; white-space:nowrap; overflow:hidden;
               text-overflow:ellipsis; }
  .e-ok{background:#4caf50} .e-warn{background:#ffb300} .e-bad{background:#e05252} .e-none{background:#555}
  #loadMore { display:none; width:100%; margin:.6rem 0; padding:.6rem; background:#242424;
              border:1px solid #333; color:#eee; border-radius:8px; cursor:pointer; }
  /* lightbox */
  #lb { position:fixed; inset:0; background:rgba(0,0,0,.92); z-index:20; display:none;
        flex-direction:column; }
  #lb.open{display:flex}
  #lbImgWrap{flex:1;display:flex;align-items:center;justify-content:center;overflow:hidden;position:relative}
  #lbImg{max-width:100%;max-height:100%;object-fit:contain}
  #lbNavL,#lbNavR{position:absolute;top:50%;transform:translateY(-50%);font-size:2rem;
        background:rgba(0,0,0,.4);border:none;color:#fff;padding:.3rem .7rem;cursor:pointer;border-radius:8px}
  #lbNavL{left:.5rem} #lbNavR{right:.5rem}
  #lbBar{background:#181818;padding:.6rem;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
  #lbBar input{flex:1;min-width:140px;padding:.5rem;background:#111;color:#eee;border:1px solid #333;border-radius:8px}
  #lbBar button{background:#2f7bd6;border:none;color:#fff;border-radius:8px;padding:.5rem .8rem;cursor:pointer}
  #lbBar button.del{background:#7a2626} #lbBar #lbClose{background:#333}
  #lbInfo{font-size:.75rem;color:#aaa;width:100%}
  #toast{position:fixed;left:50%;bottom:5rem;transform:translateX(-50%);background:#2a2a2a;
         color:#eee;padding:.5rem 1rem;border-radius:8px;font-size:.85rem;display:none;z-index:30}
  #toast.err{background:#5a1a1a;color:#ffb0b0} #toast.ok{background:#123a1a;color:#9fe6a8}
  #toast.warn{background:#5a3a00;color:#ffd48a}
</style></head>
<body>
<header>
  <span class="brand">🎞️</span>
  <nav>
    <button data-mode="setup">Setup</button>
    <button data-mode="capture">Capture</button>
    <button data-mode="review">Review</button>
  </nav>
  <span id="camstat" class="pill">connecting…</span>
  <span id="grpcount">–</span>
</header>
<main>
  <!-- SETUP -->
  <section id="view-setup" class="view">
   <div class="setup-wrap">
    <div class="setup-grid">
     <div class="col">
      <div class="card">
        <h3>1 · Choose type</h3>
        <div class="presetrow">
          <button id="p-slides">📽 Slides</button>
          <button id="p-negatives">🎞 Negatives</button>
        </div>
        <div class="note">Slides → JPEG (fast). Negatives → RAW+JPEG (archival, for inversion).
          Sets ISO 100, f/8, daylight WB.</div>
      </div>
      <div class="card">
        <h3>2 · Group name (filename prefix)</h3>
        <div class="row">
          <input id="prefix" placeholder="e.g. moms_slides_1972">
          <button id="applyPrefix">Set</button>
        </div>
        <div class="note">Saved as <code><span id="pfxEx">slide</span>_0001</code>, _0002, …
          (written into each image's metadata).</div>
        <div id="grpList" class="grplist"></div>
        <div class="note" id="grpListNote" style="display:none">All groups on this
          device — a renamed group's files stay here. <b>Open</b> switches to a
          group (Review shows it); 🗑 deletes its files.</div>
      </div>
      <div class="card">
        <h3>3 · Fine-tune exposure</h3>
        <div class="grid2">
          <div><label>Format</label><select id="f-imageformat"></select></div>
          <div><label>ISO</label><select id="f-iso"></select></div>
          <div><label>Aperture</label><select id="f-aperture"></select></div>
          <div><label>Shutter</label><select id="f-shutterspeed"></select></div>
          <div><label>White balance</label><select id="f-whitebalance"></select></div>
        </div>
        <div class="note" id="expNote"></div>
      </div>
     </div>
     <div class="col">
      <div class="card">
        <h3>4 · Test shot</h3>
        <div id="testWrap">
          <button id="testShot">📸 Take test shot</button>
          <button id="autoExp">✨ Auto-exposure</button>
          <div class="note" id="testMsg">Take a throwaway shot to check exposure (not counted),
            or let Auto-exposure dial in the shutter for you.</div>
          <img id="testImg" alt="test shot">
          <div id="testChip" class="pill" style="display:none;margin-top:.4rem"></div>
        </div>
      </div>
      <div class="card">
        <h3>System</h3>
        <div class="note">Version <code id="appVer">…</code>
          <button id="checkUpd">Check for updates</button>
          <div id="updMsg" style="margin-top:.45rem"></div></div>
        <div class="note" id="metaNote"></div>
        <div class="diagrow">
          <button id="diagBtn">Camera diagnostics</button>
          <button id="logBtn">View logs</button>
          <button id="logRefresh">↻</button>
          <button id="copyOut">📋 Copy</button>
        </div>
        <pre id="diagOut"></pre>
      </div>
      <div class="card">
        <h3>Sensor trigger (optional)</h3>
        <label class="tog"><input type="checkbox" id="trigOn">Auto-capture when the beam is blocked</label>
        <div class="grid2" style="margin-top:.5rem">
          <div><label>When blocked, OUT goes</label><select id="trigPol">
            <option value="low">LOW (active-low — typical)</option>
            <option value="high">HIGH (active-high)</option>
          </select></div>
          <div><label>GPIO line (BCM)</label><input id="trigLine" type="number" min="0" max="27"></div>
        </div>
        <div class="diagrow" style="margin-top:.5rem">
          <button id="trigSave">Save</button>
          <button id="trigRead">Read sensor now</button>
        </div>
        <div class="note" id="trigMsg">Wire OUT→GPIO24 (pin 18), VCC→3V3 (pin 1), GND→pin 6.
          Not sure of the polarity? Block the beam and hit “Read sensor now”.</div>
        <hr style="border:none;border-top:1px solid #ffffff18;margin:.8rem 0">
        <label class="tog"><input type="checkbox" id="reshootOn">Auto-reshoot dark/bright frames in place</label>
        <div class="grid2" style="margin-top:.5rem">
          <div><label>Max reshoots per frame</label><input id="reshootMax" type="number" min="1" max="4" value="1"></div>
        </div>
        <div class="diagrow" style="margin-top:.5rem"><button id="reshootSave">Save</button></div>
        <div class="note">After each shot, if it reads dark/bright the shutter steps toward correct and the
          <em>same</em> frame is reshot (keeping the best), then the baseline shutter is restored. Each reshoot
          adds a few seconds — leave enough gap before the next slide arrives.</div>
      </div>
      <div class="card">
        <h3>Brightness correction</h3>
        <label class="tog"><input type="checkbox" id="brOn">Auto-correct brightness after each shot</label>
        <div class="grid2" style="margin-top:.5rem">
          <div><label>Correct</label><select id="brMode">
            <option value="flagged">Only dark / bright frames</option>
            <option value="all">Every frame (even batch)</option>
          </select></div>
          <div><label>Aim for</label><select id="brTarget">
            <option value="95">Darker (95)</option>
            <option value="110">Normal (110)</option>
            <option value="130">Brighter (130)</option>
          </select></div>
          <div><label>Max correction</label><select id="brMaxEv">
            <option value="1">±1 stop</option>
            <option value="1.5">±1.5 stops</option>
            <option value="2">±2 stops</option>
          </select></div>
        </div>
        <label class="tog" style="margin-top:.5rem"><input type="checkbox" id="brKeep">Keep the untouched original (lets Review undo)</label>
        <div class="diagrow" style="margin-top:.5rem"><button id="brSave">Save</button>
          <button id="brInstall" style="display:none">⬇ Install brightness support</button></div>
        <div class="note" id="brMsg">Fixes the fixed-backlight problem in the pixels: a frame that meters
          dark is pulled up with a gamma curve — no extra shot, no shutter wear. Bounded by the max above,
          and the original is kept so Review can undo it. Highlights already blown can’t be recovered
          (that needs auto-reshoot); RAW files are never modified.</div>
      </div>
     </div>
    </div>
    <button id="startCap">Start capturing →</button>
   </div>
  </section>

  <!-- CAPTURE -->
  <section id="view-capture" class="view">
    <div id="stage">
      <div id="capPlace">Place a slide, then press <b>Space</b> (or tap Capture).</div>
      <img id="capImg" alt="last capture">
      <div id="chip"></div>
      <div id="capName"></div>
      <div id="spinner"><span class="dot"></span>Capturing…</div>
    </div>
    <div id="capMsg"></div>
    <div id="capBar">
      <button id="shoot">Capture<span class="khint">Space / Enter</span></button>
      <button id="redo">↩ Redo last<span class="khint">R</span></button>
    </div>
  </section>

  <!-- REVIEW -->
  <section id="view-review" class="view">
    <div id="revBar">
      <span id="revInfo">–</span>
      <label class="tog"><input type="checkbox" id="flagOnly">Only flagged</label>
      <button id="revRefresh">↻ Refresh</button>
      <button id="revZip" title="Includes an originals/ folder holding the untouched version of every brightness-corrected frame">⬇ Download all (zip)</button>
      <button id="revDelAll" class="del">🗑 Delete all</button>
    </div>
    <div id="gridwrap">
      <div id="grid"></div>
      <button id="loadMore">Load more</button>
    </div>
  </section>
</main>

<div id="lb">
  <div id="lbImgWrap">
    <button id="lbNavL">‹</button>
    <img id="lbImg" alt="">
    <button id="lbNavR">›</button>
  </div>
  <div id="lbBar">
    <div id="lbInfo"></div>
    <input id="lbCaption" placeholder="Caption for this image…" maxlength="300">
    <button id="lbSave">Save</button>
    <button id="lbDl">⬇</button>
    <button id="lbOrig" style="display:none">👁 Original</button>
    <button id="lbRevert" style="display:none">↩ Undo brighten</button>
    <button id="lbDel" class="del">🗑 Delete</button>
    <button id="lbClose">Close (Esc)</button>
  </div>
</div>
<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const EXP = ['imageformat','iso','aperture','shutterspeed','whitebalance'];
const EXPO = {ok:['✓ Good','good'],dark:['⚠ A bit dark','warn'],bright:['⚠ A bit bright','warn'],
              under:['✕ Too dark','bad'],over:['✕ Overexposed','bad'],
              correcting:['✨ Brightness correcting…','good']};
const FLAG = {dark:1,bright:1,under:1,over:1};
let mode='setup', ST={recent:[],captions:{},exposure:{},prefix:'slide',count:0};
let capIdx=0, busy=false;
let rev={items:[],total:0,offset:0,limit:60,lbIdx:-1};
const BR_NOTE = $('#brMsg').textContent;    // restored after a transient message

function toast(t,kind){ const el=$('#toast'); el.textContent=t; el.className=kind||'';
  el.style.display='block'; clearTimeout(toast._t); toast._t=setTimeout(()=>el.style.display='none',2500); }
function beep(){ try{ const a=new (window.AudioContext||window.webkitAudioContext)();
  const o=a.createOscillator(),g=a.createGain(); o.connect(g); g.connect(a.destination);
  o.frequency.value=330; g.gain.value=.15; o.start(); o.stop(a.currentTime+.18);}catch(e){} }
function typing(){ const t=document.activeElement; return t && /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName); }
async function jget(u){ return (await fetch(u)).json(); }
async function jpost(u,b){ return (await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(b||{})})).json(); }

function setMode(m){
  mode=m;
  document.querySelectorAll('.view').forEach(v=>v.classList.remove('active'));
  $('#view-'+m).classList.add('active');
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.mode===m));
  if(m==='setup'){ loadSettings(); loadVersion(); loadTrigger(); loadGroups(); }
  if(m==='review') loadReview(true);
  if(m==='capture'){ capIdx=0; renderCap(); }
}

/* ---- status ---- */
async function status(){
  let s; try{ s=await jget('/api/status'); }catch(e){ $('#camstat').textContent='offline'; $('#camstat').className='pill bad'; return; }
  if(s.busy) return;
  ST.prefix=s.prefix||'slide'; ST.count=s.count||0;
  ST.recent=s.recent||[]; ST.captions=s.captions||{}; ST.exposure=s.exposure||{};
  if(s.reshoot){
    if(document.activeElement!==$('#reshootOn')) $('#reshootOn').checked=!!s.reshoot.enabled;
    if(document.activeElement!==$('#reshootMax')) $('#reshootMax').value=s.reshoot.max||1;
  }
  if(s.brightness) fillBright(s.brightness);
  $('#pfxEx').textContent=ST.prefix; if(!$('#prefix').value) $('#prefix').value=ST.prefix;
  $('#grpcount').textContent=ST.prefix+' · '+ST.count;
  if(!s.connected){ $('#camstat').textContent=s.error?'no camera':'no camera'; $('#camstat').className='pill bad';
    $('#shoot').disabled=true; }
  else { $('#camstat').textContent=s.model.replace('Canon EOS ','')+' · '+s.battery+' · '+s.mode
      +' · '+s.iso+'/f'+s.aperture+'/'+s.shutter;
    $('#camstat').className='pill '+(s.battery&&s.battery.toLowerCase()==='low'?'warn':'good');
    $('#shoot').disabled=false; }
  if(mode==='capture') renderCap();
}

/* ---- capture ---- */
function chipFor(el,st){ const m=EXPO[st]; if(!m){ el.style.display='none'; return; }
  el.textContent=m[0]; el.className=(el.id==='chip'?'':'pill ')+m[1]; el.style.display=el.id==='chip'?'block':'inline-block'; }
function renderCap(){
  const name=ST.recent[capIdx];
  const img=$('#capImg'), pl=$('#capPlace');
  $('#redo').disabled = !ST.recent.length;
  if(!name){ img.style.display='none'; pl.style.display='block'; $('#chip').style.display='none';
    $('#capName').textContent=''; return; }
  // Lightweight preview: the tiny embedded thumbnail (few KB), not the full
  // ~3MB JPEG — the Pi Zero's WiFi shouldn't push a full frame per shot during
  // an unattended run. Click to load the full image for a slide worth checking.
  img.src='/thumb/'+encodeURIComponent(name)+'?t='+Date.now(); img.style.display='block'; pl.style.display='none';
  img.style.cursor='zoom-in';
  img.onclick=()=>{ img.src='/media/'+encodeURIComponent(name)+'?t='+Date.now(); };
  $('#capName').textContent=name+(capIdx?' ('+(capIdx+1)+' back)':'');
  chipFor($('#chip'), ST.exposure[name]);
}
async function capture(){
  if(busy) return; busy=true; $('#shoot').disabled=true; $('#spinner').style.display='flex'; $('#capMsg').textContent='';
  try{
    const d=await jpost('/api/capture');
    if(d.ok){ ST.count=d.count; ST.recent.unshift(d.name); if(d.exposure) ST.exposure[d.name]=d.exposure.status;
      // A queued correction rewrites the file a few seconds later; show that
      // it's handled rather than the (now stale) "too dark" verdict. The next
      // status poll replaces it with the real post-correction reading.
      if(d.brightness) ST.exposure[d.name]='correcting';
      capIdx=0; $('#grpcount').textContent=ST.prefix+' · '+ST.count; renderCap();
      const rs=(d.reshoots&&d.reshoots.length)?' (reshot ×'+d.reshoots.length+')':'';
      const br=d.brightness?(' · brightening '+(d.brightness.ev>0?'+':'')+d.brightness.ev+' EV'):'';
      $('#capMsg').textContent='Saved '+d.name+rs+br; $('#capMsg').className='ok'; }
    else { $('#capMsg').textContent=d.error||'capture failed'; $('#capMsg').className='err'; toast(d.error||'capture failed','err'); beep(); }
  }catch(e){ $('#capMsg').textContent='network error'; $('#capMsg').className='err'; beep(); }
  finally{ busy=false; $('#spinner').style.display='none'; $('#shoot').disabled=false; }
}
async function redoLast(){
  if(busy||!ST.recent.length) return;
  const last=ST.recent[0];
  if(!confirm('Redo '+last+'? (deletes it, then captures again)')) return;
  busy=true;
  const d=await jpost('/api/delete',{name:last});
  if(d.ok){ ST.recent.shift(); delete ST.exposure[last]; ST.count=d.count; }
  busy=false;
  await capture();
}
function browseRecent(delta){ if(!ST.recent.length) return;
  capIdx=Math.max(0,Math.min(ST.recent.length-1,capIdx+delta)); renderCap(); }

/* ---- setup ---- */
function fillSelect(id,data){ const sel=$('#f-'+id);
  if(!data){ sel.innerHTML='<option>—</option>'; sel.disabled=true; return; }
  sel.disabled=false; sel.innerHTML='';
  (data.choices||[]).forEach(c=>{ const o=document.createElement('option'); o.value=c; o.textContent=c;
    if(c===data.current) o.selected=true; sel.appendChild(o); });
  sel.onchange=()=>applyField(id,sel.value);
}
async function loadSettings(){
  const s=await jget('/api/settings');
  $('#metaNote').textContent = s.have_exiftool ? 'Metadata: written to EXIF (exiftool detected).'
    : 'Metadata: written as a JPEG comment. Install exiftool for full EXIF on JPEG + RAW.';
  if(!s.connected){ $('#expNote').textContent=s.error||'camera offline'; EXP.forEach(k=>fillSelect(k,null)); return; }
  $('#expNote').textContent='Exposure changes need the dial on M.';
  EXP.forEach(k=>fillSelect(k, s.fields[k]));
}
async function applyField(field,val){ const d=await jpost('/api/settings',{[field]:val});
  if(d.connected===false) toast(d.error||'could not apply','err'); else { toast(field+' = '+val,'ok'); status(); } }
async function applyPrefix(){ await jpost('/api/settings',{prefix:$('#prefix').value});
  await status(); $('#prefix').value=ST.prefix; toast('Group: '+ST.prefix,'ok'); loadGroups(); }
/* Groups on this device: renaming the prefix orphans the old group's files —
   still on disk but invisible to the prefix-scoped views — so Setup lists every
   group found on disk with open/delete controls. */
async function loadGroups(){
  let d; try{ d=await jget('/api/groups'); }catch(e){ return; }
  renderGroups(d.groups||[], d.current);
}
function renderGroups(groups, current){
  const el=$('#grpList'); el.innerHTML='';
  $('#grpListNote').style.display = groups.length ? 'block' : 'none';
  groups.forEach(g=>{
    const row=document.createElement('div'); row.className='grow';
    const cur=g.prefix===current, n=g.count||g.files;
    row.innerHTML='<span class="gname">'+g.prefix.replace(/</g,'&lt;')
      +(cur?' <span class="pill good">current</span>':'')+'</span>'
      +'<span class="gcount">'+n+(g.count?' image':' file')+(n===1?'':'s')+'</span>';
    if(!cur){ const b=document.createElement('button'); b.textContent='Open';
      b.onclick=()=>openGroup(g.prefix); row.appendChild(b); }
    const del=document.createElement('button'); del.className='gdel'; del.textContent='🗑';
    del.title='Delete this group'; del.onclick=()=>deleteGroup(g); row.appendChild(del);
    el.appendChild(row);
  });
}
async function openGroup(p){ await jpost('/api/settings',{prefix:p});
  await status(); $('#prefix').value=ST.prefix; toast('Group: '+ST.prefix,'ok'); loadGroups(); }
async function deleteGroup(g){
  const n=g.count||g.files;
  if(!confirm('Delete group “'+g.prefix+'” — ALL '+n+(g.count?' image':' file')+(n===1?'':'s')
    +' (plus any RAW siblings and pre-correction originals)?\n'
    +'This cannot be undone — download first if you want to keep them.')) return;
  const d=await jpost('/api/deletegroup',{prefix:g.prefix});
  if(d.ok){ toast('Deleted '+d.removed+' file'+(d.removed===1?'':'s'),'ok');
    renderGroups(d.groups||[], d.current); status(); }
  else toast(d.error||'delete failed','err');
}
async function preset(name){ const d=await jpost('/api/preset',{name});
  if(d.connected===false||d.ok===false){ toast(d.error||'preset failed','err'); return; }
  toast(name+' defaults set — fine-tune shutter','ok'); loadSettings(); status(); }
async function testShot(){
  $('#testMsg').textContent='Capturing test…'; $('#testChip').style.display='none';
  const d=await jpost('/api/test');
  if(!d.ok){ $('#testMsg').textContent=d.error||'test failed'; toast(d.error||'test failed','err'); return; }
  $('#testMsg').textContent='Test shot (not saved to the group):';
  const im=$('#testImg'); im.src='/media/'+d.name+'?t='+Date.now(); im.style.display='inline-block';
  if(d.exposure) chipFor($('#testChip'), d.exposure.status);
}
async function autoExpose(){
  $('#testMsg').textContent='Auto-exposing… taking a few test shots'; $('#testChip').style.display='none';
  $('#autoExp').disabled=true; $('#testShot').disabled=true;
  try{
    const d=await jpost('/api/autoexpose');
    if(!d.ok){ $('#testMsg').textContent=d.error||'auto-exposure failed'; toast(d.error||'failed','err'); return; }
    const im=$('#testImg'); im.src='/media/'+d.name+'?t='+Date.now(); im.style.display='inline-block';
    if(d.exposure) chipFor($('#testChip'), d.exposure.status);
    $('#testMsg').textContent='Auto-exposure: shutter '+d.final_shutter+' — '+(d.status||'?')
      +' (tried '+d.steps.map(s=>s.shutter).join(' → ')+')';
    loadSettings();                                  // refresh the shutter dropdown
  }catch(e){ $('#testMsg').textContent='auto-exposure failed (network)'; }
  finally{ $('#autoExp').disabled=false; $('#testShot').disabled=false; }
}

/* ---- self-update ---- */
async function loadVersion(){
  try{ const d=await jget('/api/version'); $('#appVer').textContent=d.version||'?'; }
  catch(e){ $('#appVer').textContent='?'; }
}
async function checkUpdate(){
  const m=$('#updMsg'); m.textContent='Checking…';
  let d; try{ d=await jget('/api/update'); }catch(e){ m.textContent='Check failed (no network?).'; return; }
  if(d.busy){ m.textContent='Busy — finish the current capture first.'; return; }
  if(d.ok===false){ m.textContent=d.error||'Check failed.'; return; }
  if(!d.available){ m.textContent='Up to date ('+(d.current||'?')+').'; return; }
  m.innerHTML='Update available: <b>'+d.latest+'</b> — you have '+(d.current||'?')+'. ';
  const b=document.createElement('button'); b.textContent='Update & restart';
  b.onclick=()=>applyUpdate(d.latest); m.appendChild(b);
}
async function applyUpdate(to){
  if(!confirm('Update to '+to+' and restart? The app will be briefly unavailable.')) return;
  const m=$('#updMsg'); m.textContent='Updating to '+to+'… the app will restart.';
  let d=null; try{ d=await jpost('/api/update',{}); }catch(e){ /* server may drop as it restarts */ }
  if(d && d.ok===false && !d.restarting){ m.textContent=d.error||'Update failed.'; return; }
  let tries=0;                                   // poll until it's back on the new version
  const iv=setInterval(async()=>{
    tries++;
    try{ const v=await jget('/api/version');
      if(v.version && v.version.indexOf(to)===0){ clearInterval(iv);
        m.textContent='Updated to '+v.version+'. Reloading…'; setTimeout(()=>location.reload(),900); return; }
    }catch(e){}
    if(tries>40){ clearInterval(iv); m.textContent='Restarted — reload the page to confirm.'; }
  }, 1500);
}

/* ---- sensor trigger ---- */
function fillTrig(t){ if(!t) return;
  $('#trigOn').checked = (t.mode==='gpio');
  $('#trigPol').value = t.active_high ? 'high' : 'low';
  if(document.activeElement!==$('#trigLine')) $('#trigLine').value = t.sensor_line;
}
function fillReshoot(r){ if(!r) return;
  if(document.activeElement!==$('#reshootOn')) $('#reshootOn').checked=!!r.enabled;
  if(document.activeElement!==$('#reshootMax')) $('#reshootMax').value=r.max||1;
}
function fillBright(b){ if(!b) return;
  const set=(id,v)=>{ if(document.activeElement!==$(id)) $(id).value=v; };
  if(document.activeElement!==$('#brOn')) $('#brOn').checked=!!b.enabled;
  if(document.activeElement!==$('#brKeep')) $('#brKeep').checked=!!b.keep_original;
  set('#brMode', b.mode||'flagged');
  set('#brTarget', String(b.target||110));
  set('#brMaxEv', String(b.max_ev||1.5));
  // NEVER disable this box. It defaults to checked, so disabling it when the
  // library is missing left the operator staring at a ticked control they
  // could not untick — reading as "this is on and I can't stop it". The
  // toggle always works; availability is explained in the note instead.
  $('#brOn').disabled = false;
  // The appliance has no shell, so offer to install the missing piece in place
  // rather than telling the operator to run apt (they can't).
  $('#brInstall').style.display = b.available ? 'none' : 'inline-block';
  if(!b.available && !installing) $('#brMsg').textContent =
    'Inactive — no images are being changed. It needs one extra system package '
    + '(python3-pil). Tap “Install brightness support”; the scanner fetches it '
    + 'itself and restarts if needed, about a minute on WiFi.'
    + (b.import_error?(' (' + b.import_error + ')'):'');
  else if(installing) { /* the installer owns the message while it runs */ }
  /* The queue also carries the metadata write for every frame, not just the
     corrections, so word it as finishing rather than correcting. */
  else if(b.pending) $('#brMsg').textContent = b.pending+' frame'+(b.pending===1?'':'s')
    +' still being finished in the background…';
  else $('#brMsg').textContent = BR_NOTE;
}
/* ---- in-app install of the optional native dependency ---- */
let installing=false;
function waitForRestart(){
  let tries=0;
  const iv=setInterval(async()=>{
    tries++;
    try{ const d=await jget('/api/brightness');
      clearInterval(iv); fillBright(d.brightness);
      if(d.brightness.available){ $('#brMsg').textContent=BR_NOTE;
        toast('Brightness correction is ready','ok'); }
      else $('#brMsg').textContent='Restarted, but the library still will not '
        + 'load: '+(d.brightness.import_error||'reason unknown')
        + '. Tap View logs under System.';
      return;
    }catch(e){}                                  // still down; keep waiting
    if(tries>40){ clearInterval(iv);
      $('#brMsg').textContent='The scanner is taking a while to come back — '
        + 'reload this page in a moment.'; }
  }, 1500);
}
async function installBrightness(){
  if(installing) return;
  const d=await jpost('/api/install',{feature:'brightness'});
  if(d.ok===false){ $('#brMsg').textContent=d.error||'Could not start the install.';
    toast(d.error||'install failed','err'); return; }
  if(d.already){ fillBright(d.brightness); toast('Already installed','ok'); return; }
  installing=true; $('#brInstall').disabled=true;
  $('#brMsg').textContent='Installing… this takes a minute or two on WiFi. '
    + 'Leave this page open.';
  const iv=setInterval(async()=>{
    let s; try{ s=await jget('/api/install'); }catch(e){ return; }
    if(s.running){ $('#brMsg').textContent='Installing… ('+Math.round(s.elapsed)
      +'s) '+(s.log||'').trim().split('\n').pop().slice(0,80); return; }
    clearInterval(iv); installing=false; $('#brInstall').disabled=false;
    fillBright(s.brightness);
    // A package that appeared after startup isn't visible to this process, so
    // the installer restarts the service; wait for it to come back rather than
    // leaving a dead page and a "still unavailable" message.
    if(s.restarting){ $('#brMsg').textContent='Installed — restarting the scanner '
      + 'to activate it…'; waitForRestart(); return; }
    if(s.success){ $('#brMsg').textContent=BR_NOTE;
      toast('Brightness correction is ready','ok'); }
    else { $('#brMsg').textContent='Install failed — tap View logs under System, '
      + 'or check the scanner is on WiFi. Last output: '
      + (s.log||'').trim().split('\n').slice(-3).join(' ').slice(0,200); }
  }, 2500);
}
async function saveBright(){
  const d=await jpost('/api/brightness',{enabled:$('#brOn').checked,
    mode:$('#brMode').value, target:parseInt($('#brTarget').value,10),
    max_ev:parseFloat($('#brMaxEv').value), keep_original:$('#brKeep').checked});
  if(d.ok){ fillBright(d.brightness);
    if(d.warning){ $('#brMsg').textContent=d.warning; toast('Saved, but not active','warn'); }
    else toast('Brightness correction: '+d.brightness.describe,'ok'); }
  else toast('save failed','err');
}
async function loadTrigger(){
  // Restores the sensor, reshoot AND brightness toggles from the lock-free
  // endpoint, so they show the saved state even when the camera is busy.
  try{ const d=await jget('/api/trigger'); fillTrig(d.trigger); fillReshoot(d.reshoot);
    fillBright(d.brightness); }
  catch(e){}
}
async function saveTrigger(){
  const body={ mode: $('#trigOn').checked?'gpio':'off',
    active_high: $('#trigPol').value==='high',
    sensor_line: parseInt($('#trigLine').value,10)||24 };
  $('#trigMsg').textContent='Saving…';
  try{
    const d=await jpost('/api/trigger',body);
    if(d.ok){ fillTrig(d.trigger);
      $('#trigMsg').textContent = d.trigger.running
        ? ('Watching '+d.trigger.describe+'. Blocking the beam will capture.')
        : 'Sensor trigger off.'; }
    else $('#trigMsg').textContent = d.error||'Save failed.';
  }catch(e){ $('#trigMsg').textContent='Save failed (network?).'; }
}
async function readSensor(){
  $('#trigMsg').textContent='Reading…';
  const d=await jget('/api/trigger');
  if(d.ok===false){ $('#trigMsg').textContent=d.error||'Read failed.'; return; }
  $('#trigMsg').textContent = 'Sensor reads '+d.level+' → '
    + (d.obstructed?'OBSTRUCTED':'clear')
    + ' (with the current polarity). Block/clear the beam and read again to confirm.';
}
async function saveReshoot(){
  const d=await jpost('/api/reshoot',{enabled:$('#reshootOn').checked,
    max:parseInt($('#reshootMax').value,10)||1});
  if(d.ok){ $('#reshootOn').checked=d.reshoot.enabled; $('#reshootMax').value=d.reshoot.max;
    toast(d.reshoot.enabled?'Auto-reshoot on (max '+d.reshoot.max+')':'Auto-reshoot off','ok'); }
  else toast('save failed','err');
}

/* ---- diagnostics ---- */
let lastShown='';     // 'diag' | 'logs' — so ↻ refreshes whatever's on screen
async function showDiag(){
  lastShown='diag';
  const o=$('#diagOut'); o.style.display='block'; o.textContent='Loading diagnostics…';
  try{ o.textContent=JSON.stringify(await jget('/api/diag'),null,2); }
  catch(e){ o.textContent='Failed to load diagnostics.'; }
}
async function showLogs(){
  lastShown='logs';
  const o=$('#diagOut'); o.style.display='block'; o.textContent='Loading logs…';
  try{ const d=await jget('/api/logs?lines=800');
    o.textContent = d.ok ? d.text : ('logs unavailable: '+(d.error||'?')); }
  catch(e){ o.textContent='Failed to load logs.'; }
}
function refreshOut(){ if(lastShown==='diag') showDiag(); else showLogs(); }
// Copy that also works over plain http:// (the async clipboard API needs a
// secure context, which a LAN IP isn't) — fall back to a hidden textarea.
async function copyText(t){
  try{ if(navigator.clipboard && window.isSecureContext){ await navigator.clipboard.writeText(t); return true; } }catch(e){}
  try{ const ta=document.createElement('textarea'); ta.value=t;
    ta.style.position='fixed'; ta.style.top='-1000px'; document.body.appendChild(ta);
    ta.focus(); ta.select(); const ok=document.execCommand('copy'); document.body.removeChild(ta); return ok;
  }catch(e){ return false; }
}
async function copyOut(){
  const t=$('#diagOut').textContent||'';
  if(!t.trim()){ toast('Nothing to copy — tap View logs first','err'); return; }
  toast(await copyText(t) ? 'Copied to clipboard' : 'Copy failed — long-press/select the text','ok');
}

/* ---- review ---- */
function eclass(st){ if(!st) return 'e-none'; if(st==='ok') return 'e-ok';
  if(st==='under'||st==='over') return 'e-bad'; return 'e-warn'; }
function revSig(items){ return (items||[]).map(i=>i.name+':'+(i.caption||'')+':'+(i.exposure||'')
  +':'+(i.mtime||0)+':'+(i.orig?1:0)).join('|'); }
async function loadReview(reset){
  if(reset){ rev.items=[]; rev.offset=0; }
  const d=await jget('/api/images?offset='+rev.offset+'&limit='+rev.limit);
  rev.total=d.total; rev.items=rev.items.concat(d.items); rev.offset=rev.items.length;
  rev._sig=revSig(rev.items);
  renderGrid();
}
// Keep Review live without a manual refresh: while it's the active view, re-poll
// the group and re-render only when the set actually changed (adds from an
// ongoing auto-run, deletes from here or another device). Paused while the
// lightbox is open so it never disrupts an in-progress caption/delete.
async function syncReview(){
  if(mode!=='review' || document.hidden) return;
  if($('#lb').classList.contains('open')) return;
  const want=Math.min(200, Math.max(rev.limit, rev.items.length));
  let d; try{ d=await jget('/api/images?offset=0&limit='+want); }catch(e){ return; }
  if(d.total===rev.total && revSig(d.items)===rev._sig) return;   // nothing changed
  rev.items=d.items; rev.total=d.total; rev.offset=rev.items.length; rev._sig=revSig(rev.items);
  renderGrid();
}
async function deleteAll(){
  if(!rev.total){ toast('Nothing to delete','ok'); return; }
  const nOrig=rev.items.filter(i=>i.orig).length;
  if(!confirm('Delete ALL '+rev.total+' image'+(rev.total===1?'':'s')+' in “'+ST.prefix+'”?\n'
    +(nOrig?('This also deletes the pre-correction originals of '+nOrig+' brightened frame'
      +(nOrig===1?'':'s')+'.\n'):'')
    +'This cannot be undone — download first if you want to keep them (the zip includes the originals).')) return;
  const d=await jpost('/api/deleteall',{prefix:ST.prefix});
  if(d.ok){ toast('Deleted '+d.removed+' file'+(d.removed===1?'':'s'),'ok'); ST.count=d.count; loadReview(true); }
  else toast(d.error||'delete failed','err');
}
function visibleItems(){ return $('#flagOnly').checked ? rev.items.filter(i=>FLAG[i.exposure]) : rev.items; }
function renderGrid(){
  const g=$('#grid'); g.innerHTML=''; const items=visibleItems();
  $('#revInfo').textContent = ST.prefix+' — '+rev.total+' image'+(rev.total===1?'':'s')
    + ($('#flagOnly').checked?' ('+items.length+' flagged shown)':'');
  items.forEach((it)=>{
    const idx=rev.items.indexOf(it);
    const t=document.createElement('div'); t.className='tile';
    t.innerHTML='<img loading="lazy" src="/thumb/'+encodeURIComponent(it.name)+'?v='+(it.mtime||0)+'">'
      +'<span class="edot '+eclass(it.exposure)+'"></span>'
      +(it.caption?'<span class="cap">'+it.caption.replace(/</g,'&lt;')+'</span>':'');
    t.onclick=()=>openLB(idx); g.appendChild(t);
  });
  $('#loadMore').style.display = (!$('#flagOnly').checked && rev.items.length<rev.total)?'block':'none';
}
function openLB(idx){ rev.lbIdx=idx; const it=rev.items[idx]; if(!it) return;
  $('#lbImg').src='/media/'+encodeURIComponent(it.name)+'?v='+(it.mtime||Date.now());
  $('#lbCaption').value=it.caption||'';
  $('#lbInfo').textContent=it.name+'  ·  '+(EXPO[it.exposure]?EXPO[it.exposure][0]:'exposure n/a')
    +(it.orig?'  ·  brightness corrected':'');
  // Only frames with a stashed pre-correction copy can be compared/undone.
  $('#lbOrig').style.display=it.orig?'inline-block':'none';
  $('#lbRevert').style.display=it.orig?'inline-block':'none';
  $('#lbOrig').textContent='👁 Original';
  $('#lb').classList.add('open');
}
// Press-and-hold style toggle: flip the lightbox between the corrected frame
// and the untouched original so a correction can be judged before undoing it.
function lbToggleOrig(){ const it=rev.items[rev.lbIdx]; if(!it||!it.orig) return;
  const b=$('#lbOrig'), showing=b.dataset.on==='1';
  b.dataset.on = showing?'':'1';
  b.textContent = showing?'👁 Original':'👁 Corrected';
  const v=it.mtime||Date.now();
  $('#lbImg').src='/media/'+encodeURIComponent(it.name)+(showing?('?v='+v):('?orig=1&v='+v));
}
async function lbRevert(){ const it=rev.items[rev.lbIdx]; if(!it) return;
  if(!confirm('Undo the brightness correction on '+it.name+'?\\nThe original replaces it.')) return;
  const d=await jpost('/api/revert',{name:it.name});
  if(!d.ok){ toast(d.error||'undo failed','err'); return; }
  it.orig=false; it.mtime=d.mtime||it.mtime; it.exposure=(d.exposure&&d.exposure.status)||'';
  $('#lbOrig').dataset.on=''; openLB(rev.lbIdx); renderGrid(); toast('Reverted to the original','ok');
}
function closeLB(){ $('#lb').classList.remove('open'); rev.lbIdx=-1; }
function lbNav(delta){ let i=rev.lbIdx+delta; if(i<0||i>=rev.items.length) return; openLB(i); }
async function lbDelete(){ const it=rev.items[rev.lbIdx]; if(!it) return;
  if(!confirm('Delete '+it.name+' (and its RAW, if any)?')) return;
  const d=await jpost('/api/delete',{name:it.name});
  if(d.ok){ rev.items.splice(rev.lbIdx,1); rev.total--; toast('Deleted','ok');
    if(rev.lbIdx>=rev.items.length) closeLB(); else openLB(rev.lbIdx); renderGrid(); }
  else toast(d.error||'delete failed','err'); }
async function lbSave(){ const it=rev.items[rev.lbIdx]; if(!it) return;
  const d=await jpost('/api/caption',{name:it.name,caption:$('#lbCaption').value});
  if(d.ok){ it.caption=d.caption; toast(d.caption?'Caption saved':'Caption cleared','ok'); renderGrid(); }
  else toast(d.error||'save failed','err'); }

/* ---- wire up ---- */
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>setMode(b.dataset.mode));
$('#p-slides').onclick=()=>preset('slides'); $('#p-negatives').onclick=()=>preset('negatives');
$('#applyPrefix').onclick=applyPrefix; $('#testShot').onclick=testShot;
$('#autoExp').onclick=autoExpose;
$('#checkUpd').onclick=checkUpdate;
$('#diagBtn').onclick=showDiag; $('#logBtn').onclick=showLogs;
$('#logRefresh').onclick=refreshOut; $('#copyOut').onclick=copyOut;
$('#trigSave').onclick=saveTrigger; $('#trigRead').onclick=readSensor;
$('#reshootSave').onclick=saveReshoot; $('#brSave').onclick=saveBright;
$('#brInstall').onclick=installBrightness;
$('#startCap').onclick=()=>setMode('capture');
$('#shoot').onclick=capture; $('#redo').onclick=redoLast;
$('#revRefresh').onclick=()=>loadReview(true); $('#flagOnly').onchange=renderGrid;
$('#revZip').onclick=()=>location.href='/api/zip'; $('#loadMore').onclick=()=>loadReview(false);
$('#revDelAll').onclick=deleteAll;
$('#lbNavL').onclick=()=>lbNav(-1); $('#lbNavR').onclick=()=>lbNav(1);
$('#lbClose').onclick=closeLB; $('#lbDel').onclick=lbDelete; $('#lbSave').onclick=lbSave;
$('#lbOrig').onclick=lbToggleOrig; $('#lbRevert').onclick=lbRevert;
$('#lbDl').onclick=()=>{ const it=rev.items[rev.lbIdx]; if(it) location.href='/media/'+encodeURIComponent(it.name)+'?dl=1'; };

document.addEventListener('keydown', e=>{
  if($('#lb').classList.contains('open')){
    if(e.key==='ArrowLeft') lbNav(-1); else if(e.key==='ArrowRight') lbNav(1);
    else if(e.key==='Escape') closeLB(); else if(e.key==='Delete' && !typing()) lbDelete();
    return;
  }
  if(typing()) return;
  if(e.key==='[') { setMode('setup'); return; }
  if(e.key===']') { setMode('review'); return; }
  if(mode==='capture'){
    if(e.code==='Space'||e.key==='Enter'){ e.preventDefault(); if(!e.repeat) capture(); }
    else if(e.key==='r'||e.key==='R'||e.key==='Backspace'){ e.preventDefault(); if(!e.repeat) redoLast(); }
    else if(e.key==='ArrowLeft'){ browseRecent(1); }
    else if(e.key==='ArrowRight'){ browseRecent(-1); }
  }
});

/* ---- Back-button / edge-swipe guard ----------------------------------------
   The app is a single page — mode switches are DOM toggles, not navigation — so
   a stray Back press (or a tablet's edge-swipe-back) would leave the app and
   drop the operator out of a capture run. Seed a history entry and re-arm it on
   every popstate so Back can't navigate away: it just keeps us here with a hint.
   (Closing the tab still exits.) */
(function guardBack(){
  try{
    history.pushState({ss:1}, '');
    window.addEventListener('popstate', ()=>{
      history.pushState({ss:1}, '');      // re-arm so the NEXT Back is caught too
      toast('Back is off here — use the Setup / Capture / Review tabs', 'warn');
    });
  }catch(e){ /* History API unavailable — nothing to guard */ }
})();

setMode('setup'); status(); setInterval(status, 15000);
setInterval(syncReview, 4000);                       // keep Review live
window.addEventListener('focus', syncReview);
document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) syncReview(); });
</script>
</body></html>
"""


def main():
    global OUT_DIR, PREFIX
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--out-dir", default="./captures")
    p.add_argument("--prefix", default="slide")
    p.add_argument("--no-setup", action="store_true")
    p.add_argument("--sensor", action="store_true",
                   help="enable the optical-sensor capture trigger (GPIO)")
    p.add_argument("--sensor-line", type=int,
                   default=TRIGGER_DEFAULTS["sensor_line"],
                   help="BCM line wired to the sensor OUT (default %(default)s = phys pin 18)")
    p.add_argument("--sensor-active-high", action="store_true",
                   help="sensor OUT goes HIGH when obstructed (default: LOW)")
    p.add_argument("--persist", action="store_true",
                   help="EXPERIMENTAL: hold one gphoto2 session open for the "
                        "whole run instead of spawning a process per operation. "
                        "Much faster to the shutter when it works, but OFF by "
                        "default until it is proven on this hardware — v0.1.35 "
                        "shipped it on and the camera stopped taking pictures.")
    args = p.parse_args()
    cam.persistent = args.persist

    OUT_DIR = Path(args.out_dir)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PREFIX = sanitize_prefix(args.prefix)

    print(f"Session folder: {OUT_DIR.resolve()}")
    print(f"Group prefix:   {PREFIX}   |   exiftool: "
          f"{'yes' if HAVE_EXIFTOOL else 'no (JPEG comment fallback)'}")
    if not args.no_setup:
        try:
            print("Detecting camera...")
            print("  " + cam.detect().splitlines()[-1])
            cam.configure(STARTUP_SETTINGS)
            if not cam.is_manual():
                print("  NOTE: dial not on M — exposure settings won't apply.")
        except CameraError as e:
            print(f"  camera not ready ({e}) — UI will show it; connect + refresh.")

    # Restore persisted settings so they survive the self-update restart (which
    # used to reset sensor/reshoot to off).
    saved = load_config()
    if isinstance(saved.get("reshoot"), dict):
        set_reshoot(saved["reshoot"])
    if isinstance(saved.get("brightness"), dict):
        set_brightness(saved["brightness"])
    if isinstance(saved.get("advance"), dict):
        try:
            set_advance(saved["advance"])
        except AdvanceError as e:
            print(f"  saved advance config rejected ({e})")

    # Root any persistent gphoto2 session in the output directory from the
    # start, so the first capture inherits a usable session instead of paying to
    # rebuild one (gphoto2 stages downloads in its working directory).
    cam.set_capture_dir(OUT_DIR)
    if cam.persistent:
        # Prove it here, where failure costs nothing, rather than discovering
        # mid-batch that this camera can't hold a session.
        cam.warmup()
        print(f"Fast session:    {'on' if cam._shell else 'unavailable'}")

    # Seed the camera-status cache BEFORE the trigger is armed, so the UI has
    # real values from the first page load. Once the trigger is running the
    # background poll deliberately never touches the camera, and the only other
    # refresh rides on a capture — so without this the pill would read "no
    # camera" until the first frame. Best effort: a camera that is off simply
    # leaves the cache empty, exactly as before.
    if _lock_acquire("startup status", 5):
        try:
            read_status()
        except CameraError as e:
            print(f"  camera not readable at startup ({friendly(str(e))})")
        finally:
            _lock_release()

    # Optical-sensor capture trigger. --sensor CLI flags override the saved
    # config; otherwise restore what was saved. Build the watcher either way so
    # /api/trigger has live config; only "gpio" mode starts a thread + libgpiod.
    if args.sensor:
        trig_cfg = {"mode": "gpio", "sensor_line": args.sensor_line,
                    "active_high": args.sensor_active_high}
    else:
        trig_cfg = saved["trigger"] if isinstance(saved.get("trigger"), dict) else {}
    try:
        set_trigger(trig_cfg)
        if trigger.enabled:
            print(f"Sensor trigger:  {trigger.describe()}")
    except TriggerError as e:
        print(f"  sensor trigger not started ({e}) — enable it later in Setup.")

    # Post-capture worker: brightness correction + EXIF metadata (daemon: a
    # queued frame is never more valuable than a clean shutdown — the capture
    # itself is already saved, only the embedded caption would be missing).
    threading.Thread(target=_post_worker, name="post-capture",
                     daemon=True).start()
    print(f"Brightness:      {brightness.describe(BRIGHTNESS)}")

    srv = QuietServer((args.host, args.port), Handler)
    print(f"\nServing at http://{args.host}:{args.port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        cam.shutdown()


if __name__ == "__main__":
    main()
