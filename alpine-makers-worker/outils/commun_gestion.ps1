<#
    Fonctions partagées par les outils de la rubrique « GESTION DU WORKER » du menu (entrées 18 à 24) :
    contrôle total, doublons, arrêt des jobs, mode de partage, mise à jour, réparation.
    menu_worker.ps1 et etat_worker.ps1 le chargent aussi quand il est présent.

    À charger APRÈS commun.ps1 (et, pour un outil, commun_cycle.ps1) :
        . (Join-Path $PSScriptRoot 'commun.ps1')
        . (Join-Path $PSScriptRoot 'commun_cycle.ps1')
        . (Join-Path $PSScriptRoot 'commun_gestion.ps1')

    Règles tenues ici :
      - aucune fonction ne pose de question, ne demande l'élévation ni ne modifie le PC ;
      - rien ne lit config.json (Get-ConfigSure du socle le fait, sans rien de sensible) ;
      - ce fichier n'ouvre aucune connexion : tout ce qui concerne le site est demandé à l'AGENT,
        par sa boîte aux lettres locale (Invoke-ActionLocale), et c'est l'agent qui appelle le site ;
      - un outil existant (rallumer, redémarrer, démarrage automatique...) est lancé par
        Invoke-OutilEnfant, jamais dans le processus de l'outil appelant ;
      - le mode de lancement du Worker (service, tâche, session, aucun) n'est jamais supposé ici.
#>

# Fichier d'état écrit par l'agent à la racine du Worker (jamais par un outil).
$script:NomFichierEtat = 'ETAT-WORKER.txt'

# Première version de l'agent qui connaît les actions de gestion (status.indicator, relais vers le site).
$script:VersionGestion = '1.48.0'

# Fichiers du paquet dont la vie dépend d'un composant : absents ou différents, ce n'est ni une erreur ni
# une alerte. Liste IDENTIQUE à INTEGRITY_COMPONENT_FILES de l'agent (agent.py).
$script:FichiersGeresParComposant = @('orca_core_bridge.ps1')

# ---------------------------------------------------------------------------
# Petites fonctions pures
# ---------------------------------------------------------------------------

function Get-ChampGestion {
    <# Lecture tolérante d'un champ d'une réponse JSON : -Defaut si l'objet est nul ou ne porte pas ce champ. #>
    param($Objet, [string]$Nom, $Defaut = $null)
    if ($null -eq $Objet) { return $Defaut }
    if ($Objet -is [System.Collections.IDictionary]) {
        if ($Objet.Contains($Nom)) { return $Objet[$Nom] }
        return $Defaut
    }
    $propriete = $Objet.PSObject.Properties[$Nom]
    if ($null -eq $propriete) { return $Defaut }
    return $propriete.Value
}

function ConvertTo-EntierGestion {
    <# Entier entre 0 et -Maximum ; 0 pour toute valeur qui n'est pas un nombre (un booléen n'en est pas un). #>
    param($Valeur, [int]$Maximum = 9999)
    if ($null -eq $Valeur -or $Valeur -is [bool]) { return 0 }
    $nombre = 0
    if (-not [int]::TryParse(([string]$Valeur).Trim(), [ref]$nombre)) { return 0 }
    if ($nombre -lt 0) { return 0 }
    if ($nombre -gt $Maximum) { return $Maximum }
    return $nombre
}

function ConvertTo-DateLocale {
    <# Secondes Unix (nombre) -> date locale du PC ; $null si la valeur n'est pas un nombre plausible. #>
    param($Secondes)
    if ($null -eq $Secondes -or $Secondes -is [bool] -or $Secondes -is [string]) { return $null }
    try {
        $nombre = [double]$Secondes
        if ($nombre -lt 946684800 -or $nombre -gt 4102444800) { return $null }   # du 01.01.2000 au 01.01.2100
        return (New-Object datetime 1970, 1, 1, 0, 0, 0, ([DateTimeKind]::Utc)).AddSeconds($nombre).ToLocalTime()
    } catch { return $null }
}

function Compare-VersionWorker {
    <#
        Compare deux versions « 1.48.0 » champ par champ, en NOMBRES (1.9.0 est avant 1.20.1).
        Rend -1 (A avant B), 0 ou 1 ; $null si l'une des deux n'est pas une suite de nombres.
    #>
    param([string]$A, [string]$B)
    $motif = '^\d{1,9}(\.\d{1,9}){0,5}$'
    $gauche = ([string]$A).Trim(); $droite = ([string]$B).Trim()
    if ($gauche -notmatch $motif -or $droite -notmatch $motif) { return $null }
    $champsGauche = @($gauche.Split('.')); $champsDroite = @($droite.Split('.'))
    $taille = [Math]::Max($champsGauche.Count, $champsDroite.Count)
    for ($i = 0; $i -lt $taille; $i++) {
        $g = 0; $d = 0
        if ($i -lt $champsGauche.Count) { $g = [int]$champsGauche[$i] }
        if ($i -lt $champsDroite.Count) { $d = [int]$champsDroite[$i] }
        if ($g -lt $d) { return -1 }
        if ($g -gt $d) { return 1 }
    }
    return 0
}

function Format-DateEtat {
    <# « 08.10 à 14:02 » : la date d'un état, telle que le menu et les outils l'écrivent. #>
    param([datetime]$Date)
    return $Date.ToString("dd'.'MM' à 'HH':'mm", [Globalization.CultureInfo]::InvariantCulture)
}

function Get-PhraseDoublons {
    <# Phrase EXACTE à taper avant d'appliquer un plan de doublons. Source unique : l'agent attend la même. #>
    param([ValidateSet('link', 'delete')][string]$Mode = 'link')
    if ($Mode -eq 'delete') { return 'SUPPRIMER LES COPIES' }
    return 'LIER LES DOUBLONS'
}

function ConvertTo-ListeChoix {
    <#
        « team, contributors » -> @('team', 'contributors'). Découpe sur la virgule, retire les blancs et les
        doublons, rend la valeur telle qu'elle est écrite dans -Permis. Lève une erreur si une valeur n'y est pas.
        Rend TOUJOURS un tableau, même vide ou à un seul élément :
            $publics = ConvertTo-ListeChoix -Texte $Publics -Permis @('team', 'contributors', 'everyone')
        (ne pas l'entourer de @( ) à l'appel ; dans -Arguments d'une action locale, écrire @($publics).)
    #>
    param([string]$Texte = '', [string[]]$Permis = @())
    $liste = New-Object System.Collections.ArrayList
    foreach ($morceau in ([string]$Texte).Split(',')) {
        $valeur = $morceau.Trim()
        if (-not $valeur) { continue }
        $retenue = $null
        foreach ($possible in @($Permis)) { if ([string]$possible -ieq $valeur) { $retenue = [string]$possible; break } }
        if ($null -eq $retenue) { throw ('Valeur non permise : « ' + $valeur + ' ». Valeurs possibles : ' + (@($Permis) -join ', ') + '.') }
        if (-not $liste.Contains($retenue)) { [void]$liste.Add($retenue) }
    }
    return , ([string[]]$liste.ToArray())
}

# ---------------------------------------------------------------------------
# État écrit par l'agent : ETAT-WORKER.txt
# ---------------------------------------------------------------------------

function Read-FichierEtat {
    <#
        Dernier état écrit par l'agent dans ETAT-WORKER.txt (lignes « cle=valeur » seulement).
        Rend : Present, Etat (ok | maj | erreur | maj_erreur | inconnu), Date (heure du PC) ou $null,
        AgeMinutes, Version, Maj, Erreurs, Alertes, Codes.
        Fichier absent, illisible, trop gros, mal formé ou remplacé par un lien : Present = $false.
        Ne lève jamais d'erreur et n'écrit rien. Le fichier est ouvert en partage complet : l'agent peut
        le remplacer pendant la lecture.
    #>
    param([string]$Racine)
    $r = [pscustomobject]@{ Present = $false; Etat = ''; Date = $null; AgeMinutes = 0; Version = ''; Maj = ''; Erreurs = 0; Alertes = 0; Codes = @() }
    try {
        $fichier = Join-Path $Racine $script:NomFichierEtat
        if (-not (Test-Path -LiteralPath $fichier -PathType Leaf)) { return $r }
        if (([IO.File]::GetAttributes($fichier) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return $r }
        $texte = ''
        $flux = New-Object IO.FileStream($fichier, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        try {
            if ($flux.Length -gt 8192) { return $r }
            $lecteur = New-Object IO.StreamReader($flux, [Text.Encoding]::UTF8)
            $texte = $lecteur.ReadToEnd()
        } finally { $flux.Dispose() }
        $valeurs = @{}
        foreach ($ligne in ($texte -split "`r?`n")) {
            if ($ligne -match '^([a-z]{3,10})=(.*)$') {
                if (-not $valeurs.ContainsKey($Matches[1])) { $valeurs[$Matches[1]] = ([string]$Matches[2]).Trim() }
            }
        }
        $etat = [string]$valeurs['etat']
        if (@('ok', 'maj', 'erreur', 'maj_erreur', 'inconnu') -cnotcontains $etat) { return $r }
        $date = [datetime]::MinValue
        if (-not [datetime]::TryParseExact([string]$valeurs['date'], 'yyyy-MM-ddTHH:mm:ss', [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$date)) { return $r }
        $age = ((Get-Date) - $date).TotalMinutes
        if ($age -lt 0) { $age = 0 }
        if ($age -gt 100000000) { $age = 100000000 }
        $r.Etat = $etat
        $r.Date = $date
        $r.AgeMinutes = [int][Math]::Floor($age)
        if ([string]$valeurs['version'] -match '^\d{1,9}(\.\d{1,9}){2}$') { $r.Version = [string]$valeurs['version'] }
        if ([string]$valeurs['maj'] -match '^\d{1,9}(\.\d{1,9}){2}$') { $r.Maj = [string]$valeurs['maj'] }
        $r.Erreurs = ConvertTo-EntierGestion $valeurs['erreurs']
        $r.Alertes = ConvertTo-EntierGestion $valeurs['alertes']
        $codes = New-Object System.Collections.ArrayList
        foreach ($code in ([string]$valeurs['codes']).Split(',')) {
            $c = $code.Trim()
            if ($c -match '^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$' -and -not $codes.Contains($c) -and $codes.Count -lt 12) { [void]$codes.Add($c) }
        }
        $r.Codes = [string[]]$codes.ToArray()
        $r.Present = $true
    } catch {
        $r.Present = $false
    }
    return $r
}

# ---------------------------------------------------------------------------
# Parler à l'agent, et par lui au site
# ---------------------------------------------------------------------------

function Get-ClasseEchecLocal {
    <#
        Pourquoi une demande déposée dans la boîte aux lettres locale n'a pas abouti :
          'redemarrer' = l'agent en mémoire est plus ancien que ses fichiers (il répond « Action locale inconnue ») ;
          'ancien'     = les fichiers installés ne connaissent pas cette action ;
          'muet'       = aucune réponse : agent arrêté ou en démarrage, Worker non associé, Python introuvable ;
          'refus'      = l'agent a répondu non, avec sa raison ; ou la demande n'est jamais partie parce que ses
                         paramètres sont refusés ici même (trop volumineux, illisibles).
    #>
    param($Reponse)
    if ($null -eq $Reponse) { return 'muet' }
    if ($Reponse.NonSupporte) {
        if ([string]$Reponse.Erreur -match 'plus ancien que ses fichiers') { return 'redemarrer' }
        return 'ancien'
    }
    $erreur = [string]$Reponse.Erreur
    # Refus d'Invoke-ActionLocale avant tout lancement : rien n'a été déposé, l'agent n'y est pour rien.
    if ($erreur -match '^(Paramètres |Action locale invalide)') { return 'refus' }
    if ($Reponse.DelaiDepasse -or ([int]$Reponse.Code -lt 0)) { return 'muet' }
    if (-not $erreur) { return 'muet' }
    if ($erreur -match '^(Configuration du Worker illisible|Worker non associe|Aucune réponse de l''agent|Réponse illisible de l''agent)') { return 'muet' }
    if ($erreur -match 'Traceback \(most recent call last\)') { return 'muet' }
    return 'refus'
}

function Set-EchecLocal {
    <# Reporte sur un résultat de Get-EtatSite / Invoke-RelaisSite la raison d'un échec de la boîte aux lettres. #>
    param($Retour, $Reponse)
    switch (Get-ClasseEchecLocal $Reponse) {
        'redemarrer' { $Retour.AgentARedemarrer = $true }
        'ancien'     { $Retour.AgentTropAncien = $true }
        'muet'       { $Retour.AgentMuet = $true }
    }
    if ($null -ne $Reponse) { $Retour.Erreur = [string]$Reponse.Erreur }
}

function Get-MessageRelais {
    <# Textes figés des refus : les mêmes mots dans tous les outils (Write-RefusRelais, Get-EtatAgent). #>
    param([string]$Cle)
    switch ($Cle) {
        'muet'       { return 'L''agent ne répond pas : il est arrêté, ou il démarre encore.' }
        'redemarrer' { return ('L''agent en mémoire est plus ancien que ses fichiers : redémarre-le avec ' + (Get-NomEntree 10) + ' pour activer cette action.') }
        'ancien'     { return 'L''agent installé est trop ancien pour cette action : mets le Worker à jour depuis le site (Mes Workers > « Mettre à jour »).' }
    }
    return ''
}

function Get-EtatAgent {
    <#
        L'agent répond-il, et connaît-il les actions de gestion ? Une seule question « status ».
        Rend : Vivant, Statut (réponse « status » ou $null), NonSupporte, Capable, ARedemarrer, Message.
          Capable     = la réponse porte le champ « indicator » (agent 1.48.0 ou plus en mémoire) ;
          ARedemarrer = l'agent répond, n'est pas Capable, alors que les fichiers installés le sont :
                        il faut le redémarrer pour activer le nouveau code ;
          Message     = '' quand tout va bien, sinon le texte figé à afficher (voir Write-RefusRelais).
    #>
    param([string]$Racine, [int]$DelaiSecondes = 8)
    $r = [pscustomobject]@{ Vivant = $false; Statut = $null; NonSupporte = $false; Capable = $false; ARedemarrer = $false; Message = '' }
    $reponse = $null
    try { $reponse = Invoke-ActionLocale -Racine $Racine -Action 'status' -DelaiSecondes $DelaiSecondes } catch { }
    if ($reponse -and $reponse.Ok -and $reponse.Resultat) {
        $r.Vivant = $true
        $r.Statut = $reponse.Resultat
        $r.Capable = (@($reponse.Resultat.PSObject.Properties.Name) -contains 'indicator')
        if (-not $r.Capable) {
            $installee = ''
            try { $installee = [string](Get-VersionInstallee -Racine $Racine) } catch { }
            $ordre = Compare-VersionWorker $installee $script:VersionGestion
            $r.ARedemarrer = ($null -ne $ordre -and $ordre -ge 0)
            if ($r.ARedemarrer) { $r.Message = Get-MessageRelais 'redemarrer' } else { $r.Message = Get-MessageRelais 'ancien' }
        }
        return $r
    }
    switch (Get-ClasseEchecLocal $reponse) {
        'redemarrer' { $r.Vivant = $true; $r.NonSupporte = $true; $r.ARedemarrer = $true; $r.Message = Get-MessageRelais 'redemarrer' }
        'ancien'     { $r.NonSupporte = $true; $r.Message = Get-MessageRelais 'ancien' }
        default      { $r.Message = Get-MessageRelais 'muet' }
    }
    return $r
}

function Get-EtatSite {
    <#
        Ce que le site sait de ce Worker (mise à jour proposée, santé, partage), demandé à l'agent : action
        locale « site-state ». -Rafraichir demande à l'agent de relire le site maintenant ; la fonction relit
        ensuite « site-state » chaque seconde tant que l'agent répond « refreshing », jusqu'au délai.
        Rend : Connu, Site (réponse du site ou $null), DateLecture, SiteSansFonction, SiteInjoignable,
        AgentTropAncien, AgentARedemarrer, AgentMuet, Http, CodeSite, Erreur.
        Connu = un état du site est disponible ; avec -Rafraichir, seulement si la relecture a réussi
        (jamais un état ancien présenté comme frais). En cas d'échec : Write-RefusRelais -Resultat <ce résultat>.
    #>
    param([string]$Racine, [switch]$Rafraichir, [int]$DelaiSecondes = 20)
    $r = [pscustomobject]@{
        Connu = $false; Site = $null; DateLecture = $null; SiteSansFonction = $false; SiteInjoignable = $false
        AgentTropAncien = $false; AgentARedemarrer = $false; AgentMuet = $false; Http = 0; CodeSite = ''; Erreur = ''
    }
    if ($DelaiSecondes -lt 1) { $DelaiSecondes = 1 }
    $limite = (Get-Date).AddSeconds($DelaiSecondes)
    $attente = [Math]::Max(3, [Math]::Min(8, $DelaiSecondes))
    $reponse = $null
    try { $reponse = Invoke-ActionLocale -Racine $Racine -Action 'site-state' -DelaiSecondes $attente -Arguments @{ refresh = [bool]$Rafraichir } } catch { }
    $etat = $null
    while ($true) {
        if (-not $reponse -or -not $reponse.Ok -or -not $reponse.Resultat) { Set-EchecLocal -Retour $r -Reponse $reponse; return $r }
        $etat = $reponse.Resultat
        if (-not [bool](Get-ChampGestion $etat 'refreshing' $false)) { break }
        if ((Get-Date) -ge $limite) {
            $r.SiteInjoignable = $true
            $r.Erreur = 'Le site n''a pas répondu dans le délai.'
            return $r
        }
        Start-Sleep -Seconds 1
        $reponse = $null
        try { $reponse = Invoke-ActionLocale -Racine $Racine -Action 'site-state' -DelaiSecondes $attente } catch { }
    }
    $r.Site = Get-ChampGestion $etat 'site'
    $r.DateLecture = ConvertTo-DateLocale (Get-ChampGestion $etat 'fetched_at')
    $r.SiteSansFonction = [bool](Get-ChampGestion $etat 'missing' $false)
    $r.SiteInjoignable = [bool](Get-ChampGestion $etat 'unreachable' $false)
    $erreur = Get-ChampGestion $etat 'error'
    if ($null -ne $erreur) {
        $r.Http = ConvertTo-EntierGestion (Get-ChampGestion $erreur 'http')
        $r.CodeSite = [string](Get-ChampGestion $erreur 'site_code' '')
        $r.Erreur = [string](Get-ChampGestion $erreur 'message' '')
        if ($r.Http -eq 409 -and $r.CodeSite -eq 'WORKER_UPDATE_REQUIRED') { $r.AgentTropAncien = $true }
    }
    $echec = ($r.SiteSansFonction -or $r.SiteInjoignable -or $r.AgentTropAncien -or ($null -ne $erreur))
    $r.Connu = ([bool](Get-ChampGestion $etat 'known' $false) -and ($null -ne $r.Site) -and -not ($Rafraichir -and $echec))
    if (-not $r.Connu -and -not $echec -and -not $r.Erreur) { $r.Erreur = 'L''agent n''a pas encore lu l''état du site.' }
    return $r
}

function Invoke-RelaisSite {
    <#
        Invoke-RelaisSiteUneFois, puis, si le site répond que la session du propriétaire manque ou a expiré
        (WORKER_EXE_SESSION_REQUIRED / _INVALID) et qu'une console avec clavier le permet, une nouvelle connexion
        (commun_connexion.ps1) et UN second essai. Mêmes paramètres et même résultat.
    #>
    param([string]$Racine, [string]$Action, [hashtable]$Arguments = $null, [int]$DelaiSecondes = 60)
    $r = Invoke-RelaisSiteUneFois -Racine $Racine -Action $Action -Arguments $Arguments -DelaiSecondes $DelaiSecondes
    $sessionAbsente = (@('WORKER_EXE_SESSION_REQUIRED', 'WORKER_EXE_SESSION_INVALID') -contains [string]$r.CodeSite)
    if (-not $sessionAbsente -or -not (Get-Command -Name 'Invoke-ConnexionProprietaire' -CommandType Function -ErrorAction SilentlyContinue)) { return $r }
    if (-not (Test-ReconnexionPossible)) { return $r }
    Write-Host ''
    if ([string]$r.CodeSite -eq 'WORKER_EXE_SESSION_INVALID') { Write-Alerte 'Ta session a expiré ou a été retirée sur le site : reconnecte-toi pour continuer.' }
    else { Write-Alerte 'Cette action demande la connexion du propriétaire (comme dans worker.exe).' }
    Clear-SessionWorker
    if ((Invoke-ConnexionProprietaire -Racine $Racine -Reconnexion) -ne 'connecte') { return $r }
    return (Invoke-RelaisSiteUneFois -Racine $Racine -Action $Action -Arguments $Arguments -DelaiSecondes $DelaiSecondes)
}

function Invoke-RelaisSiteUneFois {
    <#
        Fait faire par l'AGENT une demande signée au site, et attend sa fin :
          site-update (mise à jour), site-repair (réparations), jobs-stop (arrêt des jobs), sharing-set (partage).
        L'action locale rend tout de suite un identifiant d'opération ; « relay-status » est ensuite interrogé
        chaque seconde jusqu'à « done » ou « failed », ou jusqu'au délai.
        Rend : Ok, Resultat (réponse du site), Local (ce que l'agent a fait lui-même), Erreur, Http, CodeSite,
        SiteSansFonction, SiteInjoignable, AgentTropAncien, AgentARedemarrer, AgentMuet, DelaiDepasse.
        Toute liste de -Arguments s'écrit @( ), même à un seul élément.
        En cas d'échec : Write-RefusRelais -Resultat <ce résultat>. Aucune question, aucun affichage.
    #>
    param([string]$Racine, [string]$Action, [hashtable]$Arguments = $null, [int]$DelaiSecondes = 60)
    $r = [pscustomobject]@{
        Ok = $false; Resultat = $null; Local = $null; Erreur = ''; Http = 0; CodeSite = ''; SiteSansFonction = $false; SiteInjoignable = $false
        AgentTropAncien = $false; AgentARedemarrer = $false; AgentMuet = $false; DelaiDepasse = $false
    }
    if (@('site-update', 'site-repair', 'jobs-stop', 'sharing-set', 'resources-set') -cnotcontains [string]$Action) { $r.Erreur = 'Action de relais inconnue.'; return $r }
    if ($DelaiSecondes -lt 5) { $DelaiSecondes = 5 }
    $limite = (Get-Date).AddSeconds($DelaiSecondes)
    $depot = $null
    # Session du propriétaire (commun_connexion.ps1) : jointe par un fichier d'arguments lu puis effacé par l'agent,
    # jamais par la ligne de commande. Le site l'exige pour ces quatre demandes (agent worker_menu_session_v1).
    $fichierSession = ''
    $avecSession = $Arguments
    try {
        if (Get-Command -Name 'New-FichierSessionWorker' -CommandType Function -ErrorAction SilentlyContinue) { $fichierSession = New-FichierSessionWorker -Racine $Racine }
    } catch { $fichierSession = '' }
    if ($fichierSession) {
        $avecSession = @{}
        if ($null -ne $Arguments) { foreach ($cle in @($Arguments.get_Keys())) { $avecSession[[string]$cle] = $Arguments[$cle] } }
        $avecSession['args_file'] = $fichierSession
    }
    try { $depot = Invoke-ActionLocale -Racine $Racine -Action $Action -DelaiSecondes 10 -Arguments $avecSession } catch { }
    finally { if ($fichierSession) { Remove-FichierArgumentsWorker -Racine $Racine -Jeton $fichierSession } }
    if (-not $depot -or -not $depot.Ok -or -not $depot.Resultat) { Set-EchecLocal -Retour $r -Reponse $depot; return $r }
    $operation = [string](Get-ChampGestion $depot.Resultat 'operation' '')
    if ($operation -cnotmatch '^[0-9a-f]{32}$') { $r.Erreur = 'Réponse inattendue de l''agent : la demande n''a pas de numéro de suivi.'; return $r }
    while ($true) {
        $suivi = $null
        try { $suivi = Invoke-ActionLocale -Racine $Racine -Action 'relay-status' -DelaiSecondes 5 -Arguments @{ operation = $operation } } catch { }
        if ($suivi -and $suivi.Ok -and $suivi.Resultat) {
            $etat = [string](Get-ChampGestion $suivi.Resultat 'state' '')
            if ($etat -eq 'done' -or $etat -eq 'failed') {
                $r.Resultat = Get-ChampGestion $suivi.Resultat 'response'
                $r.Local = Get-ChampGestion $suivi.Resultat 'local'
                $r.Http = ConvertTo-EntierGestion (Get-ChampGestion $suivi.Resultat 'http')
                $r.CodeSite = [string](Get-ChampGestion $suivi.Resultat 'site_code' '')
                if ($etat -eq 'done') { $r.Ok = $true; return $r }
                $r.Erreur = [string](Get-ChampGestion $suivi.Resultat 'error' '')
                $r.SiteSansFonction = [bool](Get-ChampGestion $suivi.Resultat 'site_missing' $false)
                $r.SiteInjoignable = [bool](Get-ChampGestion $suivi.Resultat 'unreachable' $false)
                if ($r.Http -eq 409 -and $r.CodeSite -eq 'WORKER_UPDATE_REQUIRED') { $r.AgentTropAncien = $true }
                return $r
            }
        }
        elseif ((Get-ClasseEchecLocal $suivi) -eq 'refus') {
            # « Opération inconnue ou expirée » : l'agent a redémarré entre-temps, le suivi est perdu.
            $r.Erreur = [string]$suivi.Erreur
            $r.DelaiDepasse = $true
            return $r
        }
        if ((Get-Date) -ge $limite) { $r.DelaiDepasse = $true; $r.Erreur = 'Pas de réponse dans le délai.'; return $r }
        Start-Sleep -Seconds 1
    }
}

function Write-RefusRelais {
    <#
        Écrit UN message, toujours le même pour une même cause, à partir d'un résultat d'Invoke-RelaisSite ou
        de Get-EtatSite (un résultat de Get-EtatAgent est accepté aussi). Par ordre de priorité : agent muet,
        agent à redémarrer, agent trop ancien, site sans la fonction, site injoignable, trop de demandes,
        autre refus du site (son message), délai dépassé, puis la raison donnée par l'agent lui-même.
    #>
    param($Resultat)
    $muet = [bool](Get-ChampGestion $Resultat 'AgentMuet' $false)
    $redemarrer = ([bool](Get-ChampGestion $Resultat 'AgentARedemarrer' $false) -or [bool](Get-ChampGestion $Resultat 'ARedemarrer' $false))
    $ancien = [bool](Get-ChampGestion $Resultat 'AgentTropAncien' $false)
    if ($null -eq $Resultat) { $muet = $true }
    elseif ($null -ne $Resultat.PSObject.Properties['Vivant']) {
        # Résultat de Get-EtatAgent.
        $vivant = [bool]$Resultat.Vivant
        if (-not $redemarrer) {
            if ([bool]$Resultat.NonSupporte -or ($vivant -and -not [bool]$Resultat.Capable)) { $ancien = $true }
            elseif (-not $vivant) { $muet = $true }
        }
    }
    $http = ConvertTo-EntierGestion (Get-ChampGestion $Resultat 'Http')
    $codeSite = [string](Get-ChampGestion $Resultat 'CodeSite' '')
    $erreur = [string](Get-ChampGestion $Resultat 'Erreur' '')
    if ($muet) {
        Write-Alerte (Get-MessageRelais 'muet')
        Write-Conseil ('Rallume-le avec ' + (Get-NomEntree 12) + ', puis relance cet outil.')
    }
    elseif ($redemarrer) { Write-Alerte (Get-MessageRelais 'redemarrer') }
    elseif ($ancien) { Write-Alerte (Get-MessageRelais 'ancien') }
    elseif ([bool](Get-ChampGestion $Resultat 'SiteSansFonction' $false)) {
        Write-Alerte 'Le site n''a pas encore cette fonction (réponse 404) : le dashboard n''a pas été redémarré dans sa nouvelle version.'
        Write-Conseil 'En attendant, fais-le depuis le site : Mes Workers, carte de ce Worker.'
    }
    elseif ([bool](Get-ChampGestion $Resultat 'SiteInjoignable' $false)) {
        if ($http -gt 0) { Write-Alerte ('Le site ne répond pas pour l''instant (erreur ' + $http + ') : réessaie dans une minute.') }
        else { Write-Alerte 'Le site est injoignable depuis ce PC : rien ne lui a été demandé.' }
    }
    elseif ($http -eq 429) { Write-Alerte 'Trop de demandes depuis ce Worker : réessaie dans quelques minutes.' }
    elseif (@('WORKER_EXE_SESSION_REQUIRED', 'WORKER_EXE_SESSION_INVALID') -contains $codeSite) {
        Write-Alerte 'Le site exige la connexion du propriétaire pour cette action : rien n''a été fait.'
        Write-Conseil 'Ouvre MENU-WORKER.bat (ou worker.exe) et connecte-toi avec le compte du propriétaire, puis relance cette entrée.'
    }
    elseif ($codeSite -eq 'CO_OWNER_RIGHT_MISSING') {
        Write-Alerte 'Action refusée : elle est réservée au propriétaire ou demande un droit que ton compte de co-propriétaire n''a pas.'
    }
    elseif ($http -gt 0 -or $codeSite) {
        if (-not $erreur) { $erreur = 'aucune raison donnée (erreur ' + $http + ').' }
        Write-Alerte ('Le site a refusé : ' + $erreur)
    }
    elseif ([bool](Get-ChampGestion $Resultat 'DelaiDepasse' $false)) {
        Write-Alerte 'Pas de réponse dans le délai : la demande est peut-être partie. Vérifie dans Mes Workers avant de recommencer.'
    }
    elseif ($erreur) { Write-Alerte $erreur }   # refus de l'agent lui-même : sa raison, telle quelle
    else { Write-Alerte 'La demande n''a pas abouti, sans raison donnée.' }
}

function Invoke-OutilEnfant {
    <#
        Lance un outil EXISTANT (rallumer_worker, redemarrer_agent, demarrage_auto, reconnecter,
        nettoyage_leger...) dans un powershell.exe ENFANT, attendu jusqu'à sa fin, et rend son code de sortie.
        Jamais « & $chemin » dans le processus de l'outil appelant : hors mode service, ces outils démarrent
        le superviseur du Worker, qui serait alors un enfant de l'outil appelant et s'arrêterait avec lui
        (« Interrompre » ou fermeture de worker.exe). Lancé par un enfant qui se termine, il lui survit.
        Affichage et clavier sont ceux de l'appelant. Toute confirmation est posée AVANT l'appel :
        l'outil reçoit -Oui -SansPause dans -Arguments.
    #>
    param([string]$Script, [string[]]$Arguments = @())
    if (-not $Script -or -not (Test-Path -LiteralPath $Script -PathType Leaf)) {
        Write-Erreur ('Outil introuvable : ' + [string]$Script)
        return 1
    }
    $global:LASTEXITCODE = 0
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Script @Arguments | Out-Host
    } catch {
        Write-Erreur ('Impossible de lancer l''outil : ' + [string]$_.Exception.Message)
        return 1
    }
    if ($null -eq $LASTEXITCODE) { return 1 }
    return [int]$LASTEXITCODE
}

# ---------------------------------------------------------------------------
# Contrôles locaux : fichiers de l'agent, droits du dossier
# ---------------------------------------------------------------------------

function Test-IntegriteAgent {
    <#
        Compare chaque fichier listé dans agent_manifest.json (empreinte SHA-256 posée par la mise à jour)
        avec le disque. Rend : Connu, Total (fichiers contrôlés), Manquants, Modifies (50 noms au plus chacun).
        Connu = $false si le manifeste est absent ou illisible. Les fichiers gérés par un composant
        ($script:FichiersGeresParComposant) ne sont jamais contrôlés. Lecture seule.
    #>
    param([string]$Racine)
    $r = [pscustomobject]@{ Connu = $false; Total = 0; Manquants = @(); Modifies = @() }
    $fichiers = $null
    try {
        $manifeste = Join-Path $Racine 'agent_manifest.json'
        if (-not (Test-Path -LiteralPath $manifeste -PathType Leaf)) { return $r }
        $fichiers = (Get-Content -LiteralPath $manifeste -Raw -Encoding UTF8 | ConvertFrom-Json).files
    } catch { return $r }
    if ($null -eq $fichiers -or $fichiers -is [string] -or $fichiers -is [System.Array]) { return $r }
    $manquants = New-Object System.Collections.ArrayList
    $modifies = New-Object System.Collections.ArrayList
    $total = 0
    foreach ($entree in @($fichiers.PSObject.Properties)) {
        $relatif = [string]$entree.Name
        $attendue = [string]$entree.Value
        # Chemins du manifeste : relatifs, en « / ». Tout chemin absolu ou remontant est refusé.
        if (-not $relatif -or $relatif -match '^[\\/]' -or $relatif -match ':' -or $relatif -match '(^|[\\/])\.\.([\\/]|$)') { continue }
        if ($script:FichiersGeresParComposant -contains $relatif) { continue }
        if ($attendue -notmatch '^[0-9a-fA-F]{64}$') { continue }
        $total++
        $chemin = Join-Path $Racine ($relatif -replace '/', '\')
        if (-not (Test-Path -LiteralPath $chemin -PathType Leaf)) {
            if ($manquants.Count -lt 50) { [void]$manquants.Add($relatif) }
            continue
        }
        $empreinte = ''
        try { $empreinte = [string](Get-FileHash -LiteralPath $chemin -Algorithm SHA256 -ErrorAction Stop).Hash } catch { continue }   # illisible à l'instant : ni manquant ni modifié
        if ($empreinte -ine $attendue) { if ($modifies.Count -lt 50) { [void]$modifies.Add($relatif) } }
    }
    $r.Connu = $true
    $r.Total = $total
    $r.Manquants = [string[]]$manquants.ToArray()
    $r.Modifies = [string[]]$modifies.ToArray()
    return $r
}

function Get-ComptesEcritureAcl {
    <#
        Partie pure de Test-DroitsDossier : SID des comptes qui reçoivent un droit d'ÉCRITURE par une règle
        « Autoriser » de cette liste de droits, hors comptes de confiance (compte courant, propriétaire du
        dossier, Système, Administrateurs, TrustedInstaller, Créateur propriétaire). Comparaison par SID :
        les noms de comptes sont traduits selon la langue de Windows.
    #>
    param($Acl, [string]$SidCourant = '')
    $confiance = @(
        'S-1-5-18',                                                           # Système
        'S-1-5-32-544',                                                       # Administrateurs
        'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464',     # TrustedInstaller
        'S-1-3-0'                                                             # Créateur propriétaire
    )
    if ($SidCourant) { $confiance += $SidCourant }
    try {
        $proprietaire = [string]$Acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
        if ($proprietaire) { $confiance += $proprietaire }
    } catch { }
    # CreateFiles 0x2, AppendData 0x4, Delete 0x10000, ChangePermissions 0x40000, TakeOwnership 0x80000 (Write, Modify
    # et FullControl contiennent CreateFiles), plus les droits génériques d'écriture des règles héritables.
    $masque = 0x2 -bor 0x4 -bor 0x10000 -bor 0x40000 -bor 0x80000 -bor 0x40000000 -bor 0x10000000
    $trouves = New-Object System.Collections.ArrayList
    foreach ($regle in @($Acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]))) {
        if ([string]$regle.AccessControlType -ne 'Allow') { continue }
        if (([int]$regle.FileSystemRights -band $masque) -eq 0) { continue }
        $sid = [string]$regle.IdentityReference.Value
        if (-not $sid -or ($confiance -contains $sid)) { continue }
        if (-not $trouves.Contains($sid)) { [void]$trouves.Add($sid) }
    }
    return , ([string[]]$trouves.ToArray())
}

function Test-DroitsDossier {
    <#
        Le dossier du Worker est-il modifiable par un AUTRE compte du PC ? (Qui peut y écrire peut déposer
        une demande dans la boîte aux lettres de l'agent.) Rend : Connu, Ouvert, Comptes (5 noms au plus).
        Lecture seule. Toute erreur : Connu = $false.
    #>
    param([string]$Racine)
    $r = [pscustomobject]@{ Connu = $false; Ouvert = $false; Comptes = @() }
    try {
        $acl = Get-Acl -LiteralPath $Racine -ErrorAction Stop
        $courant = [string][System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $sids = Get-ComptesEcritureAcl -Acl $acl -SidCourant $courant
        $noms = New-Object System.Collections.ArrayList
        foreach ($sid in @($sids | Select-Object -First 5)) {
            $nom = [string]$sid
            try { $nom = [string](New-Object System.Security.Principal.SecurityIdentifier($sid)).Translate([System.Security.Principal.NTAccount]).Value } catch { }
            [void]$noms.Add($nom)
        }
        $r.Comptes = [string[]]$noms.ToArray()
        $r.Ouvert = (@($sids).Count -gt 0)
        $r.Connu = $true
    } catch {
        $r.Connu = $false; $r.Ouvert = $false; $r.Comptes = @()
    }
    return $r
}

# ---------------------------------------------------------------------------
# Diagnostic profond (agent 1.50.33) : outils\diagnostic_profond.py, sans l'agent
# ---------------------------------------------------------------------------

function Invoke-DiagnosticProfond {
    <#
        Lance outils\diagnostic_profond.py avec le Python du Worker (System.Diagnostics.Process : ni « & » ni
        redirection PowerShell, sorties lues à part) et rend sa réponse JSON. -Reparer : codes à réparer (les seuls
        que le script accepte : déplacements sauvegardés, bibliothèque de sécurité). -JournauxService : analyse aussi
        les journaux du service AlpineWorker (seulement quand il vise ce dossier).
        Rend : Ok, Points (liste), Reparations (liste), Erreur.
    #>
    param([string]$Racine, [string[]]$Reparer = @(), [switch]$JournauxService, [int]$DelaiSecondes = 120)
    $r = [pscustomobject]@{ Ok = $false; Points = @(); Reparations = @(); Erreur = '' }
    $script = Join-Path $script:DossierOutils 'diagnostic_profond.py'
    if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { $r.Erreur = 'diagnostic_profond.py est absent des outils de ce Worker : mets le Worker à jour (23).'; return $r }
    $python = Get-PythonWorker -Racine $Racine
    if (-not $python) { $r.Erreur = 'Interpréteur Python du Worker introuvable (python_path.txt).'; return $r }
    $codes = @($Reparer | Where-Object { [string]$_ -cmatch '^D-[A-Z-]{2,40}$' })
    $arguments = '"' + $script + '" --root "' + $Racine.TrimEnd('\') + '" --json'
    if ($JournauxService) { $arguments += ' --service-logs' }
    if ($codes.Count -gt 0) { $arguments += ' --repair ' + ($codes -join ',') }
    $p = $null
    try {
        $info = New-Object System.Diagnostics.ProcessStartInfo
        $info.FileName = $python
        $info.Arguments = $arguments
        $info.WorkingDirectory = $Racine
        $info.UseShellExecute = $false
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $info.CreateNoWindow = $true
        $info.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
        $p = [System.Diagnostics.Process]::Start($info)
        $lecture = $p.StandardOutput.ReadToEndAsync()
        $erreurs = $p.StandardError.ReadToEndAsync()
        if (-not $p.WaitForExit($DelaiSecondes * 1000)) { try { $p.Kill() } catch { }; $r.Erreur = 'Le diagnostic profond n''a pas fini dans le délai.'; return $r }
        $p.WaitForExit()
        [void]$erreurs.Wait(5000)
        $texte = ''
        if ($lecture.Wait(5000)) { $texte = [string]$lecture.Result }
    } catch {
        $r.Erreur = 'Impossible de lancer le diagnostic profond : ' + [string]$_.Exception.Message
        return $r
    } finally {
        if ($p) { try { $p.Dispose() } catch { } }
    }
    $json = (@($texte -split "`r?`n") | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1)
    if (-not $json) { $r.Erreur = 'Le diagnostic profond n''a rien rendu.'; return $r }
    try { $reponse = $json | ConvertFrom-Json } catch { $r.Erreur = 'Réponse illisible du diagnostic profond.'; return $r }
    $r.Ok = [bool](Get-ChampGestion $reponse 'ok' $false)
    $r.Points = @(Get-ChampGestion $reponse 'points' @() | Where-Object { $_ })
    $r.Reparations = @(Get-ChampGestion $reponse 'repairs' @() | Where-Object { $_ })
    if (-not $r.Ok) { $r.Erreur = 'Le diagnostic profond s''est interrompu (' + [string](Get-ChampGestion $reponse 'error' '?') + ').' }
    return $r
}

# Connexion du propriétaire (menu du Worker, comme worker.exe) et tableau de bord : chargés quand ils sont livrés.
# Absents (paquet ancien), les relais partent sans session, comme avant, et le site décide.
foreach ($socleSupplementaire in @('commun_connexion.ps1', 'commun_tableau.ps1')) {
    $cheminSocle = Join-Path $PSScriptRoot $socleSupplementaire
    if (Test-Path -LiteralPath $cheminSocle -PathType Leaf) { . $cheminSocle }
}
