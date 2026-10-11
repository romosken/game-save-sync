# Game Save Sync

Game Save Sync keeps emulator and offline game saves aligned across your PCs. Each game or emulator is a profile: a linked application plus one or more save folders. Launching that application through the tool downloads newer cloud saves first, starts the game, and uploads newer local saves when the game closes. A manual **Sync all** button does the same two-way sync without starting anything.

Replaced files are copied into a `.backup` folder. Saves are never deleted because the other side does not have them.

## Install

The Windows installer is built with PyInstaller and Inno Setup (see [Build a Windows installer](#build-a-windows-installer)). After installation, open **Game Save Sync** from the Start menu.

For development:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev,windows]"
```

On Windows, install [rclone](https://rclone.org/downloads/) or place `rclone.exe` in `vendor/` before packaging. A packaged build carries rclone next to `GameSaveSync.exe`.

## Cloud login

1. For Google Drive, create your own client first. rclone's shared Google login is retired during 2026, which is the notice the connect dialog used to show.
   - Open [Google Cloud Console](https://console.cloud.google.com/).
   - Create a project, enable the **Google Drive API**, and configure the consent screen.
   - Add the scope `https://www.googleapis.com/auth/drive.file`.
   - Create an OAuth client of type **Desktop app** and copy the client ID and client secret.
   - If Google shows an unverified-app warning during sign-in, choose Advanced and continue. Publish the app if you want the login to last; a Testing app grant expires after about a week.
2. Open the app and choose **Connect cloud**.
3. Pick Google Drive, OneDrive, or Dropbox. For Google Drive, paste the client ID and client secret.
4. Sign in through the browser window rclone opens.

The password is entered on the provider's site. rclone stores an OAuth refresh token in `%APPDATA%\GameSaveSync\rclone.conf` on this PC. Google Drive is requested with the `drive.file` scope, so the token can only see files this app creates. The token file is not encrypted. Anyone who can use this Windows account can use the token. OneDrive and Dropbox use each provider's normal rclone login.

Saves are stored at:

```text
<remote>:GameSaveSync/<profile>/<folder>/<file>
<remote>:GameSaveSync/<profile>/.backup/<timestamp>/<folder>/<file>
```

## Profiles

Add a profile with:

- a name, such as `PCSX2`
- the application `.exe`
- optional arguments
- optional **Wait for process**, when a launcher starts the real game and then exits (use the game's process name, for example `pcsx2-qt.exe`)
- one or more save folders

For an emulator, point a folder at the shared save root (for example the memory-card directory). Every game under that root syncs together.

**Create shortcut** puts a desktop shortcut on Windows. Opening that shortcut pulls, plays, and pushes. The shortcut targets `GameSaveSync.exe launch "Profile Name"`.

You can also edit `%APPDATA%\GameSaveSync\config.json`:

```json
{
  "remote": "gdrive",
  "backup_retention": 10,
  "profiles": [
    {
      "name": "PCSX2",
      "exe": "C:\\Emu\\PCSX2\\pcsx2-qt.exe",
      "args": "",
      "wait_process": "",
      "folders": [
        { "label": "memcards", "path": "C:\\Emu\\PCSX2\\memcards", "exclude": ["*.tmp"] }
      ]
    }
  ]
}
```

`backup_retention` is how many timestamped backup snapshots to keep for each save folder and for the cloud copy of that profile.

## What happens on launch

1. The tool lists the cloud folder and the local folder.
2. Files are matched by relative path and file name. `slot1.sav` is compared only with `slot1.sav`. A file that exists on only one side is copied across (downloaded before play, uploaded after play) and the other files are left in place.
3. When both copies exist, the newer modification time wins if the size or hash also differs. Google Drive listings include an MD5, and that hash is what gets compared. Providers that do not supply an MD5 are compared by file size. Timestamps within 2 seconds of each other, with different contents, are a tie: the cloud copy is kept and the local copy goes to `.backup`.
4. Identical contents are left alone, even if the timestamps differ.
5. The previous file is copied to `<save folder>\.backup\<timestamp>\` (local) or the cloud `.backup` path before it is replaced.
6. The linked program starts. If **Wait for process** is set, the tool waits until a new process with that name exits.
7. After the program exits, newer local files are uploaded. The previous cloud file is moved into the cloud backup first.

If the cloud cannot be reached before play, a dialog offers **Play anyway** or **Cancel**. A failed upload is stored as a pending upload and is retried on the next launch, **Sync now**, or **Sync all**.

Profiles whose application is running are skipped by **Sync all** and **Sync now**, so a game is not updated while it has its saves open. **Pull now** and **Push now** run even then.

## Status

The profile list shows the last result, for example `OK - 10/10 18:40`, `FAILED - pull`, `Pending upload`, or `Skipped: app running`. Failed rows are red. The details pane shows the error and the result for each save folder. If anything failed or still needs an upload, the main window shows a banner with **Sync all**.

Status is saved in `%APPDATA%\GameSaveSync\state.json`. Logs are in `%APPDATA%\GameSaveSync\logs\gamesavesync.log`.

## Commands

```text
gamesavesync
gamesavesync launch "PCSX2"
gamesavesync pull "PCSX2"
gamesavesync push "PCSX2"
gamesavesync sync "PCSX2"
gamesavesync sync-all
```

`sync-all` can be scheduled with Task Scheduler. It pulls, then pushes, every profile, and continues after a failure.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

Sync tests use rclone's local alias backend as a stand-in for the cloud. Install rclone and leave it on `PATH`, or set `GAMESAVESYNC_RCLONE` to the binary.

## Build a Windows installer

Build on Windows. PyInstaller does not cross-compile a Windows app from Linux.

1. Download `rclone.exe` into `vendor/rclone.exe`.
2. `pip install -e ".[build,windows]"`
3. From the repo root: `pyinstaller build/gamesavesync.spec`
4. Install [Inno Setup](https://jrsoftware.org/isinfo.php), then compile `build/installer.iss`.

The installer is written to `dist/GameSaveSync-Setup.exe`. Uninstall removes the program files and leaves `%APPDATA%\GameSaveSync` in place, including the cloud token and your profiles.
