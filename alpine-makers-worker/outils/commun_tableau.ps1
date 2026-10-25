<#
    Tableau de bord du Worker dans le menu console : même état que l'onglet « Tableau de bord » de worker.exe.

    À charger APRÈS commun.ps1 et commun_gestion.ps1 (commun_gestion.ps1 le charge lui-même quand il est présent).
    La source unique est l'action locale « dashboard » de l'agent (worker_dashboard.py) : lecture seule, sans
    réseau, sans secret ni chemin. Ce fichier ne fait que la mettre en forme.
#>

$script:TableauOutils = @{
    'image_generation' = 'Images (ComfyUI)'; 'ai3d' = 'IA 3D (Hunyuan3D)'; 'converter' = 'Convertisseur (FreeCAD)'
    'printguard' = 'PrintGuard'; 'model-studio' = 'Model Studio (Orca)'; 'laser-studio' = 'Laser Studio'; 'cnc-studio' = 'CNC Studio'
}

function Get-TableauWorker {
    <# Réponse de l'action « dashboard », ou $null (agent arrêté, trop ancien, ou muet). -Rafraichir relance les mesures. #>
    param([string]$Racine, [switch]$Rafraichir, [int]$DelaiSecondes = 6)
    $reponse = $null
    try { $reponse = Invoke-ActionLocale -Racine $Racine -Action 'dashboard' -DelaiSecondes $DelaiSecondes -Arguments @{ refresh = [bool]$Rafraichir } } catch { }
    if ($reponse -and $reponse.Ok -and $reponse.Resultat) { return $reponse.Resultat }
    return $null
}

function Format-GioTableau {
    <# Octets -> « 12,3 Gio » (une décimale) ; « — » si inconnu. #>
    param($Octets)
    if ($null -eq $Octets -or $Octets -is [bool]) { return '—' }
    try { $n = [double]$Octets } catch { return '—' }
    if ($n -lt 0) { return '—' }
    if ($n -lt 1GB) { return (Format-Taille $n) }
    return ('{0:N1} Gio' -f ($n / 1GB))
}

function Format-MioTableau {
    param($Mio)
    if ($null -eq $Mio -or $Mio -is [bool]) { return '—' }
    try { return ('{0:N1} Gio' -f ([double]$Mio / 1024)) } catch { return '—' }
}

function Get-NomOutilTableau {
    param([string]$Outil, [string]$Defaut = '')
    if ($script:TableauOutils.ContainsKey($Outil)) { return $script:TableauOutils[$Outil] }
    if ($Defaut) { return $Defaut }
    return $Outil
}

function Get-AgeTableau {
    param($Secondes)
    $n = ConvertTo-EntierGestion $Secondes -Maximum 100000000
    if ($null -eq $Secondes -or $Secondes -is [bool]) { return '' }
    if ($n -lt 90) { return 'il y a ' + $n + ' s' }
    if ($n -lt 5400) { return 'il y a ' + [int][Math]::Round($n / 60) + ' min' }
    return 'il y a ' + [int][Math]::Round($n / 3600) + ' h'
}

function New-LigneTableau {
    param([string]$Libelle, [string]$Niveau = 'info', [string]$Texte = '', [string]$Couleur = 'Gray')
    return [pscustomobject]@{ Libelle = $Libelle; Niveau = $Niveau; Texte = $Texte; Couleur = $Couleur }
}

function Get-ResumeIaLocales {
    param($Tableau)
    $composants = @(Get-ChampGestion $Tableau 'components' @())
    $installes = @($composants | Where-Object { [bool](Get-ChampGestion $_ 'installed' $false) }).Count
    $enMarche = @($composants | Where-Object { (Get-ChampGestion $_ 'running') -eq $true }).Count
    $associes = @($composants | Where-Object { (Get-ChampGestion $_ 'associated') -eq $true }).Count
    $associesConnus = @($composants | Where-Object { $null -ne (Get-ChampGestion $_ 'associated') }).Count -gt 0
    $modeles = Get-ChampGestion $Tableau 'models'
    $off = ConvertTo-EntierGestion (Get-ChampGestion $modeles 'off')
    foreach ($c in $composants) { $off += @(@(Get-ChampGestion $c 'modules' @()) | Where-Object { [bool](Get-ChampGestion $_ 'off' $false) }).Count }
    $texte = ('{0} installé(s) · {1} en marche' -f $installes, $enMarche)
    if ($associesConnus) { $texte += (' · ' + $associes + ' associé(s)') }
    $texte += (' · ' + (ConvertTo-EntierGestion (Get-ChampGestion $modeles 'installed')) + ' modèle(s)')
    if ($off -gt 0) { $texte += (' · ' + $off + ' OFF') }
    $comptes = Get-ChampGestion $Tableau 'ai_accounts'
    $liste = @(Get-ChampGestion $comptes 'accounts' @())
    if ($liste.Count -gt 0) {
        $texte += (' · comptes IA ' + @($liste | Where-Object { [bool](Get-ChampGestion $_ 'logged_in' $false) }).Count + '/' + $liste.Count)
    }
    return $texte
}

function Get-LignesTableauBandeau {
    <#
        Les six lignes du tableau de bord sous l'en-tête du menu, TOUJOURS six (la hauteur du menu en dépend) :
        Jobs / partage, RAM / disque, Pilote / CUDA, Installations, IA locales, Connexion.
        -Connexion : texte de la ligne « Connexion » déjà préparé par le menu (session ou mode hors ligne).
    #>
    param($Tableau, [string]$Connexion = '', [string]$NiveauConnexion = 'info')
    $lignes = @()
    if ($null -eq $Tableau) {
        $absent = 'inconnu : l''agent ne répond pas (tableau de bord indisponible)'
        $lignes += (New-LigneTableau 'Jobs / partage' 'info' $absent 'DarkGray')
        $lignes += (New-LigneTableau 'RAM / disque' 'info' '—' 'DarkGray')
        $lignes += (New-LigneTableau 'Pilote / CUDA' 'info' '—' 'DarkGray')
        $lignes += (New-LigneTableau 'Installations' 'info' '—' 'DarkGray')
        $lignes += (New-LigneTableau 'IA locales' 'info' '—' 'DarkGray')
    }
    else {
        # Jobs et partage
        $jobs = Get-ChampGestion $Tableau 'jobs'
        $locaux = ConvertTo-EntierGestion (Get-ChampGestion $jobs 'count')
        $texte = ('{0} en cours sur ce PC' -f $locaux)
        $file = Get-ChampGestion $jobs 'site_queued'
        if ($null -ne $file) { $texte += (' · ' + (ConvertTo-EntierGestion $file) + ' en file') }
        $texte += (' · max ' + [Math]::Max(1, (ConvertTo-EntierGestion (Get-ChampGestion $jobs 'max_concurrent'))))
        $bureau = ConvertTo-EntierGestion (Get-ChampGestion $jobs 'desktop_tasks')
        if ($bureau -gt 0) { $texte += (' · ' + $bureau + ' calcul(s) worker.exe') }
        $partage = Get-ChampGestion $Tableau 'sharing'
        $texte += (' · partage : ' + [string](Get-ChampGestion $partage 'label' 'inconnu'))
        $niveau = 'ok'; if ($locaux -gt 0 -or $bureau -gt 0) { $niveau = 'alerte' }
        $lignes += (New-LigneTableau 'Jobs / partage' $niveau $texte 'Gray')

        # RAM et disque
        $ram = Get-ChampGestion $Tableau 'ram'
        $disque = Get-ChampGestion $Tableau 'disk'
        $texte = 'RAM ' + (Format-MioTableau (Get-ChampGestion $ram 'used_mb')) + ' / ' + (Format-MioTableau (Get-ChampGestion $ram 'total_mb'))
        $pourcent = Get-ChampGestion $ram 'percent'
        if ($null -ne $pourcent) { $texte += (' (' + (ConvertTo-EntierGestion $pourcent) + ' %)') }
        $texte += (' · disque ' + (Format-GioTableau (Get-ChampGestion $disque 'free_bytes')) + ' libres sur ' + (Format-GioTableau (Get-ChampGestion $disque 'total_bytes')))
        $niveau = 'ok'
        if ((ConvertTo-EntierGestion $pourcent) -ge 90 -or (ConvertTo-EntierGestion (Get-ChampGestion $disque 'percent')) -ge 95) { $niveau = 'erreur' }
        elseif ((ConvertTo-EntierGestion $pourcent) -ge 75 -or (ConvertTo-EntierGestion (Get-ChampGestion $disque 'percent')) -ge 85) { $niveau = 'alerte' }
        $lignes += (New-LigneTableau 'RAM / disque' $niveau $texte 'Gray')

        # Pilote et CUDA
        $pilote = [string](Get-ChampGestion $Tableau 'driver_version' '')
        $cuda = [string](Get-ChampGestion $Tableau 'cuda_version' '')
        $cartes = @(Get-ChampGestion $Tableau 'gpus' @()).Count
        if (-not $pilote -and -not $cuda -and $cartes -eq 0) { $lignes += (New-LigneTableau 'Pilote / CUDA' 'info' 'aucune carte NVIDIA vue par l''agent' 'DarkGray') }
        else {
            $texte = 'pilote NVIDIA ' + $(if ($pilote) { $pilote } else { '—' }) + ' · CUDA ' + $(if ($cuda) { $cuda } else { '—' }) + ' · ' + $cartes + ' carte(s)'
            $lignes += (New-LigneTableau 'Pilote / CUDA' 'ok' $texte 'Gray')
        }

        # Place occupée par les installations Alpine Makers
        $stockage = Get-ChampGestion $Tableau 'storage'
        $etat = [string](Get-ChampGestion $stockage 'state' '')
        $total = Get-ChampGestion $stockage 'total_bytes'
        if ($null -eq $total) {
            $texte = 'mesure en cours (quelques minutes après le démarrage de l''agent)'
            if ($etat -eq 'pending') { $texte = 'mesure prévue sous peu (T pour le détail)' }
            $lignes += (New-LigneTableau 'Installations' 'info' $texte 'DarkGray')
        }
        else {
            $morceaux = @()
            foreach ($item in @(Get-ChampGestion $stockage 'items' @() | Select-Object -First 3)) {
                $morceaux += (([string](Get-ChampGestion $item 'key' '')) -replace '^engines$', 'moteurs' -replace '^models$', 'modèles' -replace '^venv$', 'environnements' -replace '^python$', 'Python' -replace '^outputs$', 'résultats' -replace '^other$', 'divers') + ' ' + (Format-GioTableau (Get-ChampGestion $item 'bytes'))
            }
            $texte = (Format-GioTableau $total) + ' au total'
            if ($morceaux.Count) { $texte += (' : ' + ($morceaux -join ', ')) }
            if ([bool](Get-ChampGestion $stockage 'partial' $false)) { $texte += ' (partiel)' }
            $lignes += (New-LigneTableau 'Installations' 'ok' $texte 'Gray')
        }

        $lignes += (New-LigneTableau 'IA locales' 'ok' (Get-ResumeIaLocales -Tableau $Tableau) 'Gray')
    }
    if (-not $Connexion) { $Connexion = 'non connecté' }
    $couleur = 'Gray'
    switch ($NiveauConnexion) { 'ok' { $couleur = 'Green' } 'alerte' { $couleur = 'Yellow' } 'erreur' { $couleur = 'Red' } }
    $lignes += (New-LigneTableau 'Connexion' $NiveauConnexion $Connexion $couleur)
    return $lignes
}

function Show-TableauDeBord {
    <# Tableau de bord détaillé (touche T du menu) : tout ce que l'agent sait, section par section. Lecture seule. #>
    param([string]$Racine, $Tableau = $null)
    if ($null -eq $Tableau) { $Tableau = Get-TableauWorker -Racine $Racine -Rafraichir -DelaiSecondes 10 }
    Write-Section 'Tableau de bord du Worker'
    if ($null -eq $Tableau) {
        Write-Alerte 'L''agent ne répond pas, ou il est trop ancien pour le tableau de bord : rallume-le (12) ou mets le Worker à jour (23).'
        return
    }
    $agent = Get-ChampGestion $Tableau 'agent'
    Write-Etat 'Version de l''agent' ([string](Get-ChampGestion $agent 'version' '—')) 'info'
    $maj = [string](Get-ChampGestion $agent 'update_available' '')
    if ($maj) { Write-Etat 'Mise à jour' ('disponible : ' + $maj + ' (entrée 23)') 'alerte' } else { Write-Etat 'Mise à jour' 'aucune proposée' 'ok' }
    if ([bool](Get-ChampGestion $agent 'maintenance_active' $false)) { Write-Etat 'Maintenance' 'en cours (installation, arrêt ou réparation)' 'alerte' }

    Write-Section 'Connexion au site'
    $connexion = Get-ChampGestion $Tableau 'connection'
    $battement = Get-ChampGestion $connexion 'heartbeat'
    switch ([string](Get-ChampGestion $connexion 'state' '')) {
        'ok' { Write-Etat 'Liaison (heartbeat)' 'en ligne' 'ok' }
        'offline' { Write-Etat 'Liaison (heartbeat)' ('site injoignable (' + (ConvertTo-EntierGestion (Get-ChampGestion $battement 'failures')) + ' échec(s) de suite)') 'erreur' }
        'refused' { Write-Etat 'Liaison (heartbeat)' ('refusée par le site (erreur ' + (ConvertTo-EntierGestion (Get-ChampGestion $battement 'http')) + ')') 'erreur' }
        default { Write-Etat 'Liaison (heartbeat)' 'inconnue (agent qui démarre)' 'info' }
    }
    $site = Get-ChampGestion $connexion 'site'
    if ([bool](Get-ChampGestion $site 'known' $false)) { Write-Etat 'État lu sur le site' (Get-AgeTableau (Get-ChampGestion $site 'age_seconds')) 'ok' }
    elseif ([bool](Get-ChampGestion $site 'unreachable' $false)) { Write-Etat 'État lu sur le site' 'site injoignable' 'alerte' }
    else { Write-Etat 'État lu sur le site' 'pas encore lu' 'info' }
    $resume = ''
    if (Get-Command -Name 'Get-ResumeSessionWorker' -CommandType Function -ErrorAction SilentlyContinue) { $resume = Get-ResumeSessionWorker }
    if ($resume) { Write-Etat 'Session' $resume 'ok' }
    elseif ($global:AlpineModeHorsLigne) { Write-Etat 'Session' 'mode hors ligne : actions locales sûres seulement' 'alerte' }
    else { Write-Etat 'Session' 'non connecté' 'info' }

    Write-Section 'Jobs et partage'
    $jobs = Get-ChampGestion $Tableau 'jobs'
    Write-Etat 'Jobs en cours sur ce PC' ([string](ConvertTo-EntierGestion (Get-ChampGestion $jobs 'count'))) $(if ((ConvertTo-EntierGestion (Get-ChampGestion $jobs 'count')) -gt 0) { 'alerte' } else { 'ok' })
    foreach ($j in @(Get-ChampGestion $jobs 'active' @())) {
        Write-Info ('     ' + [string](Get-ChampGestion $j 'job_id' '') + '…  ' + [string](Get-ChampGestion $j 'tool' '') + '  (' + [string](Get-ChampGestion $j 'state' '') + ')')
    }
    $actifs = Get-ChampGestion $jobs 'site_active'
    if ($null -ne $actifs) { Write-Etat 'Vu par le site' ((ConvertTo-EntierGestion $actifs).ToString() + ' actif(s), dont ' + (ConvertTo-EntierGestion (Get-ChampGestion $jobs 'site_queued')) + ' en file') 'info' }
    Write-Etat 'Jobs simultanés (max)' ([string][Math]::Max(1, (ConvertTo-EntierGestion (Get-ChampGestion $jobs 'max_concurrent')))) 'info'
    $bureau = ConvertTo-EntierGestion (Get-ChampGestion $jobs 'desktop_tasks')
    if ($bureau -gt 0) { Write-Etat 'Calculs de worker.exe' ([string]$bureau) 'alerte' }
    $graveuses = @(Get-ChampGestion $jobs 'grbl_jobs' @())
    if ($graveuses.Count -gt 0) { Write-Etat 'Gravures / cadrages' ($graveuses -join ', ') 'alerte' }
    # Jobs et ressources (resource_limits_v1) : limites appliquées par l'agent ; détail et réglage dans l'entrée 26.
    $ressources = Get-ChampGestion $Tableau 'resources'
    if ($null -ne $ressources) {
        foreach ($portee in @(@('per_job', 'Limites par job'), @('total', 'Limites cumulées'))) {
            $bloc = Get-ChampGestion (Get-ChampGestion $ressources 'effective') $portee[0]
            $parties = @()
            foreach ($r in @(@('vram_mb', 'VRAM'), @('ram_mb', 'RAM'))) {
                $v = Get-ChampGestion $bloc $r[0]
                if ($null -ne $v) { $parties += ($r[1] + ' ' + (Format-MioTableau $v)) }
            }
            $cpu = Get-ChampGestion $bloc 'cpu_percent'
            if ($null -ne $cpu) { $parties += ('processeur ' + [string]$cpu + ' %') }
            if ($parties.Count -eq 0) { $parties = @('aucune') }
            Write-Etat $portee[1] ($parties -join ' · ') 'info'
        }
        foreach ($moteur in @(Get-ChampGestion (Get-ChampGestion $ressources 'usage') 'engines' @())) {
            if ([bool](Get-ChampGestion $moteur 'restart_required' $false)) { Write-Etat ([string](Get-ChampGestion $moteur 'name' 'Moteur')) 'à redémarrer pour appliquer les limites (quand aucun job ne tourne)' 'alerte' }
        }
    }
    $partage = Get-ChampGestion $Tableau 'sharing'
    Write-Etat 'Mode de partage' ([string](Get-ChampGestion $partage 'label' 'inconnu')) 'info'
    if ([bool](Get-ChampGestion $partage 'known' $false)) {
        Write-Etat 'Acceptation automatique' $(if ([bool](Get-ChampGestion $partage 'auto_accept_jobs' $false)) { 'oui' } else { 'non (ton accord par job)' }) 'info'
        Write-Etat 'Comptes choisis' ([string](ConvertTo-EntierGestion (Get-ChampGestion $partage 'users_count'))) 'info'
    }

    Write-Section 'Ressources du PC'
    $ram = Get-ChampGestion $Tableau 'ram'
    Write-Etat 'RAM' ((Format-MioTableau (Get-ChampGestion $ram 'used_mb')) + ' utilisés sur ' + (Format-MioTableau (Get-ChampGestion $ram 'total_mb')) + ' (' + (Format-MioTableau (Get-ChampGestion $ram 'free_mb')) + ' libres)') 'info'
    $disque = Get-ChampGestion $Tableau 'disk'
    Write-Etat 'Disque du dossier Worker' ((Format-GioTableau (Get-ChampGestion $disque 'free_bytes')) + ' libres sur ' + (Format-GioTableau (Get-ChampGestion $disque 'total_bytes'))) 'info'
    $cpu = Get-ChampGestion $Tableau 'cpu'
    if ([string](Get-ChampGestion $cpu 'name' '')) { Write-Etat 'Processeur' ([string](Get-ChampGestion $cpu 'name' '') + ' · ' + (ConvertTo-EntierGestion (Get-ChampGestion $cpu 'cores')) + ' cœurs') 'info' }
    foreach ($g in @(Get-ChampGestion $Tableau 'gpus' @())) {
        $texte = (Format-MioTableau (Get-ChampGestion $g 'vram_used_mb')) + ' / ' + (Format-MioTableau (Get-ChampGestion $g 'vram_total_mb')) + ' de VRAM'
        $charge = Get-ChampGestion $g 'utilization_percent'
        if ($null -ne $charge) { $texte += (' · charge ' + $charge + ' %') }
        $temperature = Get-ChampGestion $g 'temperature_c'
        if ($null -ne $temperature) { $texte += (' · ' + $temperature + ' °C') }
        Write-Etat ([string](Get-ChampGestion $g 'name' 'GPU')) $texte 'info'
    }
    Write-Etat 'Pilote NVIDIA' ([string](Get-ChampGestion $Tableau 'driver_version' '—')) 'info'
    Write-Etat 'CUDA (pilote)' ([string](Get-ChampGestion $Tableau 'cuda_version' '—')) 'info'

    Write-Section 'Place occupée par Alpine Makers'
    $stockage = Get-ChampGestion $Tableau 'storage'
    $total = Get-ChampGestion $stockage 'total_bytes'
    if ($null -eq $total) { Write-Info 'Mesure en cours ou à venir : reviens dans quelques minutes (R pour actualiser).' }
    else {
        $date = ConvertTo-DateLocale (Get-ChampGestion $stockage 'computed_at')
        $quand = ''; if ($date) { $quand = ' (mesuré le ' + (Format-DateEtat -Date $date) + ')' }
        Write-Etat 'Total' ((Format-GioTableau $total) + $quand + $(if ([bool](Get-ChampGestion $stockage 'partial' $false)) { ', partiel' } else { '' })) 'info'
        foreach ($item in @(Get-ChampGestion $stockage 'items' @())) { Write-Etat ('  ' + [string](Get-ChampGestion $item 'label' '')) (Format-GioTableau (Get-ChampGestion $item 'bytes')) 'info' }
        foreach ($c in @(Get-ChampGestion $stockage 'components' @())) {
            Write-Info ('     ' + [string](Get-ChampGestion $c 'id' '') + ' : moteur ' + (Format-GioTableau (Get-ChampGestion $c 'engine_bytes')) + ', modèles ' + (Format-GioTableau (Get-ChampGestion $c 'models_bytes')) + ', environnement ' + (Format-GioTableau (Get-ChampGestion $c 'venv_bytes')))
        }
    }

    Write-Section 'Composants, outils et IA locales'
    $composants = @(Get-ChampGestion $Tableau 'components' @())
    if ($composants.Count -eq 0) { Write-Info 'Inventaire pas encore prêt (l''agent vient de démarrer).' }
    foreach ($c in $composants) {
        $etats = @()
        if ([bool](Get-ChampGestion $c 'installed' $false)) { $etats += 'installé' } else { $etats += ('non installé (' + [string](Get-ChampGestion $c 'status' '') + ')') }
        $marche = Get-ChampGestion $c 'running'
        if ($marche -eq $true) { $etats += 'allumé' } elseif ($marche -eq $false) { $etats += 'éteint' }
        $associe = Get-ChampGestion $c 'associated'
        if ($associe -eq $true) { $etats += 'associé' } elseif ($associe -eq $false) { $etats += 'non associé' }
        if ([bool](Get-ChampGestion $c 'repair_required' $false)) { $etats += 'réparation conseillée' }
        $niveau = 'info'
        if ($marche -eq $true) { $niveau = 'ok' } elseif ([bool](Get-ChampGestion $c 'repair_required' $false)) { $niveau = 'alerte' }
        Write-Etat (Get-NomOutilTableau -Outil ([string](Get-ChampGestion $c 'tool_id' '')) -Defaut ([string](Get-ChampGestion $c 'name' ''))) ($etats -join ' · ') $niveau
        foreach ($m in @(Get-ChampGestion $c 'modules' @())) {
            Write-Info ('     module ' + [string](Get-ChampGestion $m 'id' '') + ' : ' + [string](Get-ChampGestion $m 'state' '') + $(if ([bool](Get-ChampGestion $m 'off' $false)) { ' · OFF' } else { '' }))
        }
    }
    $modeles = Get-ChampGestion $Tableau 'models'
    foreach ($m in @(Get-ChampGestion $modeles 'by_tool' @())) {
        Write-Etat ('Modèles ' + (Get-NomOutilTableau -Outil ([string](Get-ChampGestion $m 'tool_id' '')))) ((ConvertTo-EntierGestion (Get-ChampGestion $m 'installed')).ToString() + ' installé(s), ' + (Format-GioTableau (Get-ChampGestion $m 'bytes')) + $(if ((ConvertTo-EntierGestion (Get-ChampGestion $m 'off')) -gt 0) { ', ' + (ConvertTo-EntierGestion (Get-ChampGestion $m 'off')) + ' OFF' } else { '' })) 'info'
    }
    if (-not [bool](Get-ChampGestion $modeles 'site_known' $false)) { Write-Conseil 'Associations et ON/OFF : connus après la prochaine lecture de l''état sur le site.' }

    Write-Section 'API et comptes IA du Worker'
    $comptes = Get-ChampGestion $Tableau 'ai_accounts'
    switch ([string](Get-ChampGestion $comptes 'state' '')) {
        'computing' { Write-Info 'Lecture des comptes IA en cours (quelques secondes) : R pour actualiser.' }
        'error' { Write-Alerte ([string](Get-ChampGestion $comptes 'message' 'Comptes IA illisibles.')) }
    }
    $cli = Get-ChampGestion $comptes 'cli'
    if ($cli) {
        foreach ($p in @($cli.PSObject.Properties)) {
            $installe = [bool](Get-ChampGestion $p.Value 'installed' $false)
            Write-Etat ('Outil ' + $p.Name) $(if ($installe) { 'installé ' + [string](Get-ChampGestion $p.Value 'version' '') } else { 'absent' }) $(if ($installe) { 'ok' } else { 'info' })
        }
    }
    foreach ($a in @(Get-ChampGestion $comptes 'accounts' @())) {
        $connecte = [bool](Get-ChampGestion $a 'logged_in' $false)
        Write-Etat ([string](Get-ChampGestion $a 'provider' '') + ' · ' + [string](Get-ChampGestion $a 'label' '')) $(if ($connecte) { 'connecté ' + [string](Get-ChampGestion $a 'method' '') } else { 'non connecté' }) $(if ($connecte) { 'ok' } else { 'info' })
    }
    $surveillance = Get-ChampGestion $Tableau 'fire_watch'
    Write-Etat 'Surveillance des graveuses' ([string](Get-ChampGestion $surveillance 'state' 'inconnue')) 'info'
}

# --- Journal des jobs (touche J du menu) ------------------------------------------------------------------------------
# Le fichier data\journal-des-jobs.jsonl (une ligne JSON par job terminé, écrit par l'agent : worker_agent/job_journal.py)
# est lu ici directement, en lecture partagée : la touche J marche aussi agent arrêté. Lecture seule : filtres,
# aperçus et suppression sont dans worker.exe (« Journal des jobs »).
$script:JournalStatuts = @{ 'termine' = @('Terminé', 'Green'); 'echoue' = @('Échoué', 'Red'); 'annule' = @('Annulé', 'Yellow') }

function Read-LignesJournal {
    <# Lignes d'un fichier du journal (vide s'il manque), ouvert sans gêner l'agent qui écrit ou fait tourner les fichiers. #>
    param([string]$Chemin)
    if (-not (Test-Path -LiteralPath $Chemin -PathType Leaf)) { return @() }
    try {
        $flux = [System.IO.File]::Open($Chemin, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
        try { $texte = (New-Object System.IO.StreamReader($flux, (New-Object System.Text.UTF8Encoding($false)))).ReadToEnd() } finally { $flux.Dispose() }
    } catch { return @() }
    return @($texte -split "`n" | Where-Object { $_.Trim() })
}

function ConvertTo-MomentJournal {
    <# Secondes epoch UTC (format du journal et de l'historique du site) ou date ISO 8601 -> date locale ; $null sinon. #>
    param($Valeur)
    if ($null -eq $Valeur -or $Valeur -is [bool]) { return $null }
    if ($Valeur -is [string]) {
        if (-not $Valeur) { return $null }
        $date = [DateTimeOffset]::MinValue
        if ([DateTimeOffset]::TryParse($Valeur, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeLocal, [ref]$date)) { return $date.LocalDateTime }
        return $null
    }
    return ConvertTo-DateLocale $Valeur
}

function Format-DureeJournal {
    param($Secondes)
    if ($null -eq $Secondes -or $Secondes -is [bool] -or $Secondes -is [string]) { return '—' }
    try { $total = [long][Math]::Round([double]$Secondes) } catch { return '—' }
    if ($total -lt 1) { return '< 1 s' }
    if ($total -lt 60) { return ('{0} s' -f $total) }
    if ($total -lt 3600) { return ('{0} min {1:00} s' -f [Math]::Floor($total / 60), ($total % 60)) }
    return ('{0} h {1:00} min' -f [Math]::Floor($total / 3600), [Math]::Floor(($total % 3600) / 60))
}

function Get-JournalDesJobs {
    <# Les -Nombre derniers jobs du journal local, plus récents d'abord (lancement, à défaut réception, à défaut fin). #>
    param([string]$Racine, [int]$Nombre = 20)
    $dossier = Join-Path $Racine 'data'
    # Les lignes sont écrites à la fin de chaque job : les plus récentes sont en fin du fichier courant, puis des archives.
    $lignes = @(Read-LignesJournal -Chemin (Join-Path $dossier 'journal-des-jobs.jsonl'))
    foreach ($numero in 1..3) {
        if ($lignes.Count -ge ($Nombre * 3)) { break }
        $lignes = @(Read-LignesJournal -Chemin (Join-Path $dossier ('journal-des-jobs.' + $numero + '.jsonl'))) + $lignes
    }
    $entrees = @()
    foreach ($ligne in @($lignes | Select-Object -Last ($Nombre * 3))) {
        try { $entree = $ligne | ConvertFrom-Json } catch { continue }
        if ($null -eq $entree -or -not $script:JournalStatuts.ContainsKey([string]$entree.status) -or -not [string]$entree.job_id) { continue }
        $moment = $null
        foreach ($champ in 'started_at', 'created_at', 'finished_at') { if ($null -eq $moment) { $moment = ConvertTo-MomentJournal ($entree.$champ) } }
        $entrees += [pscustomobject]@{ Moment = $moment; Entree = $entree }
    }
    return @($entrees | Sort-Object -Property @{ Expression = { if ($_.Moment) { $_.Moment } else { [datetime]::MinValue } }; Descending = $true } | Select-Object -First $Nombre)
}

function Show-JournalDesJobs {
    <# Touche J du menu : les 20 derniers jobs faits sur ce Worker (date et heure, statut, durée, job, fichiers créés). #>
    param([string]$Racine, [int]$Nombre = 20)
    Write-Section 'Journal des jobs de ce Worker'
    $liste = @(Get-JournalDesJobs -Racine $Racine -Nombre $Nombre)
    if ($liste.Count -eq 0) {
        Write-Info 'Aucun job enregistré pour l''instant (data\journal-des-jobs.jsonl, écrit par l''agent à la fin de chaque job).'
        return
    }
    $jour = ''
    foreach ($item in $liste) {
        $e = $item.Entree
        $cle = if ($item.Moment) { $item.Moment.ToString('yyyy-MM-dd') } else { '?' }
        if ($cle -ne $jour) {
            $jour = $cle
            $titre = if ($item.Moment) { $item.Moment.ToString('dddd d MMMM yyyy', [Globalization.CultureInfo]::GetCultureInfo('fr-FR')) } else { 'date inconnue' }
            if ($item.Moment -and $item.Moment.Date -eq (Get-Date).Date) { $titre = 'Aujourd''hui — ' + $titre }
            elseif ($item.Moment -and $item.Moment.Date -eq (Get-Date).Date.AddDays(-1)) { $titre = 'Hier — ' + $titre }
            Write-Host ''
            Write-Host ('  ' + $titre) -ForegroundColor Cyan
        }
        $statut = $script:JournalStatuts[[string]$e.status]
        $heure = if ($item.Moment) { $item.Moment.ToString('HH:mm') } else { '--:--' }
        $fichiers = @($e.outputs | Where-Object { $_ -and $_.name } | ForEach-Object { [string]$_.name })
        $cree = if ($fichiers.Count -eq 0) { '' } elseif ($fichiers.Count -le 2) { '  ->  ' + ($fichiers -join ', ') } else { '  ->  ' + ($fichiers[0..1] -join ', ') + ' +' + ($fichiers.Count - 2) }
        $libelle = [string]$e.label
        $place = 96 - 4 - 7 - 9 - 13 - $cree.Length
        if ($place -lt 20) { $cree = ''; $place = 63 }
        if ($libelle.Length -gt $place) { $libelle = $libelle.Substring(0, [Math]::Max(1, $place - 1)) + '…' }
        Write-Host ('    ' + $heure + '  ') -NoNewline -ForegroundColor Gray
        Write-Host ($statut[0].PadRight(9)) -NoNewline -ForegroundColor $statut[1]
        Write-Host ((Format-DureeJournal $e.duration_s).PadLeft(11) + '  ') -NoNewline -ForegroundColor DarkGray
        Write-Host $libelle -NoNewline -ForegroundColor White
        Write-Host $cree -ForegroundColor DarkGray
        if ([string]$e.error) { Write-Host ((' ' * 33) + ([string]$e.error).Substring(0, [Math]::Min(63, ([string]$e.error).Length))) -ForegroundColor DarkYellow }
    }
    Write-Host ''
    Write-Conseil 'Journal complet, filtres par jour et par heure, aperçu des fichiers et suppression : worker.exe, « Journal des jobs ».'
}
