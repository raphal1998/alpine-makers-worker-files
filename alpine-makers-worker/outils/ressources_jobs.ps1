<#
    Jobs et ressources de CE Worker (entrée 26 du menu, rubrique « Gestion du Worker »).

    Affiche ce que l'agent applique (réglages gardés en cache depuis le site, limites calculées sur le matériel détecté)
    et ce qu'il mesure (VRAM, RAM, processeur, jobs en cours ou en file, moteurs à redémarrer), puis propose de régler :
    jobs simultanés, limites PAR JOB et limites CUMULÉES de VRAM, RAM et processeur. La modification passe par l'agent
    (action locale « resources-set »), qui la relaie au site (route signée self/resources) avec la session du
    propriétaire (ou d'un co-propriétaire qui a le droit « jobs ») : sans connexion, rien ne change. Le site refait tous
    les contrôles (par job <= cumulé <= matériel détecté) et prévient le propriétaire.

    Valeurs : « 6 » ou « 6 Gio » (VRAM, RAM), « 50 % » (VRAM, RAM : du total détecté ; processeur : de tout le
    processeur), « aucune » pour retirer une limite, Entrée seule pour garder la valeur actuelle.

    Pilotage sans clavier (essais) :
        ressources_jobs.ps1 -Oui -SansPause                       affiche l'état, ne change rien
        ressources_jobs.ps1 -JobsMax 2 -VramParJob 6 -RamCumulee '50 %' -CpuParJob '25 %' -Oui -SansPause
        ressources_jobs.ps1 -SansLimites -Oui -SansPause          retire toutes les limites (jobs simultanés gardés)
    -Simulation montre le réglage demandé et n'envoie rien.
    Codes de sortie : 0 fait, ou rien à changer ; 1 échec (agent, site) ; 2 refusé ou annulé.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause,
    [switch]$Oui,
    [switch]$Simulation,
    [string]$JobsMax = '',
    [string]$VramParJob = '',
    [string]$RamParJob = '',
    [string]$CpuParJob = '',
    [string]$VramCumulee = '',
    [string]$RamCumulee = '',
    [string]$CpuCumulee = '',
    [switch]$SansLimites
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'commun_cycle.ps1')
. (Join-Path $PSScriptRoot 'commun_gestion.ps1')
Initialize-ConsoleWorker -Titre 'Jobs et ressources du Worker'
$NomOutil = 'ressources_jobs'
try { $Racine = Get-RacineWorker -Racine $Racine } catch { Write-Erreur ([string]$_.Exception.Message); Exit-Outil -Code 1 -SansPause:$SansPause }

$script:Ressources = @(@('vram', 'VRAM'), @('ram', 'RAM'), @('cpu', 'Processeur'))
$script:Portees = @(@('per_job', 'par job'), @('total', 'cumulée'))

function Format-Gio {
    param($Mio)
    if ($null -eq $Mio) { return '-' }
    $valeur = [double]$Mio / 1024
    if ($valeur -lt 1) { $texte = $valeur.ToString('0.##', [Globalization.CultureInfo]::InvariantCulture) }
    else { $texte = $valeur.ToString('0.#', [Globalization.CultureInfo]::InvariantCulture) }
    return ($texte.Replace('.', ',') + ' Gio')
}

function Format-Limite {
    <# Limite effective (Mio ou % du processeur) d'une ressource, ou « aucune ». #>
    param([string]$Nom, $Valeur)
    if ($null -eq $Valeur) { return 'aucune' }
    if ($Nom -eq 'cpu') { return (([double]$Valeur).ToString('0.#', [Globalization.CultureInfo]::InvariantCulture).Replace('.', ',') + ' % du processeur') }
    return (Format-Gio $Valeur)
}

function Format-Reglage {
    <# Limite enregistrée {unit, value} telle que l'utilisateur l'a saisie. #>
    param($Element)
    if ($null -eq $Element) { return 'aucune' }
    $valeur = ([double](Get-ChampGestion $Element 'value' 0)).ToString('0.##', [Globalization.CultureInfo]::InvariantCulture).Replace('.', ',')
    if ([string](Get-ChampGestion $Element 'unit' '') -eq 'gib') { return ($valeur + ' Gio') }
    return ($valeur + ' %')
}

function ConvertFrom-SaisieLimite {
    <#
        « 6 », « 6 Gio », « 6,5 », « 50 % », « aucune » -> @{ Garder; Valeur } : Valeur = $null (aucune limite) ou
        @{ unit; value }. Lève une erreur lisible si la saisie n'est pas comprise.
    #>
    param([string]$Texte, [string]$Nom)
    $t = ([string]$Texte).Trim().ToLowerInvariant()
    if (-not $t) { return @{ Garder = $true; Valeur = $null } }
    if ($t -match '^(aucune|aucun|sans|0|-)$') { return @{ Garder = $false; Valeur = $null } }
    $m = [regex]::Match($t, '^([0-9]+(?:[.,][0-9]+)?)\s*(%|gio|go|gb|gib|g)?$')
    if (-not $m.Success) { throw ('Valeur non comprise : « ' + $Texte + ' » (exemples : 6, 6 Gio, 50 %, aucune).') }
    $nombre = [double]::Parse($m.Groups[1].Value.Replace(',', '.'), [Globalization.CultureInfo]::InvariantCulture)
    $unite = 'gib'
    if ($m.Groups[2].Value -eq '%' -or $Nom -eq 'cpu') { $unite = 'percent' }
    if ($Nom -eq 'cpu' -and $m.Groups[2].Value -and $m.Groups[2].Value -ne '%') { throw 'Le processeur se limite en % (exemple : 50 %).' }
    return @{ Garder = $false; Valeur = @{ unit = $unite; value = $nombre } }
}

function Get-LimiteEnregistree {
    param($Limites, [string]$Portee, [string]$Nom)
    $bloc = Get-ChampGestion $Limites $Portee
    $element = Get-ChampGestion $bloc $Nom
    if ($null -eq $element) { return $null }
    return @{ unit = [string](Get-ChampGestion $element 'unit' 'gib'); value = [double](Get-ChampGestion $element 'value' 0) }
}

function Show-Ressources {
    param($Vue)
    $reglages = Get-ChampGestion $Vue 'settings'
    $materiel = Get-ChampGestion $Vue 'hardware'
    $effectives = Get-ChampGestion $Vue 'effective'
    $mesure = Get-ChampGestion $Vue 'usage'
    Write-Section 'Jobs et ressources'
    if (-not [bool](Get-ChampGestion $reglages 'known' $false)) {
        Write-Alerte 'Réglages du site pas encore reçus par l''agent (Worker hors ligne depuis son démarrage, ou site ancien) : aucune limite appliquée.'
    }
    $jobs = Get-ChampGestion $reglages 'max_concurrent_jobs'
    if ($null -eq $jobs) { $jobs = 'réglage du site' }
    Write-Etat -Libelle 'Jobs simultanés (max)' -Valeur ([string]$jobs) -Niveau info
    $gpu = [string](Get-ChampGestion $materiel 'gpu_name' '')
    $texteGpu = 'non détectée'
    if ($null -ne (Get-ChampGestion $materiel 'vram_total_mb')) { $texteGpu = (Format-Gio (Get-ChampGestion $materiel 'vram_total_mb')) + $(if ($gpu) { ' (' + $gpu + ')' } else { '' }) }
    Write-Etat -Libelle 'VRAM détectée' -Valeur $texteGpu -Niveau info
    Write-Etat -Libelle 'RAM détectée' -Valeur (Format-Gio (Get-ChampGestion $materiel 'ram_total_mb')) -Niveau info
    Write-Etat -Libelle 'Processeur' -Valeur ([string](Get-ChampGestion $materiel 'cpu_cores' '?') + ' cœurs logiques') -Niveau info
    $limites = Get-ChampGestion $reglages 'limits'
    foreach ($p in $script:Portees) {
        $bloc = Get-ChampGestion $effectives $p[0]
        $parties = @()
        foreach ($r in $script:Ressources) {
            $cle = @{ vram = 'vram_mb'; ram = 'ram_mb'; cpu = 'cpu_percent' }[$r[0]]
            $saisie = Get-LimiteEnregistree $limites $p[0] $r[0]
            $texte = $r[1] + ' ' + (Format-Limite $r[0] (Get-ChampGestion $bloc $cle))
            if ($null -ne $saisie -and $saisie.unit -eq 'percent' -and $r[0] -ne 'cpu') { $texte += ' (' + (Format-Reglage $saisie) + ' du total)' }
            $parties += $texte
        }
        Write-Etat -Libelle ('Limites ' + $p[1]) -Valeur ($parties -join ' · ') -Niveau info
    }
    $systeme = Get-ChampGestion $mesure 'system'
    if ($null -ne $systeme) {
        $morceaux = @()
        if ($null -ne (Get-ChampGestion $systeme 'vram_total_mb')) { $morceaux += ('VRAM ' + (Format-Gio (Get-ChampGestion $systeme 'vram_used_mb')) + ' / ' + (Format-Gio (Get-ChampGestion $systeme 'vram_total_mb'))) }
        if ($null -ne (Get-ChampGestion $systeme 'ram_total_mb')) { $morceaux += ('RAM ' + (Format-Gio (Get-ChampGestion $systeme 'ram_used_mb')) + ' / ' + (Format-Gio (Get-ChampGestion $systeme 'ram_total_mb'))) }
        $cpu = Get-ChampGestion $systeme 'cpu_percent'
        if ($null -ne $cpu) { $morceaux += ('processeur ' + ([double]$cpu).ToString('0.#', [Globalization.CultureInfo]::InvariantCulture).Replace('.', ',') + ' %') }
        if ($morceaux.Count -gt 0) { Write-Etat -Libelle 'Utilisation du PC' -Valeur ($morceaux -join ' · ') -Niveau info }
    }
    $mesureJobs = Get-ChampGestion $mesure 'jobs'
    if ($null -ne $mesureJobs) {
        $texteJobs = [string](ConvertTo-EntierGestion (Get-ChampGestion $mesureJobs 'running')) + ' en cours, ' + [string](ConvertTo-EntierGestion (Get-ChampGestion $mesureJobs 'waiting')) + ' en file'
        $texteJobs += ' · réservé : VRAM ' + (Format-Gio (Get-ChampGestion $mesureJobs 'reserved_vram_mb' 0)) + ', RAM ' + (Format-Gio (Get-ChampGestion $mesureJobs 'reserved_ram_mb' 0))
        Write-Etat -Libelle 'Jobs sur ce PC' -Valeur $texteJobs -Niveau info
    }
    foreach ($attente in @(Get-ChampGestion $mesure 'waiting' @())) { Write-Alerte ([string](Get-ChampGestion $attente 'message' 'Job en file sur le Worker.')) }
    foreach ($moteur in @(Get-ChampGestion $mesure 'engines' @())) {
        if ([bool](Get-ChampGestion $moteur 'restart_required' $false)) {
            Write-Alerte ([string](Get-ChampGestion $moteur 'name' 'Un moteur') + ' tourne avec d''anciens réglages (VRAM, threads) : redémarre-le avec ' + (Get-NomEntree 9) + ' quand aucun job ne tourne.')
        }
    }
    $plafonds = Get-ChampGestion $Vue 'hard_caps'
    if (@('unavailable', 'unsupported') -contains [string](Get-ChampGestion $plafonds 'state' '')) { Write-Alerte ([string](Get-ChampGestion $plafonds 'message' 'Plafonds durs indisponibles : admission seulement.')) }
    $explication = Get-ChampGestion $Vue 'explanation'
    foreach ($cle in @('hard', 'admission', 'engines')) {
        $texte = [string](Get-ChampGestion $explication $cle '')
        if ($texte) { Write-Info $texte }
    }
}

function Read-Reponse {
    param([string]$Invite)
    $reponse = $null
    if (Test-EntreeRedirigee) { Write-LignesCoupees -Prefixe '   ' -Texte ($Invite + ' :') -Couleur White; $reponse = Read-Host }
    else { $reponse = Read-Host ('   ' + $Invite) }
    if ($null -eq $reponse) { return '' }
    return ([string]$reponse).Trim()
}

try {
    Write-Explication -Titre 'JOBS ET RESSOURCES DU WORKER' -Fait @(
        'Affiche les jobs simultanés et les limites de VRAM, RAM et processeur (par job et cumulées) appliquées par l''agent, et l''utilisation mesurée.',
        'Permet de les régler : le site vérifie chaque valeur (par job <= cumulé <= matériel détecté) et prévient le propriétaire.'
    ) -NeFaitPas @(
        'Il ne règle rien hors ligne ni sans la connexion du propriétaire (ou d''un co-propriétaire qui a le droit « jobs »).',
        'Il ne redémarre aucun moteur : un moteur lancé avant le changement le signale, à redémarrer quand aucun job ne tourne.'
    ) -Exige @('Le Worker allumé, associé à ton compte, et le site joignable pour modifier. Aucune fenêtre administrateur.')

    $lecture = Invoke-ActionLocale -Racine $Racine -Action 'resources' -DelaiSecondes 10
    if (-not $lecture.Ok -or $null -eq $lecture.Resultat) {
        if ($lecture.NonSupporte) { Write-Alerte (Get-MessageRelais 'ancien') }
        elseif ($lecture.Erreur) { Write-Alerte ([string]$lecture.Erreur) }
        else { Write-Alerte (Get-MessageRelais 'muet') }
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    $vue = $lecture.Resultat
    Show-Ressources -Vue $vue
    $reglages = Get-ChampGestion $vue 'settings'
    $limites = Get-ChampGestion $reglages 'limits'

    # --- Réglage voulu : paramètres, ou questions.
    $demandes = @{}
    $parametres = @{ per_job = @{ vram = $VramParJob; ram = $RamParJob; cpu = $CpuParJob }; total = @{ vram = $VramCumulee; ram = $RamCumulee; cpu = $CpuCumulee } }
    $saisies = @(@($parametres.per_job.Values) + @($parametres.total.Values) | Where-Object { $_ })
    $direct = ([bool]$JobsMax -or [bool]$SansLimites -or $saisies.Count -gt 0)
    $jobsVoulus = $null
    if ($direct) {
        if ($JobsMax) { $jobsVoulus = $JobsMax }
        foreach ($p in $script:Portees) { foreach ($r in $script:Ressources) { if ($parametres[$p[0]][$r[0]]) { $demandes[$p[0] + '.' + $r[0]] = [string]$parametres[$p[0]][$r[0]] } } }
    }
    elseif ($Oui -and $SansPause) { Exit-Outil -Code 0 -SansPause:$SansPause }
    else {
        Write-Host ''
        Write-Host '   1. Jobs simultanés'
        Write-Host '   2. Limites par job (VRAM, RAM, processeur)'
        Write-Host '   3. Limites cumulées sur tous les jobs (VRAM, RAM, processeur)'
        Write-Host '   4. Retirer toutes les limites'
        Write-Host '   0. Ne rien changer'
        $choix = Read-Reponse 'Ton choix'
        switch ($choix) {
            '1' { $jobsVoulus = Read-Reponse 'Jobs simultanés au maximum (1 à 16)' }
            '2' { foreach ($r in $script:Ressources) { $demandes['per_job.' + $r[0]] = Read-Reponse ($r[1] + ' par job (actuel : ' + (Format-Reglage (Get-LimiteEnregistree $limites 'per_job' $r[0])) + ' ; ex. 6, 50 %, aucune ; Entrée = garder)') } }
            '3' { foreach ($r in $script:Ressources) { $demandes['total.' + $r[0]] = Read-Reponse ($r[1] + ' cumulée (actuel : ' + (Format-Reglage (Get-LimiteEnregistree $limites 'total' $r[0])) + ' ; ex. 12, 75 %, aucune ; Entrée = garder)') } }
            '4' { $SansLimites = $true }
            default { Write-Info 'Rien n''a été changé.'; Exit-Outil -Code 0 -SansPause:$SansPause }
        }
    }

    # --- Construction du réglage complet (forme du site) et contrôle de la saisie.
    $envoi = @{}
    if ($null -ne $jobsVoulus -and [string]$jobsVoulus -ne '') {
        $nombre = 0
        if (-not [int]::TryParse([string]$jobsVoulus, [ref]$nombre) -or $nombre -lt 1 -or $nombre -gt 16) {
            Write-Alerte 'Jobs simultanés : entier entre 1 et 16. Rien n''a été envoyé.'
            Exit-Outil -Code 2 -SansPause:$SansPause
        }
        $envoi['max_concurrent_jobs'] = $nombre
    }
    $nouvelles = @{ per_job = @{}; total = @{} }
    $change = [bool]$SansLimites
    foreach ($p in $script:Portees) {
        foreach ($r in $script:Ressources) {
            $actuelle = Get-LimiteEnregistree $limites $p[0] $r[0]
            $valeur = $actuelle
            if ($SansLimites) { $valeur = $null }
            elseif ($demandes.ContainsKey($p[0] + '.' + $r[0])) {
                try { $lu = ConvertFrom-SaisieLimite -Texte $demandes[$p[0] + '.' + $r[0]] -Nom $r[0] }
                catch { Write-Alerte ([string]$_.Exception.Message + ' Rien n''a été envoyé.'); Exit-Outil -Code 2 -SansPause:$SansPause }
                if (-not $lu.Garder) { $valeur = $lu.Valeur; $change = $true }
            }
            $nouvelles[$p[0]][$r[0]] = $valeur
        }
    }
    if ($change) { $envoi['limits'] = $nouvelles }
    if ($envoi.Count -eq 0) { Write-Info 'Rien à changer : rien n''a été envoyé.'; Exit-Outil -Code 0 -SansPause:$SansPause }

    Write-Host ''
    if ($envoi.ContainsKey('max_concurrent_jobs')) { Write-Etat -Libelle 'Jobs simultanés' -Valeur ([string]$envoi['max_concurrent_jobs']) -Niveau alerte }
    if ($envoi.ContainsKey('limits')) {
        foreach ($p in $script:Portees) {
            $parties = @()
            foreach ($r in $script:Ressources) { $parties += ($r[1] + ' ' + (Format-Reglage $nouvelles[$p[0]][$r[0]])) }
            Write-Etat -Libelle ('Nouvelles limites ' + $p[1]) -Valeur ($parties -join ' · ') -Niveau alerte
        }
    }
    if ($Simulation) { Write-Info '[simulation] Ce réglage n''a pas été envoyé au site : rien n''a changé.'; Exit-Outil -Code 0 -SansPause:$SansPause }
    if (-not (Confirm-Action -Question 'Enregistrer ce réglage sur le site ?' -Oui:$Oui)) { Write-Info 'Annulé : rien n''a été changé.'; Exit-Outil -Code 2 -SansPause:$SansPause }

    Write-Info 'Réglage transmis au site par l''agent...'
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'réglage des jobs et ressources demandé'
    $relais = Invoke-RelaisSite -Racine $Racine -Action 'resources-set' -Arguments $envoi -DelaiSecondes 60
    if (-not $relais.Ok) {
        Write-Host ''
        if ([int]$relais.Http -eq 400 -and [string]$relais.Erreur) { Write-Alerte ('Réglage refusé par le site : ' + [string]$relais.Erreur); $code = 2 }
        else { Write-RefusRelais -Resultat $relais; $code = 1 }
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('réglage des jobs et ressources non enregistré (erreur ' + [int]$relais.Http + ' ' + [string]$relais.CodeSite + ')')
        Exit-Outil -Code $code -SansPause:$SansPause
    }
    Write-Host ''
    Write-Ok 'Réglage enregistré par le site. Le Worker l''applique à son prochain heartbeat (20 s au plus) ; les jobs déjà lancés gardent leurs plafonds.'
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'réglage des jobs et ressources enregistré'
    Write-Info 'Le compte propriétaire reçoit une notification sur le site.'
    Exit-Outil -Code 0 -SansPause:$SansPause
} catch {
    Write-Erreur ('Erreur inattendue : ' + $_.Exception.Message)
    if (-not $Simulation) { try { Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('erreur inattendue : ' + $_.Exception.Message) } catch { } }
    Exit-Outil -Code 1 -SansPause:$SansPause
}
