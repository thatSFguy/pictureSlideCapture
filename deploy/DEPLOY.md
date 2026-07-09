# Deploying on a Raspberry Pi Zero W / Zero 2 W

> The CI image is **32-bit (armhf)** so it boots on both the single-core Zero W
> (ARMv6) and the quad-core Zero 2 W (ARMv8). The Zero 2 W is snappier, but the
> workload is bound by the camera's ~1.7 MB/s USB transfer, so either works.

Goal — the appliance flow:

> **Flash a card → plug the Pi in → it raises a WiFi AP → connect and enter your
> network → it reconnects → scan at `http://slidescanner.local:8080`.**
> No SSH, no config files, no scripts on the end card.

There's a catch that shapes everything below: a blank card can't `apt install`
Comitup/gphoto2 without a network. So this is **two phases**:

- **Part A — build the appliance image, once.** Install everything on a Pi, then
  save that card as a reusable image.
- **Part B — flash that image and go.** This is the zero-config flow above, and
  it's what you (or anyone) do from then on.

> Why the Zero 2 W is fine: the bottleneck is the camera's ~1.7 MB/s USB
> transfer, not the Pi. The app is stdlib-only Python. The zip download streams
> and thumbnails read only the file head, so 512 MB RAM is enough.

## Getting the appliance image

Two ways to produce the `slidescanner.img`:

- **CI (recommended).** GitHub Actions builds it for you — see
  [`.github/workflows/build-image.yml`](../.github/workflows/build-image.yml).
  Run it manually (*Actions → Build Pi image → Run workflow*) to get a
  build artifact, or push a `v*` **tag** to attach the compressed image to a
  **Release**. The repo (and your app) are baked in, so there's no private-repo
  copy step. **Set the repo Actions variable `SSH_AUTHORIZED_KEYS`** to your
  public key(s) first (*Settings → Secrets and variables → Actions → Variables*)
  — the appliance's `scanner` user is **key-only**; without it you can't SSH in.
  Then jump to **Part B**.
- **Locally**, following **Part A** below.

## Hardware

- Pi Zero 2 W + a good micro-SD (32 GB+; images live on it).
- **micro-USB-OTG → USB-A adapter** for the camera (data port is micro-USB OTG);
  power via the *separate* PWR port.
- Camera on an **AC coupler**, dial on **M**, Communication = **PC connection**,
  auto-power-off **disabled**.

---

## Part A — Build the appliance image (done once)

You need a network *for this build only*. It won't be needed on the finished
image.

1. **Flash a build card.** Raspberry Pi Imager → *Raspberry Pi OS Lite
   (**32-bit**, Bookworm)* — 32-bit boots on every board (Zero W / Zero 2 W /
   Pi 1+), which matches the CI image. In OS customisation set: hostname
   `slidescanner`, a username + password, enable **SSH**, and **your WiFi**
   (temporary — for the build).

2. **Get the project onto the Pi.** The GitHub repo is private, so from your
   machine:
   ```bash
   scp -r pictureSlideCapture <user>@slidescanner.local:~/
   ```
   (or authenticate `git`/`gh` on the Pi and clone it.)

3. **Install everything:**
   ```bash
   ssh <user>@slidescanner.local
   cd ~/pictureSlideCapture
   sudo bash deploy/setup_pi.sh
   ```
   This installs gphoto2, the camera udev rule (+ `plugdev`), **Comitup**, the
   hostname, and the `slidescanner` systemd service (port 8080).

4. **Verify** (plug the camera in, power it on):
   ```bash
   systemctl status slidescanner        # active?
   gphoto2 --auto-detect                # camera seen?
   ```
   Browse from another device to `http://slidescanner.local:8080` and take a
   test shot.

5. **Make the image "clean"** so a fresh flash raises the AP instead of joining
   the build WiFi. Remove the saved WiFi so no network is "known":
   ```bash
   nmcli -t -f NAME connection show                 # find your build SSID
   sudo nmcli connection delete "<your-build-ssid>"
   ```
   (Optional tidy-up: `sudo apt clean`, `sudo journalctl --rotate --vacuum-time=1s`.)
   Then shut down: `sudo shutdown -h now`.

6. **Capture the image.** Pull the card, put it in your computer, and save it:
   - Linux/macOS: `sudo dd if=/dev/<card> of=slidescanner.img bs=4M status=progress`
     (then optionally shrink with [PiShrink](https://github.com/Drewsif/PiShrink)).
   - Or use Raspberry Pi Imager / Win32DiskImager "read" to a `.img`.

   `slidescanner.img` is now your reusable appliance image.

---

## Part B — Flash and go (the actual flow)

1. **Flash** `slidescanner.img` to any card (Raspberry Pi Imager → *Use custom*).
   No customisation, no WiFi — flash it as-is.
2. **Plug in** the Pi (and the camera via the OTG adapter). Power on.
3. **Join the AP.** With no known network, Comitup raises WiFi named
   **`slidescanner-XXXX`**. On a phone/laptop, connect to it.
4. **Set your network.** A portal opens (or browse to `http://10.41.0.1`); pick
   your WiFi and enter the password. The Pi joins it and drops the AP.
5. **Scan.** Reconnect your device to that same WiFi and open
   **`http://slidescanner.local:8080`**.

Moving it to a new place later? Same thing — it doesn't recognise the new WiFi,
so it raises `slidescanner-XXXX` again; re-enter credentials.

---

## Everyday use

Setup → pick Slides/Negatives, name the group, dial in the shutter with a Test
shot, Start capturing. Offload with **Review → Download all (zip)**, then delete
the group to free the SD card.

## Service management (on the Pi)

```bash
systemctl status slidescanner
journalctl -u slidescanner -f
sudo systemctl restart slidescanner   # after updating code
```

## Notes / gotchas

- **Port 8080** — Comitup's portal owns port 80, so the scanner URL carries
  `:8080`.
- **Storage** — captures live in `~/captures` on the SD card; offload + delete
  for big RAW jobs (or add a USB stick via a powered OTG hub).
- **Comitup** manages NetworkManager and can be finicky across OS revisions; if
  the AP or reconnect misbehaves, check its docs. Confirm `nmcli device` shows
  wlan0 managed.
- Written without a Pi on hand to test — treat the first build as a shakedown.
