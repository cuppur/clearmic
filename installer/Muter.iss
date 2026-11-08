#define AppVersion "0.4.3"
#define AppName "ClearMic"
[Setup]
AppId={{F6F6802E-6476-4117-908F-7758A74ED602}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
UsePreviousGroup=no
OutputDir=..\dist
OutputBaseFilename={#AppName}-Setup-{#AppVersion}-x64
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\muter\assets\muter.ico
DisableProgramGroupPage=yes
InfoAfterFile=COMPONENTS.txt
UninstallDisplayIcon={app}\{#AppName}.exe
AppMutex=Local\Muter.Desktop
CloseApplications=yes
RestartApplications=no
[Files]
Source: "..\dist\v{#AppVersion}\{#AppName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\vendor\vbcable\*"; DestDir: "{app}\components\vbcable"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\vendor\vbcable.zip"; DestDir: "{app}\components"; Flags: ignoreversion
Source: "COMPONENTS.txt"; DestDir: "{app}"; Flags: ignoreversion
[InstallDelete]
Type: files; Name: "{app}\Muter.exe"
Type: files; Name: "{autodesktop}\Muter.lnk"
Type: files; Name: "{autoprograms}\Muter\Muter.lnk"
Type: dirifempty; Name: "{autoprograms}\Muter"
[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
[Run]
Filename: "{app}\{#AppName}.exe"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent runasoriginaluser
[Code]
var CableMissing, DriverSuccess: Boolean;
function InitializeSetup(): Boolean;
begin
  CableMissing := not RegKeyExists(HKLM, 'SYSTEM\CurrentControlSet\Services\VBAudioVACMME');
  Result := True;
end;
function NeedsCable(): Boolean;
begin
  Result := CableMissing;
end;
function HasSnapshot(): Boolean;
begin
  Result := FileExists(ExpandConstant('{tmp}\muter-audio.json'));
end;
function NeedRestart(): Boolean;
begin
  Result := CableMissing and DriverSuccess;
end;
procedure CurStepChanged(CurStep: TSetupStep);
var Code, RestoreCode: Integer; AppExe, StateFile: String;
begin
  if (CurStep = ssPostInstall) and CableMissing then begin
    AppExe := ExpandConstant('{app}\{#AppName}.exe');
    StateFile := ExpandConstant('{tmp}\muter-audio.json');
    if not Exec(AppExe, '--snapshot-audio "' + StateFile + '"', '', SW_HIDE, ewWaitUntilTerminated, Code) or (Code <> 0) then
      RaiseException('Cannot preserve Windows audio settings. Driver installation was not started.');
    try
      DriverSuccess := Exec(ExpandConstant('{app}\components\vbcable\VBCABLE_Setup_x64.exe'), '-i -h', ExpandConstant('{app}\components\vbcable'), SW_HIDE, ewWaitUntilTerminated, Code);
      DriverSuccess := DriverSuccess and ((Code = 0) or (Code = 3010));
    finally
      if not Exec(AppExe, '--restore-audio "' + StateFile + '"', '', SW_HIDE, ewWaitUntilTerminated, RestoreCode) or (RestoreCode <> 0) then
        RaiseException('Windows audio settings could not be restored. Please keep the installation log.');
    end;
    if not DriverSuccess then
      RaiseException('VB-CABLE installation did not succeed. Driver exit code: ' + IntToStr(Code));
  end;
end;
