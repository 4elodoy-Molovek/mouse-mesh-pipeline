# -*- coding: utf-8 -*-
"""
Installer for mouse-mesh-pipeline.

Packaged by PyInstaller into a single `mouse-mesh-pipeline-setup.exe`. It bundles
the whole pipeline (Python scripts + configs + the prebuilt Windows C++ binaries
and their DLLs) as a `payload/` folder. On run it:

  1. copies the pipeline into a target folder,
  2. creates a dedicated venv there and pip-installs the dependencies INTO IT
     (the machine's own Python is never modified),
  3. points the config at the bundled mesh_and_remesh.exe,
  4. drops a "Launch GUI" .bat (running the venv's pythonw) and a desktop shortcut.

Requires Python 3.10+ already installed on the target machine (the exe itself is
tiny — the heavy libraries come from PyPI wheels at install time into the venv,
which is far more robust than freezing vtk/pymeshlab).
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, ttk

APP = "mouse-mesh-pipeline"
DEFAULT_DEST = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP)


def resource(rel: str) -> str:
    """Path to a bundled resource (PyInstaller _MEIPASS in the frozen exe)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def find_python():
    """Locate the machine's Python (NOT sys.executable — that is the frozen
    installer). Returns the launcher argv, e.g. ['py', '-3'] or ['python']."""
    for cand in (["py", "-3"], ["python"], ["python3"]):
        try:
            r = subprocess.run(
                cand
                + [
                    "-c",
                    "import sys; assert sys.version_info[:2] >= (3, 10); " "print(sys.executable)",
                ],
                capture_output=True,
                text=True,
                timeout=25,
            )
            if r.returncode == 0 and r.stdout.strip():
                return cand, r.stdout.strip()
        except Exception:
            pass
    return None, None


def patch_config(dest: str, log):
    """Point the example config at the bundled exe and clear the MSYS2 path
    (the exe's DLLs are shipped beside it)."""
    exe = os.path.join(dest, "bin", "win64", "mesh_and_remesh.exe")
    for cfg in ("pipeline_config_mouse.json", "pipeline_config.json"):
        p = os.path.join(dest, "configs", cfg)
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            d["remesh_exe"] = exe
            d["msys2_bin"] = ""
            with open(p, "w", encoding="utf-8") as f:
                json.dump(d, f, indent=4, ensure_ascii=False)
            log(f"  config {cfg}: remesh_exe -> bundled exe")
        except Exception as exc:
            log(f"  [warn] config {cfg}: {exc}")


def make_shortcut(bat: str, workdir: str, log):
    try:
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        lnk = os.path.join(desktop, "Mouse Mesh Pipeline.lnk")
        ps = (
            "$s=(New-Object -COM WScript.Shell).CreateShortcut('%s');"
            "$s.TargetPath='%s';$s.WorkingDirectory='%s';$s.Save()" % (lnk, bat, workdir)
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False, timeout=25)
        log(f"  ярлык: {lnk}")
    except Exception as exc:
        log(f"  [warn] ярлык не создан: {exc}")


def install(dest: str, log):
    log(f"Установка в: {dest}")
    payload = resource("payload")
    if not os.path.isdir(payload):
        raise RuntimeError(f"payload не найден: {payload}")
    log("Копирую файлы пайплайна...")
    shutil.copytree(payload, dest, dirs_exist_ok=True)

    pyc, pyexe = find_python()
    if not pyc:
        raise RuntimeError(
            "Python 3.10+ не найден в PATH.\n"
            "Установите Python с python.org (галочка 'Add to PATH') и повторите."
        )
    log(f"Системный Python: {pyexe}")

    # Isolate everything in a dedicated venv inside the install folder — the
    # system Python is never touched.
    venv_dir = os.path.join(dest, ".venv")
    log("Создаю изолированное окружение (venv)...")
    rc = subprocess.run(pyc + ["-m", "venv", venv_dir])
    if rc.returncode != 0:
        raise RuntimeError("не удалось создать venv (нужен модуль venv в системном Python).")
    vpy = os.path.join(venv_dir, "Scripts", "python.exe")
    vpyw = os.path.join(venv_dir, "Scripts", "pythonw.exe")

    log("Обновляю pip в venv...")
    subprocess.run([vpy, "-m", "pip", "install", "--upgrade", "pip"], check=False)
    log("Устанавливаю зависимости в venv (numpy/scipy/vtk/pymeshlab/...). Несколько минут...")
    req = os.path.join(dest, "requirements.txt")
    rc = subprocess.run([vpy, "-m", "pip", "install", "-r", req])
    if rc.returncode != 0:
        raise RuntimeError("pip install завершился с ошибкой.")

    patch_config(dest, log)

    bat = os.path.join(dest, "Launch GUI.bat")
    with open(bat, "w", encoding="utf-8") as f:
        f.write('@echo off\r\ncd /d "%~dp0"\r\n')
        f.write(f'"{vpyw}" pipeline_manager.py\r\n')
    log(f"Лаунчер: {bat}")
    make_shortcut(bat, dest, log)
    log("\nГотово! Запуск — «Launch GUI.bat» или ярлык на рабочем столе.")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("mouse-mesh-pipeline — установка")
        self.geometry("640x460")
        self.q: queue.Queue = queue.Queue()

        tk.Label(self, text="Установка пайплайна и GUI", font=("Segoe UI", 13, "bold")).pack(
            pady=(12, 2)
        )
        tk.Label(
            self,
            text="Требуется установленный Python 3.10+ (зависимости ставятся через pip).",
            fg="gray30",
        ).pack()

        row = tk.Frame(self)
        row.pack(fill="x", padx=14, pady=10)
        tk.Label(row, text="Папка:").pack(side="left")
        self.dest = tk.StringVar(value=DEFAULT_DEST)
        tk.Entry(row, textvariable=self.dest).pack(side="left", fill="x", expand=True, padx=6)
        tk.Button(row, text="Обзор...", command=self._browse).pack(side="right")

        self.btn = tk.Button(
            self, text="Установить", font=("Segoe UI", 11, "bold"), bg="#2d7", command=self._start
        )
        self.btn.pack(pady=4)
        self.pb = ttk.Progressbar(self, mode="indeterminate")
        self.pb.pack(fill="x", padx=14, pady=4)
        self.log = tk.Text(self, height=16, font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=14, pady=8)

    def _browse(self):
        d = filedialog.askdirectory(initialdir=os.path.dirname(self.dest.get()) or "C:\\")
        if d:
            self.dest.set(os.path.join(d, APP))

    def _log(self, msg):
        self.q.put(msg)

    def _start(self):
        self.btn.config(state="disabled")
        self.pb.start(12)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            install(self.dest.get().strip(), self._log)
        except Exception as exc:
            self._log(f"\nОШИБКА: {exc}")
        finally:
            self.q.put(None)

    def _finish(self):
        self.pb.stop()
        self.btn.config(state="normal", text="Готово")


def main():
    app = App()

    def drain():
        done = False
        while not app.q.empty():
            m = app.q.get()
            if m is None:
                done = True
            else:
                app.log.insert("end", m + "\n")
                app.log.see("end")
        if done:
            app._finish()
        app.after(120, drain)

    app.after(120, drain)
    app.mainloop()


if __name__ == "__main__":
    main()
