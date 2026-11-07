; Cinema Gamma - Windows kurulum sihirbazı (Inno Setup 6).
; Derleme: önce PyInstaller ile dist\CinemaGamma\ klasörü üretilir, sonra
;   ISCC.exe /DAppVersion=1.0.0 installer\CinemaGamma.iss
; Kurulum yönetici izni istemez: program kullanıcının kendi klasörüne kurulur.

#define AppName "Cinema Gamma"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#define AppExe "CinemaGamma.exe"
; The program and its watchdog each hold one; CheckForMutexes is true while either runs.
#define AppMutex "Local\CinemaGamma,Local\CinemaGammaWatchdog"

[Setup]
AppId={{6D3F2B8A-41C7-4E59-9B0E-3C8D7A5F1E24}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=BerkaySoli
AppPublisherURL=https://github.com/BerkaySoli/cinema-gamma
AppSupportURL=https://github.com/BerkaySoli/cinema-gamma/issues
DefaultDirName={localappdata}\Programs\Cinema Gamma
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
LicenseFile=..\LICENSE
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
OutputDir=..\dist
OutputBaseFilename=CinemaGamma-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; The running copy is stopped by our own code below (it restores the screen colours first).
CloseApplications=no

[Languages]
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"; LicenseFile: "DISCLAIMER_tr.txt"
Name: "english"; MessagesFile: "compiler:Default.isl"; LicenseFile: "DISCLAIMER_en.txt"

[CustomMessages]
turkish.AutoStart=Windows açılışında otomatik başlat (önerilir)
english.AutoStart=Start automatically when Windows starts (recommended)
turkish.StartupGroup=Başlangıç:
english.StartupGroup=Startup:
turkish.LaunchNow=Cinema Gamma'yı şimdi başlat
english.LaunchNow=Start Cinema Gamma now

[Tasks]
Name: "autostart"; Description: "{cm:AutoStart}"; GroupDescription: "{cm:StartupGroup}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\CinemaGamma\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
; Same name the program itself uses for its Startup shortcut, so the two never duplicate.
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: autostart
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchNow}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[Code]
{ Ask a running copy (exe or the Python script) to quit through its stop file: it puts the
  original screen colours and display scales back before exiting, and its watchdog follows.
  Force-close the exe only if that fails. }
procedure StopRunningCopy();
var
  StopFile: String;
  I, ResultCode: Integer;
begin
  if not CheckForMutexes('{#AppMutex}') then
    Exit;
  StopFile := ExpandConstant('{localappdata}\CinemaGamma\stop');
  ForceDirectories(ExtractFileDir(StopFile));
  SaveStringToFile(StopFile, '', False);
  for I := 1 to 40 do
  begin
    if not CheckForMutexes('{#AppMutex}') then
      Exit;
    Sleep(200);
  end;
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  StopRunningCopy();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  StopRunningCopy();
  Result := True;
end;
