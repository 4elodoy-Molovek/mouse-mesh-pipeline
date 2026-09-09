#!/usr/bin/env bash
# Build a self-contained mouse-mesh-pipeline AppImage.
#
#   installer/appimage/build.sh
#
# Bundles a full Miniforge Python (+ the pip requirements), the pipeline scripts
# and the prebuilt Linux CGAL binaries together with their shared libraries.
# Expects the native binaries in ./bin/linux64/ and to be run from the repo root.
set -euo pipefail

# Retry a command a few times with linear backoff. The AppImage build pulls
# Miniforge, the full pip stack and appimagetool over the network on a hosted
# runner; a single transient hiccup there must not fail an otherwise-green CI.
retry() {
    local n=0 max=5
    until "$@"; do
        n=$((n + 1))
        [ "$n" -ge "$max" ] && { echo "!! gave up after $max tries: $*" >&2; return 1; }
        echo ">> retry $n/$max in $((n * 5))s: $*" >&2
        sleep $((n * 5))
    done
}

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"

VERSION="${VERSION:-0.0.0}"
ARCH="${ARCH:-x86_64}"
APPDIR="$REPO/AppDir"
DIST="$REPO/dist"
mkdir -p "$DIST"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib" \
         "$APPDIR/usr/share/mouse-mesh-pipeline" \
         "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/scalable/apps"

# --- bundled Python (Miniforge, relocatable, ships tkinter) ----------------
echo ">> installing Miniforge into the AppDir"
MF="Miniforge3-Linux-${ARCH}.sh"
retry curl -fL --retry 5 --retry-all-errors --retry-delay 5 -o "/tmp/$MF" \
    "https://github.com/conda-forge/miniforge/releases/latest/download/$MF"
bash "/tmp/$MF" -b -p "$APPDIR/usr/conda"
CPY="$APPDIR/usr/conda/bin/python"
retry "$APPDIR/usr/conda/bin/conda" install -y -n base tk >/dev/null

echo ">> installing pip requirements into the bundled Python"
retry "$CPY" -m pip install --upgrade pip
retry "$CPY" -m pip install --retries 5 -r installer/requirements.txt

# shrink: drop caches, tests and pkg tarballs
"$APPDIR/usr/conda/bin/conda" clean -afy >/dev/null || true
find "$APPDIR/usr/conda" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
find "$APPDIR/usr/conda" -type d -name tests -prune -exec rm -rf {} + 2>/dev/null || true

# --- pipeline sources ------------------------------------------------------
echo ">> copying pipeline sources"
cp "$REPO"/*.py "$APPDIR/usr/share/mouse-mesh-pipeline/"
cp -r "$REPO/cgal_remesh" "$APPDIR/usr/share/mouse-mesh-pipeline/"
cp -r "$REPO/configs" "$APPDIR/usr/share/mouse-mesh-pipeline/"
cp "$REPO/README.md" "$REPO/LICENSE" "$APPDIR/usr/share/mouse-mesh-pipeline/"
rm -f "$APPDIR"/usr/share/mouse-mesh-pipeline/cgal_remesh/*.exe 2>/dev/null || true

# --- native binaries + their shared libraries ------------------------------
echo ">> bundling native binaries and their libraries"
cp bin/linux64/mesh_and_remesh bin/linux64/tet_remesh "$APPDIR/usr/bin/"
chmod +x "$APPDIR/usr/bin/mesh_and_remesh" "$APPDIR/usr/bin/tet_remesh"
for bin in "$APPDIR/usr/bin/mesh_and_remesh" "$APPDIR/usr/bin/tet_remesh"; do
    ldd "$bin" | awk '/=>/ {print $3}' | while read -r lib; do
        case "$lib" in
            */libgmp.so*|*/libmpfr.so*|*/libtbb.so*|*/libtbbmalloc.so*|\
            */libstdc++.so*|*/libgcc_s.so*|*/libgomp.so*)
                cp -n "$lib" "$APPDIR/usr/lib/" 2>/dev/null || true ;;
        esac
    done
done

# --- AppImage metadata -----------------------------------------------------
cp installer/appimage/AppRun "$APPDIR/AppRun"
chmod +x "$APPDIR/AppRun"
cp installer/assets/mouse-mesh-pipeline.desktop \
   "$APPDIR/usr/share/applications/mouse-mesh-pipeline.desktop"
cp installer/assets/mouse-mesh-pipeline.desktop "$APPDIR/mouse-mesh-pipeline.desktop"
cp installer/assets/mouse-mesh-pipeline.svg \
   "$APPDIR/usr/share/icons/hicolor/scalable/apps/mouse-mesh-pipeline.svg"
cp installer/assets/mouse-mesh-pipeline.svg "$APPDIR/mouse-mesh-pipeline.svg"
ln -sf mouse-mesh-pipeline.svg "$APPDIR/.DirIcon"

# --- pack ------------------------------------------------------------------
echo ">> packing AppImage"
TOOL="/tmp/appimagetool-${ARCH}.AppImage"
retry curl -fL --retry 5 --retry-all-errors --retry-delay 5 -o "$TOOL" \
    "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
chmod +x "$TOOL"
OUT="$DIST/mouse-mesh-pipeline-${VERSION}-${ARCH}.AppImage"
ARCH="$ARCH" "$TOOL" --appimage-extract-and-run "$APPDIR" "$OUT"
echo ">> built $OUT"
