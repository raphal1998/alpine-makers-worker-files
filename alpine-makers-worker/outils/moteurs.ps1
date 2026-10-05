<#
    Moteurs du Worker : voir leur état, les arrêter proprement, les relancer.

    Pilotage sans clavier :
        moteurs.ps1 -Action etat -SansPause
        moteurs.ps1 -Action arreter  -Oui -SansPause [-Simulation]
        moteurs.ps1 -Action demarrer -Oui -SansPause [-Simulation]
    Codes de sortie : 0 succès, 1 échec, 2 refusé ou annulé.
#>
param(
    [string]$Racine = '',
    [ValidateSet('', 'etat', 'arreter', 'demarrer')][string]$Action = '',
    [switch]$Simulation,
    [switch]$Oui,
    [switch]$SansPause
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'commun_cycle.ps1')
Initialize-ConsoleWorker -Titre 'Moteurs du Worker'
$NomOutil = 'moteurs'

try {
    $Racine = Get-RacineWorker -Racine $Racine

    Write-Explication -Titre 'MOTEURS DU WORKER : ÉTAT, ARRÊT PROPRE, RELANCE' -Fait @(
        'Montre quels moteurs tournent : ComfyUI (images), Hunyuan3D (3D), PrintGuard, bridge Orca.',
        'Arrêter : demande à l''agent d''éteindre ses moteurs proprement. La mémoire de la carte graphique est rendue.',
        'Démarrer : demande à l''agent de relancer les moteurs arrêtés par « Arrêter », puis suit leur réveil pendant 3 minutes au plus.',
        '   Sans arrêt préalable, l''agent relancerait TOUS les moteurs installés : l''outil prévient et redemande avant.'
    ) -NeFaitPas @(
        'Il n''arrête pas le Worker lui-même : le PC reste visible « en ligne » dans le site.',
('Il ne tue jamais un processus de force. Pour tout éteindre, utilise ' + (Get-NomEntree 11) + '.'),
        'Il ne lance aucune génération et n''envoie rien aux machines.'
    ) -Exige @(
        'Un Worker en marche et à jour (les commandes locales datent de la dernière version de l''agent).',
        'Aucun droit administrateur.'
    )

    if (-not $Action) {
        if ($SansPause -or $Oui) { $Action = 'etat' }
        else {
            Write-Info '1. Voir l''état des moteurs'
            Write-Info '2. Arrêter les moteurs proprement'
            Write-Info '3. Relancer les moteurs'
            $choix = Read-Host '   Ton choix [1-3, Entrée = 1]'
            switch ($choix) { '2' { $Action = 'arreter' } '3' { $Action = 'demarrer' } default { $Action = 'etat' } }
        }
    }

    Write-Section 'État actuel'
    # Les ports des moteurs sont GLOBAUX au PC : si un service AlpineWorker en marche sert un AUTRE dossier,
    # les « en marche » ci-dessous décrivent SES moteurs, pas ceux de ce dossier-ci.
    $modeLancement = Get-ModeLancement -Racine $Racine
    if ($modeLancement.Service -and -not $modeLancement.Service.ViseCeDossier -and ([string]$modeLancement.Service.Etat -eq 'Running')) {
        Write-Alerte 'Un autre Worker tourne sur ce PC, en service Windows, depuis un AUTRE dossier :'
        Write-Info ('      ' + [string]$modeLancement.Service.Dossier)
        Write-Info '      Les ports ouverts ci-dessous sont les siens. Pour agir sur ses moteurs, utilise les outils de ce dossier-là.'
    }
    # Worker éteint : on le sait sans interroger l'agent (et sans attendre 10 s pour rien).
    $serviceDuDossier = ($modeLancement.Service -and $modeLancement.Service.ViseCeDossier)
    $workerEteint = ($serviceDuDossier -and ([string]$modeLancement.Service.Etat -ne 'Running') -and -not (Test-AgentVivant))
    if ($workerEteint) { $status = [pscustomobject]@{ Ok = $false; Resultat = $null; Erreur = 'Worker éteint.'; Code = -1; NonSupporte = $false; DelaiDepasse = $false; EnCours = $false; EnAttente = $false } }
    else { $status = Invoke-ActionSure -Racine $Racine -Action 'status' -DelaiSecondes 10 }
    Show-Moteurs -Racine $Racine -Status $status
    if ($workerEteint) {
        Write-Etat -Libelle 'Agent' -Valeur 'arrêté (Worker éteint : service AlpineWorker non démarré)' -Niveau alerte
        if (@(Get-MoteursOuverts -Racine $Racine).Count -gt 0) { Write-Alerte ('Des moteurs écoutent encore alors que le Worker est éteint (orphelins) : relance ' + (Get-NomEntree 11) + ', sa fenêtre administrateur les arrête.') }
        Write-Host ''
        Write-Conseil ('Rallume-le avec ' + (Get-NomEntree 12) + '. Les moteurs se pilotent ensuite d''ici.')
        $codeEteint = 0
        if ($Action -ne 'etat') { Write-Alerte 'Rien n''a été demandé : il n''y a pas d''agent pour arrêter ou relancer les moteurs.'; $codeEteint = 1 }
        Exit-Outil -Code $codeEteint -SansPause:$SansPause
    }
    if ($status.Ok) {
        Write-Etat -Libelle 'Agent' -Valeur ('en marche, version ' + [string]$status.Resultat.agent_version) -Niveau ok
        if ($status.Resultat.maintenance_active) { Write-Etat -Libelle 'Maintenance' -Valeur 'une installation ou un nettoyage est en cours' -Niveau alerte }
        Show-JobsActifs -Jobs (Get-JobsActifs -Racine $Racine -Status $status)
    } elseif ($status.NonSupporte) {
        Write-Etat -Libelle 'Agent' -Valeur ('version ' + (Get-VersionInstallee -Racine $Racine) + ' : commandes locales absentes') -Niveau alerte
    } else {
        Write-Etat -Libelle 'Agent' -Valeur 'ne répond pas (arrêté, ou encore en démarrage)' -Niveau alerte
    }

    if ($Action -eq 'etat') {
        if ($status.NonSupporte) { Write-Host ''; Write-ConseilMiseAJour }
        Exit-Outil -Code 0 -SansPause:$SansPause
    }

    # ------------------------------------------------------------------ arrêter / démarrer
    if ($status.NonSupporte) {
        Write-Host ''
        Write-Alerte 'Cet agent ne sait pas encore arrêter ou relancer ses moteurs à la demande d''un outil local.'
        Write-ConseilMiseAJour
        Write-Conseil 'En attendant : dans le site, ouvre ce Worker et utilise le bouton « Tout arrêter » (ou le bouton de chaque moteur).'
        Write-Conseil 'Rien n''a été arrêté de force : un arrêt brutal laisserait l''agent relancer les moteurs 30 secondes plus tard.'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    if (-not $status.Ok) {
        Write-Host ''
        Write-Alerte 'L''agent ne répond pas : il est arrêté, ou il démarre encore.'
        Write-Conseil ('S''il est arrêté, lance ' + (Get-NomEntree 12) + '. S''il vient de démarrer, réessaie dans une minute.')
        if ($status.Erreur) { Write-Info ('Détail : ' + $status.Erreur) }
        Exit-Outil -Code 1 -SansPause:$SansPause
    }

    if ($Action -eq 'arreter') {
        if (-not (Confirm-Action -Question 'Arrêter proprement tous les moteurs de ce Worker ?' -Oui:$Oui)) { Write-Info 'Annulé : rien n''a changé.'; Exit-Outil -Code 2 -SansPause:$SansPause }
        if ($Simulation) {
            Write-Section 'Simulation'
            Write-Info '[simulation] demande « engines-stop » à l''agent (délai 90 s). Rien n''a été envoyé.'
            Exit-Outil -Code 0 -SansPause:$SansPause
        }
        Write-Section 'Arrêt en cours (90 secondes au plus)'
        Write-Info 'Demande envoyée à l''agent. Un point s''affiche toutes les 5 secondes : ne ferme pas la fenêtre.'
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'demande engines-stop'
        $r = Invoke-ActionSure -Racine $Racine -Action 'engines-stop' -DelaiSecondes 90
        if (-not $r.Ok -and $r.EnCours) {
            # Ce n'est PAS un refus : l'agent a pris la demande et n'a pas fini. On suit les ports.
            Write-Info 'L''agent a bien pris la demande mais n''a pas fini en 90 secondes : suivi des ports (90 s de plus).'
            $ouverts = @(Wait-MoteursFermes -Racine $Racine -DelaiSecondes 90)
            Write-Section 'Vérification des ports'
            Show-Moteurs -Racine $Racine
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('engines-stop encore en cours après 90 s ; encore ouverts = ' + ($ouverts -join ','))
            if ($ouverts.Count -eq 0) { Write-Ok 'Tous les moteurs sont fermés.'; Write-Conseil 'L''agent a noté quels moteurs tournaient : le choix 3 « Relancer les moteurs » les rallumera.'; Exit-Outil -Code 0 -SansPause:$SansPause }
            Write-Alerte ('Encore ouverts : ' + ($ouverts -join ', ') + '. L''agent y travaille peut-être encore : relance cet outil (choix 1) dans une minute.')
            Exit-Outil -Code 1 -SansPause:$SansPause
        }
        if (-not $r.Ok) {
            if ($r.DelaiDepasse) { Write-Erreur ('Aucune réponse de l''agent : la demande a été retirée, rien ne sera arrêté plus tard. ' + $r.Erreur) }
            else {
                Write-Erreur ('L''agent a refusé : ' + $r.Erreur)
                Write-Conseil ('Un refus vient d''un travail en cours ou d''une installation : attends sa fin, ou annule-le dans le site. ' + (Get-NomEntree 2) + ' montre ce qui bloque.')
            }
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('engines-stop en échec : ' + $r.Erreur)
            Exit-Outil -Code 1 -SansPause:$SansPause
        }
        $arretes = @($r.Resultat.stopped)
        $erreurs = @($r.Resultat.errors)
        if ($arretes.Count) { Write-Ok ('Moteurs arrêtés : ' + ($arretes -join ', ')) } else { Write-Info 'Aucun moteur ne tournait.' }
        foreach ($e in $erreurs) { if ($e) { Write-Erreur ('Erreur : ' + (($e | ConvertTo-Json -Compress -Depth 3))) } }
        Write-Section 'Vérification des ports'
        Show-Moteurs -Racine $Racine
        Write-Conseil 'L''agent a noté quels moteurs tournaient : le choix 3 « Relancer les moteurs » de cet outil les rallumera.'
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('engines-stop : arrêtés = ' + ($arretes -join ',') + ' ; erreurs = ' + $erreurs.Count)
        $code = 0
        if ($erreurs.Count -gt 0) { $code = 1 }
        Exit-Outil -Code $code -SansPause:$SansPause
    }

    if ($Action -eq 'demarrer') {
        # Ce que l'agent relancerait, dit AVANT de confirmer.
        $memo = Get-MemoMoteurs -Status $status
        $suivreSeulement = $false
        Write-Section 'Ce qui sera relancé'
        if ($memo.EnCours) {
            Write-Info 'L''agent est DÉJÀ en train de démarrer des moteurs : rien n''est redemandé, on suit leurs ports.'
            $suivreSeulement = $true
        } elseif ($memo.Present -and $memo.Vide) {
            Write-Alerte 'Le mémo d''arrêt de l''agent ne cite aucun moteur (rien ne tournait au dernier arrêt) : l''agent ne relancerait RIEN.'
            Write-Conseil 'Démarre le moteur voulu depuis le site : Compte > Mes PC / Workers > ce Worker > bouton du moteur.'
            Exit-Outil -Code 1 -SansPause:$SansPause
        } elseif ($memo.Present) {
            Write-Info ('Mémo d''arrêt de l''agent : ' + ($memo.Moteurs -join ', ') + '. Seuls ces moteurs seront relancés.')
        } else {
            Write-Alerte 'Aucun mémo d''arrêt : l''agent relancera TOUS les moteurs installés et prêts.'
            Write-Alerte 'ComfyUI et Hunyuan3D ensemble = beaucoup de mémoire vidéo (VRAM). Arrête ensuite celui dont tu ne te sers pas.'
            Write-Conseil 'Pour n''en démarrer qu''un : le site (Compte > Mes PC / Workers > ce Worker > bouton du moteur).'
        }
        if (-not $suivreSeulement) {
            if (-not (Confirm-Action -Question 'Relancer les moteurs de ce Worker ?' -Oui:$Oui)) { Write-Info 'Annulé : rien n''a changé.'; Exit-Outil -Code 2 -SansPause:$SansPause }
            # Sans mémo : seconde confirmation, distincte, pour la relance de TOUT. -Oui (mode piloté) vaut accord, l'alerte est affichée.
            if (-not $memo.Present -and -not (Confirm-Action -Question 'Tu confirmes la relance de TOUS les moteurs prêts ?' -MotExact 'TOUS' -Oui:$Oui)) { Write-Info 'Annulé : rien n''a changé.'; Exit-Outil -Code 2 -SansPause:$SansPause }
        }
        if ($Simulation) {
            Write-Section 'Simulation'
            if ($suivreSeulement) { Write-Info '[simulation] aucun envoi : sondage des ports toutes les 5 s pendant 180 s.' }
            else { Write-Info '[simulation] demande « engines-start » à l''agent, puis sondage des ports toutes les 5 s pendant 180 s. Rien n''a été envoyé.' }
            Exit-Outil -Code 0 -SansPause:$SansPause
        }
        $attendus = @()
        if ($suivreSeulement) {
            $attendus = @($memo.Moteurs)
            if ($attendus.Count -eq 0 -and $memo.Dernier) { $attendus = @($memo.Dernier.requested | ForEach-Object { [string]$_ } | Where-Object { $_ }) }
        } else {
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'demande engines-start'
            $r = Invoke-ActionSure -Racine $Racine -Action 'engines-start' -DelaiSecondes 30
            if (-not $r.Ok -and $r.EnCours) {
                Write-Info 'L''agent a pris la demande et démarre les moteurs : on suit leurs ports.'
                $attendus = @($memo.Moteurs)
            } elseif (-not $r.Ok) {
                if ($r.DelaiDepasse) { Write-Erreur ('Aucune réponse de l''agent : la demande a été retirée, rien ne sera lancé plus tard. ' + $r.Erreur) }
                else { Write-Erreur ('L''agent a refusé : ' + $r.Erreur) }
                if ($r.Erreur -match 'inventaire') { Write-Conseil 'L''agent vient de démarrer et n''a pas fini l''inventaire de ses moteurs : réessaie dans une minute.' }
                Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('engines-start en échec : ' + $r.Erreur)
                Exit-Outil -Code 1 -SansPause:$SansPause
            } else {
                $attendus = @($r.Resultat.starting | ForEach-Object { [string]$_ } | Where-Object { $_ })
                $source = [string]$r.Resultat.source
                if ($attendus.Count -eq 0) {
                    if ($source -eq 'memo') { Write-Info 'L''agent n''a aucun moteur à relancer : son mémo d''arrêt ne cite aucun moteur. Démarre le moteur voulu depuis le site.' }
                    else { Write-Info 'L''agent n''a aucun moteur à relancer : aucun n''est installé et prêt.' }
                    Exit-Outil -Code 0 -SansPause:$SansPause
                }
                Write-Ok ('Relance demandée : ' + ($attendus -join ', '))
                Write-Info ('      Liste tirée de : ' + (Get-NomSourceRelance $source) + '.')
            }
        }
        Write-Section 'Réveil des moteurs (3 minutes au plus ; le chargement d''un modèle est long)'
        $enMarche = @(Wait-Moteurs -Racine $Racine -Attendus $attendus -DelaiSecondes 180)
        $manquants = @($attendus | Where-Object { $_ -ne 'converter' -and ($enMarche -notcontains $_) })
        Write-Section 'Résultat'
        Show-Moteurs -Racine $Racine
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('engines-start : en marche = ' + ($enMarche -join ',') + ' ; manquants = ' + ($manquants -join ','))
        if ($manquants.Count -gt 0) {
            Write-Alerte ('Pas encore ouverts après 3 minutes : ' + ($manquants -join ', '))
            Show-ErreursRelance -Racine $Racine -Manquants $manquants
            Write-Conseil ('Un premier démarrage peut dépasser 3 minutes. Relance cet outil (choix 1) plus tard ; si rien ne vient : ' + (Get-NomEntree 3) + ', journal du moteur.')
            Exit-Outil -Code 1 -SansPause:$SansPause
        }
        if ($attendus.Count -eq 0) { Write-Info 'Aucun moteur précis à attendre : l''état ci-dessus est celui des ports après 3 minutes.' }
        else { Write-Ok 'Tous les moteurs demandés répondent.' }
        Exit-Outil -Code 0 -SansPause:$SansPause
    }
} catch {
    Write-Erreur ('Erreur inattendue : ' + $_.Exception.Message)
    Exit-Outil -Code 1 -SansPause:$SansPause
}
