$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot
$pythonPathFile = Join-Path $PSScriptRoot "python_path.txt"
$python = ""
if (Test-Path -LiteralPath $pythonPathFile -PathType Leaf) {
  $python = ([string](Get-Content -LiteralPath $pythonPathFile -Raw -Encoding UTF8)).Trim()
}
if ([string]::IsNullOrWhiteSpace($python) -or !(Test-Path -LiteralPath $python -PathType Leaf)) {
  $command = Get-Command python.exe -ErrorAction SilentlyContinue
  if ($command -and $command.Source -and $command.Source -notmatch "\\WindowsApps\\python(?:3)?\.exe$") {
    $python = [string]$command.Source
  }
}
if ([string]::IsNullOrWhiteSpace($python) -or !(Test-Path -LiteralPath $python -PathType Leaf)) {
  Write-Error "Python est introuvable. Relance install_windows.bat pour réparer le Worker."
  exit 3
}
$gitPathFile = Join-Path $PSScriptRoot "git_path.txt"
if (Test-Path -LiteralPath $gitPathFile -PathType Leaf) {
  $git = ([string](Get-Content -LiteralPath $gitPathFile -Raw -Encoding UTF8)).Trim()
  if (![string]::IsNullOrWhiteSpace($git) -and (Test-Path -LiteralPath $git -PathType Leaf)) {
    $gitDirectory = Split-Path -Parent $git
    if (($env:Path -split ";") -notcontains $gitDirectory) {
      $env:Path = $gitDirectory + ";" + $env:Path
    }
  }
}
# A scheduled task and the per-user startup fallback may both fire at logon.
# Keep a process-lifetime file handle so only one supervisor polls this Worker.
# FileShare.None also works across elevated/non-elevated Windows sessions.
$supervisorLock = $null
try {
  $supervisorLock = [IO.File]::Open(
    (Join-Path $PSScriptRoot "supervisor.lock"),
    [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None
  )
} catch {
  $lockError = $_.Exception
  if ($lockError.InnerException) { $lockError = $lockError.InnerException }
  if (($lockError.HResult -band 0xffff) -in @(32, 33)) {
    Write-Host "Le superviseur Worker est deja actif."
    exit 0
  }
  Write-Error "Impossible de verrouiller le dossier Worker. Verifie les droits du compte Windows."
  exit 3
}
$restartDelay = 3
# L'agent sait ainsi que ce superviseur s'arrete de lui-meme sur le code 64.
$env:ALPINE_WORKER_SUPERVISOR_EXIT_CODES = "64"
# Agent 1.49.1 : tache planifiee ou entree de demarrage de session = console cachee que personne ne lit.
# L'agent ecrit alors sa sortie (et la trace d'un plantage) dans logs\agent.log. Le service (nssm) recueille
# deja la sortie : rien ne change pour lui.
$agentLog = $null
$outputRedirected = $false
try { $outputRedirected = [Console]::IsOutputRedirected } catch { $outputRedirected = $false }
if (-not $outputRedirected) {
  $logsDirectory = Join-Path $PSScriptRoot "logs"
  try {
    if (!(Test-Path -LiteralPath $logsDirectory -PathType Container)) { New-Item -ItemType Directory -Path $logsDirectory -Force | Out-Null }
    $agentLog = Join-Path $logsDirectory "agent.log"
    $env:ALPINE_WORKER_LOG_FILE = $agentLog
  } catch { $agentLog = $null }
}
function Write-SupervisorLog([string]$Message) {
  Write-Host $Message
  if ($agentLog) {
    try { Add-Content -LiteralPath $agentLog -Value ((Get-Date -Format "yyyy-MM-dd HH:mm:ss") + " [superviseur] " + $Message) -Encoding UTF8 } catch { }
  }
}
try {
  while ($true) {
    $startedAt = [DateTime]::UtcNow
    $agentExitCode = 1
    try {
      & $python "agent.py" --config "config.json"
      $agentExitCode = $LASTEXITCODE
    } catch {
      Write-SupervisorLog ("Impossible de lancer le Worker (" + $_.Exception.Message + "). Nouvelle tentative automatique.")
    }
    # Deconnexion demandee depuis le dashboard : aucune relance automatique.
    # Sortie 0 pour que la tache planifiee ne la traite pas comme un echec.
    if ($agentExitCode -eq 64) {
      Write-SupervisorLog "Worker deconnecte depuis le dashboard. Relance MENU-WORKER.bat > 5 (Reconnecter le Worker) ou redemarre le PC pour le reconnecter."
      break
    }
    # An intentional update/restart (75), or an otherwise stable run, should
    # reconnect promptly. Repeated startup failures back off to at most 60 s.
    if ($agentExitCode -eq 75 -or ([DateTime]::UtcNow - $startedAt).TotalSeconds -ge 60) {
      $restartDelay = 3
    }
    Write-SupervisorLog "Worker arrete (code $agentExitCode). Relance automatique dans $restartDelay s."
    Start-Sleep -Seconds $restartDelay
    if ($agentExitCode -ne 75) {
      $restartDelay = [Math]::Min(60, $restartDelay * 2)
    }
  }
} finally {
  if ($supervisorLock) { $supervisorLock.Dispose() }
}
