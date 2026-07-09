# Film Scanning Gantry Project

## Project Overview
Automated digitization rig for 35mm slides and negatives using a DSLR camera
mounted over a motorized XY gantry. Multiple film strips are laid out on a
backlit light pad; the gantry moves the light pad (or camera) to position each
frame under the lens, and the camera is triggered automatically over USB.

Think of it as a small CNC machine with a camera instead of a spindle.

## Current Focus (2026-07 — read this first)
Priority has shifted from the automated gantry to a **manual, web-based slide
capture tool** the user's wife can operate. The gantry is **DEFERRED** until
this manual capture prototype is proven — the goal is a usable digitization
workflow now, automation later.

- **Deliverable:** `capture_server.py` — a stdlib-only web app (no pip deps).
  Camera on USB + gphoto2; serves a phone/tablet/desktop page with a big
  Capture button and shot review. See "Slide Capture Web App" below.
- **Runtime:** developed and run on the **dev host (WSL)** for now; will move
  to a Raspberry Pi later (Pi runs gphoto2 natively, reachable from any browser
  on the LAN). **RPi deployment details are deferred** — don't build them yet.
- `scanner.py` (gantry dead-reckoning loop) is kept for the automation phase
  but is not the current priority.
- Why not a Windows .exe: gphoto2 is Linux/Mac only, and USB camera access on
  Windows needs a driver swap or the Canon SDK/digiCamControl. A Linux host
  (dev box now, Pi later) + a browser UI sidesteps all of that.

## Hardware

### Camera: Canon Rebel XTi (400D)
- 10.1MP APS-C CMOS sensor, base ISO 100
- Mini-USB (Type B) port, speaks PTP + Canon extensions
- **No live view** (pre-dates Canon live view, which started with XSi/450D) —
  this rules out real-time video-based frame centering
- Supported by gphoto2 (CONFIRMED on this body, gphoto2 2.5.32): remote shutter
  trigger, direct image download + on-card delete, status queries, and full
  remote control of exposure settings (imageformat incl. RAW/RAW+L, iso,
  aperture, shutterspeed, whitebalance, exposurecompensation) — BUT only when
  the physical mode dial is on **M**. In Green/Auto the same config nodes are
  read-only (single-choice: RAW absent, ISO/aperture/shutter locked). There is
  no remote way to change the dial; it must be set to M by hand.
- USB transfer is slow (USB 2.0-era): measured full RAW cycle
  (capture + ~8.4MB CR2 download + delete) is **~5.6 s/frame** (~1.7 MB/s).
  Download dominates and is unavoidable per frame, but overlaps the next
  frame's gantry move+settle, so effective throughput stays near ~5 s/frame.
- **capturetarget:** use `Memory card` (=1). `Internal RAM`/sdram (=0) is
  unreliable on this body over USB/IP — produced no downloadable file, was
  slower, and forced a device re-enumeration.
- **Gotchas:**
  - Camera menu: set Communication to "PC connection" (NOT "Print/PTP") or
    gphoto2 can see the camera but not control it
  - Mode dial MUST be on **M** for remote exposure control (see above)
  - Disable auto power-off for long batch runs
  - Prefer AC adapter (ACK-E2 or DC coupler clone) over the ~20-year-old battery

### Host setup: WSL + USB/IP (CONFIRMED working)
- Camera is attached to WSL via usbipd (`usbipd attach` from Windows); it
  enumerates as USB ID `04a9:3110` and gphoto2 detects it as
  "Canon EOS 400D (PTP mode)".
- **Permissions:** the default device node is `root:root` and libgphoto2's
  udev `uaccess` ACLs do NOT apply under WSL (no logind seat), so libusb hits
  "Access denied (-3)". Fix with a udev rule granting the user's group access:
  `/etc/udev/rules.d/90-canon-camera.rules`:
  `SUBSYSTEM=="usb", ATTR{idVendor}=="04a9", MODE="0664", GROUP="plugdev"`
  (user must be in `plugdev`; run `udevadm control --reload-rules` +
  `udevadm trigger`). This survives reconnects/re-enumeration.
- **Re-enumeration churn:** under USB/IP the device periodically re-enumerates
  (bus/device number changes, e.g. 001,003 → 001,004). The command issued
  *immediately after* a re-enumeration fails with an I/O error (-7) on the
  stale port, then the next one succeeds. **The host script MUST retry PTP
  operations on I/O error** (re-open camera + a short backoff). Certain ops
  (capturetarget change, sdram capture) reliably trigger it; ordinary card
  captures were stable across many consecutive shots.

### Gantry
- XY motion via stepper motors, Arduino-based control
- Plan: Arduino runs GRBL firmware (or FluidNC on ESP32) — do NOT write custom
  stepper firmware. GRBL provides acceleration ramping, homing cycles, limit
  switches, and a G-code serial interface ("G0 X125.4 Y38.1")
- Requirements: homing switches on both axes, steps/mm calibration
  (command 100mm, measure actual, adjust), backlash compensation (always
  approach positions from the same direction)

### Light Source
- High-CRI (95+) LED light pad (user has sourced one meeting requirements)
- Film sits on/above the pad; multiple negative strips loaded at once
- Note: narrowband RGB backlight is theoretically better for color negatives,
  but high-CRI white is the right all-around choice since slides are also
  being scanned

### Optics (recommended, not yet confirmed)
- EF-S 60mm f/2.8 macro or used EF 100mm f/2.8 macro
- On APS-C (~22.2mm sensor width vs. 36mm frame), only ~0.62x magnification
  (a 1:1.6 reduction) is needed to fill the frame with a 35mm negative, so
  extension tubes on a 50mm prime also work

## Camera Settings for Scanning
- ISO 100 (base), aperture f/5.6–f/8 (sharpness sweet spot), manual mode
- Shoot RAW always (essential for negative inversion)
- Custom/fixed white balance off the light source
- Manual focus on film grain; focus once, don't refocus per frame
- Expose to the right without clipping; ignore the orange mask on negatives
- Dim room lights to avoid reflections on the film

## Software Architecture (three layers)

1. **Arduino/GRBL** — dumb motion controller, addressed via G-code over serial
2. **Host computer (Python)** — the brain: sends motion commands, triggers
   camera, tracks state, runs any image analysis
3. **Camera tethering** — gphoto2 / python-gphoto2 bindings (Linux/Mac);
   digiCamControl scripting API is the Windows alternative

### Positioning Strategy: Dead Reckoning (primary, chosen approach)
Because the XTi has no live view, the plan is dead reckoning with good
mechanical fixturing rather than a real-time vision centering loop:
- 35mm frames are ~38mm apart center-to-center (36mm frame + 2mm gap)
- Film holder should mechanically register strips straight at a known pitch
- Capture with a few mm of margin around the expected frame position and crop
  in post — at 10MP there is resolution to spare (~2500+ px across after crop)

Core loop pseudocode:

```python
for row in range(num_strips):
    for frame in range(frames_per_strip):
        x = origin_x + frame * 38.0
        y = origin_y + row * strip_pitch
        grbl.move_to(x, y)      # G0 X{x} Y{y}
        grbl.wait_idle()        # poll "?" until status == Idle
        time.sleep(0.5)         # settle delay for vibration
        camera.capture()        # gphoto2 capture-image-and-download
```

### Fallback Vision Correction (if strips wander beyond crop margin)
No live view, so use a capture-analyze-correct loop instead:
1. Switch camera to small/fine JPEG (fast ~1-2s download)
2. Shoot throwaway frame, download, detect frame boundaries with OpenCV
   (backlit film: threshold + findContours or row/column intensity profiles;
   on negatives the inter-frame gaps and rebate are unexposed → they read as
   uniform bright bands vs. denser image content, so look for bright gaps
   separating darker frames; on slides/mounts it is inverted — dark gaps
   around brighter frames)
3. Compute centroid offset vs image center, convert px→mm via one-time
   calibration (command known 5mm move, measure pixel shift)
4. Corrective move, repeat until offset < ~0.2mm
5. Switch quality back to RAW, take the real capture

### Robustness Features to Build In
- **Settle delay:** 300–500ms after motion stops before firing shutter
- **State file:** write progress (strip, frame, filename) to disk after each
  capture so crashes/jams are resumable
- **Skip detection:** uniformly bright preview = empty slot / end of strip
- **Exposure check:** read histogram after each capture; flag clipped frames
  for re-shoot with longer shutter — shutter IS remotely settable via gphoto2
  (confirmed, dial on M), so auto-reshoot is viable
- **Filename mapping:** encode grid position, e.g. `strip03_frame05.cr2`,
  so physical negatives can be located later

## Slide Capture Web App (current deliverable)
Files (all in repo root, stdlib only):
- `camera.py` — shared camera control: gphoto2 CLI wrapper, one subprocess per
  op, retry-on-IO-error, get/set config, capture. Used by both tools.
- `capture_server.py` — `http.server`-based web app with an embedded
  mobile-friendly page (HTML/CSS/JS inline, no static files). Camera access is
  serialized behind a lock (camera is single-session). Endpoints:
  - `GET /` — the UI (three-mode SPA: Setup / Capture / Review)
  - `GET /api/status` — camera + current-group state (recent, captions, exposure)
  - `GET /api/settings` / `POST /api/settings` — exposure choices+current, and
    apply exposure (iso/aperture/shutterspeed/whitebalance/imageformat) and/or
    the group `prefix`
  - `POST /api/capture` — capture into the current group (returns exposure
    stats; also auto-advances one slide when enabled — see `advance.py`)
  - `POST /api/test` — throwaway setup shot (`_test.*`, not counted) to dial in
    exposure; returns exposure stats
  - `POST /api/advance` — manually advance one slide (test button; no-op error
    when auto-advance mode is `off`)
  - `POST /api/preset` — apply a quick preset (`slides` | `negatives`)
  - `POST /api/caption` — set/clear a per-image caption
  - `POST /api/delete` — delete an image and its RAW sibling (name-guarded)
  - `GET /media/<file>` (path-traversal guarded; `?dl=1` forces download)
  - `GET /thumb/<file>` — tiny embedded EXIF thumbnail (fast Review grid)
  - `GET /api/images?offset=&limit=` — paginated group listing (name, caption,
    cached exposure) for Review
  - `GET /api/zip` — zip of the current group (download-all)
  - `GET /api/exposure?name=` — on-demand exposure stats for an image
- `jpegstats.py` — pure-stdlib JPEG luminance reader for the exposure aid;
  meters off the embedded EXIF thumbnail (fast) via a minimal baseline DC-only
  decoder, else the image. Returns mean/under/over + a status/advice, or None.
- `advance.py` — auto slide-advance output (STUB). Settings-driven
  (`ADVANCE_DEFAULTS`): `mode` = `off` (default, no-op `NullAdvancer`) | `motor`
  (DC motor run until a stop/index switch trips, one pulse == one slide, via
  libgpiod `gpioset`/`gpiomon`, subprocess pattern like gphoto2; jam-protected
  by `timeout_s`) | `stepper` (fixed steps/slide — NOT implemented, points at
  GRBL). `make_advancer(settings)` builds it; tool/hardware checks are deferred
  to `advance()` time so the mode is selectable from any machine. `do_capture`
  calls it after each capture when enabled (`after_capture`); a failed advance
  is reported in the response, never fatal (image is already saved). Motor path
  is written but UNTESTED on hardware — first run is a bring-up.
- `scanner.py` — gantry batch loop (deferred phase), reuses `camera.py`.

UI — three modes (built for high-volume, keyboard-first; see the redesign plan
`~/.claude/plans/sharded-wiggling-wave.md`):
- **Setup** (once/batch): Slides/Negatives preset, group name, exposure
  dropdowns, a **Test shot** to dial in shutter, then *Start capturing*.
- **Capture** (the 95% loop): minimal — large last shot + glanceable exposure
  chip + running count. **Space/Enter = capture**, **R/Backspace = redo last**
  (delete last + recapture), **← →** browse recent. Key auto-repeat guarded;
  beep+toast on failure; updates from the capture response (no status round-trip)
  so the loop stays snappy.
- **Review** (after): thumbnail grid via `/thumb` + `/api/images`, "only
  flagged" filter, lightbox to caption/delete/download, download-all zip.
- Nav: `[` Setup, `]` Review; typing in inputs suppresses shortcuts.

Features:
- **Presets** (`slides`/`negatives`): ISO 100, f/8, daylight WB, format (JPEG
  for slides; RAW+L for negatives, since RAW is essential for inversion), plus a
  starting shutter to fine-tune. Defined in `PRESETS`.
- **Exposure controls**: dropdowns populated live from the camera (need dial on
  M), applied on change.
- **Exposure aid**: verdict (too-dark/ok/overexposed + advice) from the JPEG
  (see `jpegstats.py`), shown on capture and cached per file in `exposure.json`
  so Review flags the whole batch with no recompute. Heuristic; a guide, not a
  meter (less reliable on the orange mask of negatives).
- **File management**: per-image download/delete + download-all zip.
- **Group prefix**: filenames `<prefix>_0001, _0002, …` (per-prefix numbering,
  resumable, supports >9999; sorted numerically). Sanitized to `[A-Za-z0-9_-]`.
- **Per-image captions**: added in the Review pass (kept out of the capture loop
  for speed); stored in `captions.json` sidecar (authoritative) and embedded in
  the image with the group name.
- **Metadata**: group (+caption) written as EXIF `ImageDescription` via
  **exiftool if installed** (JPEG + CR2), else a **JPEG comment** (stdlib; COM
  segments are stripped-then-rewritten so edits don't stack).
- RAW-only capture derives a viewable preview from the CR2's embedded baseline
  JPEG (rejects the lossless sensor stream by SOF marker).

Camera access is serialized behind a lock; retries are tuned short
(`Camera(retries=3, backoff=0.8)`) so the UI fails fast (~2.5s) when the camera
is off rather than hanging ~9s.

Run (dev host):
    python3 capture_server.py            # http://localhost:8080
    python3 capture_server.py --port 8080 --out-dir ./captures --prefix trip72
Default format is Large Fine JPEG ("L"); switch to "RAW + L" (archival CR2 +
JPEG) in the settings drawer or via `STARTUP_SETTINGS`.

IMPORTANT: never run `gphoto2` from another shell while the server is up — it
bypasses the server's camera lock and both fail with I/O errors. Change settings
through the UI/API instead.

Notes / gotchas learned:
- Camera must stay powered: it dropped off USB mid-session once (auto-power-off
  + Low battery). Disable auto-power-off; use the AC dummy-battery coupler.
- Local browser on the dev machine uses `localhost:8080`. Reaching it from
  other LAN devices (her iPad/phone) from WSL needs Windows port-forwarding —
  DEFERRED; the Raspberry Pi will serve the LAN directly.

## Post-Processing
- Negatives require inversion: Negative Lab Pro (Lightroom plugin) is the
  standard; FilmLab or darktable's negadoctor are alternatives
- Slides need only minor correction

## Status / Progress
- [DONE 2026-07-07] Camera tethering validated end-to-end in WSL: USB/IP
  passthrough, permissions (udev rule), remote settings control in M mode,
  full-res RAW capture+download+delete, ~5.6 s/frame cycle time.
- [DONE 2026-07-07] Slide Capture Web App built (`camera.py` +
  `capture_server.py` + `jpegstats.py`): capture/settings/presets/captions/
  exposure-aid/file-mgmt endpoints, disconnected-camera handling, security
  guards, friendly errors — all tested.
- [DONE 2026-07-07] UI redesigned for high-volume digitization: three modes
  (Setup/Capture/Review), keyboard-first capture loop, `/thumb` + `/api/images`
  Review grid, exposure caching. Backend + endpoints verified without camera
  (seeded copies); **live click-through + real capture pending a charged
  battery** (battery died).
- [DEFERRED] Automated gantry + `scanner.py` GRBL wiring.

## Open Items / Next Steps
- Live-test the web capture happy path once the camera is reconnected
- Disable camera auto-power-off; get AC dummy-battery coupler (camera dropped
  off USB mid-session on Low battery)
- Dial in slide exposure (ISO/aperture/shutter) against the light pad
- LATER: move web app to Raspberry Pi (deferred), LAN access, autostart
- LATER: web-based self-update — a UI button that fetches the latest release
  (`git reset --hard <tag>`) and restarts the systemd service, so the appliance
  updates without SSH. `scanner` user already has NOPASSWD sudo; guard against
  updating mid-capture, only move forward, report the new version after restart.
- LATER (appliance UX/security): **first-connect trust prompt** — onboarding
  asks "Trusted home network (no login)" vs "Shared/untrusted (set an access
  PIN)". If PIN: hash it (`hashlib`/`hmac`), signed session cookie via
  `secrets`, gate all endpoints. Pure stdlib, re-promptable in Setup. This is
  the clean fix for the web UI having no auth today.
- LATER (appliance polish): serve on **port 80** instead of `:8080`. It's on
  8080 only because Comitup's captive portal must own :80 during AP mode (phone
  captive-portal detection hits :80). End state: fold WiFi provisioning into the
  app (drive `nmcli`/Comitup API), drop `comitup-web`, one service on :80 for
  both setup and scanning — also shrinks attack surface. Interim: re-add
  `CAP_NET_BIND_SERVICE` + relocate comitup-web (loses the auto portal popup),
  or just hide the port behind an `slidescanner.local` QR bookmark.
- LATER (pre-public hardening gate): security review of the appliance. Threat
  model is an immutable appliance — reflash the SD card to recover. Steps:
  remove/disable the SSH server (keep it ONLY until the first hardware bring-up
  succeeds — it's the lifeline if the Comitup AP flow fails); scope the
  `scanner` sudoers from NOPASSWD:ALL to a command allowlist; default-deny
  inbound firewall (allow 8080, mDNS 5353, and :80 only in AP mode); audit
  `capture_server.py` (path-traversal guards, subprocess arg-lists not shell,
  prefix/name/caption sanitization); disable unused services. Note the web app
  itself is LAN-only + behind home NAT (no WAN exposure, no auth today).
- LATER (gantry phase): GRBL gantry, film carrier, lens/magnification, replace
  `GrblStub` with real serial control
