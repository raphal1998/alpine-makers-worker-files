<#
    Supprimer les moteurs IA : enveloppe expliquée de worker_tools.ps1 -Action cleanup (racine du Worker).
    Remplace l'ancien « uninstall AI components.bat » de la racine, retiré du paquet en 1.21.3.

    Usage :
      supprimer_moteurs.ps1 [-Racine <dossier>] [-SansPause]
    Codes : 0 succès, 1 échec, 2 refusé, annulé ou fichier absent.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
Initialize-ConsoleWorker -Titre 'Supprimer les moteurs IA'
$Racine = Get-RacineWorker -Racine $Racine
$NomOutil = 'supprimer_moteurs'
$cible = Join-Path $Racine 'worker_tools.ps1'

Write-Explication -Titre 'SUPPRIMER LES MOTEURS IA (ZONE ROUGE)' -Fait @(
    'Affiche le Worker, son dossier, ses composants installés, le nombre de fichiers et le volume.',
    'Écrit la liste exhaustive dans runtime\local-control\cleanup-preview.json, puis exige la phrase SUPPRIMER IA.',
    'Envoie ensuite à l''agent la demande de suppression des composants (Worker allumé requis).'
) -NeFaitPas @(
    'Ne supprime ni l''identité, ni le journal, ni la configuration du Worker ; ne désinstalle pas le Worker lui-même.',
    'Aucune simulation : la seule protection est la phrase demandée. Refusé tant que le service du Worker est arrêté.'
)

if (-not (Test-Path -LiteralPath $cible -PathType Leaf)) {
    Write-Alerte ('worker_tools.ps1 est absent de la racine du Worker : ' + $cible)
    Write-Conseil 'Mets le Worker à jour depuis le dashboard pour réinstaller ses fichiers.'
    Wait-FinOutil -SansPause:$SansPause
    exit 2
}

Write-Section 'Exécution'
$ancien = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $cible -Action cleanup
$code = [int]$LASTEXITCODE
$ErrorActionPreference = $ancien
Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('worker_tools.ps1 -Action cleanup terminé, code ' + $code)
Write-Host ''
if ($code -eq 0) { Write-Ok 'Demande de suppression transmise à l''agent : suis l''avancement dans les journaux (3).' }
elseif ($code -eq 2) { Write-Alerte 'Demande refusée ou annulée (code 2) : lis le message ci-dessus.' }
else { Write-Alerte ('Terminé avec le code ' + $code + ' : lis le message ci-dessus.') }
Wait-FinOutil -SansPause:$SansPause
if ($code -eq 0) { exit 0 } elseif ($code -eq 2) { exit 2 } else { exit 1 }
