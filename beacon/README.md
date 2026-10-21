# Graveyard Armory Beacon — source

Players: download the ready-made exe from the [releases](https://github.com/RebboLOL/HC-Graveyard/releases/latest) and follow the [guide](../README.md). This folder is for anyone who wants to read, run or build the beacon themselves.

| File | What |
|---|---|
| `graveyard_armory.py` | the Windows app (Tkinter window, daily task, encrypted settings) |
| `armory_import.py` | Armory lookup and data-addon writer; also usable on its own from the command line |
| `graveyard_armory.ico` | app icon |
| `build.py` | builds `GraveyardArmory.exe` with PyInstaller |

## Run from source

Python 3.12 for Windows (with Tkinter, part of the standard installer). No other packages are needed.

```
python graveyard_armory.py
```

## Command line without the window

The client credentials come from environment variables, never from a file:

```
$env:BLIZZARD_CLIENT_ID = "..."
$env:BLIZZARD_CLIENT_SECRET = "..."
python armory_import.py --region eu --sv "C:\Program Files (x86)\World of Warcraft\_classic_era_\WTF\Account\<ACCOUNT>\SavedVariables\Graveyard.lua"
```

Options: `--dry-run` (list what would be asked), `--limit N`, `--recheck` (check skipped characters again now), `--no-living` (only the fallen).

## Build the exe

```
python build.py
```

The exe lands in `dist\GraveyardArmory.exe`.
