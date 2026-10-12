#!/usr/bin/env bash
#
# One-click Spark Swapper installer.
#
#   curl -fsSL https://raw.githubusercontent.com/gnubyte/DGX-Spark-Swapper/main/install-swapper.sh | bash
#
# What it does, in order:
#   1. Installs git (if missing) via apt.
#   2. Clones this repo to ~/spark-swapper (or updates it if present).
#   3. Runs deploy/install.sh, which copies the app to /opt/spark-swapper,
#      generates a self-signed cert, substitutes the placeholders in
#      models.json with real values, and starts the systemd service.
#
# Requires: an Ubuntu/Debian box — a DGX Spark or an AMD Strix Halo — with root access (sudo).
set -euo pipefail

REPO="https://github.com/gnubyte/DGX-Spark-Swapper"
DEST="${HOME}/spark-swapper"

say() { printf '\033[1;32m==> %s\033[0m\n' "$*"; }
err() { printf '\033[1;31m!! %s\033[0m\n' "$*" >&2; }

# ── 1. git ──
if ! command -v git >/dev/null; then
  say "Installing git…"
  sudo apt-get update -qq && sudo apt-get install -y -qq git
fi
say "git $(git --version | awk '{print $3}') present."

# ── 2. clone / update ──
if [[ -d "$DEST/.git" ]]; then
  say "Updating $DEST …"
  git -C "$DEST" pull --ff-only
else
  say "Cloning $REPO → $DEST …"
  git clone --depth 1 "$REPO" "$DEST"
fi

# ── 3. host install ──
say "Installing the app (systemd service, cert, config)…"
sudo bash "$DEST/deploy/install.sh"

IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1);exit}}')
echo
say "Spark Swapper is up:  https://${IP:-<spark-ip>}:8999"
echo "  (self-signed certificate: accept the browser warning once,"
echo "   or download the cert and trust it — see the Provisioning tab)"
echo "  First visit: create the admin login. After that, sign in."
echo "  Forgot the login?  sudo spark-swapper-reset-login"
