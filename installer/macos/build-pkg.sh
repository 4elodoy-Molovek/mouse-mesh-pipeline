#!/usr/bin/env bash
# Build the macOS installer package (mouse-mesh-pipeline-<version>.pkg).
#
#   installer/macos/build-pkg.sh
#
# Thin installer: drops the pipeline under /usr/local/mouse-mesh-pipeline with
# the prebuilt macOS CGAL binaries, a /usr/local/bin launcher and an .app in
# /Applications; the post-install script creates a venv and pip-installs the
# dependencies. Expects the native binaries in ./bin/macos/. Run from repo root.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
VERSION="${VERSION:-0.0.0}"
IDENT="com.github.4elodoymolovek.mouse-mesh-pipeline"

ROOT="$REPO/installer/macos/root"
SCRIPTS="$REPO/installer/macos/scripts"
DIST="$REPO/dist"
rm -rf "$ROOT" "$SCRIPTS"
mkdir -p "$DIST" "$SCRIPTS" \
         "$ROOT/usr/local/bin" \
         "$ROOT/usr/local/mouse-mesh-pipeline/installer" \
         "$ROOT/usr/local/mouse-mesh-pipeline/bin/macos"

PREFIX="$ROOT/usr/local/mouse-mesh-pipeline"

# --- pipeline tree ---------------------------------------------------------
cp "$REPO"/*.py "$PREFIX/"
cp -r "$REPO/cgal_remesh" "$REPO/configs" "$REPO/scripts" "$PREFIX/"
cp "$REPO/README.md" "$REPO/LICENSE" "$PREFIX/"
cp "$REPO/installer/setup_env.py" "$PREFIX/installer/"
cp "$REPO/installer/requirements.txt" "$PREFIX/requirements.txt"
rm -f "$PREFIX"/cgal_remesh/*.exe 2>/dev/null || true

# --- native binaries (+ bundled dylibs in bin/macos/libs) ------------------
cp -R "$REPO/bin/macos/." "$PREFIX/bin/macos/"
chmod +x "$PREFIX/bin/macos/mesh_and_remesh" "$PREFIX/bin/macos/tet_remesh"

# --- launcher --------------------------------------------------------------
cp "$REPO/installer/macos/mouse-mesh-pipeline" "$ROOT/usr/local/bin/mouse-mesh-pipeline"
chmod +x "$ROOT/usr/local/bin/mouse-mesh-pipeline"

# --- .app bundle (double-click launcher into Launchpad/Finder) -------------
APP="$ROOT/Applications/Mouse Mesh Pipeline.app"
mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Mouse Mesh Pipeline</string>
  <key>CFBundleDisplayName</key><string>Mouse Mesh Pipeline</string>
  <key>CFBundleIdentifier</key><string>${IDENT}</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
  <key>CFBundleExecutable</key><string>mouse-mesh-pipeline</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/mouse-mesh-pipeline" <<'SH'
#!/bin/sh
exec /usr/local/bin/mouse-mesh-pipeline
SH
chmod +x "$APP/Contents/MacOS/mouse-mesh-pipeline"

# --- post-install script ---------------------------------------------------
cp "$REPO/installer/macos/postinstall" "$SCRIPTS/postinstall"
chmod +x "$SCRIPTS/postinstall"

# --- build the pkg ---------------------------------------------------------
OUT="$DIST/mouse-mesh-pipeline-${VERSION}.pkg"
pkgbuild --root "$ROOT" \
         --scripts "$SCRIPTS" \
         --identifier "$IDENT" \
         --version "$VERSION" \
         --install-location / \
         "$OUT"
echo ">> built $OUT"
