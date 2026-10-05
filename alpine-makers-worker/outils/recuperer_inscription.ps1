<#
    Récupérer une inscription : enveloppe expliquée de install_windows.ps1 -RecoverExisting (racine du
    Worker). Remplace l'ancien « recover existing worker.bat », retiré du paquet en 1.21.3.

    Usage :
      recuperer_inscription.ps1 [-Racine <dossier>] [-SansPause] [-AutoStart Yes|No] [options de l'installateur]
      -AutoStart No   : pas de question « démarrage automatique ». Choisi tout seul quand le service
                        Windows AlpineWorker gère déjà ce dossier (un second lanceur serait un doublon).
      Toute autre option (-InstallDir "<dossier>", -MigrationCode …) est transmise telle quelle à l'installateur.
    Codes : 0 succès, 1 récupération non terminée, 2 fichier absent.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause,
    [string]$AutoStart = '',
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Reste = @()
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
Initialize-ConsoleWorker -Titre 'Récupérer une inscription'
$Racine = Get-RacineWorker -Racine $Racine
$NomOutil = 'recuperer_inscription'
$cible = Join-Path $Racine 'install_windows.ps1'

Write-Explication -Titre 'RÉCUPÉRER UNE INSCRIPTION EXISTANTE' -Fait @(
    'Lance l''assistant de récupération de l''installateur : il retrouve la fiche déjà présente sur le site pour la clé de ce PC.',
    'Demande un code de MIGRATION (pas un code d''association) : à générer sur le site, depuis la fiche du Worker.'
) -NeFaitPas @(
    'Ne crée jamais une nouvelle inscription : si la fiche a été supprimée sur le site, utilise « Réassocier le Worker » (8).',
    'Ne touche pas aux moteurs ni aux modèles installés.'
)

if (-not (Test-Path -LiteralPath $cible -PathType Leaf)) {
    Write-Alerte ('install_windows.ps1 est absent de la racine du Worker : ' + $cible)
    Write-Conseil 'Extrais l''archive complète du Worker (paquet du dashboard) avant de lancer cet outil.'
    Wait-FinOutil -SansPause:$SansPause
    exit 2
}
if ($AutoStart -eq '') {
    $mode = $null
    try { $mode = Get-ModeLancement -Racine $Racine } catch { }
    if ($mode -and $mode.Mode -eq 'service') {
        $AutoStart = 'No'
        Write-Info 'Ce PC lance le Worker par le service Windows : aucune tâche planifiée ne sera créée (la question du démarrage automatique ne sera pas posée).'
    }
}
$arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $cible, '-RecoverExisting')
if ($AutoStart) { $arguments += @('-AutoStart', $AutoStart) }
if ($Reste) { $arguments += $Reste }

Write-Section 'Exécution'
$ancien = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
& powershell.exe @arguments
$code = [int]$LASTEXITCODE
$ErrorActionPreference = $ancien
Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('install_windows.ps1 -RecoverExisting terminé, code ' + $code)
Write-Host ''
if ($code -eq 0) { Write-Ok 'Récupération terminée : vérifie que le Worker est en ligne sur le site.' }
else { Write-Alerte ('Récupération non terminée (code ' + $code + ') : aucune nouvelle inscription automatique. Lis le message ci-dessus.') }
Wait-FinOutil -SansPause:$SansPause
if ($code -eq 0) { exit 0 } else { exit 1 }
