# Film Scanning Rig

> ## ⚠️ Alpha
> Early and under active development. It works end-to-end on the author's setup
> (Canon EOS 400D on a Raspberry Pi Zero 2 W), but it's been tested on exactly one
> camera and rig, has **no authentication** (designed for a trusted home LAN),
> and interfaces may change. Use at your own risk; expect rough edges.

Digitize 35mm slides and negatives with a tethered Canon DSLR — a web app you
drive from any browser to place a slide, capture it, and review the batch.

> **Status:** manual capture app is working (including a Raspberry Pi flash-and-go
> appliance with in-app updates); the automated XY gantry is deferred until the
> manual workflow is proven. See [CLAUDE.md](CLAUDE.md) for hardware/dev notes.

## What it is

A DSLR is mounted over a backlit high-CRI light pad; film is placed in a holder
under the lens and triggered over USB. The host runs a small **stdlib-only
Python web app** (`capture_server.py`) that talks to the camera via `gphoto2`
and serves a phone/tablet/desktop UI. No live view on the target camera (Canon
EOS 400D / Rebel XTi), so the workflow is shoot → review, optimized for volume.

## Requirements

- Linux host (dev machine now; a Raspberry Pi later). macOS works too.
- [`gphoto2`](http://gphoto.org/) — `sudo apt install gphoto2`
- Python 3 (standard library only — **nothing to `pip install`**)
- Optional: `exiftool` (`sudo apt install libimage-exiftool-perl`) for full EXIF
  metadata; without it the app writes a JPEG comment instead.
- A Canon DSLR supported by gphoto2. On the 400D: set **Communication → PC
  connection** and the mode dial to **M**.

## Quick start

```bash
sudo apt install gphoto2
python3 capture_server.py            # then open http://localhost:8080
```

Options: `--port 8080`, `--out-dir ./captures`, `--prefix trip72`, `--no-setup`.

### Deploy as an appliance (Raspberry Pi Zero 2 W)

Build a configured SD-card image **once** (`deploy/setup_pi.sh` installs gphoto2,
camera permissions, Comitup for WiFi provisioning, and the systemd service),
then it's **flash-and-go**: plug in the Pi → it raises a `slidescanner-XXXX`
WiFi AP → enter your network → it reconnects → scan at
`http://slidescanner.local:8080`. Full two-phase guide in
[`deploy/DEPLOY.md`](deploy/DEPLOY.md).

Prebuilt appliance images are attached to
[Releases](https://github.com/thatSFguy/pictureSlideCapture/releases) — flash the
latest `slidescanner-*-arm64.img.xz` with Raspberry Pi Imager.

> **Minimum board: a 64-bit-capable Pi (Pi Zero 2 W or newer).** The image is
> **64-bit (arm64)**, so the single-core ARMv6 Pi Zero W / Pi 1 are no longer
> supported. The Zero 2 W is the reference target.

### Updating

Once deployed, update from the browser: **Setup → System → Check for updates**.
It pulls the latest **release tag** and restarts — no SSH, no reflash. (This
updates the *app* only; changes to the OS image or provisioning still need a new
flashed image.) Requires the Pi to have internet access.

## Using the app — three modes

- **Setup** (once per batch): pick **Slides** or **Negatives** preset, set a
  group name, fine-tune exposure, take a **Test shot** to check it, then
  *Start capturing*.
- **Capture** (the fast loop): large last shot, a glanceable exposure verdict,
  and a running count. Keyboard-first for high volume.
- **Review** (after): thumbnail grid with exposure flags, filter to flagged
  frames, caption/delete/download individual images, or download the whole
  group as a zip.

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `Space` / `Enter` | Capture |
| `R` / `Backspace` | Redo last (delete + recapture) |
| `←` / `→` | Browse recent (Capture) / prev-next (Review lightbox) |
| `[` / `]` | Go to Setup / Review |
| `Esc` | Close the Review lightbox |
| `Delete` | Delete (Review lightbox) |

## Recommended camera settings

ISO 100, f/8, Manual mode, fixed (Daylight) white balance; **JPEG** for slides,
**RAW+JPEG** for negatives (RAW is essential for inversion). Shutter is the one
value dialed in per session against your light pad using the Test shot +
exposure aid. Focus once, manually, on the film plane. Full rationale in
[CLAUDE.md](CLAUDE.md).

## Optical sensor trigger (optional)

Instead of pressing Capture, you can wire a 3-pin optical sensor (IR break-beam
or photo-interrupter) so a slide dropping into position fires the shot
automatically (`trigger.py`, off by default). It pairs with auto slide-advance:
advance → beam blocked → capture → repeat.

### Wiring — Raspberry Pi 40-pin header

The board's header usually isn't silk-screened with pin numbers. **Pin 1 is the
square solder pad** (nearest the corner); pins count in pairs down the header —
odd numbers in one row, even in the other.

<p align="center">
  <img src="docs/pinout-rpi.svg" width="480"
       alt="Raspberry Pi 40-pin header pinout; pin 1 (3V3) = VCC, pin 6 = GND, pin 18 (GPIO24) = OUT">
</p>

| Sensor wire | Connect to | Physical pin |
|-------------|------------|--------------|
| **VCC** | **3V3** | **1** |
| **GND** | **Ground** | **6** |
| **OUT** | **GPIO24** | **18** |

⚠️ **Power the sensor from 3V3 (pin 1), not 5V.** The Pi's GPIO is **3.3V-only
and not 5V-tolerant** — a 5V-powered sensor can swing OUT to 5V and damage the
input. Most IR/photo-interrupter modules run fine at 3.3V. Only if yours
*requires* 5V (pins 2/4) do you power it there, and then you **must** drop OUT to
3.3V (voltage divider or level shifter) before pin 18. GPIO24 is used because it
avoids the pins `advance.py` reserves (BCM 17/18/22/27).

### Enabling + polarity

Sensor OUT idle state varies part-to-part, so the trigger polarity is a setting.
In **Setup → Sensor trigger**: tick *Auto-capture when the beam is blocked*, pick
whether OUT goes **LOW** (active-low, the typical open-collector case — default)
or **HIGH** when blocked, set the GPIO line, and **Save**. Not sure which?
**Block the beam and hit “Read sensor now”** — it reads the raw 0/1 so you can
tell active-low (obstructed = 0) from active-high (obstructed = 1). A short
cooldown debounces bounce and avoids a double-fire during the download.

CLI equivalents: `--sensor`, `--sensor-line N` (BCM, default 24),
`--sensor-active-high`. Needs `gpiod` (`sudo apt install gpiod`; already in the
appliance image). Sensor captures share the camera lock with the UI, so a sensor
shot and a button press never overlap. libgpiod **v1 and v2** are both supported
(the CLI syntax differs between them; the app auto-detects the version).

To sanity-check wiring by hand from a shell (libgpiod **v2**, recent Pi OS):

```bash
gpioget -c gpiochip0 24               # prints 24=inactive / 24=active
gpiomon -c gpiochip0 -e falling 24    # block/clear the beam → an event per edge
```

On older **v1** it's `gpioget gpiochip0 24` / `gpiomon --falling-edge gpiochip0 24`.

## Files

| File | Role |
|------|------|
| `capture_server.py` | The web app (HTTP server + embedded UI + all endpoints) |
| `camera.py` | gphoto2 wrapper: detect, get/set config, capture, retries |
| `jpegstats.py` | Pure-stdlib JPEG brightness reader for the exposure aid |
| `advance.py` | Auto slide-advance output (stub): capture → advance → repeat, settings-driven (motor+switch / stepper); default off |
| `trigger.py` | Optical-sensor capture trigger: auto-captures on the beam-blocked edge, polarity configurable; default off |
| `scanner.py` | Gantry dead-reckoning batch loop (deferred automation phase) |
| `CLAUDE.md` | Detailed hardware, protocol, and development notes |

Captured images and per-group sidecars (`captions.json`, `exposure.json`) are
written under `captures/` and are git-ignored.

## Post-processing

Negatives require inversion — [Negative Lab Pro](https://www.negativelabpro.com/)
(Lightroom), FilmLab, or darktable's negadoctor. Slides need only minor
correction.

## Roadmap

- [x] Camera tethering + full-res capture over USB
- [x] Web capture app: presets, exposure aid, captions, review/cull, export
- [x] Raspberry Pi appliance deploy kit (`deploy/`) — untested on hardware
- [x] CI image build (GitHub Actions → flashable `.img` on tag/Release)
- [ ] Speed up CI: native ARM runners instead of QEMU (~30 min → a few min);
      gated on cost for a private repo, so revisit when public
- [ ] Physical rig: light pad, film holder with registration, camera mount
- [ ] First hardware shakedown of the Pi deploy (Comitup AP flow, real capture)
- [x] Web-based self-update: a button in the UI that pulls the latest release
      and restarts the service (no SSH needed on the appliance)
- [ ] Security hardening before going public: attack-surface review of the web
      app, remove SSH from the appliance (reflash-on-failure is the recovery
      path), scope sudo to a command allowlist, default-deny inbound firewall
- [ ] First-connect trust prompt: choose **Trusted home network** (no login) vs
      **Shared/untrusted** (set an access PIN → hashed, signed session cookie,
      all stdlib) — the clean fix for the currently-open web UI
- [ ] Serve on the default **port 80**: fold WiFi provisioning into the app
      (`nmcli`/Comitup API) and drop `comitup-web`, so one service on one port
      does setup + scanning (also shrinks attack surface). Until then, an
      `slidescanner.local` QR-code bookmark hides the `:8080`.
- [ ] Auto slide-advance (`advance.py`): stub + API done; needs hardware
      bring-up (motor + stop-switch or stepper) and a UI toggle in Setup
- [ ] Optical-sensor capture trigger (`trigger.py`): module + API + Setup UI
      done (configurable polarity, `gpiod` in the image); needs hardware bring-up
- [ ] Automated XY gantry (GRBL) for hands-free batch scanning (`scanner.py`)
