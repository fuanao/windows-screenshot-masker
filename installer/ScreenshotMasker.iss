#define MyAppName "Screenshot Masker"
#define MyAppExeName "ScreenshotMasker.exe"
#define MyAppVersion "0.2.0"
#define SourceDir "..\dist\ScreenshotMasker"
#define OutputDir "..\..\..\outputs"

[Setup]
AppId={{7D14A5C5-20A5-4D5F-A9EE-7D6A3A3FB5E1}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=fuanao
DefaultDirName={localappdata}\ScreenshotMasker
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=ScreenshotMasker_Setup
Compression=lzma2
SolidCompression=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
SetupLogging=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent unchecked
