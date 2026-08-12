cmake_minimum_required(VERSION 3.16)
project(tet_remesh CXX)

# CGAL's tetrahedral remeshing needs C++17.
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT CMAKE_BUILD_TYPE)
  # RelWithDebInfo, not Release: -O2 but WITHOUT -DNDEBUG, so CGAL assertions
  # stay on. CGAL 6.0.1 passes them; they are a cheap guard against a broken
  # remesh. (Do NOT force NDEBUG — that was only needed to bypass CGAL 5.5.2's
  # since-fixed over-strict assertions, which then produced non-manifold junk.)
  set(CMAKE_BUILD_TYPE RelWithDebInfo CACHE STRING "" FORCE)
endif()

# Require CGAL 6.0.1 headers (header-only; newer than the MSYS2 5.5.2 package).
# Set -DCGAL_DIR=/path/to/CGAL-6.0.1 (containing include/CGAL/version.h).
# See README.md for the download.

# find_package(CGAL) is preferred; if the config package is not on CMAKE_PREFIX_PATH
# (e.g. a bare MSYS2 header install) we fall back to header-only + explicit GMP/MPFR.
find_package(CGAL QUIET)

add_executable(tet_remesh tet_remesh.cpp)

if(CGAL_FOUND)
  message(STATUS "Using CGAL config package: ${CGAL_DIR}")
  target_link_libraries(tet_remesh PRIVATE CGAL::CGAL)
else()
  message(STATUS "CGAL config package not found — header-only fallback (link GMP/MPFR).")
  find_package(Boost REQUIRED)
  target_include_directories(tet_remesh PRIVATE ${Boost_INCLUDE_DIRS})
  # CGAL is header-only but its number types need GMP + MPFR at link time.
  find_library(GMP_LIB  NAMES gmp  libgmp)
  find_library(MPFR_LIB NAMES mpfr libmpfr)
  if(NOT GMP_LIB OR NOT MPFR_LIB)
    message(FATAL_ERROR "GMP/MPFR not found (need gmp + mpfr dev libraries).")
  endif()
  target_link_libraries(tet_remesh PRIVATE ${MPFR_LIB} ${GMP_LIB})
endif()
