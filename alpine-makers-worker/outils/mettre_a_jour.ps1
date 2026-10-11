<#
    Mettre à jour le Worker : demande au site, par l'agent, la version qu'il propose, puis suit l'installation.

    Usage :
        mettre_a_jour.ps1                                  vérifie, demande ton accord, puis suit la mise à jour
        mettre_a_jour.ps1 -Simulation -SansPause           vérifie et dit ce qui serait demandé ; aucune mise à jour demandée
        mettre_a_jour.ps1 -Oui -SansPause                  sans clavier : accepte la mise à jour vers une version plus récente
        mettre_a_jour.ps1 -Oui -Reinstaller -SansPause     sans clavier : réinstalle aussi les fichiers de la version en place
        -AttenteRetour N : secondes d'attente du retour de l'agent (20 à 240, 240 par défaut).
    Codes de sortie : 0 = fait, ou rien à faire ; 1 = échec ou action impossible ; 2 = refusé ou annulé.

    L'outil ne télécharge rien, n'installe rien et ne demande jamais l'élévation : il dépose une demande à l'agent,
    qui la porte au site ; le site renvoie à l'agent la même commande que son bouton « Mettre à jour ».
    -Oui seul ne lance jamais la réinstallation d'une version déjà en place : il faut -Reinstaller.
    -Oui ne rallume jamais un Worker arrêté : cette question attend une réponse tapée.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause,
    [switch]$Oui,
    [switch]$Simulation,
    [switch]$Reinstaller,
    [ValidateRange(20, 240)][int]$AttenteRetour = 240
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'commun_cycle.ps1')
. (Join-Path $PSScriptRoot 'commun_gestion.ps1')
Initialize-ConsoleWorker -Titre 'Mettre à jour le Worker'
$NomOutil = 'mettre_a_jour'

function Format-Duree {
    <# « 4 minutes », « 40 secondes » : le délai tel qu'il est dit à l'écran. #>
    param([int]$Secondes)
    if ($Secondes -ge 60 -and ($Secondes % 60) -eq 0) {
        if ($Secondes -eq 60) { return '1 minute' }
        return ([string][int]($Secondes / 60) + ' minutes')
    }
    return ([string]$Secondes + ' secondes')
}

function Get-SignatureManifeste {
    <# Ce qui change quand une mise à jour pose son manifeste (posé en dernier) : version, date et taille du fichier. #>
    param([string]$Racine)
    $version = ''
    try { $version = [string](Get-VersionInstallee -Racine $Racine) } catch { }
    try {
        $element = Get-Item -LiteralPath (Join-Path $Racine 'agent_manifest.json') -Force -ErrorAction Stop
        return ($version + '|' + $element.LastWriteTimeUtc.Ticks + '|' + $element.Length)
    } catch { return ($version + '|absent') }
}

function Get-CodeEchecRelais {
    <#
        Code de sortie après une demande au site qui n'a pas abouti :
        1 = action impossible (agent muet, à redémarrer ou trop ancien ; site sans la fonction, injoignable, muet) ;
        2 = refus (le site, ou une garde de l'agent, a dit non : calcul en cours, compte suspendu, maintenance...).
    #>
    param($Resultat)
    foreach ($champ in @('AgentMuet', 'AgentARedemarrer', 'AgentTropAncien', 'SiteSansFonction', 'SiteInjoignable', 'DelaiDepasse')) {
        if ([bool](Get-ChampGestion $Resultat $champ $false)) { return 1 }
    }
    return 2
}

function Confirm-ActionVisible {
    <#
        Confirm-Action du socle, avec la question TOUJOURS visible. Quand l'entrée est un tube (worker.exe, qui
        écrit la réponse par son champ « Réponse à l'outil »), PowerShell n'affiche pas l'invite de Read-Host :
        la question est alors écrite ici, avant d'attendre la réponse. Dans une console, Read-Host l'affiche.
    #>
    param([string]$Question, [string]$MotExact = '', [switch]$Oui)
    if ($Oui) { return $true }
    $entreeRedirigee = $false
    try { $entreeRedirigee = [Console]::IsInputRedirected } catch { }
    if ($entreeRedirigee) {
        if ($MotExact) { Write-Host ('   ' + $Question + ' Tape ' + $MotExact + ' pour confirmer :') -ForegroundColor White }
        else { Write-Host ('   ' + $Question + ' [O/N] :') -ForegroundColor White }
    }
    return (Confirm-Action -Question $Question -MotExact $MotExact)
}

function Write-ConseilPanneauOuvert {
    <#
        Un agent d'avant la version 1.48.0 remplace ses fichiers un par un : si worker.exe (le panneau du Worker)
        est ouvert, sa mise à jour peut s'arrêter sur ce fichier. Dit ici parce que la suite se passe sur le site.
    #>
    Write-Conseil ('L''agent en marche date d''avant la version ' + $script:VersionGestion + ' : une mise à jour lancée depuis le site peut échouer si la fenêtre « worker.exe » (panneau du Worker) est ouverte.')
    if ($env:ALPINE_WORKER_GUI -eq '1') { Write-Conseil 'Tu es dans ce panneau : ferme-le avant de cliquer sur « Mettre à jour » dans Mes Workers.' }
    else { Write-Conseil 'Ferme-la avant de cliquer sur « Mettre à jour » dans Mes Workers.' }
}

try {
    $Racine = Get-RacineWorker -Racine $Racine

    Write-Explication -Titre 'METTRE À JOUR LE WORKER' -Fait @(
        'Demande au site, par l''agent, quelle version il propose pour ce Worker.',
        'Si tu acceptes : le site envoie la mise à jour à l''agent, qui installe ses nouveaux fichiers puis redémarre tout seul (une à deux minutes hors ligne).',
        ('Suit l''installation jusqu''au retour de l''agent (' + (Format-Duree $AttenteRetour) + ' au plus) et dit quelle version tourne.')
    ) -NeFaitPas @(
        'Ne touche ni aux moteurs, ni aux modèles, ni à l''association, ni au partage du Worker.',
        'Ne met rien à jour pendant un calcul ou une gravure : la demande est refusée, et rien n''est changé.',
        'N''installe rien lui-même et ne demande aucune fenêtre administrateur.'
    ) -Exige @(
        'L''agent en marche et le site joignable.',
        'Aucun calcul ni gravure en cours.'
    )
    if ($Simulation) { Write-Alerte 'MODE SIMULATION : aucune mise à jour ne sera demandée.' }

    # ------------------------------------------------------------ 1. l'agent
    Write-Section 'Agent'
    $config = Get-ConfigSure -Racine $Racine
    if (-not $config.Associe) {
        Write-Erreur 'Ce Worker n''est associé à aucun compte : le site ne peut rien lui proposer.'
        Write-Conseil ('Associe-le d''abord avec ' + (Get-NomEntree 8) + '.')
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    $agent = Get-EtatAgent -Racine $Racine
    if (-not $agent.Vivant -and -not $agent.NonSupporte) {
        Write-Alerte ([string]$agent.Message)
        Write-Info 'Sans l''agent, rien ne peut être demandé au site.'
        $rallumer = Join-Path $PSScriptRoot 'rallumer_worker.ps1'
        if ($Simulation -or -not (Test-Path -LiteralPath $rallumer -PathType Leaf)) {
            Write-Conseil ('Rallume-le avec ' + (Get-NomEntree 12) + ', puis relance cet outil.')
            Exit-Outil -Code 1 -SansPause:$SansPause
        }
        Write-Info 'Si le Worker tourne en service Windows, le rallumer ouvre une fenêtre administrateur : Windows te demandera ton accord.'
        # Jamais levée par -Oui : un Worker arrêté l'est peut-être volontairement.
        if (Confirm-ActionVisible -Question ('Le rallumer maintenant avec ' + (Get-NomEntree 12) + ' ?')) {
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'agent muet : « Rallumer le Worker » lancé sur accord'
            # Sous worker.exe, « Interrompre » arrête l'outil et tout ce qu'il a lancé : tant que l'outil appelé n'a pas fini,
            # le Worker qu'il vient de démarrer en fait partie.
            if ($env:ALPINE_WORKER_GUI -eq '1') { Write-Info 'Laisse-le aller au bout : l''interrompre (ou fermer ce panneau) pendant qu''il remet le Worker en marche peut arrêter le Worker avec lui.' }
            $codeRallumage = Invoke-OutilEnfant -Script $rallumer -Arguments @('-Racine', $Racine, '-Oui', '-SansPause')
            Write-Host ''
            if ($codeRallumage -eq 3) { Write-Alerte 'Autorisation administrateur refusée : le Worker n''a pas été rallumé.' }
            elseif ($codeRallumage -ne 0) { Write-Alerte ((Get-NomEntree 12) + ' s''est terminé avec le code ' + $codeRallumage + ' : lis ses messages ci-dessus.') }
            Write-Conseil 'Relance cet outil dans une minute.'
        } else {
            Write-Conseil ('Rallume-le avec ' + (Get-NomEntree 12) + ', puis relance cet outil.')
        }
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    if (-not $agent.Capable) {
        # Agent en mémoire plus ancien que ses fichiers (à redémarrer), ou fichiers trop anciens : message figé.
        Write-RefusRelais -Resultat $agent
        Write-ConseilPanneauOuvert
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    $statut = $agent.Statut
    $versionActive = ([string](Get-ChampGestion $statut 'agent_version' '')).Trim()
    $versionInstallee = ''
    try { $versionInstallee = ([string](Get-VersionInstallee -Racine $Racine)).Trim() } catch { }
    if ($null -eq (Compare-VersionWorker $versionActive $versionActive)) { $versionActive = $versionInstallee }
    Write-Etat -Libelle 'Agent' -Valeur ('répond (version ' + $versionActive + ')') -Niveau ok

    # ------------------------------------------------------------ 2. le site
    Write-Section 'Version proposée par le site'
    Write-Info 'Lecture de l''état de ce Worker sur le site (20 secondes au plus)...'
    $site = Get-EtatSite -Racine $Racine -Rafraichir -DelaiSecondes 20
    if (-not $site.Connu) {
        Write-RefusRelais -Resultat $site
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    $proposee = ([string](Get-ChampGestion (Get-ChampGestion $site.Site 'worker') 'available_agent_version' '')).Trim()
    Write-Etat -Libelle 'Version en marche' -Valeur $versionActive -Niveau info
    if ($versionInstallee -and $versionInstallee -ne $versionActive) { Write-Etat -Libelle 'Fichiers installés' -Valeur ($versionInstallee + ' (l''agent en marche n''a pas encore redémarré dessus)') -Niveau alerte }
    $ordre = Compare-VersionWorker $proposee $versionActive
    $plusRecente = ($null -ne $ordre -and $ordre -gt 0)
    if ($plusRecente) { Write-Etat -Libelle 'Version proposée' -Valeur ($proposee + ' : plus récente') -Niveau alerte }
    elseif ($null -eq $ordre) { Write-Etat -Libelle 'Version proposée' -Valeur 'non indiquée par le site' -Niveau info }
    else { Write-Etat -Libelle 'Version proposée' -Valeur ($proposee + ' : le Worker est à jour') -Niveau ok }
    $cible = $versionActive
    if ($plusRecente) { $cible = $proposee }

    $compte = Get-ChampGestion $site.Site 'account'
    if ((Get-ChampGestion $compte 'can_act' $true) -eq $false) {
        Write-Alerte 'Le site refuse les demandes de ce Worker : le compte propriétaire n''y a plus le droit d''agir (accès suspendu, ou à confirmer). Rien n''a été demandé.'
        Write-Conseil 'Connecte-toi au site avec ce compte pour voir ce qui manque.'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    if ((Get-ChampGestion $compte 'maintenance' $false) -eq $true) {
        Write-Alerte 'Le site est en maintenance : la mise à jour ne peut pas être demandée pour l''instant. Réessaie plus tard.'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }

    # ------------------------------------------------------------ 3. ce qui s'y oppose
    Write-Section 'Avant de mettre à jour'
    $gravures = @(@(Get-ChampGestion $statut 'grbl_jobs') | Where-Object { $_ } | ForEach-Object { ([string]$_) -replace '[\x00-\x1F\x7F]', ' ' })
    $calculs = @(@(Get-ChampGestion $statut 'jobs_active') | Where-Object { $_ })
    $maintenance = [bool](Get-ChampGestion $statut 'maintenance_active' $false)
    if ($gravures.Count -gt 0) {
        Write-Etat -Libelle 'Gravure en cours' -Valeur ($gravures -join ', ') -Niveau erreur
        Write-Erreur 'Une gravure est en cours : mettre à jour redémarrerait l''agent au milieu de la gravure. Rien n''a été demandé.'
        Write-Conseil 'Attends la fin de la gravure, puis relance cet outil.'
        Exit-Outil -Code 2 -SansPause:$SansPause
    }
    if ($calculs.Count -gt 0) {
        Write-Etat -Libelle 'Calculs en cours' -Valeur ([string]$calculs.Count + ' : la mise à jour sera refusée tant qu''un calcul tourne vraiment') -Niveau alerte
        Write-Conseil ('Pour les voir : ' + (Get-NomEntree 2) + '. Pour les arrêter : ' + (Get-NomEntree 21) + '.')
    }
    if ($maintenance) { Write-Etat -Libelle 'Maintenance' -Valeur 'une installation ou une réparation est en cours : la mise à jour attendra sa fin, ou sera refusée' -Niveau alerte }
    if ($calculs.Count -eq 0 -and -not $maintenance) { Write-Etat -Libelle 'Calcul, gravure, maintenance' -Valeur 'rien en cours' -Niveau ok }

    # ------------------------------------------------------------ 4. la décision
    Write-Host ''
    if ($Simulation) {
        if ($plusRecente) { Write-Info ('[simulation] La mise à jour vers ' + $cible + ' serait demandée au site, après ton accord.') }
        else { Write-Info ('[simulation] Le Worker est à jour (' + $cible + ') : seule la réinstallation de ses fichiers serait proposée.') }
        Write-Ok 'Simulation terminée : aucune mise à jour n''a été demandée, rien n''a changé.'
        Exit-Outil -Code 0 -SansPause:$SansPause
    }
    if ($plusRecente) {
        if (-not (Confirm-ActionVisible -Question ('Mettre à jour vers ' + $cible + ' ? L''agent redémarre tout seul (une à deux minutes hors ligne).') -Oui:$Oui)) {
            Write-Info 'Annulé : rien n''a été demandé au site.'
            Exit-Outil -Code 2 -SansPause:$SansPause
        }
    } else {
        if ($Oui -and -not $Reinstaller) {
            Write-Ok ('Le Worker est à jour (' + $cible + ') : rien à faire.')
            Write-Conseil 'Pour réinstaller quand même les fichiers de cette version (fichier manquant ou modifié), relance avec -Reinstaller.'
            Exit-Outil -Code 0 -SansPause:$SansPause
        }
        if (-not (Confirm-ActionVisible -Question ('Le Worker est à jour (' + $cible + '). Réinstaller les fichiers de cette version (répare un fichier manquant ou modifié) ?') -Oui:($Oui -and $Reinstaller))) {
            Write-Info 'Rien n''a été demandé au site : le Worker reste tel quel, à jour.'
            Exit-Outil -Code 0 -SansPause:$SansPause
        }
    }

    # ------------------------------------------------------------ 5. la demande
    Write-Section 'Demande au site'
    $pidDepart = ConvertTo-EntierGestion (Get-ChampGestion $statut 'agent_pid') 2147483647
    $signatureDepart = Get-SignatureManifeste -Racine $Racine
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('mise à jour demandée au site : version en marche ' + $versionActive + ', version visée ' + $cible)
    $relais = Invoke-RelaisSite -Racine $Racine -Action 'site-update' -DelaiSecondes 60
    if (-not $relais.Ok) {
        Write-RefusRelais -Resultat $relais
        if ([string]$relais.CodeSite -eq 'WORKER_BUSY') { Write-Conseil ('Pour voir ce qui tourne : ' + (Get-NomEntree 2) + '. Pour l''arrêter : ' + (Get-NomEntree 21) + '.') }
        $codeEchec = Get-CodeEchecRelais $relais
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('demande non aboutie (réponse ' + [int]$relais.Http + ' ' + [string]$relais.CodeSite + '), code ' + $codeEchec)
        Exit-Outil -Code $codeEchec -SansPause:$SansPause
    }
    Write-Ok 'Le site a mis la mise à jour en file : l''agent la reçoit dans la minute, l''installe, puis redémarre.'

    # ------------------------------------------------------------ 6. le suivi
    Write-Section ('Installation (' + (Format-Duree $AttenteRetour) + ' au plus)')
    Write-Info 'Un point toutes les 5 secondes. Pendant son redémarrage, l''agent ne répond plus : c''est normal.'
    # Fichiers et processus inchangés après ce délai : la mise à jour n'a pas été installée (refusée ou reportée par l'agent).
    $seuilConstat = [int][Math]::Min(90, [Math]::Floor($AttenteRetour * 0.375))
    $debut = Get-Date
    $fin = $debut.AddSeconds($AttenteRetour)
    $issue = 'delai'; $versionVue = ''; $pidVu = 0
    while ((Get-Date) -lt $fin) {
        $tour = Get-Date
        $vu = Get-EtatAgent -Racine $Racine -DelaiSecondes 4
        if ($vu.Vivant -and $vu.Statut) {
            $pidVu = ConvertTo-EntierGestion (Get-ChampGestion $vu.Statut 'agent_pid') 2147483647
            $versionVue = ([string](Get-ChampGestion $vu.Statut 'agent_version' '')).Trim()
            if ($pidVu -gt 0 -and $pidVu -ne $pidDepart -and $versionVue -eq $cible) { $issue = 'fait'; break }
            if (((Get-Date) - $debut).TotalSeconds -ge $seuilConstat -and $pidVu -eq $pidDepart -and (Get-SignatureManifeste -Racine $Racine) -eq $signatureDepart) { $issue = 'non-installee'; break }
        }
        Write-Host '.' -NoNewline -ForegroundColor DarkGray
        $reste = 5 - ((Get-Date) - $tour).TotalSeconds
        if ($reste -gt 0) { Start-Sleep -Milliseconds ([int]($reste * 1000)) }
    }
    Write-Host ''

    # ------------------------------------------------------------ 7 et 8. la fin
    if ($issue -eq 'fait') {
        Write-Ok ('Mise à jour installée : l''agent a redémarré et tourne en version ' + $versionVue + '.')
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('mise à jour installée : l''agent tourne en version ' + $versionVue)
        # Même contrôle que celui de l'agent à son démarrage (qu'il ne fait que vingt secondes plus tard).
        $abimes = 0
        $integrite = Test-IntegriteAgent -Racine $Racine
        if ($integrite.Connu) { $abimes = @($integrite.Manquants).Count + @($integrite.Modifies).Count }
        else {
            $vue = Get-ChampGestion (Get-EtatAgent -Racine $Racine -DelaiSecondes 4).Statut 'integrity'
            $abimes = (ConvertTo-EntierGestion (Get-ChampGestion $vue 'missing')) + (ConvertTo-EntierGestion (Get-ChampGestion $vue 'modified'))
        }
        if ($abimes -gt 0) {
            Write-Alerte ([string]$abimes + ' fichier(s) de l''agent ne correspondent pas à cette version (un fichier ouvert pendant la mise à jour n''a pas pu être remplacé).')
            Write-Conseil ('Pour les réinstaller : ' + (Get-NomEntree 18) + '.')
        }
        if ($env:ALPINE_WORKER_GUI -eq '1') { Write-Conseil 'Le panneau lui-même a pu être remplacé : ferme et rouvre worker.exe pour utiliser sa nouvelle version.' }
        Exit-Outil -Code 0 -SansPause:$SansPause
    }
    if ($issue -eq 'non-installee') {
        Write-Erreur 'La mise à jour n''a pas été installée (un fichier de l''agent est peut-être utilisé par un autre programme). Regarde le message sur la carte du Worker dans Mes Workers.'
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'ÉCHEC : fichiers et agent inchangés, mise à jour non installée'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    if ($pidVu -gt 0 -and $pidVu -ne $pidDepart) {
        Write-Erreur ('L''agent a redémarré, mais il tourne en version ' + $versionVue + ' et non ' + $cible + '.')
    } else {
        Write-Erreur ('La mise à jour a été demandée mais l''agent n''est pas revenu en ' + (Format-Duree $AttenteRetour) + '.')
    }
    Write-Conseil ('Regarde ' + (Get-NomEntree 1) + ', puis ' + (Get-NomEntree 3) + '.')
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('ÉCHEC : la version ' + $cible + ' n''est pas active après ' + $AttenteRetour + ' s')
    Exit-Outil -Code 1 -SansPause:$SansPause
} catch {
    Write-Erreur ('Erreur inattendue : ' + [string]$_.Exception.Message)
    Exit-Outil -Code 1 -SansPause:$SansPause
}
