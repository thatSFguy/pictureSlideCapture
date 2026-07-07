#!/usr/bin/env bash
# Shared install steps for both provisioning paths:
#   - deploy/setup_pi.sh        (live, on a running Pi)
#   - deploy/provision-image.sh (CI, inside a Pi OS image under QEMU)
# Source this after setting: REPO_DIR, TARGET_USER, OUT_DIR, PORT.  Run as root.

PKGS="gphoto2 avahi-daemon"

pc_install_packages() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y $PKGS
  apt-get install -y libimage-exiftool-perl || echo "   (exiftool optional; skipped)"
}

pc_install_comitup() {
  # comitup is in Debian main (bookworm+), so no external repo is needed.
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y comitup
  cat >/etc/comitup.conf <<'EOF'
# <nnnn> is replaced by Comitup with a unique number
ap_name: slidescanner-<nnnn>
web_service: comitup-web.service
EOF
  systemctl enable NetworkManager 2>/dev/null || true
}

pc_install_udev_rule() {
  cat >/etc/udev/rules.d/90-canon-camera.rules <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="04a9", MODE="0664", GROUP="plugdev"
EOF
}

pc_install_service() {   # renders deploy/slidescanner.service and enables it
  sed -e "s#__USER__#$TARGET_USER#g" \
      -e "s#__REPO__#$REPO_DIR#g" \
      -e "s#__OUT__#$OUT_DIR#g" \
      -e "s#__PORT__#$PORT#g" \
      "$REPO_DIR/deploy/slidescanner.service" >/etc/systemd/system/slidescanner.service
  systemctl daemon-reload 2>/dev/null || true
  systemctl enable slidescanner.service
}
