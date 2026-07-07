#!/usr/bin/env bash
#
# Provision a *running* Raspberry Pi as the slide scanner (the manual/live path;
# CI builds an image instead via .github/workflows/build-image.yml).
#
# Run from the repo root on the Pi:   sudo bash deploy/setup_pi.sh
#
# NOTE: authored without a Pi to test on — verify each step; Comitup in
# particular may need extra tweaks per its docs on your OS revision.

set -euo pipefail

PORT=8080
HOSTNAME_NEW="slidescanner"

TARGET_USER="${SUDO_USER:-$(id -un)}"
if [ "$TARGET_USER" = "root" ]; then
  echo "Run as a normal user with sudo:  sudo bash deploy/setup_pi.sh" >&2
  exit 1
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$TARGET_HOME/captures"
echo ">>> user=$TARGET_USER  home=$TARGET_HOME  repo=$REPO_DIR  port=$PORT"

source "$REPO_DIR/deploy/provision-common.sh"

echo ">>> installing packages..."; pc_install_packages
echo ">>> installing Comitup..."; pc_install_comitup
echo ">>> camera udev rule + plugdev..."
pc_install_udev_rule
usermod -aG plugdev "$TARGET_USER"
udevadm control --reload-rules && udevadm trigger || true

echo ">>> hostname -> $HOSTNAME_NEW..."
hostnamectl set-hostname "$HOSTNAME_NEW" || true
if grep -q '^127.0.1.1' /etc/hosts; then
  sed -i "s/^127.0.1.1.*/127.0.1.1\t$HOSTNAME_NEW/" /etc/hosts
else
  printf '127.0.1.1\t%s\n' "$HOSTNAME_NEW" >>/etc/hosts
fi

install -d -o "$TARGET_USER" -g "$TARGET_USER" "$OUT_DIR"

echo ">>> installing slidescanner service..."
pc_install_service
systemctl restart slidescanner.service

echo
echo ">>> Done. Reboot recommended:  sudo reboot"
echo ">>> Then open:  http://$HOSTNAME_NEW.local:$PORT"
echo ">>> WiFi: if no known network is found on boot, connect to the"
echo "    'slidescanner-XXXX' AP and set your network in the portal."
