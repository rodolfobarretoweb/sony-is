#define AppName "Sony Video Stabilizer"
#define AppVersion "1.0.0"

[Setup]
AppId={{6DA353E9-67B7-4BD7-8CF9-BA9A1B1164CF}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir=dist
OutputBaseFilename=Sony-Video-Stabilizer-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\Sony Video Stabilizer.exe

[Files]
Source: "dist\Sony Video Stabilizer\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\Sony Video Stabilizer"; Filename: "{app}\Sony Video Stabilizer.exe"
Name: "{autodesktop}\Sony Video Stabilizer"; Filename: "{app}\Sony Video Stabilizer.exe"; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked
