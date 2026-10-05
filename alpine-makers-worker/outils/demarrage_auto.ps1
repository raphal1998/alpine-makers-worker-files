<#
    Démarrage automatique du Worker : montre comment il est lancé sur ce PC (service
    Windows, tâche planifiée, ou rien) et retire les tâches planifiées ORPHELINES
    (celles qui visent un dossier qui n'existe plus).

    Usage :
      demarrage_auto.ps1                       affiche la situation et propose les actions utiles
      demarrage_auto.ps1 -RetirerOrphelines    retire les tâches orphelines (après confirmation)
      demarrage_auto.ps1 -RetirerDoublon       mode service : retire la tâche planifiée qui vise AUSSI ce dossier
      demarrage_auto.ps1 -ActiverTache         aucun démarrage auto : active la tâche planifiée (configure_autostart.ps1 -AutoStart Yes)
      demarrage_auto.ps1 -ReactiverTache       mode tâche : réactive la tâche de ce dossier si elle est DÉSACTIVÉE (ne lance pas le Worker)
      demarrage_auto.ps1 -DesactiverAuto       mode tâche : retire la tâche planifiée (configure_autostart.ps1 -AutoStart No), sans arrêter le Worker
      -Simulation : ne change rien.  -Oui / -SansPause : sans clavier.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause,
    [switch]$Oui,
    [switch]$Simulation,
    [switch]$RetirerOrphelines,
    [switch]$RetirerDoublon,
    [switch]$ActiverTache,
    [switch]$ReactiverTache,
    [switch]$DesactiverAuto,
    [switch]$DejaEleve
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
Initialize-ConsoleWorker -Titre 'Démarrage automatique du Worker'
$Racine = Get-RacineWorker -Racine $Racine   # déjà en forme LONGUE (un nom court PRENOM~1 fausserait la comparaison avec le dossier du service)

$NomOutil = 'demarrage_auto'
# Sans paramètre d'action et sans -SansPause/-Oui, l'outil pose les questions lui-même.
$interactif = (-not $SansPause) -and (-not $Oui) -and (-not $DejaEleve)

function Get-DossierDeTache {
    <# Dossier du Worker visé par une tâche, relu dans SON action (même motif que le socle). Vide = illisible. #>
    param($Tache)
    $arguments = [string](($Tache.Actions | Select-Object -First 1).Arguments)
    if ($arguments -match '-File\s+"([^"]+)\\run_worker\.ps1"') { return $Matches[1].TrimEnd('\') }
    return ''
}

function Get-QualiteTache {
    <#
        Classe une tâche listée par Get-ModeLancement :
          illisible     : on n'a pas su lire son dossier -> on ignore s'il existe : JAMAIS retirée ;
          disque-absent : le lecteur du dossier n'est pas là (disque externe, réseau) : JAMAIS retirée ;
          orpheline     : dossier LU, lecteur présent, dossier inexistant ;
          ce-dossier    : vise ce Worker ;
          autre         : autre installation existante : jamais touchée.
    #>
    param($Tache)
    if ($Tache.Illisible) { return 'illisible' }
    if ($Tache.DossierExiste) {
        if ($Tache.ViseCeDossier) { return 'ce-dossier' }
        return 'autre'
    }
    $lecteur = ''
    try { $lecteur = [IO.Path]::GetPathRoot([string]$Tache.Dossier) } catch { $lecteur = '' }
    if (-not $lecteur -or -not (Test-Path -LiteralPath $lecteur)) { return 'disque-absent' }
    return 'orpheline'
}

function Remove-TachesVerifiees {
    <#
        Retire des tâches planifiées. Chaque tâche est RELUE juste avant le retrait et n'est
        retirée que si : son nom commence par « Alpine Makers Worker », son dossier est lisible et
        identique à celui affiché, et (genre « orpheline ») ce dossier n'existe toujours pas, ou
        (genre « doublon ») ce dossier est bien celui de ce Worker. Le retrait vise le couple
        nom + chemin de tâche : une tâche homonyme rangée ailleurs n'est pas touchée.
        Renvoie les noms qui ont résisté (droits insuffisants, le plus souvent).
    #>
    param([object[]]$Taches, [ValidateSet('orpheline', 'doublon')][string]$Genre)
    $resistants = @()
    foreach ($t in $Taches) {
        $nom = [string]$t.Nom
        if ($nom -notlike 'Alpine Makers Worker*') { continue }
        if (-not $t.Dossier) { Write-Alerte ('Dossier illisible, tâche laissée en place : ' + $nom); continue }
        $attendu = ([string]$t.Dossier).TrimEnd('\')
        $traitee = $false
        foreach ($candidate in @(Get-ScheduledTask -TaskName $nom -ErrorAction SilentlyContinue)) {
            $dossier = Get-DossierDeTache -Tache $candidate
            if (-not $dossier -or $dossier -ine $attendu) { continue }
            if ($Genre -eq 'orpheline' -and (Test-Path -LiteralPath $dossier)) {
                Write-Alerte ('Le dossier existe de nouveau, tâche laissée en place : ' + $nom)
                $traitee = $true
                continue
            }
            if ($Genre -eq 'doublon' -and $dossier -ine $Racine) { continue }
            try {
                Unregister-ScheduledTask -TaskName $candidate.TaskName -TaskPath $candidate.TaskPath -Confirm:$false -ErrorAction Stop
                Write-Ok ('Tâche retirée : ' + $candidate.TaskPath + $candidate.TaskName)
                Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('Tâche planifiée retirée (' + $Genre + ') : ' + $candidate.TaskPath + $candidate.TaskName)
                $traitee = $true
            } catch {
                $resistants += $nom
                $traitee = $true
            }
        }
        if (-not $traitee) { Write-Alerte ('Tâche introuvable ou modifiée depuis l''affichage, rien retiré : ' + $nom) }
    }
    return $resistants
}

function Invoke-RetraitTaches {
    <# Retrait avec repli sur une fenêtre administrateur. Renvoie le code de sortie à utiliser. #>
    param([object[]]$Taches, [ValidateSet('orpheline', 'doublon')][string]$Genre, [string]$ParametreEleve)
    # Garde : une tâche au dossier illisible n'arrive jamais jusqu'au retrait, même par erreur d'appel.
    $Taches = @($Taches | Where-Object { $_ -and $_.Dossier })
    if ($Taches.Count -eq 0) { Write-Info 'Aucune tâche à retirer.'; return 0 }
    if ($Simulation) {
        foreach ($t in $Taches) { Write-Info ('SIMULATION : la tâche « ' + $t.Nom + ' » (dossier : ' + $t.Dossier + ') serait retirée.') }
        return 0
    }
    $resistants = @(Remove-TachesVerifiees -Taches $Taches -Genre $Genre)
    if ($resistants.Count -eq 0) { return 0 }
    if ($DejaEleve -or (Test-Administrateur)) {
        foreach ($n in $resistants) { Write-Erreur ('Impossible de retirer la tâche, même en administrateur : ' + $n) }
        return 1
    }
    Write-Alerte ([string]$resistants.Count + ' tâche(s) demandent les droits administrateur. Une fenêtre Windows va te demander l''autorisation.')
    $arguments = @('-Racine', $Racine, $ParametreEleve, '-Oui')
    if ($SansPause) { $arguments += '-SansPause' }
    $code = Invoke-OutilEleve -Script $PSCommandPath -Arguments $arguments
    if ($null -eq $code) {
        Write-Erreur 'Tu as refusé les droits administrateur : les tâches sont restées en place.'
        return 3
    }
    if ($code -ne 0) { Write-Alerte 'La fenêtre administrateur a signalé un problème : relance cet outil pour vérifier.' ; return 1 }
    Write-Ok 'Retrait terminé dans la fenêtre administrateur.'
    return 0
}

# ---------------------------------------------------------------------------

Write-Explication -Titre 'DÉMARRAGE AUTOMATIQUE DU WORKER' -Fait @(
    'Montre comment le Worker démarre sur ce PC : service Windows « AlpineWorker », tâche planifiée à l''ouverture de session, ou rien du tout.',
    'Repère les tâches planifiées ORPHELINES (elles visent un dossier qui n''existe plus) et propose de les retirer.',
    'Signale, SANS jamais les retirer, les tâches dont le dossier est illisible ou dont le disque est débranché.',
    'Si rien ne lance le Worker tout seul, propose d''activer la tâche planifiée prévue par l''installateur.',
    'En mode tâche : signale une tâche DÉSACTIVÉE et propose de la réactiver ; permet aussi de retirer le démarrage automatique.'
) -NeFaitPas @(
    'N''arrête et ne redémarre pas le Worker. Ne modifie pas le service Windows.',
    'Ne retire jamais une tâche dont le dossier existe sans te le dire clairement et sans une confirmation à part.',
    'Ne touche à aucune autre tâche planifiée du PC (boutique, dashboard, courrier...).'
) -Exige @(
    'Rien pour afficher. Les droits administrateur seulement si Windows refuse de retirer une tâche.'
)

$mode = Get-ModeLancement -Racine $Racine
$codeFinal = 0

# --- Situation -----------------------------------------------------------------
Write-Section 'Comment le Worker est lancé sur ce PC'
Write-Etat -Libelle 'Dossier de ce Worker' -Valeur $Racine -Niveau info
switch ($mode.Mode) {
    'service' { Write-Etat -Libelle 'Mode de lancement' -Valeur 'SERVICE WINDOWS (démarre avec le PC, sans ouvrir de session)' -Niveau ok }
    'tache'   { Write-Etat -Libelle 'Mode de lancement' -Valeur 'TÂCHE PLANIFIÉE (démarre à l''ouverture de ta session Windows)' -Niveau ok }
    default   { Write-Etat -Libelle 'Mode de lancement' -Valeur 'AUCUN démarrage automatique : lancement manuel' -Niveau alerte }
}

if ($mode.Service) {
    Write-Section 'Service Windows AlpineWorker'
    $s = $mode.Service
    $niveauEtat = 'alerte'; if ($s.Etat -eq 'Running') { $niveauEtat = 'ok' }
    Write-Etat -Libelle 'État' -Valeur $s.Etat -Niveau $niveauEtat
    $texteDemarrage = $s.Demarrage
    switch ($s.Demarrage) {
        'Auto'     { $texteDemarrage = 'Automatique (relancé aussi par le superviseur Alpine sous 30 s)' }
        'Manual'   { $texteDemarrage = 'Manuel (arrêt volontaire : le superviseur Alpine ne le relance pas)' }
        'Disabled' { $texteDemarrage = 'Désactivé' }
    }
    $niveauDemarrage = 'ok'; if ($s.Demarrage -ne 'Auto') { $niveauDemarrage = 'alerte' }
    Write-Etat -Libelle 'Démarrage' -Valeur $texteDemarrage -Niveau $niveauDemarrage
    Write-Etat -Libelle 'Compte' -Valeur $s.Compte -Niveau info
    Write-Etat -Libelle 'Dossier visé' -Valeur $s.Dossier -Niveau info
    if ($s.ViseCeDossier) { Write-Etat -Libelle 'Vise ce dossier' -Valeur 'oui' -Niveau ok }
    else { Write-Etat -Libelle 'Vise ce dossier' -Valeur 'NON : ce service lance une AUTRE installation du Worker' -Niveau alerte }
}

Write-Section 'Tâches planifiées « Alpine Makers Worker »'
$taches = @($mode.Taches)
if ($taches.Count -eq 0) { Write-Info 'Aucune tâche planifiée du Worker visible depuis ton compte.' }
foreach ($t in $taches) {
    $qualite = 'autre installation (dossier existant) : jamais touchée par cet outil'
    $niveau = 'info'
    switch (Get-QualiteTache -Tache $t) {
        'orpheline'     { $qualite = 'ORPHELINE : son dossier n''existe plus'; $niveau = 'alerte' }
        'ce-dossier'    {
            $qualite = 'vise CE dossier'; $niveau = 'ok'
            if ($t.Etat -eq 'Disabled' -and $mode.Mode -eq 'tache') { $qualite = 'vise CE dossier, mais DÉSACTIVÉE : le Worker ne partira pas à l''ouverture de session'; $niveau = 'alerte' }
            elseif ($t.Etat -eq 'Disabled') { $qualite = 'vise CE dossier, désactivée (sans effet : le service lance déjà ce Worker)'; $niveau = 'info' }
        }
        'illisible'     { $qualite = 'DOSSIER ILLISIBLE : jamais retirée par cet outil, vérifie-la dans le Planificateur de tâches'; $niveau = 'alerte' }
        'disque-absent' { $qualite = 'LECTEUR ABSENT (disque débranché ?) : jamais retirée par cet outil'; $niveau = 'alerte' }
    }
    Write-Etat -Libelle $t.Nom -Valeur ('[' + $t.Etat + '] ' + $qualite) -Niveau $niveau
    $dossierAffiche = $t.Dossier
    if (-not $dossierAffiche) { $dossierAffiche = '(dossier illisible dans la tâche)' }
    Write-Info ('        dossier : ' + $dossierAffiche)
}

# Le socle distingue déjà « Illisible » (action de la tâche non comprise) d'« Orpheline » (dossier LU et
# inexistant). Reste un cas qu'il ne connaît pas : le dossier est lu, mais son LECTEUR est absent (disque
# débranché). Ni les illisibles ni les disques absents ne sont JAMAIS retirés : on ignore si leur dossier existe.
$orphelines = @($taches | Where-Object { (Get-QualiteTache -Tache $_) -eq 'orpheline' })
$illisibles = @($taches | Where-Object { (Get-QualiteTache -Tache $_) -eq 'illisible' })
$disquesAbsents = @($taches | Where-Object { (Get-QualiteTache -Tache $_) -eq 'disque-absent' })
$doublons = @()
if ($mode.Mode -eq 'service') { $doublons = @($taches | Where-Object { (Get-QualiteTache -Tache $_) -eq 'ce-dossier' }) }

# --- Explications selon le mode ------------------------------------------------------
Write-Section 'Ce que cela veut dire'
switch ($mode.Mode) {
    'service' {
        if ([string]$mode.Service.Demarrage -eq 'Auto') {
            Write-Info 'Le service Windows assure déjà le démarrage du Worker avec le PC, et le superviseur Alpine le relance s''il tombe.'
        } else {
            Write-Alerte 'Le service existe mais son démarrage est MANUEL (ou désactivé) : le Worker ne démarrera PAS avec le PC et personne ne le relancera.'
            Write-Info ('C''est l''état laissé par ' + (Get-NomEntree 11) + '. Pour revenir à la normale : ' + (Get-NomEntree 12) + '.')
        }
        Write-Info 'N''active PAS en plus la tâche planifiée : deux lanceurs pour le même dossier se gênent (un seul gagne le verrou, l''autre tourne à vide et brouille les journaux).'
        Write-Conseil ('Pour l''éteindre ou le rallumer : ' + (Get-NomEntree 11) + ' et ' + (Get-NomEntree 12) + '.')
    }
    'tache' {
        Write-Info 'Le Worker démarre quand TU ouvres ta session Windows. Tant que personne n''est connecté après un redémarrage du PC, le Worker reste éteint.'
        Write-Conseil ('Pour l''éteindre en coupant aussi ce démarrage : ' + (Get-NomEntree 11) + ' ; ' + (Get-NomEntree 12) + ' le remet.')
    }
    default {
        Write-Info ('Rien ne lance ce Worker tout seul : après un redémarrage du PC il reste éteint jusqu''à ce que tu lances ' + (Get-NomEntree 12) + ' (ou ' + (Get-NomEntree 5) + ').')
    }
}

# --- 1. Tâches orphelines --------------------------------------------------------
if ($orphelines.Count -gt 0) {
    Write-Section 'Tâches orphelines'
    Write-Info 'Ces tâches essaient à chaque ouverture de session de lancer un Worker qui n''existe plus.'
    Write-Info 'Elles sont sans danger mais inutiles, et elles remplissent le journal de Windows d''erreurs.'
    $faire = [bool]$RetirerOrphelines
    if (-not $faire -and $interactif) { $faire = $true }
    if ($faire) {
        if (Confirm-Action -Question ('Retirer ces ' + $orphelines.Count + ' tâche(s) orpheline(s) ?') -Oui:($Oui -or $Simulation)) {
            $code = Invoke-RetraitTaches -Taches $orphelines -Genre 'orpheline' -ParametreEleve '-RetirerOrphelines'
            if ($code -ne 0) { $codeFinal = $code }
        } else {
            Write-Info 'Annulé : les tâches orphelines sont restées en place.'
            if ($RetirerOrphelines) { $codeFinal = 2 }
        }
    } else {
        Write-Conseil ('Pour les retirer : relance ' + (Get-NomEntree 15) + ' depuis le menu et réponds O, ou ajoute -RetirerOrphelines à outils\demarrage_auto.bat.')
    }
} elseif ($RetirerOrphelines) {
    Write-Ok 'Aucune tâche orpheline : rien à retirer.'
}

if (($illisibles.Count + $disquesAbsents.Count) -gt 0) {
    Write-Section 'Tâches que cet outil ne retire jamais'
    foreach ($t in $illisibles) { Write-Alerte ('« ' + $t.Nom + ' » : dossier illisible dans la tâche. Impossible de savoir s''il existe : elle reste en place.') }
    foreach ($t in $disquesAbsents) { Write-Alerte ('« ' + $t.Nom + ' » : le lecteur de ' + $t.Dossier + ' est absent. Rebranche le disque avant de conclure : elle reste en place.') }
    Write-Conseil 'Pour vérifier ou retirer une telle tâche à la main : ouvre le « Planificateur de tâches » de Windows, cherche « Alpine Makers Worker », onglet Actions.'
}

# --- 2. Doublon service + tâche sur CE dossier -------------------------------------
if ($doublons.Count -gt 0) {
    Write-Section 'Doublon : service ET tâche planifiée pour ce dossier'
    Write-Alerte 'Une tâche planifiée vise ce dossier alors que le service Windows le lance déjà.'
    Write-Info 'ATTENTION : le dossier de cette tâche EXISTE (c''est ce Worker). La retirer ne supprime aucun fichier'
    Write-Info 'et n''arrête pas le Worker : seul le second lanceur, inutile en mode service, disparaît.'
    $faire = [bool]$RetirerDoublon
    if (-not $faire -and $interactif) { $faire = $true }
    if ($faire) {
        if (Confirm-Action -Question 'Retirer cette tâche planifiée en doublon ?' -MotExact 'RETIRER' -Oui:($Oui -or $Simulation)) {
            $code = Invoke-RetraitTaches -Taches $doublons -Genre 'doublon' -ParametreEleve '-RetirerDoublon'
            if ($code -ne 0) { $codeFinal = $code }
        } else {
            Write-Info 'Annulé : la tâche en doublon est restée en place.'
            if ($RetirerDoublon) { $codeFinal = 2 }
        }
    } else {
        Write-Conseil 'Pour la retirer : relance avec -RetirerDoublon.'
    }
} elseif ($RetirerDoublon) {
    Write-Ok 'Aucune tâche en doublon : rien à retirer.'
}

# --- 3. Aucun démarrage automatique ----------------------------------------------
if ($mode.Mode -eq 'aucun') {
    Write-Section 'Activer le démarrage automatique'
    $configurateur = Join-Path $Racine 'configure_autostart.ps1'
    if ($mode.Service -and -not $mode.Service.ViseCeDossier) {
        Write-Alerte 'Un service AlpineWorker existe sur ce PC mais lance un AUTRE dossier. Deux Workers sur le même PC'
        Write-Info  'se disputent les ports des moteurs : n''active le démarrage automatique ici que si c''est voulu.'
    }
    if (-not (Test-Path -LiteralPath $configurateur -PathType Leaf)) {
        Write-Alerte 'configure_autostart.ps1 est absent de la racine du Worker : mets le Worker à jour pour le retrouver.'
        if ($ActiverTache) { $codeFinal = 1 }
    } else {
        Write-Info 'L''installateur fournit une tâche planifiée : le Worker démarre à l''ouverture de TA session Windows.'
        Write-Alerte 'Activer cette tâche LANCE aussi le Worker tout de suite.'
        $faire = [bool]$ActiverTache
        if (-not $faire -and $interactif) { $faire = $true }
        if ($faire) {
            if ($Simulation) {
                Write-Info 'SIMULATION : configure_autostart.ps1 -AutoStart Yes serait lancé. Rien n''a été fait.'
            } elseif (Confirm-Action -Question 'Activer le démarrage automatique (et lancer le Worker maintenant) ?' -Oui:$Oui) {
                $ancien = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $configurateur -AutoStart Yes
                $codeConfig = $LASTEXITCODE
                $ErrorActionPreference = $ancien
                if ($codeConfig -eq 0) {
                    Write-Ok 'Démarrage automatique activé.'
                    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'Démarrage automatique activé (configure_autostart.ps1 -AutoStart Yes)'
                } else {
                    Write-Erreur ('configure_autostart.ps1 a échoué (code ' + $codeConfig + ').')
                    $codeFinal = 1
                }
            } else {
                Write-Info 'Annulé : rien n''a été activé.'
                if ($ActiverTache) { $codeFinal = 2 }
            }
        } else {
            Write-Conseil 'Pour l''activer : relance avec -ActiverTache.'
        }
    }
} elseif ($ActiverTache) {
    Write-Section 'Activer le démarrage automatique'
    if ($mode.Mode -eq 'service') { Write-Alerte 'Refusé : le service Windows lance déjà ce Worker. Une tâche planifiée en plus ferait doublon.' }
    elseif (@($taches | Where-Object { $_.ViseCeDossier -and $_.Etat -eq 'Disabled' }).Count -gt 0) { Write-Alerte 'La tâche planifiée existe pour ce dossier mais elle est DÉSACTIVÉE : voir ci-dessous (ou -ReactiverTache).' }
    else { Write-Ok 'La tâche planifiée est déjà en place pour ce dossier : rien à faire.' }
}

# --- 4. Mode tâche : tâche désactivée, ou retrait du démarrage automatique ----------------
if ($mode.Mode -eq 'tache') {
    $desactivees = @($taches | Where-Object { $_.ViseCeDossier -and $_.Etat -eq 'Disabled' })
    if ($desactivees.Count -gt 0) {
        Write-Section 'Tâche planifiée désactivée'
        # extinction.json : tâches coupées par « Éteindre le Worker » (format « chemin + nom », ou ancien format à nom seul).
        $notees = @()
        $memoExtinction = Join-Path $Racine 'runtime\local-control\extinction.json'
        if (Test-Path -LiteralPath $memoExtinction -PathType Leaf) {
            try { $notees = @((Get-Content -LiteralPath $memoExtinction -Raw -Encoding UTF8 | ConvertFrom-Json).tache_desactivee | ForEach-Object { [string]$_ } | Where-Object { $_ }) } catch { $notees = @() }
        }
        foreach ($t in $desactivees) {
            $cle = ([string]$t.Chemin) + [string]$t.Nom
            if (($notees -contains $cle) -or ($notees -contains [string]$t.Nom)) {
                Write-Alerte ('« ' + $t.Nom + ' » a été désactivée par ' + (Get-NomEntree 11) + ' : le Worker est éteint volontairement.')
                Write-Conseil ('Pour la réactiver ET relancer le Worker : ' + (Get-NomEntree 12) + '.')
                if ($ReactiverTache) { $codeFinal = 2 }
                continue
            }
            Write-Alerte ('« ' + $t.Nom + ' » est désactivée (pas par les outils du Worker) : le Worker ne partira pas à l''ouverture de session.')
            $faire = [bool]$ReactiverTache
            if (-not $faire -and $interactif) { $faire = $true }
            if (-not $faire) { Write-Conseil 'Pour la réactiver : relance cet outil depuis le menu et réponds O, ou ajoute -ReactiverTache.'; continue }
            if ($Simulation) { Write-Info ('SIMULATION : Enable-ScheduledTask « ' + $t.Nom + ' » serait lancé. Rien n''a été fait.'); continue }
            if (-not (Confirm-Action -Question 'Réactiver cette tâche planifiée (le Worker n''est pas lancé maintenant) ?' -Oui:$Oui)) {
                Write-Info 'Annulé : la tâche est restée désactivée.'
                if ($ReactiverTache) { $codeFinal = 2 }
                continue
            }
            $reactivee = $false
            try {
                Enable-ScheduledTask -TaskName $t.Nom -TaskPath $t.Chemin -ErrorAction Stop | Out-Null
                $reactivee = ([string](Get-ScheduledTask -TaskName $t.Nom -TaskPath $t.Chemin -ErrorAction Stop).State -ne 'Disabled')
            } catch { $reactivee = $false }
            if ($reactivee) {
                Write-Ok ('Tâche réactivée : le Worker repartira à la prochaine ouverture de session. Pour le lancer tout de suite : ' + (Get-NomEntree 12) + '.')
                Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('Tâche planifiée réactivée : ' + $t.Chemin + $t.Nom)
            } elseif ($DejaEleve -or (Test-Administrateur)) {
                Write-Erreur ('Impossible de réactiver la tâche, même en administrateur : ' + $t.Nom); $codeFinal = 1
            } else {
                Write-Alerte 'Windows refuse sans droits administrateur. Une fenêtre Windows va te demander l''autorisation.'
                $argumentsEleves = @('-Racine', $Racine, '-ReactiverTache', '-Oui')
                if ($SansPause) { $argumentsEleves += '-SansPause' }
                $codeEleve = Invoke-OutilEleve -Script $PSCommandPath -Arguments $argumentsEleves
                if ($null -eq $codeEleve) { Write-Erreur 'Tu as refusé les droits administrateur : la tâche est restée désactivée.'; $codeFinal = 3 }
                elseif ($codeEleve -ne 0) { Write-Alerte 'La fenêtre administrateur a signalé un problème : relance cet outil pour vérifier.'; $codeFinal = 1 }
                else { Write-Ok 'Tâche réactivée dans la fenêtre administrateur.' }
            }
        }
    } elseif ($ReactiverTache) {
        Write-Ok 'La tâche planifiée de ce dossier est déjà active : rien à faire.'
    }

    # Retrait du démarrage automatique : jamais proposé d'office (on ne pousse pas à le couper), seulement sur demande.
    if ($DesactiverAuto) {
        Write-Section 'Retirer le démarrage automatique'
        $configurateur = Join-Path $Racine 'configure_autostart.ps1'
        if (-not (Test-Path -LiteralPath $configurateur -PathType Leaf)) { Write-Alerte 'configure_autostart.ps1 est absent de la racine du Worker : mets le Worker à jour pour le retrouver.'; $codeFinal = 1 }
        elseif ($Simulation) { Write-Info 'SIMULATION : configure_autostart.ps1 -AutoStart No serait lancé (la tâche planifiée de ce dossier serait retirée). Rien n''a été fait.' }
        elseif (Confirm-Action -Question 'Retirer la tâche planifiée de ce dossier (le Worker en marche n''est pas arrêté) ?' -Oui:$Oui) {
            $ancien = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $configurateur -AutoStart No
            $codeConfig = $LASTEXITCODE
            $ErrorActionPreference = $ancien
            if ($codeConfig -eq 0) {
                Write-Ok 'Démarrage automatique retiré. Cet outil pourra le remettre (il proposera d''activer la tâche).'
                Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'Démarrage automatique retiré (configure_autostart.ps1 -AutoStart No)'
            } else { Write-Erreur ('configure_autostart.ps1 a échoué (code ' + $codeConfig + ').'); $codeFinal = 1 }
        } else { Write-Info 'Annulé : rien n''a été retiré.'; $codeFinal = 2 }
    } else {
        Write-Host ''
        Write-Conseil 'Pour retirer ce démarrage automatique sans éteindre le Worker : outils\demarrage_auto.bat -DesactiverAuto (depuis le dossier du Worker).'
    }
} elseif ($ReactiverTache -or $DesactiverAuto) {
    Write-Section 'Tâche planifiée'
    Write-Alerte 'Sans objet : ce dossier n''est pas lancé par une tâche planifiée.'
}

if ($Simulation) { Write-Host ''; Write-Alerte 'SIMULATION : rien n''a été modifié.' }
Wait-FinOutil -SansPause:$SansPause
if ($codeFinal -eq 64 -or $codeFinal -eq 75) { $codeFinal = 1 }
exit $codeFinal
