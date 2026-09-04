#!/bin/bash
# Double-click this file on a Mac. First run sets up Python deps (~30s), then
# the app opens in your browser. Close this window to stop the app.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 isn't installed."
  echo "Get it from https://www.python.org/downloads/macos/ then double-click this file again."
  read -r -p "Press Enter to close. "
  exit 1
fi

if [ ! -d .venv ]; then
  echo "First run — setting up (about 30 seconds)…"
  python3 -m venv .venv || { read -r -p "Setup failed. Press Enter to close. "; exit 1; }
fi
.venv/bin/pip install -q -r requirements.txt || { read -r -p "Dependency install failed. Press Enter to close. "; exit 1; }

echo
echo "ig-engagers is running at http://127.0.0.1:8765"
echo "Close this window (or press Ctrl-C) to stop it."
echo
exec .venv/bin/python app.py
