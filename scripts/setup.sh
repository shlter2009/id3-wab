#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
command -v ffmpeg >/dev/null || { echo 'Install FFmpeg and add it to PATH first.' >&2; exit 1; }
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
