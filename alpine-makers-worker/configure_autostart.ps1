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
# Migrate only entries targeting THIS installation. Never disable another Worker.
foreach ($task in Get-ScheduledTask -ErrorAction Stop) {
  if ($task.TaskName -notlike "Alpine Makers*") { continue }
  $owned = @($task.Actions | Where-Object { ([string]$_.Arguments).IndexOf($quotedSupervisor,[StringComparison]::OrdinalIgnoreCase) -ge 0 }).Count -gt 0
  if ($owned -and ($AutoStart -eq "No" -or $task.TaskName -ne $taskName)) {
    Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false -ErrorAction Stop
  }
}
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
if (Test-Path -LiteralPath $runKey) {
  $runValues = Get-ItemProperty -LiteralPath $runKey
  foreach ($property in $runValues.PSObject.Properties) {
    if ($property.Name -like "AlpineMakersWorker*" -and ([string]$property.Value).IndexOf($quotedSupervisor,[StringComparison]::OrdinalIgnoreCase) -ge 0) {
      Remove-ItemProperty -LiteralPath $runKey -Name $property.Name -ErrorAction Stop
    }
  }
}
if ($AutoStart -eq "Yes") {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$supervisor`""
  $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $workerRoot
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity.Name
  $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
  $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force -ErrorAction Stop | Out-Null
  Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
  Write-Host "Demarrage automatique active a l'ouverture de cette session Windows."
} else {
  Write-Host "Demarrage automatique desactive. Aucun Worker n'est lance par cet installateur."
  Write-Host "Un Worker deja lance reste actif. Utilise MENU-WORKER.bat > 5 (Reconnecter le Worker) pour un lancement manuel."
}
