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

echo ">>> install app from a clean public clone (strips CI git credentials)..."
# arm-runner copied the repo WITH the CI checkout's git config, which embeds a
# now-expired GitHub auth token (http.<url>.extraheader). That stale token makes
# runtime `git fetch` prompt for a username and fail. Re-clone the public repo
# fresh so origin is clean + anonymous, then pin the exact built commit so the
# image's code matches its tag.
REPO_URL="https://github.com/thatSFguy/pictureSlideCapture.git"
SHA="$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || true)"
TMP="$(mktemp -d)"
if git clone --quiet "$REPO_URL" "$TMP/app"; then
  git -C "$TMP/app" fetch --tags --force --quiet origin || true
  [ -n "$SHA" ] && git -C "$TMP/app" checkout --quiet "$SHA" 2>/dev/null \
    || echo "   (couldn't pin $SHA; staying on default branch)"
  rm -rf "$REPO_DIR"
  mv "$TMP/app" "$REPO_DIR"
else
  echo "   WARNING: clone failed (no network?); scrubbing CI credentials in place"
  git -C "$REPO_DIR" remote set-url origin "$REPO_URL" 2>/dev/null || true
  git -C "$REPO_DIR" config --local --remove-section 'http.https://github.com/' 2>/dev/null || true
  git -C "$REPO_DIR" config --local --unset-all credential.helper 2>/dev/null || true
  rm -rf "$TMP"
fi
git config --system --add safe.directory "$REPO_DIR" 2>/dev/null || true

chown -R root:root "$REPO_DIR"        # app read-only; service runs as $TARGET_USER

echo ">>> service..."; pc_install_service

apt-get clean
rm -f "$REPO_DIR/deploy/authorized_keys" || true   # don't ship the key list in-repo path
echo ">>> image provisioned OK (no WiFi set -> boots into Comitup AP)"
