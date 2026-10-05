<#
    Menu du Worker Alpine Makers : tous les outils, regroupés et expliqués.

    Usage :
        menu_worker.ps1                 menu interactif
        menu_worker.ps1 -Apercu         affiche le menu UNE fois et sort (code 0), sans rien demander
        menu_worker.ps1 -Choix 9        lance directement l'entrée 9, puis sort avec le code de l'outil
        menu_worker.ps1 -Choix 17 -Oui  idem, en acceptant la confirmation ROUGE du MENU, et elle seule.
                                        -Oui n'est JAMAIS relayé à un outil de la zone rouge : l'outil garde
                                        sa propre confirmation (« SUPPRIMER IA » pour 17).

    Le catalogue ci-dessous est celui de TOUT Worker. Les entrées propres au PC du propriétaire
    (elles agissent sur des services qui ne sont pas ceux du Worker) vivent dans menu_proprietaire.ps1,
    livré seulement aux Workers du compte propriétaire : absent, le menu ne les connaît pas.

    -SansPause vaut aussi pour les entrées 5, 6, 7 et 17 (anciens .bat de la racine, qui finissent
    par un « pause ») : avec -Choix et -SansPause, ils sont lancés clavier fermé et ne bloquent pas.

    Les entrées 5, 6 et 17 passent par worker_tools.ps1 et ont besoin de l'AGENT EN MARCHE : il dépose
    une demande que l'agent doit lire. Sur un PC géré par le service AlpineWorker, worker_tools.ps1 ne
    lance plus rien dans ta session (sa garde) : service arrêté, la demande resterait sans réponse. Le
    menu refuse donc ces entrées (code 2) et renvoie à 12. Il les refuse aussi quand le service en
    marche sert un AUTRE dossier (ses ports sont déjà pris). Sans service du tout, worker_tools.ps1
    démarre le Worker dans ta session : le menu le dit et demande ton accord.

    Le menu ne demande JAMAIS l'élévation et ne modifie rien lui-même : il lit
    l'état sans droits particuliers, puis passe la main à l'outil choisi, dans
    la même console. C'est l'outil qui explique, confirme et, s'il le faut,
    ouvre une fenêtre administrateur.
#>
param(
    [string]$Racine = '',
    [switch]$Apercu,
    [string]$Choix = '',
    [switch]$Oui,
    [switch]$SansPause
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
Initialize-ConsoleWorker -Titre 'Alpine Makers - Menu du Worker'
try { $Racine = Get-RacineWorker -Racine $Racine }
catch {
    Write-Host ''
    Write-Erreur $_.Exception.Message
    Write-Conseil 'Lance MENU-WORKER.bat depuis le dossier du Worker (celui qui contient agent.py), ou indique-le avec -Racine.'
    Wait-FinOutil -SansPause:($SansPause -or $Apercu -or [bool]$Choix)
    exit 1
}

$script:LargeurMenu = 98           # le rendu tient dans une console de 100 colonnes
$script:StatusNonSupporte = $false # agent trop ancien : on ne repose pas la question à chaque rafraîchissement
$script:DossierOutilsMenu = $PSScriptRoot
$script:MessageRetour = $null      # ce qu'Invoke-Entree veut voir affiché APRÈS le redessin du menu

# ---------------------------------------------------------------------------
# Catalogue des entrées
# ---------------------------------------------------------------------------

function New-Entree {
    param([int]$Numero, [string]$Groupe, [string]$Nom, [string]$Description, [string]$Type, [string]$Fichier, [switch]$Admin, [switch]$Arret, [switch]$Rouge, [switch]$RelanceWorker)
    $chemin = ''
    if ($Type -eq 'ps1') { $chemin = Join-Path $script:DossierOutilsMenu $Fichier } else { $chemin = Join-Path $Racine $Fichier }
    return [pscustomobject]@{
        Numero = $Numero; Groupe = $Groupe; Nom = $Nom; Description = $Description; Type = $Type
        Fichier = $Fichier; Chemin = $chemin; Admin = [bool]$Admin; Arret = [bool]$Arret; Rouge = [bool]$Rouge
        # Vrai pour les .bat qui passent par worker_tools.ps1 : ils ont besoin de l'agent en marche. Sans
        # service AlpineWorker pour ce dossier, worker_tools.ps1 lance run_worker.ps1 dans la session.
        RelanceWorker = [bool]$RelanceWorker
    }
}

$script:Groupes = @(
    [pscustomobject]@{ Cle = 'diagnostic'; Titre = 'DIAGNOSTIC';        Aide = 'regarder sans rien changer' }
    [pscustomobject]@{ Cle = 'connexion';  Titre = 'CONNEXION';         Aide = 'le Worker n''apparaît plus en ligne dans le dashboard' }
    [pscustomobject]@{ Cle = 'moteurs';    Titre = 'MOTEURS ET WORKER'; Aide = 'arrêter, relancer, libérer la carte graphique' }
    [pscustomobject]@{ Cle = 'entretien';  Titre = 'ENTRETIEN';         Aide = 'sauvegarde, ménage, démarrage du PC' }
    [pscustomobject]@{ Cle = 'rouge';      Titre = 'ZONE ROUGE';        Aide = 'actions lourdes : lis bien l''explication avant de confirmer' }
)

# Nom : 30 caractères au plus. Description : UNE ligne, 45 caractères au plus.
$script:Entrees = @(
    (New-Entree  1 'diagnostic' 'État complet du Worker'        'Service, agent, moteurs, ports, disque, GPU.'  'ps1' 'etat_worker.ps1')
    (New-Entree  2 'diagnostic' 'Jobs bloquants'                'Ce qui empêche un arrêt ou une mise à jour.'   'ps1' 'jobs_bloquants.ps1')
    (New-Entree  3 'diagnostic' 'Journaux'                      'Dernières lignes et erreurs des journaux.'     'ps1' 'journaux.ps1')
    (New-Entree  4 'diagnostic' 'Rapport de diagnostic'         'Fichier à partager pour l''aide, sans secret.' 'ps1' 'rapport_diagnostic.ps1')

    (New-Entree  5 'connexion'  'Reconnecter le Worker'         'Relance la liaison (Worker allumé requis).'    'ps1' 'reconnecter.ps1' -RelanceWorker)
    (New-Entree  6 'connexion'  'Actualiser les adresses IP'    'Changement de réseau (Worker allumé requis).'   'ps1' 'actualiser_ip.ps1' -RelanceWorker)
    (New-Entree  7 'connexion'  'Récupérer une inscription'     'Retrouve la fiche existante sur le site.'      'ps1' 'recuperer_inscription.ps1')
    (New-Entree  8 'connexion'  'Réassocier le Worker'          'Après suppression de sa fiche sur le site.'    'ps1' 'reassocier_worker.ps1' -Admin)

    (New-Entree  9 'moteurs'    'Moteurs (état, arrêt, départ)' 'ComfyUI, Hunyuan3D, PrintGuard, bridge Orca.'  'ps1' 'moteurs.ps1')
    (New-Entree 10 'moteurs'    'Redémarrer l''agent'           'Relance propre quand il ne répond plus.'       'ps1' 'redemarrer_agent.ps1' -Admin)
    (New-Entree 11 'moteurs'    'Éteindre le Worker (lui seul)' 'Agent et moteurs ; le reste du PC continue.'   'ps1' 'eteindre_worker.ps1' -Admin -Arret)
    (New-Entree 12 'moteurs'    'Rallumer le Worker'            'Le remet en marche après une extinction.'      'ps1' 'rallumer_worker.ps1' -Admin)

    (New-Entree 13 'entretien'  'Sauvegarder le Worker'         'Copie config et état, sans les moteurs.'       'ps1' 'sauvegarder_worker.ps1')
    (New-Entree 14 'entretien'  'Nettoyage léger'               'Petits fichiers temporaires de plus de 7 j.'   'ps1' 'nettoyage_leger.ps1')
    (New-Entree 15 'entretien'  'Démarrage automatique'         'Lancement au démarrage, tâches orphelines.'    'ps1' 'demarrage_auto.ps1')
    (New-Entree 16 'entretien'  'Libérer la mémoire du PC'      'Ferme le superflu, garde Alpine Makers.'       'ps1' 'liberer_memoire.ps1')

    (New-Entree 17 'rouge'      'Supprimer les moteurs IA'      'Désinstalle les moteurs (Worker allumé).'      'ps1' 'supprimer_moteurs.ps1' -Arret -Rouge -RelanceWorker)
)

# Entrées du PC du propriétaire : le fichier n'est livré qu'à ses Workers. Il complète $script:Entrees
# (et la table des libellés du socle) à la suite des numéros ci-dessus.
$extensionProprietaire = Join-Path $PSScriptRoot 'menu_proprietaire.ps1'
if (Test-Path -LiteralPath $extensionProprietaire -PathType Leaf) { . $extensionProprietaire }

# Bornes affichées dans les messages (« 1 à N ») : celles du catalogue réellement chargé.
$script:NumeroMax = [int](@($script:Entrees | ForEach-Object { [int]$_.Numero }) | Measure-Object -Maximum).Maximum

# Les outils citent les entrées par la table du socle (Get-NomEntree) : elle doit dire EXACTEMENT la
# même chose que ce catalogue. Contrôlé à chaque lancement ; un écart s'affiche sous le bandeau.
$script:EcartsLibelles = @()
foreach ($e in $script:Entrees) { if ([string]$script:NomsEntrees[[int]$e.Numero] -cne [string]$e.Nom) { $script:EcartsLibelles += [int]$e.Numero } }
foreach ($n in @($script:NomsEntrees.Keys)) { if (@($script:Entrees | Where-Object { $_.Numero -eq $n }).Count -eq 0) { $script:EcartsLibelles += [int]$n } }

# Fenêtre basse (Windows Terminal ne se laisse pas agrandir) : affichage resserré, voir Write-Menu.
$script:Compact = $false

# ---------------------------------------------------------------------------
# Affichage
# ---------------------------------------------------------------------------

function New-Segment {
    param([string]$Texte, [ConsoleColor]$Couleur = 'Gray')
    return [pscustomobject]@{ Texte = $Texte; Couleur = $Couleur }
}

function Get-LargeurMarque {
    if ($script:ModeAscii) { return 4 }
    return 1
}

function Write-LigneEtat {
    <# Ligne du bandeau : marque colorée, libellé aligné, puis des segments de couleurs différentes. #>
    param([string]$Niveau = 'info', [string]$Libelle, [object[]]$Segments = @())
    $couleur = 'DarkCyan'; $marque = Get-Trait 'info'
    switch ($Niveau) {
        'ok'     { $couleur = 'Green';  $marque = Get-Trait 'ok' }
        'alerte' { $couleur = 'Yellow'; $marque = Get-Trait 'att' }
        'erreur' { $couleur = 'Red';    $marque = Get-Trait 'non' }
    }
    $largeurMarque = Get-LargeurMarque
    Write-Host ('   ' + $marque.PadRight($largeurMarque) + ' ') -ForegroundColor $couleur -NoNewline
    Write-Host ($Libelle.PadRight(15) + ' ') -ForegroundColor Gray -NoNewline
    $reste = $script:LargeurMenu - (3 + $largeurMarque + 1 + 16)
    foreach ($s in $Segments) {
        if ($reste -le 0) { break }
        $t = [string]$s.Texte
        if ($t.Length -gt $reste) { $t = $t.Substring(0, [Math]::Max(0, $reste - 3)) + '...' }
        Write-Host $t -ForegroundColor $s.Couleur -NoNewline
        $reste -= $t.Length
    }
    Write-Host ''
}

function Write-LigneAlerte {
    <# Une alerte peut être longue : elle est coupée entre deux mots et poursuivie sous le texte. #>
    param([string]$Texte)
    $marge = 3 + (Get-LargeurMarque) + 1 + 16
    $largeur = $script:LargeurMenu - $marge
    $lignes = @(); $courante = ''
    foreach ($mot in ($Texte -split '\s+')) {
        if (-not $mot) { continue }
        if ($courante -and (($courante.Length + 1 + $mot.Length) -gt $largeur)) { $lignes += $courante; $courante = $mot }
        elseif ($courante) { $courante = $courante + ' ' + $mot }
        else { $courante = $mot }
    }
    if ($courante) { $lignes += $courante }
    $premiere = $true
    foreach ($l in $lignes) {
        if ($premiere) { Write-LigneEtat -Niveau 'alerte' -Libelle 'À savoir' -Segments @((New-Segment $l 'Yellow')); $premiere = $false }
        else { Write-Host ((' ' * $marge) + $l) -ForegroundColor Yellow }
    }
}

function Write-MessageRetour {
    <# Message laissé par le dernier outil, affiché SOUS le menu redessiné, coupé entre deux mots. #>
    param([string]$Texte)
    $largeur = $script:LargeurMenu - 8
    $courante = ''; $premiere = $true
    foreach ($mot in ($Texte -split '\s+')) {
        if (-not $mot) { continue }
        if ($courante -and (($courante.Length + 1 + $mot.Length) -gt $largeur)) {
            if ($premiere) { Write-Alerte $courante; $premiere = $false } else { Write-Host ('     ' + $courante) -ForegroundColor Yellow }
            $courante = $mot
        }
        elseif ($courante) { $courante = $courante + ' ' + $mot }
        else { $courante = $mot }
    }
    if ($courante) {
        if ($premiere) { Write-Alerte $courante } else { Write-Host ('     ' + $courante) -ForegroundColor Yellow }
    }
}

function Get-Pastille {
    param([bool]$Allume)
    if ($script:ModeAscii) { if ($Allume) { return 'ON' } else { return '--' } }
    if ($Allume) { return [string][char]0x25CF }
    return [string][char]0x25CB
}

function Get-VramNvidia {
    <# VRAM par nvidia-smi, délai court. Renvoie $null si l'outil est absent, muet ou trop lent. #>
    $exe = $null
    $cmd = Get-Command 'nvidia-smi.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($cmd) { $exe = $cmd.Source }
    if (-not $exe) {
        $candidat = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe'
        if (Test-Path -LiteralPath $candidat -PathType Leaf) { $exe = $candidat }
    }
    if (-not $exe) { return $null }
    $p = $null
    try {
        $info = New-Object System.Diagnostics.ProcessStartInfo
        $info.FileName = $exe
        $info.Arguments = '--query-gpu=name,memory.used,memory.total --format=csv,noheader,nounits'
        $info.UseShellExecute = $false
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $info.CreateNoWindow = $true
        $p = [System.Diagnostics.Process]::Start($info)
        $lecture = $p.StandardOutput.ReadToEndAsync()
        if (-not $p.WaitForExit(4000)) { try { $p.Kill() } catch { }; return $null }
        $texte = [string]$lecture.Result
        $cartes = @()
        foreach ($ligne in ($texte -split "`r?`n")) {
            $morceaux = @($ligne -split ',' | ForEach-Object { $_.Trim() })
            if ($morceaux.Count -lt 3) { continue }
            $utilise = 0.0; $total = 0.0
            if (-not [double]::TryParse($morceaux[1], [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$utilise)) { continue }
            if (-not [double]::TryParse($morceaux[2], [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$total)) { continue }
            if ($total -le 0) { continue }
            $cartes += [pscustomobject]@{
                Nom = ($morceaux[0] -replace '^NVIDIA\s+', '')
                UtiliseGio = [Math]::Round($utilise / 1024, 1); TotalGio = [Math]::Round($total / 1024, 1)
                Pourcent = [int][Math]::Round(100 * $utilise / $total)
            }
        }
        if ($cartes.Count -eq 0) { return $null }
        return $cartes
    } catch {
        return $null
    } finally {
        if ($p) { try { $p.Dispose() } catch { } }
    }
}

function Get-EtatBandeau {
    <# Tout ce que le bandeau affiche, lu SANS élévation. Chaque lecture tolère l'échec. #>
    $etat = [pscustomobject]@{
        Config = $null; Version = ''; Lancement = $null; Moteurs = @(); Vram = $null
        AgentNiveau = 'info'; AgentTexte = ''; AgentDetail = ''; Alertes = @()
    }
    try { $etat.Config = Get-ConfigSure -Racine $Racine } catch { }
    try { $etat.Version = Get-VersionInstallee -Racine $Racine } catch { }
    try { $etat.Lancement = Get-ModeLancement -Racine $Racine } catch { }
    try { $etat.Moteurs = @(Get-EtatMoteurs -Racine $Racine) } catch { $etat.Moteurs = @() }
    try { $etat.Vram = Get-VramNvidia } catch { }

    $serviceDemarre = $false; $tacheEnCours = $false; $serviceArrete = $false; $eteintVolontaire = $false
    if ($etat.Lancement) {
        $svc = $etat.Lancement.Service
        if ($svc -and $svc.ViseCeDossier) {
            if ($svc.Etat -eq 'Running') { $serviceDemarre = $true } else { $serviceArrete = $true }
            if ($svc.Etat -eq 'Stopped' -and $svc.Demarrage -ne 'Auto') { $eteintVolontaire = $true }
        }
        if (@($etat.Lancement.Taches | Where-Object { $_.ViseCeDossier -and $_.Etat -eq 'Running' }).Count -gt 0) { $tacheEnCours = $true }
    }
    $relais = $false
    try { $relais = Test-AgentVivant } catch { }
    $moteursActifs = @($etat.Moteurs | Where-Object { $_.EnEcoute }).Count

    # Agent : d'abord la boîte aux lettres locale (si l'agent installé la connaît), sinon des indices locaux.
    # Service de ce dossier arrêté, ni relais ni tâche : la réponse est connue d'avance, on n'attend pas 4 s
    # à chaque redessin pour l'apprendre.
    $statut = $null
    $questionInutile = ($serviceArrete -and -not $relais -and -not $tacheEnCours)
    if (-not $script:StatusNonSupporte -and -not $questionInutile) {
        try {
            $r = Invoke-ActionLocale -Racine $Racine -Action 'status' -DelaiSecondes 4
            if ($r.NonSupporte) { $script:StatusNonSupporte = $true }
            elseif ($r.Ok -and $r.Resultat) { $statut = $r.Resultat }
        } catch { }
    }

    if ($statut) {
        $etat.AgentNiveau = 'ok'; $etat.AgentTexte = 'en ligne, il répond'
        $details = @()
        $jobs = @($statut.jobs_active).Count
        if ($jobs -gt 0) { $details += ('{0} job(s) en cours' -f $jobs); $etat.AgentNiveau = 'alerte' }
        if ($statut.maintenance_active) { $details += 'maintenance en cours'; $etat.AgentNiveau = 'alerte' }
        if ($statut.disconnecting) { $details += 'déconnexion en cours'; $etat.AgentNiveau = 'alerte' }
        $etat.AgentDetail = ($details -join ', ')
    }
    elseif ($relais)         { $etat.AgentNiveau = 'ok';     $etat.AgentTexte = 'en marche'; $etat.AgentDetail = 'son relais caméra répond' }
    elseif ($serviceDemarre) { $etat.AgentNiveau = 'ok';     $etat.AgentTexte = 'en marche'; $etat.AgentDetail = 'le service est démarré' }
    elseif ($tacheEnCours)   { $etat.AgentNiveau = 'ok';     $etat.AgentTexte = 'en marche'; $etat.AgentDetail = 'la tâche planifiée tourne' }
    elseif ($moteursActifs)  { $etat.AgentNiveau = 'alerte'; $etat.AgentTexte = 'incertain'; $etat.AgentDetail = 'moteurs ouverts sans agent : 11 les arrête, 12 rallume' }
    elseif ($eteintVolontaire) { $etat.AgentNiveau = 'alerte'; $etat.AgentTexte = 'éteint';  $etat.AgentDetail = 'volontairement : utilise 12 pour le rallumer' }
    else                     { $etat.AgentNiveau = 'erreur'; $etat.AgentTexte = 'arrêté';    $etat.AgentDetail = 'aucun signe de vie : utilise 12 pour le rallumer' }

    $alertes = @()
    if (-not $etat.Config -or -not $etat.Config.Present) { $alertes += 'Aucune configuration : ce Worker n''a jamais été associé au dashboard.' }
    elseif (-not $etat.Config.Associe) { $alertes += 'Ce Worker n''est pas associé au dashboard : utilise 8 (Réassocier).' }
    if (Test-Path -LiteralPath (Join-Path $Racine '.worker-detached')) { $alertes += 'Sa fiche a été SUPPRIMÉE du site (.worker-detached) : utilise 8 (Réassocier).' }
    if (Test-Path -LiteralPath (Join-Path $Racine '.worker-disconnected')) { $alertes += 'Il a été déconnecté depuis le dashboard (.worker-disconnected) : utilise 5 (Reconnecter).' }
    if ($etat.Lancement) {
        $orphelines = @($etat.Lancement.Taches | Where-Object { $_.Orpheline })
        if ($orphelines.Count -gt 0) { $alertes += ('{0} tâche(s) planifiée(s) orpheline(s), dont le dossier a disparu : utilise 15.' -f $orphelines.Count) }
        $illisibles = @($etat.Lancement.Taches | Where-Object { $_.Illisible })
        if ($illisibles.Count -gt 0) { $alertes += ('{0} tâche(s) planifiée(s) du Worker au dossier illisible : 15 les montre, vérifie-les dans le Planificateur de tâches.' -f $illisibles.Count) }
        if ($etat.Lancement.Mode -eq 'tache' -and -not $tacheEnCours -and @($etat.Lancement.Taches | Where-Object { $_.ViseCeDossier -and $_.Etat -ne 'Disabled' }).Count -eq 0) { $alertes += 'La tâche planifiée de ce dossier est DÉSACTIVÉE : utilise 12 (Worker éteint par 11), sinon 15.' }
        if ($etat.Lancement.Service -and -not $etat.Lancement.Service.ViseCeDossier) { $alertes += 'Le service AlpineWorker vise un AUTRE dossier : les outils d''ici ne le toucheront pas.' }
        if ($etat.Lancement.Mode -eq 'aucun') { $alertes += 'Aucun démarrage automatique ne vise ce dossier : utilise 15.' }
    }
    try {
        if (Test-Path -LiteralPath (Join-Path $env:ProgramData 'AlpineMakers\pause-supervision.flag')) { $alertes += 'La supervision de TOUT le PC est en pause : rien ne sera relancé automatiquement.' }
    } catch { }
    if ($script:StatusNonSupporte) { $alertes += 'Agent trop ancien pour dialoguer avec les outils (arrêt propre des moteurs) : mets le Worker à jour depuis le site (Mes Workers > « Mettre à jour »).' }
    if ($script:EcartsLibelles.Count -gt 0) { $alertes += ('Paquet d''outils incohérent : les entrées ' + (($script:EcartsLibelles | Sort-Object -Unique) -join ', ') + ' n''ont pas le même nom dans le menu et dans commun.ps1. Mets le Worker à jour.') }
    $etat.Alertes = $alertes
    return $etat
}

function Write-Bandeau {
    param([object]$Etat)
    $nom = ''; $id = ''
    if ($Etat.Config) { $nom = [string]$Etat.Config.Nom; $id = [string]$Etat.Config.WorkerIdCourt }
    if (-not $nom) { $nom = '(sans nom)' }
    Write-Cadre -Titre 'ALPINE MAKERS   -   MENU DU WORKER' -Lignes @('Tous les outils de ce Worker, expliqués. Tape un numéro, puis Entrée.', ('Dossier : ' + $Racine)) -Largeur ($script:LargeurMenu - 2)
    Write-Host ''

    # Worker
    $segments = @((New-Segment $nom 'White'))
    if ($id) { $segments += (New-Segment ('   identifiant ' + $id + '...') 'Gray') }
    if ($Etat.Version) { $segments += (New-Segment ('   version ' + $Etat.Version) 'Gray') } else { $segments += (New-Segment '   version inconnue' 'DarkGray') }
    Write-LigneEtat -Niveau 'info' -Libelle 'Worker' -Segments $segments

    # Lancement
    $l = $Etat.Lancement
    if (-not $l) { Write-LigneEtat -Niveau 'alerte' -Libelle 'Lancement' -Segments @((New-Segment 'illisible sur ce PC' 'Yellow')) }
    elseif ($l.Mode -eq 'service') {
        $etatSvc = [string]$l.Service.Etat; $niveau = 'alerte'; $couleur = 'Yellow'
        switch ([string]$l.Service.Etat) {
            'Running'       { $etatSvc = 'démarré'; $niveau = 'ok'; $couleur = 'Green' }
            'Stopped'       { $etatSvc = 'arrêté'; $niveau = 'erreur'; $couleur = 'Red' }
            'Start Pending' { $etatSvc = 'en cours de démarrage' }
            'Stop Pending'  { $etatSvc = 'en cours d''arrêt' }
        }
        $demarrage = [string]$l.Service.Demarrage
        switch ([string]$l.Service.Demarrage) {
            'Auto'     { $demarrage = 'démarrage automatique' }
            'Manual'   { $demarrage = 'démarrage manuel (pas de relance)' }
            'Disabled' { $demarrage = 'désactivé' }
        }
        if ([string]$l.Service.Etat -eq 'Stopped' -and [string]$l.Service.Demarrage -ne 'Auto') {
            # Arrêt VOULU (état laissé par 11) : jaune, pas rouge, et la marche à suivre tient sur la ligne.
            Write-LigneEtat -Niveau 'alerte' -Libelle 'Lancement' -Segments @((New-Segment 'service AlpineWorker : ' 'Gray'), (New-Segment 'éteint VOLONTAIREMENT' 'Yellow'), (New-Segment ' - 12 pour le rallumer' 'Gray'))
        }
        else { Write-LigneEtat -Niveau $niveau -Libelle 'Lancement' -Segments @((New-Segment 'service Windows AlpineWorker : ' 'Gray'), (New-Segment $etatSvc $couleur), (New-Segment (', ' + $demarrage) 'Gray')) }
    }
    elseif ($l.Mode -eq 'tache') {
        $t = @($l.Taches | Where-Object { $_.ViseCeDossier }) | Select-Object -First 1
        $etatTache = [string]$t.Etat
        switch ([string]$t.Etat) { 'Running' { $etatTache = 'en cours' } 'Ready' { $etatTache = 'prête' } 'Disabled' { $etatTache = 'désactivée' } }
        $niveauTache = 'ok'
        if ([string]$t.Etat -eq 'Disabled') { $niveauTache = 'alerte' }
        Write-LigneEtat -Niveau $niveauTache -Libelle 'Lancement' -Segments @((New-Segment ('tâche planifiée : ' + $etatTache + '   (' + $t.Nom + ')') 'Gray'))
    }
    else { Write-LigneEtat -Niveau 'alerte' -Libelle 'Lancement' -Segments @((New-Segment 'manuel : ni service ni tâche planifiée pour ce dossier' 'Yellow')) }

    # Agent
    $couleurAgent = 'Gray'
    switch ($Etat.AgentNiveau) { 'ok' { $couleurAgent = 'Green' } 'alerte' { $couleurAgent = 'Yellow' } 'erreur' { $couleurAgent = 'Red' } }
    $segments = @((New-Segment $Etat.AgentTexte $couleurAgent))
    if ($Etat.AgentDetail) { $segments += (New-Segment ('  (' + $Etat.AgentDetail + ')') 'Gray') }
    Write-LigneEtat -Niveau $Etat.AgentNiveau -Libelle 'Agent' -Segments $segments

    # Moteurs : une pastille colorée par moteur
    $total = @($Etat.Moteurs).Count
    if ($total -eq 0) { Write-LigneEtat -Niveau 'alerte' -Libelle 'Moteurs' -Segments @((New-Segment 'état illisible' 'Yellow')) }
    else {
        $actifs = @($Etat.Moteurs | Where-Object { $_.EnEcoute }).Count
        $niveau = 'ok'
        if ($actifs -eq 0) { $niveau = 'info' }
        $segments = @((New-Segment ('{0}/{1} en marche' -f $actifs, $total) 'White'))
        $muets = 0
        foreach ($m in $Etat.Moteurs) {
            $couleur = 'DarkGray'
            if ($m.EnEcoute) { $couleur = 'Green' } elseif ($m.FichePresente) { $couleur = 'Yellow'; $niveau = 'alerte'; $muets++ }
            $court = ([string]$m.Nom -replace '\s*\(.*\)$', '')
            $segments += (New-Segment ('   ' + (Get-Pastille -Allume ([bool]$m.EnEcoute)) + ' ' + $court) $couleur)
        }
        Write-LigneEtat -Niveau $niveau -Libelle 'Moteurs' -Segments $segments
        if ($muets -gt 0) {
            Write-Host ((' ' * (3 + (Get-LargeurMarque) + 17)) + 'vert = en marche   jaune = attendu mais muet   gris = arrêté') -ForegroundColor DarkGray
        }
    }

    # Carte graphique
    if ($Etat.Vram) {
        foreach ($c in @($Etat.Vram)) {
            $niveau = 'ok'; $couleur = 'Green'
            if ($c.Pourcent -ge 90) { $niveau = 'erreur'; $couleur = 'Red' } elseif ($c.Pourcent -ge 70) { $niveau = 'alerte'; $couleur = 'Yellow' }
            $plein = [int][Math]::Round($c.Pourcent / 5.0)
            if ($plein -gt 20) { $plein = 20 }
            $carPlein = [string][char]0x2588; $carVide = [string][char]0x2591
            if ($script:ModeAscii) { $carPlein = '#'; $carVide = '.' }
            $jauge = ($carPlein * $plein) + ($carVide * (20 - $plein))
            Write-LigneEtat -Niveau $niveau -Libelle 'Mémoire vidéo' -Segments @((New-Segment ($jauge + ' ') $couleur), (New-Segment ('{0} / {1} Gio ({2} %)' -f $c.UtiliseGio, $c.TotalGio, $c.Pourcent) $couleur), (New-Segment ('   ' + $c.Nom) 'Gray'))
        }
    }
    else { Write-LigneEtat -Niveau 'info' -Libelle 'Mémoire vidéo' -Segments @((New-Segment 'non mesurée (nvidia-smi absent ou muet)' 'DarkGray')) }

    foreach ($a in @($Etat.Alertes)) { Write-LigneAlerte -Texte $a }
}

function Write-Entree {
    param([object]$Entree)
    $disponible = Test-Path -LiteralPath $Entree.Chemin -PathType Leaf
    $numero = ('[{0,2}]' -f $Entree.Numero)
    $badges = ''
    if ($Entree.Admin) { $badges += '[ADMIN]' }
    if ($Entree.Arret) { $badges += '[ARRET]' }
    $largeurNom = 30
    $largeurDescription = $script:LargeurMenu - (2 + 4 + 1 + $largeurNom + 1 + 1 + 14)
    $nom = [string]$Entree.Nom
    if ($nom.Length -gt $largeurNom) { $nom = $nom.Substring(0, $largeurNom) }
    $description = [string]$Entree.Description
    if (-not $disponible) { $description = '(indisponible) ' + $Entree.Fichier + ' absent' }
    if ($description.Length -gt $largeurDescription) { $description = $description.Substring(0, $largeurDescription - 3) + '...' }

    if (-not $disponible) {
        Write-Host ('  ' + $numero + ' ' + $nom.PadRight($largeurNom) + ' ' + $description.PadRight($largeurDescription) + ' ' + $badges) -ForegroundColor DarkGray
        return
    }
    $couleurNumero = 'Cyan'; $couleurNom = 'White'
    if ($Entree.Rouge) { $couleurNumero = 'Red'; $couleurNom = 'Red' }
    Write-Host ('  ' + $numero + ' ') -ForegroundColor $couleurNumero -NoNewline
    Write-Host ($nom.PadRight($largeurNom) + ' ') -ForegroundColor $couleurNom -NoNewline
    Write-Host ($description.PadRight($largeurDescription) + ' ') -ForegroundColor Gray -NoNewline
    if ($Entree.Admin) { Write-Host '[ADMIN]' -ForegroundColor Yellow -NoNewline }
    if ($Entree.Arret) { Write-Host '[ARRET]' -ForegroundColor Red -NoNewline }
    Write-Host ''
}

function Write-Menu {
    param([object]$Etat)
    Write-Bandeau -Etat $Etat
    foreach ($g in $script:Groupes) {
        $entreesGroupe = @($script:Entrees | Where-Object { $_.Groupe -eq $g.Cle })
        if ($entreesGroupe.Count -eq 0) { continue }   # pas de titre de section sans entrée
        $couleurTitre = 'White'
        if ($g.Cle -eq 'rouge') { $couleurTitre = 'Red' }
        # Affichage resserré (fenêtre de moins de 50 lignes) : ni ligne vide ni trait entre les groupes.
        if (-not $script:Compact) { Write-Host '' }
        Write-Host ('  ' + $g.Titre) -ForegroundColor $couleurTitre -NoNewline
        Write-Host ('   ' + $g.Aide) -ForegroundColor DarkGray
        if (-not $script:Compact) { Write-Host ('  ' + ('-' * ($script:LargeurMenu - 2))) -ForegroundColor DarkGray }
        foreach ($e in $entreesGroupe) { Write-Entree -Entree $e }
    }
    Write-Host ''
    Write-Host ('  ' + ('-' * ($script:LargeurMenu - 2))) -ForegroundColor DarkGray
    Write-Host '  [ A] ' -ForegroundColor Cyan -NoNewline; Write-Host 'Aide (ouvre le mode d''emploi)      ' -ForegroundColor White -NoNewline
    Write-Host '[ R] ' -ForegroundColor Cyan -NoNewline;   Write-Host 'Rafraîchir l''état      ' -ForegroundColor White -NoNewline
    Write-Host '[ Q] ' -ForegroundColor Cyan -NoNewline;   Write-Host 'Quitter' -ForegroundColor White
    Write-Host '  ' -NoNewline
    Write-Host '[ADMIN]' -ForegroundColor Yellow -NoNewline; Write-Host ' ouvrira une fenêtre administrateur      ' -ForegroundColor DarkGray -NoNewline
    Write-Host '[ARRET]' -ForegroundColor Red -NoNewline;    Write-Host ' arrête quelque chose' -ForegroundColor DarkGray
    if ($script:Compact) {
        # Le bandeau a pu sortir par le haut de la fenêtre : l'essentiel est redit juste au-dessus de l'invite.
        $synthese = 'Agent : ' + [string]$Etat.AgentTexte
        $nbAlertes = @($Etat.Alertes).Count
        if ($nbAlertes -gt 0) { $synthese += ('   -   ' + $nbAlertes + ' alerte(s) « À savoir » plus haut : remonte pour les lire') }
        $couleurSynthese = 'Gray'
        switch ($Etat.AgentNiveau) { 'ok' { $couleurSynthese = 'Green' } 'alerte' { $couleurSynthese = 'Yellow' } 'erreur' { $couleurSynthese = 'Red' } }
        if ($nbAlertes -gt 0 -and $couleurSynthese -eq 'Green') { $couleurSynthese = 'Yellow' }
        Write-Host ('  ' + $synthese) -ForegroundColor $couleurSynthese
    }
}

# ---------------------------------------------------------------------------
# Lancement d'une entrée
# ---------------------------------------------------------------------------

function ConvertTo-CodeSur {
    <# 64 et 75 sont réservés par le superviseur : le menu ne les relaie jamais. #>
    param([int]$Code)
    if ($Code -eq 64 -or $Code -eq 75) { return 1 }
    return $Code
}

function Test-ParametreOutil {
    <# Vrai si le script déclare ce paramètre : évite une erreur de liaison sur un outil qui ne le connaît pas. #>
    param([string]$Chemin, [string]$Parametre)
    try { return [bool]((Get-Command -Name $Chemin -ErrorAction Stop).Parameters.ContainsKey($Parametre)) } catch { return $false }
}

function Get-VerdictRelance {
    <#
        Les entrées 5, 6 et 17 passent par worker_tools.ps1 : il dépose une demande que l'AGENT doit lire.
        Sa garde : quand le service AlpineWorker vise ce dossier, il ne lance plus rien dans la session.
        Fonction pure (aucune lecture) :
          'ok'            = le Worker tourne déjà ;
          'refuser'       = le service de ce dossier n'est pas démarré : la demande resterait sans réponse
                            (120 s d'attente pour rien, et pour 17 une suppression exécutée plus tard) ;
          'refuser-autre' = un service en marche sert un AUTRE dossier : worker_tools.ps1 démarrerait ici
                            un second Worker sur des ports déjà pris ;
          'confirmer'     = pas de service : l'outil va démarrer le Worker dans cette session.
    #>
    param([object]$Lancement, [bool]$AgentVivant)
    if (-not $Lancement) { return 'confirmer' }
    if ($Lancement.Mode -eq 'service') {
        if ([string]$Lancement.Service.Etat -eq 'Running') { return 'ok' }
        return 'refuser'
    }
    if ($Lancement.Service -and -not $Lancement.Service.ViseCeDossier -and [string]$Lancement.Service.Etat -eq 'Running') { return 'refuser-autre' }
    if ($AgentVivant) { return 'ok' }
    if (@($Lancement.Taches | Where-Object { $_.ViseCeDossier -and $_.Etat -eq 'Running' }).Count -gt 0) { return 'ok' }
    return 'confirmer'
}

function Invoke-LanceurRacine {
    <#
        Lance un .bat historique de la racine dans la MÊME console et renvoie son code de sortie.
        Ces .bat finissent tous par un « pause » inconditionnel. Avec -ClavierFerme, l'entrée standard
        du .bat est un tube fermé aussitôt : « pause » rend la main tout de suite, rien ne bloque.
        Sans -ClavierFerme, le .bat hérite du clavier et garde sa pause (usage interactif).
    #>
    param([string]$Chemin, [switch]$ClavierFerme, [string]$Arguments = '')
    # -Arguments : texte FIXE écrit par le menu (jamais une saisie), ajouté après le chemin cité ; le .bat le relaie par %*.
    if ($Arguments -and $Arguments -notmatch '^[A-Za-z0-9 -]+$') { throw 'Arguments de lanceur invalides.' }
    $suite = ''
    if ($Arguments) { $suite = ' ' + $Arguments }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $env:ComSpec
    # cmd /c "<commande>" retire la paire de guillemets extérieure : le chemin reste cité (espace, accent).
    $info.Arguments = '/d /c ""' + $Chemin + '"' + $suite + '"'
    $info.WorkingDirectory = $Racine
    $info.UseShellExecute = $false
    if ($ClavierFerme) { $info.RedirectStandardInput = $true }
    $p = [System.Diagnostics.Process]::Start($info)
    try {
        if ($ClavierFerme) { $p.StandardInput.Close() }
        $p.WaitForExit()
        return [int]$p.ExitCode
    } finally {
        $p.Dispose()
    }
}

function Invoke-Entree {
    <# Lance l'outil dans la MÊME console. Renvoie son code de sortie (1 indisponible ou planté, 2 refusé). #>
    param([object]$Entree, [switch]$Direct)
    if (-not (Test-Path -LiteralPath $Entree.Chemin -PathType Leaf)) {
        Write-Host ''
        Write-Alerte ('« ' + $Entree.Nom + ' » est indisponible : ' + $Entree.Fichier + ' est absent de ce Worker.')
        Write-Conseil 'Mets le Worker à jour depuis le dashboard : les outils manquants seront réinstallés.'
        return 1
    }
    if ($Entree.Rouge) {
        Write-Host ''
        Write-Alerte ('ZONE ROUGE : « ' + $Entree.Nom + ' ». ' + $Entree.Description)
        if ($Entree.Type -eq 'bat') { Write-Alerte 'Cet ancien outil ne redemande PAS de confirmation : c''est ici que tu décides.' }
        if (-not (Confirm-Action -Question 'Tu veux vraiment ouvrir cet outil ?' -MotExact 'ROUGE' -Oui:$Oui)) {
            Write-Info 'Annulé : rien n''a été lancé.'
            $script:MessageRetour = 'Annulé : rien n''a été lancé.'
            return 2
        }
    }
    if ($Entree.RelanceWorker) {
        $lancement = $null; $vivant = $false
        try { $lancement = Get-ModeLancement -Racine $Racine } catch { }
        try { $vivant = [bool](Test-AgentVivant) } catch { }
        $verdict = Get-VerdictRelance -Lancement $lancement -AgentVivant $vivant
        if ($verdict -eq 'refuser' -or $verdict -eq 'refuser-autre') {
            $refus = 'Le Worker est éteint (service AlpineWorker non démarré) : « ' + $Entree.Nom + ' » a besoin de l''agent en marche, sa demande resterait sans réponse. Rallume-le d''abord avec 12, puis relance cette entrée.'
            if ($verdict -eq 'refuser-autre') { $refus = 'Un service AlpineWorker en marche sert un AUTRE dossier (' + [string]$lancement.Service.Dossier + ') : « ' + $Entree.Nom + ' » démarrerait ici un second Worker sur des ports déjà pris. Utilise le menu de ce dossier-là.' }
            Write-Host ''
            Write-Alerte $refus
            $script:MessageRetour = $refus
            return 2
        }
        if ($verdict -eq 'confirmer') {
            Write-Host ''
            Write-Alerte 'Aucun signe du Worker : cet outil va d''abord le DÉMARRER dans ta session (agent, puis moteurs), avant d''agir.'
            if (-not (Confirm-Action -Question 'Tu veux continuer ?' -Oui:$Oui)) {
                Write-Info 'Annulé : rien n''a été lancé.'
                $script:MessageRetour = 'Annulé : rien n''a été lancé.'
                return 2
            }
        }
    }
    $code = 0
    try {
        if ($Entree.Type -eq 'ps1') {
            $global:LASTEXITCODE = 0
            $parametres = @{ Racine = $Racine }
            if ($Direct -and $SansPause -and (Test-ParametreOutil -Chemin $Entree.Chemin -Parametre 'SansPause')) { $parametres['SansPause'] = $true }
            # -Oui n'est JAMAIS relayé à une entrée rouge : il a déjà servi à la confirmation ROUGE du menu, il ne
            # doit pas lever en plus les confirmations propres à l'outil (mot exact à taper, /oui du .bat).
            if ($Direct -and $Oui -and -not $Entree.Rouge -and (Test-ParametreOutil -Chemin $Entree.Chemin -Parametre 'Oui')) { $parametres['Oui'] = $true }
            # Out-Host : ce que l'outil écrit s'affiche, et ne se mélange pas à la valeur renvoyée ici.
            & $Entree.Chemin @parametres | Out-Host
            if ($null -ne $LASTEXITCODE) { $code = [int]$LASTEXITCODE }
        }
        else {
            $argumentsLanceur = ''
            if ($Entree.Numero -eq 7) {
                # install_windows.ps1 demande « Démarrer automatiquement… ? [O/N] ». Sur un PC lancé par le service,
                # répondre O créerait une tâche planifiée, second lanceur du même dossier : la question est supprimée.
                $lancementActuel = $null
                try { $lancementActuel = Get-ModeLancement -Racine $Racine } catch { }
                if ($lancementActuel -and $lancementActuel.Mode -eq 'service') {
                    $argumentsLanceur = '-AutoStart No'
                    Write-Host ''
                    Write-Info 'Ce PC lance le Worker par le service Windows : aucune tâche planifiée ne sera créée (la question du démarrage automatique ne sera pas posée).'
                }
            }
            $code = Invoke-LanceurRacine -Chemin $Entree.Chemin -ClavierFerme:([bool]($Direct -and $SansPause)) -Arguments $argumentsLanceur
        }
    } catch {
        Write-Host ''
        Write-Erreur ('L''outil « ' + $Entree.Nom + ' » s''est arrêté sur une erreur : ' + $_.Exception.Message)
        Write-Conseil 'Le menu continue. Lance 4 (Rapport de diagnostic) si le problème se répète.'
        # Le menu va être redessiné (Clear-Host) : la cause est gardée pour être réaffichée dessous.
        $script:MessageRetour = ('« ' + $Entree.Nom + ' » s''est arrêté sur une erreur : ' + $_.Exception.Message + ' Lance 4 (Rapport de diagnostic) si cela se répète.')
        $code = 1
    }
    try { Initialize-ConsoleWorker -Titre 'Alpine Makers - Menu du Worker' } catch { }
    return (ConvertTo-CodeSur -Code $code)
}

function Show-Aide {
    $lisezMoi = Join-Path $Racine 'README.md'
    if (-not (Test-Path -LiteralPath $lisezMoi -PathType Leaf)) {
        Write-Alerte 'README.md est absent de ce Worker : mets le Worker à jour pour le récupérer.'
        return
    }
    try {
        Start-Process -FilePath 'notepad.exe' -ArgumentList (ConvertTo-ArgumentCite $lisezMoi) | Out-Null
        Write-Ok 'Le mode d''emploi est ouvert dans le Bloc-notes.'
    } catch {
        Write-Alerte ('Impossible d''ouvrir le Bloc-notes. Le fichier est ici : ' + $lisezMoi)
    }
}

# ---------------------------------------------------------------------------
# Déroulé
# ---------------------------------------------------------------------------

if ($Apercu) {
    Write-Menu -Etat (Get-EtatBandeau)
    Write-Host ''
    exit 0
}

if ($Choix) {
    $texte = $Choix.Trim()
    $numero = 0
    if (-not [int]::TryParse($texte, [ref]$numero)) { Write-Erreur ('-Choix attend un numéro du menu (1 à ' + $script:NumeroMax + '), pas « ' + $texte + ' ».'); exit 1 }
    $entree = $script:Entrees | Where-Object { $_.Numero -eq $numero } | Select-Object -First 1
    if (-not $entree) { Write-Erreur ('Aucune entrée ' + $numero + ' dans le menu (1 à ' + $script:NumeroMax + ').'); exit 1 }
    exit (Invoke-Entree -Entree $entree -Direct)
}

# Le menu fait une cinquantaine de lignes : on agrandit la fenêtre quand la console le permet
# (console Windows classique ; sous Windows Terminal l'appel est sans effet et on continue).
try {
    $ui = $Host.UI.RawUI
    $tampon = $ui.BufferSize
    if ($tampon.Width -lt 100) { $tampon.Width = 100; $ui.BufferSize = $tampon }
    $fenetre = $ui.WindowSize
    $hauteur = [Math]::Min(58, $ui.MaxPhysicalWindowSize.Height)
    $largeur = [Math]::Min([Math]::Max($fenetre.Width, 100), $ui.MaxPhysicalWindowSize.Width)
    if ($fenetre.Height -lt $hauteur -or $fenetre.Width -lt $largeur) {
        $fenetre.Height = [Math]::Max($fenetre.Height, $hauteur); $fenetre.Width = $largeur
        $ui.WindowSize = $fenetre
    }
} catch { }
# Fenêtre restée basse (Windows Terminal, petite console) : le menu se resserre et redit l'état près de l'invite.
try { if ([int]$Host.UI.RawUI.WindowSize.Height -gt 0 -and [int]$Host.UI.RawUI.WindowSize.Height -lt 50) { $script:Compact = $true } } catch { }

$message = $null
while ($true) {
    try { Clear-Host } catch { }
    # La lecture de l'état prend quelques secondes : l'écran ne reste pas vide pendant ce temps.
    Write-Host ''
    Write-Host '   Lecture de l''état du Worker...' -ForegroundColor DarkGray
    $etatLu = Get-EtatBandeau
    try { Clear-Host } catch { }
    Write-Menu -Etat $etatLu
    if ($message) { Write-Host ''; Write-MessageRetour -Texte $message; $message = $null }

    $redessiner = $false
    while (-not $redessiner) {
        Write-Host ''
        $saisie = $null
        try { $saisie = Read-Host '  Ton choix' } catch { exit 0 }   # pas de clavier (console fermée, entrée redirigée)
        if ($null -eq $saisie) { exit 0 }
        $saisie = $saisie.Trim().ToUpper()
        if ($saisie -eq '') { Write-Info ('Tape un numéro (1 à ' + $script:NumeroMax + '), ou A, R, Q, puis Entrée.'); continue }
        if ($saisie -eq 'Q') { Write-Host ''; Write-Info 'À bientôt.'; exit 0 }
        if ($saisie -eq 'R') { $redessiner = $true; continue }
        if ($saisie -eq 'A') { Show-Aide; continue }
        $numero = 0
        if (-not [int]::TryParse($saisie, [ref]$numero)) { Write-Alerte ('« ' + $saisie + ' » n''est pas un choix du menu. Tape un numéro (1 à ' + $script:NumeroMax + '), ou A, R, Q.'); continue }
        $entree = $script:Entrees | Where-Object { $_.Numero -eq $numero } | Select-Object -First 1
        if (-not $entree) { Write-Alerte ('Il n''y a pas d''entrée ' + $numero + '. Les numéros vont de 1 à ' + $script:NumeroMax + '.'); continue }
        if (-not (Test-Path -LiteralPath $entree.Chemin -PathType Leaf)) { [void](Invoke-Entree -Entree $entree); continue }

        $script:MessageRetour = $null
        $code = Invoke-Entree -Entree $entree
        if ($script:MessageRetour) { $message = $script:MessageRetour; $script:MessageRetour = $null }
        elseif ($code -eq 3) { $message = 'La fenêtre administrateur a été refusée : rien n''a été fait.' }
        elseif ($entree.Type -eq 'ps1' -and $code -ne 0 -and $code -ne 2) { $message = ('« ' + $entree.Nom + ' » s''est terminé avec le code ' + $code + ' (0 = succès, 1 = échec, 2 = annulé, 3 = élévation refusée).') }
        $redessiner = $true
    }
}
