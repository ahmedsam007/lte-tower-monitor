; Inno Setup recipe for the Windows installer (Tower-Monitor-windows-setup.exe).
; Build the app first (pyinstaller --noconfirm packaging/TowerMonitor.spec), then:
;   iscc packaging\TowerMonitor.iss
; It installs for the current user only, so Windows does not ask for an administrator.
; Settings and logs live in %APPDATA%\Tower Monitor and are kept when it is uninstalled.

#define AppName "Tower Monitor"
#define AppVersion GetEnv("TOWER_MONITOR_VERSION")
#if AppVersion == ""
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{05653714-CDF6-428A-B65B-6D08D276EBFD}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Ahmed Abuammouna
AppPublisherURL=https://ahmedsam.com/
AppSupportURL=https://github.com/ahmedsam007/lte-tower-monitor
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppName}.exe
UninstallDisplayName={#AppName}
OutputDir=..\dist
OutputBaseFilename=Tower-Monitor-windows-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
#if FileExists(CompilerPath + "Languages\Arabic.isl")
Name: "arabic"; MessagesFile: "compiler:Languages\Arabic.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Old program files from an earlier version (never the settings, which are in %APPDATA%).
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
