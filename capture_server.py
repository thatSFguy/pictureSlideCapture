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
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse, parse_qs

import jpegstats
from advance import ADVANCE_DEFAULTS, AdvanceError, make_advancer
from camera import Camera, CameraError

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
cam_lock = threading.Lock()          # camera is single-session: serialize access
OUT_DIR = Path("./captures")
PREFIX = "slide"
HAVE_EXIFTOOL = shutil.which("exiftool") is not None

# Auto slide-advance output (stub; default "off" == no-op). See advance.py.
ADVANCE = dict(ADVANCE_DEFAULTS)
advancer = make_advancer(ADVANCE)


def set_advance(cfg: dict) -> dict:
    """Merge new advance settings and rebuild the advancer. Lock held.
    Commits only if the new config builds — bad config raises AdvanceError
    without corrupting the live advancer."""
    global ADVANCE, advancer
    merged = {**ADVANCE, **{k: cfg[k] for k in ADVANCE_DEFAULTS if k in cfg}}
    new = make_advancer(merged)               # may raise AdvanceError
    ADVANCE, advancer = merged, new
    return ADVANCE


def _advance_once() -> dict:
    """Advance one slide, mapping failures to a result dict (never raises)."""
    try:
        advancer.advance()
        return {"ok": True, "mode": advancer.mode}
    except (AdvanceError, NotImplementedError) as e:
        return {"ok": False, "error": str(e) or "advance not implemented"}


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


def _grab(stem: str) -> tuple:
    """Capture to <stem>.<ext>, normalize case, derive a preview if RAW-only.
    Returns (jpg, raw, derived) or raises CameraError. Assumes cam_lock held.
    capturetarget=Memory card is set inside cam.capture() (same gphoto2 session)
    so the 400D can't re-enumerate back to Internal RAM between set and shot."""
    glob = f"{stem}.*"
    _wipe(glob)                                    # clear any prior file at stem
    try:
        cam.capture(OUT_DIR / f"{stem}.%C")
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
        raise CameraError("captured but no displayable image — set the format "
                          "to Large JPEG or 'RAW + L'")
    return jpg, raw, derived


def do_capture() -> dict:
    """Capture one frame into the current group. Assumes cam_lock held."""
    prefix, n = PREFIX, next_index(PREFIX)
    jpg, raw, derived = _grab(f"{prefix}_{n:04d}")
    write_metadata(jpg, raw)
    stats = jpegstats.luma_stats(jpg)
    if stats:                                      # cache verdict for Review
        ex = load_exposure()
        ex[jpg.name] = stats.get("status")
        save_exposure(ex)
    # Auto-advance to the next slide (no-op unless enabled). The image is
    # already saved, so a failed advance is reported, not fatal.
    adv = _advance_once() if (advancer.enabled and ADVANCE.get("after_capture")) \
        else None
    return {"ok": True, "name": jpg.name, "index": n, "count": image_count(prefix),
            "raw": raw.name if raw else None, "preview_from_raw": derived,
            "exposure": stats, "advance": adv}


def do_advance() -> dict:
    """Manual one-slide advance (test button / decoupled from capture)."""
    if not advancer.enabled:
        return {"ok": False, "error": "auto-advance is off"}
    return _advance_once()


def do_test() -> dict:
    """Throwaway setup shot to dial in exposure (not counted). Lock held."""
    jpg, _raw, _d = _grab("_test")
    return {"ok": True, "name": jpg.name, "exposure": jpegstats.luma_stats(jpg)}


def read_status() -> dict:
    """Light status: one batched gphoto2 call. Assumes cam_lock held."""
    try:
        model = cam.model()
        v = cam.get_many(["batterylevel", "autoexposuremode", "availableshots",
                          "iso", "aperture", "shutterspeed", "imageformat"])
    except CameraError as e:
        return {"connected": False, "error": friendly(str(e)), "prefix": PREFIX,
                "count": image_count(PREFIX), "recent": recent_images(PREFIX),
                "captions": load_captions(), "exposure": load_exposure()}
    mode = v.get("autoexposuremode", "?")
    return {
        "connected": True, "model": model,
        "battery": v.get("batterylevel", "?"), "mode": mode,
        "manual": mode.lower() == "manual", "shots": v.get("availableshots", "?"),
        "iso": v.get("iso", "?"), "aperture": v.get("aperture", "?"),
        "shutter": v.get("shutterspeed", "?"), "format": v.get("imageformat", "?"),
        "prefix": PREFIX, "count": image_count(PREFIX),
        "recent": recent_images(PREFIX), "captions": load_captions(),
        "exposure": load_exposure(),
    }


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


def read_images(offset: int, limit: int) -> dict:
    """Paginated listing of the current group for Review (no camera needed)."""
    imgs = group_images(PREFIX)                    # newest first
    caps, ex = load_captions(), load_exposure()
    page = imgs[offset:offset + limit]
    items = [{"name": f.name, "caption": caps.get(f.name, ""),
              "exposure": ex.get(f.name, "")} for f in page]
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


def update_apply() -> dict:
    """Check out the latest release tag, then restart the service."""
    info = update_check()
    latest = info["latest"]
    if not latest:
        return {"ok": False, "error": "no release tags found on origin"}
    if not info["available"]:
        return {"ok": False, "current": info["current"],
                "error": f"already up to date ({info['current']})"}
    _git(["checkout", "--force", latest], timeout=60)
    threading.Timer(1.2, _restart_service).start()   # let this response flush
    return {"ok": True, "from": info["current"], "to": latest, "restarting": True}


def _restart_service() -> None:
    subprocess.run(["sudo", "-n", "systemctl", "restart", SERVICE_NAME],
                   capture_output=True, text=True)


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
         "advance_mode": ADVANCE.get("mode")}
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
    return d


# ---- HTTP handler ---------------------------------------------------------

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

    def _with_camera(self, fn):
        if not cam_lock.acquire(blocking=False):
            return self._json({"ok": False, "busy": True,
                               "error": "camera busy"}, 409)
        try:
            self._json(fn())
        except CameraError as e:
            self._json({"ok": False, "error": friendly(str(e))}, 500)
        finally:
            cam_lock.release()

    def _guarded_update(self, check_only: bool):
        # Serialize against capture (same lock) so we never restart mid-shot.
        if not cam_lock.acquire(blocking=False):
            return self._json({"ok": False, "busy": True,
                               "error": "busy — finish the capture first"}, 409)
        try:
            self._json(update_check() if check_only else update_apply())
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)
        finally:
            cam_lock.release()

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path == "/":
            self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
        elif path == "/api/status":
            self._with_camera(read_status)
        elif path == "/api/settings":
            self._with_camera(read_settings)
        elif path == "/api/zip":
            self._serve_zip()
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
        elif path.startswith("/thumb/"):
            self._serve_thumb(unquote(path[len("/thumb/"):]))
        elif path.startswith("/media/"):
            self._serve_media(unquote(path[len("/media/"):]), "dl" in q)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/capture":
            self._with_camera(do_capture)
        elif path == "/api/test":
            self._with_camera(do_test)
        elif path == "/api/advance":
            self._with_camera(do_advance)   # lock: never advance mid-capture
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

    def _serve_media(self, name: str, download: bool):
        f = self._safe(name)
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

    def _serve_zip(self):
        rx = name_re(PREFIX)
        files = [f for f in sorted(OUT_DIR.glob(f"{PREFIX}_*")) if rx.match(f.name)]
        if not files:
            return self._send(404, b"no files in group", "text/plain")
        # Build to a temp file on the SAME disk (not /tmp, which is tmpfs/RAM on
        # the Pi), then stream it — avoids buffering a whole batch in 512 MB RAM.
        tmp = tempfile.NamedTemporaryFile(dir=OUT_DIR, suffix=".zip", delete=False)
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
                for f in files:
                    z.write(f, f.name)
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
  .row button, #testShot, #checkUpd, #startCap { border:none; border-radius:8px; color:#fff;
           background:#2f7bd6; padding:.55rem .9rem; cursor:pointer; font-size:.9rem; }
  #testShot { width:100%; padding:.75rem; font-weight:600; }
  #checkUpd { margin-left:.5rem; background:#333; }
  #startCap { width:100%; padding:1rem; font-size:1.1rem; font-weight:700; margin-top:1.3rem; }
  #testWrap { margin-top:.2rem; background:#0c0c0c; border:1px solid #222;
              border-radius:10px; padding:.6rem; text-align:center; }
  #testImg { max-width:100%; max-height:44vh; border-radius:6px; display:none; margin-top:.5rem; }
  .note { font-size:.72rem; color:#777; margin-top:.4rem; }
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
          <div class="note" id="testMsg">Take a throwaway shot to check exposure (not counted).</div>
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
        </div>
        <pre id="diagOut"></pre>
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
      <button id="revZip">⬇ Download all (zip)</button>
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
    <button id="lbDel" class="del">🗑 Delete</button>
    <button id="lbClose">Close (Esc)</button>
  </div>
</div>
<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
const EXP = ['imageformat','iso','aperture','shutterspeed','whitebalance'];
const EXPO = {ok:['✓ Good','good'],dark:['⚠ A bit dark','warn'],bright:['⚠ A bit bright','warn'],
              under:['✕ Too dark','bad'],over:['✕ Overexposed','bad']};
const FLAG = {dark:1,bright:1,under:1,over:1};
let mode='setup', ST={recent:[],captions:{},exposure:{},prefix:'slide',count:0};
let capIdx=0, busy=false;
let rev={items:[],total:0,offset:0,limit:60,lbIdx:-1};

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
  if(m==='setup'){ loadSettings(); loadVersion(); }
  if(m==='review') loadReview(true);
  if(m==='capture'){ capIdx=0; renderCap(); }
}

/* ---- status ---- */
async function status(){
  let s; try{ s=await jget('/api/status'); }catch(e){ $('#camstat').textContent='offline'; $('#camstat').className='pill bad'; return; }
  if(s.busy) return;
  ST.prefix=s.prefix||'slide'; ST.count=s.count||0;
  ST.recent=s.recent||[]; ST.captions=s.captions||{}; ST.exposure=s.exposure||{};
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
  img.src='/media/'+name+'?t='+Date.now(); img.style.display='block'; pl.style.display='none';
  $('#capName').textContent=name+(capIdx?' ('+(capIdx+1)+' back)':'');
  chipFor($('#chip'), ST.exposure[name]);
}
async function capture(){
  if(busy) return; busy=true; $('#shoot').disabled=true; $('#spinner').style.display='flex'; $('#capMsg').textContent='';
  try{
    const d=await jpost('/api/capture');
    if(d.ok){ ST.count=d.count; ST.recent.unshift(d.name); if(d.exposure) ST.exposure[d.name]=d.exposure.status;
      capIdx=0; $('#grpcount').textContent=ST.prefix+' · '+ST.count; renderCap();
      $('#capMsg').textContent='Saved '+d.name; $('#capMsg').className='ok'; }
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
  await status(); $('#prefix').value=ST.prefix; toast('Group: '+ST.prefix,'ok'); }
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

/* ---- diagnostics ---- */
async function showDiag(){
  const o=$('#diagOut'); o.style.display='block'; o.textContent='Loading diagnostics…';
  try{ o.textContent=JSON.stringify(await jget('/api/diag'),null,2); }
  catch(e){ o.textContent='Failed to load diagnostics.'; }
}
async function showLogs(){
  const o=$('#diagOut'); o.style.display='block'; o.textContent='Loading logs…';
  try{ const d=await jget('/api/logs?lines=400');
    o.textContent = d.ok ? d.text : ('logs unavailable: '+(d.error||'?')); }
  catch(e){ o.textContent='Failed to load logs.'; }
}

/* ---- review ---- */
function eclass(st){ if(!st) return 'e-none'; if(st==='ok') return 'e-ok';
  if(st==='under'||st==='over') return 'e-bad'; return 'e-warn'; }
async function loadReview(reset){
  if(reset){ rev.items=[]; rev.offset=0; }
  const d=await jget('/api/images?offset='+rev.offset+'&limit='+rev.limit);
  rev.total=d.total; rev.items=rev.items.concat(d.items); rev.offset=rev.items.length;
  renderGrid();
}
function visibleItems(){ return $('#flagOnly').checked ? rev.items.filter(i=>FLAG[i.exposure]) : rev.items; }
function renderGrid(){
  const g=$('#grid'); g.innerHTML=''; const items=visibleItems();
  $('#revInfo').textContent = ST.prefix+' — '+rev.total+' image'+(rev.total===1?'':'s')
    + ($('#flagOnly').checked?' ('+items.length+' flagged shown)':'');
  items.forEach((it)=>{
    const idx=rev.items.indexOf(it);
    const t=document.createElement('div'); t.className='tile';
    t.innerHTML='<img loading="lazy" src="/thumb/'+encodeURIComponent(it.name)+'">'
      +'<span class="edot '+eclass(it.exposure)+'"></span>'
      +(it.caption?'<span class="cap">'+it.caption.replace(/</g,'&lt;')+'</span>':'');
    t.onclick=()=>openLB(idx); g.appendChild(t);
  });
  $('#loadMore').style.display = (!$('#flagOnly').checked && rev.items.length<rev.total)?'block':'none';
}
function openLB(idx){ rev.lbIdx=idx; const it=rev.items[idx]; if(!it) return;
  $('#lbImg').src='/media/'+encodeURIComponent(it.name)+'?t='+Date.now();
  $('#lbCaption').value=it.caption||'';
  $('#lbInfo').textContent=it.name+'  ·  '+(EXPO[it.exposure]?EXPO[it.exposure][0]:'exposure n/a');
  $('#lb').classList.add('open');
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
$('#checkUpd').onclick=checkUpdate;
$('#diagBtn').onclick=showDiag; $('#logBtn').onclick=showLogs;
$('#startCap').onclick=()=>setMode('capture');
$('#shoot').onclick=capture; $('#redo').onclick=redoLast;
$('#revRefresh').onclick=()=>loadReview(true); $('#flagOnly').onchange=renderGrid;
$('#revZip').onclick=()=>location.href='/api/zip'; $('#loadMore').onclick=()=>loadReview(false);
$('#lbNavL').onclick=()=>lbNav(-1); $('#lbNavR').onclick=()=>lbNav(1);
$('#lbClose').onclick=closeLB; $('#lbDel').onclick=lbDelete; $('#lbSave').onclick=lbSave;
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

setMode('setup'); status(); setInterval(status, 15000);
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
    args = p.parse_args()

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

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"\nServing at http://{args.host}:{args.port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
