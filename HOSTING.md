# Hosting and backups

## 1. Running it on a computer in the building (recommended first)

You need a computer that stays on during work hours, is on the shop wifi (or plugged into the same network), and has Python 3.10 or newer installed (python.org, tick "Add Python to PATH" when installing).

1. Copy the `pallet-tracker` folder onto that computer.
2. Windows: double-click `start-tracker.bat`. Mac/Linux: run `./start-tracker.sh`.
   The first time, it sets itself up (a minute or two). After that it starts straight away.
3. On that computer, open http://localhost:5000 and create the first login.
4. Leave the black window open. Closing it stops the app.

### Letting phones and other computers connect
You do not need access to the router for this to work. Phones just need the computer's current address.

1. When `start-tracker.bat` starts, its window prints the address to use, for example `http://192.168.1.50:5000`. The **Connect** link at the bottom of the app shows the same thing and can be printed. Open it on the tracker computer.
2. Type that address into the phone's browser (the phone must be on the **same wifi or network** as the computer) and bookmark it.
3. Windows will ask once whether to allow the program through the firewall. Allow it on **private** networks.
4. In the computer's power settings, set sleep to "Never" while plugged in, or the app disappears when it sleeps.

**If the address changes:** most networks hand a computer the same address again each time, especially if it stays switched on, but they are allowed to change it. If a bookmark stops working, open the Connect page (or look at the window) on the tracker computer and use the new address.

**Making the address permanent without touching the router yourself:**
- Ask whoever looks after the building's network to reserve an address for this computer. It takes them a minute. They need the computer's name and its physical (MAC) address; both are on the Connect page's instructions. On Windows, `ipconfig /all` shows the physical address.
- Setting a fixed address on the computer itself is possible, but without knowing the router's settings it can clash with another device or not work, so only do that with the network owner's agreement.

**The computer's name:** `http://COMPUTERNAME:5000` sometimes works as a bookmark that survives address changes. It works on many Windows networks and computers but often not on phones, so treat it as a bonus.

**If phones cannot connect even with the right address:** the wifi may keep devices apart (guest wifi, "client isolation"). That can only be fixed in the router. If it is not an option, the choice is a different network for the phones, or hosting the app online (section 3).

### Starting by itself when the computer turns on
- Simplest (Windows): press Win+R, type `shell:startup`, and put a shortcut to `start-tracker.bat` in the folder that opens. It starts when someone logs in.
- Starting without anyone logging in: Windows Task Scheduler, "Create Basic Task", trigger "When the computer starts", action "Start a program" pointing at `start-tracker.bat`, and set "Start in" to the pallet-tracker folder.

### Security on the shop wifi
The app uses plain http inside your building, so passwords travel unencrypted over the wifi. That is normal for a small internal tool, but use a password-protected (WPA2/WPA3) wifi network, don't share the address outside the building, and **don't set up port forwarding** on the router to reach it from the internet.

## 2. Backups

- **Automatic:** the first visit each day makes a backup, and the newest 60 are kept. The app has to be visited for this to happen, which is the case on any working day.
- **Manual:** open **Backups** (footer link) and press "Back up now". Every backup can be downloaded from that page.
- **Where they are:** `instance/backups/`, on the same computer. If that computer breaks, so do they, so also do one of these:
  - Download a backup from the Backups page now and then and keep it on a USB stick or another computer.
  - Or make the app copy each backup automatically to another folder (USB drive, shared network folder, or a OneDrive/Dropbox folder). In `start-tracker.bat`, remove `rem ` from the line `rem set TRACKER_BACKUP_COPY_DIR=D:\TrackerBackups` and change the path. The Backups page shows the folder when it's set, and tells you if a copy failed.

### Restoring a backup
1. Stop the app (close its window).
2. In the `instance` folder, rename `tracker.db` to `tracker.db.old`. Delete `tracker.db-wal` and `tracker.db-shm` if they are there.
3. Copy the backup you want (from `instance/backups/` or your USB stick) into `instance`, and rename the copy to `tracker.db`.
4. Start the app. Check that your data looks right. Keep `tracker.db.old` until you are sure.

### Moving to a different computer
Install Python there, copy the whole `pallet-tracker` folder **including `instance`** (stop the app first), and start it. Everything comes with it.

## 3. Moving to a free cloud host later

The app is written so this is mostly a matter of settings, but check these first:

- **Disk that survives:** the data lives in one SQLite file. The host must give you a disk that is kept between restarts. Hosts that wipe their disk on every restart will lose your data. Check this before choosing, and check the host's current free-plan limits, because they change.
- **Python support:** the host needs to run a Flask app. `wsgi.py` is the entry point (the object is called `application`). `requirements.txt` lists what to install. Hosts that start a command instead can use `python serve.py`.
- **Settings** are environment variables:

| Variable | What it does | Default |
|---|---|---|
| `TRACKER_DATA_DIR` | Folder for the database, backups, and secret key. Point it at the disk that is kept. | `instance/` next to the app |
| `TRACKER_SECRET_KEY` | Secret used to sign logins. Set a long random value on a shared host. | created automatically on first run |
| `TRACKER_COOKIE_SECURE` | Set to `1` when the site is served over https. | off |
| `TRACKER_BACKUP_COPY_DIR` | Extra folder every backup is copied into. | none |
| `TRACKER_BACKUP_KEEP` | How many backups to keep. | 60 |
| `TRACKER_HOST`, `TRACKER_PORT` | Address and port for `serve.py`. | 0.0.0.0 and 5000 |

- **Moving your data:** stop the old copy, take a backup file, and place it as `tracker.db` inside the new host's data folder (see "Restoring a backup").
- **Before it is reachable from the internet:** the app does not yet have protection against forged form submissions (CSRF tokens), and it has no login-attempt limits. That is acceptable on the shop wifi but not on the open internet. Ask me to add both before moving it to a public host.
- `/health` answers "ok" without a login, for host monitoring.

## 4. Quick checks if something is wrong
- **Phones can't connect:** is the computer on and the window open? Is it the current address (check the window or the Connect page)? Is the phone on the same wifi? Did the firewall prompt get denied? (Windows Defender Firewall > allow an app > Python, private networks.)
- **"Address already in use":** another copy is running. Close the other window, or set a different `TRACKER_PORT`.
- **Forgot the only password:** on the computer run `venv\Scripts\flask --app tracker create-user NEWNAME` (Windows) or `venv/bin/flask --app tracker create-user NEWNAME`, then log in with that.
