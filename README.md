# Film Scanning Rig

> ## ⚠️ Alpha
> Early and under active development. It works end-to-end on the author's setup
> (Canon EOS 400D on a Raspberry Pi Zero W), but it's been tested on exactly one
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
latest `slidescanner-*-armhf.img.xz` with Raspberry Pi Imager (32-bit, boots
both the Pi Zero W and Zero 2 W).

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

## Files

| File | Role |
|------|------|
| `capture_server.py` | The web app (HTTP server + embedded UI + all endpoints) |
| `camera.py` | gphoto2 wrapper: detect, get/set config, capture, retries |
| `jpegstats.py` | Pure-stdlib JPEG brightness reader for the exposure aid |
| `advance.py` | Auto slide-advance output (stub): capture → advance → repeat, settings-driven (motor+switch / stepper); default off |
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
- [ ] Automated XY gantry (GRBL) for hands-free batch scanning (`scanner.py`)
