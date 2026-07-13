#!/usr/bin/env bash
# One-shot installer for FeedMeSomePi on Raspberry Pi OS (Bullseye or Bookworm,
# 32-bit). Safe to re-run.
set -euo pipefail

cd "$(dirname "$0")"

echo "==> Installing system packages (Tk + Pillow-ImageTk + alsa-utils for notify sound)"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    python3-tk python3-pil.imagetk \
    alsa-utils

echo "==> Creating virtualenv (.venv) with access to system site packages"
# --system-site-packages so we reuse the apt-installed python3-tk / python3-pil
# instead of pulling Pillow wheels (which build slowly on a Pi Zero).
python3 -m venv --system-site-packages .venv

echo "==> Installing Python deps"
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Installing desktop entry under the Internet menu"
# Categories=Network;News; lands the launcher in PiOS's "Internet" menu
# group. We substitute the absolute install path into the Exec= line so
# the launcher works no matter where the user cloned the repo.
desktop_src="assets/feedmesomepi.desktop.in"
desktop_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$desktop_dir"
sed "s|@INSTALL_DIR@|$PWD|g" "$desktop_src" \
    > "$desktop_dir/feedmesomepi.desktop"
chmod 644 "$desktop_dir/feedmesomepi.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$desktop_dir" >/dev/null 2>&1 || true
fi

echo
echo "Done. Launch FeedMeSomePi from the Internet menu, or run ./run.sh."
