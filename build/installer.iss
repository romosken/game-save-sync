; Inno Setup script. Compile on Windows after PyInstaller:
;   ISCC build\installer.iss

#define AppVersion "0.1.0"

[Setup]
AppId={{7B0E5A1C-4E2A-4F4A-9C1E-6D8A2B9F0C11}
AppName=Game Save Sync
AppVersion={#AppVersion}
AppPublisher=Game Save Sync
DefaultDirName={autopf}\GameSaveSync
DefaultGroupName=Game Save Sync
OutputDir=..\dist
OutputBaseFilename=GameSaveSync-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayIcon={app}\GameSaveSync.exe

[Files]
Source: "..\dist\GameSaveSync\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Game Save Sync"; Filename: "{app}\GameSaveSync.exe"
Name: "{autodesktop}\Game Save Sync"; Filename: "{app}\GameSaveSync.exe"

[Run]
Filename: "{app}\GameSaveSync.exe"; Description: "Open Game Save Sync"; Flags: nowait postinstall skipifsilent
