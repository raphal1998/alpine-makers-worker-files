<#
    Mode de partage de CE Worker (entrée 22 du menu, rubrique « Gestion du Worker »).

    Lit le partage sur le SITE (par l'agent, route signée), l'affiche, puis propose de le régler : privé,
    comptes choisis, public (un ou plusieurs publics, outils toujours choisis), acceptation automatique des jobs.
    Rien ne se règle hors ligne. Seuls six champs partent, par l'agent (action locale « sharing-set ») :
    enabled, audiences, tools, expected (empreinte du partage lue juste avant), auto_accept_jobs et
    confirm_everyone_auto. Tout le reste se règle sur le site : inviter ou retirer un compte, outils et modèles
    par compte, membres de l'équipe, admission du Worker.

    Les confirmations de cet outil protègent d'une erreur de manipulation ; le site refait tous les contrôles
    et prévient le compte propriétaire à chaque réglage.

    Pilotage sans clavier (essais ; worker.exe ne transmet que -SansPause) :
        mode_partage.ps1 -Oui -SansPause                      affiche l'état, ne change rien
        mode_partage.ps1 -Mode prive -Oui -SansPause
        mode_partage.ps1 -Mode comptes -Oui -SansPause
        mode_partage.ps1 -Mode public -Publics team,contributors -Outils image_generation -AutoAccepter non -Oui -SansPause
        mode_partage.ps1 -AutoAccepter oui -Oui -SansPause    Worker déjà public : acceptation automatique seule
    -Publics : team, contributors, everyone.   -Outils : parmi les outils que le site dit partageables.
    -ConfirmerTous vaut le mot OUVRIR A TOUS (tous les comptes + acceptation automatique) ; -Oui ne le remplace jamais.
    -Simulation montre le réglage demandé et n'envoie rien.
    Codes de sortie : 0 fait, ou rien à changer ; 1 échec ou réglage impossible (agent, site, compte) ; 2 refusé ou annulé.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause,
    [switch]$Oui,
    [switch]$Simulation,
    [string]$Mode = '',
    [string]$Publics = '',
    [string]$Outils = '',
    [string]$AutoAccepter = '',
    [switch]$ConfirmerTous
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'commun_cycle.ps1')
. (Join-Path $PSScriptRoot 'commun_gestion.ps1')
Initialize-ConsoleWorker -Titre 'Mode de partage du Worker'
$NomOutil = 'mode_partage'
try { $Racine = Get-RacineWorker -Racine $Racine } catch { Write-Erreur ([string]$_.Exception.Message); Exit-Outil -Code 1 -SansPause:$SansPause }

# Publics que cet outil sait ouvrir, dans l'ordre du site, et leur nom à l'écran.
$script:PublicsConnus = @('team', 'contributors', 'everyone')
$script:NomsPublics = @{ team = 'mon équipe'; contributors = 'comptes qui partagent aussi un Worker'; everyone = 'tous les comptes connectés' }
$script:Fleche = '->'
if (-not $script:ModeAscii) { $script:Fleche = [string][char]0x2192 }
$MotOuvrirATous = 'OUVRIR A TOUS'
$TexteResteSite = 'Restent sur le site (choix 5) : inviter un compte par son pseudo, retirer un compte, régler outils et modèles par compte, choisir des membres de ton équipe.'

# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------

function Test-EntreeRedirigee {
    try { return [bool][Console]::IsInputRedirected } catch { return $false }
}

function Read-Reponse {
    <#
        Pose une question et rend la réponse sans ses blancs ('' si rien n'est tapé, ou si l'entrée est fermée).
        Sous worker.exe l'entrée est un tube : PowerShell n'y écrit pas l'invite de Read-Host, la question est
        donc écrite en clair avant la lecture.
    #>
    param([string]$Invite)
    $reponse = $null
    if (Test-EntreeRedirigee) {
        Write-LignesCoupees -Prefixe '   ' -Texte ($Invite + ' :') -Couleur White
        $reponse = Read-Host
    }
    else { $reponse = Read-Host ('   ' + $Invite) }
    if ($null -eq $reponse) { return '' }
    return ([string]$reponse).Trim()
}

function Confirm-Question {
    <# Confirm-Action, avec la question écrite en clair quand l'entrée est un tube (voir Read-Reponse). #>
    param([string]$Question, [string]$MotExact = '', [switch]$Oui)
    if (-not $Oui -and (Test-EntreeRedirigee)) {
        if ($MotExact) { Write-LignesCoupees -Prefixe '   ' -Texte ($Question + ' Tape ' + $MotExact + ' pour confirmer :') -Couleur White }
        else { Write-LignesCoupees -Prefixe '   ' -Texte ($Question + ' [O/N] :') -Couleur White }
    }
    return (Confirm-Action -Question $Question -MotExact $MotExact -Oui:$Oui)
}

function ConvertTo-ChoixNumerotes {
    <#
        « 1, 3 » -> les valeurs n° 1 et n° 3 de -Valeurs, sans doublon. Rend un tableau vide si rien n'est tapé,
        $null si un numéro n'est pas dans la liste.
    #>
    param([string]$Texte = '', [string[]]$Valeurs = @())
    $liste = New-Object System.Collections.ArrayList
    foreach ($morceau in @(([string]$Texte) -split '[,;\s]+')) {
        if (-not $morceau) { continue }
        $numero = 0
        if (-not [int]::TryParse($morceau, [ref]$numero)) { return $null }
        if ($numero -lt 1 -or $numero -gt $Valeurs.Count) { return $null }
        if (-not $liste.Contains($Valeurs[$numero - 1])) { [void]$liste.Add($Valeurs[$numero - 1]) }
    }
    return , ([string[]]$liste.ToArray())
}

# ---------------------------------------------------------------------------
# État du partage lu sur le site
# ---------------------------------------------------------------------------

function Get-Textes {
    <# Champ « liste » d'une réponse du site -> tableau de textes non vides, sans doublon (toujours un tableau). #>
    param($Objet, [string]$Nom)
    $liste = New-Object System.Collections.ArrayList
    if ($null -ne $Objet) {
        $propriete = $Objet.PSObject.Properties[$Nom]
        if ($null -ne $propriete -and $null -ne $propriete.Value) {
            foreach ($element in @($propriete.Value)) {
                $texte = [string]$element
                if ($texte -and $texte.Length -le 80 -and -not $liste.Contains($texte) -and $liste.Count -lt 32) { [void]$liste.Add($texte) }
            }
        }
    }
    return , ([string[]]$liste.ToArray())
}

function ConvertTo-Partage {
    <#
        Bloc « sharing » rendu par le site -> objet simple. $null si le bloc est absent ou sans empreinte :
        sans empreinte lue, aucun réglage ne part.
    #>
    param($Bloc)
    if ($null -eq $Bloc -or $Bloc -is [string] -or $Bloc -is [System.Array]) { return $null }
    $revision = [string](Get-ChampGestion $Bloc 'revision' '')
    if ($revision -cnotmatch '^[0-9a-f]{16}$') { return $null }
    $admission = Get-ChampGestion $Bloc 'admission'
    return [pscustomobject]@{
        Mode            = [string](Get-ChampGestion $Bloc 'mode' '')
        Publics         = (Get-Textes $Bloc 'audiences')
        Outils          = (Get-Textes $Bloc 'tools')
        Partageables    = (Get-Textes $Bloc 'shareable_tools')
        Libelles        = (Get-ChampGestion $Bloc 'tool_labels')
        Auto            = ((Get-ChampGestion $Bloc 'auto_accept_jobs' $false) -eq $true)
        Comptes         = (ConvertTo-EntierGestion (Get-ChampGestion $Bloc 'users_count'))
        MembresChoisis  = ([string](Get-ChampGestion $Bloc 'team_scope' '') -eq 'members')
        NombreMembres   = (ConvertTo-EntierGestion (Get-ChampGestion $Bloc 'team_members_count'))
        AEquipe         = ((Get-ChampGestion $Bloc 'has_team' $false) -eq $true)
        AdmissionOk     = ((Get-ChampGestion $admission 'allowed' $false) -eq $true)
        AdmissionMotif  = ([string](Get-ChampGestion $admission 'refusal' '')).Trim()
        Revision        = $revision
    }
}

function Get-NomMode {
    param([string]$Valeur)
    switch ($Valeur) {
        'prive'   { return 'Privé' }
        'comptes' { return 'Comptes choisis' }
        'public'  { return 'Public' }
    }
    return 'inconnu'
}

function Format-Publics {
    param([string[]]$Liste = @())
    $noms = @()
    foreach ($id in @($Liste)) {
        if (-not $id) { continue }
        if ($script:NomsPublics.ContainsKey($id)) { $noms += [string]$script:NomsPublics[$id] } else { $noms += $id }
    }
    if ($noms.Count -eq 0) { return 'aucun' }
    return ($noms -join ', ')
}

function Get-NomOutil {
    <# Nom d'un outil tel que le site le donne (tool_labels), sinon son identifiant. #>
    param([string]$Outil, $Libelles)
    $nom = (([string](Get-ChampGestion $Libelles $Outil '')) -replace '[\x00-\x1f]', ' ').Trim()
    if (-not $nom) { return $Outil }
    if ($nom.Length -gt 60) { $nom = $nom.Substring(0, 60) }
    return $nom
}

function Format-Outils {
    param([string[]]$Liste = @(), $Libelles = $null)
    $noms = @()
    foreach ($outil in @($Liste)) { if ($outil) { $noms += (Get-NomOutil $outil $Libelles) } }
    if ($noms.Count -eq 0) { return 'aucun' }
    return ($noms -join ', ')
}

function Get-TexteAuto {
    param([bool]$Auto)
    if ($Auto) { return 'acceptés automatiquement' }
    return 'soumis à ton accord (Mes Workers > Demandes de jobs)'
}

function Test-MemeListe {
    param([string[]]$A = @(), [string[]]$B = @())
    $gauche = @($A | Sort-Object -Unique)
    $droite = @($B | Sort-Object -Unique)
    return (($gauche -join '|') -ceq ($droite -join '|'))
}

function Write-TitreEcran {
    <# Titre d'un bloc de l'écran, écrit tel quel (Write-Section le passerait tout en capitales). #>
    param([string]$Titre)
    Write-Host ''
    Write-Host ('  ' + $Titre) -ForegroundColor White
    Write-Host ('  ' + ('-' * [Math]::Min(70, [Math]::Max(8, $Titre.Length)))) -ForegroundColor DarkGray
}

function Show-Partage {
    param($Partage, $Date = $null, [string]$Titre = 'PARTAGE ACTUEL')
    if ($null -eq $Date) { $Date = Get-Date }
    Write-TitreEcran ($Titre + ' (lu sur le site le ' + (Format-DateEtat -Date $Date) + ')')
    Write-Etat -Libelle 'Mode' -Valeur (Get-NomMode $Partage.Mode) -Niveau info
    Write-Etat -Libelle 'Publics ouverts' -Valeur (Format-Publics $Partage.Publics) -Niveau info
    # Sans public ouvert, aucun outil n'est ouvert à un public, quelle que soit la liste gardée par le site.
    $outilsOuverts = 'aucun'
    if (@($Partage.Publics).Count -gt 0) { $outilsOuverts = Format-Outils $Partage.Outils $Partage.Libelles }
    Write-Etat -Libelle 'Outils ouverts à ces publics' -Valeur $outilsOuverts -Niveau info
    Write-Etat -Libelle 'Comptes choisis' -Valeur ([string]$Partage.Comptes + ' compte(s) ont accepté une invitation') -Niveau info
    Write-Etat -Libelle 'Jobs des autres comptes' -Valeur (Get-TexteAuto $Partage.Auto) -Niveau info
    $equipe = 'toute l''équipe'
    if (-not $Partage.AEquipe) { $equipe = 'pas d''équipe' }
    elseif ($Partage.MembresChoisis) { $equipe = [string]$Partage.NombreMembres + ' membre(s) choisi(s)' }
    # Sans le public « mon équipe », personne de l'équipe n'a d'accès par ce public : la ligne le dit.
    elseif ($Partage.Publics -notcontains 'team') { $equipe = 'toute l''équipe   (public « mon équipe » fermé)' }
    Write-Etat -Libelle 'Mon équipe' -Valeur $equipe -Niveau info
    if ($Partage.AdmissionOk) { Write-Etat -Libelle 'Admission du Worker' -Valeur 'accordée' -Niveau ok }
    elseif ($Partage.AdmissionMotif) { Write-Etat -Libelle 'Admission du Worker' -Valeur ('refusée : ' + $Partage.AdmissionMotif) -Niveau alerte }
    else { Write-Etat -Libelle 'Admission du Worker' -Valeur 'refusée' -Niveau alerte }
}

function Show-Menu {
    param($Partage)
    Write-TitreEcran 'QUE VEUX-TU FAIRE ?'
    Write-Host '   1. Privé : toi seul'
    Write-Host ('   2. Comptes choisis : seulement les comptes déjà invités et qui ont accepté (' + $Partage.Comptes + ')')
    Write-Host '   3. Public : ouvrir à un ou plusieurs publics'
    # Comme la case du site : l'acceptation automatique ne se règle que sur un Worker public.
    if ($Partage.Mode -eq 'public') {
        $sens = 'activer'
        if ($Partage.Auto) { $sens = 'couper' }
        Write-Host ('   4. Accepter automatiquement les jobs : ' + $sens)
    }
    Write-Host '   5. Ouvrir Mes Workers dans le navigateur'
    Write-Host '   0. Ne rien changer'
    Write-Host ''
    Write-Info $TexteResteSite
}

function Read-ChoixMenu {
    <# Numéro choisi dans le menu ; 0 si rien n'est tapé ; -1 après trois réponses hors liste. #>
    param($Partage)
    $permis = @('0', '1', '2', '3')
    if ($Partage.Mode -eq 'public') { $permis += '4' }
    $permis += '5'
    for ($essai = 1; $essai -le 3; $essai++) {
        $reponse = Read-Reponse 'Ton choix'
        if (-not $reponse) { return 0 }
        if ($permis -contains $reponse) { return [int]$reponse }
        Write-Alerte ('Choix non reconnu : tape un des numéros ' + ($permis -join ', ') + '.')
    }
    return -1
}

function Read-ReglagePublic {
    <# Les trois questions du choix « Public ». Rend Publics, Outils et Auto, ou $null si une réponse manque (la raison est déjà écrite). #>
    param($Partage)
    Write-Host ''
    Write-LignesCoupees -Prefixe '   ' -Texte 'Publics à ouvrir (numéros séparés par une virgule) : 1 = mon équipe   2 = comptes qui partagent aussi un Worker   3 = tous les comptes connectés' -Couleur Gray
    $choisis = ConvertTo-ChoixNumerotes -Texte (Read-Reponse 'Publics') -Valeurs $script:PublicsConnus
    if ($null -eq $choisis) { Write-Alerte 'Réponse non comprise : il faut des numéros de la liste, séparés par une virgule. Rien n''a été changé.'; return $null }
    if ($choisis.Count -eq 0) { Write-Info 'Aucun public choisi : rien n''a été changé.'; return $null }

    $partageables = @($Partage.Partageables)
    if ($partageables.Count -eq 0) { Write-Alerte 'Le site ne propose aucun outil de ce Worker à ouvrir à un public : rien n''a été changé.'; return $null }
    Write-Host ''
    Write-Host '   Outils à ouvrir à ces publics (numéros séparés par une virgule) :'
    for ($i = 0; $i -lt $partageables.Count; $i++) { Write-Host ('      ' + ($i + 1) + ' = ' + (Get-NomOutil $partageables[$i] $Partage.Libelles)) }
    Write-Info 'Jamais l''imprimante, la graveuse, un équipement ni un outil système.'
    # Le réglage précédent est seulement PROPOSÉ : il n'est jamais repris sans que la question soit posée.
    $precedents = [string[]]@($Partage.Outils | Where-Object { $partageables -contains $_ })
    if ($precedents.Count -gt 0) { Write-Info ('Entrée seule = garder : ' + (Format-Outils $precedents $Partage.Libelles)) }
    $outilsChoisis = ConvertTo-ChoixNumerotes -Texte (Read-Reponse 'Outils') -Valeurs $partageables
    if ($null -eq $outilsChoisis) { Write-Alerte 'Réponse non comprise : il faut des numéros de la liste, séparés par une virgule. Rien n''a été changé.'; return $null }
    if ($outilsChoisis.Count -eq 0) {
        if ($precedents.Count -eq 0) { Write-Info 'Aucun outil choisi : rien n''a été changé.'; return $null }
        $outilsChoisis = $precedents
    }

    Write-Host ''
    $reponse = Read-Reponse 'Accepter automatiquement leurs jobs ? [O/N]   (N = chaque calcul attend ton accord)'
    return [pscustomobject]@{ Publics = [string[]]$choisis; Outils = [string[]]$outilsChoisis; Auto = [bool]($reponse -match '^(?i:o|oui|y|yes)$') }
}

function Open-MesWorkers {
    <#
        Ouvre Mes Workers dans le navigateur. L'adresse vient de la configuration du Worker (jamais d'une
        saisie) et n'est ouverte que si c'est une adresse absolue http ou https. En simulation, elle est
        seulement montrée.
    #>
    param([string]$Racine, [switch]$Simulation)
    $adresse = ([string](Get-ConfigSure -Racine $Racine).ServeurUrl).Trim()
    $uri = $null
    if (-not [System.Uri]::TryCreate($adresse, [System.UriKind]::Absolute, [ref]$uri) -or (@('http', 'https') -notcontains [string]$uri.Scheme) -or -not $uri.Host) {
        Write-Alerte 'L''adresse du site n''est pas connue de ce Worker : ouvre le site toi-même, puis Mes Workers.'
        return $false
    }
    $cible = [string]$uri.Scheme + '://' + [string]$uri.Authority + ([string]$uri.AbsolutePath).TrimEnd('/') + '/#workers'
    if ($Simulation) { Write-Info ('[simulation] Adresse qui serait ouverte : ' + $cible); return $true }
    try { Start-Process -FilePath $cible | Out-Null } catch { Write-Alerte ('Le navigateur n''a pas pu être ouvert. Adresse de Mes Workers : ' + $cible); return $false }
    Write-Ok ('Mes Workers ouvert dans le navigateur : ' + $cible)
    return $true
}

# ---------------------------------------------------------------------------
# Refus
# ---------------------------------------------------------------------------

function Write-RefusAdmission {
    param([string]$Motif)
    if (-not $Motif) { $Motif = 'le site n''en donne pas la raison.' }
    Write-Alerte ('Partage impossible pour l''instant : ' + $Motif)
    if ($Motif -match 'Admission globale') { Write-Conseil 'L''admission se demande sur le site ; elle ne se règle jamais depuis ce PC.' }
}

function Write-RefusPartage {
    <#
        Message d'un réglage refusé ou non abouti (un seul, toujours le même pour une même cause).
        Rend le code de sortie : 2 quand le site a refusé ce réglage-là, 1 pour une panne ou un réglage
        impossible (agent, site, compte propriétaire, maintenance).
    #>
    param($Resultat)
    $http = [int]$Resultat.Http
    $codeSite = [string]$Resultat.CodeSite
    $motif = ([string]$Resultat.Erreur).Trim()
    if ($Resultat.AgentMuet -or $Resultat.AgentARedemarrer -or $Resultat.AgentTropAncien -or $Resultat.SiteSansFonction -or $Resultat.SiteInjoignable -or $http -eq 429) {
        Write-RefusRelais -Resultat $Resultat
        return 1
    }
    switch ($codeSite) {
        'WORKER_SECURITY'       { Write-RefusAdmission -Motif $motif; return 2 }
        'CONFIRMATION_REQUIRED' { Write-Alerte 'Confirmation « OUVRIR A TOUS » manquante : rien n''a changé.'; return 2 }
        'SELF_SERVICE_FIELD'    { Write-Alerte 'Réglage refusé par le site (champ non autorisé) : mets le Worker à jour.'; return 2 }
        'SHARING_CHANGED'       { Write-Alerte 'Le partage a été modifié sur le site pendant que tu choisissais : voici le nouvel état. Rien n''a changé.'; return 2 }
        'WORKER_OWNER_DISABLED' { Write-Alerte 'Le compte propriétaire ne peut plus agir sur le site : le partage ne se règle pas d''ici.'; return 1 }
        'SITE_MAINTENANCE'      { Write-Alerte 'Site en maintenance : réessaie plus tard.'; return 1 }
    }
    # Équipe absente ou désactivée, outils non partageables, aucun outil : le message du site, tel quel.
    if ($motif -and ($codeSite -eq 'TEAM_REQUIRED' -or $codeSite -eq 'TEAMS_DISABLED' -or $http -eq 400)) { Write-Alerte $motif; return 2 }
    Write-RefusRelais -Resultat $Resultat
    return 1
}

function Write-AvantApres {
    param([string]$Libelle, [string]$Avant, [string]$Apres)
    if ($Avant -ceq $Apres) { Write-Etat -Libelle $Libelle -Valeur ($Avant + '   (inchangé)') -Niveau info }
    else { Write-Etat -Libelle $Libelle -Valeur ($Avant + '  ' + $script:Fleche + '  ' + $Apres) -Niveau alerte }
}

# ---------------------------------------------------------------------------
# Déroulement
# ---------------------------------------------------------------------------

try {
    Write-Explication -Titre 'MODE DE PARTAGE DU WORKER' -Fait @(
        'Lit sur le site le partage de CE Worker et l''affiche.',
        'Permet de le passer en privé, en comptes choisis ou en public, et de régler l''acceptation automatique des jobs.',
        'Le site refait tous les contrôles et prévient le compte propriétaire à chaque réglage.'
    ) -NeFaitPas @(
        'Il ne règle rien hors ligne : sans réponse du site, rien ne change.',
        'Il n''invite et ne retire aucun compte, ne règle pas les outils et modèles par compte, ne choisit pas de membres d''équipe : cela reste sur le site.',
        'Il ne donne jamais l''admission du Worker et n''ouvre jamais l''imprimante, la graveuse ni un équipement.'
    ) -Exige @(
        'Le Worker allumé, associé à ton compte, et le site joignable. Aucune fenêtre administrateur.'
    )

    # --- Paramètres du pilotage sans clavier : contrôlés avant toute demande à l'agent.
    $modeVoulu = ([string]$Mode).Trim().ToLowerInvariant()
    $autoVoulu = ([string]$AutoAccepter).Trim().ToLowerInvariant()
    $publicsVoulus = [string[]]@()
    $refus = ''
    if ($modeVoulu -and (@('prive', 'comptes', 'public') -cnotcontains $modeVoulu)) { $refus = 'Mode inconnu (-Mode) : prive, comptes ou public.' }
    elseif ($autoVoulu -and (@('oui', 'non') -cnotcontains $autoVoulu)) { $refus = 'Valeur inconnue (-AutoAccepter) : oui ou non.' }
    elseif ($modeVoulu -ne 'public' -and (([string]$Publics).Trim() -or ([string]$Outils).Trim())) { $refus = '-Publics et -Outils ne s''emploient qu''avec -Mode public.' }
    elseif ($autoVoulu -and $modeVoulu -and $modeVoulu -ne 'public') { $refus = '-AutoAccepter ne s''emploie qu''avec -Mode public, ou seul sur un Worker déjà public.' }
    elseif ($modeVoulu -eq 'public') {
        if (-not ([string]$Publics).Trim()) { $refus = 'Choisis au moins un public (-Publics).' }
        elseif (-not ([string]$Outils).Trim()) { $refus = 'Choisis au moins un outil (-Outils).' }
        else {
            try { $publicsVoulus = ConvertTo-ListeChoix -Texte (([string]$Publics) -replace '[\s;]+', ',') -Permis $script:PublicsConnus }
            catch { $refus = 'Publics (-Publics) : ' + [string]$_.Exception.Message }
            if (-not $refus -and $publicsVoulus.Count -eq 0) { $refus = 'Choisis au moins un public (-Publics).' }
        }
    }
    if ($refus) {
        Write-Alerte $refus
        Write-Info 'Rien n''a été demandé au site.'
        Exit-Outil -Code 2 -SansPause:$SansPause
    }

    # --- Lecture du partage sur le site : aucun réglage hors ligne.
    Write-Info 'Lecture du partage sur le site, par l''agent...'
    $lecture = Get-EtatSite -Racine $Racine -Rafraichir
    if (-not $lecture.Connu) {
        Write-RefusRelais -Resultat $lecture
        Write-Info 'Le partage ne se règle pas hors ligne : rien n''a été changé.'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    $partage = ConvertTo-Partage (Get-ChampGestion $lecture.Site 'sharing')
    if ($null -eq $partage) {
        Write-Alerte 'Le site n''a pas donné l''état du partage de ce Worker : rien ne peut être réglé d''ici.'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    Show-Partage -Partage $partage -Date $lecture.DateLecture
    $compte = Get-ChampGestion $lecture.Site 'account'
    if ((Get-ChampGestion $compte 'can_act' $true) -eq $false) {
        Write-Host ''
        Write-Alerte 'Le compte propriétaire ne peut plus agir sur le site : le partage ne se règle pas d''ici.'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    if ((Get-ChampGestion $compte 'maintenance' $false) -eq $true) {
        Write-Host ''
        Write-Alerte 'Site en maintenance : réessaie plus tard.'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }

    # --- Choix : paramètres, ou menu.
    $choix = 0
    if ($modeVoulu -eq 'prive') { $choix = 1 }
    elseif ($modeVoulu -eq 'comptes') { $choix = 2 }
    elseif ($modeVoulu -eq 'public') { $choix = 3 }
    elseif ($autoVoulu) { $choix = 4 }
    $direct = ($choix -ne 0)
    if (-not $direct) {
        if ($Oui -and $SansPause) {
            # Pilotage sans clavier, aucun réglage demandé : l'état seulement.
            Exit-Outil -Code 0 -SansPause:$SansPause
        }
        Show-Menu -Partage $partage
        $choix = Read-ChoixMenu -Partage $partage
    }
    if ($choix -lt 0) {
        Write-Info 'Rien n''a été changé.'
        Exit-Outil -Code 2 -SansPause:$SansPause
    }
    if ($choix -eq 0) {
        Write-Info 'Rien n''a été changé.'
        Exit-Outil -Code 0 -SansPause:$SansPause
    }
    if ($choix -eq 5) {
        Write-Host ''
        $ouvert = Open-MesWorkers -Racine $Racine -Simulation:$Simulation
        Write-Info 'Rien n''a été changé depuis ce PC.'
        if ($ouvert) { Exit-Outil -Code 0 -SansPause:$SansPause }
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    if ($choix -eq 4 -and $partage.Mode -ne 'public') {
        Write-Host ''
        Write-Alerte 'L''acceptation automatique des jobs ne se règle que sur un Worker en mode Public : rien n''a été changé.'
        Exit-Outil -Code 2 -SansPause:$SansPause
    }
    if (($choix -eq 2 -or $choix -eq 3) -and -not $partage.AdmissionOk) {
        # Le site refuserait ce réglage (admission exigée pour tout partage) : inutile de poser les questions.
        Write-Host ''
        Write-RefusAdmission -Motif $partage.AdmissionMotif
        Write-Info 'Rien n''a été demandé au site.'
        Exit-Outil -Code 2 -SansPause:$SansPause
    }

    # --- Réglage demandé (état APRÈS l'opération).
    $apres = [pscustomobject]@{ Mode = [string]$partage.Mode; Publics = [string[]]@($partage.Publics); Outils = [string[]]@($partage.Outils); Auto = [bool]$partage.Auto }
    switch ($choix) {
        1 { $apres.Mode = 'prive'; $apres.Publics = [string[]]@(); $apres.Outils = [string[]]@() }
        2 { $apres.Mode = 'comptes'; $apres.Publics = [string[]]@(); $apres.Outils = [string[]]@() }
        3 {
            $reglage = $null
            if ($direct) {
                $partageables = @($partage.Partageables)
                $outilsVoulus = [string[]]@()
                $refus = ''
                if ($partageables.Count -eq 0) { $refus = 'Le site ne propose aucun outil de ce Worker à ouvrir à un public.' }
                else {
                    try { $outilsVoulus = ConvertTo-ListeChoix -Texte (([string]$Outils) -replace '[\s;]+', ',') -Permis $partageables }
                    catch { $refus = 'Outils (-Outils) : ' + [string]$_.Exception.Message }
                    if (-not $refus -and $outilsVoulus.Count -eq 0) { $refus = 'Choisis au moins un outil (-Outils).' }
                }
                if ($refus) {
                    Write-Host ''
                    Write-Alerte $refus
                    Write-Info 'Rien n''a été demandé au site.'
                    Exit-Outil -Code 2 -SansPause:$SansPause
                }
                # Sans -AutoAccepter, l'acceptation automatique reste celle du site.
                $autoDemande = [bool]$partage.Auto
                if ($autoVoulu) { $autoDemande = ($autoVoulu -eq 'oui') }
                $reglage = [pscustomobject]@{ Publics = [string[]]$publicsVoulus; Outils = [string[]]$outilsVoulus; Auto = $autoDemande }
            }
            else { $reglage = Read-ReglagePublic -Partage $partage }
            if ($null -eq $reglage) { Exit-Outil -Code 2 -SansPause:$SansPause }
            $apres.Mode = 'public'
            $apres.Publics = [string[]]@($script:PublicsConnus | Where-Object { $reglage.Publics -contains $_ })
            $apres.Outils = [string[]]@($reglage.Outils)
            $apres.Auto = [bool]$reglage.Auto
        }
        4 {
            if ($direct) { $apres.Auto = ($autoVoulu -eq 'oui') } else { $apres.Auto = (-not $partage.Auto) }
        }
    }

    $dejaEnPlace = $false
    switch ($choix) {
        1 { $dejaEnPlace = ($partage.Mode -eq 'prive') }
        2 { $dejaEnPlace = ($partage.Mode -eq 'comptes') }
        3 { $dejaEnPlace = ($partage.Mode -eq 'public' -and (Test-MemeListe $partage.Publics $apres.Publics) -and (Test-MemeListe $partage.Outils $apres.Outils) -and ($partage.Auto -eq $apres.Auto)) }
        4 { $dejaEnPlace = ($partage.Auto -eq $apres.Auto) }
    }
    if ($dejaEnPlace) {
        Write-Host ''
        Write-Ok 'Ce réglage est déjà en place : rien n''a été envoyé au site.'
        Exit-Outil -Code 0 -SansPause:$SansPause
    }
    if ($choix -eq 2 -and $partage.Comptes -eq 0) {
        Write-Host ''
        Write-Alerte 'Aucun compte n''a encore accepté d''invitation : le partage sera actif mais personne n''y aura accès tant que tu n''auras invité personne depuis le site.'
        if (-not $Simulation -and -not (Confirm-Question -Question 'Continuer quand même ?' -Oui:$Oui)) {
            Write-Info 'Annulé : rien n''a été changé.'
            Exit-Outil -Code 2 -SansPause:$SansPause
        }
    }

    # --- Récapitulatif « Avant -> Après », avertissements, confirmations.
    Write-TitreEcran ('AVANT ' + $script:Fleche + ' APRÈS')
    $outilsAvant = 'aucun'
    if (@($partage.Publics).Count -gt 0) { $outilsAvant = Format-Outils $partage.Outils $partage.Libelles }
    $outilsApres = 'aucun'
    if (@($apres.Publics).Count -gt 0) { $outilsApres = Format-Outils $apres.Outils $partage.Libelles }
    Write-AvantApres -Libelle 'Mode' -Avant (Get-NomMode $partage.Mode) -Apres (Get-NomMode $apres.Mode)
    Write-AvantApres -Libelle 'Publics ouverts' -Avant (Format-Publics $partage.Publics) -Apres (Format-Publics $apres.Publics)
    Write-AvantApres -Libelle 'Outils ouverts à ces publics' -Avant $outilsAvant -Apres $outilsApres
    Write-AvantApres -Libelle 'Jobs des autres comptes' -Avant (Get-TexteAuto $partage.Auto) -Apres (Get-TexteAuto $apres.Auto)

    if ($choix -ne 4 -and $partage.MembresChoisis -and ($apres.Publics -notcontains 'team')) {
        Write-Host ''
        Write-Alerte ('Le choix de ' + $partage.NombreMembres + ' membre(s) de ton équipe sera effacé (règle du site) : il faudra le refaire sur le site, dans « Mon équipe », si tu rouvres à l''équipe.')
    }

    # « Tous les comptes » + acceptation automatique : jugé sur l'état APRÈS l'opération, confirmation à part.
    $tousAvant = (($partage.Publics -contains 'everyone') -and [bool]$partage.Auto)
    $tousApres = (($apres.Publics -contains 'everyone') -and [bool]$apres.Auto)
    $ouvrirATous = ($tousApres -and -not $tousAvant)

    if ($Simulation) {
        Write-Host ''
        if ($ouvrirATous) { Write-Info ('[simulation] Ce réglage exigerait le mot ' + $MotOuvrirATous + ' : tout compte connecté pourrait lancer un calcul sur ce PC sans ton accord.') }
        Write-Info '[simulation] Ce réglage n''a pas été envoyé au site : rien n''a changé.'
        Exit-Outil -Code 0 -SansPause:$SansPause
    }
    if ($ouvrirATous -and -not $ConfirmerTous) {
        Write-Host ''
        # Jamais levée par -Oui : le mot exact, ou -ConfirmerTous.
        if (-not (Confirm-Question -Question 'Tout compte connecté pourra lancer un calcul sur ce PC sans ton accord.' -MotExact $MotOuvrirATous)) {
            Write-Info ('Confirmation « ' + $MotOuvrirATous + ' » non donnée : rien n''a été changé.')
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'ouverture à tous avec acceptation automatique : confirmation non donnée, rien envoyé'
            Exit-Outil -Code 2 -SansPause:$SansPause
        }
    }
    Write-Host ''
    if (-not (Confirm-Question -Question 'Enregistrer ce réglage ?' -Oui:$Oui)) {
        Write-Info 'Annulé : rien n''a été changé.'
        Exit-Outil -Code 2 -SansPause:$SansPause
    }

    # --- Envoi : liste blanche stricte, empreinte « expected » de la lecture faite au début.
    $parametres = @{}
    $resume = ''
    switch ($choix) {
        1 { $parametres = @{ enabled = $false; expected = [string]$partage.Revision }; $resume = 'privé' }
        2 { $parametres = @{ enabled = $true; audiences = @(); expected = [string]$partage.Revision }; $resume = 'comptes choisis' }
        3 {
            $parametres = @{ enabled = $true; audiences = @($apres.Publics); tools = @($apres.Outils); expected = [string]$partage.Revision; auto_accept_jobs = [bool]$apres.Auto }
            $resume = 'public (' + ($apres.Publics -join ', ') + ' ; outils ' + ($apres.Outils -join ', ') + ')'
        }
        4 { $parametres = @{ auto_accept_jobs = [bool]$apres.Auto } }
    }
    if ($choix -eq 3 -or $choix -eq 4) {
        if ($apres.Auto) { $resume = ($resume + ' acceptation automatique activée').Trim() } else { $resume = ($resume + ' acceptation automatique coupée').Trim() }
    }
    if ($ouvrirATous) { $parametres['confirm_everyone_auto'] = $true }

    Write-Info 'Réglage transmis au site par l''agent...'
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('réglage du partage demandé : ' + $resume)
    $relais = Invoke-RelaisSite -Racine $Racine -Action 'sharing-set' -Arguments $parametres -DelaiSecondes 60

    if (-not $relais.Ok) {
        Write-Host ''
        $codeSortie = [int](@(Write-RefusPartage -Resultat $relais) | Select-Object -Last 1)
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('réglage du partage non enregistré (erreur ' + [int]$relais.Http + ' ' + [string]$relais.CodeSite + ')')
        if ([string]$relais.CodeSite -eq 'SHARING_CHANGED') {
            $relecture = Get-EtatSite -Racine $Racine -Rafraichir
            $actuel = $null
            if ($relecture.Connu) { $actuel = ConvertTo-Partage (Get-ChampGestion $relecture.Site 'sharing') }
            if ($null -ne $actuel) { Show-Partage -Partage $actuel -Date $relecture.DateLecture }
            else { Write-Alerte 'Le nouvel état n''a pas pu être relu : relance cet outil.' }
        }
        Exit-Outil -Code $codeSortie -SansPause:$SansPause
    }

    Write-Host ''
    if ((Get-ChampGestion $relais.Resultat 'changed' $true) -eq $false) { Write-Ok 'Le site avait déjà ce réglage : rien n''a changé.' }
    else { Write-Ok 'Réglage enregistré par le site.' }
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('réglage du partage enregistré : ' + $resume)
    $relecture = Get-EtatSite -Racine $Racine -Rafraichir
    $nouveau = $null
    $dateNouveau = $null
    if ($relecture.Connu) { $nouveau = ConvertTo-Partage (Get-ChampGestion $relecture.Site 'sharing'); $dateNouveau = $relecture.DateLecture }
    # Relecture impossible : l'état rendu par le site avec sa réponse fait foi.
    if ($null -eq $nouveau) { $nouveau = ConvertTo-Partage (Get-ChampGestion $relais.Resultat 'sharing') }
    if ($null -ne $nouveau) { Show-Partage -Partage $nouveau -Date $dateNouveau -Titre 'NOUVEAU PARTAGE' }
    else { Write-Alerte 'Le nouvel état n''a pas pu être relu : vérifie-le dans Mes Workers.' }
    Write-Host ''
    Write-Info 'Le compte propriétaire reçoit une notification sur le site.'
    Exit-Outil -Code 0 -SansPause:$SansPause
} catch {
    Write-Erreur ('Erreur inattendue : ' + $_.Exception.Message)
    if (-not $Simulation) { try { Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('erreur inattendue : ' + $_.Exception.Message) } catch { } }
    Exit-Outil -Code 1 -SansPause:$SansPause
}
