<#
    Actualiser les adresses IP : enveloppe expliquée de worker_tools.ps1 -Action network (racine du Worker).
    Remplace l'ancien « update worker IP.bat » de la racine, retiré du paquet en 1.21.3.

    Usage :
      actualiser_ip.ps1 [-Racine <dossier>] [-SansPause]
    Codes : 0 succès, 1 échec, 2 refusé, annulé ou fichier absent.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
Initialize-ConsoleWorker -Titre 'Actualiser les adresses IP'
$Racine = Get-RacineWorker -Racine $Racine
$NomOutil = 'actualiser_ip'
$cible = Join-Path $Racine 'worker_tools.ps1'

Write-Explication -Titre 'ACTUALISER LES ADRESSES IP' -Fait @(
    'Redétecte les adresses IPv4 de ce PC et invalide l''ancien inventaire réseau.',
    'L''agent renvoie le nouvel inventaire au dashboard à son prochain heartbeat (Worker allumé requis).'
) -NeFaitPas @(
    'Ne touche ni à l''identité du Worker, ni à Windows, ni à l''imprimante : celle-ci se reconfigure dans Équipements.'
)

if (-not (Test-Path -LiteralPath $cible -PathType Leaf)) {
    Write-Alerte ('worker_tools.ps1 est absent de la racine du Worker : ' + $cible)
    Write-Conseil 'Mets le Worker à jour depuis le dashboard pour réinstaller ses fichiers.'
    Wait-FinOutil -SansPause:$SansPause
    exit 2
}

Write-Section 'Exécution'
$ancien = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $cible -Action network
$code = [int]$LASTEXITCODE
$ErrorActionPreference = $ancien
Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('worker_tools.ps1 -Action network terminé, code ' + $code)
Write-Host ''
if ($code -eq 0) { Write-Ok 'Adresses réactualisées : le dashboard les recevra au prochain heartbeat.' }
elseif ($code -eq 2) { Write-Alerte 'Demande refusée ou annulée (code 2) : lis le message ci-dessus.' }
else { Write-Alerte ('Terminé avec le code ' + $code + ' : lis le message ci-dessus.') }
Wait-FinOutil -SansPause:$SansPause
if ($code -eq 0) { exit 0 } elseif ($code -eq 2) { exit 2 } else { exit 1 }
