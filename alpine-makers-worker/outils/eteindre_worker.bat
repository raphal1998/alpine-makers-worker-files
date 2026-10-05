@echo off
rem Lanceur de l'outil eteindre_worker.ps1 du Worker Alpine Makers. Les options sont relayees telles quelles.
setlocal
chcp 65001 >nul
title Alpine Makers - eteindre_worker
if not exist "%~dp0eteindre_worker.ps1" (
  echo.
  echo   Outil introuvable : eteindre_worker.ps1 est absent du dossier outils.
  echo   Mets le Worker a jour depuis le dashboard pour reinstaller ses outils.
  echo.
  if "%~1"=="" pause
  exit /b 2
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0eteindre_worker.ps1" %*
set "RESULT=%ERRORLEVEL%"
endlocal & exit /b %RESULT%
