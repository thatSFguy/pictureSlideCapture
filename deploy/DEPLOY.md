# Deploying on a Raspberry Pi Zero 2 W

Turns a Pi Zero 2 W into a self-contained slide-scanner appliance: flash → boot →
join WiFi (via an on-device AP the first time in a new place) → scan from any
browser at `http://slidescanner.local:8080`.

> The Zero 2 W is adequate for this: the bottleneck is the camera's ~1.7 MB/s USB
> transfer, not the Pi. The app is stdlib-only Python. Caveats handled in code:
> the download-all zip streams (won't exhaust 512 MB RAM) and thumbnails read
> only the file head. The exposure aid meters off the embedded JPEG thumbnail, so
> it stays fast for JPEG and RAW+JPEG (the presets' defaults).

## Hardware

- Raspberry Pi Zero 2 W + a good micro-SD card (32 GB+; images live on it).
- **micro-USB-OTG → USB-A adapter** for the camera (the Zero 2 W's data port is
  micro-USB OTG). Power via the *separate* PWR port.
- Canon camera on an **AC coupler** (not the battery), dial on **M**,
  Communication set to **PC connection**, auto-power-off **disabled**.

## 1. Flash the OS (with first-boot network for setup)

Use **Raspberry Pi Imager** → *Raspberry Pi OS Lite (64-bit, Bookworm)*. In the
gear/OS-customisation:

- Set **hostname** = `slidescanner`
- Set a **username + password** (remember them)
- Enable **SSH**
- Set your **WiFi** SSID/password — needed for the *first* boot so setup can
  install packages. (Comitup takes over WiFi afterwards for other locations.)

Flash, insert, power on, wait ~1–2 min for first boot.

## 2. Copy the project onto the Pi

The GitHub repo is **private**, so either:

- **scp from your machine** (simplest):
  ```bash
  scp -r pictureSlideCapture <user>@slidescanner.local:~/
  ```
- **or** authenticate `git`/`gh` on the Pi and `git clone` it.

## 3. Run the setup script

```bash
ssh <user>@slidescanner.local
cd ~/pictureSlideCapture
sudo bash deploy/setup_pi.sh
sudo reboot
```

`setup_pi.sh` installs gphoto2, the camera udev rule (+ adds you to `plugdev`),
Comitup (WiFi provisioning), sets the hostname, and installs/enables the
`slidescanner` systemd service on port 8080.

## 4. Use it

Plug the camera in (OTG adapter), power it on, then browse to:

```
http://slidescanner.local:8080
```

Setup → pick Slides/Negatives, name the group, dial in the shutter with a Test
shot, Start capturing. Offload finished batches with **Review → Download all
(zip)**, then delete the group to free the SD card.

## WiFi in a new location (the AP flow)

If the Pi boots somewhere it doesn't recognise, Comitup raises a WiFi access
point named **`slidescanner-XXXX`**. From a phone/laptop:

1. Join the `slidescanner-XXXX` network.
2. A portal opens (or browse to `http://10.41.0.1`); pick the WiFi and enter the
   password.
3. The Pi joins that network and drops the AP. Reconnect your device to the same
   network and open `http://slidescanner.local:8080`.

## Service management

```bash
systemctl status slidescanner        # is it running?
journalctl -u slidescanner -f        # live logs
sudo systemctl restart slidescanner  # after pulling new code
```

## Notes / gotchas

- **Ports:** the app is on **8080** because Comitup's portal owns port 80.
- **Storage:** captures live in `~/captures` on the SD card. High-volume RAW
  jobs fill space fast — offload via the zip and delete groups. (A USB stick via
  a powered OTG hub is an option if you outgrow the card.)
- **Comitup** can be finicky across OS revisions (it manages NetworkManager). If
  the AP or reconnect misbehaves, see the Comitup docs — this script uses its
  standard install and a minimal `/etc/comitup.conf`.
- This guide/script were written without a Pi on hand to test — treat the first
  run as a shakedown and expect a tweak or two.
