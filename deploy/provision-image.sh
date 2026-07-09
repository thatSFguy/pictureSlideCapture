#!/usr/bin/env bash
#
# Provision a Raspberry Pi OS image (run by CI *inside* the image under QEMU;
# see .github/workflows/build-image.yml). Produces a flash-and-go appliance:
# no WiFi configured -> boots into the Comitup AP; SSH is key-only.
#
# The repo is copied into the image at REPO_DIR before this runs.

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

TARGET_USER="scanner"
TARGET_HOME="/home/$TARGET_USER"
REPO_DIR="/opt/slidescanner"
OUT_DIR="$TARGET_HOME/captures"
PORT=8080
HOSTNAME_NEW="slidescanner"

source "$REPO_DIR/deploy/provision-common.sh"

echo ">>> creating appliance user '$TARGET_USER'..."
if ! id "$TARGET_USER" >/dev/null 2>&1; then
  useradd -m -s /bin/bash -G plugdev,sudo "$TARGET_USER"
fi
# key-only maintenance user -> passwordless sudo (no password exists to prompt)
echo "$TARGET_USER ALL=(ALL) NOPASSWD:ALL" >/etc/sudoers.d/010-$TARGET_USER
chmod 440 /etc/sudoers.d/010-$TARGET_USER

echo ">>> packages..."; pc_install_packages
echo ">>> Comitup..."; pc_install_comitup
echo ">>> camera udev rule..."; pc_install_udev_rule

echo ">>> SSH (keys only)..."
install -d -m 700 -o "$TARGET_USER" -g "$TARGET_USER" "$TARGET_HOME/.ssh"
if [ -s "$REPO_DIR/deploy/authorized_keys" ]; then
  install -m 600 -o "$TARGET_USER" -g "$TARGET_USER" \
    "$REPO_DIR/deploy/authorized_keys" "$TARGET_HOME/.ssh/authorized_keys"
else
  echo "   WARNING: no deploy/authorized_keys provided — SSH login will be impossible."
  install -m 600 -o "$TARGET_USER" -g "$TARGET_USER" /dev/null "$TARGET_HOME/.ssh/authorized_keys"
fi
cat >/etc/ssh/sshd_config.d/10-slidescanner.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
EOF
ssh-keygen -A          # ensure host keys exist
systemctl enable ssh

echo ">>> hostname -> $HOSTNAME_NEW..."
echo "$HOSTNAME_NEW" >/etc/hostname
if grep -q '^127.0.1.1' /etc/hosts; then
  sed -i "s/^127.0.1.1.*/127.0.1.1\t$HOSTNAME_NEW/" /etc/hosts
else
  printf '127.0.1.1\t%s\n' "$HOSTNAME_NEW" >>/etc/hosts
fi

echo ">>> app + captures dir..."
install -d -o "$TARGET_USER" -g "$TARGET_USER" "$OUT_DIR"

echo ">>> point app repo at public origin + fetch tags (for in-app self-update)..."
REPO_URL="https://github.com/thatSFguy/pictureSlideCapture.git"
git config --system --add safe.directory "$REPO_DIR" 2>/dev/null || true
if git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "$REPO_DIR" remote set-url origin "$REPO_URL" 2>/dev/null \
    || git -C "$REPO_DIR" remote add origin "$REPO_URL"
else
  rm -rf "$REPO_DIR"; git clone "$REPO_URL" "$REPO_DIR"
fi
git -C "$REPO_DIR" fetch --tags --force origin \
  || echo "   (tag fetch failed now; self-update will fetch on first check)"

chown -R root:root "$REPO_DIR"        # app read-only; service runs as $TARGET_USER

echo ">>> service..."; pc_install_service

apt-get clean
rm -f "$REPO_DIR/deploy/authorized_keys" || true   # don't ship the key list in-repo path
echo ">>> image provisioned OK (no WiFi set -> boots into Comitup AP)"
