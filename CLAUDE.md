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
- **capturetarget:** prefer `Internal RAM`/sdram (=0) — the frame is buffered in
  the camera and downloaded straight to the host, so it's fast and never touches
  or wears the CF card. `Memory card` also downloads fine. (Both were verified
  via the `/api/debugcapture` matrix once the real bug — a non-writable working
  directory, see gotchas — was fixed. Earlier notes blaming capturetarget for
  "no downloadable file" were wrong.)
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
Files (all in repo root; stdlib only, with one OPTIONAL apt dependency —
`python3-pil` for `brightness.py`, which no-ops without it):
- `camera.py` — shared camera control: gphoto2 CLI wrapper, one subprocess per
  op, retry-on-IO-error, get/set config, capture. Used by both tools.
  `capture(..., on_shutter=)` streams gphoto2's stdout (`_run_streaming`) and
  calls back the instant the camera reports it has the frame — exposure over,
  download still to come. That split is what makes the sensor-to-shutter latency
  measurable; see "Throughput" below, it is the rig's binding constraint.
  **The streaming path needs its own watchdog timer**, not a deadline checked in
  the read loop: a wedged gphoto2 prints nothing, so the loop blocks in
  `readline()` forever and the check is never reached. (`subprocess.run`'s
  `timeout` covered this for free; losing it would let a sleeping camera pin the
  camera lock indefinitely. Caught by a test with a stub that hangs before its
  first line of output.)
  `ready()`/`wait_ready()` probe the camera for the next shot: the 400D drops off
  the USB bus for ~1-2s after each SDRAM capture, so before firing a rapid
  follow-up shot (auto-reshoot, auto-exposure) we poll a cheap config read until
  it answers again — waits exactly as long as needed instead of a blind sleep.
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
  - `GET /api/trigger` — optical-sensor config + live level (for polarity);
    `POST /api/trigger` — apply sensor-trigger config (`mode`/`active_high`/…)
  - `POST /api/reshoot` — set auto-reshoot (`enabled`/`max`): after a capture,
    if the frame is flagged dark/over, step the shutter toward correct and
    reshoot the SAME frame (keeping the best of ≤`max` tries), then restore the
    baseline shutter. Fixes the odd dense slide in place during an auto-run
    without drifting the rest. Reshoot captures go to temp stems, the winner is
    promoted onto the frame, so an overshoot never leaves it worse. Default off.
  - `GET /api/brightness` / `POST /api/brightness` — digital brightness
    correction config (`enabled`/`mode`/`target`/`max_ev`/`keep_original`), plus
    `available` (is python3-pil installed) and `pending` (queue depth). Lock-free
    like `/api/trigger`, so the Setup toggle shows its real state mid-capture.
  - `POST /api/revert` — undo a brightness correction: restore the stashed
    original over the corrected frame and re-meter it
  - `GET /api/install` / `POST /api/install` — install an optional native
    dependency **from inside the app**, for an appliance whose operator has no
    shell and no sudo (self-update ships code, not apt packages). Runs
    `sudo -n apt-get update && apt-get install -y <pkgs>` on a background thread
    (minutes on a Pi Zero W — way past any browser timeout), so POST starts it
    and the UI polls GET for `running`/`elapsed`/`log`/`success`. Refuses to
    start while the camera lock is held so it can't steal CPU mid-batch.
    **`importlib.invalidate_caches()` is NOT enough** (v0.1.31 shipped assuming
    it was, and the install failed on hardware with apt reporting
    *"python3-pil is already the newest version"* while the running process
    still couldn't import it): a package that appears after interpreter startup
    isn't reliably picked up, so the service must restart. The installer decides
    which case it's in instead of restarting blindly — it asks a **fresh
    interpreter** (`_fresh_import_ok`, `sys.executable -c "import PIL.Image"`):
    fresh-import OK → only this process is stale → take `cam_lock` and restart
    (UI polls for it to come back); fresh-import ALSO fails → the library itself
    is broken (missing shared lib, ABI mismatch) → a restart would change
    nothing, so report the child's real error instead.
    **Security:** the request names a *feature* from the fixed `APT_FEATURES`
    table (`brightness` → `python3-pil`); no client string ever reaches the apt
    command line. When the sudoers NOPASSWD:ALL → allowlist hardening lands,
    `apt-get` needs an entry. Two API traps found and fixed while building it:
    the install RESULT field must NOT be called `ok` (it merged over the
    request-level `ok`, so a started install replied `{"ok": null}`), and the
    status snapshot helper must not re-take `_install_lock` (a second install
    request — double-tap, or a second tab — deadlocked that request thread).
    **Installer output goes to the JOURNAL as well as the UI** (`[install]`
    prefix). v0.1.31 kept it in memory only, so the one artifact an operator can
    export — `journalctl` via *View logs* — showed a bare `FAILED` with no apt
    output, and the real cause was unreachable without a shell the appliance
    doesn't have. Anything that can fail on the appliance must log to the
    journal.
  - The appliance is on Debian **trixie** (Pi OS Lite arm64), not bookworm;
    `python3-pil` there is Pillow 11.1.0. Don't assume bookworm package versions.
  - `GET /api/version` — current app version (`git describe`)
  - `GET /api/update` — check origin for a newer release tag; `POST /api/update`
    — check out the latest tag + restart the service (in-app self-update; git +
    passwordless sudo on the appliance; serialized behind the camera lock so it
    never restarts mid-capture). Repo is **public** so the Pi pulls with no creds.
  - `GET /api/logs?lines=` — tail the systemd journal for `slidescanner` (via
    `sudo journalctl`); in-app troubleshooting with no SSH
  - `GET /api/diag` — system + live camera snapshot (version, gphoto2, and the
    actual `capturetarget`/`imageformat`/mode/shots/battery). Surfaced in the UI
    under **Setup → System** (Camera diagnostics / View logs).
  - `POST /api/preset` — apply a quick preset (`slides` | `negatives`)
  - `POST /api/caption` — set/clear a per-image caption
  - `POST /api/delete` — delete an image and its RAW sibling (name-guarded)
  - `POST /api/deleteall` — delete the whole current group + RAW siblings and
    clear its caption/exposure caches (prefix-guarded so a stale tab can't wipe
    the wrong group); Review "Delete all" button, for clearing after download
  - `GET /media/<file>` (path-traversal guarded; `?dl=1` forces download,
    `?orig=1` serves the pre-correction copy from `captures/originals/`)
  - `GET /thumb/<file>` — tiny embedded EXIF thumbnail (fast Review grid)
  - `GET /api/images?offset=&limit=` — paginated group listing (name, caption,
    cached exposure) for Review
  - `GET /api/zip` — zip of the current group (download-all). Pre-correction
    copies ride along under `originals/` in the archive; without that,
    download-all + delete-all would silently discard them and bake every
    brightness correction in permanently. `?originals=0` opts out (halves the
    zip when every frame was corrected).
  - `GET /api/exposure?name=` — on-demand exposure stats for an image
- `brightness.py` — **digital brightness correction** (the fixed-backlight fix).
  Slide density varies per frame and the light pad can't be re-dialed per shot,
  so after each capture a flagged frame is pulled onto a target brightness by
  re-encoding the JPEG. Complements `_auto_reshoot` (optical): this costs no
  shutter actuation and no extra ~8s capture cycle, but can't recover clipped
  highlights — reshoot is still the fix for those.
  - **Curve = gamma, not linear gain**: `out = 255*(in/255)**g` with `g` solved
    so the metered mean lands on `target`. Pins 0 and 255, so brightening can
    never clip highlights (a linear gain would blow any specular hot spot).
  - Bounded by `max_ev` (default ±1.5 stops) and skipped below `MIN_MEAN`=8 (a
    blank/empty slot is noise, not an underexposure), so a genuinely dark night
    slide can't be wrecked. The untouched capture is copied to
    `captures/originals/<name>` (`keep_original`) — Review can compare
    (`/media/<f>?orig=1`) and undo (`POST /api/revert`).
  - **Needs `python3-pil` (apt, NOT pip — consistent with gphoto2/gpiod/
    exiftool).** Pure Python can't re-encode a 10MP JPEG in usable time. Every
    entry point degrades to a no-op when PIL is absent and the UI says so.
    It is NOT bundleable as code: it's a C extension against libjpeg/zlib, and a
    pure-Python decode+encode of a 10MP frame is minutes/frame on a Pi. (A
    DCT-domain trick can shift brightness without a full decode, but only as a
    linear DC offset — it lifts blacks and clips highlights, i.e. strictly worse
    than the gamma curve. Rejected. Scaling the DC *quant table* is even worse:
    it scales deviation from mid-gray, which is contrast, not brightness.)
  - **Installing it needs no SSH** — see `APT_FEATURES` / `POST /api/install`
    below. The appliance is sealed (no shell for the operator) and self-update
    pulls code but not apt packages, so the app installs the package itself.
    Re-flashing also works: `python3-pil` is now in `deploy` PKGS, so new images
    ship with it.
  - **Encoding fidelity:** re-encodes with the CAMERA's own quantization tables
    (`qtables=src.quantization`) and chroma sampling (4:2:2 on the 400D) — no
    second-generation quality cliff. `subsampling="keep"` does NOT work here:
    `point()` returns a plain image, not a JPEG-backed one, and PIL raises
    *"Cannot use 'keep' when original image is not a JPEG"* — which silently
    fell back to quality-92 4:2:0 and shrank files ~40% before it was caught.
    Use the numeric sampling from `JpegImagePlugin.get_sampling()` instead.
    Expect a lifted frame to grow (~137% at +1.5EV): lifted shadows carry more
    entropy at the same tables. That's honest, not a bug.
  - **EXIF is spliced over byte-for-byte** from the original (keeps MakerNotes;
    doesn't depend on PIL's exif-writing quirks) and the **embedded thumbnail is
    put through the same curve** — otherwise Review's `/thumb` grid AND
    `jpegstats` (which meters off that thumbnail) would both still report the
    uncorrected frame. The thumbnail is re-encoded to fit its ORIGINAL byte slot
    and only the length tag is patched, so every EXIF offset stays valid.
  - **RAW is never touched**, and RAW-derived previews (`imageformat=RAW`, where
    the JPEG is just extracted from the CR2) are skipped.
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
  **Motor drive pin — reserved GPIO10 (phys pin 19).** For a low-side N-channel
  MOSFET gate (set `motor_line=10`, keep `motor_active_high=true`). Chosen
  because: (1) it's free — GPIO10 is SPI0 MOSI but SPI is disabled on the
  appliance; (2) **boot-safe** — GPIO9–27 default to an internal *pull-down*, so
  the gate sits LOW (motor OFF) at power-up (GPIO0–8 pull *up* — avoid those for
  a motor); (3) physically next to the sensor pins (18/GPIO24) with GND on pin
  20, so it's one extra solder joint near the ones already needed. Still add a
  hardware ~10k gate→GND (the internal pull is weak and only after SoC config),
  ~100–220Ω gate series, a flyback diode across the motor, and power the motor
  from its own supply with ground common to the Pi. NOTE: the motor advance is
  **switch-based** (runs until `switch_line`, default GPIO27, trips) — with only
  the drive pin and no index switch it just runs to `timeout_s`; a purely timed
  run mode is not implemented.
- `gpiocli.py` — version-aware libgpiod CLI argv builders (`get_cmd`/`mon_cmd`/
  `set_hold_cmd`/`parse_level`). **libgpiod v1 and v2 have incompatible syntax**
  (v2, on recent Pi OS: `gpioget -c <chip> <line>`, `gpiomon -e rising -c …`; v1:
  `gpioget <chip> <line>`, `gpiomon --rising-edge …`). A hard-coded v1 command
  fails on v2 with `cannot find line 'gpiochip0'` (v2 parses the chip as a line
  name). `major()` detects the version once; `trigger.py` + `advance.py` use it
  so both work on either. Reads parse v1 (`0`/`1`) and v2 (`24=inactive`).
- `trigger.py` — optical-sensor capture trigger (settings-driven, default
  `off`). **Two daemon threads** (libgpiod via subprocess through `gpiocli`, same
  pattern as advance.py — needs `apt install gpiod`, now in the appliance image):
  a **reader** streams edges from one long-lived `gpiomon` in real time and
  records the **timestamp** of each debounced edge as a coalesced "capture
  requested"; a **worker** performs captures one at a time on the
  **unobstructed→obstructed** edge. **Key hardware quirk:** the 400D's USB
  re-enumeration fires a **phantom obstruct edge on the sensor line as each
  capture finishes** (confirmed in the logs — an edge at the exact second a shot
  completes, which a hands-off operator can't produce).
  The transient is rejected **by timestamp, not by a blanket settle**:
  `sensor_capture` returns the moment the camera finished its USB work
  (`LAST_USB_DONE`), and an edge landing within `cooldown_s` (default **1.0s**,
  plus `_PHANTOM_LEAD`=0.5s of slack before it) of that moment is discarded.
  Everything else is honoured immediately, **including an edge that arrived
  mid-capture** — a real slide dropped while the previous frame was downloading.
  **This replaced a settle-and-clear model (wait 2s after every shot, then throw
  away whatever queued during it) that was losing slides.** The 2026-08-09
  journal proved it: in the 18:37:22–18:38:15 stretch, when the operator fed
  faster than the ~8s cycle, five `edge -> capture requested` lines have no
  matching `edge detected -> capturing` — silent frame loss, plus ~2s of dead
  time on every slide. Note the journal also shows **no phantom edge at all**
  through most of a 4-hour run, so the transient is rarer than the old design
  assumed and a wide blind spot was never worth its cost.
  **Coalescing keeps the EARLIEST pending edge, not the latest** — this is
  load-bearing and was a bug when first written the other way: the phantom
  arrives during the callback's tail, so a "latest wins" slot let it overwrite a
  real mid-capture drop, and the worker then judged the *phantom's* timestamp,
  discarded it, and lost the real slide with it.
  (An earlier attempt kept mid-capture drops by level-checking the beam
  with `gpioget`, but the reader's own `gpiomon` holds the line, so every check
  failed *"sensor read failed"* and dropped the slide — that regression is
  removed.) **Polarity is configurable** because sensor OUT idle state
  varies: `active_high=False` (default) treats obstructed as LOW → **falling**
  edge; `active_high=True` → **rising** edge. `bias` sets an internal pull-up
  (default, gives an open-collector sensor a defined idle) / pull-down / disable.
  The reader coalesces contact bounce with a short fixed debounce
  (`_READER_BOUNCE`=0.3s). `read_level()` (`gpioget`, used only from the UI when
  the watcher isn't holding the line) reads the raw 0/1 so polarity can be
  discovered by blocking the beam. The
  trigger callback (`sensor_capture`) is serialized behind the same `cam_lock`,
  so a sensor shot and a button press never overlap; if the camera is busy the
  trigger is skipped, not queued.
  Wiring (Pi 40-pin): **OUT→GPIO24 (phys pin 18)**, **VCC→3V3 (pin 1)**,
  **GND→pin 6**. Power from 3V3 so OUT can't exceed the Pi's 3.3V-only GPIO; if
  the sensor needs 5V (pin 2/4), level-shift/divide OUT down to 3.3V first.
  Endpoints: `GET /api/trigger` (config + live level), `POST /api/trigger`
  (apply `mode`/`active_high`/`sensor_line`…). CLI: `--sensor`,
  `--sensor-line N`, `--sensor-active-high`. Configurable from the UI
  (Setup → Sensor trigger); UNTESTED on hardware — first run is a bring-up.
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
  flagged" filter, lightbox to caption/delete/download, download-all zip, and
  **Delete all** (clear the group after downloading). Brightness-corrected frames
  get **👁 Original** (toggles the lightbox to `?orig=1`) and **↩ Undo brighten**
  (`/api/revert`). The grid **auto-syncs**
  every 4s while active + on tab focus (reflects an ongoing auto-run's new
  frames and deletes from any device); paused while the lightbox is open.
- Nav: `[` Setup, `]` Review; typing in inputs suppresses shortcuts.
- **Back-button guard**: the SPA switches modes with DOM toggles (no history
  navigation), so a stray browser Back — or a tablet edge-swipe-back — would
  leave the app mid-run. `guardBack()` seeds a history entry on load and re-pushes
  it on every `popstate`, so Back can't navigate away (it shows a hint toast and
  stays put); closing the tab still exits.

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
- **UI rule learned the hard way:** never `disabled` a checkbox that defaults to
  CHECKED. The brightness toggle was disabled while the library was missing,
  which left the operator looking at a ticked control they couldn't untick —
  reading as "this is running and I can't stop it" when in fact nothing was
  being changed. Keep the control live; explain state in the note.
- **Post-capture worker** (`_post_q` / `_post_one`) — ONE background thread doing
  everything the operator must not wait for: the brightness correction, then the
  EXIF metadata write, then a re-meter if the pixels moved. One job per frame, so
  each file is touched by exactly one thread (a separate metadata job would
  rewrite the file and invalidate brightness's stale-swap token).
  **The metadata write moved here from the capture path** — `exiftool` is a large
  Perl program whose cold start alone costs seconds on a Zero W, and nothing in
  the capture response depends on it, so inline it was pure operator wait.
- **Brightness correction** (Setup → Brightness correction; see `brightness.py`):
  after each capture, a flagged frame is queued for a gamma correction onto the
  target. Runs on the post-capture worker, **NOT in the capture path** — a 10MP
  decode+encode is ~0.5s on the dev box but 4-6s on a Pi Zero W (measured in the
  journal), against an ~8s capture cycle, so queuing keeps the Capture button
  snappy and lets the next shot fire while the last one is still being corrected.
  The capture response reports the planned `ev` (Capture shows "brightening
  +0.9 EV" and a `correcting` chip); the worker then rewrites the file, applies
  the group/caption metadata, and re-meters, which Review's 4s auto-sync picks up.
  **Stale-swap guard:** group filenames get reused (Redo-last frees a number and
  the next shot takes it back), so each queued job carries a size+mtime token and
  the worker refuses to swap if the frame changed while it was queued — otherwise
  a slow correction could drop stale pixels onto a brand-new capture.
- **File management**: per-image download/delete + download-all zip.
- **Settings persistence**: sensor-trigger, auto-reshoot, brightness and advance
  config are saved to `config.json` and restored at startup, so they survive the
  self-update restart (which used to reset them to off). The trigger + reshoot +
  brightness toggles also load from the lock-free `/api/trigger`, so the UI shows
  their real state even while the camera is busy.
- **Camera lock diagnostics**: `cam_lock` records its holder + hold time; a
  "busy" response says *what* holds it and for *how long* (e.g. `busy: sensor
  capture (12.3s)`), and `/api/diag` shows `camera_lock`. gphoto2 op timeouts are
  bounded (config 25s, capture 30s) so a wedged/asleep camera frees the lock
  promptly instead of pinning it for ~90s.
- **Lightweight preview**: the Capture-mode last-shot preview loads the tiny
  embedded thumbnail (few KB), not the full ~3MB frame — click it to load full
  res. Sensor captures run server-side, so a hands-off batch needs no browser
  open at all (open Review afterward).
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

### Throughput — READ THIS BEFORE OPTIMISING ANYTHING
There are thousands of slides to get through, and the binding constraint is
**sensor edge → shutter**, not the capture cycle. Optimising anything after the
shutter is wasted effort.

**The mechanism (as described by the user, 2026-08-09).** The slide pusher is a
rotary arm on a **continuously running motor with a manual speed controller —
NOT controlled by the Pi**. Per revolution:
- **3 o'clock** — the arm obstructs the light bar; the sensor fires. Rotation is
  counter-clockwise.
- **12 o'clock** — best case, where the picture is actually taken.
- **10–11 o'clock** — where it typically lands.
- **9 o'clock** — the new slide begins pushing the *old* one out of position.
  Anything not exposed by here is a spoiled frame (the operator retrieves the
  slide and re-feeds it).

So the shutter has to fire within **half a revolution** of the trigger, and the
motor speed must be set for the **WORST CASE** latency, not the average.
**Variance is therefore as expensive as the mean** — one slow outlier forces the
whole run to be slower. The ~5s USB download after the shutter is irrelevant to
this race: once the camera has the frame, the slide is free to move.

**The number to watch** is logged per sensor frame:
`[trigger] SHUTTER at +X.XXs from edge (lock wait Y.YYs)` — emitted the moment
gphoto2 reports the frame is on the camera (`Camera._SHUTTER_MARKERS`, streamed
live from gphoto2's stdout by `_run_streaming`, not inferred after the download).

**Correction to an earlier wrong conclusion.** It was previously recorded here
that "trigger latency is zero, there is no lock contention to chase". That was
wrong and cost time. `trigger._serve` logs `edge detected -> capturing` **before**
`_fire()` → `sensor_capture()` → `_lock_acquire(..., 8)`, so **every camera-lock
wait is invisible in the journal**. Same-second log lines prove nothing about it.
The UI's 15s background `/api/status` poll used to take that lock and hold it for
two gphoto2 sessions — a plausible source of exactly the "usually 10-11 o'clock,
occasionally past 9" variance. It now serves cached camera fields
(`read_status_cached`) whenever the sensor trigger is armed, and never touches
the camera.
**That lock-free poll needs a cache feeder, or the UI goes blind.** First cut
shipped without one and the camera pill read "no camera" for a whole batch:
nothing else ever called `read_status`. The cache is now seeded at startup
(before the trigger is armed) and topped up by `_refresh_cam_status()`, which
rides on a capture whose lock we already hold and runs **after the shutter**, so
it can never delay an exposure. `STATUS_REFRESH_S`=240s.

**Measured breakdown (appliance, v0.1.33, 12 sensor frames, 2026-08-09):**
edge→shutter mean **4.71s** (3.05–5.16). Of that, `probe` was 1.62s mean and the
shutter then fired a very consistent **3.09s** into `_grab` (2.96–3.30) — that is
gphoto2 startup + PTP session open + the exposure. The download *after* the
shutter is only **0.46s**, confirming the whole post-shutter half is irrelevant
to the race. `[post] meta=` measured 2.2–2.8s, i.e. exiftool really was that
expensive; moving it off the capture path was worth it.

The probe was the entire story: it ran on nearly every frame (see
`READY_PROBE_WINDOW`) and was both the biggest single slice AND essentially all
the variance. With it: 4.68–5.16s. The one frame that skipped it: 3.05s.
**Remaining target is that ~3.1s of gphoto2 setup + exposure**, which only a
persistent session can attack.

Other costs taken off the pre-shutter path:
- `capturetarget` is sent **once per session**, not before every shutter — it was
  a PTP round-trip sitting directly in front of the exposure. Re-sent after any
  failure, after `apply_settings`, and by `_grab`'s no-file retry
  (`cam.forget_capturetarget()`).
- The **readiness probe** (a whole gphoto2 session) is skipped unless the previous
  shot ended within `READY_PROBE_WINDOW`=20s.
- The **exiftool metadata write** moved to the post-capture worker.

Full per-frame breakdown in the journal:
`[capture] <name>: scan=… probe=… shot=… meter=… tail=… total=…`
(`scan` = the two `OUT_DIR` globs for next-index + count, `probe` = readiness
session, `shot` = `_grab` incl. download, `meter` = jpegstats, `tail` =
planning/queueing), plus `[post] <name>: meta=… total=… (queue N)`.

**A hardware option costs nothing in software:** moving the sensor so it trips
EARLIER in the revolution buys margin directly, without slowing the motor.

### Persistent gphoto2 session (`ShellSession` in camera.py)
The answer to that ~3.1s: `gphoto2 --shell` keeps the PTP session open and takes
commands on stdin, so process start + camlib scan + USB claim + PTP OpenSession
are paid ONCE per run instead of before every exposure. Enabled by default;
`--no-persist` reverts to a process per operation.

**OFF by default** (`--persist` to enable). v0.1.35 shipped it on and **the
camera stopped taking pictures entirely**; see the buffering note below. Do not
re-enable it by default until it has run a real batch on the hardware.

Things that are load-bearing, and were each a bug or a near-miss first:
- **`stdbuf -oL` IS MANDATORY.** gphoto2 writes through stdio, which
  block-buffers when stdout is a pipe, so without it every reply — including the
  sentinel — sits in libc's buffer and never arrives. That was the v0.1.35
  outage: each session start blocked for its full 30s timeout, and behind the
  sensor trigger's 8s lock wait every slide was skipped as "camera busy". If
  `stdbuf` is missing the session now refuses to start rather than hang.
  `trigger.py` already did this for `gpiomon`, with a comment explaining why —
  the lesson was in the repo and got missed anyway.
- **The test stub must NOT flush by hand.** The original one did, the suite
  passed, and the feature hung on hardware — the tests were a fiction. It now
  block-buffers by default and honours `_STDBUF_O` the way libc does, so a
  missing `stdbuf` reproduces the outage in the suite (case 9).
- **Prove the session at startup** (`Camera.warmup()`, called from `main()`
  before the trigger is armed) and disable it for good if it fails. v0.1.35
  discovered impossibility *during captures* and paid for that discovery per
  frame, which is what turned a slow path into no pictures at all.
- **Synchronise with a sentinel, not the prompt.** The shell's prompt goes
  through readline, which may not print it at all when stdin is a pipe. Each
  command is followed by a unique BOGUS command; the shell answers
  `Command '<marker>' not found.` unconditionally, which is a reliable
  end-of-output marker. (`_SHELL_SENTINEL`.)
- **Read raw bytes, not lines.** The shell's final output before it waits for
  input has no trailing newline, so a `readline()` loop blocks with data unread.
- **One process may claim the camera at a time.** Everything must go through the
  session; `Camera._run` translates the CLI arg list to shell commands and, for
  anything it can't express (`--auto-detect`), CLOSES the session first. `model()`
  is cached for exactly this reason — an uncached call would tear the session
  down just to re-read a constant.
- **`cwd` matters only for captures** (gphoto2 stages downloads there). Config
  ops pass `cwd=None` = "any live session will do". Demanding a match made every
  settings read rebuild the session (~3s) and quietly undid the whole feature.
- **Re-root the session at the output dir before anything opens one**
  (`set_capture_dir`, called from `main()`), or the startup status read opens it
  at the process cwd and the FIRST CAPTURE — a real slide — pays the restart.
- **Reopen eagerly** (`_rewarm_shell`) after an op that had to close it, so the
  ~3s lands on that op and not on the next exposure.
- **The `--filename` pattern is fixed when the shell launches**, so captures land
  on `SHELL_CAPTURE_STEM` and are renamed onto the caller's name afterwards
  (`_promote_shell_files`); a no-op after a legacy run, so callers never need to
  know which path ran.
- **It self-disables.** Any session error falls back to a fresh process for that
  frame (never losing it) and after `_SHELL_MAX_FAILS`=3 consecutive failures the
  feature is switched off for the run — retrying a doomed session forever would
  be SLOWER than never trying (a wasted attempt plus the legacy run).

**STILL UNVERIFIED ON HARDWARE:** whether the 400D's post-capture USB
re-enumeration kills a held session. The v0.1.35 outage was the buffering bug, so
it never got far enough to answer that question. To try it, start with
`--persist` and watch the journal: `Fast session: on` at startup, then
`SHUTTER at +…` per frame. Check `/api/diag → persistent_session` for the
`shell` / `legacy` / `fallbacks` counts. Against the stub the win is the full
~3s (first frame 3.34s to shutter, every one after it 0.30s), but the stub is a
stub — it has already been wrong once.

Run (dev host):
    python3 capture_server.py            # http://localhost:8080
    python3 capture_server.py --port 8080 --out-dir ./captures --prefix trip72
Default format is Large Fine JPEG ("L"); switch to "RAW + L" (archival CR2 +
JPEG) in the settings drawer or via `STARTUP_SETTINGS`.

IMPORTANT: never run `gphoto2` from another shell while the server is up — it
bypasses the server's camera lock and both fail with I/O errors. Change settings
through the UI/API instead.

Notes / gotchas learned:
- **"captured but no displayable image" = gphoto2 needs a WRITABLE cwd (Pi
  2026-07-09, the real root cause):** the shutter fired and gphoto2 saw the file
  ("New file is in location … on the camera") but it never saved locally
  (`~/captures` empty, no "Saving file as" line). Cause: **gphoto2 stages the
  download in its current working directory even when `--filename` is absolute**,
  and the systemd service ran with `WorkingDirectory=/opt/slidescanner` (root-
  owned, NOT writable by the `scanner` user) → the download was silently
  dropped. It "worked on the laptop" because that ran from a writable repo dir.
  Fix: run gphoto2 with **`cwd=<output dir>`** — `camera.capture()` passes
  `cwd=dest.parent`. capturetarget and `--filename` arg-order were RED HERRINGS
  (chased both, both wrong); a `/api/debugcapture` variant matrix proved every
  capture variant downloads fine once cwd is writable. Diagnose via
  **Setup → System → View logs** (the `[capture]` block) + **Camera diagnostics**.
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
- [DONE 2026-07-09] Pi Zero W appliance image builds in CI (32-bit armhf, boots
  Zero W + Zero 2 W) and, as of v0.1.2, **raises the Comitup WiFi AP and joins
  the network on a fresh flash** — first hardware bring-up passed. Fix vs
  v0.1.0/0.1.1: explicitly `systemctl enable comitup` (package self-enable is
  unreliable under the QEMU build) and set a WiFi country (Bookworm rfkill-blocks
  the radio until one is set, so AP mode couldn't start). Still to verify on the
  Pi: web UI reachable at `slidescanner.local:8080`, camera detect + real
  capture over the OTG adapter.
- [DONE 2026-07-09] **End-to-end capture working on the Pi Zero W appliance**
  (v0.1.10): real slide captured, downloaded, and metered via the web UI over
  WiFi. Journey: fixed Comitup AP (enable + WiFi country), self-update
  (public repo + clean clone, no stale CI credential), and the capture bug —
  which was NOT capturetarget or arg-order (both chased and wrong) but gphoto2
  needing a **writable cwd** to stage the download (service ran from a
  root-owned dir). Added in-app logs + `/api/diag` diagnostics, updater
  validate/rollback, and a lock-wait to kill spurious "camera busy". Exposure:
  ISO 100 / f8 / **1/30** gave a good slide off the light pad (1/60 was ~1 stop
  dark). Self-update validated on hardware (v0.1.4→…→v0.1.10 via the button).
- [DONE 2026-08-06] **Digital brightness correction** (`brightness.py`) — the
  answer to "a ton of slides, can't re-dial the backlight per shot". A flagged
  frame is gamma-corrected onto a target on a background worker (no extra shot,
  no shutter wear), bounded to ±1.5 EV, with the untouched original kept in
  `captures/originals/` and a Review compare/undo. Verified end-to-end on real
  400D captures on the dev host: a 1.7-stop-under frame (mean 33) lands at mean
  90 = "ok", full-image luma confirms the PIXELS moved (33.4 → 88.8, not just
  the thumbnail), EXIF + MakerNotes + corrected thumbnail survive, resolution
  and 4:2:2 sampling preserved, revert restores byte-identically, and the no-PIL
  host degrades to a visible no-op. **Not yet run on the Pi** — needs
  `sudo apt install python3-pil` there (self-update doesn't install packages),
  and the per-frame CPU cost on a Zero W is still to be measured.
- [DONE 2026-08-09] **Real production run on the appliance** — many hundreds of
  slides captured via the sensor trigger across ~4 hours, with brightness
  correction working on the Pi. Two problems found in that journal and fixed:
  the sensor's settle-and-clear was **silently dropping slides** whenever the
  operator fed faster than the capture cycle (now rejected by timestamp — see
  `trigger.py`), and `exiftool` + a redundant readiness probe were sitting in
  the capture path where the operator waits on them (both moved/skipped). Added
  `[capture]`/`[post]` sub-timings so the cycle can be measured, not guessed.
  **Not yet re-run on hardware** — the numbers to check next are `probe=` (should
  be 0.0 in steady state) and `tail=`, which sets the right `cooldown_s`.
- [TODO] Exposure/quality polish: tighter framing to drop the black mount
  border (more resolution + accurate metering), optional custom WB off the
  light pad (slides read slightly blue). Remove the temporary `/api/debugcapture`.
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
