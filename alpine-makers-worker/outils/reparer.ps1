<#
    Réparer le Worker (entrée 24 du menu) : les réparations rapides et sûres seulement.

    Usage :
        reparer.ps1                    contrôle, résumé, UNE question, puis les réparations rapides
        reparer.ps1 -Simulation        contrôle, résumé et liste de ce qui SERAIT lancé : rien n'est lancé
        reparer.ps1 -Oui -SansPause    sans clavier : lance les réparations rapides sans poser la question
    Codes de sortie : 0 = contrôle fait (réparé, ou rien à réparer) ; 1 = une réparation lancée a échoué,
    ou contrôle impossible ; 2 = tout a été refusé.

    Même moteur que « Contrôle total et réparation » (18), en périmètre réduit : cet outil lance
    controle_total.ps1 -Rapide, qui ne propose que les outils du menu de la classe rapide (rallumer le
    Worker, retirer une tâche planifiée orpheline ou en doublon) et les étapes sûres du plan du site.
    Aucune réparation lourde, aucune réinstallation des fichiers de l'agent, aucune élévation ici.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause,
    [switch]$Oui,
    [switch]$Simulation
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'commun_cycle.ps1')
. (Join-Path $PSScriptRoot 'commun_gestion.ps1')
Initialize-ConsoleWorker -Titre 'Réparer le Worker'
$NomOutil = 'reparer'

try {
    $Racine = Get-RacineWorker -Racine $Racine
} catch {
    Write-Erreur ([string]$_.Exception.Message)
    Exit-Outil -Code 1 -SansPause:$SansPause
}
$moteur = Join-Path $PSScriptRoot 'controle_total.ps1'
if (-not (Test-Path -LiteralPath $moteur -PathType Leaf)) {
    Write-Host ''
    Write-Erreur '« Réparer » a besoin de controle_total.ps1, absent du dossier outils de ce Worker.'
    Write-Conseil ('Mets le Worker à jour avec ' + (Get-NomEntree 23) + ', ou depuis le site : les outils manquants seront réinstallés.')
    Exit-Outil -Code 1 -SansPause:$SansPause
}
# Le moteur explique, questionne, répare, fait la pause de fin et rend le code de sortie.
$global:LASTEXITCODE = 0
try {
    & $moteur -Racine $Racine -Rapide -SansPause:$SansPause -Oui:$Oui -Simulation:$Simulation
} catch {
    Write-Erreur ('Erreur inattendue : ' + [string]$_.Exception.Message)
    Exit-Outil -Code 1 -SansPause:$SansPause
}
$code = 1
if ($null -ne $LASTEXITCODE) { $code = [int]$LASTEXITCODE }
if ($code -eq 64 -or $code -eq 75) { $code = 1 }
exit $code
