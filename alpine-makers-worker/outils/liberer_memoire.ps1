<#
    Libérer la mémoire (RAM) du PC sans couper Alpine Makers : enveloppe expliquée de
    free_resources.ps1 (à la racine du Worker).

    Déroulé : 1. simulation (-DryRun) depuis TA session ; 2. présentation de ce qui serait
    fermé ; 3. confirmation ; 4. passage réel, avec en option les services Windows non
    essentiels (fenêtre administrateur).

    Usage :
      liberer_memoire.ps1                     interactif
      liberer_memoire.ps1 -Simulation         s'arrête après la simulation (alias : -DryRun)
      liberer_memoire.ps1 -Oui                ferme sans poser de question
      liberer_memoire.ps1 -Oui -AvecServices  ferme aussi les services Windows non essentiels (administrateur)
      -SansPause : pas d'attente clavier à la fin.

    Fenêtre DÉJÀ administrateur : free_resources.ps1 y arrête d'office les services non
    essentiels. Sans -AvecServices (ou un accord explicite à l'écran), l'outil refuse (code 2).
    La fenêtre élevée refait TOUJOURS la simulation et redemande l'accord, sauf si -Oui a été
    demandé par toi au lancement.
    Codes : 0 succès, 1 échec ou libération incomplète, 2 refusé/annulé, 3 élévation refusée.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause,
    [switch]$Oui,
    [Alias('DryRun')][switch]$Simulation,
    [switch]$AvecServices,
    [switch]$DejaEleve
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
Initialize-ConsoleWorker -Titre 'Libérer la mémoire du PC'
$Racine = Get-RacineWorker -Racine $Racine

$NomOutil = 'liberer_memoire'
$scriptLiberation = Join-Path $Racine 'free_resources.ps1'

function Format-Mio {
    param($Mio)
    if ($null -eq $Mio -or [string]$Mio -eq '') { return 'inconnu' }
    $v = [double]$Mio
    if ($v -ge 1024) { return ('{0:N1} Gio' -f ($v / 1024)) }
    return ('{0:N0} Mio' -f $v)
}

function Invoke-Liberation {
    <# Lance free_resources.ps1 et renvoie son rapport (DERNIÈRE ligne JSON), ou $null. #>
    param([switch]$Essai)
    $arguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $scriptLiberation)
    if ($Essai) { $arguments += '-DryRun' }
    $ancien = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    $sortie = & powershell.exe @arguments 2>$null
    $ErrorActionPreference = $ancien
    $json = ($sortie | ForEach-Object { [string]$_ } | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1)
    if (-not $json) { return $null }
    try { return ($json | ConvertFrom-Json) } catch { return $null }
}

function Show-Memoire {
    param($Mesure, [string]$Titre)
    if (-not $Mesure) { return }
    Write-Etat -Libelle ($Titre + ' : mémoire vive (RAM)') -Valeur ((Format-Mio $Mesure.ram_used_mb) + ' utilisés sur ' + (Format-Mio $Mesure.ram_total_mb)) -Niveau info
    if ($null -ne $Mesure.vram_total_mb) {
        Write-Etat -Libelle ($Titre + ' : carte graphique (VRAM)') -Valeur ((Format-Mio $Mesure.vram_used_mb) + ' utilisés sur ' + (Format-Mio $Mesure.vram_total_mb)) -Niveau info
    }
}

function Show-Conserves {
    param($Rapport)
    $gardes = @($Rapport.kept | Where-Object { $_ })
    if ($gardes.Count -eq 0) { return }
    Write-Section 'Ce qui est conservé (nombre de programmes)'
    foreach ($g in @($gardes | Select-Object -First 10)) { Write-Info ('     ' + ([string]$g.count).PadLeft(4) + '  ' + [string]$g.reason) }
}

function Show-AFermer {
    <# Présente la liste d'une simulation : ce qui SERAIT fermé par un passage réel lancé depuis CETTE fenêtre. #>
    param($Rapport)
    $aFermer = @($Rapport.closed | Where-Object { $_ })
    Write-Section ('Ce qui SERAIT fermé : ' + [string]$Rapport.closed_count + ' programme(s)')
    if ([int]$Rapport.closed_count -eq 0) {
        Write-Ok 'Rien de superflu à fermer dans ta session.'
    } else {
        $sommeRam = [double]0
        foreach ($p in $aFermer) { $sommeRam += [double]$p.ram_mb }
        $montres = 0
        foreach ($p in $aFermer) {
            if ($montres -ge 25) { Write-Info ('     ... et ' + ([int]$Rapport.closed_count - $montres) + ' autre(s), plus petits'); break }
            Write-Info ('     ' + (Format-Mio $p.ram_mb).PadLeft(9) + '  ' + ([string]$p.name).PadRight(28) + ' ' + [string]$p.reason)
            $montres++
        }
        Write-Host ''
        Write-Etat -Libelle 'Mémoire vive concernée (environ)' -Valeur (Format-Mio $sommeRam) -Niveau info
    }
    Show-Conserves -Rapport $Rapport
}

function Get-ServicesVises {
    <#
        Services Windows EN COURS que free_resources.ps1 arrêterait dans une fenêtre
        administrateur. Sa simulation ne les liste pas : on relit SA règle
        ($reServicesNonEssentiels) dans son code, sans l'exécuter ni la recopier ici.
        Renvoie : Connue ($false si la règle est introuvable), Services (Nom, Libelle).
    #>
    $retour = [pscustomobject]@{ Connue = $false; Services = @() }
    $regle = ''
    try {
        $erreursAnalyse = $null
        $arbre = [System.Management.Automation.Language.Parser]::ParseFile($scriptLiberation, [ref]$null, [ref]$erreursAnalyse)
        $affectation = $arbre.Find({
            param($n)
            ($n -is [System.Management.Automation.Language.AssignmentStatementAst]) -and
            ($n.Left -is [System.Management.Automation.Language.VariableExpressionAst]) -and
            ($n.Left.VariablePath.UserPath -ieq 'reServicesNonEssentiels')
        }, $true)
        if ($affectation) {
            $constante = $affectation.Right.Find({ param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true)
            if ($constante) { $regle = [string]$constante.Value }
        }
    } catch { $regle = '' }
    if (-not $regle) { return $retour }
    $retour.Connue = $true
    $liste = @()
    foreach ($s in @(Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Running' })) {
        if (([string]$s.Name).ToLower() -match $regle) { $liste += [pscustomobject]@{ Nom = [string]$s.Name; Libelle = [string]$s.DisplayName } }
    }
    $retour.Services = @($liste | Sort-Object Nom)
    return $retour
}

function Show-ServicesVises {
    $vises = Get-ServicesVises
    Write-Section 'Services Windows qui SERAIENT arrêtés (fenêtre administrateur)'
    if (-not $vises.Connue) {
        Write-Alerte 'Liste précise indisponible : ce sont les services non essentiels en cours (indexation, télémétrie, Xbox, synchronisation, découverte réseau...).'
        return
    }
    if (@($vises.Services).Count -eq 0) { Write-Ok 'Aucun service non essentiel n''est en cours : rien à arrêter de ce côté.'; return }
    foreach ($s in $vises.Services) { Write-Info ('     ' + $s.Nom.PadRight(28) + ' ' + $s.Libelle) }
    Write-Info 'Rien n''est désactivé : ils reviennent au prochain redémarrage du PC.'
}

function Show-Resultat {
    <# Résultat d'un passage RÉEL : ce qui a été fermé (nom + raison), services arrêtés, erreurs. Renvoie le code de sortie. #>
    param($Rapport, [string]$Mention = '')
    Show-Memoire -Mesure $Rapport.before -Titre 'Avant'
    Show-Memoire -Mesure $Rapport.after -Titre 'Après'
    $fermes = @($Rapport.closed | Where-Object { $_ })
    Write-Section ('Programmes fermés : ' + [string]$Rapport.closed_count)
    foreach ($p in $fermes) { Write-Info ('     ' + (Format-Mio $p.ram_mb).PadLeft(9) + '  ' + ([string]$p.name).PadRight(28) + ' ' + [string]$p.reason) }
    if ([int]$Rapport.closed_count -gt $fermes.Count) { Write-Info ('     ... et ' + ([int]$Rapport.closed_count - $fermes.Count) + ' autre(s), plus petits') }
    $services = @($Rapport.services_stopped | Where-Object { $_ })
    if ($services.Count -gt 0) {
        Write-Section ('Services Windows arrêtés : ' + $services.Count)
        foreach ($s in $services) { Write-Info ('     ' + [string]$s) }
    }
    if ($Rapport.services_skipped) { Write-Info ([string]$Rapport.services_skipped) }
    Write-Host ''
    Write-Etat -Libelle 'Mémoire vive rendue' -Valeur (Format-Mio $Rapport.freed_ram_mb) -Niveau ok
    $erreurs = @($Rapport.errors | Where-Object { $_ })
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('Libération' + $Mention + ' : ' + $Rapport.closed_count + ' programmes fermés, ' + $services.Count + ' services arrêtés, ' + (Format-Mio $Rapport.freed_ram_mb) + ' rendus, ' + $erreurs.Count + ' erreur(s)')
    if ($erreurs.Count -gt 0) {
        Write-Section ('Ce qui n''a pas pu être fermé : ' + $erreurs.Count)
        foreach ($err in $erreurs) { Write-Alerte ([string]$err) }
        Write-Erreur 'La libération est INCOMPLÈTE : les éléments ci-dessus sont restés ouverts.'
        return 1
    }
    Write-Ok 'Libération terminée sans erreur.'
    return 0
}

# ---------------------------------------------------------------------------

Write-Explication -Titre 'LIBÉRER LA MÉMOIRE DU PC' -Fait @(
    'Ferme les applications et scripts superflus de TA session Windows (navigateurs, lanceurs de jeux, logiciels d''éclairage, consoles oubliées...) pour rendre de la mémoire vive.',
    'Commence TOUJOURS par une simulation : tu vois la liste avant que quoi que ce soit ne soit fermé.',
    'En option, dans une fenêtre administrateur : arrête aussi des services Windows non essentiels (indexation, télémétrie, Xbox...). Un redémarrage du PC remet tout en place.'
) -NeFaitPas @(
    'Ne ferme RIEN d''Alpine Makers : Worker et moteurs, dashboard, boutique et API, MongoDB, courrier, tunnels Cloudflare, Docker/WSL, Git Sync, ni Claude.',
    'Ne touche pas à Windows, à l''antivirus, au pare-feu, à l''accès à distance, au watercooling ni aux ventilateurs.',
('Ne libère presque pas la mémoire de la carte graphique (VRAM) : pour cela, arrête les moteurs IA avec ' + (Get-NomEntree 9) + '.'),
    'Ne désinstalle et ne désactive rien.'
) -Exige @(
    'Enregistre ton travail avant : les applications listées seront fermées sans demander.',
    'Une fenêtre NORMALE (non administrateur) pour fermer seulement les programmes : lancé depuis une fenêtre administrateur, free_resources.ps1 arrête TOUJOURS aussi les services Windows non essentiels.',
    'Les droits administrateur uniquement pour l''option « services Windows ».'
)

if (-not (Test-Path -LiteralPath $scriptLiberation -PathType Leaf)) {
    Write-Erreur 'free_resources.ps1 est absent de la racine du Worker.'
    Write-Conseil 'Mets le Worker à jour depuis le dashboard : ce script fait partie du paquet de l''agent.'
    Wait-FinOutil -SansPause:$SansPause
    exit 1
}

$estAdmin = Test-Administrateur

# --- Instance relancée en administrateur ---------------------------------------------------
# Une fois élevé, free_resources.ps1 lit le chemin de processus qu'il classait « non
# identifiable » dans la session normale : la liste peut donc S'ALLONGER. On refait la
# simulation ICI et on redemande l'accord sur CETTE liste avant tout passage réel.
if ($DejaEleve) {
    if (-not $estAdmin) {
        Write-Erreur 'Cette fenêtre n''a pas les droits administrateur : les services Windows ne pourraient pas être arrêtés. Rien n''a été fermé.'
        Wait-FinOutil -SansPause:$SansPause
        exit 1
    }
    Write-Section 'Fenêtre administrateur : nouvelle simulation (rien n''est fermé)'
    Write-Info 'Avec les droits administrateur, davantage de programmes deviennent identifiables : la liste ci-dessous peut être plus longue que la précédente.'
    $essaiEleve = Invoke-Liberation -Essai
    if (-not $essaiEleve) { Write-Erreur 'La simulation n''a rendu aucun rapport lisible : par prudence, rien ne sera fermé.'; Wait-FinOutil -SansPause:$SansPause; exit 1 }
    Show-Memoire -Mesure $essaiEleve.before -Titre 'Maintenant'
    Show-AFermer -Rapport $essaiEleve
    Show-ServicesVises
    if ($Simulation) {
        Write-Host ''
        Write-Alerte 'SIMULATION : rien n''a été fermé, aucun service n''a été arrêté.'
        Wait-FinOutil -SansPause:$SansPause
        exit 0
    }
    Write-Section 'Confirmation (fenêtre administrateur)'
    Write-Alerte 'Enregistre ton travail : les programmes ET les services listés ci-dessus seront arrêtés d''office.'
    if (-not (Confirm-Action -Question 'Fermer ces programmes et arrêter ces services maintenant ?' -Oui:$Oui)) {
        Write-Info 'Annulé : rien n''a été fermé.'
        Wait-FinOutil -SansPause:$SansPause
        exit 2
    }
    Write-Section 'Passage réel (administrateur)'
    $rapport = Invoke-Liberation
    if (-not $rapport) { Write-Erreur 'free_resources.ps1 n''a rendu aucun rapport : impossible de dire ce qui a été fermé.'; Wait-FinOutil -SansPause:$SansPause; exit 1 }
    $codeEleve = Show-Resultat -Rapport $rapport -Mention ' (administrateur)'
    Wait-FinOutil -SansPause:$SansPause
    exit $codeEleve
}

# --- 1. Simulation -------------------------------------------------------------------
Write-Section 'Étape 1 : simulation (rien n''est fermé)'
Write-Info 'Analyse des programmes de ta session... (quelques secondes)'
$essai = Invoke-Liberation -Essai
if (-not $essai) {
    Write-Erreur 'La simulation n''a rendu aucun rapport lisible : par prudence, rien ne sera fermé.'
    Wait-FinOutil -SansPause:$SansPause
    exit 1
}
Show-Memoire -Mesure $essai.before -Titre 'Maintenant'
Show-AFermer -Rapport $essai
if ($estAdmin) {
    # Fenêtre déjà administrateur : free_resources.ps1 arrête les services d'office, sans option pour l'éviter.
    Show-ServicesVises
}

if ($Simulation) {
    Write-Host ''
    Write-Alerte 'SIMULATION : rien n''a été fermé.'
    if ($estAdmin) { Write-Alerte 'Cette fenêtre est administrateur : un passage réel lancé d''ici arrêterait AUSSI les services Windows listés ci-dessus.' }
    Wait-FinOutil -SansPause:$SansPause
    exit 0
}

# --- 2. Choix --------------------------------------------------------------------------
$interactif = (-not $Oui) -and (-not $SansPause)
$faireServices = [bool]$AvecServices

# Fenêtre administrateur SANS l'option « services » : free_resources.ps1 les arrêterait quand
# même. Sans accord explicite (-AvecServices, ou réponse O ci-dessous), on refuse.
if ($estAdmin -and -not $faireServices) {
    Write-Section 'Attention : cette fenêtre est administrateur'
    Write-Alerte 'Cette fenêtre est administrateur : free_resources.ps1 arrêtera AUSSI les services Windows non essentiels listés ci-dessus. Il n''a pas d''option pour les épargner.'
    if (-not $interactif) {
        Write-Erreur 'Refusé : tu n''as pas demandé l''arrêt des services (-AvecServices). Rien n''a été fermé.'
        Write-Conseil 'Pour fermer seulement les programmes : relance cet outil depuis une fenêtre NORMALE (non administrateur).'
        Write-Conseil 'Pour arrêter aussi les services : relance avec -AvecServices.'
        Wait-FinOutil -SansPause:$SansPause
        exit 2
    }
    if (-not (Confirm-Action -Question 'Acceptes-tu que ces services Windows soient arrêtés EN PLUS des programmes ?')) {
        Write-Info 'Refusé : rien n''a été fermé, aucun service n''a été arrêté.'
        Write-Conseil 'Pour fermer seulement les programmes : relance cet outil depuis une fenêtre NORMALE (non administrateur).'
        Wait-FinOutil -SansPause:$SansPause
        exit 2
    }
    $faireServices = $true
}

if ([int]$essai.closed_count -eq 0 -and -not $faireServices -and -not $interactif) {
    Wait-FinOutil -SansPause:$SansPause
    exit 0
}

Write-Section 'Étape 2 : confirmation'
Write-Alerte 'Enregistre ton travail : les programmes listés ci-dessus seront fermés d''office.'
if ($estAdmin) { Write-Alerte 'Les services Windows listés ci-dessus seront arrêtés eux aussi.' }
if (-not (Confirm-Action -Question 'Fermer ces programmes maintenant ?' -Oui:$Oui)) {
    Write-Info 'Annulé : rien n''a été fermé.'
    Wait-FinOutil -SansPause:$SansPause
    exit 2
}
if (-not $faireServices -and $interactif) {
    Write-Host ''
    Write-Info 'Option : arrêter AUSSI les services Windows non essentiels (indexation, télémétrie, Xbox, synchronisation...).'
    Write-Info 'Il faut une fenêtre administrateur. Rien n''est désactivé : ils reviennent au prochain redémarrage du PC.'
    $faireServices = Confirm-Action -Question 'Inclure ces services Windows ?'
}

# --- 3. Passage réel ------------------------------------------------------------------
if ($faireServices -and -not $estAdmin) {
    Write-Section 'Étape 3 : passage réel dans une fenêtre administrateur'
    Write-Info 'Windows va te demander l''autorisation. La nouvelle fenêtre refera la simulation avec les droits administrateur'
    Write-Info '(la liste peut s''allonger), te montrera les services visés, puis te redemandera ton accord avant de fermer.'
    # -Oui n'est transmis QUE si tu l'as toi-même demandé en lançant l'outil : un « O » répondu
    # ici porte sur la liste de cette fenêtre, pas sur celle, plus complète, de la fenêtre élevée.
    $arguments = @('-Racine', $Racine, '-AvecServices')
    if ($Oui) { $arguments += '-Oui' }
    if ($SansPause) { $arguments += '-SansPause' }
    $code = Invoke-OutilEleve -Script $PSCommandPath -Arguments $arguments
    if ($null -eq $code) {
        Write-Erreur 'Tu as refusé les droits administrateur : rien n''a été fermé.'
        Write-Conseil 'Relance cet outil et réponds N à l''option « services Windows » pour fermer seulement les programmes.'
        Wait-FinOutil -SansPause:$SansPause
        exit 3
    }
    if ($code -eq 0) { Write-Ok 'Libération terminée dans la fenêtre administrateur.' }
    elseif ($code -eq 2) { Write-Info 'Annulé dans la fenêtre administrateur : rien n''a été fermé.' }
    else { Write-Alerte 'La fenêtre administrateur a signalé un problème : relis son résumé, ou le journal logs\outils-worker.log.' }
    Wait-FinOutil -SansPause:$SansPause
    if ($code -eq 0) { exit 0 }
    if ($code -eq 2) { exit 2 }
    exit 1
}

Write-Section 'Étape 3 : passage réel'
$reel = Invoke-Liberation
if (-not $reel) {
    Write-Erreur 'free_resources.ps1 n''a rendu aucun rapport : impossible de dire ce qui a été fermé.'
    Wait-FinOutil -SansPause:$SansPause
    exit 1
}
$mention = ''
if ($estAdmin) { $mention = ' (administrateur)' }
$codeFinal = Show-Resultat -Rapport $reel -Mention $mention
Write-Conseil ('Pour libérer la mémoire de la carte graphique (VRAM) : arrête les moteurs IA avec ' + (Get-NomEntree 9) + ', choix 2.')
Wait-FinOutil -SansPause:$SansPause
exit $codeFinal
