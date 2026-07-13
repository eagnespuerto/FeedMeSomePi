#!/usr/bin/env bash
# Removes the desktop-menu shortcut installed by setup.sh. Leaves the
# venv, feeds.json, and article cache in place — delete the repo
# directory and ~/.feedmesomepi/ yourself if you want a full wipe.
set -euo pipefail

desktop_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
target="$desktop_dir/feedmesomepi.desktop"

if [ -f "$target" ]; then
    rm -f "$target"
    echo "Removed $target"
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$desktop_dir" >/dev/null 2>&1 || true
    fi
else
    echo "No desktop entry at $target — nothing to do."
fi
