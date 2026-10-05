@echo off
rem Menu du Worker Alpine Makers : tous les outils, regroupes et expliques.
rem Double-clique ce fichier. Il ne demande jamais les droits administrateur lui-meme.
setlocal
chcp 65001 >nul
title Alpine Makers - Menu du Worker
if not exist "%~dp0outils\menu_worker.ps1" (
  echo.
  echo   Le menu est introuvable : outils\menu_worker.ps1 est absent de ce dossier.
  echo   Mets le Worker a jour depuis le dashboard pour reinstaller ses outils.
  echo.
  pause
  exit /b 2
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0outils\menu_worker.ps1" %*
set "RESULT=%ERRORLEVEL%"
endlocal & exit /b %RESULT%
