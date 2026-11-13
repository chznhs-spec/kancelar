[Setup]
AppName=VLČR Kancelář
AppVersion=1.0.0
DefaultDirName={pf}\VLČR Kancelář
DefaultGroupName=VLČR Kancelář
OutputDir=.
OutputBaseFilename=vlcr_kancelar_windows_installer
Compression=lzma
SolidCompression=yes

[Files]
; Zkopíruje hlavní aplikaci a složku s moduly z dist/ do Program Files
Source: "dist\vlcr-kancelar\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "dist\apps\*"; DestDir: "{app}\apps"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Vytvoří ikony v nabídce Start a na ploše
Name: "{group}\VLČR Kancelář"; Filename: "{app}\vlcr-kancelar.exe"
Name: "{group}\Odinstalovat VLČR Kancelář"; Filename: "{app}\unins000.exe"
Name: "{commondesktop}\VLČR Kancelář"; Filename: "{app}\vlcr-kancelar.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Vytvořit ikonu na ploše"; GroupDescription: "Další úkoly:"

[Run]
Filename: "{app}\vlcr-kancelar.exe"; Description: "Spustit VLČR Kancelář", Flags: nowait postinstall skipifsilent