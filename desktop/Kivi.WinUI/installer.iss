#define MyAppName "Kivi"
#define MyAppVersion "0.21.0"
#define MyAppExeName "Kivi.exe"

[Setup]
AppId={{E6C1F2A0-8E7E-4A2C-9E3D-8F3B7A9D2C11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName={localappdata}\Programs\Kivi
DefaultGroupName=Kivi
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=Kivi-WinUI-Setup
Compression=none
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "..\..\dist\KiviWinUI\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Kivi"; Filename: "{app}\Kivi.exe"
Name: "{autodesktop}\Kivi"; Filename: "{app}\Kivi.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"

[Run]
Filename: "{app}\Kivi.exe"; Description: "Запустить Kivi"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
