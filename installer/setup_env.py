# -*- coding: utf-8 -*-
"""
Headless environment setup for a deployed mouse-mesh-pipeline tree.

This is the non-GUI counterpart of installer/mmp_setup.py. The native packages
(deb / rpm / Arch pkg / macOS .pkg) drop the pipeline into a fixed prefix and
then run this from their post-install script:

    python3 setup_env.py <install_dir>

It creates a dedicated virtualenv inside <install_dir>/.venv, pip-installs the
requirements into it (the system Python is never touched) and points every
bundled config at the platform's prebuilt mesh_and_remesh binary. Re-running is
safe: an existing venv is reused.

AppImage does NOT use this (it ships a fully self-contained Python already).
"""

import json
import os
import subprocess
import sys


def _bin_subdir() -> str:
    """Where the native C++ binaries live inside the install tree."""
    if sys.platform == "darwin":
        return os.path.join("bin", "macos")
    if os.name == "nt":
        return os.path.join("bin", "win64")
    return os.path.join("bin", "linux64")


def _exe_name() -> str:
    return "mesh_and_remesh.exe" if os.name == "nt" else "mesh_and_remesh"


def patch_configs(dest: str) -> None:
    """Point every example config at the bundled native binary."""
    exe = os.path.join(dest, _bin_subdir(), _exe_name())
    cfg_dir = os.path.join(dest, "configs")
    if not os.path.isdir(cfg_dir):
        return
    for name in os.listdir(cfg_dir):
        if not name.endswith(".json"):
            continue
        path = os.path.join(cfg_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg["remesh_exe"] = exe
            cfg["msys2_bin"] = ""  # DLLs ship beside the exe / not needed off Windows
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=4, ensure_ascii=False)
            print(f"  config {name}: remesh_exe -> {exe}")
        except Exception as exc:  # noqa: BLE001 - report and keep going
            print(f"  [warn] config {name}: {exc}")


def make_venv(dest: str) -> str:
    """Create <dest>/.venv (reused if present) and return its python path."""
    venv_dir = os.path.join(dest, ".venv")
    py = os.path.join(
        venv_dir,
        "Scripts" if os.name == "nt" else "bin",
        "python.exe" if os.name == "nt" else "python",
    )
    if not os.path.exists(py):
        print("Creating virtualenv (.venv) ...")
        subprocess.check_call([sys.executable, "-m", "venv", venv_dir])
    return py


def install(dest: str) -> None:
    dest = os.path.abspath(dest)
    print(f"Setting up mouse-mesh-pipeline in: {dest}")
    py = make_venv(dest)

    subprocess.check_call([py, "-m", "pip", "install", "--upgrade", "pip"])
    req = os.path.join(dest, "requirements.txt")
    print("Installing dependencies into the venv (numpy/scipy/vtk/pymeshlab/...).")
    subprocess.check_call([py, "-m", "pip", "install", "-r", req])

    patch_configs(dest)
    print("Done.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: {os.path.basename(sys.argv[0])} <install_dir>")
    try:
        install(sys.argv[1])
    except subprocess.CalledProcessError as exc:
        sys.exit(f"setup failed: {exc}")
