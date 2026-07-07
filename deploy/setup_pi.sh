#!/usr/bin/env bash
#
# Provision a Raspberry Pi (Zero 2 W, Raspberry Pi OS Lite Bookworm) as the
# slide scanner appliance. Installs gphoto2, camera USB permissions, the
# capture web app as a systemd service, and Comitup for WiFi provisioning
# (raises an AP when no known network so you can set credentials from a phone).
#
# Run from the repo root on the Pi:   sudo bash deploy/setup_pi.sh
#
# NOTE: authored without a Pi to test on — verify each step; Comitup in
# particular may need extra tweaks per its docs on your OS revision.

set -euo pipefail

PORT=8080
HOSTNAME_NEW="slidescanner"

# --- target user / paths (the normal user who invoked sudo) ----------------
TARGET_USER="${SUDO_USER:-$(id -un)}"
if [ "$TARGET_USER" = "root" ]; then
  echo "Run as a normal user with sudo:  sudo bash deploy/setup_pi.sh" >&2
  exit 1
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$TARGET_HOME/captures"
echo ">>> user=$TARGET_USER  home=$TARGET_HOME  repo=$REPO_DIR  port=$PORT"

# --- packages --------------------------------------------------------------
echo ">>> installing gphoto2 + avahi..."
apt-get update
apt-get install -y gphoto2 avahi-daemon wget
apt-get install -y libimage-exiftool-perl || echo "   (exiftool optional; skipped)"

# --- Comitup WiFi provisioning (davesteele apt repo) -----------------------
if ! command -v comitup >/dev/null 2>&1; then
  echo ">>> installing Comitup..."
  TMPDEB=/tmp/comitup-apt-source.deb
  wget -qO "$TMPDEB" https://davesteele.github.io/comitup/deb/davesteele-comitup-apt-source_latest.deb
  dpkg -i "$TMPDEB"
  apt-get update
  apt-get install -y comitup
fi
echo ">>> configuring Comitup AP name..."
cat >/etc/comitup.conf <<'EOF'
# <nnnn> is replaced by Comitup with a unique number
ap_name: slidescanner-<nnnn>
web_service: comitup-web.service
EOF
systemctl enable NetworkManager 2>/dev/null || true
# Comitup serves its config portal on port 80 (AP mode); the app uses 8080.

# --- camera USB permissions (udev) -----------------------------------------
echo ">>> installing camera udev rule + adding $TARGET_USER to plugdev..."
cat >/etc/udev/rules.d/90-canon-camera.rules <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="04a9", MODE="0664", GROUP="plugdev"
EOF
usermod -aG plugdev "$TARGET_USER"
udevadm control --reload-rules && udevadm trigger || true

# --- hostname -> http://slidescanner.local ---------------------------------
echo ">>> setting hostname to $HOSTNAME_NEW..."
hostnamectl set-hostname "$HOSTNAME_NEW" || true
if grep -q '^127.0.1.1' /etc/hosts; then
  sed -i "s/^127.0.1.1.*/127.0.1.1\t$HOSTNAME_NEW/" /etc/hosts
else
  echo -e "127.0.1.1\t$HOSTNAME_NEW" >>/etc/hosts
fi

# --- captures directory ----------------------------------------------------
install -d -o "$TARGET_USER" -g "$TARGET_USER" "$OUT_DIR"

# --- systemd service -------------------------------------------------------
echo ">>> installing slidescanner.service..."
sed -e "s#__USER__#$TARGET_USER#g" \
    -e "s#__REPO__#$REPO_DIR#g" \
    -e "s#__OUT__#$OUT_DIR#g" \
    -e "s#__PORT__#$PORT#g" \
    "$REPO_DIR/deploy/slidescanner.service" >/etc/systemd/system/slidescanner.service
systemctl daemon-reload
systemctl enable slidescanner.service
systemctl restart slidescanner.service

echo
echo ">>> Done. A reboot is recommended:  sudo reboot"
echo ">>> After reboot, open:  http://$HOSTNAME_NEW.local:$PORT"
echo ">>> WiFi: if no known network is found on boot, connect to the"
echo "    'slidescanner-XXXX' WiFi AP and set your network in the portal."
