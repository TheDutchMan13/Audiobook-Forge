"""Tiny launcher that becomes 'Audiobook Forge.exe'.

It sits in the project folder next to forge_gui.py and opens the window using the
Python runtime in the 'runtime' folder beside it. Dropping an .epub on the .exe opens it.
"""
import ctypes
import os
import subprocess
import sys
from pathlib import Path

OLD_RUNTIME = Path(os.environ.get("FORGE_RUNTIME", r"C:\forge-runtime"))
CREATE_NO_WINDOW = 0x08000000


def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def fail(message):
    ctypes.windll.user32.MessageBoxW(0, message, "Audiobook Forge", 0x10)
    sys.exit(1)


def main():
    app = app_dir()
    bundled = app / "runtime" / "python" / "pythonw.exe"
    pythonw = bundled if bundled.exists() else OLD_RUNTIME / "Scripts" / "pythonw.exe"
    gui = app / "forge_gui.py"
    if not pythonw.exists():
        fail(f"The Python runtime was not found:\n{bundled}\n\nKeep the 'runtime' folder next to this program.")
    if not gui.exists():
        fail(f"forge_gui.py was not found next to this program:\n{gui}")
    subprocess.Popen([str(pythonw), str(gui), *sys.argv[1:]], cwd=str(app), creationflags=CREATE_NO_WINDOW)


if __name__ == "__main__":
    main()
