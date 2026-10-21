# HC Graveyard — Armory Beacon

**[Graveyard](https://www.curseforge.com/wow/addons/hc-graveyard)** is a World of Warcraft Classic addon that turns your guild's fallen Hardcore characters into a walkable 3D memorial: their race, their gear, their killer, their last words.

This repository hosts the **Graveyard Armory Beacon** (`GraveyardArmory.exe`), a small Windows companion app. It looks your guild up in the official Blizzard Armory and hands the result to the addon, so the fallen wear the gear they really died in — even guildmates who never installed Graveyard.

**[⬇ Download the latest beacon](https://github.com/RebboLOL/HC-Graveyard/releases/latest)**

---

## What the beacon does

| | |
|---|---|
| **Fallen guildmates** | Looks up every fallen character that has no gear yet and saves the gear it wore (and its gender, which the death log does not record). A helm or cloak the player had hidden stays hidden on the spirit. |
| **Living guildmates** | Saves the current Armory gear of every guildmate from level 20, at most every three days. Graveyard keeps it as the *guild memory*: should one of them fall later, their grave wears that gear. |
| **Your guild** | Graveyard reads the result at login and shares it with every guild member who uses Graveyard. **One beacon per guild is enough**, and the guild keeps passing the data on while the beacon is offline. |

The beacon only talks to Blizzard. It reads Graveyard's saved data and writes one small data addon (`Graveyard_Armory`); it never changes the game or your characters.

## Set it up (about five minutes)

### 1. Install Graveyard and log in once

Install [Graveyard](https://www.curseforge.com/wow/addons/hc-graveyard), log in with a character **in your guild**, wait a minute and type `/reload` (or log out). WoW then saves Graveyard's data, including the list of your guildmates the beacon needs.

### 2. Create your free Blizzard API client

The beacon uses your own key for the Blizzard API. It takes two minutes:

1. Open **[develop.battle.net/access/clients](https://develop.battle.net/access/clients)** and log in with your Battle.net account. Accept the API Terms of Use if asked.
2. Click **Create Client**. Any name works (e.g. *Graveyard Armory*). Tick *I do not have a service URL* and describe the use in a few words (e.g. *reads my guild's characters for a WoW addon*).
3. Keep the page open: you need the **Client ID** and the **Client secret**.

Every user brings their own client; the beacon never ships with one. Keep your secret to yourself.

### 3. Download and start the beacon

1. Download `GraveyardArmory.exe` from the **[latest release](https://github.com/RebboLOL/HC-Graveyard/releases/latest)** and put it anywhere, e.g. `Documents\Graveyard Armory`. There is nothing to install.
2. Start it. Windows may show **"Windows protected your PC"** because the exe is not code-signed: click *More info* → *Run anyway*. (You can compare the checksum with the release page: `Get-FileHash .\GraveyardArmory.exe` in PowerShell.)
3. The beacon finds your WoW folder by itself. If not, click **Browse…** and choose the `World of Warcraft` or `_classic_era_` folder.
4. Check the **region** (preset from your WoW settings) and paste **Client ID** and **Client secret**.
5. Tick **Remember** (encrypted for your Windows account) and **Update automatically every day**.
6. Click **Update Armory gear** and wait for the result line.

### 4. Back in the game

- The **very first time, restart WoW** and make sure *Graveyard Armory Data* is enabled in the AddOns list. After that a `/reload` is enough.
- In the chat you see lines like *"142 fallen dressed in their Armory gear"* and *"Guild memory: Armory gear of 37 guildmates updated"*.
- Your guild receives the data automatically while you are online. In Graveyard, *Settings → Armory* (or `/gy beacon`) shows this guide, the download link and how much Armory data is on your PC.

## Provide it for your guild

- **One member runs the beacon** — ideally an officer who is on a PC every few days. Everyone else just uses Graveyard.
- **How the data travels:** the beacon updates once a day (at 12:00, or soon after the PC starts). When the beacon user logs in, Graveyard imports the update and sends it to the guild. Guildmates who are online get it at once; whoever logs in later catches up from anyone online. The beacon user does **not** need to be online every day.
- **More than one beacon** is fine: Graveyard always keeps the newest gear it has seen.
- **No beacon?** Graveyard still works: every member running it quietly records the gear of guildmates they meet (out of combat, at most every six hours) and shares it with the guild.

## The result line

| Status | Meaning |
|---|---|
| ok | gear found and saved (gender too) |
| cached | known from an earlier run |
| notfound | not (or no longer) in the Armory, checked again after 7 days |
| mismatch | the name now belongs to another character (different class, race or a higher level), skipped on purpose and checked again after 7 days |
| noitems | character found, but nothing equipped (gender is still saved) |
| error | Blizzard answered with an error, retried next run |

Deleted characters disappear from the Armory, so expect roughly a third of the fallen to be found. The first run with a few hundred characters takes a few minutes; later runs only ask for what is new.

## Supported game versions

- **WoW Classic Era** realms, including Hardcore and Season of Discovery.
- Not supported (yet): Anniversary realms and WoW Forever use a different Armory. Graveyard itself also runs on WoW Forever and collects gear there by inspecting guildmates.

## Privacy and security

- Client ID and secret are encrypted with Windows DPAPI for your Windows account and stored in `%APPDATA%\Graveyard Armory\settings.json`. They are never written in plain text and never logged. Untick **Remember** to delete them.
- The beacon only talks to Blizzard: `oauth.battle.net` (login with your client) and `<region>.api.blizzard.com` (character profile and equipment). No other servers, no telemetry.
- It reads `WTF\Account\*\SavedVariables\Graveyard.lua` (parsed as data, nothing is executed) and writes only to `Interface\AddOns\Graveyard_Armory\`. The previous data file is kept as a daily backup (last 14).
- The source code is in [`beacon/`](beacon/).

## Files

| Where | What |
|---|---|
| `%APPDATA%\Graveyard Armory\` | `settings.json`, `armory_cache_<region>.json`, `armory.log` |
| `<WoW>\_classic_era_\Interface\AddOns\Graveyard_Armory\` | `Graveyard_Armory.toc`, `Data.lua` |

## Troubleshooting

- **"Blizzard did not accept the API client":** copy Client ID and secret again; make sure there are no spaces.
- **"No Graveyard data in this folder yet":** enable Graveyard, log in, then type `/reload` or log out so WoW saves its data.
- **No living guildmates in the result:** log in once with a character in your guild and `/reload`, so Graveyard can save the guild list.
- **Nothing changes in the game:** `/reload`. After the first run restart WoW and check that *Graveyard Armory Data* is enabled in the AddOns list.
- **Check your setup:** `GraveyardArmory.exe --check` writes a short report to the log (click *Open log* in the beacon).

## Command line

| Command | Does |
|---|---|
| `GraveyardArmory.exe` | opens the window |
| `GraveyardArmory.exe --auto` | updates without a window (what the daily task runs) |
| `GraveyardArmory.exe --check` | checks the setup without contacting Blizzard |

## Uninstall

Untick the daily update (or run `schtasks /Delete /TN "Graveyard Armory" /F`), then delete the exe, the folder `%APPDATA%\Graveyard Armory` and the addon folder `Graveyard_Armory`.

## License

MIT, Copyright (c) 2026 Rebo. Not affiliated with or endorsed by Blizzard Entertainment. World of Warcraft and Battle.net are trademarks of Blizzard Entertainment, Inc.
