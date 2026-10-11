# -*- mode: python ; coding: utf-8 -*-
"""Windows build. Run from the repo root: pyinstaller build/gamesavesync.spec"""

from pathlib import Path

project = Path(SPECPATH).resolve().parent
rclone_bins = []
for name in ("rclone.exe", "rclone"):
    candidate = project / "vendor" / name
    if candidate.is_file():
        rclone_bins.append((str(candidate), "."))

a = Analysis(
    [str(project / "build" / "entry.py")],
    pathex=[str(project / "src")],
    binaries=rclone_bins,
    datas=[],
    hiddenimports=[
        "psutil",
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "tkinter.messagebox",
        "tkinter.scrolledtext",
        "win32com.client",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GameSaveSync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="GameSaveSync",
)
