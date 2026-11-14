#define MyAppName "Kivi"
#define MyAppVersion "0.21.0"
#define MyAppPublisher "Kivi Project"
#define MyAppExeName "Kivi.exe"

[Setup]
AppId={{A1B9B987-3D87-47D5-B86F-7D2C7F0B4E10}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\Kivi
DefaultGroupName=Kivi
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=Kivi-Setup
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "..\..\dist\Kivi\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Kivi"; Filename: "{app}\Kivi.exe"
Name: "{autodesktop}\Kivi"; Filename: "{app}\Kivi.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"

[Run]
Filename: "{app}\Kivi.exe"; Description: "Запустить Kivi"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
