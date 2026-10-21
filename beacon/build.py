"""Build GraveyardArmory.exe (Windows) with PyInstaller.

    python build.py

Result: dist\\GraveyardArmory.exe
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
PYINSTALLER = "pyinstaller==6.16.0"


def main():
    subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", PYINSTALLER],
                   check=True)
    icon = HERE / "graveyard_armory.ico"
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
                    "--noupx", "--name", "GraveyardArmory", "--icon", str(icon),
                    "--add-data", f"{icon};.", "--paths", str(HERE),
                    str(HERE / "graveyard_armory.py")], check=True, cwd=str(HERE))
    print(HERE / "dist" / "GraveyardArmory.exe")


if __name__ == "__main__":
    main()
