<#
    Éteindre CE Worker, et rien d'autre sur le PC.

    Pilotage sans clavier :
        eteindre_worker.ps1 -Simulation -Oui -SansPause      (aucun effet)
        eteindre_worker.ps1 -Oui -SansPause [-Forcer]
    Codes de sortie : 0 succès, 1 échec, 2 refusé ou annulé, 3 élévation refusée.

    -DejaEleve est posé par Invoke-OutilEleve : l'instance administrateur n'exécute
    QUE la phase privilégiée (service ou processus), sans rien redemander.
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
Initialize-ConsoleWorker -Titre 'Éteindre le Worker'
$NomOutil = 'eteindre_worker'

# Test-ServiceAutreDossier, Get-PresenceWorker, Confirm-Forcage, Wait-MoteursFermes : commun_cycle.ps1.

function Disable-TachesDuWorker {
    <#
        Mode tâche planifiée : la tâche « Alpine Makers Worker ... » se déclenche à
        l'ouverture de session et se relance après un échec (999 fois, toutes les
        minutes). Tuer le superviseur ne suffit donc pas : on DÉSACTIVE la tâche (elle
        ne part plus à l'ouverture de session et n'est plus relancée), puis on l'ARRÊTE.
        Désactiver d'ABORD : si Windows refuse, rien n'a encore été arrêté et l'outil peut
        demander une fenêtre administrateur sans laisser le Worker à moitié éteint.
        Ne touche que les tâches qui visent CE dossier.
        Renvoie : Desactivees (noms), Echecs (noms).
    #>
    param([string]$Racine, [switch]$Simulation)
    $r = [pscustomobject]@{ Desactivees = @(); Echecs = @() }
    $modeTaches = Get-ModeLancement -Racine $Racine
    foreach ($t in @($modeTaches.Taches | Where-Object { $_.ViseCeDossier })) {
        if ($t.Etat -eq 'Disabled') { continue }
        if ($Simulation) {
            Write-Info ('[simulation]    Disable-ScheduledTask puis Stop-ScheduledTask « ' + $t.Nom + ' »')
            continue
        }
        try {
            # -TaskPath : une tâche homonyme rangée dans un autre dossier du Planificateur n'est jamais touchée.
            Disable-ScheduledTask -TaskName $t.Nom -TaskPath $t.Chemin -ErrorAction Stop | Out-Null
            Stop-ScheduledTask -TaskName $t.Nom -TaskPath $t.Chemin -ErrorAction Stop
            $relue = Get-ScheduledTask -TaskName $t.Nom -TaskPath $t.Chemin -ErrorAction Stop
            if ([string]$relue.State -ne 'Disabled') { throw ('état après coup : ' + [string]$relue.State) }
            $r.Desactivees += $t.Nom
            Write-Ok ('Tâche planifiée « ' + $t.Nom + ' » désactivée et arrêtée.')
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('tâche planifiée désactivée : ' + $t.Nom)
        } catch {
            $r.Echecs += $t.Nom
            Write-Alerte ('Tâche planifiée « ' + $t.Nom + ' » NON désactivée : ' + $_.Exception.Message)
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('ÉCHEC désactivation de la tâche ' + $t.Nom + ' : ' + $_.Exception.Message)
        }
    }
    return $r
}

function Invoke-PhasePrivilegiee {
    <# Arrête le service (ou le superviseur, l'agent et les moteurs reconnus, un PID à la fois), puis balaie les orphelins. Renvoie 0 ou 1. #>
    param([string]$Racine, $Mode)
    $avant = Get-PidsDuWorker -Racine $Racine
    Write-Info ('Processus reconnus comme appartenant à ce Worker : ' + $avant.Processus.Count)
    $echec = $false
    $restants = 0
    if ($Mode.Mode -eq 'service') {
        if (-not $Mode.Service.ViseCeDossier) { Write-Erreur 'Le service AlpineWorker ne vise pas ce dossier : il n''est pas touché.'; return 1 }
        try {
            Set-Service -Name 'AlpineWorker' -StartupType Manual -ErrorAction Stop
            Write-Ok 'Démarrage du service passé en Manuel : la supervision ne le relancera pas.'
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'service AlpineWorker : démarrage passé en Manuel'
        } catch {
            Write-Erreur ('Impossible de passer le service en Manuel : ' + $_.Exception.Message)
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('ÉCHEC Set-Service Manual : ' + $_.Exception.Message)
            return 1
        }
        try { Stop-Service -Name 'AlpineWorker' -Force -ErrorAction Stop -WarningAction SilentlyContinue } catch { Write-Alerte ('Stop-Service : ' + $_.Exception.Message) }
        if (Wait-Service -Nom 'AlpineWorker' -Etat 'Stopped' -DelaiSecondes 30) {
            Write-Ok 'Service AlpineWorker arrêté.'
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'service AlpineWorker arrêté'
        } else {
            Write-Alerte 'Le service n''est pas arrêté après 30 secondes : arrêt de ses processus reconnus.'
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'service AlpineWorker non arrêté après 30 s'
            $echec = $true
        }
    }
    $tacheRestante = $false
    if ($Mode.Mode -eq 'tache') {
        # AVANT d'arrêter l'arbre : sinon le planificateur relance run_worker.ps1 une minute plus tard.
        $taches = Disable-TachesDuWorker -Racine $Racine
        if ($taches.Echecs.Count -gt 0) { $tacheRestante = $true }
    }
    # Balayage : ce qui était reconnu avant + ce qui se reconnaît encore maintenant (orphelins).
    $apres = Get-PidsDuWorker -Racine $Racine
    $cibles = @{}
    foreach ($p in @($avant.Processus) + @($apres.Processus)) { if ($p -and (Test-MemeProcessus -Entree $p)) { $cibles[$p.ProcessId] = $p } }
    if ($cibles.Count -gt 0) {
        Write-Info ('Processus du Worker encore vivants : ' + $cibles.Count + '. Arrêt par PID.')
        $restants = Stop-PidsReconnus -Liste @($cibles.Values) -Racine $Racine -Outil $NomOutil
        if ($restants -gt 0) { Write-Erreur ($restants.ToString() + ' processus résistent encore.'); $echec = $true }
        else { Write-Ok 'Plus aucun processus du Worker.' }
    } else {
        Write-Ok 'Aucun processus orphelin.'
    }
    if ($Mode.Mode -eq 'service' -and $echec) {
        if (Wait-Service -Nom 'AlpineWorker' -Etat 'Stopped' -DelaiSecondes 15) { $echec = ($cibles.Count -gt 0 -and $restants -gt 0) }
    }
    if ($tacheRestante) { Write-Erreur 'La tâche planifiée est restée active : le Worker repartira à la prochaine ouverture de session.'; $echec = $true }
    if ($echec) { return 1 }
    return 0
}

try {
    $Racine = Get-RacineWorker -Racine $Racine
    $mode = Get-ModeLancement -Racine $Racine

    # ------------------------------------------------------------ instance administrateur
    if ($DejaEleve) {
        Write-Cadre -Titre 'ÉTEINDRE LE WORKER : phase administrateur' -Lignes @('Cette fenêtre se ferme seule ; le résultat s''affiche dans la première.')
        if (-not (Test-Administrateur)) { Write-Erreur 'Cette fenêtre n''a pas les droits administrateur.'; exit 3 }
        $code = Invoke-PhasePrivilegiee -Racine $Racine -Mode $mode
        Exit-Outil -Code $code -SansPause
    }

    # ------------------------------------------------------------ explication
    Write-Explication -Titre 'ÉTEINDRE CE WORKER (ET RIEN D''AUTRE)' -Fait @(
        'Vérifie d''abord qu''aucun travail (image, 3D, conversion) n''est en cours.',
        'Demande à l''agent d''arrêter proprement ses moteurs : ComfyUI, Hunyuan3D, PrintGuard, bridge Orca.',
        'Arrête ensuite le Worker lui-même et coupe son démarrage automatique :',
        '   service Windows passé en démarrage Manuel, ou tâche planifiée désactivée puis arrêtée.',
        'Vérifie que ses ports sont fermés, et que le service est arrêté ou la tâche bien désactivée.',
((Get-NomEntree 12) + ' remet ce démarrage automatique. Si la coupure échoue, l''outil le dit en rouge.')
    ) -NeFaitPas @(
        'Il ne touche ni au dashboard, ni à la boutique, ni au courrier, ni à MongoDB, ni à Git Sync, ni à Docker.',
        'Il ne met PAS la supervision générale en pause : les autres services restent surveillés.',
        'Il ne supprime rien : identité, association, modèles et travaux sont conservés.',
        'Il n''arrête jamais un programme d''après son nom (python, powershell...) : seulement les processus reconnus comme ceux de ce Worker.'
    ) -Exige @(
        'En mode service (ou si Windows refuse de désactiver la tâche) : une fenêtre administrateur.',
        '   Windows te demandera ton accord (écran assombri) au moment d''arrêter.',
        'Taper le mot ETEINDRE pour confirmer.'
    )
    if ($Simulation) { Write-Alerte 'MODE SIMULATION : rien ne sera arrêté, modifié ni écrit.' }

    # ------------------------------------------------------------ (a) pré-vol
    Write-Section 'Avant d''éteindre'
    $config = Get-ConfigSure -Racine $Racine
    Write-Etat -Libelle 'Dossier du Worker' -Valeur $Racine
    if ($config.Nom) { Write-Etat -Libelle 'Nom' -Valeur $config.Nom }
    $demarrageAvant = ''
    switch ($mode.Mode) {
        'service' { $demarrageAvant = $mode.Service.Demarrage; Write-Etat -Libelle 'Mode de lancement' -Valeur ('service Windows AlpineWorker : ' + $mode.Service.Etat + ', démarrage ' + $mode.Service.Demarrage) }
        'tache'   {
            Write-Etat -Libelle 'Mode de lancement' -Valeur 'tâche planifiée à l''ouverture de session'
            foreach ($t in @($mode.Taches | Where-Object { $_.ViseCeDossier })) { Write-Info ('      Tâche « ' + $t.Nom + ' » : état ' + $t.Etat) }
        }
        default   { Write-Etat -Libelle 'Mode de lancement' -Valeur 'lancement manuel (ni service ni tâche pour ce dossier)' }
    }
    if (Test-ServiceAutreDossier -Mode $mode) { Exit-Outil -Code 2 -SansPause:$SansPause }
    if ($mode.Service -and -not $mode.Service.ViseCeDossier) { Write-Info '      Un service AlpineWorker (arrêté) existe pour un AUTRE dossier : il ne sera pas touché.' }

    $status = Invoke-ActionSure -Racine $Racine -Action 'status' -DelaiSecondes 10
    Show-Moteurs -Racine $Racine -Status $status
    $moteursAvant = @(Get-EtatMoteurs -Racine $Racine | Where-Object { $_.EnEcoute } | ForEach-Object { $_.Outil })
    $jobs = Get-JobsActifs -Racine $Racine -Status $status
    Show-JobsActifs -Jobs $jobs

    # Depuis le menu (ni -Oui ni -SansPause), un refus propose le forçage au lieu de finir en impasse.
    $interactif = (-not $Oui -and -not $SansPause)
    $forcageConfirme = $false
    if ($status.Ok -and $status.Resultat.maintenance_active -and -not $Forcer) {
        Write-Host ''
        Write-Erreur 'Une installation, une mise à jour ou un nettoyage est en cours sur ce Worker.'
        Write-Conseil 'Attends sa fin. L''interrompre peut laisser un moteur à moitié installé.'
        if (-not (Confirm-Forcage -Consequence 'le moteur en cours d''installation peut rester à moitié installé' -Lanceur 'eteindre_worker.bat' -Interactif $interactif)) { Write-Info 'Rien n''a changé.'; Exit-Outil -Code 2 -SansPause:$SansPause }
        $Forcer = $true; $forcageConfirme = $true
    }
    if ($jobs.Connu -and $jobs.Nombre -gt 0) {
        Write-Host ''
        if (-not $Forcer) {
            Write-Erreur 'Extinction refusée : un travail est en cours. L''éteindre maintenant le ferait échouer.'
            Write-Conseil ('Attends sa fin ou annule-le dans le site, puis relance cet outil. ' + (Get-NomEntree 2) + ' montre ce qui bloque.')
            if (-not (Confirm-Forcage -Consequence 'le travail en cours sera PERDU' -Lanceur 'eteindre_worker.bat' -Interactif $interactif)) { Write-Info 'Rien n''a changé.'; Exit-Outil -Code 2 -SansPause:$SansPause }
            $Forcer = $true; $forcageConfirme = $true
        }
        elseif (-not $forcageConfirme) {
            Write-Alerte 'Tu as demandé -Forcer : le travail en cours sera PERDU.'
            if (-not (Confirm-Action -Question 'Éteindre malgré le travail en cours ?' -MotExact 'FORCER' -Oui:$Oui)) { Write-Info 'Annulé : rien n''a changé.'; Exit-Outil -Code 2 -SansPause:$SansPause }
        }
    }

    # ------------------------------------------------------------ (b) confirmation
    Write-Host ''
    if (-not (Confirm-Action -Question 'Éteindre ce Worker maintenant ?' -MotExact 'ETEINDRE' -Oui:$Oui)) { Write-Info 'Annulé : rien n''a changé.'; Exit-Outil -Code 2 -SansPause:$SansPause }

    $agentRepond = [bool]$status.Ok

    # ------------------------------------------------------------ simulation : le plan, sans effet
    if ($Simulation) {
        Write-Section 'Ce qui serait fait'
        if ($agentRepond) { Write-Info '[simulation] 1. demande « engines-stop » à l''agent : arrêt PROPRE des moteurs (90 s au plus).' }
        else {
            Write-Info '[simulation] 1. arrêt propre des moteurs IMPOSSIBLE (agent arrêté ou trop ancien) : arrêt brutal.'
            Write-Info '                 Conséquence : l''agent relancera ces moteurs 30 s après son prochain démarrage.'
        }
        if ($mode.Mode -eq 'service') {
            Write-Info '[simulation] 2. ouverture d''une fenêtre administrateur (accord Windows demandé).'
            Write-Info ('[simulation] 3. Set-Service AlpineWorker -StartupType Manual   (avant : ' + $demarrageAvant + ')')
            Write-Info '[simulation] 4. Stop-Service AlpineWorker -Force, attente de l''état Stopped (30 s).'
            Write-Info '[simulation]    Le fichier pause-supervision.flag n''est PAS créé.'
        } else {
            if ($mode.Mode -eq 'tache') {
                Write-Info '[simulation] 2. désactivation PUIS arrêt de la tâche planifiée de ce dossier (sinon elle relance le Worker) :'
                [void](Disable-TachesDuWorker -Racine $Racine -Simulation)
                Write-Info '[simulation]    puis arrêt de l''arbre du superviseur (run_worker.ps1 de ce dossier), par PID reconnu.'
            } else {
                Write-Info '[simulation] 2. arrêt de l''arbre du superviseur (run_worker.ps1 de ce dossier), par PID reconnu.'
            }
            $presenceSimulee = Get-PresenceWorker -Racine $Racine -Status $status
            Write-Info ('[simulation]    Ce dossier tourne-t-il ? Réponse : ' + $presenceSimulee.Etat + '.')
            Write-Info '[simulation]    Fenêtre administrateur seulement si ses processus sont illisibles, ou si Windows refuse de désactiver la tâche.'
        }
        $vus = Get-PidsDuWorker -Racine $Racine
        if ($vus.Processus.Count -gt 0) { [void](Stop-PidsReconnus -Liste $vus.Processus -Racine $Racine -Outil $NomOutil -Simulation) }
        else { Write-Info '[simulation]    Aucun processus du Worker lisible sans droits administrateur (normal en mode service).' }
        Write-Info '[simulation] 5. balayage des processus orphelins du Worker, par PID reconnu.'
        Write-Info '[simulation] 6. vérification : ports 8188, 8189, 8000, 1984 fermés, 15064 absent de HTTP.sys, service arrêté ou tâche désactivée.'
        Write-Info '[simulation] 7. écriture de runtime\local-control\extinction.json et d''une ligne dans logs\outils-worker.log.'
        Write-Host ''
        Write-Ok 'Simulation terminée : rien n''a été arrêté, modifié ni écrit.'
        Exit-Outil -Code 0 -SansPause:$SansPause
    }

    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('début : mode ' + $mode.Mode + ', démarrage avant = ' + $demarrageAvant + ', moteurs en marche = ' + ($moteursAvant -join ',') + ', forcer = ' + [bool]$Forcer)

    # ------------------------------------------------------------ (c) arrêt propre des moteurs
    Write-Section 'Étape 1 : arrêt propre des moteurs'
    $arretPropre = $false
    $arretEntame = $false
    if ($agentRepond) {
        Write-Info 'Demande envoyée à l''agent. L''arrêt peut durer 90 secondes : un point s''affiche toutes les 5 secondes, ne ferme pas la fenêtre.'
        $r = Invoke-ActionSure -Racine $Racine -Action 'engines-stop' -DelaiSecondes 90
        if ($r.Ok) {
            $arretPropre = $true
            $arretes = @($r.Resultat.stopped)
            if ($arretes.Count) { Write-Ok ('Moteurs arrêtés par l''agent : ' + ($arretes -join ', ')) } else { Write-Ok 'Aucun moteur ne tournait.' }
            foreach ($e in @($r.Resultat.errors)) { if ($e) { Write-Alerte ('Erreur signalée : ' + ($e | ConvertTo-Json -Compress -Depth 3)) } }
        } elseif ($r.EnCours) {
            # Ce n'est PAS un refus : l'agent a pris la demande (relances automatiques déjà coupées) et n'a pas fini.
            $arretEntame = $true
            Write-Info 'L''agent a bien pris la demande mais n''a pas fini en 90 secondes : suivi des ports des moteurs (90 s de plus).'
            $ouverts = @(Wait-MoteursFermes -Racine $Racine -DelaiSecondes 90)
            if ($ouverts.Count -eq 0) { $arretPropre = $true; Write-Ok 'Tous les moteurs sont fermés.' }
            else { Write-Alerte ('Encore ouverts : ' + ($ouverts -join ', ') + '. Ils seront arrêtés avec le Worker.') }
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('engines-stop encore en cours après 90 s ; moteurs encore ouverts = ' + ($ouverts -join ','))
        } else {
            Write-Alerte ('L''agent n''a pas arrêté ses moteurs : ' + $r.Erreur)
            if (-not $Forcer) {
                Write-Conseil 'Rien n''a été éteint. La cause habituelle : un travail ou une installation a commencé entre-temps.'
                Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('engines-stop refusé : ' + $r.Erreur)
                if (-not (Confirm-Forcage -Consequence 'les moteurs seront arrêtés brutalement, avec le travail qu''ils font' -Lanceur 'eteindre_worker.bat' -Interactif $interactif)) { Exit-Outil -Code 1 -SansPause:$SansPause }
                $Forcer = $true
            }
        }
    }
    if (-not $arretPropre) {
        if ($arretEntame) { Write-Info 'Les relances automatiques des moteurs sont déjà coupées par l''agent : ils ne repartiront pas seuls.' }
        else { Write-Alerte 'Arrêt brutal des moteurs : l''agent relancera ces moteurs 30 secondes après son prochain démarrage.' }
        if ($status.NonSupporte) { Write-Conseil 'Pour un arrêt propre à l''avenir, mets le Worker à jour depuis le site (Compte > Mes PC / Workers > « Mettre à jour »).' }
    }

    # ------------------------------------------------------------ (d)(e) arrêt du Worker
    Write-Section 'Étape 2 : arrêt du Worker'
    $codePhase = 0
    $besoinElevation = ($mode.Mode -eq 'service')
    if (-not $besoinElevation -and -not (Test-Administrateur)) {
        # Preuves propres à CE dossier d'abord ; les ports (globaux) seulement si des processus sont illisibles.
        $presence = Get-PresenceWorker -Racine $Racine -Status $status
        if ($presence.Etat -eq 'illisible' -or ($presence.Etat -eq 'oui' -and -not $presence.Lisible)) {
            Write-Info 'Le Worker tourne, mais ses processus sont illisibles sans droits administrateur.'
            $besoinElevation = $true
        }
        if (-not $besoinElevation -and $mode.Mode -eq 'tache') {
            # Essai sans élévation : une tâche créée par ce compte se désactive sans droits particuliers.
            $essai = Disable-TachesDuWorker -Racine $Racine
            if ($essai.Echecs.Count -gt 0) {
                Write-Info 'Windows refuse de désactiver la tâche sans droits administrateur.'
                $besoinElevation = $true
            }
        }
    }
    if ($besoinElevation -and -not (Test-Administrateur)) {
        Write-Info 'Windows va te demander ton accord pour ouvrir une fenêtre administrateur...'
        $codePhase = Invoke-OutilEleve -Script $PSCommandPath -Arguments (Get-ArgumentsCommuns -Racine $Racine)
        if ($null -eq $codePhase) {
            Write-Erreur 'Tu as refusé la fenêtre administrateur : le Worker n''a pas été arrêté.'
            if ($arretPropre) { Write-Conseil ('Ses moteurs, eux, sont déjà arrêtés. ' + (Get-NomEntree 9) + ', choix 3, les rallume.') }
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'élévation refusée'
            Exit-Outil -Code 3 -SansPause:$SansPause
        }
    } else {
        $codePhase = Invoke-PhasePrivilegiee -Racine $Racine -Mode $mode
    }

    # Mémo d'une extinction précédente (deuxième extinction d'affilée) : tâches déjà désactivées par cet outil.
    $tachesMemoAvant = @()
    $fichierMemoAvant = Join-Path $Racine 'runtime\local-control\extinction.json'
    if (Test-Path -LiteralPath $fichierMemoAvant -PathType Leaf) {
        try { $tachesMemoAvant = @((Get-Content -LiteralPath $fichierMemoAvant -Raw -Encoding UTF8 | ConvertFrom-Json).tache_desactivee | ForEach-Object { [string]$_ } | Where-Object { $_ }) } catch { Write-Info 'Ancien mémo d''extinction illisible : ignoré.' }
    }

    # ------------------------------------------------------------ (f) vérification
    Write-Section 'Étape 3 : vérification'
    Start-Sleep -Seconds 2
    $toutFerme = $true
    foreach ($l in @(Get-VerificationArret -Racine $Racine)) {
        if ($l.Ferme) { Write-Etat -Libelle $l.Libelle -Valeur 'fermé' -Niveau ok }
        else { Write-Etat -Libelle $l.Libelle -Valeur 'ENCORE OUVERT' -Niveau erreur; $toutFerme = $false }
    }
    $modeApres = Get-ModeLancement -Racine $Racine
    if ($mode.Mode -eq 'service') {
        if ($modeApres.Service -and $modeApres.Service.Etat -eq 'Stopped') { Write-Etat -Libelle 'Service AlpineWorker' -Valeur ('arrêté, démarrage ' + $modeApres.Service.Demarrage) -Niveau ok }
        else { Write-Etat -Libelle 'Service AlpineWorker' -Valeur ('état ' + [string]$modeApres.Service.Etat) -Niveau erreur; $toutFerme = $false }
        if ($modeApres.Service -and $modeApres.Service.Demarrage -eq 'Auto') { Write-Alerte 'Le démarrage est resté Automatique : la supervision va relancer le Worker.'; $toutFerme = $false }
    }
    $tachesDuDossier = @($modeApres.Taches | Where-Object { $_.ViseCeDossier })
    $tachesActives = @($tachesDuDossier | Where-Object { $_.Etat -ne 'Disabled' })
    # Ne noter que les tâches désactivées PAR cet outil (cette fois-ci, ou lors d'une extinction précédente encore en mémo) :
    # « Rallumer » ne doit pas réactiver une tâche que tu avais désactivée toi-même.
    $activesAvant = @($mode.Taches | Where-Object { $_.ViseCeDossier -and $_.Etat -ne 'Disabled' } | ForEach-Object { [string]$_.Nom })
    # Notées par « chemin + nom » (Get-CleTache) ; l'ancien format à nom seul reste lu (Test-TacheDansMemo).
    $tachesDesactivees = @($tachesDuDossier | Where-Object { $_.Etat -eq 'Disabled' -and (($activesAvant -contains [string]$_.Nom) -or (Test-TacheDansMemo -Tache $_ -Memo $tachesMemoAvant)) } | ForEach-Object { Get-CleTache $_ })
    foreach ($t in $tachesDuDossier) {
        if ($t.Etat -eq 'Disabled') { Write-Etat -Libelle 'Tâche planifiée' -Valeur ('désactivée : ' + $t.Nom) -Niveau ok }
        else { Write-Etat -Libelle 'Tâche planifiée' -Valeur ('ENCORE ACTIVE (' + $t.Etat + ') : ' + $t.Nom) -Niveau erreur }
    }

    # ------------------------------------------------------------ (g) mémo d'extinction
    $dossierMemo = Join-Path $Racine 'runtime\local-control'
    $fichierMemo = Join-Path $dossierMemo 'extinction.json'
    try {
        if (-not (Test-Path -LiteralPath $dossierMemo)) { New-Item -ItemType Directory -Path $dossierMemo -Force | Out-Null }
        if ($demarrageAvant -eq 'Manual' -and (Test-Path -LiteralPath $fichierMemo -PathType Leaf)) {
            # Deuxième extinction d'affilée : garder le démarrage d'origine noté la première fois.
            try { $ancien = Get-Content -LiteralPath $fichierMemo -Raw -Encoding UTF8 | ConvertFrom-Json; if ($ancien.demarrage_avant) { $demarrageAvant = [string]$ancien.demarrage_avant } } catch { }
        }
        $memo = [ordered]@{
            date = (Get-Date).ToString('o')
            mode = $mode.Mode
            demarrage_avant = $demarrageAvant
            moteurs_qui_tournaient = @($moteursAvant)
            arret_propre_des_moteurs = $arretPropre
            tache_desactivee = @($tachesDesactivees)
        }
        $texte = $memo | ConvertTo-Json -Depth 4
        [IO.File]::WriteAllText($fichierMemo, $texte, (New-Object System.Text.UTF8Encoding($false)))
    } catch { Write-Alerte ('Mémo d''extinction non écrit : ' + $_.Exception.Message) }

    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('fin : phase administrateur = ' + $codePhase + ', tout fermé = ' + $toutFerme + ', arrêt propre = ' + $arretPropre + ', tâches encore actives = ' + $tachesActives.Count)

    Write-Host ''
    if ($toutFerme -and $codePhase -eq 0 -and $tachesActives.Count -eq 0) {
        $suite = @()
        switch ($mode.Mode) {
            'service' { $suite = @('Démarrage Manuel : il reste éteint, même après un redémarrage du PC.', ('Pour le remettre en route : ' + (Get-NomEntree 12)), '(il remet aussi le démarrage automatique du service).') }
            'tache'   { $suite = @('Tâche désactivée : il ne repartira pas à l''ouverture de session.', ('Pour le remettre en route : ' + (Get-NomEntree 12)), '(il réactive aussi la tâche planifiée).') }
            default   { $suite = @('Ni service ni tâche planifiée ici : rien ne le relance tout seul.', ('Pour le remettre en route : ' + (Get-NomEntree 12) + '.')) }
        }
        Write-Cadre -Titre 'WORKER ÉTEINT' -Couleur Green -Lignes (@('Il apparaîtra « hors ligne » dans le site d''ici une minute.') + $suite)
        Exit-Outil -Code 0 -SansPause:$SansPause
    }
    if ($tachesActives.Count -gt 0) {
        Write-Erreur 'Le Worker est arrêté pour l''instant, mais sa tâche planifiée est restée ACTIVE :'
        Write-Erreur 'il repartira à la prochaine ouverture de session (et peut-être dès la minute qui vient).'
        Write-Conseil 'Relance cet outil et accepte la fenêtre administrateur.'
        Write-Conseil ('Sinon, retire la tâche toi-même, depuis le dossier du Worker : outils\demarrage_auto.bat -DesactiverAuto   (' + (Get-NomEntree 15) + ' la recréera quand tu voudras).')
    }
    Write-Erreur 'L''extinction n''est pas complète : regarde les lignes en rouge ci-dessus.'
    if ($codePhase -ne 0 -and $besoinElevation) { Show-TraceOutil -Racine $Racine -Outil $NomOutil }
    Write-Conseil ('Relance cet outil une seconde fois : sa fenêtre administrateur balaie les processus restants. Si un port reste ouvert, ' + (Get-NomEntree 1) + ' indique le PID qui le tient.')
    Exit-Outil -Code 1 -SansPause:$SansPause
} catch {
    Write-Erreur ('Erreur inattendue : ' + $_.Exception.Message)
    if (-not $Simulation) { try { Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('erreur inattendue : ' + $_.Exception.Message) } catch { } }
    Exit-Outil -Code 1 -SansPause:$SansPause
}
