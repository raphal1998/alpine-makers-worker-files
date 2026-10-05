<#
    Rallumer ce Worker après « Éteindre le Worker (lui seul) » (ou après une déconnexion).

    Pilotage sans clavier :
        rallumer_worker.ps1 -Simulation -Oui -SansPause      (aucun effet)
        rallumer_worker.ps1 -Oui -SansPause [-SansMoteurs]
    Codes de sortie : 0 succès, 1 échec, 2 refusé ou annulé, 3 élévation refusée.

    -DejaEleve est posé par Invoke-OutilEleve : l'instance administrateur ne fait QUE
    remettre le service en Automatique et le démarrer ; avec -SeulementTache elle ne
    fait QUE réactiver la tâche planifiée (le Worker, lui, est toujours lancé SANS
    droits administrateur).
#>
param(
    [string]$Racine = '',
    [switch]$Simulation,
    [switch]$Oui,
    [switch]$SansMoteurs,
    [switch]$SansPause,
    [switch]$DejaEleve,
    [switch]$SeulementTache
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'commun_cycle.ps1')
Initialize-ConsoleWorker -Titre 'Rallumer le Worker'
$NomOutil = 'rallumer_worker'

# Test-ServiceAutreDossier, Get-PresenceWorker, Get-MemoMoteurs, Test-TacheDansMemo : commun_cycle.ps1.

function Get-TachesAReactiver {
    <#
        Tâches planifiées de CE dossier, actuellement désactivées, ET notées dans le mémo
        d'extinction (tache_desactivee) : ce sont celles qu'« Éteindre le Worker » a
        coupées. Une tâche que tu as désactivée toi-même n'est pas dans le mémo : on n'y touche pas.
        Renvoie : AReactiver, DesactiveesParToi : des tâches (Nom, Chemin = dossier du Planificateur).
        Le mémo note « chemin + nom » ; l'ancien format à nom seul reste lu (Test-TacheDansMemo).
    #>
    param([string]$Racine)
    $notees = @()
    $fichier = Join-Path $Racine 'runtime\local-control\extinction.json'
    if (Test-Path -LiteralPath $fichier -PathType Leaf) {
        try { $notees = @((Get-Content -LiteralPath $fichier -Raw -Encoding UTF8 | ConvertFrom-Json).tache_desactivee | ForEach-Object { [string]$_ } | Where-Object { $_ }) } catch { $notees = @() }
    }
    $desactivees = @((Get-ModeLancement -Racine $Racine).Taches | Where-Object { $_.ViseCeDossier -and $_.Etat -eq 'Disabled' })
    return [pscustomobject]@{
        AReactiver = @($desactivees | Where-Object { Test-TacheDansMemo -Tache $_ -Memo $notees })
        DesactiveesParToi = @($desactivees | Where-Object { -not (Test-TacheDansMemo -Tache $_ -Memo $notees) })
    }
}

function Enable-TachesDuWorker {
    <# Réactive les tâches données (issues de Get-TachesAReactiver), par nom ET dossier du Planificateur. Renvoie : Activees (noms), Echecs (noms). #>
    param([string]$Racine, [object[]]$Taches = @(), [switch]$Simulation)
    $r = [pscustomobject]@{ Activees = @(); Echecs = @() }
    foreach ($tache in @($Taches | Where-Object { $_ })) {
        $nom = [string]$tache.Nom
        if ($Simulation) { Write-Info ('[simulation]    Enable-ScheduledTask « ' + $nom + ' »'); continue }
        try {
            Enable-ScheduledTask -TaskName $nom -TaskPath $tache.Chemin -ErrorAction Stop | Out-Null
            $relue = Get-ScheduledTask -TaskName $nom -TaskPath $tache.Chemin -ErrorAction Stop
            if ([string]$relue.State -eq 'Disabled') { throw 'la tâche est restée désactivée' }
            $r.Activees += $nom
            Write-Ok ('Tâche planifiée « ' + $nom + ' » réactivée : le Worker repartira à chaque ouverture de session.')
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('tâche planifiée réactivée : ' + $nom)
        } catch {
            $r.Echecs += $nom
            Write-Alerte ('Tâche planifiée « ' + $nom + ' » NON réactivée : ' + $_.Exception.Message)
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('ÉCHEC réactivation de la tâche ' + $nom + ' : ' + $_.Exception.Message)
        }
    }
    return $r
}

function Invoke-PhaseService {
    <# Administrateur : démarrage Automatique, marqueur de déconnexion retiré, service démarré. Renvoie 0 ou 1. #>
    param([string]$Racine)
    try {
        Set-Service -Name 'AlpineWorker' -StartupType Automatic -ErrorAction Stop
        Write-Ok 'Démarrage du service remis en Automatique.'
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'service AlpineWorker : démarrage remis en Automatique'
    } catch {
        Write-Erreur ('Impossible de remettre le démarrage Automatique : ' + $_.Exception.Message)
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('ÉCHEC Set-Service Automatic : ' + $_.Exception.Message)
        return 1
    }
    Remove-MarqueurDeconnexion -Racine $Racine -Outil $NomOutil
    $s = Get-Service -Name 'AlpineWorker' -ErrorAction SilentlyContinue
    if ($s -and [string]$s.Status -ne 'Running') {
        try { Start-Service -Name 'AlpineWorker' -ErrorAction Stop -WarningAction SilentlyContinue } catch { Write-Alerte ('Start-Service : ' + $_.Exception.Message) }
    }
    if (Wait-Service -Nom 'AlpineWorker' -Etat 'Running' -DelaiSecondes 30) {
        Write-Ok 'Service AlpineWorker démarré.'
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'service AlpineWorker démarré'
        return 0
    }
    Write-Erreur 'Le service n''est pas en marche après 30 secondes.'
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'ÉCHEC : service AlpineWorker non démarré après 30 s'
    return 1
}

try {
    $Racine = Get-RacineWorker -Racine $Racine
    $mode = Get-ModeLancement -Racine $Racine

    if ($DejaEleve) {
        Write-Cadre -Titre 'RALLUMER LE WORKER : phase administrateur' -Lignes @('Cette fenêtre se ferme seule ; la suite s''affiche dans la première.')
        if (-not (Test-Administrateur)) { Write-Erreur 'Cette fenêtre n''a pas les droits administrateur.'; exit 3 }
        if ($SeulementTache) {
            # Phase dédiée : réactiver la tâche, RIEN d'autre. Le Worker est lancé par l'instance non administrateur.
            $e = Enable-TachesDuWorker -Racine $Racine -Taches @((Get-TachesAReactiver -Racine $Racine).AReactiver)
            $codeTache = 0
            if ($e.Echecs.Count -gt 0) { $codeTache = 1 }
            Exit-Outil -Code $codeTache -SansPause
        }
        if ($mode.Mode -ne 'service') { Write-Erreur 'Aucun service AlpineWorker ne vise ce dossier.'; exit 1 }
        $code = Invoke-PhaseService -Racine $Racine
        Exit-Outil -Code $code -SansPause
    }

    Write-Explication -Titre 'RALLUMER CE WORKER' -Fait @(
('Remet le Worker en route après ' + (Get-NomEntree 11) + ', ou après une déconnexion depuis le site.'),
        'En mode service : remet le démarrage AUTOMATIQUE, puis démarre le service.',
        'En mode tâche planifiée : réactive la tâche coupée par l''extinction, puis lance le Worker.',
        'Attend une PREUVE que l''agent tourne (réponse, relais caméra, ou processus reconnu) ; sinon il annonce un échec.',
        'Si l''agent a gardé un mémo d''arrêt : lui demande de relancer les moteurs qui tournaient avant l''extinction.',
        'Sans mémo (extinction brutale) : ne demande rien, l''agent relance seul ses moteurs ; l''outil observe leurs ports.',
        'Suit le réveil des moteurs pendant 3 minutes au plus.'
    ) -NeFaitPas @(
        'Il ne réinstalle rien et ne change ni l''identité ni l''association du Worker.',
        'Il ne touche à aucun autre service du PC.',
        'Il ne lance aucune génération et n''envoie rien aux machines.'
    ) -Exige @(
        'En mode service : une fenêtre administrateur (Windows te demandera ton accord).',
        'En mode tâche ou manuel : une fenêtre NORMALE, surtout pas administrateur.',
        '   (Une fenêtre administrateur n''est demandée que si Windows refuse de réactiver la tâche.)'
    )
    if ($Simulation) { Write-Alerte 'MODE SIMULATION : rien ne sera démarré, modifié ni supprimé.' }

    # ------------------------------------------------------------ état de départ
    Write-Section 'État de départ'
    Write-Etat -Libelle 'Dossier du Worker' -Valeur $Racine
    $config = Get-ConfigSure -Racine $Racine
    if (-not $config.Associe) {
        Write-Erreur 'Ce Worker n''est associé à aucun compte : le démarrer ne servirait à rien.'
        Write-Conseil ('Lance d''abord ' + (Get-NomEntree 8) + ' (ou install_windows.bat pour une première installation).')
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    $dejaEnMarche = $false
    switch ($mode.Mode) {
        'service' {
            Write-Etat -Libelle 'Mode de lancement' -Valeur ('service Windows AlpineWorker : ' + $mode.Service.Etat + ', démarrage ' + $mode.Service.Demarrage)
            $dejaEnMarche = ($mode.Service.Etat -eq 'Running' -and $mode.Service.Demarrage -eq 'Auto')
        }
        'tache' {
            Write-Etat -Libelle 'Mode de lancement' -Valeur 'tâche planifiée à l''ouverture de session'
            foreach ($t in @($mode.Taches | Where-Object { $_.ViseCeDossier })) { Write-Info ('      Tâche « ' + $t.Nom + ' » : état ' + $t.Etat) }
        }
        default { Write-Etat -Libelle 'Mode de lancement' -Valeur 'lancement manuel (ni service ni tâche pour ce dossier)' }
    }
    # Un autre dossier sert déjà les ports du Worker : démarrer celui-ci créerait un conflit, et tout
    # ce qu'on lirait sur les ports serait l'état de l'AUTRE Worker.
    if (Test-ServiceAutreDossier -Mode $mode) { Exit-Outil -Code 2 -SansPause:$SansPause }
    $status = Invoke-ActionSure -Racine $Racine -Action 'status' -DelaiSecondes 8
    $incertain = $false
    if ($mode.Mode -ne 'service') {
        # Preuves propres à CE dossier (processus reconnus, réponse à status), pas les ports globaux.
        $presence = Get-PresenceWorker -Racine $Racine -Status $status
        $dejaEnMarche = ($presence.Etat -eq 'oui')
        $incertain = ($presence.Etat -eq 'illisible')
    }
    $taches = Get-TachesAReactiver -Racine $Racine
    foreach ($n in $taches.AReactiver) { Write-Etat -Libelle 'Tâche planifiée' -Valeur ('désactivée par « Éteindre » : elle sera réactivée (' + $n.Nom + ')') -Niveau alerte }
    foreach ($n in $taches.DesactiveesParToi) {
        Write-Etat -Libelle 'Tâche planifiée' -Valeur ('désactivée, mais pas par « Éteindre » : laissée telle quelle (' + $n.Nom + ')') -Niveau alerte
        Write-Info ('      Le Worker ne repartira donc pas seul à l''ouverture de session. Pour la réactiver : ' + (Get-NomEntree 15) + '.')
    }
    if (Test-Path -LiteralPath (Join-Path $Racine '.worker-disconnected') -PathType Leaf) { Write-Etat -Libelle 'Marqueur de déconnexion' -Valeur 'présent : il sera retiré' -Niveau alerte }

    $fichierMemo = Join-Path $Racine 'runtime\local-control\extinction.json'
    $moteursMemo = @()
    if (Test-Path -LiteralPath $fichierMemo -PathType Leaf) {
        try {
            $memo = Get-Content -LiteralPath $fichierMemo -Raw -Encoding UTF8 | ConvertFrom-Json
            $moteursMemo = @($memo.moteurs_qui_tournaient | ForEach-Object { [string]$_ } | Where-Object { $_ })
            Write-Etat -Libelle 'Dernière extinction' -Valeur ([string]$memo.date)
            if ($moteursMemo.Count) { Write-Etat -Libelle 'Moteurs qui tournaient' -Valeur ($moteursMemo -join ', ') }
        } catch { Write-Info '      Mémo d''extinction illisible : il sera ignoré.' }
    }
    Show-Moteurs -Racine $Racine -Status $status
    if ($dejaEnMarche) { Write-Etat -Libelle 'Worker' -Valeur 'déjà en marche' -Niveau ok }
    elseif ($incertain) {
        Write-Etat -Libelle 'Worker' -Valeur 'incertain : des ports sont ouverts, mais ses processus sont illisibles d''ici' -Niveau alerte
        Write-Info '      Le lancement sera demandé quand même : si un superviseur de ce dossier tourne déjà,'
        Write-Info '      le nouveau se retire de lui-même (verrou supervisor.lock). Aucun doublon possible.'
    }
    else { Write-Etat -Libelle 'Worker' -Valeur 'arrêté' -Niveau alerte }

    Write-Host ''
    $question = 'Rallumer ce Worker maintenant ?'
    if ($dejaEnMarche) { $question = 'Le Worker tourne déjà. Relancer seulement ses moteurs ?' }
    if (-not (Confirm-Action -Question $question -Oui:$Oui)) { Write-Info 'Annulé : rien n''a changé.'; Exit-Outil -Code 2 -SansPause:$SansPause }

    # Mode tâche ou manuel depuis une fenêtre administrateur : l'agent tournerait en administrateur.
    $tacheCible = @($mode.Taches | Where-Object { $_.ViseCeDossier }) | Select-Object -First 1
    if (-not $dejaEnMarche -and $mode.Mode -eq 'aucun' -and (Test-Administrateur)) {
        Write-Erreur 'Cette fenêtre est administrateur : le Worker démarrerait avec des droits administrateur.'
        Write-Conseil 'Ferme-la, puis relance cet outil depuis une fenêtre normale (double-clic).'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }

    # ------------------------------------------------------------ simulation
    if ($Simulation) {
        Write-Section 'Ce qui serait fait'
        if ($taches.AReactiver.Count -gt 0) {
            Write-Info '[simulation] 0. AVANT tout : réactivation de la tâche planifiée coupée par « Éteindre le Worker »'
            Write-Info '[simulation]    (fenêtre administrateur seulement si Windows refuse sans elle) :'
            [void](Enable-TachesDuWorker -Racine $Racine -Taches $taches.AReactiver -Simulation)
        }
        if ($dejaEnMarche) { Write-Info '[simulation] 1. le Worker tourne déjà : aucun démarrage.' }
        elseif ($mode.Mode -eq 'service') {
            Write-Info '[simulation] 1. ouverture d''une fenêtre administrateur (accord Windows demandé).'
            Write-Info '[simulation] 2. Set-Service AlpineWorker -StartupType Automatic'
            Remove-MarqueurDeconnexion -Racine $Racine -Outil $NomOutil -Simulation
            Write-Info '[simulation] 3. Start-Service AlpineWorker, attente de l''état Running (30 s).'
        } else {
            Remove-MarqueurDeconnexion -Racine $Racine -Outil $NomOutil -Simulation
            if ($mode.Mode -eq 'tache' -and (Test-Administrateur)) { Write-Info ('[simulation] 1. Start-ScheduledTask « ' + $tacheCible.Nom + ' » (elle tourne sous son propre compte).') }
            else { Write-Info '[simulation] 1. lancement caché de run_worker.ps1, SANS droits administrateur.' }
        }
        Write-Info '[simulation] 4. attente d''une preuve que l''agent tourne (90 s au plus) : réponse, relais caméra, ou processus reconnu.'
        Write-Info '[simulation]    Sans preuve : ÉCHEC annoncé, pas d''attente des moteurs, mémo conservé.'
        if ($SansMoteurs) { Write-Info '[simulation] 5. -SansMoteurs : aucune relance de moteur demandée.' }
        else {
            Write-Info '[simulation] 5. relecture de « status ». Mémo d''arrêt présent : demande « engines-start » (moteurs du mémo seulement).'
            Write-Info '[simulation]    Pas de mémo (extinction brutale) : AUCUNE demande, l''agent relance seul ses moteurs ; on observe.'
            Write-Info '[simulation]    Dans les deux cas : sondage des ports toutes les 5 s (180 s au plus).'
        }
        Write-Info '[simulation] 6. suppression du mémo runtime\local-control\extinction.json, seulement si tout a réussi.'
        Write-Host ''
        Write-Ok 'Simulation terminée : rien n''a été démarré, modifié ni supprimé.'
        Exit-Outil -Code 0 -SansPause:$SansPause
    }

    $codeFinal = 0
    $elevationRefusee = $false
    $lanceIci = $false
    # Relevé AVANT tout démarrage : des moteurs orphelins peuvent écouter alors que l'agent est mort.
    # Seul un moteur ouvert APRÈS le démarrage prouvera que l'agent tourne (Wait-Agent).
    $moteursDejaOuverts = @(Get-MoteursOuverts -Racine $Racine)

    # ------------------------------------------------------------ démarrage automatique (tâche planifiée)
    # Fait même si le Worker tourne déjà : sinon il ne repartirait plus à l'ouverture de session.
    if ($taches.AReactiver.Count -gt 0) {
        Write-Section 'Étape 0 : réactivation de la tâche planifiée'
        $e = Enable-TachesDuWorker -Racine $Racine -Taches $taches.AReactiver
        if ($e.Echecs.Count -gt 0) {
            if (Test-Administrateur) { $codeFinal = 1 }
            else {
                Write-Info 'Windows refuse sans droits administrateur : il va te demander ton accord pour une fenêtre administrateur.'
                Write-Info 'Cette fenêtre ne fait QUE réactiver la tâche ; le Worker, lui, est lancé sans droits administrateur.'
                $codeTache = Invoke-OutilEleve -Script $PSCommandPath -Arguments ((Get-ArgumentsCommuns -Racine $Racine) + @('-SeulementTache'))
                if ($null -eq $codeTache) { $elevationRefusee = $true; Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'élévation refusée (réactivation de la tâche)' }
                elseif ($codeTache -ne 0) { $codeFinal = 1; Show-TraceOutil -Racine $Racine -Outil $NomOutil }
            }
        }
        $restantes = @((Get-TachesAReactiver -Racine $Racine).AReactiver)
        if ($restantes.Count -gt 0) {
            Write-Erreur 'La tâche planifiée est restée DÉSACTIVÉE : le Worker ne repartira pas seul à l''ouverture de session.'
            Write-Conseil ('Relance cet outil et accepte la fenêtre administrateur, ou réactive-la avec ' + (Get-NomEntree 15) + '.')
            if (-not $elevationRefusee) { $codeFinal = 1 }
        } else { $elevationRefusee = $false }
        $mode = Get-ModeLancement -Racine $Racine
        $tacheCible = @($mode.Taches | Where-Object { $_.ViseCeDossier }) | Select-Object -First 1
    }
    if (-not $dejaEnMarche -and $mode.Mode -eq 'tache' -and (Test-Administrateur) -and $tacheCible -and $tacheCible.Etat -eq 'Disabled') {
        Write-Erreur 'Cette fenêtre est administrateur et la tâche planifiée est désactivée : impossible de lancer le Worker sans lui donner des droits administrateur.'
        Write-Conseil 'Ferme-la, puis relance cet outil depuis une fenêtre normale (double-clic).'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }

    # ------------------------------------------------------------ démarrage
    if (-not $dejaEnMarche) {
        Write-Section 'Étape 1 : démarrage du Worker'
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('début : mode ' + $mode.Mode)
        if ($mode.Mode -eq 'service') {
            $code = 0
            if (Test-Administrateur) { $code = Invoke-PhaseService -Racine $Racine }
            else {
                Write-Info 'Windows va te demander ton accord pour ouvrir une fenêtre administrateur...'
                $code = Invoke-OutilEleve -Script $PSCommandPath -Arguments (Get-ArgumentsCommuns -Racine $Racine)
                if ($null -eq $code) {
                    Write-Erreur 'Tu as refusé la fenêtre administrateur : le Worker reste éteint.'
                    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'élévation refusée'
                    Exit-Outil -Code 3 -SansPause:$SansPause
                }
            }
            if ($code -ne 0) {
                Write-Erreur 'Le service n''a pas démarré.'
                Show-TraceOutil -Racine $Racine -Outil $NomOutil
                Write-Conseil ('Regarde ' + (Get-NomEntree 3) + ', journaux « service » et « service-erreurs ».')
                Exit-Outil -Code 1 -SansPause:$SansPause
            }
            Write-Ok 'Service AlpineWorker démarré, démarrage Automatique.'
        } else {
            Remove-MarqueurDeconnexion -Racine $Racine -Outil $NomOutil
            if ($mode.Mode -eq 'tache' -and (Test-Administrateur)) {
                Start-ScheduledTask -TaskName $tacheCible.Nom -TaskPath $tacheCible.Chemin -ErrorAction Stop
                Write-Ok ('Tâche planifiée « ' + $tacheCible.Nom + ' » lancée.')
                Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'tâche planifiée lancée'
            } else {
                Start-SuperviseurCache -Racine $Racine
                $lanceIci = $true
                Write-Ok 'Superviseur du Worker lancé en arrière-plan (sans droits administrateur).'
                Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'run_worker.ps1 lancé caché'
            }
        }
    }

    # ------------------------------------------------------------ attente de l'agent
    Write-Section 'Étape 2 : attente de l''agent (90 secondes au plus)'
    $agent = Wait-Agent -Racine $Racine -DelaiSecondes 90 -MoteursDejaOuverts $moteursDejaOuverts
    if ($agent -eq 'probable' -and $lanceIci) {
        # « probable » repose sur des ports GLOBAUX. Lancé d'ici, dans cette session, l'agent de CE dossier
        # est forcément lisible : s'il est invisible, ces ports appartiennent à autre chose.
        $controle = Get-PresenceWorker -Racine $Racine
        if (@($controle.Coeur | Where-Object { $_.Role -eq 'agent' }).Count -eq 0) {
            Write-Alerte 'Des ports du Worker répondent, mais aucun agent de CE dossier n''est visible : ils appartiennent à un autre programme.'
            $agent = 'absent'
        }
    }
    switch ($agent) {
        'confirme' { Write-Ok 'L''agent répond.' }
        'probable' { Write-Ok 'L''agent est en marche (son relais caméra ou un moteur répond ; il est trop ancien pour répondre directement aux outils).' }
        default {
            # Aucun signe : on exige une PREUVE avant d'annoncer quoi que ce soit.
            $presenceApres = Get-PresenceWorker -Racine $Racine
            $agentVu = (@($presenceApres.Coeur | Where-Object { $_.Role -eq 'agent' }).Count -gt 0)
            $superviseurVu = (@($presenceApres.Coeur | Where-Object { $_.Role -eq 'superviseur' }).Count -gt 0)
            $serviceEnMarche = $false
            if ($mode.Mode -eq 'service') {
                $s = Get-Service -Name 'AlpineWorker' -ErrorAction SilentlyContinue
                $serviceEnMarche = ($s -and [string]$s.Status -eq 'Running')
            }
            if ($agentVu) {
                $agent = 'processus'
                Write-Alerte 'Le processus de l''agent existe bien (reconnu par son PID), mais il ne répond pas encore aux outils.'
                Write-Conseil 'Sur un agent ancien sans caméra ni moteur, c''est normal. Vérifie dans le site que le Worker repasse « en ligne ».'
            } elseif ($mode.Mode -eq 'service' -and $serviceEnMarche) {
                Write-Erreur 'Le service AlpineWorker tourne, mais RIEN ne prouve que l''agent a démarré : aucun signe en 90 secondes.'
                Write-Info 'Sans droits administrateur, les processus d''un service sont illisibles : impossible de trancher d''ici.'
                Write-Conseil ('Regarde ' + (Get-NomEntree 3) + ' (journaux « service » et « agent ») et vérifie dans le site si le Worker repasse « en ligne ».')
                $codeFinal = 1
            } elseif ($mode.Mode -eq 'service') {
                Write-Erreur 'Le service AlpineWorker s''est arrêté de nouveau : le Worker n''a PAS démarré.'
                Write-Conseil ('Regarde ' + (Get-NomEntree 3) + ' (journaux « service » et « service-erreurs »), puis ' + (Get-NomEntree 1) + '.')
                $codeFinal = 1
            } elseif ($superviseurVu) {
                Write-Erreur 'Le superviseur tourne, mais l''agent ne tient pas : il plante au démarrage et se relance en boucle.'
                Write-Conseil ('Regarde ' + (Get-NomEntree 3) + ', journal « agent », pour lire son erreur.')
                $codeFinal = 1
            } else {
                Write-Erreur 'Le Worker n''a PAS démarré : run_worker.ps1 s''est arrêté aussitôt.'
                Write-Info 'Causes habituelles : Python introuvable (python_path.txt), dossier déjà verrouillé par un autre superviseur, droits insuffisants.'
                Write-Conseil ('Lance ' + (Get-NomEntree 1) + ', puis ' + (Get-NomEntree 3) + '.')
                $codeFinal = 1
            }
        }
    }
    $agentProuve = ($agent -ne 'absent')
    if (-not $agentProuve) { Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'ÉCHEC : aucune preuve que l''agent a démarré' }

    # ------------------------------------------------------------ moteurs
    if (-not $agentProuve) {
        Write-Info 'Relance des moteurs non tentée : le Worker n''a pas démarré.'
    } elseif ($SansMoteurs) {
        Write-Info 'Tu as demandé -SansMoteurs : aucun moteur n''est relancé par cet outil.'
    } else {
        Write-Section 'Étape 3 : relance des moteurs (3 minutes au plus)'
        $attendus = @()
        $relanceDemandee = $false
        # On regarde d'abord ce que l'agent SAIT de son dernier arrêt. Sans mémo d'arrêt, « engines-start »
        # lancerait TOUS les moteurs prêts (ComfyUI et Hunyuan3D ensemble) alors que l'agent relance déjà
        # seul ceux qui tournaient : on ne le demande donc que si le mémo existe.
        $statusApres = Invoke-ActionSure -Racine $Racine -Action 'status' -DelaiSecondes 10
        $memoAgent = Get-MemoMoteurs -Status $statusApres
        if ($statusApres.NonSupporte) {
            Write-Info 'Cet agent ne reçoit pas encore de demande locale : il relance SEUL, environ 30 secondes après son démarrage, les moteurs qui étaient en marche. On observe simplement leurs ports.'
            $attendus = @($moteursMemo)
            if ($attendus.Count -eq 0) { $attendus = @(Get-EtatMoteurs -Racine $Racine | Where-Object { $_.FichePresente } | ForEach-Object { $_.Outil }) }
            if ($attendus.Count -eq 0) { Write-Info 'Aucun moteur connu à attendre (ni mémo d''extinction, ni fiche de moteur).' }
        } elseif (-not $statusApres.Ok) {
            Write-Alerte ('L''agent ne répond pas encore aux outils : aucune relance demandée. ' + $statusApres.Erreur)
            Write-Conseil ('Il relance seul les moteurs dont la relance automatique est allumée. Pour les autres : ' + (Get-NomEntree 9) + ', choix 3, dans une minute.')
            $attendus = @($moteursMemo)
        } elseif ($memoAgent.EnCours) {
            Write-Info 'L''agent est déjà en train de démarrer ses moteurs : on suit leurs ports.'
            $attendus = @($memoAgent.Moteurs)
            if ($attendus.Count -eq 0) { $attendus = @($moteursMemo) }
        } elseif (-not $memoAgent.Present) {
            Write-Info 'Pas de mémo d''arrêt chez l''agent (extinction brutale, ou aucun arrêt propre) : aucune relance demandée.'
            Write-Info 'L''agent relance SEUL, environ 30 secondes après son démarrage, les moteurs restés en relance automatique. On observe leurs ports.'
            $attendus = @($moteursMemo | Where-Object { $memoAgent.Intentions -contains $_ })
            if ($attendus.Count -eq 0) { $attendus = @($memoAgent.Intentions) }
            if ($attendus.Count -eq 0) { Write-Info ('Aucun moteur n''est en relance automatique : rien à attendre. Pour en démarrer un : ' + (Get-NomEntree 9) + ', ou le site.') }
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('pas de mémo d''arrêt : aucune demande engines-start ; moteurs observés = ' + ($attendus -join ','))
        } elseif ($memoAgent.Vide) {
            Write-Info 'Le mémo d''arrêt de l''agent ne cite aucun moteur : rien ne tournait à l''extinction, rien à relancer.'
            Write-Conseil ('Pour démarrer un moteur : le site (Compte > Mes PC / Workers > ce Worker).')
        } else {
            Write-Info ('Mémo d''arrêt de l''agent : ' + ($memoAgent.Moteurs -join ', ') + '. Demande de relance envoyée (un point toutes les 5 secondes).')
            $r = Invoke-ActionSure -Racine $Racine -Action 'engines-start' -DelaiSecondes 30
            if ($r.Ok) {
                $relanceDemandee = $true
                $attendus = @($r.Resultat.starting | ForEach-Object { [string]$_ } | Where-Object { $_ })
                if ($attendus.Count) { Write-Ok ('Relance demandée : ' + ($attendus -join ', ') + '   (liste : ' + (Get-NomSourceRelance ([string]$r.Resultat.source)) + ')') } else { Write-Info 'L''agent n''a aucun moteur à relancer.' }
                Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('engines-start (' + [string]$r.Resultat.source + ') : ' + ($attendus -join ','))
            } elseif ($r.EnCours) {
                $relanceDemandee = $true
                Write-Info 'L''agent a pris la demande et démarre les moteurs : on suit leurs ports.'
                $attendus = @($memoAgent.Moteurs)
            } else {
                Write-Alerte ('L''agent n''a pas accepté la relance : ' + $r.Erreur)
                $codeFinal = 1
            }
        }
        # Jamais de sondage sans liste : on ne ferait qu'attendre 3 minutes pour rien.
        if ($attendus.Count -gt 0) {
            $enMarche = @(Wait-Moteurs -Racine $Racine -Attendus $attendus -DelaiSecondes 180)
            $manquants = @($attendus | Where-Object { $_ -ne 'converter' -and ($enMarche -notcontains $_) })
            if ($manquants.Count -gt 0) {
                Write-Alerte ('Pas encore ouverts après 3 minutes : ' + ($manquants -join ', '))
                if ($relanceDemandee) { Show-ErreursRelance -Racine $Racine -Manquants $manquants }
                Write-Conseil ('Un gros modèle peut demander plus de temps. Relance ' + (Get-NomEntree 9) + ', choix 1, dans quelques minutes ; si rien ne vient : ' + (Get-NomEntree 3) + '.')
                $codeFinal = 1
            }
        }
    }

    # ------------------------------------------------------------ mémo consommé
    # Seulement si l'agent est prouvé ET que tout a réussi : sinon la liste des moteurs et le nom de la
    # tâche à réactiver doivent rester disponibles pour le second essai.
    if (Test-Path -LiteralPath $fichierMemo -PathType Leaf) {
        if ($agentProuve -and $codeFinal -eq 0 -and -not $elevationRefusee) {
            Remove-Item -LiteralPath $fichierMemo -Force -ErrorAction SilentlyContinue
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'mémo extinction.json consommé et supprimé'
        } else {
            Write-Info 'Mémo d''extinction conservé pour le prochain essai.'
        }
    }

    Write-Section 'Résultat'
    Show-Moteurs -Racine $Racine
    $modeApres = Get-ModeLancement -Racine $Racine
    if ($modeApres.Mode -eq 'service') {
        $niveauService = 'ok'
        if ($modeApres.Service.Etat -ne 'Running' -or $modeApres.Service.Demarrage -ne 'Auto') { $niveauService = 'erreur'; $codeFinal = 1 }
        Write-Etat -Libelle 'Service AlpineWorker' -Valeur ($modeApres.Service.Etat + ', démarrage ' + $modeApres.Service.Demarrage) -Niveau $niveauService
    }
    if ($codeFinal -eq 0 -and $elevationRefusee) { $codeFinal = 3 }
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('fin : agent = ' + $agent + ', code = ' + $codeFinal)
    Write-Host ''
    if (-not $agentProuve) { Write-Erreur 'Le Worker n''est PAS rallumé (ou rien ne le prouve) : regarde les lignes en rouge ci-dessus.' }
    elseif ($codeFinal -eq 0) { Write-Ok 'Worker rallumé. Il repasse « en ligne » dans le site en moins d''une minute.' }
    elseif ($codeFinal -eq 3) { Write-Alerte 'Worker rallumé, mais tu as refusé la fenêtre administrateur : sa tâche planifiée reste désactivée.' }
    else { Write-Alerte 'Worker rallumé, mais tout n''est pas en ordre : regarde les lignes en jaune et en rouge ci-dessus.' }
    Exit-Outil -Code $codeFinal -SansPause:$SansPause
} catch {
    Write-Erreur ('Erreur inattendue : ' + $_.Exception.Message)
    if (-not $Simulation) { try { Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('erreur inattendue : ' + $_.Exception.Message) } catch { } }
    Exit-Outil -Code 1 -SansPause:$SansPause
}
