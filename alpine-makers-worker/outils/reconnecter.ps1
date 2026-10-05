<#
    Reconnecter le Worker : enveloppe expliquée de worker_tools.ps1 -Action reconnect (racine du Worker).
    Remplace l'ancien « reconnect worker.bat » de la racine, retiré du paquet en 1.21.3.

    Usage :
      reconnecter.ps1 [-Racine <dossier>] [-SansPause]
    Codes : 0 succès, 1 échec, 2 refusé, annulé ou fichier absent.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
Initialize-ConsoleWorker -Titre 'Reconnecter le Worker'
$Racine = Get-RacineWorker -Racine $Racine
$NomOutil = 'reconnecter'
$cible = Join-Path $Racine 'worker_tools.ps1'

Write-Explication -Titre 'RECONNECTER LE WORKER' -Fait @(
    'Efface le marqueur de déconnexion posé par le bouton « Déconnecter » du site.',
    'Lance le superviseur s''il est absent (PC sans service), sinon demande à l''agent un heartbeat et un réexamen du réseau.'
) -NeFaitPas @(
    'Ne recrée jamais une identité : si le site refuse le Worker, utilise « Récupérer une inscription » (7) ou « Réassocier » (8).',
    'N''installe rien, ne touche ni à Windows ni aux moteurs.'
)

if (-not (Test-Path -LiteralPath $cible -PathType Leaf)) {
    Write-Alerte ('worker_tools.ps1 est absent de la racine du Worker : ' + $cible)
    Write-Conseil 'Mets le Worker à jour depuis le dashboard pour réinstaller ses fichiers.'
    Wait-FinOutil -SansPause:$SansPause
    exit 2
}

Write-Section 'Exécution'
$ancien = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $cible -Action reconnect
$code = [int]$LASTEXITCODE
$ErrorActionPreference = $ancien
Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('worker_tools.ps1 -Action reconnect terminé, code ' + $code)
Write-Host ''
if ($code -eq 0) { Write-Ok 'Demande de reconnexion transmise : vérifie l''état sur le site dans la minute.' }
elseif ($code -eq 2) { Write-Alerte 'Demande refusée ou annulée (code 2) : lis le message ci-dessus.' }
else { Write-Alerte ('Terminé avec le code ' + $code + ' : lis le message ci-dessus.') }
Wait-FinOutil -SansPause:$SansPause
if ($code -eq 0) { exit 0 } elseif ($code -eq 2) { exit 2 } else { exit 1 }
