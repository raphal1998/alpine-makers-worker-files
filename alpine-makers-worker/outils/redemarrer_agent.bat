@echo off
rem Lanceur de l'outil redemarrer_agent.ps1 du Worker Alpine Makers. Les options sont relayees telles quelles.
setlocal
chcp 65001 >nul
title Alpine Makers - redemarrer_agent
if not exist "%~dp0redemarrer_agent.ps1" (
  echo.
  echo   Outil introuvable : redemarrer_agent.ps1 est absent du dossier outils.
  echo   Mets le Worker a jour depuis le dashboard pour reinstaller ses outils.
  echo.
  if "%~1"=="" pause
  exit /b 2
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0redemarrer_agent.ps1" %*
set "RESULT=%ERRORLEVEL%"
endlocal & exit /b %RESULT%
