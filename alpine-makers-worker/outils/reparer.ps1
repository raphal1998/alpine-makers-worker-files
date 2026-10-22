<#
    Réparer le Worker (entrée 24 du menu) : diagnostic EN PROFONDEUR, puis toutes les réparations sûres.

    Usage :
        reparer.ps1                    diagnostic profond, UNE question pour ses réparations, puis le contrôle rapide
                                       (une question) et ses réparations
        reparer.ps1 -Simulation        tout le diagnostic et la liste de ce qui SERAIT réparé : rien n'est lancé
        reparer.ps1 -Oui -SansPause    sans clavier : lance toutes les réparations sûres sans poser de question
    Codes de sortie : 0 = contrôle fait (réparé, ou rien à réparer) ; 1 = une réparation lancée a échoué,
    ou contrôle impossible ; 2 = tout a été refusé.

    1. Diagnostic profond (agent 1.50.33), qui marche même agent arrêté ou figé :
         - processus : agent figé (vivant mais muet), doublons d'agent ou de superviseur, processus du Worker restés
           seuls sans agent, verrou du superviseur tenu par un processus invisible, ports des moteurs pris par un
           AUTRE programme, service Windows désactivé ;
         - fichiers (outils\diagnostic_profond.py) : Python, config.json, bibliothèque de sécurité, fichiers d'état
           abîmés, journal des jobs (intégrité SQLite), boîte locale périmée, verrous orphelins, moteurs et
           lecteurs de stockage absents, disque, jobs anciens, et la passe « inconnu » : les erreurs et traces qui
           reviennent dans les journaux récents, regroupées, avec une piste quand elle est connue.
       Réparé sans risque : agent figé ou en doublon redémarré (« Redémarrer l'agent », 10, dans une fenêtre à part),
       processus restés seuls arrêtés (par PID reconnu), fichiers d'état abîmés, boîte locale et verrous orphelins
       DÉPLACÉS dans runtime\sauvegardes-reparation (jamais effacés), bibliothèque de sécurité réinstallée.
       Jamais une donnée utilisateur, un modèle, la configuration ni le journal des jobs.
    2. Contrôle rapide : controle_total.ps1 -Rapide, qui ne propose que les outils du menu de la classe rapide
       (rallumer le Worker, retirer une tâche planifiée orpheline ou en doublon) et les étapes sûres du plan du site.
       Aucune réparation lourde, aucune réinstallation des fichiers de l'agent ici.
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

function New-PointProfond {
    param([string]$Code, [string]$Gravite, [string]$Titre, [string]$Detail = '', [string]$Reparation = '', [string]$Manuel = '')
    return [pscustomobject]@{ code = $Code; gravite = $Gravite; titre = $Titre; detail = $Detail; reparable = [bool]$Reparation; reparation = $Reparation; manuel = $Manuel }
}

function Get-PointsProcessus {
    <#
        Contrôles des processus et du lancement, lus SANS élévation. Rend { Points, Agents, Orphelins }.
        Agent figé : l'agent noté dans runtime\agent-process.json (ou reconnu par sa ligne de commande) vit depuis plus
        de deux minutes et ne répond pas à deux « status » de suite.
    #>
    param([string]$Racine, $Mode, $EtatAgent)
    $points = @()
    $vus = Get-PidsDuWorker -Racine $Racine
    $agents = @($vus.Processus | Where-Object { $_.Role -eq 'agent' })
    $vieux = $false
    foreach ($a in $agents) { if ($a.Creation -and ((Get-Date) - [datetime]$a.Creation).TotalSeconds -gt 120) { $vieux = $true } }
    $fiche = @(Get-FichesProcessusWorker -Racine $Racine | Where-Object { $_.Role -eq 'agent' }) | Select-Object -First 1
    if ($fiche -and -not $vieux) {
        $cim = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$fiche.Pid) -ErrorAction SilentlyContinue
        if ($cim -and (Test-IdentiteProcessus -Processus $cim -Identite $fiche.Identite) -and $cim.CreationDate -and ((Get-Date) - [datetime]$cim.CreationDate).TotalSeconds -gt 120) { $vieux = $true; if ($agents.Count -eq 0) { $agents = @($cim) } }
    }
    $muet = -not $EtatAgent.Vivant
    if ($muet -and $vieux) {
        # Seconde chance : un agent très occupé répond parfois en retard.
        $encore = Get-EtatAgent -Racine $Racine -DelaiSecondes 10
        $muet = -not $encore.Vivant
    }
    $orphelins = @()
    if ($muet -and $vieux) {
        $points += New-PointProfond -Code 'P-AGENT-FIGE' -Gravite 'erreur' -Titre 'L''agent tourne depuis plus de deux minutes mais ne répond plus : il est figé.' -Detail 'Un agent figé ne reçoit plus de jobs et ne pilote plus ses moteurs ; les calculs qu''il suivait seront perdus.' -Reparation ('le redémarrer avec ' + (Get-NomEntree 10) + ' (fenêtre à part, administrateur si le Worker est un service)')
    }
    elseif ($agents.Count -eq 0 -and $muet) {
        $orphelins = @($vus.Processus | Where-Object { $_.Role -ne 'superviseur' })
        if ($orphelins.Count -gt 0) {
            $noms = (@($orphelins | ForEach-Object { [string]$_.Nom } | Select-Object -Unique) -join ', ')
            $points += New-PointProfond -Code 'P-PROCESSUS-SEULS' -Gravite 'alerte' -Titre ([string]$orphelins.Count + ' processus du Worker tournent sans agent : ' + $noms) -Detail 'Ils tiennent les ports et la mémoire des moteurs : le Worker relancé ne pourrait pas redémarrer ses moteurs.' -Reparation 'les arrêter, par PID reconnu (ce sont des processus de CE Worker uniquement)'
        }
    }
    $doublons = Get-DoublonsWorker -Processus $vus
    if ($doublons.Doublon -and -not ($muet -and $vieux)) {
        $points += New-PointProfond -Code 'P-DOUBLONS' -Gravite 'alerte' -Titre ('Doublon : ' + $doublons.Superviseurs.Count + ' superviseur(s) et ' + $doublons.Agents.Count + ' agent(s) de ce dossier tournent en même temps.') -Detail 'Deux agents se disputent les mêmes moteurs, ports et jobs.' -Reparation ('tout arrêter puis relancer un seul agent avec ' + (Get-NomEntree 10))
    }
    $superviseurs = @($vus.Processus | Where-Object { $_.Role -eq 'superviseur' })
    if ($Mode.Mode -ne 'service' -and $superviseurs.Count -eq 0 -and $vus.Illisibles -eq 0 -and -not (Test-VerrouSuperviseurLibre -Racine $Racine)) {
        $points += New-PointProfond -Code 'P-VERROU-TENU' -Gravite 'alerte' -Titre 'supervisor.lock est tenu, mais aucun superviseur de ce dossier n''est visible d''ici.' -Detail 'Un superviseur lancé sous un autre compte (ou en administrateur) empêche tout nouveau démarrage.' -Manuel ((Get-NomEntree 10) + ' depuis une fenêtre administrateur (elle voit tous les processus), sinon redémarre le PC.')
    }
    if ($Mode.Service -and $Mode.Service.ViseCeDossier -and [string]$Mode.Service.Demarrage -eq 'Disabled' -and -not (Test-Path -LiteralPath (Join-Path $Racine 'runtime\local-control\extinction.json') -PathType Leaf)) {
        $points += New-PointProfond -Code 'P-SERVICE-DESACTIVE' -Gravite 'erreur' -Titre 'Le service Windows AlpineWorker est DÉSACTIVÉ, et pas par « Éteindre le Worker ».' -Detail 'Il ne démarrera plus, même après un redémarrage du PC.' -Manuel ((Get-NomEntree 12) + ' le remet en démarrage automatique et le démarre.')
    }
    $autreService = ($Mode.Service -and -not $Mode.Service.ViseCeDossier -and ([string]$Mode.Service.Etat -eq 'Running'))
    if (-not $autreService) {
        foreach ($o in @(Get-PortsOccupes -Racine $Racine -Processus $vus)) {
            $points += New-PointProfond -Code 'P-PORT-PRIS' -Gravite 'erreur' -Titre ('Le port ' + $o.Port + ' (' + $o.Libelle + ') est pris par un AUTRE programme : ' + $o.Programme + ' (PID ' + $o.Pid + ').') -Detail 'Ce moteur ne peut pas démarrer tant que ce programme garde le port.' -Manuel 'Ferme ce programme (ou change son port). Les outils du Worker n''arrêtent jamais un programme qui n''est pas le leur.'
        }
    }
    return [pscustomobject]@{ Points = $points; Orphelins = $orphelins }
}

function Write-PointProfond {
    param($Point)
    $texte = '[' + [string]$Point.code + '] ' + [string]$Point.titre
    switch ([string]$Point.gravite) {
        'erreur' { Write-Erreur $texte }
        'alerte' { Write-Alerte $texte }
        default  { Write-Info ('  ' + $texte) }
    }
    if ([string]$Point.detail) { Write-Info ('      ' + [string]$Point.detail) }
    if ([bool]$Point.reparable) { Write-Conseil ('Réparation sûre : ' + [string]$Point.reparation) }
    elseif ([string]$Point.manuel) { Write-Conseil ('À faire : ' + [string]$Point.manuel) }
}

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

# ------------------------------------------------------------ 1. diagnostic profond
$echecProfond = $false
try {
    Write-Section 'Diagnostic profond'
    Write-Info 'Recherche de ce qui empêche le Worker de bien marcher, même ce que l''état complet ne connaît pas : processus, fichiers d''état, journal, verrous, disque, erreurs qui reviennent dans les journaux...'
    $mode = Get-ModeLancement -Racine $Racine
    $etatAgent = Get-EtatAgent -Racine $Racine -DelaiSecondes 8
    $processus = Get-PointsProcessus -Racine $Racine -Mode $mode -EtatAgent $etatAgent
    $avecService = [bool]($mode.Service -and $mode.Service.ViseCeDossier)
    $fichiers = Invoke-DiagnosticProfond -Racine $Racine -JournauxService:$avecService
    $points = @($processus.Points) + @($fichiers.Points)
    if (-not $fichiers.Ok) { Write-Alerte ('Contrôle des fichiers incomplet : ' + $fichiers.Erreur) }
    $ordre = @{ 'erreur' = 0; 'alerte' = 1; 'info' = 2 }
    $points = @($points | Sort-Object @{ Expression = { if ($ordre.ContainsKey([string]$_.gravite)) { $ordre[[string]$_.gravite] } else { 3 } } })
    if ($points.Count -eq 0) { Write-Ok 'Aucun problème trouvé par le diagnostic profond.' }
    foreach ($p in $points) { Write-PointProfond -Point $p }

    $reparables = @($points | Where-Object { [bool]$_.reparable })
    if ($reparables.Count -gt 0) {
        Write-Section 'Réparations profondes proposées'
        $i = 0
        foreach ($p in $reparables) { $i++; Write-Info ([string]$i + '. [' + [string]$p.code + '] ' + [string]$p.reparation) }
        Write-Info 'Rien n''est effacé : les fichiers retirés sont gardés dans runtime\sauvegardes-reparation.'
        $accord = $false
        if ($Simulation) { Write-Info '[simulation] Ces réparations ne sont pas lancées.' }
        else { $accord = Confirm-Action -Question 'Lancer ces réparations profondes ?' -Oui:$Oui }
        if ($accord) {
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('réparations profondes : ' + (@($reparables | ForEach-Object { [string]$_.code }) -join ','))
            $faits = @(); $rates = @()
            $codesFichiers = @($reparables | Where-Object { [string]$_.code -like 'D-*' } | ForEach-Object { [string]$_.code })
            if ($codesFichiers.Count -gt 0) {
                $retour = Invoke-DiagnosticProfond -Racine $Racine -Reparer $codesFichiers -JournauxService:$avecService
                if (-not $retour.Ok -and @($retour.Reparations).Count -eq 0) { $rates += ('fichiers : ' + $retour.Erreur) }
                foreach ($rep in @($retour.Reparations)) {
                    if ([bool]$rep.ok) { $faits += ('[' + [string]$rep.code + '] ' + [string]$rep.message) } else { $rates += ('[' + [string]$rep.code + '] ' + [string]$rep.message) }
                }
            }
            $codesProcessus = @($reparables | ForEach-Object { [string]$_.code })
            if ($codesProcessus -contains 'P-PROCESSUS-SEULS') {
                $bilan = Stop-ToutLeWorker -Racine $Racine -Outil $NomOutil -Deja @($processus.Orphelins)
                if (@($bilan.Survivants).Count -eq 0) { $faits += ('[P-PROCESSUS-SEULS] ' + $bilan.Arretes + ' processus resté(s) seul(s) arrêté(s).') }
                else { $rates += ('[P-PROCESSUS-SEULS] ' + @($bilan.Survivants).Count + ' processus résistent.'); Show-Survivants -Survivants $bilan.Survivants -Illisibles $bilan.Illisibles -Administrateur:(Test-Administrateur) }
            }
            if ($codesProcessus -contains 'P-AGENT-FIGE' -or $codesProcessus -contains 'P-DOUBLONS') {
                $arguments = @('-Racine', $Racine, '-Oui', '-SansPause')
                if ($codesProcessus -contains 'P-AGENT-FIGE') { $arguments += '-Forcer' }   # un agent figé ne finira jamais ses calculs
                if ($env:ALPINE_WORKER_GUI -eq '1') { Write-Info 'Laisse-le aller au bout : l''interrompre (ou fermer ce panneau) pendant qu''il remet le Worker en marche peut arrêter le Worker avec lui.' }
                $codeOutil = Invoke-OutilEnfant -Script (Join-Path $PSScriptRoot 'redemarrer_agent.ps1') -Arguments $arguments
                if ($codeOutil -eq 0) { $faits += ('« Redémarrer l''agent » (10) : agent relancé.') }
                elseif ($codeOutil -eq 3) { $rates += '« Redémarrer l''agent » (10) : fenêtre administrateur refusée.' }
                else { $rates += ('« Redémarrer l''agent » (10) : échec (code ' + $codeOutil + ').') }
            }
            Write-Section 'Bilan du diagnostic profond'
            foreach ($f in $faits) { Write-Ok ('Réparé : ' + $f) }
            foreach ($f in $rates) { Write-Erreur ('Non réparé : ' + $f) }
            if ($rates.Count -gt 0) { $echecProfond = $true }
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('réparations profondes : ' + $faits.Count + ' faite(s), ' + $rates.Count + ' en échec')
        }
        elseif (-not $Simulation) { Write-Info 'Réparations profondes non lancées.' }
    }
    $manuels = @($points | Where-Object { -not [bool]$_.reparable -and [string]$_.gravite -ne 'info' })
    if ($manuels.Count -gt 0) {
        Write-Section 'À faire à la main (diagnostic profond)'
        foreach ($p in $manuels) { Write-Info ('[' + [string]$p.code + '] ' + [string]$p.manuel) }
    }
} catch {
    Write-Alerte ('Diagnostic profond interrompu : ' + [string]$_.Exception.Message + '. Le contrôle rapide continue.')
}

# ------------------------------------------------------------ 2. contrôle rapide (moteur commun avec 18)
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
if ($echecProfond -and $code -ne 1) { $code = 1 }
exit $code
