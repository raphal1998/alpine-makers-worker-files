<#
    Arrêter tous les jobs de CE Worker (entrée 21 du menu, rubrique « Gestion du Worker »).

    Demande l'arrêt des calculs en cours et en attente de ce Worker, de ses installations en cours et des
    demandes pas encore transmises. La demande part au SITE par l'agent (action locale « jobs-stop », route
    signée) : c'est le site qui annule sa file et renvoie les arrêts à l'agent. Si le site ne répond pas,
    l'agent demande lui-même l'arrêt des calculs en cours sur ce PC ; l'outil dit alors ce qui reste à faire.

    Cet outil ne commande AUCUN équipement : ni graveuse, ni imprimante, ni caméra, ni moteur. Une gravure en
    cours est nommée, jamais arrêtée. Il n'arrête aucun processus lui-même et ne demande aucune élévation.
    Les seules actions locales déposées sont « status », « jobs-stop » et « relay-status ».

    Pilotage sans clavier (essais ; worker.exe ne transmet que -SansPause) :
        arreter_jobs.ps1 -Simulation -SansPause            montre l'état, n'envoie rien
        arreter_jobs.ps1 -Oui -SansPause [-SuiviSecondes 60]
    Codes de sortie : 0 arrêt demandé au site ; 1 échec ou arrêt incomplet (agent arrêté ou trop ancien,
    site injoignable ou sans la fonction, refus du site) ; 2 annulé.
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
    <# Noms de machines venus de l'agent : sans caractère de contrôle, 80 caractères et 20 noms au plus. #>
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
    <# Agent muet : aucun calcul ne peut être piloté. Lecture seule du journal et des ports, puis les renvois. #>
    param([string]$Racine)
    Write-Info 'Sans réponse de l''agent, aucun calcul ne peut être arrêté d''ici.'
    Write-Section 'Ce que ce PC montre encore'
    Show-JobsActifs -Jobs (Get-JobsActifs -Racine $Racine)
    $lancement = $null
    try { $lancement = Get-ModeLancement -Racine $Racine } catch { }
    # Les ports des moteurs sont ceux du PC : si un autre Worker tourne ici, ce sont les siens.
    if ($lancement -and (Test-ServiceAutreDossier -Mode $lancement)) { return }
    $ouverts = @()
    foreach ($m in @(Get-EtatMoteurs -Racine $Racine)) { if ($m.EnEcoute) { $ouverts += [string]$m.Nom } }
    if ($ouverts.Count -eq 0) { Write-Etat -Libelle 'Moteurs encore ouverts' -Valeur 'aucun' -Niveau ok }
    else {
        Write-Etat -Libelle 'Moteurs encore ouverts' -Valeur ($ouverts -join ', ') -Niveau alerte
        Write-Conseil ('Si le Worker est bien éteint, ces moteurs sont restés seuls : ferme-les avec ' + (Get-NomEntree 11) + '.')
    }
}

function Wait-FinDesCalculs {
    <# Relit « status » jusqu'à ce que plus aucun calcul ne soit en cours, ou jusqu'au délai. Rend le nombre restant ; -1 si l'agent ne répond plus. #>
    param([string]$Racine, [int]$DelaiSecondes)
    $limite = (Get-Date).AddSeconds($DelaiSecondes)
    $restants = -1
    $points = $false
    while ($true) {
        $restants = -1
        $etat = Get-EtatAgent -Racine $Racine -DelaiSecondes 8
        if ($etat.Vivant) {
            $liste = Get-ListeChamp $etat.Statut 'jobs_active'
            if ($liste.Connu) { $restants = [int]$liste.Elements.Count }
        }
        if ($restants -eq 0 -or (Get-Date) -ge $limite) { break }
        # Sous worker.exe la sortie est un tube : des points, jamais de retour au début de la ligne.
        Write-Host '.' -NoNewline -ForegroundColor DarkGray
        $points = $true
        Start-Sleep -Seconds 3
    }
    if ($points) { Write-Host '' }
    return $restants
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
    Write-Conseil ('Moteurs (ComfyUI, Hunyuan3D, PrintGuard, bridge Orca) : ils restent allumés   (pour les arrêter : ' + (Get-NomEntree 9) + ')')
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

try {
    Write-Explication -Titre 'ARRÊTER TOUS LES JOBS' -Fait @(
        'Demande l''arrêt de tous les calculs en cours et en attente de CE Worker (images, 3D, conversions).',
        'Demande aussi l''arrêt de ses installations en cours, et annule les demandes pas encore transmises (mise à jour ou réparation demandée incluse).',
        'La demande part au site par l''agent. Si le site ne répond pas, l''arrêt est demandé directement aux calculs en cours sur ce PC.'
    ) -NeFaitPas @(
        'Il n''arrête JAMAIS une graveuse, une imprimante 3D ni un autre équipement : cela se fait sur la machine elle-même.',
        'Il n''éteint ni les moteurs (ComfyUI, Hunyuan3D, PrintGuard, bridge Orca), ni les caméras, ni la surveillance IA.',
        'Il ne touche pas aux jobs en attente pour tes autres Workers.'
    ) -Exige @(
        'Le Worker allumé et associé à ton compte. Aucune fenêtre administrateur.'
    )

    $config = Get-ConfigSure -Racine $Racine
    $agent = Get-EtatAgent -Racine $Racine
    $gravures = Get-ListeChamp $agent.Statut 'grbl_jobs'

    if (-not $agent.Vivant -or -not $agent.Capable) {
        Write-Section 'Arrêt impossible d''ici'
        if (-not $config.Associe) {
            Write-Alerte 'Ce Worker n''est pas associé à un compte : il ne reçoit aucun job du site.'
            Write-Conseil ('Pour l''associer : ' + (Get-NomEntree 8) + '.')
        }
        else { Write-RefusRelais -Resultat $agent }
        Write-Info 'Rien n''a été arrêté.'
        if ($agent.Vivant) {
            # L'agent répond mais ne connaît pas encore cette action (trop ancien, ou à redémarrer).
            Write-Conseil 'En attendant, utilise le bouton « Arrêter les jobs » du site (Mes Workers, carte de ce Worker) : lui annule aussi les calculs de ton compte pas encore attribués à un Worker.'
            Show-EtatActuel -Statut $agent.Statut
        }
        elseif ($config.Associe) { Show-AgentArrete -Racine $Racine }
        Show-NonArrete -Gravures $gravures
        if (-not $Simulation) { Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'arrêt des jobs impossible : agent arrêté, trop ancien ou Worker non associé' }
        Exit-Outil -Code 1 -SansPause:$SansPause
    }

    Show-EtatActuel -Statut $agent.Statut
    Write-Host ''
    Write-Info $SousTexteArret

    if ($Simulation) {
        Write-Info '[simulation] La demande d''arrêt n''a pas été envoyée : rien n''a changé.'
        Show-NonArrete -Gravures $gravures
        Exit-Outil -Code 0 -SansPause:$SansPause
    }
    if (-not (Confirm-Question -Question $QuestionArret -Oui:$Oui)) {
        Write-Info 'Annulé : rien n''a été arrêté.'
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'annulé par l''utilisateur'
        Exit-Outil -Code 2 -SansPause:$SansPause
    }

    Write-Section 'Arrêt'
    Write-Info 'Demande d''arrêt transmise au site par l''agent...'
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

    if (-not $relais.Ok -and -not $repli) {
        # Refus du site ou de l'agent, ou pas de réponse : aucun calcul n'a reçu de demande d'arrêt.
        Write-RefusRelais -Resultat $relais
        if (-not $relais.DelaiDepasse) { Write-Info 'Aucun calcul n''a été arrêté par cet outil.' }
        if ($controleArrete) { Write-Ok 'Contrôle des doublons lancé depuis ce PC : arrêt demandé.' }
        Show-NonArrete -Gravures $gravures
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('ÉCHEC : demande non aboutie (erreur ' + [int]$relais.Http + ' ' + [string]$relais.CodeSite + ')')
        Exit-Outil -Code 1 -SansPause:$SansPause
    }

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
        # Repli : le site n'a pas pu être joint (ou n'a pas encore la fonction), l'agent a agi seul sur ce PC.
        Write-RefusRelais -Resultat $relais
        $locaux = Get-Compteur $local 'jobs_cancel_requested'
        Write-Compte $locaux ('Arrêt demandé directement à ' + $locaux + ' calcul(s) en cours sur ce PC, sans passer par le site.')
        if ($repli -eq 'unreachable') { Write-Conseil 'Relance cet outil quand le site répond de nouveau : sa file d''attente et les installations n''ont pas été touchées.' }
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('site non joint (' + $repli + ') : arrêt local demandé à ' + $locaux + ' calcul(s)')
    }
    if ($controleArrete) { Write-Ok 'Contrôle des doublons lancé depuis ce PC : arrêt demandé.' }

    if ($SuiviSecondes -gt 0) {
        Write-Section ('Suivi (' + $SuiviSecondes + ' secondes au plus)')
        $restants = Wait-FinDesCalculs -Racine $Racine -DelaiSecondes $SuiviSecondes
        if ($restants -eq 0) { Write-Ok 'Plus aucun calcul en cours sur ce Worker.' }
        elseif ($restants -gt 0) {
            Write-Alerte ([string]$restants + ' job(s) attendent encore la confirmation d''arrêt de leur moteur')
            Write-Conseil ('Pour les suivre : ' + (Get-NomEntree 2) + '.')
        }
        else { Write-Alerte ('L''agent ne répond plus : la fin des calculs n''a pas pu être suivie. Vérifie avec ' + (Get-NomEntree 1) + '.') }
    }

    Show-NonArrete -Gravures $gravures -Resultat $resultat -Repli $repli
    if ($relais.Ok) { Exit-Outil -Code 0 -SansPause:$SansPause }
    Exit-Outil -Code 1 -SansPause:$SansPause
} catch {
    Write-Erreur ('Erreur inattendue : ' + $_.Exception.Message)
    if (-not $Simulation) { try { Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('erreur inattendue : ' + $_.Exception.Message) } catch { } }
    Exit-Outil -Code 1 -SansPause:$SansPause
}
