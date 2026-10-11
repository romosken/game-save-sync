# Bundled rclone

Place the Windows rclone binary here before building the installer:

- `vendor/rclone.exe`

The PyInstaller spec copies that file next to `GameSaveSync.exe`. Download it from https://rclone.org/downloads/ (Windows, 64-bit).

The Linux test suite uses rclone from `PATH` or `.tools/rclone` and does not need this file.
