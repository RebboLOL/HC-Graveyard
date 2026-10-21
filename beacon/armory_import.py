"""Graveyard - armory_import: holt das Gear gefallener Chars ohne Ausruestung
aus dem offiziellen Arsenal (Blizzard Profile API, Classic Era) und schreibt es
als Datenaddon Graveyard_Armory, das Graveyard beim Login uebernimmt.

Ablauf
1. liest die Graveyard-SavedVariables (alle Accounts unter WTF\\Account oder --sv)
2. waehlt Graeber ohne Gear oder ohne Geschlecht (keine Demo-Graeber)
3. fragt je Char Profil + Ausruestung ab (namespace profile-classic1x-<region>)
   und uebernimmt nur, wenn Level, Klasse und Rasse zum Grab passen (sonst
   koennte der Name inzwischen einem neuen Char gehoeren)
4. schreibt AddOns\\Graveyard_Armory\\Data.lua (+ TOC beim ersten Lauf)

Zugangsdaten: eigener API-Client auf https://develop.battle.net/access/clients,
dann vor dem Aufruf setzen (nie in Dateien oder Skripte schreiben):
    $env:BLIZZARD_CLIENT_ID = "..."; $env:BLIZZARD_CLIENT_SECRET = "..."

Aufruf
    python armory_import.py --dry-run        Kandidaten zeigen, kein Netz
    python armory_import.py                  abfragen + Datenaddon schreiben
    python armory_import.py --limit 20       nur die ersten 20 (Test)
    python armory_import.py --recheck        verworfene Chars sofort neu pruefen
Danach im Spiel /reload (beim allerersten Mal: Spiel neu starten, damit der
Client den neuen Addon-Ordner kennt).

Als Modul (Graveyard Armory, companion\\graveyard_armory.py): load_graves,
Api, fetch_gear, collect_entries, load_cache/save_cache, write_addon. Die
SavedVariables liest ein eigener Lua-Datenleser (parse_lua_data) - kein lupa,
es wird kein Lua ausgefuehrt.
"""
import argparse
import base64
import json
import os
import pathlib
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

WOW = pathlib.Path(r"C:\Program Files (x86)\World of Warcraft\_classic_era_")
OUT_ADDON = WOW / "Interface" / "AddOns" / "Graveyard_Armory"
CACHE = pathlib.Path(__file__).resolve().parents[1] / "armory_cache.json"
LOCALES = {"eu": "en_GB", "us": "en_US", "kr": "ko_KR", "tw": "zh_TW"}
# API-Slot-Typen -> WoW-Inventarslots 1..19 (Reihenfolge des Gear-Codecs)
SLOTS = {
    "HEAD": 1, "NECK": 2, "SHOULDER": 3, "SHIRT": 4, "CHEST": 5, "WAIST": 6, "LEGS": 7,
    "FEET": 8, "WRIST": 9, "HANDS": 10, "FINGER_1": 11, "FINGER_2": 12, "TRINKET_1": 13,
    "TRINKET_2": 14, "BACK": 15, "MAIN_HAND": 16, "OFF_HAND": 17, "RANGED": 18, "TABARD": 19,
}
RETRY_NOT_FOUND = 7 * 86400  # 404 erst nach einer Woche erneut fragen
DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
STATS_KEYS = ("ok", "cached", "notfound", "mismatch", "noitems", "error")
SEX = {"MALE": 2, "FEMALE": 3}  # Arsenal gender.type -> WoW UnitSex (Graveyard genderId)


# ----------------------------------------------------------------- Codec
def enc36(n):
    n = int(n)
    if n <= 0:
        return "0"
    out = []
    while n > 0:
        out.append(DIGITS[n % 36])
        n //= 36
    return "".join(reversed(out))


def encode_gear(slots):
    """slots: {1..19: (id, ench, sfx)} -> Graveyard-Gear-String (DB.lua).

    Feld 20 (ausgeblendeter Helm/Umhang) haengt with_flags an.
    """
    parts = []
    for s in range(1, 20):
        it = slots.get(s)
        if not it:
            parts.append("")
            continue
        iid, ench, sfx = it
        enc = enc36(iid)
        if ench or sfx:
            enc += "." + (enc36(ench) if ench else "")
        if sfx:
            enc += "." + ("-" if sfx < 0 else "") + enc36(abs(sfx))
        parts.append(enc)
    return ";".join(parts)


# Helm/Umhang-Sichtbarkeit (Gear-Feld 20, G.GearHidden im Addon): /appearance
# fuehrt nur sichtbare Teile. Ausgeruestet (laut /equipment), aber dort nicht
# gelistet = ausgeblendet. Live bestaetigt am 21.10.2026 (tools/appearance_probe.py):
# Char mit ausgeblendetem Helm und Umhang ohne HEAD und BACK, Vergleichs-Char vollstaendig.
HIDE_SLOTS = (("HEAD", 1, "h"), ("BACK", 15, "c"))


def hidden_flags(gear, appearance):
    """Gear-String + Antwort von .../appearance -> "", "h", "c" oder "hc".
    Ohne verwertbare Antwort (oder ohne Items) "": gilt als sichtbar."""
    items = (appearance or {}).get("items") if isinstance(appearance, dict) else None
    if not isinstance(items, list) or not items:
        return ""
    shown = {((e.get("slot") or {}).get("type") or "").upper() for e in items if isinstance(e, dict)}
    parts = (gear or "").split(";")
    return "".join(f for st, slot, f in HIDE_SLOTS
                   if len(parts) >= slot and parts[slot - 1] and st not in shown)


def with_flags(gear, flags):
    """Gear-String auf 19 Slots normieren und Feld 20 setzen (leer = weglassen)."""
    parts = (gear or "").split(";")[:19]
    parts += [""] * (19 - len(parts))
    if flags:
        parts.append(flags)
    return ";".join(parts)


def appearance_matches(g, ap):
    """Gehoert die /appearance-Antwort noch zum Grab (Klasse, Rasse)?"""
    for key, field in (("classId", "playable_class"), ("raceId", "playable_race")):
        want, got = g.get(key), ((ap or {}).get(field) or {}).get("id")
        if want and got and int(want) != int(got):
            return False
    return True


def parse_equipment(js):
    """Antwort von .../equipment -> {slot: (id, ench, sfx)}."""
    slots = {}
    for e in js.get("equipped_items") or []:
        st = ((e.get("slot") or {}).get("type") or "").upper()
        slot = SLOTS.get(st)
        iid = (e.get("item") or {}).get("id")
        if not slot or not isinstance(iid, int):
            continue
        ench = 0
        for en in e.get("enchantments") or []:
            est = (en.get("enchantment_slot") or {})
            if (est.get("type") == "PERMANENT" or est.get("id") == 0) and isinstance(en.get("enchantment_id"), int):
                ench = en["enchantment_id"]
                break
        sfx = 0
        re_ = e.get("random_enchantment") or {}
        if isinstance(re_.get("id"), int):
            sfx = re_["id"]
        slots[slot] = (iid, ench, sfx)
    return slots


# --------------------------------------------------------------- Slugs
def realm_slug(name):
    return name.lower().replace("'", "").replace(" ", "-")


def name_slug(name):
    return urllib.parse.quote(name.lower())


# ------------------------------------------------------------ Lua-Daten
# Leser fuer SavedVariables: nur Daten (Tabellen, Strings, Zahlen, true/false/
# nil), so wie WoW sie schreibt. Fuehrt nichts aus und braucht kein lupa.
_LUA_TOKEN = re.compile(rb"""
    (?P<ws>\s+)
  | (?P<lcomment>--\[(?P<lceq>=*)\[.*?\](?P=lceq)\])
  | (?P<comment>--[^\n]*)
  | (?P<lstr>\[(?P<lseq>=*)\[.*?\](?P=lseq)\])
  | (?P<str>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
  | (?P<num>0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>[{}\[\]=,;-])
""", re.S | re.X)
_LUA_ESC = re.compile(rb"\\(\d{1,3}|x[0-9a-fA-F]{2}|u\{[0-9a-fA-F]+\}|z\s*|\r\n|\n\r|.)", re.S)
_LUA_ESC_CHAR = {b"n": b"\n", b"t": b"\t", b"r": b"\r", b"a": b"\a", b"b": b"\b", b"f": b"\f",
                 b"v": b"\v", b"\\": b"\\", b'"': b'"', b"'": b"'", b"\n": b"\n", b"\r": b"\n",
                 b"\r\n": b"\n", b"\n\r": b"\n"}


def _lua_unescape(m):
    e = m.group(1)
    if e[:1].isdigit():
        n = int(e)
        if n > 255:
            raise ValueError(f"Lua-Daten: ungueltige Escape-Sequenz \\{n}")
        return bytes([n])
    if e[:1] == b"x":
        return bytes([int(e[1:], 16)])
    if e[:1] == b"u":
        return chr(int(e[2:-1], 16)).encode("utf-8", "surrogatepass")
    if e[:1] == b"z":
        return b""
    if e in _LUA_ESC_CHAR:
        return _LUA_ESC_CHAR[e]
    raise ValueError(f"Lua-Daten: ungueltige Escape-Sequenz \\{e.decode('latin-1')}")


def parse_lua_data(src):
    """SavedVariables-Text (bytes/str) -> {Globalname: Wert}; Tabellen werden dicts."""
    if isinstance(src, str):
        src = src.encode("utf-8")
    toks, pos, n = [], 0, len(src)
    if src.startswith(b"\xef\xbb\xbf"):
        pos = 3
    while pos < n:
        m = _LUA_TOKEN.match(src, pos)
        if not m:
            raise ValueError(f"Lua-Daten: unerwartetes Zeichen bei Byte {pos}")
        pos = m.end()
        kind = m.lastgroup
        if kind in ("ws", "comment", "lcomment"):
            continue
        toks.append((kind, m.group(kind), m.start()))
    toks.append(("eof", b"", n))

    def fail(i, what):
        raise ValueError(f"Lua-Daten: {what} bei Byte {toks[i][2]}")

    def expect(i, op):
        if toks[i][1] != op or toks[i][0] != "op":
            fail(i, f"'{op.decode()}' erwartet")
        return i + 1

    def value(i):
        kind, tok, _ = toks[i]
        if kind == "op" and tok == b"{":
            return table(i + 1)
        if kind == "op" and tok == b"-" and toks[i + 1][0] == "num":
            v, j = value(i + 1)
            return -v, j
        if kind == "num":
            if tok[:2] in (b"0x", b"0X"):
                return int(tok, 16), i + 1
            if re.fullmatch(rb"\d+", tok):
                return int(tok), i + 1
            return float(tok), i + 1
        if kind == "str":
            return _LUA_ESC.sub(_lua_unescape, tok[1:-1]).decode("utf-8", "replace"), i + 1
        if kind == "lstr":
            body = tok[tok.index(b"[", 1) + 1:tok.rindex(b"]", 0, -1)]
            if body.startswith(b"\r\n"):
                body = body[2:]
            elif body[:1] in (b"\n", b"\r"):
                body = body[1:]
            return body.decode("utf-8", "replace"), i + 1
        if kind == "name" and tok in (b"true", b"false", b"nil"):
            return {b"true": True, b"false": False, b"nil": None}[tok], i + 1
        fail(i, "Wert erwartet")

    def table(i):
        t, arr = {}, 1
        while True:
            kind, tok, _ = toks[i]
            if kind == "op" and tok == b"}":
                return t, i + 1
            if kind == "op" and tok == b"[":
                key, i = value(i + 1)
                i = expect(i, b"]")
                i = expect(i, b"=")
                val, i = value(i)
                if key is None:
                    fail(i, "Schluessel nil")
            elif kind == "name" and toks[i + 1][1] == b"=" and toks[i + 1][0] == "op":
                key = tok.decode("ascii")
                val, i = value(i + 2)
            else:
                val, i = value(i)
                key, arr = arr, arr + 1
            if isinstance(key, float) and key.is_integer():
                key = int(key)
            if val is None:
                t.pop(key, None)
            else:
                t[key] = val
            kind, tok, _ = toks[i]
            if kind == "op" and tok in (b",", b";"):
                i += 1
            elif not (kind == "op" and tok == b"}"):
                fail(i, "',' oder '}' erwartet")

    out, i = {}, 0
    while toks[i][0] != "eof":
        if toks[i][0] != "name":
            fail(i, "Variablenname erwartet")
        name = toks[i][1].decode("ascii")
        i = expect(i + 1, b"=")
        val, i = value(i)
        out[name] = val
        if toks[i][0] == "op" and toks[i][1] == b";":
            i += 1
    return out


# ------------------------------------------------------------ SavedVars
def find_savedvars(explicit, wow=None):
    if explicit:
        return [pathlib.Path(explicit)]
    base = pathlib.Path(wow) if wow else WOW
    return sorted((base / "WTF" / "Account").glob("*/SavedVariables/Graveyard.lua"))


def load_graves(paths):
    """-> Liste {realmKey, gid, name, level, classId, raceId, diedAt}: Graeber
    ohne Gear ODER ohne Geschlecht. Graeber, die ihr Gear schon aus dem Arsenal
    haben (seit dem Login in den SavedVariables), brauchen weiter das
    Geschlecht (19.10.: sonst fielen genau diese 142 aus der Abfrage)."""
    out, seen = [], set()
    for p in paths:
        db = parse_lua_data(pathlib.Path(p).read_bytes()).get("GraveyardDB")
        if not isinstance(db, dict) or not isinstance(db.get("realms"), dict):
            continue
        for rk, r in db["realms"].items():
            graves = r.get("graves") if isinstance(r, dict) else None
            if not isinstance(graves, dict):
                continue
            for gid, g in graves.items():
                if not isinstance(g, dict):
                    continue
                has_gear = bool(g.get("gear"))
                has_sex = g.get("genderId") in (2, 3)
                if g.get("demo") or (has_gear and has_sex):
                    continue
                key = (rk, gid)
                if key in seen or not g.get("name"):
                    continue
                seen.add(key)
                out.append({"realmKey": rk, "gid": gid, "name": g.get("name"), "level": g.get("level"),
                            "classId": g.get("classId"), "raceId": g.get("raceId"), "diedAt": g.get("diedAt")})
    return out


def load_guild(paths, min_level=20, max_days=60):
    """-> Liste {realmKey, name, level, days}: lebende Gildies aus
    realm.guildList (Graveyard M20) fuer den Arsenal-Beacon. Ohne Chars unter
    min_level und Chars, die laenger als max_days offline sind."""
    out, seen = [], set()
    for p in paths:
        db = parse_lua_data(pathlib.Path(p).read_bytes()).get("GraveyardDB")
        if not isinstance(db, dict) or not isinstance(db.get("realms"), dict):
            continue
        for rk, r in db["realms"].items():
            gl = r.get("guildList") if isinstance(r, dict) else None
            members = gl.get("members") if isinstance(gl, dict) else None
            if not isinstance(members, dict):
                continue
            for key, m in members.items():
                if not isinstance(m, dict) or not m.get("name"):
                    continue
                if (m.get("level") or 0) < min_level or (m.get("days") or 0) > max_days:
                    continue
                if (rk, key) in seen:
                    continue
                seen.add((rk, key))
                out.append({"realmKey": rk, "name": m["name"], "level": m.get("level"), "days": m.get("days") or 0})
    return out


# ---------------------------------------------------------------- API
class TokenError(SystemExit):
    """Kein Zugriffstoken (Zugangsdaten oder Netz). detail = HTTP-Code oder Grund."""

    def __init__(self, detail):
        super().__init__(f"Token-Abruf fehlgeschlagen ({detail}) - Client-ID/Secret und Netz pruefen")
        self.detail = detail


class Api:
    def __init__(self, region, client_id, secret, opener=None):
        self.region = region
        self.locale = LOCALES[region]
        self.cid, self.secret = client_id, secret
        self.token = None
        self.open = opener or urllib.request.urlopen
        self.calls = 0

    def _token(self):
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        auth = base64.b64encode(f"{self.cid}:{self.secret}".encode()).decode()
        last = "?"
        for url in ("https://oauth.battle.net/token", f"https://{self.region}.battle.net/oauth/token"):
            req = urllib.request.Request(url, data=body, headers={"Authorization": "Basic " + auth})
            try:
                with self.open(req, timeout=20) as r:
                    self.token = json.loads(r.read().decode())["access_token"]
                    return
            except urllib.error.HTTPError as e:
                last = e.code
            except urllib.error.URLError as e:
                last = e.reason
        raise TokenError(last)

    def get(self, path):
        """-> (status, json|None)"""
        if not self.token:
            self._token()
        q = urllib.parse.urlencode({"namespace": f"profile-classic1x-{self.region}", "locale": self.locale})
        url = f"https://{self.region}.api.blizzard.com{path}?{q}"
        for attempt in range(4):
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + self.token})
            self.calls += 1
            try:
                with self.open(req, timeout=20) as r:
                    return r.status, json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    self._token()
                    continue
                if e.code == 429:
                    time.sleep(2 + attempt * 3)
                    continue
                return e.code, None
            except urllib.error.URLError:
                time.sleep(2)
        return 0, None


def check_identity(grave, prof):
    """Gehoert das Arsenal-Profil zum Grab? (gleiches Level, Klasse, Rasse)"""
    if not prof:
        return "no profile"
    lvl = prof.get("level")
    cls = (prof.get("character_class") or {}).get("id")
    race = (prof.get("race") or {}).get("id")
    if grave.get("classId") and cls and int(grave["classId"]) != cls:
        return f"class {cls} != {grave['classId']}"
    if grave.get("raceId") and race and int(grave["raceId"]) != race:
        return f"race {race} != {grave['raceId']}"
    # Höheres Level = anderer Char unter demselben Namen (ein toter HC-Char
    # steigt nicht mehr auf). Niedrigeres Level: entweder ein älterer
    # Arsenal-Stand des Gefallenen oder ein Neuer mit dem Namen. Ein Neuer kann
    # erst nach dem Tod angelegt werden -> letzter Login vor dem Tod = derselbe
    # Char (Lauf 19.10.: 13 von 71 Verwerfungen waren "Level tiefer").
    if grave.get("level") and lvl:
        gl = int(grave["level"])
        if lvl > gl:
            return f"level {lvl} vs grave {gl}"
        if lvl < gl - 2:
            last = (prof.get("last_login_timestamp") or 0) / 1000
            died = grave.get("diedAt") or 0
            if not (last and died and last <= died + 600):
                return f"level {lvl} vs grave {gl}, login after death"
    return None


# ------------------------------------------------------------ Abfrage
def load_cache(path):
    path = pathlib.Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_cache(path, cache):
    path = pathlib.Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def profile_sex(prof):
    """Arsenal-Profil -> 2 (maennlich) / 3 (weiblich) / None."""
    return SEX.get(str(((prof or {}).get("gender") or {}).get("type") or "").upper())


def fetch_gear(graves, api, cache, now, recheck=False, log=print, progress=None, stop=None):
    """Fragt die Graeber im Arsenal ab und traegt die Ergebnisse in cache ein
    (Gear + Geschlecht). progress(i, n, status, grave) nach jedem Grab;
    stop() -> True bricht ab. -> stats {ok, cached, notfound, mismatch, noitems, error}"""
    stats = dict.fromkeys(STATS_KEYS, 0)
    for i, g in enumerate(graves, 1):
        if stop and stop():
            break
        key = f"{g['realmKey']}|{g['gid']}"
        c = cache.get(key)
        realm = realm_slug(g["realmKey"].rsplit(":", 1)[0])
        base = f"/profile/wow/character/{realm}/{name_slug(g['name'])}"
        rc = recheck and c and c.get("status") == "mismatch"
        if not rc and c and c.get("gear") and "sex" not in c:
            # Treffer aus Laeufen vor dem Geschlechts-Feld: nur das Profil nachholen
            st, prof = api.get(base)
            c["sex"] = profile_sex(prof) if st == 200 and not check_identity(g, prof) else None
            time.sleep(0.05)
        if not rc and c and c.get("gear") and not c.get("app"):
            # Treffer aus Laeufen vor der Sichtbarkeit: Helm/Umhang einmalig nachholen
            st, ap = api.get(base + "/appearance")
            if st == 200 and ap and appearance_matches(g, ap):
                c["gear"] = with_flags(c["gear"], hidden_flags(c["gear"], ap))
            if st in (200, 404):
                c["app"] = 1
            time.sleep(0.05)
        if not rc and c and (c.get("gear") or (c.get("status") in ("notfound", "mismatch")
                                               and now - c.get("at", 0) < RETRY_NOT_FOUND)):
            stats["cached"] += 1
            if progress:
                progress(i, len(graves), "cached", g)
            continue
        st, prof = api.get(base)
        if st == 404:
            cache[key] = {"status": "notfound", "at": now}
            status = "notfound"
        elif st != 200:
            status = "error"
            log(f"  {g['name']}: HTTP {st}")
        else:
            why = check_identity(g, prof)
            if why:
                cache[key] = {"status": "mismatch", "why": why, "at": now, "lvl": prof.get("level"),
                              "lastLogin": int((prof.get("last_login_timestamp") or 0) / 1000)}
                status = "mismatch"
            else:
                st2, eq = api.get(base + "/equipment")
                slots = parse_equipment(eq) if st2 == 200 and eq else {}
                seen = int((prof.get("last_login_timestamp") or now * 1000) / 1000)
                sex = profile_sex(prof)
                if slots:
                    gear = encode_gear(slots)
                    st3, ap = api.get(base + "/appearance")
                    ok3 = st3 == 200 and appearance_matches(g, ap)
                    gear = with_flags(gear, hidden_flags(gear, ap if ok3 else None))
                    cache[key] = {"status": "ok", "gear": gear, "seenAt": seen, "at": now,
                                  "sex": sex, "app": 1 if st3 in (200, 404) else 0}
                    status = "ok"
                else:
                    status = "noitems" if st2 == 200 else "notfound"
                    cache[key] = {"status": status, "at": now, "seenAt": seen, "sex": sex}
        stats[status] += 1
        time.sleep(0.05)
        if progress:
            progress(i, len(graves), status, g)
    return stats


LIVING_TTL = 3 * 86400


def living_key(m):
    return f"living|{m['realmKey']}|{m['name'].lower()}"


def fetch_living(members, api, cache, now, log=print, progress=None, stop=None):
    """Gear lebender Gildies (Arsenal-Stand) fuer das Gilden-Gedaechtnis.
    Je Char hoechstens alle 3 Tage. -> stats wie fetch_gear"""
    stats = dict.fromkeys(STATS_KEYS, 0)
    for i, m in enumerate(members, 1):
        if stop and stop():
            break
        key = living_key(m)
        c = cache.get(key)
        if c and now - c.get("at", 0) < LIVING_TTL:
            stats["cached"] += 1
            if progress:
                progress(i, len(members), "cached", m)
            continue
        realm = realm_slug(m["realmKey"].rsplit(":", 1)[0])
        base = f"/profile/wow/character/{realm}/{name_slug(m['name'])}"
        st, prof = api.get(base)
        if st == 404:
            cache[key] = {"status": "notfound", "at": now}
            status = "notfound"
        elif st != 200 or not prof:
            status = "error"
            log(f"  {m['name']}: HTTP {st}")
        else:
            st2, eq = api.get(base + "/equipment")
            slots = parse_equipment(eq) if st2 == 200 and eq else {}
            seen = int((prof.get("last_login_timestamp") or now * 1000) / 1000)
            guild = (prof.get("guild") or {}).get("name")
            gear = ""
            if slots:
                gear = encode_gear(slots)
                st3, ap = api.get(base + "/appearance")
                gear = with_flags(gear, hidden_flags(gear, ap if st3 == 200 else None))
            cache[key] = {"status": "ok" if slots else "noitems", "at": now, "seenAt": seen,
                          "gear": gear, "sex": profile_sex(prof),
                          "level": prof.get("level"), "classId": (prof.get("character_class") or {}).get("id"),
                          "raceId": (prof.get("race") or {}).get("id"), "guild": guild}
            status = "ok" if slots else "noitems"
        stats[status] += 1
        time.sleep(0.05)
        if progress:
            progress(i, len(members), status, m)
    return stats


def collect_living(members, cache):
    out = []
    for m in members:
        c = cache.get(living_key(m))
        if c and c.get("gear"):
            out.append(dict(m, gear=c["gear"], seenAt=c.get("seenAt", 0), sex=c.get("sex"),
                            level=c.get("level") or m.get("level"), classId=c.get("classId"),
                            raceId=c.get("raceId"), guild=c.get("guild")))
    return out


def collect_entries(all_graves, cache, now):
    """Alle bekannten Treffer, auch aus frueheren Laeufen (Cache): Gear und/oder
    Geschlecht (Deathlog kennt kein Geschlecht, Graveyard fuellt es damit auf)."""
    entries = []
    for g in all_graves:
        c = cache.get(f"{g['realmKey']}|{g['gid']}")
        if c and (c.get("gear") or c.get("sex") in (2, 3)):
            entries.append(dict(g, gear=c.get("gear") or "", seenAt=c.get("seenAt", now), sex=c.get("sex")))
    return entries


# --------------------------------------------------------------- Ausgabe
def lua_str(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'


def write_addon(entries, region, now, out_addon=None, generator="armory_import.py", living=None):
    out_addon = pathlib.Path(out_addon) if out_addon else OUT_ADDON
    out_addon.mkdir(parents=True, exist_ok=True)
    toc = out_addon / "Graveyard_Armory.toc"
    if not toc.exists():
        toc.write_text("## Interface: 11509\n## Title: Graveyard Armory Data\n"
                       "## Notes: Equipment of fallen characters from the official Armory, read by Graveyard.\n"
                       "## Author: Rebo\n## Version: 1\n\nData.lua\n", encoding="utf-8", newline="\n")
    data = out_addon / "Data.lua"
    if data.exists():
        day = time.strftime("%Y%m%d")
        shutil.copy2(data, out_addon / f"Data_backup_{day}.lua")
    lines = [f"-- Graveyard Armory Data: generated by {generator}, do not edit.",
             f"GraveyardArmoryData = {{ generated = {now}, region = {lua_str(region)}, realms = {{"]
    by_realm = {}
    for e in entries:
        by_realm.setdefault(e["realmKey"], []).append(e)
    for rk in sorted(by_realm):
        lines.append(f"  [{lua_str(rk)}] = {{")
        for e in sorted(by_realm[rk], key=lambda x: x["gid"]):
            sex = f", sex = {e['sex']}" if e.get("sex") in (2, 3) else ""
            lines.append(f"    [{lua_str(e['gid'])}] = {{ name = {lua_str(e['name'])}, diedAt = {int(e['diedAt'] or 0)}, "
                         f"gear = {lua_str(e['gear'])}, seenAt = {int(e['seenAt'])}{sex} }},")
        lines.append("  },")
    lines.append("},")
    # lebende Gildies (Gilden-Gedaechtnis, Graveyard M20)
    lines.append("living = {")
    by_realm = {}
    for e in living or []:
        by_realm.setdefault(e["realmKey"], []).append(e)
    for rk in sorted(by_realm):
        lines.append(f"  [{lua_str(rk)}] = {{")
        for e in sorted(by_realm[rk], key=lambda x: x["name"].lower()):
            f = [f"name = {lua_str(e['name'])}", f"seenAt = {int(e['seenAt'] or 0)}"]
            for k in ("level", "classId", "raceId", "sex"):
                if e.get(k):
                    f.append(f"{k} = {int(e[k])}")
            if e.get("guild"):
                f.append(f"guild = {lua_str(e['guild'])}")
            f.append(f"gear = {lua_str(e['gear'])}")
            lines.append(f"    [{lua_str(e['name'].lower())}] = {{ " + ", ".join(f) + " },")
        lines.append("  },")
    lines.append("} }")
    data.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return data


# ------------------------------------------------------------------ main
def _cli_progress(i, n, status, grave):
    if status != "cached" and i % 25 == 0:
        print(f"  {i}/{n} ...")


def main(argv=None, opener=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", default="eu", choices=sorted(LOCALES))
    ap.add_argument("--sv", help="Pfad zu Graveyard.lua (Standard: alle Accounts)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-living", action="store_true",
                    help="Gear lebender Gildies nicht abfragen (Gilden-Gedaechtnis)")
    ap.add_argument("--recheck", action="store_true",
                    help="verworfene Chars (mismatch) sofort neu pruefen statt nach 7 Tagen")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    all_graves = load_graves(find_savedvars(args.sv))
    print(f"{len(all_graves)} Graeber ohne Gear oder Geschlecht gefunden")
    graves = all_graves[:args.limit] if args.limit else all_graves
    if args.dry_run:
        for g in graves[:15]:
            realm = g["realmKey"].rsplit(":", 1)[0]
            print(f"  {g['name']:14} L{g['level']}  -> /profile/wow/character/{realm_slug(realm)}/{name_slug(g['name'])}/equipment")
        if len(graves) > 15:
            print(f"  ... und {len(graves) - 15} weitere")
        return 0

    cid, secret = os.environ.get("BLIZZARD_CLIENT_ID"), os.environ.get("BLIZZARD_CLIENT_SECRET")
    if not (cid and secret):
        print("BLIZZARD_CLIENT_ID / BLIZZARD_CLIENT_SECRET fehlen (siehe Kopf dieses Skripts).")
        return 2
    api = Api(args.region, cid, secret, opener)
    cache = load_cache(CACHE)
    now = int(time.time())
    stats = fetch_gear(graves, api, cache, now, recheck=args.recheck, progress=_cli_progress)
    members = [] if args.no_living else load_guild(find_savedvars(args.sv))
    lstats = fetch_living(members, api, cache, now) if members else {}
    save_cache(CACHE, cache)

    entries = collect_entries(all_graves, cache, now)
    living = collect_living(members, cache)
    out = write_addon(entries, args.region, now, living=living)
    if members:
        print(f"Lebende Gildies: {len(members)} geprueft, {len(living)} mit Gear ({lstats})")
    print(f"Ergebnis: {stats} - API-Aufrufe {api.calls}")
    print(f"{sum(1 for e in entries if e['gear'])} Graeber mit Arsenal-Gear -> {out}")
    print(f"Geschlecht bekannt: {sum(1 for e in entries if e.get('sex'))}")
    print("Im Spiel: /reload (beim ersten Mal das Spiel neu starten).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
