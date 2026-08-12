param([switch]$Tbb)   # -Tbb: build parallel Mesh_3 (needs Intel TBB, see below)

# Build the CGAL remeshing tools with the MSYS2 UCRT64 g++ toolchain.
#
#   powershell -ExecutionPolicy Bypass -File build.ps1          # sequential (default)
#   powershell -ExecutionPolicy Bypass -File build.ps1 -Tbb     # parallel Mesh_3 (multi-core)
#
# -Tbb parallelises the CGAL Mesh_3 refinement (Parallel_tag) across cores:
# speeds up EACH mesh ~6x on 8 cores (skin envelope 85s -> 14s), and the full
# 6-tissue envelope build 194s -> 91s. Needs Intel oneTBB.
#
#   The MSYS2 package (pacman -S mingw-w64-ucrt-x86_64-tbb) pulls gcc-libs 16,
#   which CONFLICTS with the pinned gcc 13 toolchain. So build oneTBB from source
#   with the SAME gcc 13 (guaranteed ABI match), once:
#       curl -LO https://github.com/uxlfoundation/oneTBB/archive/refs/tags/v2022.0.0.tar.gz
#       tar xf v2022.0.0.tar.gz
#       # oneTBB's assembler probe uses /dev/null (fails on native g++): in
#       # cmake/compilers/GNU.cmake, guard the version so non-numeric -> 2.30.
#       cmake -S oneTBB-2022.0.0 -B bld -G "MinGW Makefiles" -DTBB_TEST=OFF `
#             -DTBB_STRICT=OFF -DCMAKE_BUILD_TYPE=Release `
#             -DCMAKE_INSTALL_PREFIX=G:/nauchka/utilities/tbb
#       cmake --build bld -j8 ; cmake --install bld
#   Then set $env:TBB_DIR (default ..\..\..\tbb = G:\nauchka\utilities\tbb) and
#   this script links -ltbb12 -ltbbmalloc. libtbb12.dll + libtbbmalloc.dll must
#   sit beside the .exe (this repo keeps copies) or on PATH.
#
#   Tetrahedral remeshing stays sequential regardless (we run --no-remesh, moot).
#   Combine with build_envelopes.py: TBB exe + --jobs 1 (each mesh uses all cores;
#   avoid --jobs>1 which oversubscribes N processes x all cores).
#
# Requires:
#   - MSYS2 UCRT64 gcc + gmp/mpfr/boost/eigen3 (pacman -S ...gcc ...eigen3;
#     the cgal package pulls boost/eigen/gmp/mpfr, but CGAL itself comes from
#     the 6.0.1 headers below, NOT the MSYS2 5.5.2 package).
#   - CGAL 6.0.1 HEADERS (header-only; NOT the MSYS2 CGAL 5.5.2 package):
#       curl -LO https://github.com/CGAL/cgal/releases/download/v6.0.1/CGAL-6.0.1-library.tar.xz
#       tar xf CGAL-6.0.1-library.tar.xz      # produces CGAL-6.0.1/
#     Point $env:CGAL_DIR at it. Default below assumes ..\..\..\CGAL-6.0.1
#     (i.e. G:\nauchka\utilities\CGAL-6.0.1).
#
# Why CGAL 6.0.1 and not the MSYS2 5.5.2 package: 5.5.2's tetrahedral remeshing
# crashes on over-strict assertions for a valid multi-material labeled-image mesh,
# and -DNDEBUG silently produces non-manifold junk. 6.0.1 runs with assertions ON
# and produces a clean mesh. CGAL is header-only, so newer headers compile fine
# with the same MSYS2 g++/gmp/mpfr.

$ErrorActionPreference = "Stop"
$UCRT = "D:\msys64\ucrt64"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$CGAL_DIR = if ($env:CGAL_DIR) { $env:CGAL_DIR } else { Join-Path $here "..\..\..\CGAL-6.0.1" }
$cgalInc  = Join-Path $CGAL_DIR "include"

if (-not (Test-Path (Join-Path $cgalInc "CGAL\version.h"))) {
    throw "CGAL 6.0.1 headers not found at $cgalInc - set `$env:CGAL_DIR or download (see header comment)."
}

$gxx = Join-Path $UCRT "bin\g++.exe"
$inc = Join-Path $UCRT "include"
$lib = Join-Path $UCRT "lib"

Push-Location $here
try {
    # -O2 WITHOUT -DNDEBUG: keep CGAL assertions on (6.0.1 passes them; a cheap
    # safety net that would catch a genuinely broken remesh).
    $common = @("-O2", "-std=c++17", "-I$cgalInc", "-I$inc", "-L$lib", "-lmpfr", "-lgmp")
    if ($Tbb) {
        # -DCGAL_LINKED_WITH_TBB flips Concurrency_tag to Parallel_tag in the .cpp
        # and makes CGAL use TBB. Prefer a locally-built oneTBB (compatible with
        # the pinned gcc 13 toolchain); fall back to the MSYS2 ucrt64 package.
        $TBB_DIR = if ($env:TBB_DIR) { $env:TBB_DIR } else { Join-Path $here "..\..\..\tbb" }
        $tbbInc  = Join-Path $TBB_DIR "include"
        $tbbLib  = Join-Path $TBB_DIR "lib"
        if (Test-Path (Join-Path $tbbInc "oneapi\tbb.h")) {
            # oneTBB import libs: -ltbb12 (core) + -ltbbmalloc (CGAL's parallel
            # Mesh_3 uses TBB's scalable_allocator -> scalable_malloc/free).
            $common += @("-DCGAL_LINKED_WITH_TBB", "-I$tbbInc", "-L$tbbLib", "-ltbb12", "-ltbbmalloc")
            Write-Host "TBB: parallel Mesh_3 ENABLED (oneTBB from $TBB_DIR)."
            Write-Host "     libtbb12.dll must be beside the .exe or on PATH ($TBB_DIR\bin)."
        } elseif (Test-Path (Join-Path $inc "tbb\parallel_for.h")) {
            $common += @("-DCGAL_LINKED_WITH_TBB", "-ltbb", "-ltbbmalloc")
            Write-Host "TBB: parallel Mesh_3 ENABLED (MSYS2 ucrt64 tbb)."
        } else {
            throw "-Tbb requested but no TBB found. Build oneTBB from source (see header) and set `$env:TBB_DIR, or 'pacman -S mingw-w64-ucrt-x86_64-tbb'."
        }
    }
    # g++ prints deprecation warnings to stderr; under $ErrorActionPreference=Stop
    # + stream redirection PS 5.1 would wrap those as a terminating NativeCommand
    # error. Success/failure is taken from $LASTEXITCODE instead.
    $ErrorActionPreference = "Continue"
    foreach ($src in @("mesh_and_remesh", "tet_remesh")) {
        Write-Host "Compiling $src.cpp against CGAL 6.0.1 ..."
        & $gxx "$src.cpp" "-o" "$src.exe" @common
        if ($LASTEXITCODE -ne 0) { throw "build failed: $src" }
        Write-Host "  -> $src.exe"
    }
    Write-Host "Done. Add '$($UCRT)\bin' to PATH when running (for libgmp/libmpfr/libstdc++ DLLs)."
}
finally { Pop-Location }
