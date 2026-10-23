#ifndef ReleaseDir
  #define ReleaseDir "..\发布\幕墙方案助手_V5_智能解读"
#endif
[Setup]
AppId={{782B3188-20BC-4E52-85F2-731EB0180624}
AppName=幕墙方案助手
AppVersion=5.0.0
AppVerName=幕墙方案助手 V5（AI 解读与方案决策）
DefaultDirName={localappdata}\Programs\幕墙方案助手
DefaultGroupName=幕墙方案助手
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#ReleaseDir}
OutputBaseFilename=幕墙方案助手_V5_安装包
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\幕墙方案助手.exe
CloseApplications=yes
RestartApplications=no
Uninstallable=not IsPortableTest
CreateUninstallRegKey=not IsPortableTest

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[Files]
Source: "{#ReleaseDir}\幕墙方案助手\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\幕墙方案助手"; Filename: "{app}\幕墙方案助手.exe"; WorkingDir: "{app}"; Check: not IsPortableTest
Name: "{userprograms}\卸载幕墙方案助手"; Filename: "{uninstallexe}"; Check: not IsPortableTest
Name: "{userdesktop}\幕墙方案助手"; Filename: "{app}\幕墙方案助手.exe"; WorkingDir: "{app}"; Tasks: desktopicon; Check: not IsPortableTest

[Run]
Filename: "{app}\幕墙方案助手.exe"; Description: "启动幕墙方案助手"; Flags: nowait postinstall skipifsilent

; Deliberately no deletion of LOCALAPPDATA\MqHelper during upgrade or uninstall.

[Code]
function IsPortableTest: Boolean;
begin
  Result := ExpandConstant('{param:PORTABLETEST|0}') = '1';
end;
