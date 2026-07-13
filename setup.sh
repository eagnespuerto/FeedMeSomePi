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

echo
echo "Done. Run ./run.sh to launch FeedMeSomePi."
