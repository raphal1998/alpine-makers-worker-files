param([Parameter(Mandatory=$true)][ValidateSet("reconnect","network","cleanup")][string]$Action)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
if (!(Test-Path -LiteralPath "config.json" -PathType Leaf)) { throw "Lance cet outil depuis le dossier du Worker deja associe." }
$python = ([string](Get-Content -LiteralPath "python_path.txt" -Raw -Encoding UTF8)).Trim()
if (!(Test-Path -LiteralPath $python -PathType Leaf)) { throw "Interpreteur Python du Worker introuvable." }
# The supervisor's lifetime lock prevents duplicate processes, even if a
# scheduled task or another manual launcher is already running.
$supervisor = Join-Path $PSScriptRoot "run_worker.ps1"
# Une reconnexion manuelle leve la deconnexion demandee depuis le dashboard.
if ($Action -eq "reconnect") { Remove-Item -LiteralPath ".worker-disconnected" -Force -ErrorAction SilentlyContinue }
# Sur un PC ou ce Worker est un SERVICE Windows (nssm "AlpineWorker" visant ce dossier), le
# superviseur appartient au service. Le lancer ici, dans la session, creerait un second
# lanceur : apres un arret volontaire du service il prendrait le verrou supervisor.lock, et
# le service, a son retour, tournerait a vide (run_worker.ps1 sort 0, nssm le relance en boucle).
$serviceGereCeDossier = $false
try {
  $dossierService = [string](Get-ItemProperty -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Services\AlpineWorker\Parameters" -ErrorAction Stop).AppDirectory
  if ($dossierService -and ($dossierService.TrimEnd("\") -ieq ([string]$PSScriptRoot).TrimEnd("\"))) { $serviceGereCeDossier = $true }
} catch { }
if ($serviceGereCeDossier) {
  $etatService = [string](Get-Service -Name "AlpineWorker" -ErrorAction SilentlyContinue).Status
  if ($etatService -ne "Running") {
    Write-Host "Ce Worker est un service Windows, actuellement arrete ($etatService)."
    Write-Host "Rallume-le avec MENU-WORKER.bat > Rallumer le Worker (fenetre administrateur)."
    # Une suppression confirmee ne doit JAMAIS rester en attente : elle s'executerait seule au
    # prochain demarrage du service, longtemps apres la confirmation.
    if ($Action -eq "cleanup") { Write-Host "Nettoyage refuse tant que le service est arrete : rallume d'abord le Worker."; exit 2 }
    Write-Host "La demande ci-dessous restera en attente tant que le service est arrete."
  }
} else {
  Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$supervisor`"" -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
}
& $python "local_control.py" --root $PSScriptRoot --action $Action
exit $LASTEXITCODE
