; Audiobook Forge installer (Inno Setup 6.5.2 or newer recommended).
; Built by "Build installer.bat", which passes AppSrc (the app folder) and AppVersion, and adds /DSPAN when
; the installer is too big for one file.

#ifndef AppSrc
  #define AppSrc "C:\Audiobook-Forge"
#endif
#ifndef AppVersion
  #define AppVersion "1.0"
#endif
#define AppName "Audiobook Forge"
#define AppExe "Audiobook Forge.exe"

[Setup]
AppId={{6F1C8A52-3B7D-4E0B-9C2A-AF0E5D4B7A11}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Hans De Kryger
DefaultDirName=C:\Audiobook-Forge
DisableDirPage=no
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputBaseFilename=Audiobook Forge Setup {#AppVersion}
SetupIconFile={#AppSrc}\forge.ico
UninstallDisplayIcon={app}\forge.ico
InfoBeforeFile=requirements.txt
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
Compression=lzma2/normal
SolidCompression=no
LZMAUseSeparateProcess=yes
#ifdef SPAN
DiskSpanning=yes
  #if Ver >= EncodeVer(6,5,2)
DiskSliceSize=max
  #endif
#endif

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "contextmenu"; Description: "Add ""Open in Audiobook Forge"" to the right-click menu of EPUB, PDF and Word files"; GroupDescription: "Shortcuts:"

[Dirs]
; the program keeps its settings, work files and finished books in its own folder
Name: "{app}"; Permissions: users-modify

[Files]
Source: "{#AppSrc}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; \
  Excludes: "\work,\output,\logs,\bench,\pasted,\custom_covers,\casting,\themes,\installer,\models\previews,__pycache__,*.pyc,\settings.json,\pronunciations.json,\watcher.log,\watcher_status.json,\watcher.stop,\gui_busy.flag,\pause-*.flag,\*.epub,\*.wav,\forge_gui_old.py,\forge_gui_v2_backup.py,\tts_test.py,\check_gpu.py,\bench.py,\bench2.py,\*.partial.*"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.epub\shell\AudiobookForge"; ValueType: string; ValueData: "Open in Audiobook Forge"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.epub\shell\AudiobookForge"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\forge.ico"; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.epub\shell\AudiobookForge\command"; ValueType: string; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\AudiobookForge"; ValueType: string; ValueData: "Open in Audiobook Forge"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\AudiobookForge"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\forge.ico"; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.pdf\shell\AudiobookForge\command"; ValueType: string; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.docx\shell\AudiobookForge"; ValueType: string; ValueData: "Open in Audiobook Forge"; Flags: uninsdeletekey; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.docx\shell\AudiobookForge"; ValueType: string; ValueName: "Icon"; ValueData: "{app}\forge.ico"; Tasks: contextmenu
Root: HKA; Subkey: "Software\Classes\SystemFileAssociations\.docx\shell\AudiobookForge\command"; ValueType: string; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: contextmenu

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open Audiobook Forge"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; files the program made while running (finished books in "output" and your settings are left alone)
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\work"
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\themes"
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
const
  SpaceMessage = 'Please choose a folder without spaces in its name, for example C:\Audiobook-Forge.' + #13#10 + #13#10 +
                 'The graphics card libraries cannot start from a folder whose path contains a space.';

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpSelectDir) and (not WizardSilent) and (Pos(' ', WizardDirValue) > 0) then
  begin
    MsgBox(SpaceMessage, mbError, MB_OK);
    Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  { also stops silent installs (/DIR=...) into a folder with a space }
  if Pos(' ', ExpandConstant('{app}')) > 0 then
    Result := SpaceMessage
  else
    Result := '';
end;
