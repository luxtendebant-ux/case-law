; HK Case Law Monitor — установщик (NSIS, установка для текущего пользователя, без прав администратора)
; © 2026 Вяселев Руслан Равилевич / Vyaselev Ruslan Ravilevich
Unicode true
SetCompressor /SOLID lzma
SetCompressorDictSize 64
ManifestDPIAware true
RequestExecutionLevel user

!define APPNAME "HK Case Law Monitor"
!define APPID "HKCaseLawMonitor"
!define VERSION "2.0.1"
!define PUBLISHER "Вяселев Руслан Равилевич / Vyaselev Ruslan Ravilevich"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPID}"
!define RUNKEY "Software\Microsoft\Windows\CurrentVersion\Run"
!define TASKNAME "HKCaseLawMonitor AutoCheck"
!define WV2URL "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

Name "${APPNAME} ${VERSION}"
OutFile "out\HKCaseLawMonitor-Setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\${APPNAME}"
InstallDirRegKey HKCU "Software\${APPID}" "InstallDir"
BrandingText "${APPNAME} ${VERSION} — © 2026 Вяселев Р. Р."

VIProductVersion "2.0.1.0"
VIFileVersion "2.0.1.0"
VIAddVersionKey /LANG=1049 "ProductName" "${APPNAME}"
VIAddVersionKey /LANG=1049 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=1049 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=1049 "CompanyName" "${PUBLISHER}"
VIAddVersionKey /LANG=1049 "LegalCopyright" "© 2026 ${PUBLISHER}"
VIAddVersionKey /LANG=1049 "FileDescription" "Установка ${APPNAME}"

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
!include "FileFunc.nsh"

!define MUI_ICON "app.ico"
!define MUI_UNICON "app.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "wizard.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "wizard.bmp"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_RIGHT
!define MUI_HEADERIMAGE_BITMAP "header.bmp"
!define MUI_ABORTWARNING
!define MUI_COMPONENTSPAGE_SMALLDESC

!define MUI_WELCOMEPAGE_TITLE "Установка ${APPNAME} ${VERSION}"
!define MUI_WELCOMEPAGE_TEXT "Программа для поиска и мониторинга судебной практики Гонконга, контроля дел, анализа решений и работы с ИИ-агентами.$\r$\n$\r$\nПрограмма устанавливается только для текущего пользователя и не требует прав администратора.$\r$\n$\r$\n© 2026 ${PUBLISHER}"
!insertmacro MUI_PAGE_WELCOME
!define MUI_LICENSEPAGE_RADIOBUTTONS
!insertmacro MUI_PAGE_LICENSE "EULA.txt"
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\HKCaseLawMonitor.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Запустить ${APPNAME}"
!define MUI_FINISHPAGE_SHOWREADME "$INSTDIR\README.txt"
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Открыть краткое руководство"
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "Russian"

Function .onInit
  SetShellVarContext current
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "Для ${APPNAME} нужна 64-разрядная Windows 10 или 11."
    Abort
  ${EndIf}
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "Для ${APPNAME} нужна Windows 10 или 11."
    Abort
  ${EndIf}
FunctionEnd

Function StopRunning
  IfFileExists "$INSTDIR\hkcl.exe" 0 +3
    nsExec::Exec '"$INSTDIR\hkcl.exe" shutdown'
    Sleep 2500
FunctionEnd

Section "Программа (обязательно)" SecMain
  SectionIn RO
  SetShellVarContext current
  Call StopRunning
  ; чистое обновление: старые файлы программы удаляются, данные пользователя (%APPDATA%) не затрагиваются
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\lib"
  RMDir /r "$INSTDIR\app"
  SetOutPath "$INSTDIR"
  File /r "dist\HKCaseLawMonitor\*.*"
  File "README.txt"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  WriteRegStr HKCU "Software\${APPID}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\${APPID}" "Version" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\HKCaseLawMonitor.exe,0"
  WriteRegStr HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTKEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegStr HKCU "${UNINSTKEY}" "URLInfoAbout" "https://luxtendebant-ux.github.io/case-law/"
  WriteRegStr HKCU "${UNINSTKEY}" "Comments" "Поиск и мониторинг судебной практики Гонконга"
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINSTKEY}" "EstimatedSize" "$0"

  CreateDirectory "$SMPROGRAMS\${APPNAME}"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" "$INSTDIR\HKCaseLawMonitor.exe" "" "$INSTDIR\HKCaseLawMonitor.exe" 0 SW_SHOWNORMAL "" "Поиск и мониторинг судебной практики Гонконга"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Лицензионное соглашение.lnk" "$INSTDIR\EULA.txt"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Краткое руководство.lnk" "$INSTDIR\README.txt"
  CreateShortcut "$SMPROGRAMS\${APPNAME}\Удалить ${APPNAME}.lnk" "$INSTDIR\Uninstall.exe"

  ; .NET Framework 4.7.2+ (нужен окну программы)
  ReadRegDWORD $0 HKLM "SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full" "Release"
  ${If} $0 < 461808
    DetailPrint "Внимание: не найден .NET Framework 4.7.2 или новее."
    IfSilent +2
    MessageBox MB_ICONEXCLAMATION "Не найден .NET Framework 4.7.2 или новее. Окно программы может не открыться; программа будет работать через браузер. Обновите Windows (Центр обновления)."
  ${EndIf}
  ; Microsoft Edge WebView2 Runtime (собственное окно программы)
  Call CheckWebView2
SectionEnd

Function CheckWebView2
  StrCpy $1 ""
  ReadRegStr $1 HKLM "SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${If} $1 == ""
  ${OrIf} $1 == "0.0.0.0"
    ReadRegStr $1 HKCU "SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  ${EndIf}
  ${If} $1 == ""
  ${OrIf} $1 == "0.0.0.0"
    DetailPrint "Microsoft Edge WebView2 Runtime не найден."
    IfSilent done
    MessageBox MB_YESNO|MB_ICONINFORMATION "Для собственного окна программы нужен бесплатный компонент Microsoft Edge WebView2 Runtime (в Windows 11 он уже есть). Он ставится без прав администратора.$\r$\n$\r$\nОткрыть страницу загрузки Microsoft?" IDNO done
    ExecShell "open" "${WV2URL}"
  ${Else}
    DetailPrint "Microsoft Edge WebView2 Runtime: $1"
  ${EndIf}
  done:
FunctionEnd

Section "Ярлык на рабочем столе" SecDesktop
  SetShellVarContext current
  CreateShortcut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\HKCaseLawMonitor.exe" "" "$INSTDIR\HKCaseLawMonitor.exe" 0 SW_SHOWNORMAL "" "Поиск и мониторинг судебной практики Гонконга"
SectionEnd

Section "Автозапуск при входе в Windows" SecAutostart
  WriteRegStr HKCU "${RUNKEY}" "${APPID}" '"$INSTDIR\HKCaseLawMonitor.exe" --background'
SectionEnd

Section "Проверка по расписанию (08:00)" SecTask
  nsExec::ExecToLog '"$INSTDIR\hkcl.exe" schedule install'
  Pop $0
  ${If} $0 != 0
    DetailPrint "Задачу Планировщика заданий создать не удалось (код $0). Её можно создать в программе: раздел «Автоматизация»."
  ${EndIf}
SectionEnd

LangString DESC_Main ${LANG_RUSSIAN} "Программа, встроенный Python и все компоненты. Отдельно ставить ничего не нужно."
LangString DESC_Desktop ${LANG_RUSSIAN} "Значок программы на рабочем столе."
LangString DESC_Autostart ${LANG_RUSSIAN} "Программа запускается при входе в Windows и работает в области уведомлений: расписание, уведомления, доступ для ИИ-агента."
LangString DESC_Task ${LANG_RUSSIAN} "Задача Windows запускает проверку лент и дел по расписанию, даже если программа закрыта. Расписание меняется в разделе «Автоматизация»."
!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain} $(DESC_Main)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} $(DESC_Desktop)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecAutostart} $(DESC_Autostart)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecTask} $(DESC_Task)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

; ---------------------------------------------------------------- удаление
Function un.onInit
  SetShellVarContext current
FunctionEnd

Section "Uninstall"
  SetShellVarContext current
  IfFileExists "$INSTDIR\hkcl.exe" 0 +3
    nsExec::Exec '"$INSTDIR\hkcl.exe" shutdown'
    Sleep 2500
  nsExec::Exec 'schtasks /Delete /TN "${TASKNAME}" /F'
  DeleteRegValue HKCU "${RUNKEY}" "${APPID}"
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\lib"
  RMDir /r "$INSTDIR\app"
  Delete "$INSTDIR\HKCaseLawMonitor.exe"
  Delete "$INSTDIR\hkcl.exe"
  Delete "$INSTDIR\app.ico"
  Delete "$INSTDIR\EULA.txt"
  Delete "$INSTDIR\THIRD-PARTY-NOTICES.txt"
  Delete "$INSTDIR\README.txt"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  Delete "$DESKTOP\${APPNAME}.lnk"
  RMDir /r "$SMPROGRAMS\${APPNAME}"
  DeleteRegKey HKCU "${UNINSTKEY}"
  DeleteRegKey HKCU "Software\${APPID}"
  IfSilent keep
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 "Удалить также данные программы (настройки, собранные решения, дела на контроле, документы)?$\r$\n$\r$\n$APPDATA\${APPID}" IDNO keep
    RMDir /r "$APPDATA\${APPID}"
  keep:
SectionEnd
