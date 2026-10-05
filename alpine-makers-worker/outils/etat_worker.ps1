<#
    État complet du Worker, en LECTURE SEULE et SANS fenêtre administrateur.

    Usage :
        etat_worker.ps1                     affichage expliqué, puis verdict
        etat_worker.ps1 -SansPause          idem, sans attendre la touche Entrée
        etat_worker.ps1 -Json               un seul objet JSON (pour rapport_diagnostic)

    Codes de sortie : 0 = contrôle fait (même s'il trouve des problèmes), 1 = contrôle impossible.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause,
    [switch]$Json
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'diagnostic_commun.ps1')

# En mode -Json, rien d'autre que l'objet JSON ne doit sortir : l'affichage est neutralisé.
if ($Json) { function Write-Host { param([Parameter(ValueFromRemainingArguments = $true)]$Reste, $ForegroundColor, [switch]$NoNewline) } }

Initialize-ConsoleWorker -Titre 'État du Worker Alpine Makers'
try { $Racine = Get-RacineWorker -Racine $Racine } catch {
    if ($Json) { [Console]::Out.WriteLine((@{ ok = $false; erreur = [string]$_.Exception.Message } | ConvertTo-Json -Compress)) }
    else { Write-Erreur ([string]$_.Exception.Message); Wait-FinOutil -SansPause:$SansPause }
    exit 1
}

Write-Explication -Titre 'État du Worker' -Fait @(
    'Contrôle l''identité, le mode de lancement, l''agent, les moteurs, le réseau,',
    'l''horloge, la carte graphique, le disque et les journaux.',
    'Termine par un verdict : chaque problème trouvé, avec l''outil du menu à lancer.'
) -NeFaitPas @(
    'Ne modifie rien : aucun service, aucun processus, aucun fichier.',
    'Ne lit jamais le token ni l''identité du Worker.'
) -Exige @(
    'Rien. Aucune fenêtre administrateur n''est demandée : quand une information',
    'exige l''administrateur, l''outil le dit au lieu de deviner.'
)

# Libellés EXACTS du menu, avec leur numéro : ils viennent de la table du socle (Get-NomEntree),
# que menu_worker.ps1 contrôle à chaque lancement. Le verdict ne cite que des outils trouvables.
$script:Menu = @{
    jobs        = (Get-NomEntree 2 -Nu)
    journaux    = (Get-NomEntree 3 -Nu)
    reconnecter = (Get-NomEntree 5 -Nu)
    adressesIp  = (Get-NomEntree 6 -Nu)
    reassocier  = (Get-NomEntree 8 -Nu)
    moteurs     = (Get-NomEntree 9 -Nu)
    redemarrer  = (Get-NomEntree 10 -Nu)
    eteindre    = (Get-NomEntree 11 -Nu)
    rallumer    = (Get-NomEntree 12 -Nu)
    nettoyage   = (Get-NomEntree 14 -Nu)
    demarrage   = (Get-NomEntree 15 -Nu)
}
# Pas d'outil de réparation ni de mise à jour dans le menu : on dit l'action réelle.
$script:ActionInstall = 'Aucun outil du menu : relance install_windows.bat (dans le dossier du Worker), il retrouve Python et Git et réécrit python_path.txt et git_path.txt'
$script:ActionMiseAJour = 'Aucun outil du menu : mets le Worker à jour depuis le site (Compte > Mes PC / Workers > ce Worker > « Mettre à jour »)'
# Moteur (tool_id) -> clé du journal dans l'outil Journaux.
$script:CleJournalMoteur = @{ 'image_generation' = 'images'; 'ai3d' = '3d'; 'printguard' = 'printguard'; 'model-studio' = 'orca' }

$problemes = New-Object System.Collections.ArrayList
function Add-Probleme {
    param([ValidateSet('erreur', 'alerte')][string]$Gravite, [string]$Texte, [string]$Outil)
    [void]$problemes.Add([pscustomobject]@{ Gravite = $Gravite; Probleme = $Texte; Outil = $Outil })
}
$constats = [ordered]@{ ok = $true; genere_le = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'); racine = $Racine; administrateur = (Test-Administrateur) }

# ---------------------------------------------------------------------------
# 1. Identité et version
# ---------------------------------------------------------------------------
Write-Section 'Identité et version'
$config = Get-ConfigSure -Racine $Racine
$version = Get-VersionInstallee -Racine $Racine
$python = Get-PythonWorker -Racine $Racine
$fichierPython = Join-Path $Racine 'python_path.txt'
$pythonFichierValide = $false
if (Test-Path -LiteralPath $fichierPython -PathType Leaf) {
    $cheminEcrit = ([string](Get-Content -LiteralPath $fichierPython -Raw -Encoding UTF8)).Trim().Trim([char]0xFEFF)
    $pythonFichierValide = [bool]($cheminEcrit -and (Test-Path -LiteralPath $cheminEcrit -PathType Leaf))
}
$fichierGit = Join-Path $Racine 'git_path.txt'
$gitEtat = 'absent'
if (Test-Path -LiteralPath $fichierGit -PathType Leaf) {
    $cheminGit = ([string](Get-Content -LiteralPath $fichierGit -Raw -Encoding UTF8)).Trim().Trim([char]0xFEFF)
    if ($cheminGit -and (Test-Path -LiteralPath $cheminGit -PathType Leaf)) { $gitEtat = 'valide' } else { $gitEtat = 'invalide' }
}

Write-Etat 'Dossier du Worker' $Racine 'info'
if (-not $config.Present) {
    Write-Etat 'Configuration' 'config.json absent : Worker jamais associé' 'erreur'
    Add-Probleme 'erreur' 'Le Worker n''a pas de configuration (config.json absent).' $script:Menu.reassocier
} elseif (-not $config.Associe) {
    Write-Etat 'Association' 'non associé à un compte' 'erreur'
    Add-Probleme 'erreur' 'Le Worker n''est associé à aucun compte.' $script:Menu.reassocier
} else {
    Write-Etat 'Association' ('associé  (identifiant ' + $config.WorkerIdCourt + '...)') 'ok'
}
if ($config.Nom) { Write-Etat 'Nom affiché sur le site' $config.Nom 'info' }
if ($config.ServeurUrl) { Write-Etat 'Serveur' $config.ServeurUrl 'info' }
if ($version) { Write-Etat 'Version de l''agent' $version 'info' } else { Write-Etat 'Version de l''agent' 'inconnue' 'alerte' }
if ($pythonFichierValide) { Write-Etat 'Python (python_path.txt)' $python 'ok' }
elseif ($python) {
    Write-Etat 'Python (python_path.txt)' ('fichier absent ou invalide ; secours : ' + $python) 'alerte'
    Add-Probleme 'alerte' 'python_path.txt est absent ou désigne un Python qui n''existe plus.' $script:ActionInstall
} else {
    Write-Etat 'Python' 'introuvable' 'erreur'
    Add-Probleme 'erreur' 'Aucun Python utilisable : l''agent ne peut pas démarrer.' $script:ActionInstall
}
switch ($gitEtat) {
    'valide'   { Write-Etat 'Git (git_path.txt)' 'valide' 'ok' }
    'invalide' { Write-Etat 'Git (git_path.txt)' 'désigne un fichier qui n''existe plus' 'alerte'; Add-Probleme 'alerte' 'git_path.txt désigne un Git qui n''existe plus (installation de moteurs impossible).' $script:ActionInstall }
    default    { Write-Etat 'Git (git_path.txt)' 'absent (utile seulement pour installer des moteurs)' 'info' }
}
$constats.identite = [ordered]@{ config_presente = $config.Present; associe = $config.Associe; worker_id_court = $config.WorkerIdCourt; nom = $config.Nom; serveur = $config.ServeurUrl; version = $version; python = [string]$python; python_path_valide = $pythonFichierValide; git_path = $gitEtat }

# ---------------------------------------------------------------------------
# 2. Lancement
# ---------------------------------------------------------------------------
Write-Section 'Lancement'
$mode = $null
try { $mode = Get-ModeLancement -Racine $Racine } catch { Write-Etat 'Mode de lancement' ('illisible : ' + $_.Exception.Message) 'alerte' }
if ($mode) {
    switch ($mode.Mode) {
        'service' { Write-Etat 'Mode' 'service Windows (démarre avec le PC, sans session ouverte)' 'ok' }
        'tache'   { Write-Etat 'Mode' 'tâche planifiée (démarre à l''ouverture de session)' 'ok' }
        default   {
            Write-Etat 'Mode' 'lancement manuel : rien ne démarre le Worker tout seul' 'alerte'
            Add-Probleme 'alerte' 'Aucun démarrage automatique ne vise ce dossier : après un redémarrage du PC, le Worker restera éteint.' $script:Menu.demarrage
        }
    }
    if ($mode.Service) {
        $s = $mode.Service
        Write-Etat 'Service AlpineWorker' ($s.Etat + ' / démarrage ' + $s.Demarrage + ' / compte ' + $s.Compte) 'info'
        if (-not $s.ViseCeDossier) {
            Write-Etat 'Dossier du service' ('AUTRE dossier : ' + $s.Dossier) 'alerte'
            Add-Probleme 'alerte' ('Le service AlpineWorker vise un autre dossier (' + $s.Dossier + ') : les outils de ce dossier ne le piloteront pas.') $script:Menu.demarrage
        } else {
            Write-Etat 'Dossier du service' 'ce dossier' 'ok'
            if ($s.Etat -ne 'Running') {
                if ($s.Demarrage -eq 'Auto') {
                    Write-Etat 'État du service' ($s.Etat + ' (le superviseur le relance sous 30 s)') 'erreur'
                    Add-Probleme 'erreur' 'Le service AlpineWorker est arrêté alors qu''il est en démarrage automatique.' ($script:Menu.rallumer + ' ; s''il retombe : ' + $script:Menu.journaux + ', journal « service-erreurs-precedent »')
                } else {
                    Write-Etat 'État du service' ($s.Etat + ', démarrage ' + $s.Demarrage + ' = arrêt volontaire, personne ne le relancera') 'alerte'
                    Add-Probleme 'alerte' ('Le Worker est éteint volontairement (état laissé par « ' + [string]$script:NomsEntrees[11] + ' » : service en démarrage manuel ou désactivé). Ce n''est pas une panne.') $script:Menu.rallumer
                }
            } elseif ($s.Demarrage -ne 'Auto') {
                Write-Etat 'Démarrage du service' ($s.Demarrage + ' : il ne repartira pas après un redémarrage du PC') 'alerte'
                Add-Probleme 'alerte' 'Le service tourne mais n''est pas en démarrage automatique.' ($script:Menu.rallumer + ' : il remet le démarrage automatique')
            }
        }
    } else { Write-Etat 'Service AlpineWorker' 'non installé sur ce PC' 'info' }

    $taches = @($mode.Taches)
    if ($taches.Count -eq 0) { Write-Etat 'Tâches planifiées' 'aucune' 'info' }
    foreach ($t in $taches) {
        if ($t.Orpheline) {
            Write-Etat 'Tâche planifiée' ($t.Nom + ' : ORPHELINE, vise un dossier disparu (' + $t.Dossier + ')') 'alerte'
            Add-Probleme 'alerte' ('La tâche planifiée « ' + $t.Nom + ' » vise un dossier qui n''existe plus.') ($script:Menu.demarrage + ' : il retire les tâches orphelines')
        } elseif ($t.ViseCeDossier) {
            if ($mode.Mode -eq 'tache' -and $t.Etat -eq 'Disabled') {
                Write-Etat 'Tâche planifiée' ($t.Nom + ' : DÉSACTIVÉE, vise ce dossier') 'alerte'
                Add-Probleme 'alerte' ('La tâche planifiée « ' + $t.Nom + ' » est désactivée : le Worker ne partira pas à l''ouverture de session.') ($script:Menu.rallumer + ' s''il a été éteint par ' + $script:Menu.eteindre + ' ; sinon ' + $script:Menu.demarrage)
            } else { Write-Etat 'Tâche planifiée' ($t.Nom + ' : ' + $t.Etat + ', vise ce dossier') 'info' }
            if ($mode.Mode -eq 'service' -and $t.Etat -ne 'Disabled') {
                Add-Probleme 'alerte' ('Le service ET la tâche « ' + $t.Nom + ' » lancent ce même Worker : risque de double lancement.') $script:Menu.demarrage
            }
        } elseif ($t.Illisible) {
            Write-Etat 'Tâche planifiée' ($t.Nom + ' : ' + $t.Etat + ', dossier illisible dans la tâche : vérifie-la dans le Planificateur de tâches') 'alerte'
        } else {
            Write-Etat 'Tâche planifiée' ($t.Nom + ' : ' + $t.Etat + ', vise un autre dossier (' + $t.Dossier + ')') 'info'
        }
    }
    $constats.lancement = $mode
}

# Processus : identifiables seulement si leur ligne de commande est lisible.
$processus = $null
try { $processus = Get-ProcessusDuWorker -Racine $Racine } catch { }
if ($processus) {
    $nb = @($processus.Processus).Count
    if ($nb -gt 0) {
        $roles = (@($processus.Processus) | Group-Object Role | ForEach-Object { [string]$_.Count + ' ' + $_.Name }) -join ', '
        Write-Etat 'Processus reconnus' $roles 'ok'
    } elseif ($processus.Illisibles -gt 0 -and -not (Test-Administrateur)) {
        Write-Etat 'Processus reconnus' ('indisponible : ' + $processus.Illisibles + ' ligne(s) de commande illisibles sans fenêtre administrateur') 'info'
    } else { Write-Etat 'Processus reconnus' 'aucun' 'info' }
    $constats.processus = [ordered]@{ reconnus = $nb; illisibles = [int]$processus.Illisibles; detail = @(@($processus.Processus) | ForEach-Object { [ordered]@{ pid = $_.ProcessId; nom = $_.Nom; role = $_.Role } }) }
}

# ---------------------------------------------------------------------------
# 3. Marqueurs
# ---------------------------------------------------------------------------
Write-Section 'Marqueurs'
$deconnecte = Test-Path -LiteralPath (Join-Path $Racine '.worker-disconnected')
$detache = Test-Path -LiteralPath (Join-Path $Racine '.worker-detached')
$pauseGlobale = Test-Path -LiteralPath (Join-Path $env:ProgramData 'AlpineMakers\pause-supervision.flag')
if ($detache) {
    Write-Etat '.worker-detached' 'présent : Worker RETIRÉ du site' 'erreur'
    Add-Probleme 'erreur' 'Le Worker a été retiré du site : il ne se reconnectera pas tout seul.' $script:Menu.reassocier
} else { Write-Etat '.worker-detached' 'absent' 'ok' }
if ($deconnecte) {
    Write-Etat '.worker-disconnected' 'présent : déconnecté depuis le site' 'alerte'
    Add-Probleme 'alerte' 'Le Worker a été déconnecté depuis le site : il reste hors ligne tant qu''on ne le reconnecte pas.' $script:Menu.reconnecter
} else { Write-Etat '.worker-disconnected' 'absent' 'ok' }
if ($pauseGlobale) {
    Write-Etat 'pause-supervision.flag' 'PRÉSENT : supervision de TOUTES les sources en pause' 'alerte'
    Add-Probleme 'alerte' 'La supervision de TOUTES les sources (boutique, courrier, dashboard, Worker) est en pause : plus rien n''est relancé automatiquement.' 'Aucun outil du Worker : retire C:\ProgramData\AlpineMakers\pause-supervision.flag quand la maintenance de la machine est finie'
} else { Write-Etat 'pause-supervision.flag' 'absent' 'ok' }
$constats.marqueurs = [ordered]@{ deconnecte = $deconnecte; detache = $detache; pause_supervision_globale = $pauseGlobale }

# ---------------------------------------------------------------------------
# 4. Agent
# ---------------------------------------------------------------------------
Write-Section 'Agent'
$relais = Test-AgentVivant
if ($relais) { Write-Etat 'Relais caméra (port 1984)' 'en écoute : l''agent est vivant' 'ok' }
else { Write-Etat 'Relais caméra (port 1984)' 'fermé : agent arrêté, ou aucun relais caméra configuré' 'info' }

$statut = $null
$action = $null
# Worker éteint VOLONTAIREMENT (service de ce dossier arrêté, démarrage manuel ou désactivé) : inutile
# d'attendre 8 s une réponse qui ne viendra pas, et ce n'est pas une panne. L'alerte de la section
# Lancement suffit ; aucun problème « l'agent ne répond pas » n'est ajouté.
$eteintVolontaire = [bool]($mode -and $mode.Service -and $mode.Service.ViseCeDossier -and $mode.Service.Etat -ne 'Running' -and $mode.Service.Demarrage -ne 'Auto' -and -not $relais)
if ($python -and -not $eteintVolontaire) { $action = Invoke-ActionLocaleSure -Racine $Racine -Action 'status' -DelaiSecondes 8 }
$constatAgent = [ordered]@{ relais_camera = $relais; statut_disponible = $false; non_supporte = $false; eteint_volontairement = $eteintVolontaire; erreur = '' }
if ($eteintVolontaire) {
    Write-Etat 'Dialogue avec l''agent' 'sans objet : le Worker est éteint volontairement' 'info'
} elseif (-not $action) {
    Write-Etat 'Dialogue avec l''agent' 'impossible sans Python' 'alerte'
} elseif ($action.NonSupporte) {
    $constatAgent.non_supporte = $true
    Write-Etat 'Dialogue avec l''agent' 'non disponible : agent trop ancien' 'alerte'
    Write-Conseil 'Maintenance, jobs en cours et intentions des moteurs ne peuvent pas être lus.'
    Write-Conseil 'Mets le Worker à jour depuis le site (Compte > Mes PC / Workers > ce Worker > « Mettre à jour ») pour les obtenir.'
    Write-Conseil 'Le reste du contrôle continue.'
    Add-Probleme 'alerte' 'L''agent installé est trop ancien pour répondre aux outils (état détaillé, arrêt propre des moteurs).' $script:ActionMiseAJour
} elseif (-not $action.Ok) {
    $constatAgent.erreur = [string]((Protect-TexteRapport -Lignes @(([string]$action.Erreur) -split "`n" | Select-Object -First 1)) -join '')
    Write-Etat 'Dialogue avec l''agent' 'aucune réponse en 8 s' 'alerte'
    if ($constatAgent.erreur) { Write-Conseil ('Détail : ' + $constatAgent.erreur) }
    $serviceTourne = [bool]($mode -and $mode.Service -and $mode.Service.ViseCeDossier -and $mode.Service.Etat -eq 'Running')
    if ($serviceTourne -or $relais) { Add-Probleme 'alerte' 'L''agent semble lancé mais ne répond pas aux outils (occupé, bloqué ou en cours de démarrage).' ('Relance ce contrôle dans une minute ; s''il reste muet : ' + $script:Menu.redemarrer) }
    else { Add-Probleme 'erreur' 'L''agent ne répond pas et rien n''indique qu''il tourne.' $script:Menu.rallumer }
} else {
    $statut = $action.Resultat
    $constatAgent.statut_disponible = $true
    $constatAgent.statut = $statut
    Write-Etat 'Dialogue avec l''agent' ('répond (version ' + [string]$statut.agent_version + ')') 'ok'
    if ($statut.maintenance_active) { Write-Etat 'Maintenance' 'en cours : les moteurs sont volontairement à l''arrêt' 'alerte'; Add-Probleme 'alerte' 'Une maintenance est en cours (installation ou mise à jour).' ('Attends la fin ; si elle dure plus de 30 minutes : ' + $script:Menu.journaux) }
    else { Write-Etat 'Maintenance' 'aucune' 'ok' }
    if ($statut.maintenance_recovery) { Write-Etat 'Reprise de maintenance' 'en attente' 'alerte' }
    if ($statut.disconnecting) { Write-Etat 'Déconnexion' 'en cours' 'alerte' }
    $jobsActifs = @($statut.jobs_active)
    if ($jobsActifs.Count -gt 0) {
        Write-Etat 'Jobs en cours' ([string]$jobsActifs.Count + ' (ils bloquent l''arrêt des moteurs et la mise à jour)') 'alerte'
        foreach ($j in $jobsActifs) { Write-Conseil ([string]$j.job_id + '  ' + [string]$j.tool_id + '  état ' + [string]$j.state) }
        Add-Probleme 'alerte' ([string]$jobsActifs.Count + ' job(s) en cours ou non soldé(s) bloquent l''arrêt des moteurs et la mise à jour.') $script:Menu.jobs
    } else { Write-Etat 'Jobs en cours' 'aucun' 'ok' }
    # Affirmation sûre ICI seulement : un agent qui sait répondre « status » (et compter
    # jobs_previous_identity) est postérieur au correctif qui lui fait ignorer ces jobs.
    # Sans ce dialogue, c'est l'outil Jobs bloquants qui vérifie le correctif dans agent.py.
    if ([int]$statut.jobs_previous_identity -gt 0) { Write-Etat 'Jobs d''anciennes identités' ([string]$statut.jobs_previous_identity + ' (ignorés par l''agent, aucune action)') 'info' }
}
$constats.agent = $constatAgent

# ---------------------------------------------------------------------------
# 5. Moteurs
# ---------------------------------------------------------------------------
Write-Section 'Moteurs'
$moteurs = @()
try { $moteurs = @(Get-EtatMoteurs -Racine $Racine) } catch { Write-Etat 'Moteurs' ('lecture impossible : ' + $_.Exception.Message) 'alerte' }
$constatMoteurs = @()
# Worker éteint mais moteurs encore ouverts = processus ORPHELINS : ils gardent la mémoire de la carte
# graphique et gêneront le prochain démarrage. « Éteint » = service de ce dossier non démarré (ou, sans
# service, aucun processus reconnu ni tâche en cours), pas de relais caméra, pas de réponse de l'agent.
$serviceDuDossier = [bool]($mode -and $mode.Service -and $mode.Service.ViseCeDossier)
$serviceAutreEnMarche = [bool]($mode -and $mode.Service -and -not $mode.Service.ViseCeDossier -and $mode.Service.Etat -eq 'Running')
$workerEteint = $false
if (-not $relais -and -not $statut -and -not $serviceAutreEnMarche) {
    if ($serviceDuDossier) { $workerEteint = ($mode.Service.Etat -ne 'Running') }
    elseif ($mode) {
        $tacheEnCours = (@($mode.Taches | Where-Object { $_.ViseCeDossier -and $_.Etat -eq 'Running' }).Count -gt 0)
        $coeurVu = ($processus -and @($processus.Processus | Where-Object { $_.Role -eq 'superviseur' -or $_.Role -eq 'agent' }).Count -gt 0)
        $workerEteint = (-not $tacheEnCours -and -not $coeurVu -and $processus -and $processus.Illisibles -eq 0)
    }
}
$orphelins = @()
foreach ($m in $moteurs) {
    $intention = $null
    if ($statut -and $statut.engines) {
        $ligne = @($statut.engines | Where-Object { $_.tool_id -eq $m.Outil }) | Select-Object -First 1
        if ($ligne) { $intention = $ligne.intent }
    }
    $libelle = $m.Nom + ' :' + $m.Port
    if ($m.EnEcoute) {
        $detail = 'en écoute'
        if ($m.Outil -eq 'model-studio') { $detail = 'en écoute (servi par Windows HTTP.sys, sous le PID 4 : normal)' }
        elseif ($m.PidPort -gt 0) { $detail = 'en écoute (port tenu par le PID ' + $m.PidPort + ')' }
        if ($workerEteint) { $orphelins += $m.Nom; Write-Etat $libelle ($detail + ' ALORS QUE le Worker est éteint : orphelin') 'alerte' }
        else { Write-Etat $libelle $detail 'ok' }
    } elseif ($m.FichePresente) {
        Write-Etat $libelle 'port fermé, fiche présente : tombé ou en démarrage' 'alerte'
        $cleJournal = [string]$script:CleJournalMoteur[[string]$m.Outil]
        $outilMoteur = $script:Menu.journaux
        if ($cleJournal) { $outilMoteur = $script:Menu.journaux + ', journal « ' + $cleJournal + ' »' }
        Add-Probleme 'alerte' ($m.Nom + ' ne répond pas sur son port ' + $m.Port + ' alors qu''il est noté comme lancé (tombé, ou encore en démarrage : un gros modèle met plusieurs minutes).') ($outilMoteur + ' ; si rien ne bouge : ' + $script:Menu.moteurs + ', arrêt puis départ')
    } elseif ($intention -eq $true) {
        Write-Etat $libelle 'arrêté, mais l''agent a prévu de le relancer' 'info'
    } else {
        Write-Etat $libelle 'arrêté' 'info'
    }
    $constatMoteurs += [ordered]@{ outil = $m.Outil; nom = $m.Nom; port = $m.Port; en_ecoute = [bool]$m.EnEcoute; pid_port = [int]$m.PidPort; fiche_presente = [bool]$m.FichePresente; intention = $intention }
}
if ($orphelins.Count -gt 0) {
    Add-Probleme 'alerte' ('Le Worker est éteint mais ' + $orphelins.Count + ' moteur(s) tournent encore (orphelins : ' + ($orphelins -join ', ') + ') : ils gardent la mémoire de la carte graphique et gêneront le prochain démarrage.') ($script:Menu.eteindre + ' : relance-le, sa fenêtre administrateur balaie les processus restants ; puis ' + $script:Menu.rallumer)
}
Write-Conseil 'FreeCAD (conversion) n''a pas de processus permanent : il est lancé à la demande.'
$constats.moteurs = $constatMoteurs

# ---------------------------------------------------------------------------
# 6. Réseau et horloge
# ---------------------------------------------------------------------------
Write-Section 'Réseau et horloge'
$constatReseau = [ordered]@{ teste = $false; joignable = $false; code_http = 0; ecart_horloge_secondes = $null; erreur = '' }
if (-not $config.ServeurUrl) {
    Write-Etat 'Serveur' 'adresse inconnue (Worker non associé)' 'info'
} else {
    $constatReseau.teste = $true
    $url = $config.ServeurUrl.TrimEnd('/') + '/health'
    $reponse = $null
    $avant = [DateTime]::UtcNow
    try {
        try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
        $requete = [Net.HttpWebRequest][Net.WebRequest]::Create($url)
        $requete.Method = 'GET'; $requete.Timeout = 8000; $requete.ReadWriteTimeout = 8000
        $requete.AllowAutoRedirect = $false; $requete.UserAgent = 'AlpineMakersWorkerOutils/1.0'
        try { $reponse = $requete.GetResponse() } catch [Net.WebException] {
            if ($_.Exception.Response) { $reponse = $_.Exception.Response } else { throw }
        }
    } catch {
        $message = [string]$_.Exception.Message
        if ($_.Exception.InnerException) { $message = [string]$_.Exception.InnerException.Message }
        $constatReseau.erreur = $message
    }
    $apres = [DateTime]::UtcNow
    if ($reponse) {
        $code = [int]$reponse.StatusCode
        $constatReseau.joignable = $true; $constatReseau.code_http = $code
        $dateServeur = [string]$reponse.Headers['Date']
        try { $reponse.Close() } catch { }
        if ($code -eq 200) { Write-Etat 'Serveur (/health)' 'joignable (200)' 'ok' }
        else {
            Write-Etat 'Serveur (/health)' ('répond, mais avec le code ' + $code) 'alerte'
            Add-Probleme 'alerte' ('Le serveur répond avec le code HTTP ' + $code + ' au lieu de 200 (site en maintenance, ou limite Cloudflare).') 'Aucun outil du Worker : vérifie le site, puis relance ce contrôle'
        }
        if ($dateServeur) {
            try {
                $heureServeur = [DateTimeOffset]::Parse($dateServeur, [Globalization.CultureInfo]::InvariantCulture).UtcDateTime
                $milieu = $avant.AddTicks([long](($apres - $avant).Ticks / 2))
                $ecart = [int][Math]::Round(($milieu - $heureServeur).TotalSeconds)
                $constatReseau.ecart_horloge_secondes = $ecart
                if ([Math]::Abs($ecart) -gt 60) {
                    Write-Etat 'Horloge de ce PC' ('DÉCALÉE de ' + $ecart + ' s par rapport au serveur') 'erreur'
                    Write-Conseil 'Toutes les requêtes signées du Worker seront refusées tant que l''heure est fausse.'
                    Add-Probleme 'erreur' ('Horloge décalée de ' + $ecart + ' s : toutes les requêtes signées seront refusées.') 'Resynchronise l''heure Windows (Paramètres > Heure et langue > Date et heure > Synchroniser maintenant)'
                } else { Write-Etat 'Horloge de ce PC' ('à l''heure (écart ' + $ecart + ' s)') 'ok' }
            } catch { Write-Etat 'Horloge de ce PC' 'non vérifiable (date du serveur illisible)' 'info' }
        } else { Write-Etat 'Horloge de ce PC' 'non vérifiable (le serveur n''a pas donné son heure)' 'info' }
    } else {
        Write-Etat 'Serveur (/health)' ('INJOIGNABLE : ' + $url) 'erreur'
        if ($constatReseau.erreur) { Write-Conseil ('Détail : ' + $constatReseau.erreur) }
        Add-Probleme 'erreur' 'Le serveur est injoignable depuis ce PC (Internet coupé, DNS, pare-feu, ou site arrêté).' ('Aucun outil ne répare Internet : vérifie que le site s''ouvre dans un navigateur sur ce PC. Si le PC vient de changer de réseau, une fois le site joignable : ' + $script:Menu.adressesIp)
    }
}
$constats.reseau = $constatReseau

# ---------------------------------------------------------------------------
# 7. Carte graphique
# ---------------------------------------------------------------------------
Write-Section 'Carte graphique'
$constatGpu = [ordered]@{ disponible = $false; cartes = @() }
$smi = $null
$commande = Get-Command 'nvidia-smi.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($commande) { $smi = $commande.Source }
elseif (Test-Path -LiteralPath (Join-Path $env:SystemRoot 'System32\nvidia-smi.exe')) { $smi = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe' }
if (-not $smi) {
    Write-Etat 'nvidia-smi' 'introuvable : pas de carte NVIDIA, ou pilote non installé' 'info'
} else {
    $lignesGpu = @()
    try { $ErrorActionPreference = 'Continue'; $lignesGpu = @(& $smi '--query-gpu=name,memory.used,memory.total,driver_version' '--format=csv,noheader' 2>$null) } catch { } finally { $ErrorActionPreference = 'Stop' }
    $cartes = @()
    foreach ($l in $lignesGpu) {
        $champs = @(([string]$l) -split '\s*,\s*')
        if ($champs.Count -lt 4) { continue }
        $utilise = 0; $total = 0
        if ($champs[1] -match '(\d+)') { $utilise = [int]$Matches[1] }
        if ($champs[2] -match '(\d+)') { $total = [int]$Matches[1] }
        $cartes += [ordered]@{ nom = $champs[0]; memoire_utilisee_mio = $utilise; memoire_totale_mio = $total; pilote = $champs[3] }
        $niveau = 'ok'; $suffixe = ''
        if ($total -gt 0 -and ($utilise / $total) -gt 0.92) { $niveau = 'alerte'; $suffixe = '  (mémoire presque pleine)' }
        Write-Etat $champs[0] ([string]$utilise + ' / ' + [string]$total + ' Mio utilisés, pilote ' + $champs[3] + $suffixe) $niveau
        if ($niveau -eq 'alerte') { Add-Probleme 'alerte' ('La mémoire de la carte graphique est presque pleine (' + $utilise + ' / ' + $total + ' Mio) : une génération peut échouer.') ($script:Menu.moteurs + ' : arrête un moteur dont tu ne te sers pas, c''est lui qui occupe la carte') }
    }
    if ($cartes.Count -eq 0) { Write-Etat 'nvidia-smi' 'présent mais sans réponse (pilote en erreur ?)' 'alerte'; Add-Probleme 'alerte' 'nvidia-smi ne répond pas : le pilote NVIDIA est peut-être en erreur.' 'Redémarre le PC, puis réinstalle le pilote NVIDIA si cela persiste' }
    else { $constatGpu.disponible = $true; $constatGpu.cartes = $cartes }
}
$constats.gpu = $constatGpu

# ---------------------------------------------------------------------------
# 8. Disque
# ---------------------------------------------------------------------------
Write-Section 'Disque'
$constatDisque = [ordered]@{ lecteur = ''; libre_gio = $null; total_gio = $null }
try {
    $lecteur = New-Object System.IO.DriveInfo([System.IO.Path]::GetPathRoot($Racine))
    $libre = [Math]::Round($lecteur.AvailableFreeSpace / 1GB, 1)
    $total = [Math]::Round($lecteur.TotalSize / 1GB, 1)
    $constatDisque.lecteur = $lecteur.Name; $constatDisque.libre_gio = $libre; $constatDisque.total_gio = $total
    if ($libre -lt 8) {
        Write-Etat ('Lecteur ' + $lecteur.Name) ([string]$libre + ' Gio libres sur ' + $total + ' : TROP PEU') 'alerte'
        Add-Probleme 'alerte' ('Il reste ' + $libre + ' Gio sur le lecteur du Worker : les générations et les mises à jour peuvent échouer.') $script:Menu.nettoyage
    } else { Write-Etat ('Lecteur ' + $lecteur.Name) ([string]$libre + ' Gio libres sur ' + $total) 'ok' }
} catch { Write-Etat 'Espace disque' 'illisible' 'info' }
$constats.disque = $constatDisque

# ---------------------------------------------------------------------------
# 9. Journaux
# ---------------------------------------------------------------------------
Write-Section 'Journaux (200 dernières lignes de chacun)'
$constatJournaux = @()
$fichiersJournaux = @()
$dossierLogs = Join-Path $Racine 'logs'
if (Test-Path -LiteralPath $dossierLogs -PathType Container) { $fichiersJournaux += @(Get-ChildItem -LiteralPath $dossierLogs -Filter '*.log' -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -notmatch '(?i)token' } | ForEach-Object { $_.FullName }) }
# Journaux du service : le fichier courant, ET le dernier fichier daté non vide. nssm
# repart d'un fichier vide à chaque redémarrage : après un plantage suivi d'une relance
# par le superviseur, la cause (Traceback) est dans le fichier daté, pas dans .err.log.
$journauxService = @(Get-JournauxConnus -Racine $Racine | Where-Object { $_.Existe -and @('service-erreurs', 'service-precedent', 'service-erreurs-precedent') -contains $_.Cle })
foreach ($js in $journauxService) { $fichiersJournaux += $js.Chemin }
$erreursPrecedentes = [string](@($journauxService | Where-Object { $_.Cle -eq 'service-erreurs-precedent' } | ForEach-Object { $_.Chemin }) | Select-Object -First 1)
if ($fichiersJournaux.Count -eq 0) { Write-Etat 'Journaux' 'aucun journal trouvé' 'info' }
$bruyants = @()
foreach ($f in $fichiersJournaux) {
    $fin = Get-FinFichier -Chemin $f -Lignes 200
    $nbErreurs = @($fin | Where-Object { Test-LigneErreur $_ }).Count
    $nom = Split-Path -Leaf $f
    $niveau = 'ok'
    if ($nbErreurs -ge 20) { $niveau = 'alerte'; $bruyants += $nom }
    elseif ($nbErreurs -gt 0) { $niveau = 'info' }
    Write-Etat $nom ([string]$nbErreurs + ' ligne(s) d''erreur sur ' + $fin.Count + ' lues') $niveau
    $constatJournaux += [ordered]@{ fichier = $f; lignes_lues = $fin.Count; lignes_erreur = $nbErreurs }
    if ($erreursPrecedentes -and $f -eq $erreursPrecedentes -and $nbErreurs -gt 0) {
        $quand = $null
        try { $quand = (Get-Item -LiteralPath $f -Force).LastWriteTime } catch { }
        Write-Conseil 'C''est le journal d''erreurs de la fois PRÉCÉDENTE : ce que le service a écrit juste avant de s''arrêter.'
        # Un arrêt DEMANDÉ du service interrompt Python par Ctrl+C : il laisse un Traceback
        # « KeyboardInterrupt » qui n'est pas une panne. On ne l'inscrit pas au verdict.
        if (@($fin | Where-Object { $_ -match 'KeyboardInterrupt' }).Count -gt 0) {
            Write-Conseil 'Il se termine par « KeyboardInterrupt » : c''est la trace d''un arrêt demandé, pas une panne.'
        } elseif ($quand -and ((Get-Date) - $quand).TotalHours -le 24) {
            # Signalé au verdict pendant 24 h seulement : au-delà, c'est de l'histoire ancienne.
            Add-Probleme 'alerte' ('Le service du Worker a écrit des erreurs avant son dernier arrêt, le ' + $quand.ToString('yyyy-MM-dd à HH:mm') + ' (il a pu être relancé depuis).') ($script:Menu.journaux + ', journal « service-erreurs-precedent » : la cause y est écrite')
        }
    }
}
if ($bruyants.Count -gt 0) { Add-Probleme 'alerte' ('Beaucoup d''erreurs récentes dans : ' + ($bruyants -join ', ') + '.') ($script:Menu.journaux + ', mode « erreurs »') }
$constats.journaux = $constatJournaux

# ---------------------------------------------------------------------------
# 10. Boîte locale
# ---------------------------------------------------------------------------
Write-Section 'Boîte aux lettres locale'
$constatBoite = [ordered]@{ presente = $false; demandes_en_attente = 0; demandes_bloquees = 0; resultats = 0; resultats_anciens = 0 }
$boite = Join-Path $Racine 'runtime\local-control'
if (-not (Test-Path -LiteralPath $boite -PathType Container)) {
    Write-Etat 'runtime\local-control' 'absente (créée à la première demande)' 'info'
} else {
    $constatBoite.presente = $true
    $maintenant = Get-Date
    $demandes = @(Get-ChildItem -LiteralPath $boite -Filter '*.request.json' -File -ErrorAction SilentlyContinue)
    $resultats = @(Get-ChildItem -LiteralPath $boite -Filter '*.result.json' -File -ErrorAction SilentlyContinue)
    $bloquees = @($demandes | Where-Object { ($maintenant - $_.LastWriteTime).TotalMinutes -gt 2 })
    # Même seuil que Nettoyage léger (7 jours) : on n'annonce que ce qu'il saura réellement retirer.
    $anciens = @($resultats | Where-Object { ($maintenant - $_.LastWriteTime).TotalDays -gt 7 })
    $constatBoite.demandes_en_attente = $demandes.Count; $constatBoite.demandes_bloquees = $bloquees.Count
    $constatBoite.resultats = $resultats.Count; $constatBoite.resultats_anciens = $anciens.Count
    if ($bloquees.Count -gt 0) {
        Write-Etat 'Demandes en attente' ([string]$demandes.Count + ', dont ' + $bloquees.Count + ' de plus de 2 minutes : l''agent ne les lit pas') 'alerte'
        if ($workerEteint) { Write-Conseil ('Normal tant que le Worker est éteint : aucune n''est lue. Celles de plus de 7 jours partent avec ' + $script:Menu.nettoyage + '.') }
        else { Add-Probleme 'alerte' ([string]$bloquees.Count + ' demande(s) locale(s) attendent depuis plus de 2 minutes : l''agent est arrêté ou bloqué.') ($script:Menu.redemarrer + ' (celles de plus de 7 jours partent ensuite avec ' + $script:Menu.nettoyage + ')') }
    } elseif ($demandes.Count -gt 0) { Write-Etat 'Demandes en attente' ([string]$demandes.Count + ' (récentes)') 'info' }
    else { Write-Etat 'Demandes en attente' 'aucune' 'ok' }
    if ($anciens.Count -gt 0) {
        Write-Etat 'Anciennes réponses' ([string]$anciens.Count + ' de plus de 7 jours (sans danger, mais inutiles)') 'info'
        Write-Conseil ('Pour les retirer : ' + $script:Menu.nettoyage + '.')
    } else { Write-Etat 'Anciennes réponses' ([string]$resultats.Count + ', aucune de plus de 7 jours') 'ok' }
}
$constats.boite_locale = $constatBoite

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
$tries = @(@($problemes | Where-Object { $_.Gravite -eq 'erreur' }) + @($problemes | Where-Object { $_.Gravite -eq 'alerte' }))
$constats.problemes = @($tries | ForEach-Object { [ordered]@{ gravite = $_.Gravite; probleme = $_.Probleme; outil = $_.Outil } })
$constats.nombre_erreurs = @($tries | Where-Object { $_.Gravite -eq 'erreur' }).Count
$constats.nombre_alertes = @($tries | Where-Object { $_.Gravite -eq 'alerte' }).Count

if ($Json) {
    [Console]::Out.WriteLine(($constats | ConvertTo-Json -Depth 8 -Compress))
    exit 0
}

if ($tries.Count -eq 0) {
    Write-Cadre -Titre 'VERDICT : tout va bien' -Lignes @('Aucun problème détecté par ce contrôle.') -Couleur Green
} else {
    $couleur = 'Yellow'
    if ($constats.nombre_erreurs -gt 0) { $couleur = 'Red' }
    Write-Cadre -Titre ('VERDICT : ' + $tries.Count + ' point(s) à traiter') -Lignes @(([string]$constats.nombre_erreurs + ' bloquant(s), ' + $constats.nombre_alertes + ' à surveiller. Traite-les dans l''ordre.')) -Couleur $couleur
    $n = 0
    foreach ($p in $tries) {
        $n++
        Write-Host ''
        if ($p.Gravite -eq 'erreur') { Write-Erreur ([string]$n + '. ' + $p.Probleme) } else { Write-Alerte ([string]$n + '. ' + $p.Probleme) }
        # « À lancer » seulement quand la consigne COMMENCE par un outil du menu, reconnu à son numéro.
        if (([string]$p.Outil) -match '^[^:;]*\(\d+\)') { Write-Conseil ('À lancer : ' + $p.Outil) }
        else { Write-Conseil ('À faire : ' + $p.Outil) }
    }
}
if (-not (Test-Administrateur)) {
    Write-Host ''
    Write-Info 'Contrôle fait sans fenêtre administrateur : c''est voulu. Les processus du service'
    Write-Info 'ne sont pas identifiables ainsi ; les ports et les fiches suffisent au diagnostic.'
}
Wait-FinOutil -SansPause:$SansPause
exit 0
