param([Parameter(Mandatory=$true)][ValidateSet("Yes","No")][string]$AutoStart)
$ErrorActionPreference = "Stop"
$sandboxOnly = ($env:ALPINE_LOCAL_SANDBOX -eq '1') -or (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'TEST_SANDBOX_ONLY.txt'))
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'config.json')) {
  $localConfig = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  $sandboxOnly = $sandboxOnly -or ($localConfig.local_sandbox -eq $true)
  $localConfig = $null
}
if ($sandboxOnly) { throw "Worker TEST : aucune tache planifiee ni entree de demarrage Windows ne sera modifiee. Utilise le lanceur isole TEST." }
$workerRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$supervisor = Join-Path $workerRoot "run_worker.ps1"
if (!(Test-Path -LiteralPath $supervisor -PathType Leaf)) { throw "Superviseur Worker introuvable." }
$digest = [Security.Cryptography.SHA256]::Create()
try { $suffix = ([BitConverter]::ToString($digest.ComputeHash([Text.Encoding]::UTF8.GetBytes($workerRoot.ToLowerInvariant())))).Replace("-", "").Substring(0,16) } finally { $digest.Dispose() }
$taskName = "Alpine Makers Worker $suffix"
$quotedSupervisor = '"' + $supervisor + '"'
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
# Unprivileged fallback: same suffix and same command line as the scheduled task.
$runName = "AlpineMakersWorker$suffix"
$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$supervisor`""
$runCommand = "powershell.exe $arguments"

function Test-AccessDenied {
  # Insufficient rights, recognised without the localized message: error category,
  # exception type, HRESULT 0x80070005 (E_ACCESSDENIED) or the CIM native code.
  param($ErrorRecord)
  if (!$ErrorRecord) { return $false }
  if ($ErrorRecord.CategoryInfo -and [string]$ErrorRecord.CategoryInfo.Category -eq "PermissionDenied") { return $true }
  if ([string]$ErrorRecord.FullyQualifiedErrorId -match "0x80070005") { return $true }
  $exception = $ErrorRecord.Exception
  while ($exception) {
    if ($exception -is [UnauthorizedAccessException]) { return $true }
    if ($exception.HResult -eq -2147024891) { return $true }
    $native = $exception.PSObject.Properties["NativeErrorCode"]
    if ($native -and [string]$native.Value -eq "AccessDenied") { return $true }
    $exception = $exception.InnerException
  }
  return $false
}

function Test-OwnedTask {
  # True only when one action of the task launches THIS installation's supervisor.
  param($Task)
  return @($Task.Actions | Where-Object { ([string]$_.Arguments).IndexOf($quotedSupervisor,[StringComparison]::OrdinalIgnoreCase) -ge 0 }).Count -gt 0
}

function Remove-OwnedRunEntries {
  # Removes the per-user startup entries targeting THIS supervisor, except -Keep.
  param([string]$Keep = "")
  if (!(Test-Path -LiteralPath $runKey)) { return }
  $runValues = Get-ItemProperty -LiteralPath $runKey
  foreach ($property in $runValues.PSObject.Properties) {
    if ($property.Name -like "AlpineMakersWorker*" -and $property.Name -ne $Keep -and ([string]$property.Value).IndexOf($quotedSupervisor,[StringComparison]::OrdinalIgnoreCase) -ge 0) {
      Remove-ItemProperty -LiteralPath $runKey -Name $property.Name -ErrorAction Stop
    }
  }
}

function Start-SupervisorNow {
  # run_worker.ps1 holds supervisor.lock: a second supervisor exits by itself (code 0).
  Start-Process -FilePath "powershell.exe" -ArgumentList $arguments -WorkingDirectory $workerRoot -WindowStyle Hidden -ErrorAction Stop | Out-Null
}

# Migrate only entries targeting THIS installation. Never disable another Worker.
$deniedTasks = @()
foreach ($task in Get-ScheduledTask -ErrorAction Stop) {
  if ($task.TaskName -notlike "Alpine Makers*") { continue }
  if ((Test-OwnedTask $task) -and ($AutoStart -eq "No" -or $task.TaskName -ne $taskName)) {
    try {
      Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false -ErrorAction Stop
    } catch {
      if (!(Test-AccessDenied $_)) { throw }
      $deniedTasks += [string]$task.TaskName
    }
  }
}
if ($AutoStart -eq "Yes") {
  if ($deniedTasks.Count -gt 0) {
    Write-Host ("Ancienne tache planifiee de ce Worker laissee en place (droits administrateur requis pour la retirer) : " + ($deniedTasks -join ", ") + ". MENU-WORKER.bat > 15 (Demarrage automatique) sait la retirer.")
  }
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $workerRoot
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
  $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
  $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
  $mechanism = "task"
  try {
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
  } catch {
    # Only a refusal for insufficient rights has a fallback. Any other error stays an error.
    if (!(Test-AccessDenied $_)) { throw }
    $mechanism = "session"
    $shortName = ([string]$identity.Name -split "\\")[-1]
    foreach ($existing in @(Get-ScheduledTask -ErrorAction SilentlyContinue)) {
      if ($existing.TaskName -ne $taskName -or !(Test-OwnedTask $existing) -or [string]$existing.State -eq "Disabled") { continue }
      # A task that opens ANOTHER account's session would never start this Worker here.
      $taskUser = [string]$existing.Principal.UserId
      if ($taskUser -and ($taskUser -ieq $identity.Name -or $taskUser -ieq $shortName -or ($identity.User -and $taskUser -eq $identity.User.Value))) { $mechanism = "kept" }
    }
  }
  if ($mechanism -eq "task") {
    # One mechanism only: the fallback entry of a previous installation is removed.
    Remove-OwnedRunEntries
    Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
    Write-Host "Demarrage automatique active a l'ouverture de cette session Windows (tache planifiee)."
  } elseif ($mechanism -eq "kept") {
    Remove-OwnedRunEntries
    Write-Host "Windows refuse de recreer la tache planifiee sans droits administrateur."
    Write-Host "Tache planifiee existante conservee (creee avec des droits administrateur) : le Worker demarre a l'ouverture de cette session Windows."
    try {
      Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
    } catch {
      if (!(Test-AccessDenied $_)) { throw }
      Start-SupervisorNow
      Write-Host "Windows refuse aussi de lancer cette tache maintenant : le Worker est lance directement pour cette session."
    }
  } else {
    if ($runCommand.Length -gt 259) { throw "Dossier du Worker trop long pour une entree de demarrage Windows (260 caracteres au plus). Installe le Worker dans un dossier plus court, ou relance l'installateur en administrateur." }
    if (!(Test-Path -LiteralPath $runKey)) { New-Item -Path $runKey -Force | Out-Null }
    Set-ItemProperty -LiteralPath $runKey -Name $runName -Value $runCommand -ErrorAction Stop
    Remove-OwnedRunEntries -Keep $runName
    Start-SupervisorNow
    Write-Host "Windows refuse la tache planifiee sans droits administrateur : ce n'est pas une erreur d'installation."
    Write-Host "Demarrage automatique active a l'ouverture de cette session Windows par l'entree de demarrage de Windows (registre du compte), sans relance automatique par le planificateur. Le Worker est lance maintenant."
    Write-Host "Pour une tache planifiee : relance l'installateur en administrateur, ou MENU-WORKER.bat > 15 (Demarrage automatique)."
  }
} else {
  # The per-user entry needs no privilege: it is removed even when a task resists.
  Remove-OwnedRunEntries
  if ($deniedTasks.Count -gt 0) {
    throw ("Demarrage automatique NON retire : Windows refuse de retirer la tache planifiee sans droits administrateur (" + ($deniedTasks -join ", ") + "). L'entree de demarrage de session est retiree. Utilise MENU-WORKER.bat > 15 (Demarrage automatique), qui demande les droits administrateur.")
  }
  Write-Host "Demarrage automatique desactive. Aucun Worker n'est lance par cet installateur."
  Write-Host "Un Worker deja lance reste actif. Utilise MENU-WORKER.bat > 5 (Reconnecter le Worker) pour un lancement manuel."
}
