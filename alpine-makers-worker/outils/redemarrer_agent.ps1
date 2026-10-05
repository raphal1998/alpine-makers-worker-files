<#
    Redémarrer l'agent du Worker (le programme qui parle au site), sans rien éteindre durablement.

    Pilotage sans clavier :
        redemarrer_agent.ps1 -Simulation -Oui -SansPause      (aucun effet)
        redemarrer_agent.ps1 -Oui -SansPause [-Forcer]
    Codes de sortie : 0 succès, 1 échec, 2 refusé ou annulé, 3 élévation refusée.

    -DejaEleve est posé par Invoke-OutilEleve : l'instance administrateur ne fait QUE
    redémarrer le service (ou arrêter l'arbre du superviseur en mode tâche/manuel).
#>
param(
    [string]$Racine = '',
    [switch]$Simulation,
    [switch]$Oui,
    [switch]$Forcer,
    [switch]$SansPause,
    [switch]$DejaEleve
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'commun_cycle.ps1')
Initialize-ConsoleWorker -Titre 'Redémarrer l''agent du Worker'
$NomOutil = 'redemarrer_agent'

# Test-ServiceAutreDossier, Get-PresenceWorker, Confirm-Forcage, Get-MoteursOuverts : commun_cycle.ps1.

function Invoke-PhaseRedemarrage {
    <# Service : Restart-Service. Tâche/manuel : arrêt de l'arbre du superviseur par PID (la relance se fait SANS élévation, par l'appelant). Renvoie 0 ou 1. #>
    param([string]$Racine, $Mode)
    if ($Mode.Mode -eq 'service') {
        if (-not $Mode.Service.ViseCeDossier) { Write-Erreur 'Le service AlpineWorker ne vise pas ce dossier : il n''est pas touché.'; return 1 }
        $avant = Get-PidsDuWorker -Racine $Racine
        try { Restart-Service -Name 'AlpineWorker' -Force -ErrorAction Stop -WarningAction SilentlyContinue } catch { Write-Alerte ('Restart-Service : ' + $_.Exception.Message) }
        if (-not (Wait-Service -Nom 'AlpineWorker' -Etat 'Running' -DelaiSecondes 45)) {
            Write-Erreur 'Le service n''est pas revenu en marche après 45 secondes.'
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'ÉCHEC : service non revenu en marche après 45 s'
            return 1
        }
        Write-Ok 'Service AlpineWorker redémarré.'
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'service AlpineWorker redémarré'
        # Les processus de l'ANCIENNE instance qui auraient survécu gardent leurs ports : le nouvel agent ne pourrait pas relancer ses moteurs.
        $ancienSuperviseur = @($avant.Processus | Where-Object { $_.Role -eq 'superviseur' -and (Test-MemeProcessus -Entree $_) })
        if ($ancienSuperviseur.Count -gt 0) {
            Write-Erreur 'Le service se dit en marche, mais l''ancien superviseur n''a pas été remplacé : le redémarrage n''a pas eu lieu.'
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'ÉCHEC : ancien superviseur toujours vivant après Restart-Service'
            return 1
        }
        $survivants = @($avant.Processus | Where-Object { Test-MemeProcessus -Entree $_ })
        if ($survivants.Count -gt 0) {
            Write-Info ('Processus de l''ancienne instance encore vivants : ' + $survivants.Count + '. Arrêt par PID.')
            [void](Stop-PidsReconnus -Liste $survivants -Racine $Racine -Outil $NomOutil)
        }
        return 0
    }
    $vus = Get-PidsDuWorker -Racine $Racine
    if ($vus.Processus.Count -eq 0) { Write-Info 'Aucun processus du Worker à arrêter.'; return 0 }
    $restants = Stop-PidsReconnus -Liste $vus.Processus -Racine $Racine -Outil $NomOutil
    if ($restants -gt 0) { Write-Erreur ($restants.ToString() + ' processus résistent encore.'); return 1 }
    Write-Ok 'Ancien superviseur, agent et moteurs arrêtés.'
    return 0
}

try {
    $Racine = Get-RacineWorker -Racine $Racine
    $mode = Get-ModeLancement -Racine $Racine

    if ($DejaEleve) {
        Write-Cadre -Titre 'REDÉMARRER L''AGENT : phase administrateur' -Lignes @('Cette fenêtre se ferme seule ; la suite s''affiche dans la première.')
        if (-not (Test-Administrateur)) { Write-Erreur 'Cette fenêtre n''a pas les droits administrateur.'; exit 3 }
        $code = Invoke-PhaseRedemarrage -Racine $Racine -Mode $mode
        Exit-Outil -Code $code -SansPause
    }

    Write-Explication -Titre 'REDÉMARRER L''AGENT DU WORKER' -Fait @(
        'Arrête puis relance l''agent : utile quand le Worker reste « hors ligne », figé, ou après un changement de réseau.',
        'Vérifie d''abord qu''aucun travail n''est en cours.',
        'Attend le retour de l''agent et affiche son état.'
    ) -NeFaitPas @(
        'Il ne change ni l''identité, ni l''association, ni le mode de démarrage du Worker.',
        'Il ne touche à aucun autre service du PC.',
        'Il n''éteint pas durablement les moteurs : ceux qui étaient en marche repartent seuls, environ 30 secondes après l''agent.'
    ) -Exige @(
        'En mode service : une fenêtre administrateur (Windows te demandera ton accord).',
        'En mode tâche ou manuel : une fenêtre normale suffit le plus souvent.'
    )
    if ($Simulation) { Write-Alerte 'MODE SIMULATION : rien ne sera arrêté ni relancé.' }

    Write-Section 'Avant de redémarrer'
    Write-Etat -Libelle 'Dossier du Worker' -Valeur $Racine
    switch ($mode.Mode) {
        'service' { Write-Etat -Libelle 'Mode de lancement' -Valeur ('service Windows AlpineWorker : ' + $mode.Service.Etat + ', démarrage ' + $mode.Service.Demarrage) }
        'tache'   { Write-Etat -Libelle 'Mode de lancement' -Valeur 'tâche planifiée à l''ouverture de session' }
        default   { Write-Etat -Libelle 'Mode de lancement' -Valeur 'lancement manuel (ni service ni tâche pour ce dossier)' }
    }
    # Les ports sont globaux au PC : s'ils appartiennent au Worker d'un AUTRE dossier, on ne décide rien d'après eux.
    if (Test-ServiceAutreDossier -Mode $mode) { Exit-Outil -Code 2 -SansPause:$SansPause }
    $status = Invoke-ActionSure -Racine $Racine -Action 'status' -DelaiSecondes 10
    Show-Moteurs -Racine $Racine -Status $status
    $jobs = Get-JobsActifs -Racine $Racine -Status $status
    Show-JobsActifs -Jobs $jobs

    if ($mode.Mode -eq 'service' -and $mode.Service.Demarrage -ne 'Auto') {
        Write-Host ''
        Write-Alerte 'Ce Worker a été éteint volontairement (démarrage Manuel).'
        Write-Conseil ('Utilise ' + (Get-NomEntree 12) + ' : lui seul remet le démarrage automatique.')
        Exit-Outil -Code 2 -SansPause:$SansPause
    }
    if ($mode.Mode -eq 'tache' -and @($mode.Taches | Where-Object { $_.ViseCeDossier -and $_.Etat -ne 'Disabled' }).Count -eq 0) {
        # Tâche désactivée. Par « Éteindre le Worker » (mémo présent) : arrêt volontaire, on renvoie vers « Rallumer ».
        # Par toi (pas de mémo) : le Worker se lance alors à la main ; le redémarrage reste permis depuis une fenêtre normale.
        if (Test-Path -LiteralPath (Join-Path $Racine 'runtime\local-control\extinction.json') -PathType Leaf) {
            Write-Host ''
            Write-Alerte 'La tâche planifiée de ce Worker est désactivée : il a été éteint volontairement.'
            Write-Conseil ('Utilise ' + (Get-NomEntree 12) + ' : il réactive la tâche, puis relance le Worker.')
            Exit-Outil -Code 2 -SansPause:$SansPause
        }
        Write-Info '      La tâche planifiée de ce dossier est désactivée : le Worker sera relancé directement, sans elle.'
        if (Test-Administrateur) {
            Write-Host ''
            Write-Erreur 'Cette fenêtre est administrateur et la tâche planifiée est désactivée : l''agent relancé tournerait avec des droits administrateur.'
            Write-Conseil 'Ferme-la, puis relance cet outil depuis une fenêtre normale (double-clic).'
            Exit-Outil -Code 1 -SansPause:$SansPause
        }
    }
    # Depuis le menu (ni -Oui ni -SansPause), un refus propose le forçage au lieu de finir en impasse :
    # c'est justement le cas d'un travail resté « en cours » alors que son annulation est déjà demandée.
    $interactif = (-not $Oui -and -not $SansPause)
    $forcageConfirme = $false
    if ($status.Ok -and $status.Resultat.maintenance_active -and -not $Forcer) {
        Write-Host ''
        Write-Erreur 'Une installation, une mise à jour ou un nettoyage est en cours : redémarrage refusé.'
        Write-Conseil 'Attends sa fin. Ne passe outre que si elle est bloquée depuis longtemps.'
        if (-not (Confirm-Forcage -Consequence 'le moteur en cours d''installation peut rester à moitié installé' -Lanceur 'redemarrer_agent.bat' -Interactif $interactif)) { Write-Info 'Rien n''a changé.'; Exit-Outil -Code 2 -SansPause:$SansPause }
        $Forcer = $true; $forcageConfirme = $true
    }
    if ($jobs.Connu -and $jobs.Nombre -gt 0) {
        Write-Host ''
        if (-not $Forcer) {
            Write-Erreur 'Redémarrage refusé : un travail est en cours, il serait perdu.'
            Write-Conseil ('Attends sa fin ou annule-le dans le site. ' + (Get-NomEntree 2) + ' montre ce qui bloque.')
            if (-not (Confirm-Forcage -Consequence 'le travail en cours sera PERDU' -Lanceur 'redemarrer_agent.bat' -Interactif $interactif)) { Write-Info 'Rien n''a changé.'; Exit-Outil -Code 2 -SansPause:$SansPause }
            $Forcer = $true; $forcageConfirme = $true
        }
        elseif (-not $forcageConfirme) {
            Write-Alerte 'Tu as demandé -Forcer : le travail en cours sera PERDU.'
            if (-not (Confirm-Action -Question 'Redémarrer malgré le travail en cours ?' -MotExact 'FORCER' -Oui:$Oui)) { Write-Info 'Annulé : rien n''a changé.'; Exit-Outil -Code 2 -SansPause:$SansPause }
        }
    }
    Write-Host ''
    if (-not (Confirm-Action -Question 'Redémarrer l''agent maintenant ?' -Oui:$Oui)) { Write-Info 'Annulé : rien n''a changé.'; Exit-Outil -Code 2 -SansPause:$SansPause }

    $tacheCible = @($mode.Taches | Where-Object { $_.ViseCeDossier -and $_.Etat -ne 'Disabled' }) | Select-Object -First 1
    if ($mode.Mode -eq 'aucun' -and (Test-Administrateur)) {
        Write-Erreur 'Cette fenêtre est administrateur : l''agent relancé tournerait avec des droits administrateur.'
        Write-Conseil 'Ferme-la, puis relance cet outil depuis une fenêtre normale (double-clic).'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }

    if ($Simulation) {
        Write-Section 'Ce qui serait fait'
        if ($mode.Mode -eq 'service') {
            Write-Info '[simulation] 1. ouverture d''une fenêtre administrateur (accord Windows demandé).'
            Write-Info '[simulation] 2. Restart-Service AlpineWorker -Force, attente de l''état Running (45 s).'
            Write-Info '[simulation] 3. arrêt, par PID reconnu, des processus de l''ancienne instance qui auraient survécu.'
        } else {
            Write-Info '[simulation] 1. arrêt de l''arbre du superviseur (run_worker.ps1 de ce dossier), par PID reconnu :'
            $vus = Get-PidsDuWorker -Racine $Racine
            if ($vus.Processus.Count -gt 0) { [void](Stop-PidsReconnus -Liste $vus.Processus -Racine $Racine -Outil $NomOutil -Simulation) }
            else {
                $presenceSimulee = Get-PresenceWorker -Racine $Racine -Status $status
                Write-Info ('[simulation]    aucun processus de ce dossier lisible d''ici. Ce dossier tourne-t-il ? Réponse : ' + $presenceSimulee.Etat + '.')
                Write-Info '[simulation]    Fenêtre administrateur seulement si la réponse est « illisible ».'
            }
            Write-Info '[simulation] 2. relance cachée de run_worker.ps1, SANS droits administrateur.'
        }
        Write-Info '[simulation] 4. attente de l''agent (90 s au plus), puis affichage de son état.'
        Write-Host ''
        Write-Ok 'Simulation terminée : rien n''a été arrêté ni relancé.'
        Exit-Outil -Code 0 -SansPause:$SansPause
    }

    Write-Section 'Étape 1 : redémarrage'
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('début : mode ' + $mode.Mode + ', forcer = ' + [bool]$Forcer)
    $besoinElevation = ($mode.Mode -eq 'service')
    if (-not $besoinElevation -and -not (Test-Administrateur)) {
        # Preuves propres à CE dossier d'abord (processus reconnus, réponse à status) ; les ports, globaux,
        # ne comptent que si des processus sont illisibles.
        $presence = Get-PresenceWorker -Racine $Racine -Status $status
        if ($presence.Etat -eq 'illisible' -or ($presence.Etat -eq 'oui' -and -not $presence.Lisible)) { Write-Info 'Le Worker tourne, mais ses processus sont illisibles sans droits administrateur.'; $besoinElevation = $true }
        elseif ($presence.Etat -eq 'non') { Write-Info 'Aucun processus de ce Worker ne tourne : il sera simplement lancé.' }
    }
    $code = 0
    if ($besoinElevation -and -not (Test-Administrateur)) {
        Write-Info 'Windows va te demander ton accord pour ouvrir une fenêtre administrateur...'
        $code = Invoke-OutilEleve -Script $PSCommandPath -Arguments (Get-ArgumentsCommuns -Racine $Racine)
        if ($null -eq $code) {
            Write-Erreur 'Tu as refusé la fenêtre administrateur : l''agent n''a pas été redémarré.'
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'élévation refusée'
            Exit-Outil -Code 3 -SansPause:$SansPause
        }
    } else {
        $code = Invoke-PhaseRedemarrage -Racine $Racine -Mode $mode
    }
    if ($code -ne 0) {
        Write-Erreur 'Le redémarrage a échoué.'
        if ($besoinElevation) { Show-TraceOutil -Racine $Racine -Outil $NomOutil }
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    if ($mode.Mode -eq 'service') { Write-Ok 'Service AlpineWorker redémarré.' }
    else {
        # Relance TOUJOURS depuis cette instance non élevée (ou par la tâche planifiée si la fenêtre est administrateur).
        if ($mode.Mode -eq 'tache' -and (Test-Administrateur)) { Start-ScheduledTask -TaskName $tacheCible.Nom -TaskPath $tacheCible.Chemin -ErrorAction Stop; Write-Ok 'Tâche planifiée relancée.' }
        else { Start-SuperviseurCache -Racine $Racine; Write-Ok 'Superviseur du Worker relancé en arrière-plan (sans droits administrateur).' }
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'superviseur relancé'
    }

    Write-Section 'Étape 2 : attente de l''agent (90 secondes au plus)'
    # Relevé juste APRÈS l'arrêt de l'ancienne instance : un moteur encore ouvert maintenant lui a survécu
    # (orphelin), il ne prouve donc rien. Ceux que le nouvel agent relance n'ouvrent leur port que 30 s plus tard.
    $moteursDejaOuverts = @(Get-MoteursOuverts -Racine $Racine)
    $agent = Wait-Agent -Racine $Racine -DelaiSecondes 90 -MoteursDejaOuverts $moteursDejaOuverts
    $codeFinal = 0
    switch ($agent) {
        'confirme' { Write-Ok 'L''agent répond.' }
        'probable' { Write-Ok 'L''agent semble reparti (il est trop ancien pour répondre directement aux outils).' }
        default    { Write-Alerte ('Aucun signe de l''agent en 90 secondes. Vérifie dans le site qu''il repasse « en ligne », sinon lance ' + (Get-NomEntree 1) + ', puis ' + (Get-NomEntree 3) + '.'); $codeFinal = 1 }
    }

    Write-Section 'État après redémarrage'
    $status = Invoke-ActionSure -Racine $Racine -Action 'status' -DelaiSecondes 10
    if ($status.Ok) { Write-Etat -Libelle 'Agent' -Valeur ('en marche, version ' + [string]$status.Resultat.agent_version) -Niveau ok }
    Show-Moteurs -Racine $Racine -Status $status
    Write-Conseil 'Les moteurs qui étaient en marche repartent seuls environ 30 secondes après l''agent ; un gros modèle met plusieurs minutes.'
    Write-Conseil ('Pour suivre leur réveil : ' + (Get-NomEntree 9) + ', choix 1.')
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('fin : agent = ' + $agent)
    Exit-Outil -Code $codeFinal -SansPause:$SansPause
} catch {
    Write-Erreur ('Erreur inattendue : ' + $_.Exception.Message)
    if (-not $Simulation) { try { Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('erreur inattendue : ' + $_.Exception.Message) } catch { } }
    Exit-Outil -Code 1 -SansPause:$SansPause
}
