<#
    Fonctions partagées par les outils du CYCLE DE VIE du Worker :
    moteurs.ps1, eteindre_worker.ps1, rallumer_worker.ps1, redemarrer_agent.ps1,
    reassocier_worker.ps1. Tout bloc commun à ces outils vit ICI, en un seul exemplaire.

    À charger APRÈS commun.ps1 :
        . (Join-Path $PSScriptRoot 'commun.ps1')
        . (Join-Path $PSScriptRoot 'commun_cycle.ps1')

    Règles tenues ici :
      - aucun arrêt par nom d'exécutable : uniquement des PID reconnus par
        Get-ProcessusDuWorker, revérifiés (date de création) avant taskkill ;
      - jamais le PID 0, le PID 4 (System / HTTP.sys) ni le processus courant ;
      - aucune lecture de secret, aucun code d'association dans un journal ;
      - aucun code de sortie 64 ni 75 (réservés au superviseur du Worker).
#>

$script:PortRelaisCamera = 1984

function Exit-Outil {
    <# Sortie unique : pause éventuelle, puis code. 64 et 75 sont interdits : ils deviennent 1. #>
    param([int]$Code = 0, [switch]$SansPause)
    if ($Code -eq 64 -or $Code -eq 75) { $Code = 1 }
    Wait-FinOutil -SansPause:$SansPause
    exit $Code
}

function Invoke-ActionSure {
    <#
        Relais d'Invoke-ActionLocale (commun.ps1), qui reconnaît lui-même l'agent trop ancien
        quel que soit $ErrorActionPreference. Seul ajout : les points de progression pendant
        les demandes longues (arrêt et relance des moteurs), pour que l'écran ne reste jamais figé.
    #>
    param([string]$Racine, [string]$Action, [int]$DelaiSecondes = 15)
    $longue = ($Action -eq 'engines-stop' -or $Action -eq 'engines-start')
    return (Invoke-ActionLocale -Racine $Racine -Action $Action -DelaiSecondes $DelaiSecondes -Progression:$longue)
}

function Test-ServiceAutreDossier {
    <#
        Les ports des moteurs et du relais caméra sont GLOBAUX au PC. Si un service
        AlpineWorker EN MARCHE sert un AUTRE dossier, ces ports sont les siens : agir
        ici d'après eux serait faux. Affiche le refus et renvoie $true.
    #>
    param($Mode)
    if (-not $Mode.Service -or $Mode.Service.ViseCeDossier -or ([string]$Mode.Service.Etat -ne 'Running')) { return $false }
    $autre = [string]$Mode.Service.Dossier
    if (-not $autre) { $autre = '(dossier illisible dans le registre)' }
    Write-Host ''
    Write-Erreur 'Un autre Worker tourne déjà sur ce PC, en service Windows, depuis un AUTRE dossier :'
    Write-Info ('      ' + $autre)
    Write-Info 'Les moteurs et les ports ouverts (8188, 8189, 8000, 1984, 15064) sont les siens, pas ceux de ce dossier-ci.'
    Write-Conseil 'Utilise les outils de ce dossier-là (son MENU-WORKER.bat). Rien n''a été touché ici.'
    return $true
}

function Get-PresenceWorker {
    <#
        CE dossier tourne-t-il ? (modes tâche et manuel.) Preuves propres au dossier
        d'abord : superviseur ou agent reconnu par sa ligne de commande (lisible dans
        la même session), ou réponse à « status » (boîte aux lettres DE CE dossier).
        Les ports, globaux, ne servent d'indice que si des processus sont illisibles.
        Etat : oui | illisible | non. Lisible : le superviseur ou l'agent est visible d'ici.
    #>
    param([string]$Racine, $Status = $null)
    $vus = Get-PidsDuWorker -Racine $Racine
    $coeur = @($vus.Processus | Where-Object { $_.Role -eq 'superviseur' -or $_.Role -eq 'agent' })
    $etat = 'non'
    if ($coeur.Count -gt 0) { $etat = 'oui' }
    elseif ($Status -and $Status.Ok) { $etat = 'oui' }
    elseif ($vus.Illisibles -gt 0) {
        $ports = ((Test-AgentVivant) -or (@(Get-EtatMoteurs -Racine $Racine | Where-Object { $_.EnEcoute }).Count -gt 0))
        if ($ports) { $etat = 'illisible' }
    }
    return [pscustomobject]@{ Etat = $etat; Lisible = ($coeur.Count -gt 0); Processus = @($vus.Processus); Coeur = $coeur; Illisibles = [int]$vus.Illisibles }
}

function Get-MoteursOuverts {
    <# Identifiants des moteurs dont le port écoute MAINTENANT. À relever AVANT une action, pour Wait-Agent. #>
    param([string]$Racine)
    return @(Get-EtatMoteurs -Racine $Racine | Where-Object { $_.EnEcoute } | ForEach-Object { [string]$_.Outil })
}

function Get-CleTache {
    <# Clé « chemin + nom » d'une tâche planifiée (ex. « \Alpine Makers Worker abc »), notée dans extinction.json. #>
    param($Tache)
    $chemin = [string]$Tache.Chemin
    if (-not $chemin) { $chemin = '\' }
    if (-not $chemin.EndsWith('\')) { $chemin += '\' }
    return ($chemin + [string]$Tache.Nom)
}

function Test-TacheDansMemo {
    <# Le mémo d'extinction cite-t-il cette tâche ? Accepte le format actuel (chemin + nom) et l'ancien (nom seul). #>
    param($Tache, [string[]]$Memo = @())
    return (($Memo -contains (Get-CleTache $Tache)) -or ($Memo -contains [string]$Tache.Nom))
}

function Write-ConseilMiseAJour {
    Write-Conseil 'L''agent installé est trop ancien pour obéir aux outils de ce menu (arrêt et relance propres des moteurs).'
    Write-Conseil 'Dans le site : Compte > Mes PC / Workers > ce Worker > « Mettre à jour » (le Worker doit être allumé). Relance ensuite cet outil.'
}

function Get-MemoMoteurs {
    <#
        Ce que la réponse « status » dit de la relance des moteurs :
          Present  : un mémo d'arrêt existe (écrit par « engines-stop ») ;
          Moteurs  : moteurs que ce mémo fera relancer (tournaient, ou relance auto allumée) ;
          Vide     : mémo présent mais sans aucun moteur = « engines-start » ne relancerait RIEN ;
          EnCours  : un démarrage demandé par un outil est déjà en cours chez l'agent ;
          Dernier  : compte rendu du dernier démarrage (requested, started, errors, source) ou $null ;
          Intentions : moteurs que l'agent relance SEUL (relance auto allumée).
        Sans mémo, « engines-start » se rabat sur l'inventaire : TOUS les moteurs installés et prêts.
    #>
    param($Status)
    $r = [pscustomobject]@{ Present = $false; Moteurs = @(); Vide = $false; EnCours = $false; Dernier = $null; Intentions = @() }
    if (-not $Status -or -not $Status.Ok -or -not $Status.Resultat) { return $r }
    $champs = @($Status.Resultat.PSObject.Properties.Name)
    if (($champs -contains 'memo') -and $Status.Resultat.memo) {
        $r.Present = $true
        $liste = @()
        foreach ($cle in @('were_running', 'intent_enabled')) {
            foreach ($m in @($Status.Resultat.memo.$cle)) { if ($m -and ($liste -notcontains [string]$m)) { $liste += [string]$m } }
        }
        $r.Moteurs = @($liste)
        $r.Vide = ($liste.Count -eq 0)
    }
    if (($champs -contains 'engines_start') -and $Status.Resultat.engines_start) {
        $r.EnCours = [bool]$Status.Resultat.engines_start.in_progress
        $r.Dernier = $Status.Resultat.engines_start.last
    }
    if ($champs -contains 'engines') { $r.Intentions = @($Status.Resultat.engines | Where-Object { $_ -and $_.intent } | ForEach-Object { [string]$_.tool_id }) }
    return $r
}

function Get-NomSourceRelance {
    <# « source » renvoyée par engines-start, en français. #>
    param([string]$Source)
    switch ($Source) {
        'memo'      { return 'mémo d''arrêt : les moteurs qui tournaient avant l''arrêt' }
        'inventory' { return 'inventaire : tous les moteurs installés et prêts' }
    }
    return $Source
}

function Show-ErreursRelance {
    <# Après une relance incomplète : ce que l'agent a noté pour les moteurs manquants (status.engines_start.last.errors). #>
    param([string]$Racine, [string[]]$Manquants = @())
    $s = Invoke-ActionSure -Racine $Racine -Action 'status' -DelaiSecondes 10
    $memo = Get-MemoMoteurs -Status $s
    if ($memo.EnCours) { Write-Info 'L''agent est encore en train de démarrer des moteurs : le chargement d''un gros modèle dépasse parfois 3 minutes.' }
    if (-not $memo.Dernier) { return }
    foreach ($e in @($memo.Dernier.errors)) {
        if (-not $e) { continue }
        $outil = [string]$e.tool_id
        if ($outil -and $Manquants.Count -gt 0 -and ($Manquants -notcontains $outil)) { continue }
        if (-not $outil) { $outil = 'agent' }
        Write-Alerte ('Erreur notée par l''agent pour ' + $outil + ' : ' + [string]$e.error)
    }
}

function Get-JobsActifs {
    <#
        Travaux en cours de l'identité COURANTE. Source : la réponse status si on l'a,
        sinon outils\jobs_bloquants.py --json (lecture seule du journal durable).
        Renvoie : Connu (bool), Nombre, Lignes (texte), Anciens (travaux d'une identité précédente), Source.
    #>
    param([string]$Racine, $Status = $null)
    $r = [pscustomobject]@{ Connu = $false; Nombre = 0; Lignes = @(); Anciens = 0; Source = 'aucune' }
    $travaux = $null
    if ($Status -and $Status.Ok -and $Status.Resultat -and ($Status.Resultat.PSObject.Properties.Name -contains 'jobs_active')) {
        $r.Connu = $true; $r.Source = 'agent'
        $travaux = @($Status.Resultat.jobs_active)
        if ($Status.Resultat.PSObject.Properties.Name -contains 'jobs_previous_identity') { try { $r.Anciens = [int]$Status.Resultat.jobs_previous_identity } catch { } }
    } else {
        $script = Join-Path $script:DossierOutils 'jobs_bloquants.py'
        $python = Get-PythonWorker -Racine $Racine
        if ($python -and (Test-Path -LiteralPath $script -PathType Leaf)) {
            $ancienne = $ErrorActionPreference
            try {
                $ErrorActionPreference = 'Continue'
                $sortie = & $python $script --root $Racine --json 2>$null
                $json = ($sortie | ForEach-Object { [string]$_ } | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1)
                if ($json) {
                    $rapport = $json | ConvertFrom-Json
                    if ($rapport.ok) {
                        $r.Connu = $true; $r.Source = 'journal local'
                        $travaux = @($rapport.jobs | Where-Object { $_ -and $_.identity -ne 'precedente' })
                        try { $r.Anciens = [int]$rapport.active_previous } catch { }
                    }
                }
            } catch { } finally { $ErrorActionPreference = $ancienne }
        }
    }
    foreach ($j in @($travaux)) {
        if ($null -eq $j) { continue }
        $r.Nombre++
        $id = [string]$j.job_id
        if ($id.Length -gt 13) { $id = $id.Substring(0, 13) + '...' }
        $r.Lignes += ('{0}  outil {1}  état {2}' -f $id, [string]$j.tool_id, [string]$j.state)
    }
    return $r
}

function Show-JobsActifs {
    param($Jobs)
    if (-not $Jobs.Connu) {
        Write-Etat -Libelle 'Travaux en cours' -Valeur 'impossible à vérifier ici' -Niveau alerte
        Write-Conseil 'Regarde dans le site (Mes Workers) qu''aucune génération ni conversion n''est en cours.'
        return
    }
    if ($Jobs.Nombre -eq 0) { Write-Etat -Libelle 'Travaux en cours' -Valeur ('aucun   (source : ' + $Jobs.Source + ')') -Niveau ok }
    else {
        Write-Etat -Libelle 'Travaux en cours' -Valeur ([string]$Jobs.Nombre + '   (source : ' + $Jobs.Source + ')') -Niveau erreur
        foreach ($l in $Jobs.Lignes) { Write-Info ('      ' + $l) }
    }
    if ($Jobs.Anciens -gt 0) { Write-Info ('      ' + $Jobs.Anciens + ' ancien(s) travail(aux) d''une identité précédente : ils ne bloquent rien.') }
}

function Show-Moteurs {
    <# Tableau des moteurs : port local + (si status disponible) relance automatique vue par l'agent. N'émet rien dans le pipeline. #>
    param([string]$Racine, $Status = $null)
    $parOutil = @{}
    if ($Status -and $Status.Ok -and $Status.Resultat -and $Status.Resultat.engines) {
        foreach ($e in @($Status.Resultat.engines)) { $parOutil[[string]$e.tool_id] = $e }
    }
    foreach ($m in @(Get-EtatMoteurs -Racine $Racine)) {
        $valeur = ''
        $niveau = 'info'
        if ($m.EnEcoute) { $valeur = 'en marche (port ' + $m.Port + ')'; $niveau = 'ok' }
        elseif ($m.FichePresente) { $valeur = 'port ' + $m.Port + ' fermé : arrêté, ou en cours de démarrage'; $niveau = 'alerte' }
        else { $valeur = 'arrêté (port ' + $m.Port + ' fermé)' }
        if ($parOutil.ContainsKey($m.Outil)) {
            if ($parOutil[$m.Outil].intent) { $valeur += '   relance auto : oui' } else { $valeur += '   relance auto : non' }
        }
        Write-Etat -Libelle $m.Nom -Valeur $valeur -Niveau $niveau
    }
}

function Get-PidsDuWorker {
    <#
        Get-ProcessusDuWorker, avec la date de création (déjà lue par le socle) mise en
        texte : elle sert à revérifier qu'un PID désigne toujours le MÊME processus avant
        de l'arrêter. Sans date, le processus ne sera jamais arrêté (Test-MemeProcessus).
    #>
    param([string]$Racine)
    $vu = Get-ProcessusDuWorker -Racine $Racine
    $liste = @()
    foreach ($p in @($vu.Processus)) {
        $creation = ''
        if ($p.Creation) { $creation = ([datetime]$p.Creation).ToString('o') }
        $liste += [pscustomobject]@{ ProcessId = [int]$p.ProcessId; ParentProcessId = [int]$p.ParentProcessId; Nom = [string]$p.Nom; Role = [string]$p.Role; Creation = $creation }
    }
    return [pscustomobject]@{ Processus = $liste; Illisibles = [int]$vu.Illisibles }
}

function Test-MemeProcessus {
    param($Entree)
    if (-not $Entree.Creation) { return $false }
    $cim = Get-CimInstance Win32_Process -Filter ('ProcessId=' + [int]$Entree.ProcessId) -ErrorAction SilentlyContinue
    if (-not $cim -or -not $cim.CreationDate) { return $false }
    return ($cim.CreationDate.ToString('o') -eq $Entree.Creation)
}

function Stop-PidsReconnus {
    <#
        Arrête des processus RECONNUS (liste issue de Get-PidsDuWorker), UN PAR UN et sans
        « taskkill /T » : /T suivrait les ParentProcessId sans contrôle de date et pourrait
        emporter un programme étranger qui a hérité d'un PID réutilisé. Chaque PID est
        revérifié (même date de création) juste avant son arrêt.
        Ordre : superviseur (sinon il relance l'agent), agent (sinon il relance ses moteurs),
        puis les moteurs, les plus profonds de l'arbre d'abord. Les enfants nés entre-temps
        sont pris par le second balayage de l'appelant.
        Renvoie le nombre de processus encore vivants après coup.
    #>
    param([object[]]$Liste = @(), [string]$Racine, [string]$Outil, [switch]$Simulation)
    $parPid = @{}
    foreach ($p in @($Liste)) { if ($p) { $parPid[[int]$p.ProcessId] = $p } }
    $profondeur = @{}
    foreach ($p in @($Liste)) {
        if (-not $p) { continue }
        $n = 0; $courant = [int]$p.ParentProcessId; $vus = @{}
        while ($parPid.ContainsKey($courant) -and -not $vus.ContainsKey($courant)) { $vus[$courant] = $true; $n++; $courant = [int]$parPid[$courant].ParentProcessId }
        $profondeur[[int]$p.ProcessId] = $n
    }
    $ordre = @()
    $ordre += @($Liste | Where-Object { $_ -and $_.Role -eq 'superviseur' })
    $ordre += @($Liste | Where-Object { $_ -and $_.Role -eq 'agent' })
    $ordre += @($Liste | Where-Object { $_ -and $_.Role -ne 'superviseur' -and $_.Role -ne 'agent' } | Sort-Object @{ Expression = { $profondeur[[int]$_.ProcessId] }; Descending = $true }, ProcessId)
    foreach ($p in $ordre) {
        if ($p.ProcessId -le 4 -or $p.ProcessId -eq $PID) { continue }
        if ($Simulation) { Write-Info ('[simulation] taskkill /PID ' + $p.ProcessId + ' /F   (' + $p.Role + ', ' + $p.Nom + ')'); continue }
        if (-not (Test-MemeProcessus -Entree $p)) { continue }
        $ancienne = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & taskkill.exe /PID $p.ProcessId /F 2>&1 | Out-Null
        } catch { } finally { $ErrorActionPreference = $ancienne }
        Write-JournalOutil -Racine $Racine -Outil $Outil -Message ('arrêt du processus reconnu PID ' + $p.ProcessId + ' (' + $p.Role + ', ' + $p.Nom + ')')
    }
    if ($Simulation) { return 0 }
    Start-Sleep -Seconds 2
    return @($Liste | Where-Object { $_ -and (Test-MemeProcessus -Entree $_) }).Count
}

function Start-SuperviseurCache {
    <# Lance run_worker.ps1 caché, SANS élévation, exactement comme worker_tools.ps1. #>
    param([string]$Racine)
    $superviseur = Join-Path $Racine 'run_worker.ps1'
    if (-not (Test-Path -LiteralPath $superviseur -PathType Leaf)) { throw "run_worker.ps1 est absent de $Racine" }
    $arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $superviseur + '"'
    Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -WorkingDirectory $Racine -WindowStyle Hidden | Out-Null
}

function Remove-MarqueurDeconnexion {
    <# .worker-disconnected = « déconnecté depuis le site » : tant qu'il existe, l'agent reste hors ligne. #>
    param([string]$Racine, [string]$Outil, [switch]$Simulation)
    $marqueur = Join-Path $Racine '.worker-disconnected'
    if (-not (Test-Path -LiteralPath $marqueur -PathType Leaf)) { return }
    if ($Simulation) { Write-Info '[simulation] suppression du marqueur .worker-disconnected'; return }
    Remove-Item -LiteralPath $marqueur -Force -ErrorAction SilentlyContinue
    Write-JournalOutil -Racine $Racine -Outil $Outil -Message 'marqueur .worker-disconnected supprimé'
    Write-Ok 'Marqueur de déconnexion supprimé : le Worker a le droit de se reconnecter.'
}

function Wait-Service {
    param([string]$Nom, [string]$Etat, [int]$DelaiSecondes = 30)
    $fin = (Get-Date).AddSeconds($DelaiSecondes)
    while ((Get-Date) -lt $fin) {
        $s = Get-Service -Name $Nom -ErrorAction SilentlyContinue
        if ($s -and ([string]$s.Status -eq $Etat)) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Wait-Agent {
    <#
        Attend que l'agent réponde. Renvoie 'confirme' (il a répondu à status),
        'probable' (relais caméra ou moteur rouvert, mais agent trop ancien pour
        répondre) ou 'absent'.
        -MoteursDejaOuverts : moteurs dont le port écoutait AVANT l'action (Get-MoteursOuverts).
        Des moteurs orphelins peuvent écouter alors que l'agent est mort : seul un moteur
        ouvert DEPUIS l'action prouve que l'agent l'a lancé.
    #>
    param([string]$Racine, [int]$DelaiSecondes = 90, [string[]]$MoteursDejaOuverts = @())
    $debut = Get-Date
    $fin = $debut.AddSeconds($DelaiSecondes)
    $statusPossible = $true
    while ((Get-Date) -lt $fin) {
        if ($statusPossible) {
            $s = Invoke-ActionSure -Racine $Racine -Action 'status' -DelaiSecondes 8
            if ($s.Ok) { Write-Host ''; return 'confirme' }
            if ($s.NonSupporte) { $statusPossible = $false }
        }
        if (Test-AgentVivant) { Write-Host ''; return 'probable' }
        if (-not $statusPossible -and ((Get-Date) - $debut).TotalSeconds -ge 10) {
            # Agent ancien sans relais caméra : un moteur qui rouvre son port prouve que l'agent l'a lancé.
            if (@(Get-EtatMoteurs -Racine $Racine | Where-Object { $_.EnEcoute -and ($MoteursDejaOuverts -notcontains [string]$_.Outil) }).Count -gt 0) { Write-Host ''; return 'probable' }
        }
        Write-Host '.' -NoNewline -ForegroundColor DarkGray
        Start-Sleep -Seconds 5
    }
    Write-Host ''
    return 'absent'
}

function Wait-Moteurs {
    <# Sonde les ports toutes les 5 s. -Attendus : identifiants d'outils ; vide = on observe jusqu'au délai. Renvoie les outils en marche. #>
    param([string]$Racine, [string[]]$Attendus = @(), [int]$DelaiSecondes = 180)
    $debut = Get-Date
    $fin = $debut.AddSeconds($DelaiSecondes)
    $dernier = ''
    $dernierAffichage = -100
    while ($true) {
        $etats = @(Get-EtatMoteurs -Racine $Racine)
        $enMarche = @($etats | Where-Object { $_.EnEcoute } | ForEach-Object { $_.Outil })
        $morceaux = @()
        foreach ($e in $etats) { $marque = 'non'; if ($e.EnEcoute) { $marque = 'OUI' }; $morceaux += ($e.Nom + ' : ' + $marque) }
        $resume = $morceaux -join '   '
        $ecoule = [int]((Get-Date) - $debut).TotalSeconds
        if ($resume -ne $dernier -or ($ecoule - $dernierAffichage) -ge 30) { Write-Info (('{0,4} s   ' -f $ecoule) + $resume); $dernier = $resume; $dernierAffichage = $ecoule }
        $manquants = @($Attendus | Where-Object { $_ -and $_ -ne 'converter' -and ($enMarche -notcontains $_) })
        if ($Attendus.Count -gt 0 -and $manquants.Count -eq 0) { return $enMarche }
        if ((Get-Date) -ge $fin) { return $enMarche }
        Start-Sleep -Seconds 5
    }
}

function Get-VerificationArret {
    <# Après extinction : tout ce qui devrait être fermé. Renvoie des lignes { Libelle, Ferme }. #>
    param([string]$Racine)
    $lignes = @()
    foreach ($m in @(Get-EtatMoteurs -Racine $Racine)) {
        $ou = 'port ' + $m.Port
        if ($m.Outil -eq 'model-studio') { $ou = 'port ' + $m.Port + ' dans HTTP.sys' }
        $lignes += [pscustomobject]@{ Libelle = ($m.Nom + ' (' + $ou + ')'); Ferme = (-not $m.EnEcoute) }
    }
    $lignes += [pscustomobject]@{ Libelle = ('Relais caméra de l''agent (port ' + $script:PortRelaisCamera + ')'); Ferme = (-not (Test-AgentVivant)) }
    return $lignes
}

function Wait-MoteursFermes {
    <#
        Après un « engines-stop » que l'agent exécute ENCORE (réponse EnCours du socle) : sonde
        les ports toutes les 5 s jusqu'à ce que tous les moteurs soient fermés, ou jusqu'au délai.
        Renvoie les noms des moteurs encore ouverts (vide = tout est fermé).
    #>
    param([string]$Racine, [int]$DelaiSecondes = 90)
    $fin = (Get-Date).AddSeconds($DelaiSecondes)
    while ($true) {
        $ouverts = @(Get-EtatMoteurs -Racine $Racine | Where-Object { $_.EnEcoute })
        if ($ouverts.Count -eq 0) { Write-Host ''; return @() }
        if ((Get-Date) -ge $fin) { Write-Host ''; return @($ouverts | ForEach-Object { [string]$_.Nom }) }
        Write-Host '.' -NoNewline -ForegroundColor DarkGray
        Start-Sleep -Seconds 5
    }
}

function Confirm-Forcage {
    <#
        Un refus (travail, maintenance, moteurs) n'est pas une impasse depuis le menu : en mode
        interactif on PROPOSE de forcer, mot exact FORCER. En mode piloté (-Oui ou -SansPause)
        on ne demande rien : on écrit la commande exacte à relancer et on renvoie $false.
    #>
    param([string]$Consequence, [string]$Lanceur, [bool]$Interactif)
    if (-not $Interactif) {
        Write-Conseil ('En dernier recours seulement, depuis le dossier du Worker : outils\' + $Lanceur + ' -Forcer   (' + $Consequence + ')')
        return $false
    }
    Write-Host ''
    Write-Alerte ('Tu peux passer outre, en dernier recours : ' + $Consequence)
    return (Confirm-Action -Question 'Continuer quand même ?' -MotExact 'FORCER')
}

function Show-TraceOutil {
    <# Dernières lignes du journal des outils pour cet outil : sert à lire ce qu'a fait la fenêtre administrateur. #>
    param([string]$Racine, [string]$Outil, [int]$Lignes = 8)
    $journal = Join-Path $Racine 'logs\outils-worker.log'
    if (-not (Test-Path -LiteralPath $journal -PathType Leaf)) { return }
    $trouve = @(Get-Content -LiteralPath $journal -Encoding UTF8 -Tail 60 | Where-Object { $_ -match [regex]::Escape($Outil) } | Select-Object -Last $Lignes)
    if ($trouve.Count -eq 0) { return }
    Write-Section 'Dernières traces de la fenêtre administrateur'
    foreach ($l in $trouve) { Write-Info $l }
}

function Test-CompteDuService {
    <#
        L'identité du Worker est chiffrée (DPAPI) pour le compte Windows qui fait tourner
        le service. Une fenêtre administrateur ouverte sous un AUTRE compte ne pourrait
        pas la relire. Renvoie $true si le compte courant est celui du service.
    #>
    param($Service)
    if (-not $Service -or -not $Service.Compte) { return $true }
    $compte = [string]$Service.Compte
    if ($compte -match '^(?i:LocalSystem|NT AUTHORITY\\)') { return $false }
    $nom = $compte
    if ($compte.Contains('\')) { $nom = $compte.Substring($compte.LastIndexOf('\') + 1) }
    return ($nom -ieq [Environment]::UserName)
}

function Get-ArgumentsCommuns {
    <# Arguments à retransmettre à l'instance administrateur. #>
    param([string]$Racine)
    return @('-Racine', $Racine, '-SansPause', '-Oui')
}
