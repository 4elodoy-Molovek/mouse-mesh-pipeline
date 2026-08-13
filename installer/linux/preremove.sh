#!/bin/sh
# Pre-remove: drop the generated venv and byte-cache (not owned by the package).
rm -rf /opt/mouse-mesh-pipeline/.venv
find /opt/mouse-mesh-pipeline -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
exit 0
