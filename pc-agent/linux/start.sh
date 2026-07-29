#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "NovaRelynx setup"
echo "================"
echo

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Python 3 was not found on this machine."
    echo "Install it with your distro's package manager, e.g.:"
    echo "  Debian/Ubuntu:  sudo apt install python3 python3-pip"
    echo "  Fedora:         sudo dnf install python3 python3-pip"
    echo "  Arch:           sudo pacman -S python python-pip"
    echo "Then run this script again."
    exit 1
fi

echo "Installing required packages (only takes a moment, and only happens once)..."
"$PYTHON" -m pip install --user -q -r requirements.txt

echo
echo "Starting the dashboard... a browser tab will open in a moment."
echo "Keep this terminal open -- closing it stops your tunnels."
echo
exec "$PYTHON" dashboard.py
