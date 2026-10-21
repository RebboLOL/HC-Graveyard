"""Graveyard Armory - Windows-Begleitprogramm zu Graveyard.

Holt das Gear gefallener Chars aus dem offiziellen Arsenal (Modul
tools\\armory_import) und schreibt das Datenaddon Graveyard_Armory, das
Graveyard beim Login uebernimmt und per Gilden-Sync weitergibt.
Oberflaeche Tkinter, Zugangsdaten per Windows-DPAPI (nur dieser Windows-
Benutzer kann sie entschluesseln), optionale Windows-Aufgabe (taeglich).

Aufruf
    GraveyardArmory.exe              Oberflaeche
    GraveyardArmory.exe --auto       Lauf ohne Fenster (Aufgabenplanung)
    GraveyardArmory.exe --check      Einrichtung pruefen, kein Netz (Ergebnis in armory.log)
    --home <Ordner>                  anderer Einstellungsordner (Tests)
Einstellungen, Cache und Protokoll: %APPDATA%\\Graveyard Armory
"""
import argparse
import base64
import ctypes
import html
import json
import os
import pathlib
import queue
import re
import ssl
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from ctypes import wintypes

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "tools"))
import armory_import as armory  # noqa: E402

APP = "Graveyard Armory"
VERSION = "1.1.0"
TASK = "Graveyard Armory"
CLIENTS_URL = "https://develop.battle.net/access/clients"
REGIONS = ("eu", "us", "kr", "tw")
ENTROPY = b"Graveyard Armory/credentials/1"
TASK_TIME = "12:00"
KEEP_BACKUPS = 14
NO_SV = ("No Graveyard data in this folder yet. Enable Graveyard, log in once, "
         "then log out or type /reload so WoW saves it.")


class Problem(Exception):
    """Fehler mit Meldung fuer den Nutzer (englisch)."""


class Busy(Exception):
    pass


def default_home():
    return pathlib.Path(os.environ.get("APPDATA") or pathlib.Path.home()) / APP


def resource(name):
    base = getattr(sys, "_MEIPASS", None) or pathlib.Path(__file__).resolve().parent
    return pathlib.Path(base) / name


# ------------------------------------------------------------------ DPAPI
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_wchar_p if protect else ctypes.c_void_p,
                   ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                   ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buf = ctypes.create_string_buffer(data, len(data))
    ebuf = ctypes.create_string_buffer(ENTROPY, len(ENTROPY))
    inp = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    ent = _Blob(len(ENTROPY), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    descr = APP if protect else None
    if not fn(ctypes.byref(inp), descr, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def dpapi_protect(data):
    return _dpapi(data, True)


def dpapi_unprotect(data):
    return _dpapi(data, False)


# --------------------------------------------------------------- Settings
class Store:
    """settings.json im Einstellungsordner; Zugangsdaten nur als DPAPI-Blob."""

    def __init__(self, home):
        self.home = pathlib.Path(home)
        self.file = self.home / "settings.json"
        try:
            self.data = json.loads(self.file.read_text(encoding="utf-8"))
            if not isinstance(self.data, dict):
                self.data = {}
        except (OSError, ValueError):
            self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, **kw):
        self.data.update(kw)
        self.save()

    def save(self):
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_name("settings.tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, self.file)

    def credentials(self):
        blob = self.data.get("credentials")
        if not blob:
            return None
        try:
            d = json.loads(dpapi_unprotect(base64.b64decode(blob)).decode("utf-8"))
            return d["id"], d["secret"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save_credentials(self, cid, secret):
        blob = dpapi_protect(json.dumps({"id": cid, "secret": secret}).encode("utf-8"))
        self.set(credentials=base64.b64encode(blob).decode("ascii"))

    def forget_credentials(self):
        if self.data.pop("credentials", None) is not None:
            self.save()


class RunLock:
    """Verhindert zwei gleichzeitige Laeufe (Fenster + Aufgabe)."""

    def __init__(self, home):
        self.path = pathlib.Path(home) / "run.lock"
        self.fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if time.time() - self.path.stat().st_mtime > 3600:
                self.path.unlink()
        except OSError:
            pass
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise Busy() from None
        return self

    def __exit__(self, *exc):
        os.close(self.fd)
        try:
            self.path.unlink()
        except OSError:
            pass


def append_log(home, mode, lines):
    """armory.log: letzte 500 Zeilen, nie Zugangsdaten."""
    f = pathlib.Path(home) / "armory.log"
    try:
        old = f.read_text(encoding="utf-8").splitlines() if f.exists() else []
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        new = old + [f"{stamp} [{mode}] {line}" for line in lines]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("\n".join(new[-500:]) + "\n", encoding="utf-8")
    except OSError:
        pass


# ------------------------------------------------------------- WoW-Ordner
def normalize_wow(path):
    """Gewaehlter Ordner -> _classic_era_-Ordner oder None."""
    if not path:
        return None
    p = pathlib.Path(path)

    def ok(c):
        return c.is_dir() and ((c / "WTF").is_dir() or (c / "Interface").is_dir()
                               or (c / "WowClassic.exe").exists())
    cands = [p, p / "_classic_era_"] + [q for q in p.parents if q.name.lower() == "_classic_era_"]
    for c in cands:
        if c.name.lower() == "_classic_era_" and ok(c):
            return c
    if (p / "WTF").is_dir() and (p / "Interface").is_dir():
        return p
    return None


def _fixed_drives():
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    for i in range(26):
        if mask & (1 << i):
            root = f"{chr(65 + i)}:\\"
            if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) == 3:  # DRIVE_FIXED
                yield root


def find_wow():
    """-> moegliche _classic_era_-Ordner, die mit Graveyard-Daten zuerst."""
    bases = []
    try:
        import winreg
        keys = ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"))
        for hive, sub in keys:
            try:
                with winreg.OpenKey(hive, sub) as k:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            with winreg.OpenKey(k, winreg.EnumKey(k, i)) as e:
                                name = winreg.QueryValueEx(e, "DisplayName")[0]
                                if "warcraft" in str(name).lower():
                                    bases.append(winreg.QueryValueEx(e, "InstallLocation")[0])
                        except OSError:
                            continue
            except OSError:
                continue
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft") as k:
                bases.append(winreg.QueryValueEx(k, "InstallPath")[0])
        except OSError:
            pass
    except ImportError:
        pass
    # Battle.net-Agent: Installationspfade stehen als Strings in product.db
    pdb = pathlib.Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Battle.net" / "Agent" / "product.db"
    try:
        raw = pdb.read_bytes()
        for m in re.finditer(rb"[A-Za-z]:[/\\][^\x00-\x1f]{1,250}", raw):
            s = m.start()
            n = raw[s - 1] if s else 0
            chunk = raw[s:s + n] if 0 < n < 128 and s + n <= len(raw) else m.group()
            text = chunk.decode("utf-8", "ignore")
            if "warcraft" in text.lower():
                bases.append(text)
    except OSError:
        pass
    for root in _fixed_drives():
        for rel in (r"Program Files (x86)\World of Warcraft", r"Program Files\World of Warcraft",
                    r"World of Warcraft", r"Games\World of Warcraft", r"Blizzard\World of Warcraft",
                    r"Battle.net\World of Warcraft"):
            bases.append(root + rel)
    found = []
    for b in bases:
        n = normalize_wow(b)
        if n and all(str(n).lower() != str(f).lower() for f in found):
            found.append(n)
    found.sort(key=lambda p: not armory.find_savedvars(None, p))
    return found


def detect_region(wow):
    try:
        txt = (pathlib.Path(wow) / "WTF" / "Config.wtf").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r'^SET portal "(\w+)"', txt, re.M | re.I)
    r = m.group(1).lower() if m else None
    return r if r in REGIONS else None


# ------------------------------------------------------------ Import-Lauf
def prune_backups(out_addon, keep=KEEP_BACKUPS):
    files = sorted(pathlib.Path(out_addon).glob("Data_backup_*.lua"))
    files = [f for f in files if re.fullmatch(r"Data_backup_\d{8}\.lua", f.name)]
    for f in files[:-keep] if len(files) > keep else []:
        try:
            f.unlink()
        except OSError:
            pass


def result_line(stats, calls):
    return " · ".join(f"{k} {stats[k]}" for k in armory.STATS_KEYS) + f" · API calls {calls}"


def run_import(wow, region, cid, secret, home, log=None, progress=None, stop=None, opener=None):
    """Ein kompletter Lauf wie armory_import.main, fuer Fenster und Aufgabe."""
    wow, home = pathlib.Path(wow), pathlib.Path(home)
    if region not in REGIONS:
        raise Problem(f"Unknown region {region!r}.")
    paths = armory.find_savedvars(None, wow)
    if not paths:
        raise Problem(NO_SV)
    try:
        all_graves = armory.load_graves(paths)
    except (OSError, ValueError) as e:
        raise Problem(f"Could not read Graveyard's saved data: {e}") from None
    home.mkdir(parents=True, exist_ok=True)
    cache_path = home / f"armory_cache_{region}.json"
    try:
        cache = armory.load_cache(cache_path)
    except (OSError, ValueError):
        cache_path.replace(cache_path.with_suffix(".broken.json"))
        cache = {}
    api = armory.Api(region, cid, secret, opener)
    now = int(time.time())
    try:
        stats = armory.fetch_gear(all_graves, api, cache, now, log=log or (lambda s: None),
                                  progress=progress, stop=stop)
        # Gilden-Gedaechtnis: Gear lebender Gildies (Graveyard M20)
        members = armory.load_guild(paths)
        lstats = armory.fetch_living(members, api, cache, now, log=log or (lambda s: None),
                                     stop=stop) if members else {}
    except armory.TokenError as e:
        d = e.detail
        if isinstance(d, int) and d in (400, 401, 403):
            raise Problem(f"Blizzard did not accept the API client (HTTP {d}). "
                          "Check Client ID and Client secret.") from None
        if isinstance(d, int):
            raise Problem(f"Blizzard's login service answered HTTP {d}. Please try again later.") from None
        raise Problem(f"Could not reach Blizzard ({d}). Check your internet connection.") from None
    finally:
        armory.save_cache(cache_path, cache)
    entries = armory.collect_entries(all_graves, cache, now)
    living = armory.collect_living(members, cache)
    out_addon = wow / "Interface" / "AddOns" / "Graveyard_Armory"
    first = not (out_addon / "Graveyard_Armory.toc").exists()
    try:
        data = armory.write_addon(entries, region, now, out_addon=out_addon, generator=APP, living=living)
    except PermissionError:
        raise Problem(f"No permission to write to {out_addon}. Check the folder permissions "
                      "or start Graveyard Armory once as administrator.") from None
    prune_backups(out_addon)
    return {"stats": stats, "graves": len(all_graves), "entries": sum(1 for e in entries if e["gear"]),
            "sexes": sum(1 for e in entries if e.get("sex")), "data": str(data),
            "living": len(living), "members": len(members), "livingStats": lstats,
            "calls": api.calls, "first": first, "cancelled": bool(stop and stop())}


def summary_line(res):
    living = f", {res['living']} living guildmates" if res.get("members") else ""
    return f"{res['entries']} fallen with Armory gear, {res['sexes']} genders known{living} -> {res['data']}"


# -------------------------------------------------------- Aufgabenplanung
def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _schtasks(*args):
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, errors="replace",
                          creationflags=_no_window())


def self_command(home=None):
    """-> (Programm, Argumente) fuer die Aufgabe."""
    extra = f' --home "{home}"' if home and pathlib.Path(home) != default_home() else ""
    if getattr(sys, "frozen", False):
        return sys.executable, "--auto" + extra
    pyw = pathlib.Path(sys.executable).with_name("pythonw.exe")
    return str(pyw if pyw.exists() else sys.executable), f'"{pathlib.Path(__file__).resolve()}" --auto' + extra


def task_xml(command, arguments, at=TASK_TIME):
    esc = html.escape
    start = time.strftime("%Y-%m-%dT") + at + ":00"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{esc(APP)}: fetches Armory gear of fallen characters for the Graveyard addon.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{esc(command)}</Command>
      <Arguments>{esc(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def task_command(name=TASK):
    """-> (Programm, Argumente) der eingerichteten Aufgabe oder None."""
    r = _schtasks("/Query", "/TN", name, "/XML")
    if r.returncode != 0:
        return None
    cmd = re.search(r"<Command>(.*?)</Command>", r.stdout, re.S)
    args = re.search(r"<Arguments>(.*?)</Arguments>", r.stdout, re.S)
    return (html.unescape(cmd.group(1)) if cmd else "", html.unescape(args.group(1)) if args else "")


def install_task(home, name=TASK, command=None):
    cmd, args = command or self_command(home)
    home = pathlib.Path(home)
    home.mkdir(parents=True, exist_ok=True)
    tmp = home / "task.xml"
    tmp.write_text(task_xml(cmd, args), encoding="utf-16")
    try:
        r = _schtasks("/Create", "/TN", name, "/XML", str(tmp), "/F")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if r.returncode != 0:
        raise Problem("Windows Task Scheduler refused the task: " + (r.stderr or r.stdout).strip())


def remove_task(name=TASK):
    return _schtasks("/Delete", "/TN", name, "/F").returncode == 0


# ---------------------------------------------------- Lauf ohne Fenster
def auto_run(home, opener=None):
    store, lines = Store(home), []
    try:
        with RunLock(home):
            wow = normalize_wow(store.get("wow")) or next(iter(find_wow()), None)
            if not wow:
                raise Problem("World of Warcraft (_classic_era_) not found. Open Graveyard Armory and pick the folder.")
            creds = store.credentials()
            if not creds:
                raise Problem("No saved API client. Open Graveyard Armory, enter it and tick 'Remember'.")
            region = store.get("region") or detect_region(wow) or "eu"
            res = run_import(wow, region, *creds, home, log=lines.append, opener=opener)
            text = result_line(res["stats"], res["calls"])
            lines.append(text)
            lines.append(summary_line(res))
            store.set(last_run={"at": int(time.time()), "ok": True, "mode": "auto", "text": text,
                                "entries": res["entries"]})
            return 0
    except Busy:
        lines.append("Another update is running - skipped.")
        return 0
    except Problem as e:
        lines.append("ERROR: " + str(e))
        store.set(last_run={"at": int(time.time()), "ok": False, "mode": "auto", "text": str(e)})
        return 3
    except Exception as e:  # noqa: BLE001 - Aufgabe darf nie mit Dialog haengen
        lines.append(f"ERROR: unexpected {type(e).__name__}: {e}")
        return 4
    finally:
        append_log(home, "auto", lines)


def check(home):
    """Einrichtung pruefen ohne Netz; Ergebnis ins Protokoll (und stdout)."""
    store, lines, ok = Store(home), [f"{APP} {VERSION} check"], True
    wow = normalize_wow(store.get("wow"))
    found = find_wow()
    lines.append(f"saved folder: {wow or '-'} | detected: {', '.join(map(str, found)) or '-'}")
    wow = wow or next(iter(found), None)
    if wow:
        paths = armory.find_savedvars(None, wow)
        lines.append(f"Graveyard save files: {len(paths)}")
        if paths:
            try:
                lines.append(f"fallen without gear or gender: {len(armory.load_graves(paths))}")
            except (OSError, ValueError) as e:
                ok = False
                lines.append(f"ERROR reading save data: {e}")
        else:
            ok = False
            lines.append(NO_SV)
        data = wow / "Interface" / "AddOns" / "Graveyard_Armory" / "Data.lua"
        lines.append(f"data addon: {'present' if data.exists() else 'not yet written'}")
        lines.append(f"region: {store.get('region') or '-'} (WoW settings: {detect_region(wow) or '-'})")
    else:
        ok = False
        lines.append("ERROR: World of Warcraft (_classic_era_) not found")
    has = bool(store.get("credentials"))
    creds = store.credentials() if has else None
    lines.append("saved API client: " + ("yes, readable" if creds else "yes, NOT readable" if has else "no"))
    try:
        tc = task_command()
    except OSError:
        tc = None
    lines.append("scheduled task: " + (f"yes ({tc[0]} {tc[1]})" if tc else "no"))
    try:
        n = ssl.create_default_context().cert_store_stats().get("x509_ca", 0)
        lines.append(f"TLS: ok ({n} CA certificates)")
    except Exception as e:  # noqa: BLE001
        ok = False
        lines.append(f"TLS: ERROR {e}")
    try:
        import tkinter
        from tkinter import ttk
        root = tkinter.Tk()
        root.withdraw()
        ttk.Style(root).theme_use("clam")
        root.iconbitmap(default=str(resource("graveyard_armory.ico")))
        lines.append(f"Tk: ok ({root.tk.call('info', 'patchlevel')}, clam theme, icon)")
        root.destroy()
    except Exception as e:  # noqa: BLE001
        ok = False
        lines.append(f"Tk: ERROR {e}")
    lines.append("result: " + ("OK" if ok else "PROBLEMS FOUND"))
    append_log(home, "check", lines)
    if sys.stdout:
        try:
            print("\n".join(lines))
        except (OSError, ValueError):
            pass
    return 0 if ok else 1


# --------------------------------------------------------------- Fenster
C = {"bg0": "#0f0d0a", "bg1": "#17130e", "bg2": "#241c13", "border": "#8c6b3d", "borderlt": "#c79e57",
     "accent": "#ffd147", "text": "#f2e8cf", "muted": "#a8987a", "ok": "#9cc98a", "bad": "#e8876b"}


def create_app(home, opener=None):
    """Fenster bauen (ohne mainloop); opener = Mock fuer Tests."""
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox, ttk

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.home, self.store, self.opener = pathlib.Path(home), Store(home), opener
            self.q, self.worker, self.stop, self.closing = queue.Queue(), None, threading.Event(), False
            self.run_lines = []
            self.title(APP)
            self.configure(bg=C["bg0"])
            self.resizable(False, False)
            try:
                self.iconbitmap(default=str(resource("graveyard_armory.ico")))
            except tk.TclError:
                pass
            self.k = self.winfo_fpixels("1i") / 96.0
            self._fonts()
            self._style()
            self.wow_var, self.region_var = tk.StringVar(), tk.StringVar(value="eu")
            self.cid_var, self.secret_var = tk.StringVar(), tk.StringVar()
            self.remember_var, self.schedule_var = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
            self.show_var = tk.BooleanVar(value=False)
            self._build()
            self._load()
            self.protocol("WM_DELETE_WINDOW", self.on_close)
            self.after(60, self._poll)
            self.after(200, self._sync_task)
            self.after(10, self._dark_titlebar)

        # ---------------------------------------------------------- Optik
        def _fonts(self):
            fam = set(tkfont.families(self))
            serif = next((f for f in ("Palatino Linotype", "Book Antiqua", "Georgia") if f in fam), "Times New Roman")
            self.f_title = tkfont.Font(family=serif, size=20, weight="bold")
            self.f_head = tkfont.Font(family=serif, size=11, weight="bold")
            self.f_body = tkfont.Font(family="Segoe UI", size=10)
            self.f_small = tkfont.Font(family="Segoe UI", size=9)
            self.f_bold = tkfont.Font(family="Segoe UI", size=10, weight="bold")
            self.f_btn = tkfont.Font(family=serif, size=12, weight="bold")
            self.f_link = tkfont.Font(family="Segoe UI", size=9, underline=True)

        def _style(self):
            st = ttk.Style(self)
            st.theme_use("clam")
            st.configure("G.Horizontal.TProgressbar", troughcolor=C["bg2"], background=C["borderlt"],
                         bordercolor=C["border"], lightcolor=C["borderlt"], darkcolor=C["border"],
                         thickness=int(10 * self.k))
            for w in ("TCheckbutton", "TRadiobutton"):
                st.configure(w, background=C["bg1"], foreground=C["text"], font=self.f_body,
                             indicatorbackground=C["bg2"], indicatorforeground=C["accent"],
                             upperbordercolor=C["border"], lowerbordercolor=C["border"], focuscolor=C["bg1"])
                st.map(w, background=[("active", C["bg1"])], foreground=[("disabled", C["muted"])],
                       indicatorbackground=[("pressed", C["bg2"]), ("selected", C["bg2"])])
            st.configure("Bare.TCheckbutton", background=C["bg0"])
            st.map("Bare.TCheckbutton", background=[("active", C["bg0"])])

        def _dark_titlebar(self):
            try:
                hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
                val = ctypes.c_int(1)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(val), ctypes.sizeof(val))
            except (AttributeError, OSError):
                pass

        def _label(self, parent, text="", fg="text", font=None, bg="bg1", anchor="w", **kw):
            return tk.Label(parent, text=text, fg=C[fg], bg=C[bg], font=font or self.f_body,
                            anchor=anchor, justify="left", **kw)

        def _entry(self, parent, var, **kw):
            return tk.Entry(parent, textvariable=var, bg=C["bg2"], fg=C["text"], insertbackground=C["accent"],
                            relief="flat", highlightthickness=1, highlightbackground=C["border"],
                            highlightcolor=C["borderlt"], disabledbackground=C["bg1"], font=self.f_body, **kw)

        def _button(self, parent, text, cmd, primary=False):
            if primary:
                return tk.Button(parent, text=text, command=cmd, bg=C["borderlt"], fg=C["bg0"], font=self.f_btn,
                                 activebackground=C["accent"], activeforeground=C["bg0"], relief="flat", bd=0,
                                 cursor="hand2", disabledforeground=C["bg2"], pady=int(7 * self.k))
            return tk.Button(parent, text=text, command=cmd, bg=C["bg2"], fg=C["text"], font=self.f_body,
                             activebackground=C["border"], activeforeground=C["text"], relief="flat", bd=0,
                             highlightthickness=1, highlightbackground=C["border"], cursor="hand2",
                             padx=int(12 * self.k), disabledforeground=C["muted"])

        def _section(self, title):
            outer = tk.Frame(self.body, bg=C["bg1"], highlightthickness=1, highlightbackground=C["border"])
            outer.pack(fill="x", pady=(0, int(10 * self.k)))
            self._label(outer, title.upper(), fg="borderlt", font=self.f_head).pack(
                fill="x", padx=int(12 * self.k), pady=(int(8 * self.k), 0))
            inner = tk.Frame(outer, bg=C["bg1"])
            inner.pack(fill="x", padx=int(12 * self.k), pady=(int(4 * self.k), int(10 * self.k)))
            return inner

        # ------------------------------------------------------------ Bau
        def _build(self):
            px = int(18 * self.k)
            wrap = int(560 * self.k)
            self.body = tk.Frame(self, bg=C["bg0"])
            self.body.pack(fill="both", padx=px, pady=(int(14 * self.k), px))

            head = tk.Frame(self.body, bg=C["bg0"])
            head.pack(fill="x")
            self._label(head, "GRAVEYARD ARMORY", fg="accent", font=self.f_title, bg="bg0").pack(side="left")
            self._label(head, "v" + VERSION, fg="muted", font=self.f_small, bg="bg0").pack(side="right", anchor="s")
            self._label(self.body, "Dress your guild's fallen in the gear the official Armory still remembers.",
                        fg="muted", bg="bg0").pack(fill="x")
            tk.Frame(self.body, bg=C["border"], height=1).pack(fill="x", pady=(int(8 * self.k), int(12 * self.k)))

            # WoW-Ordner
            s = self._section("World of Warcraft Classic Era")
            s.columnconfigure(0, weight=1)
            self.wow_entry = self._entry(s, self.wow_var)
            self.wow_entry.grid(row=0, column=0, sticky="ew", ipady=int(3 * self.k))
            self.wow_entry.bind("<FocusOut>", lambda e: self._wow_changed(self.wow_var.get()))
            self.wow_entry.bind("<Return>", lambda e: self._wow_changed(self.wow_var.get()))
            self.browse_btn = self._button(s, "Browse…", self._browse)
            self.browse_btn.grid(row=0, column=1, padx=(int(8 * self.k), 0), sticky="ns")
            self.wow_status = self._label(s, "", fg="muted", font=self.f_small, wraplength=wrap)
            self.wow_status.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(int(4 * self.k), 0))

            # API-Client
            s = self._section("Blizzard API client")
            s.columnconfigure(1, weight=1)
            self._label(s, "Region").grid(row=0, column=0, sticky="w", pady=int(2 * self.k))
            rf = tk.Frame(s, bg=C["bg1"])
            rf.grid(row=0, column=1, columnspan=2, sticky="w")
            self.region_btns = []
            for r in REGIONS:
                b = ttk.Radiobutton(rf, text=r.upper(), value=r, variable=self.region_var, command=self._save_prefs)
                b.pack(side="left", padx=(0, int(12 * self.k)))
                self.region_btns.append(b)
            self.region_hint = self._label(rf, "", fg="muted", font=self.f_small)
            self.region_hint.pack(side="left")
            self._label(s, "Client ID").grid(row=1, column=0, sticky="w", pady=int(3 * self.k))
            self.cid_entry = self._entry(s, self.cid_var)
            self.cid_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(int(10 * self.k), 0),
                                ipady=int(3 * self.k))
            self._label(s, "Client secret").grid(row=2, column=0, sticky="w", pady=int(3 * self.k))
            self.secret_entry = self._entry(s, self.secret_var, show="•")
            self.secret_entry.grid(row=2, column=1, sticky="ew", padx=(int(10 * self.k), 0), ipady=int(3 * self.k))
            ttk.Checkbutton(s, text="show", variable=self.show_var, command=self._toggle_show).grid(
                row=2, column=2, padx=(int(8 * self.k), 0))
            self.remember_btn = ttk.Checkbutton(s, text="Remember on this PC (encrypted for your Windows account)",
                                                variable=self.remember_var, command=self._remember_changed)
            self.remember_btn.grid(row=3, column=0, columnspan=3, sticky="w", pady=(int(6 * self.k), int(6 * self.k)))
            self.help = tk.Text(s, bg=C["bg1"], fg=C["muted"], font=self.f_small, wrap="word", bd=0,
                                highlightthickness=0, height=6, width=10, cursor="arrow", padx=0, pady=0,
                                spacing1=int(2 * self.k))
            self.help.grid(row=4, column=0, columnspan=3, sticky="ew")
            self.help.bind("<Configure>", lambda e: self.after_idle(self._fit_help))
            self._fill_help()

            # Automatik
            self.schedule_btn = ttk.Checkbutton(
                self.body, style="Bare.TCheckbutton", variable=self.schedule_var, command=self._schedule_changed,
                text="Update automatically every day (Windows Task Scheduler, 12:00 or at the next start)")
            self.schedule_btn.pack(fill="x", pady=(0, int(10 * self.k)))

            # Start + Fortschritt
            self.run_btn = self._button(self.body, "UPDATE ARMORY GEAR", self._run_clicked, primary=True)
            self.run_btn.pack(fill="x")
            pf = tk.Frame(self.body, bg=C["bg0"])
            pf.pack(fill="x", pady=(int(10 * self.k), 0))
            pf.columnconfigure(0, weight=1)
            self.bar = ttk.Progressbar(pf, style="G.Horizontal.TProgressbar", mode="determinate", maximum=1)
            self.bar.grid(row=0, column=0, sticky="ew")
            self.count = self._label(pf, "", fg="muted", font=self.f_small, bg="bg0", width=11, anchor="e")
            self.count.grid(row=0, column=1, sticky="e")
            self.now_label = self._label(self.body, "", fg="muted", font=self.f_small, bg="bg0")
            self.now_label.pack(fill="x", pady=(int(3 * self.k), 0))

            # Ergebnis
            self.result = self._label(self.body, "", fg="text", font=self.f_bold, bg="bg0", wraplength=wrap)
            self.result.pack(fill="x", pady=(int(8 * self.k), 0))
            self.detail = self._label(self.body, "", fg="muted", font=self.f_small, bg="bg0", wraplength=wrap)
            self.detail.pack(fill="x")
            self.hint = self._label(self.body, "", fg="accent", font=self.f_small, bg="bg0", wraplength=wrap)
            self.hint.pack(fill="x", pady=(int(2 * self.k), 0))

            foot = tk.Frame(self.body, bg=C["bg0"])
            foot.pack(fill="x", pady=(int(12 * self.k), 0))
            self.last = self._label(foot, "", fg="muted", font=self.f_small, bg="bg0", wraplength=int(470 * self.k))
            self.last.pack(side="left", fill="x", expand=True)
            log = self._label(foot, "Open log", fg="borderlt", font=self.f_link, bg="bg0", cursor="hand2")
            log.pack(side="right", anchor="n")
            log.bind("<Button-1>", lambda e: self._open_log())
            tk.Frame(self.body, bg=C["bg0"], width=wrap + int(14 * self.k), height=1).pack()  # feste Breite

        def _fill_help(self):
            t = self.help
            t.configure(state="normal")
            t.delete("1.0", "end")
            t.tag_configure("b", foreground=C["text"])
            t.tag_configure("link", foreground=C["accent"], underline=True)
            t.tag_bind("link", "<Button-1>", lambda e: webbrowser.open(CLIENTS_URL))
            t.tag_bind("link", "<Enter>", lambda e: t.configure(cursor="hand2"))
            t.tag_bind("link", "<Leave>", lambda e: t.configure(cursor="arrow"))
            parts = [("Every officer uses their own free API client:\n", "b"),
                     ("1. Open ", None), ("develop.battle.net/access/clients", "link"),
                     (", log in with your Battle.net account and accept the API Terms of Use.\n", None),
                     ("2. Click ", None), ("Create Client", "b"),
                     (": any name (e.g. Graveyard Armory), tick \"I do not have a service URL\", "
                      "describe the use in a few words. The redirect URL is not used.\n", None),
                     ("3. Copy ", None), ("Client ID", "b"), (" and ", None), ("Client secret", "b"),
                     (" into the fields above. Keep the secret to yourself.", None)]
            for text, tag in parts:
                t.insert("end", text, tag or ())
            t.configure(state="disabled")

        def _fit_help(self):
            # Hoehe = Anzahl umbrochener Zeilen bei der tatsaechlichen Breite
            try:
                if self.help.winfo_width() < 100:
                    return
                n = self.help.count("1.0", "end", "displaylines")
                n = n[0] if isinstance(n, tuple) else n
                if n and int(n) != int(self.help.cget("height")):
                    self.help.configure(height=int(n))
            except tk.TclError:
                pass

        # --------------------------------------------------------- Zustand
        def _load(self):
            st = self.store
            wow = normalize_wow(st.get("wow"))
            if not wow:
                found = find_wow()
                wow = found[0] if found else None
            self.wow_var.set(str(wow) if wow else "")
            self._refresh_wow(wow, save=False)
            region = st.get("region") or (detect_region(wow) if wow else None) or "eu"
            self.region_var.set(region)
            creds = st.credentials()
            if creds:
                self.cid_var.set(creds[0])
                self.secret_var.set(creds[1])
            self.remember_var.set(bool(creds) or st.get("remember", True))
            self._show_last()

        def _refresh_wow(self, wow, save=True):
            self.wow = wow
            hint = ""
            if wow:
                r = detect_region(wow)
                hint = f"(your WoW is set to {r.upper()})" if r else ""
            self.region_hint.configure(text=hint)
            if not wow:
                self._status(self.wow_status, "✗ No WoW Classic Era folder selected. Use Browse… and pick the "
                             "World of Warcraft folder or its _classic_era_ folder.", "bad")
                return
            paths = armory.find_savedvars(None, wow)
            if not paths:
                self._status(self.wow_status, "✗ " + NO_SV, "bad")
            else:
                try:
                    n = len(armory.load_graves(paths))
                    acc = f"{len(paths)} account" + ("s" if len(paths) != 1 else "")
                    self._status(self.wow_status, f"✓ Graveyard data found ({acc}) · {n} fallen without gear or gender",
                                 "ok")
                except (OSError, ValueError) as e:
                    self._status(self.wow_status, f"✗ Could not read Graveyard's saved data: {e}", "bad")
            if save:
                self._save_prefs()

        def _status(self, label, text, kind):
            label.configure(text=text, fg=C[kind] if kind in C else C["muted"])

        def _wow_changed(self, path):
            if self.worker:
                return
            wow = normalize_wow(path.strip().strip('"')) if path.strip() else None
            if wow and str(wow) != path:
                self.wow_var.set(str(wow))
            if wow is None and path.strip():
                self._status(self.wow_status, "✗ Not a WoW Classic Era folder. Pick the World of Warcraft "
                             "folder or its _classic_era_ folder.", "bad")
                self.wow = None
                return
            if (wow and str(wow)) != (self.wow and str(self.wow)):
                old = self.wow
                self._refresh_wow(wow)
                if wow and not old:
                    r = detect_region(wow)
                    if r:
                        self.region_var.set(r)

        def _browse(self):
            start = self.wow_var.get() or r"C:\Program Files (x86)"
            path = filedialog.askdirectory(parent=self, initialdir=start,
                                           title="Pick the World of Warcraft or _classic_era_ folder")
            if path:
                self.wow_var.set(os.path.normpath(path))
                self._wow_changed(os.path.normpath(path))

        def _toggle_show(self):
            self.secret_entry.configure(show="" if self.show_var.get() else "•")

        def _save_prefs(self):
            self.store.set(wow=str(self.wow) if self.wow else self.store.get("wow"),
                           region=self.region_var.get(), remember=bool(self.remember_var.get()))

        def _remember_changed(self):
            if not self.remember_var.get():
                self.store.forget_credentials()
                if self.schedule_var.get():
                    remove_task()
                    self.schedule_var.set(False)
                    self._status(self.hint, "Automatic updates turned off: they need the remembered API client.", "muted")
            self._save_prefs()

        def _creds(self):
            return self.cid_var.get().strip(), self.secret_var.get().strip()

        # ----------------------------------------------------- Aufgabe
        def _sync_task(self):
            try:
                tc = task_command()
            except OSError:
                tc = None
            self.schedule_var.set(bool(tc))
            if tc and getattr(sys, "frozen", False):
                cmd, args = self_command(self.home)
                if (tc[0].lower(), tc[1]) != (cmd.lower(), args):
                    try:
                        install_task(self.home)  # Programm wurde verschoben
                    except Problem:
                        pass

        def _schedule_changed(self):
            if not self.schedule_var.get():
                remove_task()
                self._status(self.hint, "Automatic updates turned off.", "muted")
                return
            cid, secret = self._creds()
            problem = None
            if not self.wow:
                problem = "Pick your WoW folder first."
            elif not (cid and secret):
                problem = "Enter Client ID and Client secret first: the scheduled update needs them."
            if problem:
                self.schedule_var.set(False)
                self._status(self.hint, problem, "bad")
                return
            try:
                self.remember_var.set(True)
                self.store.save_credentials(cid, secret)
                self._save_prefs()
                install_task(self.home)
            except (Problem, OSError) as e:
                self.schedule_var.set(False)
                self._status(self.hint, str(e), "bad")
                return
            self._status(self.hint, "Automatic updates on: every day at 12:00, or soon after you start the PC "
                         "if it was off. Results appear below and in the log.", "ok")

        # -------------------------------------------------------- Lauf
        def _run_clicked(self):
            if self.worker:
                self.stop.set()
                self.run_btn.configure(text="CANCELLING…", state="disabled")
                return
            self._wow_changed(self.wow_var.get())
            cid, secret = self._creds()
            problem = None
            if not self.wow:
                problem = "Pick your WoW Classic Era folder first."
            elif not armory.find_savedvars(None, self.wow):
                problem = NO_SV
            elif not (cid and secret):
                problem = "Enter your Client ID and Client secret (see the steps above)."
            if problem:
                self._status(self.result, problem, "bad")
                self.detail.configure(text="")
                self.hint.configure(text="")
                return
            self._save_prefs()
            self.stop.clear()
            self.run_lines = []
            self._set_busy(True)
            self._status(self.result, "Asking the Armory…", "text")
            self.detail.configure(text="")
            self.hint.configure(text="")
            self.bar.configure(value=0, maximum=1)
            args = (self.wow, self.region_var.get(), cid, secret)
            self.worker = threading.Thread(target=self._work, args=args, daemon=True)
            self.worker.start()

        def _work(self, wow, region, cid, secret):
            try:
                with RunLock(self.home):
                    res = run_import(wow, region, cid, secret, self.home,
                                     log=lambda s: self.q.put(("log", s)),
                                     progress=lambda i, n, st, g: self.q.put(("progress", i, n, st, g["name"])),
                                     stop=self.stop.is_set, opener=self.opener)
                self.q.put(("done", res, cid, secret))
            except Busy:
                self.q.put(("error", "Another update (the scheduled one?) is running right now. "
                                     "Try again in a minute."))
            except Problem as e:
                self.q.put(("error", str(e)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", f"Unexpected error: {type(e).__name__}: {e}"))

        def _set_busy(self, busy):
            state = "disabled" if busy else "normal"
            for w in (self.wow_entry, self.browse_btn, self.cid_entry, self.secret_entry):
                w.configure(state=state)
            for w in self.region_btns + [self.remember_btn, self.schedule_btn]:
                w.state(["disabled"] if busy else ["!disabled"])
            self.run_btn.configure(text="CANCEL" if busy else "UPDATE ARMORY GEAR", state="normal")

        def _poll(self):
            try:
                while True:
                    ev = self.q.get_nowait()
                    self._event(ev)
            except queue.Empty:
                pass
            if not (self.closing and self.worker is None):
                self.after(60, self._poll)

        def _event(self, ev):
            kind = ev[0]
            if kind == "progress":
                _, i, n, status, name = ev
                self.bar.configure(maximum=max(n, 1), value=i)
                self.count.configure(text=f"{i} / {n}")
                self.now_label.configure(text=f"{name}: {status}")
            elif kind == "log":
                self.run_lines.append(ev[1])
            elif kind == "done":
                _, res, cid, secret = ev
                self.worker = None
                if self.remember_var.get():
                    try:
                        self.store.save_credentials(cid, secret)
                    except OSError:
                        pass
                self._set_busy(False)
                text = result_line(res["stats"], res["calls"])
                self.run_lines += [text, summary_line(res)]
                append_log(self.home, "manual", self.run_lines)
                self.store.set(last_run={"at": int(time.time()), "ok": True, "mode": "manual", "text": text,
                                         "entries": res["entries"]})
                self.now_label.configure(text="Cancelled - results so far were saved." if res["cancelled"] else "")
                self._status(self.result, text, "text")
                self.detail.configure(text=f"{res['entries']} fallen with Armory gear, {res['sexes']} genders known "
                                      f"→ {res['data']}\n"
                                      "ok = new gear · notfound = not in the Armory · mismatch = the name now "
                                      "belongs to another character (both re-checked after 7 days)")
                first = res["first"]
                self._status(self.hint, "Now in game: restart WoW once so it finds the new Graveyard_Armory addon "
                             "(later updates only need /reload)." if first else
                             "Now in game: type /reload (the very first time: restart the game).", "accent")
                self._show_last()
                if self.closing:
                    self.destroy()
            elif kind == "error":
                self.worker = None
                self._set_busy(False)
                self.run_lines.append("ERROR: " + ev[1])
                append_log(self.home, "manual", self.run_lines)
                self.now_label.configure(text="")
                self._status(self.result, ev[1], "bad")
                if self.closing:
                    self.destroy()

        def _show_last(self):
            lr = self.store.get("last_run")
            if not isinstance(lr, dict) or not lr.get("at"):
                self.last.configure(text="")
                return
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(lr["at"]))
            mode = "automatic" if lr.get("mode") == "auto" else "manual"
            state = f"{lr.get('entries', 0)} with gear" if lr.get("ok") else "failed: " + str(lr.get("text", ""))
            self.last.configure(text=f"Last {mode} run: {when} · {state}")

        def _open_log(self):
            f = self.home / "armory.log"
            if f.exists():
                os.startfile(str(f))  # noqa: S606
            else:
                self._status(self.hint, "No log yet.", "muted")

        def on_close(self):
            if self.worker:
                if not messagebox.askyesno(APP, "An update is running. Cancel it and close?", parent=self):
                    return
                self.closing = True
                self.stop.set()
                self.run_btn.configure(text="CANCELLING…", state="disabled")
                return
            self.destroy()

        def destroy(self):
            for aid in self.tk.splitlist(self.tk.call("after", "info")):
                try:
                    self.after_cancel(aid)
                except tk.TclError:
                    pass
            super().destroy()

        def report_callback_exception(self, exc, val, tb):
            append_log(self.home, "error", traceback.format_exception(exc, val, tb)[-3:])
            messagebox.showerror(APP, f"Unexpected error: {val}", parent=self)

    return App()


def run_gui(home):
    create_app(home).mainloop()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="GraveyardArmory", description=APP)
    ap.add_argument("--auto", action="store_true", help="update without window (Task Scheduler)")
    ap.add_argument("--check", action="store_true", help="check the setup without contacting Blizzard")
    ap.add_argument("--home", help="settings folder (default %%APPDATA%%\\Graveyard Armory)")
    args = ap.parse_args(argv)
    home = pathlib.Path(args.home) if args.home else default_home()
    if args.check:
        return check(home)
    if args.auto:
        return auto_run(home)
    return run_gui(home)


if __name__ == "__main__":
    sys.exit(main())
