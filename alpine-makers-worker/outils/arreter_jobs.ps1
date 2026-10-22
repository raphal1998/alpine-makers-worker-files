<#
    Arrêter tous les jobs de CE Worker (entrée 21 du menu, rubrique « Gestion du Worker »).

    Coupe TOUT ce qui calcule pour ce Worker, en trois temps (agent 1.50.33) :
      1. demande au SITE, par l'agent (action locale « jobs-stop », route signée), l'annulation de sa file pour ce
         Worker, de ses installations en cours et des demandes pas encore transmises ;
      2. l'agent arrête aussitôt, sur CE PC, les calculs des jobs et ceux lancés depuis worker.exe (convertisseur,
         préparation CNC FreeCAD, Labo image, tranchage…), puis coupe l'arbre de processus de ceux qui résistent
         (lanceur, FreeCADCmd et leurs enfants : seulement les processus qu'il a lancés, PID ET date de création) ;
      3. cet outil arrête enfin les processus de CALCUL de ce Worker qui vivraient encore (agent muet, calcul
         orphelin, tranchage OrcaSlicer), reconnus par ce qu'ils exécutent, jamais par leur nom seul.

    Cet outil ne commande AUCUN équipement : ni graveuse, ni imprimante, ni caméra. Une gravure en cours est nommée,
    jamais arrêtée d'ici : couper son flux laisserait le laser dans son dernier état. Les moteurs (ComfyUI,
    Hunyuan3D, PrintGuard, bridge Orca) restent allumés : 9 les arrête, 11 éteint tout le Worker.
    Les seules actions locales déposées sont « status », « jobs-stop » et « relay-status ».

    Pilotage sans clavier (essais ; worker.exe ne transmet que -SansPause) :
        arreter_jobs.ps1 -Simulation -SansPause            montre l'état et ce qui serait arrêté, n'envoie rien
        arreter_jobs.ps1 -Oui -SansPause [-SuiviSecondes 60]
    Codes de sortie : 0 tout est arrêté et le site a pris la demande ; 1 échec ou arrêt incomplet (agent arrêté ou
    trop ancien, site injoignable ou sans la fonction, refus du site, calcul qui résiste) ; 2 annulé.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause,
    [switch]$Oui,
    [switch]$Simulation,
    [int]$SuiviSecondes = 60
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'commun_cycle.ps1')
. (Join-Path $PSScriptRoot 'commun_gestion.ps1')
Initialize-ConsoleWorker -Titre 'Arrêter tous les jobs du Worker'
$NomOutil = 'arreter_jobs'
try { $Racine = Get-RacineWorker -Racine $Racine } catch { Write-Erreur ([string]$_.Exception.Message); Exit-Outil -Code 1 -SansPause:$SansPause }

$QuestionArret = 'Arrêter tous les calculs en cours et en attente de ce Worker, ainsi que ses installations en cours et les demandes pas encore transmises (mise à jour ou réparation demandée incluse) ?'
$SousTexteArret = 'Les calculs perdus ne reprennent pas ; un téléchargement interrompu devra être relancé. La file d''attente de tes autres Workers n''est pas touchée.'
$QuestionLocale = 'L''agent ne répond pas : arrêter quand même les processus de calcul de ce Worker encore présents sur ce PC ?'

function Get-ListeChamp {
    <#
        Champ « liste » d'une réponse de l'agent. Connu = $false si le champ est absent ou nul ; une liste vide
        est connue. (Une fonction qui rend une liste vide rend $null : la liste est donc rangée dans un objet.)
    #>
    param($Objet, [string]$Nom)
    $r = [pscustomobject]@{ Connu = $false; Elements = @() }
    if ($null -eq $Objet) { return $r }
    $propriete = $Objet.PSObject.Properties[$Nom]
    if ($null -eq $propriete -or $null -eq $propriete.Value) { return $r }
    $r.Connu = $true
    $r.Elements = @($propriete.Value)
    return $r
}

function Get-Compteur {
    param($Objet, [string]$Nom)
    return (ConvertTo-EntierGestion (Get-ChampGestion $Objet $Nom))
}

function Format-Noms {
    <# Noms venus de l'agent : sans caractère de contrôle, 80 caractères et 20 noms au plus. #>
    param($Elements)
    $noms = @()
    foreach ($element in @($Elements)) {
        $nom = (([string]$element) -replace '[\x00-\x1f]', ' ').Trim()
        if (-not $nom) { continue }
        if ($nom.Length -gt 80) { $nom = $nom.Substring(0, 80) }
        if ($noms.Count -lt 20) { $noms += $nom }
    }
    return ($noms -join ', ')
}

function Write-Compte {
    <# Ligne de bilan : marquée « fait » seulement quand le nombre n'est pas nul. #>
    param([int]$Nombre, [string]$Texte)
    if ($Nombre -gt 0) { Write-Ok $Texte } else { Write-Info ('  ' + $Texte) }
}

function Test-EntreeRedirigee {
    try { return [bool][Console]::IsInputRedirected } catch { return $false }
}

function Confirm-Question {
    <#
        Confirm-Action, avec la question écrite en clair quand l'entrée est un tube : sous worker.exe,
        PowerShell n'écrit pas l'invite de Read-Host, la question resterait invisible.
    #>
    param([string]$Question, [switch]$Oui)
    if (-not $Oui -and (Test-EntreeRedirigee)) { Write-LignesCoupees -Prefixe '   ' -Texte ($Question + ' [O/N] :') -Couleur White }
    return (Confirm-Action -Question $Question -Oui:$Oui)
}

function Get-NomsTaches {
    <# Calculs lancés depuis worker.exe, tels que l'agent les nomme (outil et libellé). #>
    param($Liste)
    $noms = @()
    foreach ($t in @($Liste)) { if ($t) { $noms += ([string](Get-ChampGestion $t 'label' (Get-ChampGestion $t 'tool' '?'))) } }
    return $noms
}

function Show-CalculsLocaux {
    <# Processus de calcul de ce Worker visibles d'ici (Get-ProcessusDuWorker, rôle « calcul »). Renvoie leur nombre. #>
    param([string]$Racine, [switch]$AvecInstallations)
    $roles = @('calcul')
    if ($AvecInstallations) { $roles += 'installation' }
    $vus = @((Get-PidsDuWorker -Racine $Racine).Processus | Where-Object { $roles -contains [string]$_.Role })
    if ($vus.Count -eq 0) { Write-Etat -Libelle 'Processus de calcul' -Valeur 'aucun visible sur ce PC' -Niveau ok }
    else {
        Write-Etat -Libelle 'Processus de calcul' -Valeur ([string]$vus.Count + ' sur ce PC') -Niveau alerte
        foreach ($p in @($vus | Select-Object -First 12)) { Write-Info ('      PID ' + $p.ProcessId + '  ' + $p.Nom + '  (' + (Get-LibelleRole $p.Role) + ')') }
    }
    return $vus.Count
}

function Show-EtatActuel {
    <# Ce qui tourne sur ce Worker d'après « status » : outil et état d'un calcul, jamais son contenu. #>
    param($Statut)
    Write-Section 'En ce moment sur ce Worker'
    $jobs = Get-ListeChamp $Statut 'jobs_active'
    if (-not $jobs.Connu) { Write-Etat -Libelle 'Calculs en cours' -Valeur 'impossible à lire' -Niveau alerte }
    elseif ($jobs.Elements.Count -eq 0) { Write-Etat -Libelle 'Calculs en cours' -Valeur 'aucun' -Niveau ok }
    else {
        Write-Etat -Libelle 'Calculs en cours' -Valeur ([string]$jobs.Elements.Count) -Niveau alerte
        foreach ($j in @($jobs.Elements | Select-Object -First 20)) {
            if ($null -eq $j) { continue }
            Write-Info ('      outil ' + (Format-Noms @(Get-ChampGestion $j 'tool_id' '?')) + '   état ' + (Format-Noms @(Get-ChampGestion $j 'state' '?')))
        }
    }
    $taches = Get-ListeChamp $Statut 'desktop_tasks'
    if ($taches.Connu -and $taches.Elements.Count -gt 0) {
        Write-Etat -Libelle 'Calculs de worker.exe' -Valeur ([string]$taches.Elements.Count + ' : ' + (Format-Noms (Get-NomsTaches $taches.Elements))) -Niveau alerte
    }
    elseif ($taches.Connu) { Write-Etat -Libelle 'Calculs de worker.exe' -Valeur 'aucun' -Niveau ok }
    $operations = Get-Compteur $Statut 'maintenance_count'
    if ([bool](Get-ChampGestion $Statut 'maintenance_active' $false) -or $operations -gt 0) {
        $texte = 'en cours'
        if ($operations -gt 0) { $texte = 'en cours (' + $operations + ' opération(s))' }
        Write-Etat -Libelle 'Installation ou maintenance' -Valeur $texte -Niveau alerte
    }
    else { Write-Etat -Libelle 'Installation ou maintenance' -Valeur 'aucune' -Niveau ok }
    $gravures = Get-ListeChamp $Statut 'grbl_jobs'
    if (-not $gravures.Connu) { Write-Etat -Libelle 'Gravure ou cadrage' -Valeur 'impossible à lire' -Niveau alerte }
    elseif ($gravures.Elements.Count -eq 0) { Write-Etat -Libelle 'Gravure ou cadrage' -Valeur 'aucune gravure en cours' -Niveau ok }
    else { Write-Etat -Libelle 'Gravure ou cadrage' -Valeur ('en cours sur : ' + (Format-Noms $gravures.Elements) + '   (jamais arrêté par cet outil)') -Niveau alerte }
    if ([string](Get-ChampGestion (Get-ChampGestion $Statut 'duplicates') 'audit_state' '') -eq 'running') {
        Write-Etat -Libelle 'Contrôle des doublons' -Valeur 'en cours (lancé depuis ce PC)' -Niveau alerte
    }
    else { Write-Etat -Libelle 'Contrôle des doublons' -Valeur 'aucun en cours' -Niveau ok }
}

function Show-AgentArrete {
    <# Agent muet : lecture seule du journal, des moteurs et des processus de calcul. Renvoie le nombre de calculs vus. #>
    param([string]$Racine)
    Write-Info 'Sans réponse de l''agent, le site n''est pas prévenu d''ici : seuls les processus de calcul de ce PC peuvent être arrêtés.'
    Write-Section 'Ce que ce PC montre encore'
    Show-JobsActifs -Jobs (Get-JobsActifs -Racine $Racine)
    $lancement = $null
    try { $lancement = Get-ModeLancement -Racine $Racine } catch { }
    # Les ports des moteurs sont ceux du PC : si un autre Worker tourne ici, ce sont les siens.
    if ($lancement -and (Test-ServiceAutreDossier -Mode $lancement)) { return 0 }
    $ouverts = @()
    foreach ($m in @(Get-EtatMoteurs -Racine $Racine)) { if ($m.EnEcoute) { $ouverts += [string]$m.Nom } }
    if ($ouverts.Count -eq 0) { Write-Etat -Libelle 'Moteurs encore ouverts' -Valeur 'aucun' -Niveau ok }
    else {
        Write-Etat -Libelle 'Moteurs encore ouverts' -Valeur ($ouverts -join ', ') -Niveau alerte
        Write-Conseil ('Si le Worker est bien éteint, ces moteurs sont restés seuls : ferme-les avec ' + (Get-NomEntree 11) + '.')
    }
    return (Show-CalculsLocaux -Racine $Racine -AvecInstallations)
}

function Wait-FinDesCalculs {
    <#
        Relit « status » jusqu'à ce que plus aucun calcul ne soit en cours (jobs, calculs de worker.exe) et que le
        suivi de l'agent (arrêt forcé après son délai) soit fini, ou jusqu'au délai. Rend Restants (-1 : agent muet),
        Taches et Balayage (champ stop_sweep, ou $null).
    #>
    param([string]$Racine, [int]$DelaiSecondes)
    $limite = (Get-Date).AddSeconds($DelaiSecondes)
    $r = [pscustomobject]@{ Restants = -1; Taches = 0; Balayage = $null }
    $points = $false
    while ($true) {
        $r.Restants = -1; $r.Taches = 0
        $balayageEnCours = $false
        $etat = Get-EtatAgent -Racine $Racine -DelaiSecondes 8
        if ($etat.Vivant) {
            $liste = Get-ListeChamp $etat.Statut 'jobs_active'
            if ($liste.Connu) { $r.Restants = [int]$liste.Elements.Count }
            $taches = Get-ListeChamp $etat.Statut 'desktop_tasks'
            if ($taches.Connu) { $r.Taches = [int]$taches.Elements.Count }
            $r.Balayage = Get-ChampGestion $etat.Statut 'stop_sweep'
            $balayageEnCours = ([string](Get-ChampGestion $r.Balayage 'state' '') -eq 'running')
        }
        if (($r.Restants -eq 0 -and $r.Taches -eq 0 -and -not $balayageEnCours) -or (Get-Date) -ge $limite) { break }
        # Sous worker.exe la sortie est un tube : des points, jamais de retour au début de la ligne.
        Write-Host '.' -NoNewline -ForegroundColor DarkGray
        $points = $true
        Start-Sleep -Seconds 3
    }
    if ($points) { Write-Host '' }
    return $r
}

function Show-Balayage {
    <# Ce que l'agent a coupé de force après son délai, et ce qui lui résiste. Renvoie le nombre qui résiste. #>
    param($Balayage)
    if ($null -eq $Balayage) { return 0 }
    foreach ($f in @(Get-ChampGestion $Balayage 'forced' @())) {
        if (-not $f) { continue }
        $nom = Format-Noms @(Get-ChampGestion $f 'name' 'calcul')
        $texte = 'Arrêt forcé : ' + $nom + ' (' + (Get-Compteur $f 'killed') + ' processus)'
        if (-not [bool](Get-ChampGestion $f 'confirmed' $false)) { $texte += ' ; le moteur qui calculait pour lui reste allumé' }
        Write-Ok $texte
    }
    $resiste = 0
    foreach ($f in @(Get-ChampGestion $Balayage 'resisting' @())) {
        if (-not $f) { continue }
        $resiste++
        Write-Alerte ('Résiste à l''arrêt forcé : ' + (Format-Noms @(Get-ChampGestion $f 'name' 'calcul')))
    }
    return $resiste
}

function Show-NonArrete {
    <#
        Bilan fixe de ce que cet outil n'arrête JAMAIS, nommément. -Resultat : compteurs rendus par le site ;
        -Repli : 'unreachable' ou 'missing' quand le site n'a pas pu être joint (arrêt local seulement).
    #>
    param($Gravures, $Resultat = $null, [string]$Repli = '')
    $consigne = '   (l''arrêt d''une graveuse se fait sur la machine ou dans le Laser Studio)'
    Write-Section 'Non arrêté par cet outil'
    if (-not $Gravures.Connu) { Write-Alerte ('Gravure ou cadrage en cours : impossible à vérifier d''ici' + $consigne) }
    elseif ($Gravures.Elements.Count -gt 0) { Write-Alerte ('Gravure ou cadrage en cours sur : ' + (Format-Noms $Gravures.Elements) + $consigne) }
    else { Write-Conseil ('Gravure ou cadrage : aucune gravure en cours' + $consigne) }
    Write-Conseil 'Impression 3D en cours : jamais arrêtée d''ici'
    Write-Conseil ('Moteurs (ComfyUI, Hunyuan3D, PrintGuard, bridge Orca) : ils restent allumés   (pour les arrêter : ' + (Get-NomEntree 9) + ' ; pour tout éteindre : ' + (Get-NomEntree 11) + ')')
    Write-Conseil 'Caméras et surveillance IA : inchangées'
    if ($null -ne $Resultat) { Write-Conseil ('Opérations courtes déjà commencées (désinstallation, démarrage d''un moteur) : ' + (Get-Compteur $Resultat 'commands_finishing') + ' se terminent seules') }
    else { Write-Conseil 'Opérations courtes déjà commencées (désinstallation, démarrage d''un moteur) : elles se terminent seules' }
    if ($Repli) {
        $raison = 'site injoignable'
        if ($Repli -eq 'missing') { $raison = 'site sans la fonction' }
        Write-Alerte ('File d''attente du site : non touchée (' + $raison + ')')
        Write-Alerte 'Installations en cours : non arrêtées'
    }
    Write-Conseil 'Jobs en attente pour tes autres Workers : non touchés'
    Write-Conseil 'Calculs de ton compte pas encore attribués à un Worker : non touchés (ils restent dans la file du site)'
}

function Invoke-PasseForcee {
    <#
        Dernier temps : les processus de calcul de ce Worker qui vivent encore sont arrêtés (Stop-CalculsDuWorker).
        Renvoie le nombre de ceux qui résistent (0 = plus rien ne calcule d'après ce PC).
    #>
    param([string]$Racine, [switch]$AvecInstallations)
    $r = Stop-CalculsDuWorker -Racine $Racine -Outil $NomOutil -AvecInstallations:$AvecInstallations
    if (@($r.Vus).Count -eq 0) { Write-Ok 'Aucun processus de calcul de ce Worker ne tourne encore sur ce PC.' }
    else {
        Write-Compte $r.Arretes ([string]$r.Arretes + ' processus de calcul arrêté(s) de force : ' + (Format-Noms (@($r.Vus) | ForEach-Object { [string]$_.Nom } | Select-Object -Unique)))
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('passe forcée : ' + $r.Arretes + ' processus de calcul arrêté(s), ' + @($r.Survivants).Count + ' résistent')
    }
    if (@($r.Survivants).Count -gt 0) {
        Write-Section 'Résiste encore'
        Show-Survivants -Survivants $r.Survivants -Illisibles $r.Illisibles -Administrateur:(Test-Administrateur)
    }
    elseif ($r.Illisibles -gt 0 -and -not (Test-Administrateur)) {
        Write-Info ('      ' + $r.Illisibles + ' processus python ou powershell sont illisibles sans droits administrateur (normal si le Worker est un service) : l''agent, lui, a pu les arrêter.')
    }
    return @($r.Survivants).Count
}

try {
    Write-Explication -Titre 'ARRÊTER TOUS LES JOBS' -Fait @(
        'Demande au site l''arrêt de tous les calculs en cours et en attente de CE Worker (images, 3D, conversions), de ses installations en cours et des demandes pas encore transmises.',
        'Arrête aussitôt sur CE PC les calculs des jobs et ceux lancés depuis worker.exe : convertisseur, préparation pour une CNC (FreeCAD), Labo image, tranchage.',
        'Un calcul qui ne s''arrête pas seul est coupé de force après 20 secondes, avec ses processus (FreeCAD, OrcaSlicer...) : seulement ceux de ce Worker.',
        'Si l''agent ne répond pas, il arrête quand même les processus de calcul de ce Worker encore présents sur ce PC.'
    ) -NeFaitPas @(
        'Il n''arrête JAMAIS une graveuse, une imprimante 3D ni un autre équipement : cela se fait sur la machine elle-même.',
        ('Il n''éteint ni les moteurs (ComfyUI, Hunyuan3D, PrintGuard, bridge Orca), ni les caméras, ni la surveillance IA : ' + (Get-NomEntree 9) + ' ou ' + (Get-NomEntree 11) + '.'),
        'Il ne touche pas aux jobs en attente pour tes autres Workers, ni à un programme qui n''est pas de ce Worker.'
    ) -Exige @(
        'Le Worker allumé et associé à ton compte pour prévenir le site. Aucune fenêtre administrateur.'
    )

    $config = Get-ConfigSure -Racine $Racine
    $agent = Get-EtatAgent -Racine $Racine
    $gravures = Get-ListeChamp $agent.Statut 'grbl_jobs'

    if (-not $agent.Vivant -or -not $agent.Capable) {
        Write-Section 'Arrêt par le site impossible d''ici'
        if (-not $config.Associe) {
            Write-Alerte 'Ce Worker n''est pas associé à un compte : il ne reçoit aucun job du site.'
            Write-Conseil ('Pour l''associer : ' + (Get-NomEntree 8) + '.')
        }
        else { Write-RefusRelais -Resultat $agent }
        if ($agent.Vivant) {
            # L'agent répond mais ne connaît pas encore cette action (trop ancien, ou à redémarrer).
            Write-Conseil 'En attendant, utilise le bouton « Arrêter les jobs » du site (Mes Workers, carte de ce Worker) : lui annule aussi les calculs de ton compte pas encore attribués à un Worker.'
            Show-EtatActuel -Statut $agent.Statut
            [void](Show-CalculsLocaux -Racine $Racine)
        }
        $calculs = 0
        if (-not $agent.Vivant -and $config.Associe) { $calculs = Show-AgentArrete -Racine $Racine }
        elseif (-not $agent.Vivant) { $calculs = Show-CalculsLocaux -Racine $Racine -AvecInstallations }
        $resistent = 0
        if ($calculs -gt 0 -and -not $agent.Vivant) {
            # Agent muet : ses calculs orphelins (lanceurs, FreeCAD, installateurs) sont arrêtés d'ici, sur confirmation.
            if ($Simulation) {
                Write-Info '[simulation] Ces processus de calcul seraient arrêtés :'
                [void](Stop-CalculsDuWorker -Racine $Racine -Outil $NomOutil -AvecInstallations -Simulation)
            }
            elseif (Confirm-Question -Question $QuestionLocale -Oui:$Oui) {
                Write-Section 'Arrêt des calculs de ce PC'
                $resistent = Invoke-PasseForcee -Racine $Racine -AvecInstallations
            }
            else { Write-Info 'Rien n''a été arrêté.' }
        }
        else { Write-Info 'Rien n''a été arrêté.' }
        Show-NonArrete -Gravures $gravures
        if (-not $agent.Vivant -and $config.Associe) {
            Write-Conseil ('Agent arrêté ou figé : ' + (Get-NomEntree 24) + ' le diagnostique et le relance ; ' + (Get-NomEntree 12) + ' rallume un Worker éteint.')
        }
        if (-not $Simulation) { Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('arrêt par le site impossible : agent arrêté, trop ancien ou Worker non associé ; calculs locaux vus = ' + $calculs + ', résistent = ' + $resistent) }
        Exit-Outil -Code 1 -SansPause:$SansPause
    }

    Show-EtatActuel -Statut $agent.Statut
    [void](Show-CalculsLocaux -Racine $Racine)
    Write-Host ''
    Write-Info $SousTexteArret

    if ($Simulation) {
        Write-Info '[simulation] La demande d''arrêt n''a pas été envoyée : rien n''a changé.'
        [void](Stop-CalculsDuWorker -Racine $Racine -Outil $NomOutil -Simulation)
        Show-NonArrete -Gravures $gravures
        Exit-Outil -Code 0 -SansPause:$SansPause
    }
    if (-not (Confirm-Question -Question $QuestionArret -Oui:$Oui)) {
        Write-Info 'Annulé : rien n''a été arrêté.'
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'annulé par l''utilisateur'
        Exit-Outil -Code 2 -SansPause:$SansPause
    }

    Write-Section 'Arrêt'
    Write-Info 'Demande d''arrêt transmise au site par l''agent ; l''agent arrête aussi tout de suite les calculs de ce PC...'
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'arrêt de tous les jobs demandé'
    $relais = Invoke-RelaisSite -Racine $Racine -Action 'jobs-stop' -DelaiSecondes 60
    $local = $relais.Local
    $repli = ''
    if (-not $relais.Ok -and [bool](Get-ChampGestion $local 'fallback' $false)) {
        $repli = 'unreachable'
        if ([string](Get-ChampGestion $local 'fallback_reason' '') -eq 'missing') { $repli = 'missing' }
    }
    $gravuresApres = Get-ListeChamp $local 'grbl_jobs'
    if ($gravuresApres.Connu) { $gravures = $gravuresApres }
    $controleArrete = [bool](Get-ChampGestion $local 'audit_cancelled' $false)
    $siteRefuse = (-not $relais.Ok -and -not $repli)

    Write-Section 'Arrêté'
    $resultat = $null
    if ($relais.Ok) {
        $resultat = $relais.Resultat
        $demandes = Get-Compteur $resultat 'requested'
        $annulees = Get-Compteur $resultat 'commands_cancelled'
        $installations = Get-Compteur $resultat 'commands_stop_requested'
        Write-Compte $demandes ([string]$demandes + ' calcul(s) : arrêt demandé (' + (Get-Compteur $resultat 'queued_cancelled') + ' en attente annulé(s), ' + (Get-Compteur $resultat 'pending_confirmation') + ' en cours attendent la confirmation de leur moteur)')
        Write-Compte $annulees ([string]$annulees + ' demande(s) en attente annulée(s) avant transmission')
        Write-Compte $installations ('arrêt demandé pour ' + $installations + ' installation(s) ou contrôle(s) en cours')
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('arrêt demandé au site : ' + $demandes + ' calcul(s), ' + $annulees + ' demande(s) annulée(s), ' + $installations + ' installation(s) ou contrôle(s)')
    }
    else {
        # Refus du site, site injoignable ou sans la fonction : l'agent a quand même arrêté les calculs de ce PC.
        Write-RefusRelais -Resultat $relais
        if ($siteRefuse) { Write-Info 'Le site n''a rien annulé de sa file ; les calculs de ce PC, eux, reçoivent l''arrêt.' }
        if ($repli -eq 'unreachable') { Write-Conseil 'Relance cet outil quand le site répond de nouveau : sa file d''attente et les installations n''ont pas été touchées.' }
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('site non joint ou refus (' + $repli + ' ' + [int]$relais.Http + ' ' + [string]$relais.CodeSite + ')')
    }
    if ($null -ne $local) {
        $locaux = Get-Compteur $local 'jobs_cancel_requested'
        Write-Compte $locaux ('Arrêt demandé directement à ' + $locaux + ' calcul(s) en cours sur ce PC, sans attendre le site.')
        $tachesCoupees = @(Get-ChampGestion $local 'desktop_tasks_cancelled' @())
        $nomsTaches = Get-NomsTaches $tachesCoupees
        Write-Compte $nomsTaches.Count ([string]$nomsTaches.Count + ' calcul(s) lancé(s) depuis worker.exe : arrêt demandé' + $(if ($nomsTaches.Count) { ' (' + (Format-Noms $nomsTaches) + ')' } else { '' }))
    }
    if ($controleArrete) { Write-Ok 'Contrôle des doublons lancé depuis ce PC : arrêt demandé.' }

    $resistent = 0
    $restantsSuivi = 0
    if ($SuiviSecondes -gt 0) {
        Write-Section ('Suivi (' + $SuiviSecondes + ' secondes au plus ; un calcul qui résiste est coupé de force après 20 secondes)')
        $suivi = Wait-FinDesCalculs -Racine $Racine -DelaiSecondes $SuiviSecondes
        $resistent += (Show-Balayage -Balayage $suivi.Balayage)
        if ($suivi.Restants -eq 0 -and $suivi.Taches -eq 0) { Write-Ok 'Plus aucun calcul en cours sur ce Worker.' }
        elseif ($suivi.Restants -ge 0) {
            $restantsSuivi = $suivi.Restants + $suivi.Taches
            if ($suivi.Restants -gt 0) { Write-Alerte ([string]$suivi.Restants + ' job(s) attendent encore la confirmation d''arrêt de leur moteur') }
            if ($suivi.Taches -gt 0) { Write-Alerte ([string]$suivi.Taches + ' calcul(s) de worker.exe pas encore arrêté(s)') }
            Write-Conseil ('Pour les suivre : ' + (Get-NomEntree 2) + '. Un moteur qui ne confirme pas son arrêt se relance avec ' + (Get-NomEntree 9) + ' (Arrêter puis Relancer).')
        }
        else { Write-Alerte ('L''agent ne répond plus : la fin des calculs n''a pas pu être suivie. Vérifie avec ' + (Get-NomEntree 1) + ', puis ' + (Get-NomEntree 24) + '.') }
    }

    Write-Section 'Dernière vérification sur ce PC'
    $resistent += (Invoke-PasseForcee -Racine $Racine)

    Show-NonArrete -Gravures $gravures -Resultat $resultat -Repli $repli
    if ($resistent -gt 0) { Write-Conseil ('Ce qui résiste encore : ' + (Get-NomEntree 11) + ' éteint tout le Worker (fenêtre administrateur), puis ' + (Get-NomEntree 12) + ' le rallume.') }
    if ($relais.Ok -and $resistent -eq 0) { Exit-Outil -Code 0 -SansPause:$SansPause }
    Exit-Outil -Code 1 -SansPause:$SansPause
} catch {
    Write-Erreur ('Erreur inattendue : ' + $_.Exception.Message)
    if (-not $Simulation) { try { Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('erreur inattendue : ' + $_.Exception.Message) } catch { } }
    Exit-Outil -Code 1 -SansPause:$SansPause
}
