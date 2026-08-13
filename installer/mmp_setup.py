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
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, ttk

APP = "mouse-mesh-pipeline"
DEFAULT_DEST = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), APP)

# The scientific wheels (vtk/pymeshlab/pymeshfix/...) ship binary wheels only for
# these CPython minors. A newer Python (e.g. 3.14) usually has no matching wheel,
# so pip would try to build from source and fail — we warn and prefer these.
SUPPORTED_MINORS = ((3, 12), (3, 11), (3, 13), (3, 10))

# Don't pop a console window for each child process (installer is --windowed).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def resource(rel: str) -> str:
    """Path to a bundled resource (PyInstaller _MEIPASS in the frozen exe)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _query_python(cand):
    """Return (exe, (major, minor)) for a launcher argv, or (None, None)."""
    try:
        r = subprocess.run(
            cand
            + ["-c", "import sys;print('%d.%d\\t%s' % (*sys.version_info[:2], sys.executable))"],
            capture_output=True,
            text=True,
            timeout=25,
            creationflags=_NO_WINDOW,
        )
        if r.returncode == 0 and r.stdout.strip():
            ver, _, exe = r.stdout.strip().partition("\t")
            maj, _, minor = ver.partition(".")
            return exe.strip(), (int(maj), int(minor))
    except Exception:
        pass
    return None, None


def find_python():
    """Locate a suitable machine Python (NOT sys.executable — that's the frozen
    installer). Prefer a version that has prebuilt scientific wheels; otherwise
    fall back to any >= 3.10. Returns (argv, exe, (major, minor)) or (None,)*3."""
    for m in SUPPORTED_MINORS:  # e.g. py -3.12, py -3.11, ...
        exe, ver = _query_python(["py", "-%d.%d" % m])
        if exe:
            return ["py", "-%d.%d" % m], exe, ver
    for cand in (["py", "-3"], ["python"], ["python3"]):
        exe, ver = _query_python(cand)
        if exe and ver >= (3, 10):
            return cand, exe, ver
    return None, None, None


def run_logged(cmd, log, logf):
    """Run a command, streaming combined stdout+stderr to the GUI log AND the
    install.log file, with no flashing console window. Returns the exit code."""
    line0 = "$ " + " ".join(str(c) for c in cmd)
    log(line0)
    logf.write("\n" + line0 + "\n")
    logf.flush()
    try:
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=_NO_WINDOW,
        )
    except Exception as exc:
        logf.write("  [spawn error] %s\n" % exc)
        logf.flush()
        log("  [spawn error] %s" % exc)
        return 1
    for line in p.stdout:
        line = line.rstrip("\n")
        logf.write(line + "\n")
        logf.flush()
        log(line)
    p.wait()
    logf.write("[exit %d]\n" % p.returncode)
    logf.flush()
    return p.returncode


def _bare_names(req_path):
    """Package names from requirements.txt, stripped of version pins/markers."""
    names = []
    with open(req_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            name = re.split(r"[<>=!~;\[ ]", line, 1)[0].strip()
            if name:
                names.append(name)
    return names


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
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            check=False,
            timeout=25,
            creationflags=_NO_WINDOW,
        )
        log(f"  ярлык: {lnk}")
    except Exception as exc:
        log(f"  [warn] ярлык не создан: {exc}")


def install(dest: str, log):
    """Deploy the pipeline into `dest`, writing a full install.log alongside so
    failures (especially pip) are diagnosable instead of vanishing in a console
    that closes instantly."""
    dest = os.path.abspath(dest)
    os.makedirs(dest, exist_ok=True)
    log_path = os.path.join(dest, "install.log")
    logf = open(log_path, "w", encoding="utf-8")

    def L(msg=""):
        """Tee a line to the GUI and the install.log file."""
        log(msg)
        try:
            logf.write(str(msg) + "\n")
            logf.flush()
        except Exception:
            pass

    try:
        L("=== mouse-mesh-pipeline installer ===")
        L("time      : " + time.strftime("%Y-%m-%d %H:%M:%S"))
        L("platform  : " + platform.platform())
        L("installer : python " + sys.version.split()[0] + " (frozen)")
        L("dest      : " + dest)
        L("log       : " + log_path)
        L("")

        payload = resource("payload")
        if not os.path.isdir(payload):
            raise RuntimeError("payload не найден: " + payload)
        L("Копирую файлы пайплайна...")
        shutil.copytree(payload, dest, dirs_exist_ok=True)

        pyc, pyexe, pyver = find_python()
        if not pyc:
            raise RuntimeError(
                "Python 3.10+ не найден в PATH.\n"
                "Установите Python с python.org (галочка 'Add to PATH') и повторите."
            )
        L("Системный Python: %s  (версия %d.%d)" % (pyexe, pyver[0], pyver[1]))
        if pyver not in SUPPORTED_MINORS:
            L("")
            L("[ВНИМАНИЕ] Python %d.%d вне протестированного диапазона 3.10-3.13." % pyver)
            L("           У научных пакетов (vtk/pymeshlab/...) может не быть готовых")
            L("           колёс под эту версию, и pip не соберёт их из исходников.")
            L("           Рекомендуется установить Python 3.12 и повторить установку.")
            L("           Сейчас будет попытка + фолбэк на последние версии пакетов.")
            L("")

        # Isolate everything in a dedicated venv — the system Python is untouched.
        venv_dir = os.path.join(dest, ".venv")
        L("Создаю изолированное окружение (venv)...")
        if run_logged(pyc + ["-m", "venv", venv_dir], log, logf) != 0:
            raise RuntimeError("не удалось создать venv (нужен модуль venv в системном Python).")
        vpy = os.path.join(venv_dir, "Scripts", "python.exe")
        vpyw = os.path.join(venv_dir, "Scripts", "pythonw.exe")

        L("Обновляю pip/setuptools/wheel...")
        run_logged(
            [vpy, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"], log, logf
        )

        req = os.path.join(dest, "requirements.txt")
        L("Устанавливаю зависимости в venv (numpy/scipy/vtk/pymeshlab/...). Несколько минут...")
        rc = run_logged([vpy, "-m", "pip", "install", "--prefer-binary", "-r", req], log, logf)
        if rc != 0:
            L("")
            L("[!] Установка по закреплённым версиям не удалась.")
            L("    Пробую последние совместимые версии (часто помогает на новом Python)...")
            rc = run_logged(
                [vpy, "-m", "pip", "install", "--prefer-binary"] + _bare_names(req), log, logf
            )
        if rc != 0:
            hint = ""
            if pyver not in SUPPORTED_MINORS:
                hint = (
                    "\nВероятная причина: Python %d.%d слишком новый — для части пакетов\n"
                    "нет готовых колёс. Установите Python 3.12 и повторите." % pyver
                )
            raise RuntimeError("pip install завершился с ошибкой.\nЛог: %s%s" % (log_path, hint))

        patch_config(dest, L)

        bat = os.path.join(dest, "Launch GUI.bat")
        with open(bat, "w", encoding="utf-8") as f:
            f.write('@echo off\r\ncd /d "%~dp0"\r\n')
            f.write('"%s" pipeline_manager.py\r\n' % vpyw)
        L("Лаунчер: " + bat)
        make_shortcut(bat, dest, L)
        L("")
        L("Готово! Запуск — «Launch GUI.bat» или ярлык на рабочем столе.")
    except Exception:
        L("")
        L("ОШИБКА при установке (полный лог: %s):" % log_path)
        L(traceback.format_exc())
        raise
    finally:
        try:
            logf.close()
        except Exception:
            pass


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
