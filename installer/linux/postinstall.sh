#!/bin/sh
# Post-install: make the native binaries executable and build the venv.
# Runs as root from the package manager (apt/dnf/pacman). Never fail the whole
# install if pip cannot run (e.g. no network) - the user can re-run setup_env.py.
set -e
PREFIX=/opt/mouse-mesh-pipeline

chmod +x "$PREFIX"/bin/linux64/* 2>/dev/null || true

PY=$(command -v python3 || true)
if [ -z "$PY" ]; then
    echo "mouse-mesh-pipeline: python3 not found on PATH." >&2
    echo "Install Python 3, then run:  sudo python3 $PREFIX/installer/setup_env.py $PREFIX" >&2
else
    if ! "$PY" "$PREFIX/installer/setup_env.py" "$PREFIX"; then
        echo "mouse-mesh-pipeline: environment setup did not finish (network?)." >&2
        echo "Re-run later:  sudo python3 $PREFIX/installer/setup_env.py $PREFIX" >&2
    fi
fi

# Best-effort desktop integration.
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications 2>/dev/null || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q -t /usr/share/icons/hicolor 2>/dev/null || true
fi
exit 0
