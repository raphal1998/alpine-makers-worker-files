@echo off
setlocal
set "INSTALLER=%~dp0install_windows.ps1"
if not exist "%INSTALLER%" (
  echo ERREUR: install_windows.ps1 est introuvable dans ce dossier.
  if not defined ALPINE_INSTALL_NO_PAUSE pause
  exit /b 2
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%" %*
set "RESULT=%ERRORLEVEL%"
if "%RESULT%"=="0" exit /b 0
rem Agent 1.51.1 : en cas d'echec la fenetre reste ouverte (double-clic) pour lire le message et le journal.
rem Hors bloc entre parentheses : un chemin TMP contenant ")" ne coupe pas la commande.
echo.
echo L'installation du Worker a echoue. Code: %RESULT%
echo Journal de l'installation : "%TMP%\alpine-makers-worker-installation.log"
if not defined ALPINE_INSTALL_NO_PAUSE pause
exit /b %RESULT%
