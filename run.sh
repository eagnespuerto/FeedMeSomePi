#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "No .venv found. Run ./setup.sh first." >&2
    exit 1
fi

. .venv/bin/activate
exec python3 rss_aggregator.py "$@"
