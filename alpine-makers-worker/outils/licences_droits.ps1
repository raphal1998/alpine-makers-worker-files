<#
    Licences et droits de ce Worker (touche K du menu, capacité legal_status_v1).

    Pour le compte connecté (session du menu, ou celle transmise par worker.exe), l'état juridique de chaque moteur,
    module et modèle installé sur ce Worker, puis des moteurs et modules qu'il peut encore installer : mêmes états que
    la page « Licences et justificatifs » du site (autorisation obtenue, documenté, conditions acceptées, déclaration
    utilisateur, demande en cours, restriction connue, à vérifier, conditions à accepter), ce qu'il faut faire et un
    résumé chiffré par état.

    Demandé au site par l'agent (action locale « legal-status », route signée) : la session part dans un fichier
    d'arguments lu puis effacé par l'agent, jamais sur une ligne de commande. Lecture seule : accepter des conditions ou
    déclarer ses droits se fait sur le site (adresses affichées). Session exigée ; co-propriétaire : droit « status ».

    Usage : licences_droits.ps1 [-Racine <dossier du Worker>] [-SansPause]
    Codes de sortie : 0 lu ; 1 refusé ou illisible (pas de session, droit manquant, agent ou site injoignable).
#>
param([string]$Racine = '', [switch]$SansPause)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'commun_gestion.ps1')
Initialize-ConsoleWorker -Titre 'Licences et droits du Worker'

# Mêmes états, dans le même ordre, que les pastilles de Mes Workers et de worker.exe (legal_compliance.STATE_LABELS).
$script:OrdreEtats = @('authorization_obtained', 'documented', 'notices_accepted', 'user_declaration', 'request_pending', 'known_restriction', 'to_verify', 'action')
$script:LibellesEtats = @{
    authorization_obtained = 'autorisation obtenue'; documented = 'documenté'; notices_accepted = 'conditions acceptées'
    user_declaration = 'déclaration utilisateur'; request_pending = 'demande en cours'; known_restriction = 'restriction connue'
    to_verify = 'à vérifier'; action = 'conditions à accepter'
}
$script:CouleursEtats = @{
    authorization_obtained = 'Green'; documented = 'DarkGreen'; notices_accepted = 'DarkCyan'; user_declaration = 'DarkYellow'
    request_pending = 'Magenta'; known_restriction = 'Red'; to_verify = 'Yellow'; action = 'Cyan'
}

function Exit-Licences {
    param([int]$Code = 0)
    Wait-FinOutil -SansPause:$SansPause
    exit $Code
}

function Get-LienLicences {
    <# Adresse complète d'une page des licences du site ; '' pour tout autre lien (jamais une adresse externe). #>
    param([string]$Serveur, [string]$Chemin)
    if (-not $Chemin -or $Chemin -notmatch '^/(legal|licences-personnelles)(#[A-Za-z0-9._:+@()-]{1,120}|\?component=[A-Za-z0-9._~%-]{1,360})?$') { return '' }
    if (-not $Serveur -or $Serveur -notmatch '^https?:[/][/][^/\s]+') { return $Chemin }
    return ($Serveur.TrimEnd('/') + $Chemin)
}

function Get-MessageLicences {
    <# Raison lisible d'un refus rendu par l'agent ou le site. #>
    param($Reponse)
    $code = [string](Get-ChampGestion $Reponse 'code' '')
    $erreur = [string](Get-ChampGestion $Reponse 'error' '')
    if ($code -in @('WORKER_EXE_SESSION_INVALID', 'WORKER_EXE_SESSION_REQUIRED')) { return 'session expirée ou absente : reconnecte-toi (C dans le menu).' }
    if ($code -eq 'CO_OWNER_RIGHT_MISSING') { return 'ce compte n''a pas le droit « état, journaux et diagnostic ».' }
    if ([bool](Get-ChampGestion $Reponse 'update_required' $false)) { return 'mets le Worker à jour (23) pour voir ses licences et droits.' }
    if ([bool](Get-ChampGestion $Reponse 'site_missing' $false)) { return 'le Dashboard n''a pas encore cette fonction : il doit être redémarré dans sa nouvelle version.' }
    if ([bool](Get-ChampGestion $Reponse 'unreachable' $false)) { return ('site injoignable : ' + $(if ($erreur) { $erreur } else { 'réessaie dans un instant.' })) }
    if ($erreur) { return $erreur }
    return 'réponse illisible.'
}

function Read-LicencesWorker {
    <# Réponse de l'action locale « legal-status » : Ok, Donnees, Erreur. La session part par fichier d'arguments. #>
    param([string]$Racine, [int]$DelaiSecondes = 40)
    $r = [pscustomobject]@{ Ok = $false; Donnees = $null; Erreur = '' }
    $fichier = ''
    try { $fichier = New-FichierSessionWorker -Racine $Racine } catch { $r.Erreur = [string]$_.Exception.Message; return $r }
    if (-not $fichier) { $r.Erreur = 'connexion du compte requise (C dans le menu).'; return $r }
    try { $reponse = Invoke-ActionLocale -Racine $Racine -Action 'legal-status' -DelaiSecondes $DelaiSecondes -Arguments @{ args_file = $fichier } }
    finally { Remove-FichierArgumentsWorker -Racine $Racine -Jeton $fichier }
    if ($reponse.NonSupporte) { $r.Erreur = 'agent trop ancien pour les licences et droits : mets le Worker à jour (23).'; return $r }
    if ($reponse.DelaiDepasse) { $r.Erreur = 'l''agent ne répond pas : rallume-le (12) ou réessaie.'; return $r }
    if (-not $reponse.Ok) { $r.Erreur = [string]$reponse.Erreur; return $r }
    if (-not [bool](Get-ChampGestion $reponse.Resultat 'ok' $false)) { $r.Erreur = Get-MessageLicences $reponse.Resultat; return $r }
    $r.Ok = $true; $r.Donnees = $reponse.Resultat
    return $r
}

function Write-PastillesLicences {
    <# Les huit états et leur nombre, quatre par ligne ; un état à zéro reste gris. #>
    param($Resume)
    $etats = Get-ChampGestion $Resume 'states'
    for ($debut = 0; $debut -lt $script:OrdreEtats.Count; $debut += 4) {
        Write-Host '     ' -NoNewline
        foreach ($code in $script:OrdreEtats[$debut..($debut + 3)]) {
            $nombre = ConvertTo-EntierGestion (Get-ChampGestion $etats $code)
            $couleur = $script:CouleursEtats[$code]
            if ($nombre -eq 0) { $couleur = 'DarkGray' }
            Write-Host (($script:LibellesEtats[$code] + ' ' + $nombre).PadRight(23)) -ForegroundColor $couleur -NoNewline
        }
        Write-Host ''
    }
}

function Write-ElementLicences {
    <# Un élément : type et nom, éditeur et licence, ses états (couleurs de /legal), puis ce qu'il faut faire et où. #>
    param($Element, [string]$Serveur)
    $type = [string](Get-ChampGestion $Element 'kind_label' '')
    $nom = [string](Get-ChampGestion $Element 'label' '')
    $moteur = [string](Get-ChampGestion $Element 'engine' '')
    if ($moteur) { $nom += ' · ' + $moteur }
    Write-Host ('   ' + $type.PadRight(11)) -ForegroundColor DarkGray -NoNewline
    Write-Host $nom -ForegroundColor White
    $licence = [string](Get-ChampGestion $Element 'license' '')
    $editeur = [string](Get-ChampGestion $Element 'publisher' '')
    $meta = @()
    if ($editeur) { $meta += ('éditeur ' + $editeur) }
    if ($licence) { $meta += ('licence ' + $licence) }
    if ($meta.Count) { Write-LignesCoupees -Prefixe '              ' -Texte ($meta -join ' · ') -Couleur DarkGray }
    Write-Host '              ' -NoNewline
    foreach ($etat in @(Get-ChampGestion $Element 'states' @())) {
        $code = [string](Get-ChampGestion $etat 'code' '')
        if (-not $script:LibellesEtats.ContainsKey($code)) { continue }
        Write-Host ('[' + $script:LibellesEtats[$code] + '] ') -ForegroundColor $script:CouleursEtats[$code] -NoNewline
    }
    Write-Host ''
    $declaration = Get-ChampGestion $Element 'declaration'
    if ($declaration -and [string](Get-ChampGestion $declaration 'label' '')) {
        Write-LignesCoupees -Prefixe '              ' -Texte ('Ta déclaration : ' + [string](Get-ChampGestion $declaration 'label' '') + '.') -Couleur DarkCyan
    }
    foreach ($afaire in @(Get-ChampGestion $Element 'todo' @())) {
        $texte = [string](Get-ChampGestion $afaire 'label' '')
        $detail = [string](Get-ChampGestion $afaire 'detail' '')
        if ($detail) { $texte += ' : ' + $detail }
        $lien = Get-LienLicences -Serveur $Serveur -Chemin ([string](Get-ChampGestion $afaire 'url' ''))
        if ($lien) { $texte += '  ' + $lien }
        $couleur = 'Yellow'
        switch ([string](Get-ChampGestion $afaire 'type' '')) { 'wait' { $couleur = 'DarkYellow' } 'info' { $couleur = 'Gray' } }
        Write-LignesCoupees -Prefixe '              > ' -Texte $texte -Couleur $couleur
    }
}

function Show-LicencesWorker {
    <# Résumé, puis la liste des éléments installés et celle des moteurs et modules proposés. #>
    param([string]$Racine, $Donnees)
    $config = $null
    try { $config = Get-ConfigSure -Racine $Racine } catch { }
    $serveur = ''
    if ($config) { $serveur = [string]$config.ServeurUrl }
    Write-Section 'Licences et droits de ce Worker'
    $lecteur = Get-ChampGestion $Donnees 'viewer'
    $qui = [string](Get-ChampGestion $lecteur 'label' '')
    $resumeSession = ''
    if (Get-Command -Name 'Get-ResumeSessionWorker' -CommandType Function -ErrorAction SilentlyContinue) { $resumeSession = Get-ResumeSessionWorker }
    Write-Etat 'Compte' $(if ($resumeSession) { $resumeSession } elseif ($qui) { $qui } else { 'connecté' }) 'info'
    Write-Etat 'Politique des licences' ([string](Get-ChampGestion $Donnees 'policy_version' '—')) 'info'
    if ([bool](Get-ChampGestion $Donnees 'country_unknown' $false)) {
        Write-Alerte ('Ton pays d''utilisation n''est pas vérifié : certaines licences en dépendent. ' + (Get-LienLicences -Serveur $serveur -Chemin '/legal#legalCountry'))
    }
    $resumes = Get-ChampGestion $Donnees 'summary'
    $installes = Get-ChampGestion $resumes 'installed'
    $total = ConvertTo-EntierGestion (Get-ChampGestion $installes 'total')
    $agir = ConvertTo-EntierGestion (Get-ChampGestion $installes 'to_act')
    $attente = ConvertTo-EntierGestion (Get-ChampGestion $installes 'waiting')
    Write-Etat 'Éléments installés' ([string]$total + ' · ' + $(if ($agir) { [string]$agir + ' attendent un geste de ta part' } else { 'rien à faire de ta part' }) + $(if ($attente) { ' · ' + $attente + ' en attente d''une réponse' } else { '' })) $(if ($agir) { 'alerte' } else { 'ok' })
    Write-PastillesLicences -Resume $installes

    Write-Section 'Installés sur ce Worker'
    $liste = @(Get-ChampGestion $Donnees 'installed' @() | Where-Object { $_ })
    if ($liste.Count -eq 0) { Write-Info 'Aucun moteur, module ni modèle installé sur ce Worker.' }
    foreach ($element in $liste) { Write-ElementLicences -Element $element -Serveur $serveur }

    $proposes = @(Get-ChampGestion $Donnees 'available' @() | Where-Object { $_ })
    if ($proposes.Count -gt 0) {
        Write-Section ('Moteurs et modules proposés à l''installation (' + $proposes.Count + ')')
        foreach ($element in $proposes) { Write-ElementLicences -Element $element -Serveur $serveur }
    }
    $modeles = ConvertTo-EntierGestion (Get-ChampGestion $Donnees 'available_models')
    Write-Host ''
    if ($modeles -gt 0) { Write-Info ([string]$modeles + ' modèle(s) du catalogue peuvent aussi s''installer : leurs licences sont sur la page Licences et justificatifs.') }
    if ([bool](Get-ChampGestion $Donnees 'truncated' $false)) { Write-Info 'Liste tronquée : la page Licences et justificatifs montre tout.' }
    Write-Conseil ('Détail, preuves et acceptation des conditions : ' + (Get-LienLicences -Serveur $serveur -Chemin '/legal'))
    Write-Conseil ('Déclarer tes droits : ' + (Get-LienLicences -Serveur $serveur -Chemin '/licences-personnelles'))
    $note = [string](Get-ChampGestion $Donnees 'notice' '')
    if ($note) { Write-LignesCoupees -Prefixe '   ' -Texte $note -Couleur DarkGray }
}

try { $Racine = Get-RacineWorker -Racine $Racine } catch { Write-Erreur ([string]$_.Exception.Message); Exit-Licences 1 }
# Session : celle du menu (même processus), sinon celle que worker.exe transmet par l'environnement.
if (-not $global:AlpineSessionWorker -and (Get-Command -Name 'Import-SessionTransmise' -CommandType Function -ErrorAction SilentlyContinue)) { [void](Import-SessionTransmise) }
if (-not (Get-Command -Name 'New-FichierSessionWorker' -CommandType Function -ErrorAction SilentlyContinue)) {
    Write-Alerte 'commun_connexion.ps1 est absent de ce Worker : mets le Worker à jour (23).'
    Exit-Licences 1
}
if (-not $global:AlpineSessionWorker) {
    Write-Alerte 'Licences et droits : connexion du compte requise. Ouvre le menu du Worker (MENU-WORKER.bat) et connecte-toi (C), ou passe par worker.exe.'
    Exit-Licences 1
}
if (-not (Test-DroitWorker 'status')) {
    Write-Alerte ('Refusé : les licences et droits demandent le droit « état, journaux et diagnostic » (' + (Get-ResumeSessionWorker) + ').')
    Exit-Licences 1
}
Write-Info 'Lecture des licences et droits sur le site, par l''agent du Worker...'
$lu = Read-LicencesWorker -Racine $Racine
if (-not $lu.Ok) {
    Write-Alerte ('Licences et droits indisponibles : ' + $lu.Erreur)
    Exit-Licences 1
}
Show-LicencesWorker -Racine $Racine -Donnees $lu.Donnees
Exit-Licences 0
