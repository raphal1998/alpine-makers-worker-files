@echo off
setlocal
set "INSTALLER=%~dp0install_windows.ps1"
if not exist "%INSTALLER%" (
  echo ERREUR: install_windows.ps1 est introuvable dans ce dossier.
  exit /b 2
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%" %*
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo.
  echo L'installation du Worker a echoue. Code: %RESULT%
)
exit /b %RESULT%
