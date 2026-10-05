<#
    Réassocier ce Worker à ton compte quand sa fiche a DISPARU du site.

    Cas réel du 20.09.2026 : Worker révoqué PUIS fiche supprimée côté site.
      - install_windows.bat refuse (« Identité existante conservée ») car config.json
        contient encore un identifiant ;
      - « Récupérer une inscription » (7) n'a plus de fiche à récupérer.
    Cet outil refait une association NEUVE avec un code d'association, sans réinstaller.

    Pilotage sans clavier :
        reassocier_worker.ps1 -Simulation -Oui -SansPause                       (aucun effet)
        reassocier_worker.ps1 -Oui -SansPause -ServeurUrl https://... -Nom "..." -Code 123-456
    Codes de sortie : 0 succès, 1 échec, 2 refusé ou annulé, 3 élévation refusée.

    Le code d'association n'est JAMAIS écrit dans un journal ni réaffiché.
#>
param(
    [string]$Racine = '',
    [string]$ServeurUrl = '',
    [string]$Nom = '',
    [string]$Code = '',
    [string]$FichierCode = '',
    [switch]$Simulation,
    [switch]$Oui,
    [switch]$SansPause,
    [switch]$DejaEleve,
    [switch]$SeulementArreter
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'commun_cycle.ps1')
Initialize-ConsoleWorker -Titre 'Réassocier le Worker'
$NomOutil = 'reassocier_worker'
$FormatCode = '^\d{3}-\d{3}$'

# Test-ServiceAutreDossier, Get-PresenceWorker, Get-MoteursOuverts : commun_cycle.ps1.

$LibelleBoutonCode = 'Compte > Mes PC / Workers > bouton « Associer un PC »'

function Hide-CodeAssociation {
    <# Aucun message affiché ou journalisé ne doit contenir le code : il est remplacé par ***-***. #>
    param([string]$Texte, [string]$CodeAssociation)
    $t = [string]$Texte
    if ($CodeAssociation) { $t = $t.Replace($CodeAssociation, '***-***') }
    return ($t -replace '\b\d{3}-\d{3}\b', '***-***')
}

function Save-CodePourFenetreAdmin {
    <#
        Le code ne passe JAMAIS sur la ligne de commande de la fenêtre administrateur (elle est lisible
        par tout programme du compte, et par la journalisation PowerShell). Il transite par un fichier
        du profil (%LOCALAPPDATA%, fermé aux autres comptes), effacé aussitôt lu. Renvoie le chemin.
    #>
    param([string]$CodeAssociation)
    $dossier = Join-Path $env:LOCALAPPDATA 'AlpineMakers'
    if (-not (Test-Path -LiteralPath $dossier)) { New-Item -ItemType Directory -Path $dossier -Force | Out-Null }
    $fichier = Join-Path $dossier ('code-' + [guid]::NewGuid().ToString('N') + '.tmp')
    [IO.File]::WriteAllText($fichier, $CodeAssociation, (New-Object System.Text.UTF8Encoding($false)))
    return $fichier
}

function Read-CodeDepuisFichier {
    <# Lit puis EFFACE le fichier de passage. Refuse tout autre fichier qu'un « code-<32 hex>.tmp » de quelques octets. #>
    param([string]$Fichier)
    if (-not $Fichier -or ((Split-Path -Leaf $Fichier) -notmatch '^code-[0-9a-f]{32}\.tmp$')) { return '' }
    if (-not (Test-Path -LiteralPath $Fichier -PathType Leaf)) { return '' }
    $lu = ''
    try {
        if ((Get-Item -LiteralPath $Fichier -Force).Length -le 64) { $lu = ([string][IO.File]::ReadAllText($Fichier)).Trim() }
    } finally {
        Remove-Item -LiteralPath $Fichier -Force -ErrorAction SilentlyContinue
    }
    if ($lu -match $FormatCode) { return $lu }
    return ''
}

function Open-VerrouSuperviseur {
    <#
        Modes tâche et manuel : run_worker.ps1 tient supervisor.lock (partage interdit) tant
        qu'il vit, et tout second superviseur sort aussitôt (« déjà actif », code 0). En
        prenant ce verrou NOUS-MÊMES pendant l'association, ni la tâche planifiée (relance
        après échec, toutes les minutes), ni une ouverture de session, ni un double-clic ne
        peut relancer un agent avec l'ANCIENNE identité, qui réécrirait config.json par-dessus
        la nouvelle association. Rien n'est écrit dans le fichier.
        Renvoie le flux ouvert (à fermer avec Dispose), ou $null si le verrou est déjà tenu.
    #>
    param([string]$Racine)
    try {
        return [IO.File]::Open((Join-Path $Racine 'supervisor.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    } catch {
        $cause = $_.Exception
        if ($cause.InnerException) { $cause = $cause.InnerException }
        $code = ($cause.HResult -band 0xffff)
        if ($code -eq 32 -or $code -eq 33) { return $null }
        throw
    }
}

function Read-CodeAssociation {
    <# Demande le code (3 essais). Rien n'est journalisé ; le code n'est pas réaffiché. #>
    for ($i = 0; $i -lt 3; $i++) {
        $saisie = ([string](Read-Host '   Code d''association (format 123-456)')).Trim()
        if ($saisie -match $FormatCode) { return $saisie }
        Write-Alerte 'Format attendu : trois chiffres, un tiret, trois chiffres.'
    }
    return ''
}

function Invoke-Association {
    <# Lance « agent.py --pair » depuis le dossier du Worker. Renvoie { Code ; Message } ; le code d'association est retiré de tout message. #>
    param([string]$Racine, [string]$Url, [string]$NomWorker, [string]$CodeAssociation)
    $python = Get-PythonWorker -Racine $Racine
    if (-not $python) { return [pscustomobject]@{ Code = 1; Message = 'Interpréteur Python du Worker introuvable (python_path.txt).' } }
    $arguments = @('agent.py', '--config', 'config.json', '--pair', $Url, $CodeAssociation, '--name', $NomWorker) | ForEach-Object { ConvertTo-ArgumentCite $_ }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $python
    $info.Arguments = ($arguments -join ' ')
    $info.WorkingDirectory = $Racine
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = [Text.Encoding]::UTF8
    $info.StandardErrorEncoding = [Text.Encoding]::UTF8
    $info.EnvironmentVariables['PYTHONUTF8'] = '1'
    $info.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $processus = New-Object System.Diagnostics.Process
    $processus.StartInfo = $info
    [void]$processus.Start()
    $sortie = $processus.StandardOutput.ReadToEndAsync()
    $erreurs = $processus.StandardError.ReadToEndAsync()
    if (-not $processus.WaitForExit(240000)) {
        try { $processus.Kill() } catch { }
        return [pscustomobject]@{ Code = 1; Message = 'Aucune réponse du serveur après 4 minutes : association abandonnée.' }
    }
    $processus.WaitForExit()
    $codeSortie = [int]$processus.ExitCode
    $texte = ([string]$erreurs.Result).Trim()
    if (-not $texte -and $codeSortie -ne 0) { $texte = ([string]$sortie.Result).Trim() }
    $ligne = @($texte -split "`r?`n" | Where-Object { $_ -match 'Association impossible' } | Select-Object -Last 1)
    if ($ligne.Count -gt 0) { $texte = [string]$ligne[0] }
    elseif ($texte) { $texte = [string](@($texte -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)[0]) }
    if ($CodeAssociation) { $texte = $texte.Replace($CodeAssociation, '***-***') }
    if ($texte.Length -gt 300) { $texte = $texte.Substring(0, 300) }
    return [pscustomobject]@{ Code = $codeSortie; Message = $texte }
}

function Invoke-PhaseService {
    <#
        Administrateur, mode service : Manuel + arrêt, association, puis TOUJOURS
        Automatique + démarrage, même si l'association échoue. Renvoie 0 ou 1.
    #>
    param([string]$Racine, $Mode, [string]$Url, [string]$NomWorker, [string]$CodeAssociation)
    if (-not $Mode.Service.ViseCeDossier) { Write-Erreur 'Le service AlpineWorker ne vise pas ce dossier : il n''est pas touché.'; return 1 }
    if (-not (Test-CompteDuService -Service $Mode.Service)) {
        Write-Erreur ('Cette fenêtre administrateur tourne sous un autre compte que le service (' + $Mode.Service.Compte + ').')
        Write-Conseil 'L''identité du Worker est chiffrée pour ce compte-là : ouvre la session Windows de ce compte et relance l''outil.'
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'abandon : compte administrateur différent du compte du service'
        return 1
    }
    $resultat = 1
    $avant = Get-PidsDuWorker -Racine $Racine
    try {
        Set-Service -Name 'AlpineWorker' -StartupType Manual -ErrorAction Stop
        Write-Ok 'Démarrage du service passé en Manuel le temps de l''association (la supervision ne le relancera pas au milieu).'
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'service AlpineWorker : Manuel le temps de l''association'
        try { Stop-Service -Name 'AlpineWorker' -Force -ErrorAction Stop -WarningAction SilentlyContinue } catch { Write-Alerte ('Stop-Service : ' + $_.Exception.Message) }
        $arrete = Wait-Service -Nom 'AlpineWorker' -Etat 'Stopped' -DelaiSecondes 30
        $apres = Get-PidsDuWorker -Racine $Racine
        $cibles = @{}
        foreach ($p in @($avant.Processus) + @($apres.Processus)) { if ($p -and (Test-MemeProcessus -Entree $p)) { $cibles[$p.ProcessId] = $p } }
        if ($cibles.Count -gt 0) { [void](Stop-PidsReconnus -Liste @($cibles.Values) -Racine $Racine -Outil $NomOutil) }
        if (-not $arrete) { $arrete = Wait-Service -Nom 'AlpineWorker' -Etat 'Stopped' -DelaiSecondes 15 }
        if (-not $arrete) {
            Write-Erreur 'Le service ne s''arrête pas : association non tentée.'
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'ÉCHEC : service non arrêté, association non tentée'
        } else {
            Write-Ok 'Service arrêté. Association en cours auprès du serveur (jusqu''à quelques minutes)...'
            $a = Invoke-Association -Racine $Racine -Url $Url -NomWorker $NomWorker -CodeAssociation $CodeAssociation
            if ($a.Code -eq 0) {
                Write-Ok 'Le serveur a accepté l''association.'
                Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'association acceptée par le serveur'
                Remove-MarqueurDeconnexion -Racine $Racine -Outil $NomOutil
                $resultat = 0
            } else {
                Write-Erreur $(if ($a.Message) { $a.Message } else { 'Association impossible (aucun détail renvoyé).' })
                Write-Conseil 'L''ancienne configuration est restée en place. Un code ne sert qu''une fois et expire après 10 minutes : génères-en un nouveau.'
                Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('association refusée : ' + $a.Message)
            }
        }
    } catch {
        $messageSur = Hide-CodeAssociation -Texte $_.Exception.Message -CodeAssociation $CodeAssociation
        Write-Erreur ('Erreur pendant l''association : ' + $messageSur)
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('erreur : ' + $messageSur)
    } finally {
        # Quoi qu'il arrive : le service retrouve son démarrage Automatique et repart.
        try { Set-Service -Name 'AlpineWorker' -StartupType Automatic -ErrorAction Stop; Write-Ok 'Démarrage du service remis en Automatique.' }
        catch { Write-Erreur ('ATTENTION : démarrage Automatique NON remis : ' + $_.Exception.Message); $resultat = 1 }
        try { Start-Service -Name 'AlpineWorker' -ErrorAction Stop -WarningAction SilentlyContinue } catch { Write-Alerte ('Start-Service : ' + $_.Exception.Message) }
        if (Wait-Service -Nom 'AlpineWorker' -Etat 'Running' -DelaiSecondes 30) { Write-Ok 'Service AlpineWorker redémarré.' } else { Write-Erreur ('Le service n''a pas redémarré : lance ' + (Get-NomEntree 12) + '.') }
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'service AlpineWorker : Automatique remis, démarrage demandé'
    }
    return $resultat
}

function Stop-ArbreLocal {
    param([string]$Racine)
    $vus = Get-PidsDuWorker -Racine $Racine
    if ($vus.Processus.Count -eq 0) { return 0 }
    $restants = Stop-PidsReconnus -Liste $vus.Processus -Racine $Racine -Outil $NomOutil
    if ($restants -gt 0) { return 1 }
    return 0
}

try {
    $Racine = Get-RacineWorker -Racine $Racine
    $mode = Get-ModeLancement -Racine $Racine

    # ------------------------------------------------------------ instance administrateur
    if ($DejaEleve) {
        Write-Cadre -Titre 'RÉASSOCIER LE WORKER : phase administrateur'
        if (-not (Test-Administrateur)) { Write-Erreur 'Cette fenêtre n''a pas les droits administrateur.'; exit 3 }
        if ($SeulementArreter) { Exit-Outil -Code (Stop-ArbreLocal -Racine $Racine) -SansPause }
        if ($mode.Mode -ne 'service') { Write-Erreur 'Aucun service AlpineWorker ne vise ce dossier.'; Exit-Outil -Code 1 -SansPause:$SansPause }
        if ($FichierCode) { $Code = Read-CodeDepuisFichier -Fichier $FichierCode }
        if ($Code -and ($Code -notmatch $FormatCode)) { Write-Erreur 'Code d''association mal formé.'; Exit-Outil -Code 1 -SansPause:$SansPause }
        if (-not $Code) {
            Write-Info ('Génère MAINTENANT le code dans le site (' + $LibelleBoutonCode + ') : il expire après 10 minutes.')
            $Code = Read-CodeAssociation
            if (-not $Code) { Write-Erreur 'Aucun code valable : rien n''a été arrêté ni modifié.'; Exit-Outil -Code 2 -SansPause:$SansPause }
        }
        $resultat = Invoke-PhaseService -Racine $Racine -Mode $mode -Url $ServeurUrl -NomWorker $Nom -CodeAssociation $Code
        $Code = ''
        Exit-Outil -Code $resultat -SansPause:$SansPause
    }

    # ------------------------------------------------------------ explication
    Write-Explication -Titre 'RÉASSOCIER CE WORKER À TON COMPTE' -Fait @(
        'Refait une association NEUVE entre ce PC et ton compte, avec un code d''association du site.',
        'Arrête le Worker le temps de l''opération, puis le relance, quel que soit le résultat.',
        'Conserve tout : moteurs installés, modèles, réglages, travaux. Rien n''est réinstallé.'
    ) -NeFaitPas @(
        'Il ne sert PAS si la fiche du Worker existe encore dans « Mes Workers » :',
('   dans ce cas utilise ' + (Get-NomEntree 7) + ' : il demande un code de MIGRATION, pas d''association'),
        '   (Mes PC / Workers > ce Worker > « Sécurité et autorisations » > « Migrer / remplacer l''identité »).',
        'Il ne remet pas les partages ni les équipements : le Worker revient comme un nouveau Worker.',
        'Il n''affiche et n''enregistre jamais le code d''association.'
    ) -Exige @(
        'QUAND l''utiliser : la fiche du Worker a disparu du site (supprimée ou révoquée puis supprimée),',
        '   et install_windows.bat répond « Identité existante conservée ».',
('Un code d''association tout neuf (' + $LibelleBoutonCode + ') : valable 10 minutes, une seule fois.'),
        'L''adresse https:// du site, et en mode service une fenêtre administrateur.',
        'Taper le mot REASSOCIER pour confirmer.'
    )
    if ($Simulation) { Write-Alerte 'MODE SIMULATION : rien ne sera arrêté, associé ni modifié.' }

    # ------------------------------------------------------------ état
    Write-Section 'Situation actuelle'
    $config = Get-ConfigSure -Racine $Racine
    Write-Etat -Libelle 'Dossier du Worker' -Valeur $Racine
    if ($config.Associe) { Write-Etat -Libelle 'Association actuelle' -Valeur ('identifiant ' + $config.WorkerIdCourt + '...  (elle sera REMPLACÉE)') -Niveau alerte }
    else { Write-Etat -Libelle 'Association actuelle' -Valeur 'aucune' }
    if ($config.Nom) { Write-Etat -Libelle 'Nom actuel' -Valeur $config.Nom }
    if ($config.ServeurUrl) { Write-Etat -Libelle 'Site actuel' -Valeur $config.ServeurUrl }
    switch ($mode.Mode) {
        'service' { Write-Etat -Libelle 'Mode de lancement' -Valeur ('service Windows AlpineWorker : ' + $mode.Service.Etat + ', démarrage ' + $mode.Service.Demarrage) }
        'tache'   { Write-Etat -Libelle 'Mode de lancement' -Valeur 'tâche planifiée à l''ouverture de session' }
        default   { Write-Etat -Libelle 'Mode de lancement' -Valeur 'lancement manuel (ni service ni tâche pour ce dossier)' }
    }
    # Les ports sont globaux au PC : s'ils appartiennent au Worker d'un AUTRE dossier, on ne décide rien d'après eux.
    if (Test-ServiceAutreDossier -Mode $mode) { Exit-Outil -Code 2 -SansPause:$SansPause }
    $status = Invoke-ActionSure -Racine $Racine -Action 'status' -DelaiSecondes 8
    $jobs = Get-JobsActifs -Racine $Racine -Status $status
    Show-JobsActifs -Jobs $jobs
    if ($jobs.Connu -and $jobs.Nombre -gt 0) { Write-Alerte 'Un travail est noté « en cours » : il sera interrompu par l''arrêt du Worker.' }

    Write-Host ''
    if (-not (Confirm-Action -Question 'La fiche de ce Worker a-t-elle bien DISPARU de « Mes Workers » dans le site ?' -Oui:$Oui)) {
        Write-Info 'Si la fiche existe encore, ne réassocie pas : tu créerais un doublon et perdrais ses partages.'
        Write-Conseil ('Utilise ' + (Get-NomEntree 7) + ' : il demande un code de MIGRATION (Mes PC / Workers > ce Worker > « Sécurité et autorisations » > « Migrer / remplacer l''identité »).')
        Exit-Outil -Code 2 -SansPause:$SansPause
    }
    if (-not (Confirm-Action -Question 'Remplacer l''association de ce Worker ?' -MotExact 'REASSOCIER' -Oui:$Oui)) { Write-Info 'Annulé : rien n''a changé.'; Exit-Outil -Code 2 -SansPause:$SansPause }

    # ------------------------------------------------------------ adresse, nom, code
    $interactif = (-not $Oui)
    if (-not $ServeurUrl) {
        $ServeurUrl = $config.ServeurUrl
        if ($interactif) {
            $saisie = ([string](Read-Host ('   Adresse du site [Entrée = ' + $ServeurUrl + ']'))).Trim()
            if ($saisie) { $ServeurUrl = $saisie }
        }
    }
    $ServeurUrl = ([string]$ServeurUrl).Trim().TrimEnd('/')
    if ($ServeurUrl -notmatch '^https://[A-Za-z0-9][A-Za-z0-9.\-]*(:\d{1,5})?(/[A-Za-z0-9._~/\-]*)?$') {
        Write-Erreur 'L''adresse du site doit commencer par https:// et ne contenir ni espace ni guillemet.'
        Write-Conseil 'Exemple : https://dashboard.alpine-makers.ch'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    if (-not $Nom) {
        $Nom = $config.Nom
        if (-not $Nom) { $Nom = $env:COMPUTERNAME }
        if ($interactif) {
            $saisie = ([string](Read-Host ('   Nom du Worker dans le site [Entrée = ' + $Nom + ']'))).Trim()
            if ($saisie) { $Nom = $saisie }
        }
    }
    $Nom = ([string]$Nom).Trim()
    if (-not $Nom -or $Nom.Length -gt 80 -or $Nom -match '["\\\r\n]') { Write-Erreur 'Nom du Worker invalide : 1 à 80 caractères, sans guillemet ni barre oblique inverse.'; Exit-Outil -Code 1 -SansPause:$SansPause }
    if ($Code -and ($Code -notmatch $FormatCode)) { Write-Erreur 'Code d''association mal formé : trois chiffres, un tiret, trois chiffres.'; Exit-Outil -Code 1 -SansPause:$SansPause }

    Write-Section 'Ce qui va être utilisé'
    Write-Etat -Libelle 'Site' -Valeur $ServeurUrl
    Write-Etat -Libelle 'Nom du Worker' -Valeur $Nom
    if ($Code) { Write-Etat -Libelle 'Code d''association' -Valeur 'fourni (non affiché)' } else { Write-Etat -Libelle 'Code d''association' -Valeur 'il sera demandé au dernier moment' }

    $besoinElevation = ($mode.Mode -eq 'service' -and -not (Test-Administrateur))

    # ------------------------------------------------------------ simulation
    if ($Simulation) {
        Write-Section 'Ce qui serait fait'
        if ($mode.Mode -eq 'service') {
            if ($besoinElevation) { Write-Info '[simulation] 1. ouverture d''une fenêtre administrateur (accord Windows demandé) ; le code y est demandé s''il manque.' }
            Write-Info '[simulation] 2. Set-Service AlpineWorker -StartupType Manual   (sinon la supervision relancerait le service en pleine association)'
            Write-Info '[simulation] 3. Stop-Service AlpineWorker -Force, attente de Stopped (30 s), balayage des processus reconnus.'
            Write-Info '[simulation] 4. depuis le dossier du Worker : python agent.py --config config.json --pair <site> <code> --name <nom>'
            Write-Info '[simulation] 5. TOUJOURS, même en cas d''échec : Set-Service Automatic puis Start-Service AlpineWorker.'
        } else {
            Write-Info '[simulation] 1. arrêt de l''arbre du superviseur (run_worker.ps1 de ce dossier), par PID reconnu :'
            $vus = Get-PidsDuWorker -Racine $Racine
            if ($vus.Processus.Count -gt 0) { [void](Stop-PidsReconnus -Liste $vus.Processus -Racine $Racine -Outil $NomOutil -Simulation) }
            else {
                $presenceSimulee = Get-PresenceWorker -Racine $Racine -Status $status
                Write-Info ('[simulation]    aucun processus de ce dossier lisible d''ici. Ce dossier tourne-t-il ? Réponse : ' + $presenceSimulee.Etat + '.')
            }
            Write-Info '[simulation] 2. prise du verrou supervisor.lock pendant toute l''association : ni la tâche planifiée ni un'
            Write-Info '[simulation]    double-clic ne peut relancer l''agent avec l''ANCIENNE identité. Verrou déjà tenu = refus.'
            Write-Info '[simulation] 3. depuis le dossier du Worker : python agent.py --config config.json --pair <site> <code> --name <nom>'
            Write-Info '[simulation] 4. verrou rendu, puis relance cachée de run_worker.ps1, SANS droits administrateur, même en cas d''échec.'
        }
        Write-Info '[simulation] 6. vérification : l''identifiant du Worker a changé, l''agent répond.'
        Write-Info '[simulation]    En cas d''échec, l''ancienne configuration reste en place.'
        Write-Host ''
        Write-Ok 'Simulation terminée : rien n''a été arrêté, associé ni modifié.'
        Exit-Outil -Code 0 -SansPause:$SansPause
    }

    if (-not $Code -and -not $interactif) {
        Write-Erreur 'En mode piloté (-Oui), le paramètre -Code est obligatoire.'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }

    $idAvant = $config.WorkerIdCourt
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('début : mode ' + $mode.Mode + ', site ' + $ServeurUrl + ', identifiant avant = ' + $idAvant)
    $resultat = 1

    if ($mode.Mode -eq 'service') {
        Write-Section 'Association (mode service)'
        if ($besoinElevation) {
            if (-not (Test-CompteDuService -Service $mode.Service)) {
                Write-Alerte ('Le service tourne sous le compte ' + $mode.Service.Compte + ', différent de ta session.')
                Write-Conseil 'Ouvre la session Windows de ce compte pour réassocier : l''identité du Worker est chiffrée pour lui.'
                Exit-Outil -Code 1 -SansPause:$SansPause
            }
            Write-Info 'Windows va te demander ton accord pour ouvrir une fenêtre administrateur.'
            if (-not $Code) { Write-Info 'Le code d''association te sera demandé DANS cette fenêtre : génère-le juste avant.' }
            $arguments = @('-Racine', $Racine, '-Oui', '-ServeurUrl', $ServeurUrl, '-Nom', $Nom)
            if ($SansPause) { $arguments += '-SansPause' }
            # Jamais « -Code » sur la ligne de commande de la fenêtre administrateur : fichier de passage, effacé aussitôt lu.
            $fichierPassage = ''
            $retour = $null
            try {
                if ($Code) { $fichierPassage = Save-CodePourFenetreAdmin -CodeAssociation $Code; $arguments += @('-FichierCode', $fichierPassage) }
                $retour = Invoke-OutilEleve -Script $PSCommandPath -Arguments $arguments
            } finally {
                if ($fichierPassage) { Remove-Item -LiteralPath $fichierPassage -Force -ErrorAction SilentlyContinue }
                $arguments = @(); $Code = ''
            }
            if ($null -eq $retour) {
                Write-Erreur 'Tu as refusé la fenêtre administrateur : rien n''a été arrêté ni modifié.'
                Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'élévation refusée'
                Exit-Outil -Code 3 -SansPause:$SansPause
            }
            if ([int]$retour -eq 2) { Write-Info 'Annulé dans la fenêtre administrateur : rien n''a été arrêté ni modifié.'; Exit-Outil -Code 2 -SansPause:$SansPause }
            $resultat = [int]$retour
        } else {
            if (-not $Code) { Write-Info ('Génère MAINTENANT le code dans le site (' + $LibelleBoutonCode + ') : il expire après 10 minutes.'); $Code = Read-CodeAssociation }
            if (-not $Code) { Write-Erreur 'Aucun code valable : rien n''a été arrêté ni modifié.'; Exit-Outil -Code 2 -SansPause:$SansPause }
            $resultat = Invoke-PhaseService -Racine $Racine -Mode $mode -Url $ServeurUrl -NomWorker $Nom -CodeAssociation $Code
            $Code = ''
        }
    } else {
        Write-Section 'Association (mode tâche ou manuel)'
        if (Test-Administrateur) {
            Write-Erreur 'Cette fenêtre est administrateur : le Worker relancé tournerait avec des droits administrateur.'
            Write-Conseil 'Ferme-la, puis relance cet outil depuis une fenêtre normale (double-clic).'
            Exit-Outil -Code 1 -SansPause:$SansPause
        }
        if (-not $Code) { Write-Info ('Génère MAINTENANT le code dans le site (' + $LibelleBoutonCode + ') : il expire après 10 minutes.'); $Code = Read-CodeAssociation }
        if (-not $Code) { Write-Erreur 'Aucun code valable : rien n''a été arrêté ni modifié.'; Exit-Outil -Code 2 -SansPause:$SansPause }
        # Preuves propres à CE dossier d'abord (processus reconnus, réponse à status) ; les ports, globaux,
        # ne comptent que si des processus sont illisibles.
        $presence = Get-PresenceWorker -Racine $Racine -Status $status
        $arretOk = $true
        if ($presence.Etat -eq 'illisible' -or ($presence.Etat -eq 'oui' -and -not $presence.Lisible)) {
            Write-Info 'Le Worker tourne, mais ses processus sont illisibles d''ici : une fenêtre administrateur va seulement l''arrêter.'
            $retour = Invoke-OutilEleve -Script $PSCommandPath -Arguments @('-Racine', $Racine, '-SansPause', '-Oui', '-SeulementArreter')
            if ($null -eq $retour) { Write-Erreur 'Fenêtre administrateur refusée : rien n''a été modifié.'; $Code = ''; Exit-Outil -Code 3 -SansPause:$SansPause }
            $arretOk = ($retour -eq 0)
        } else {
            $arretOk = ((Stop-ArbreLocal -Racine $Racine) -eq 0)
        }
        $verrou = $null
        try {
            if ($arretOk) {
                $verrou = Open-VerrouSuperviseur -Racine $Racine
                if (-not $verrou) {
                    Write-Erreur 'Le Worker tourne encore : le verrou du superviseur (supervisor.lock) est tenu par un autre processus.'
                    Write-Conseil 'La tâche planifiée vient peut-être de le relancer. Attends une minute et relance cet outil.'
                    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'abandon : supervisor.lock déjà tenu, association non tentée'
                }
            }
            if (-not $arretOk) { Write-Erreur 'Le Worker ne s''arrête pas : association non tentée.' }
            elseif ($verrou) {
                Write-Ok 'Worker arrêté et verrouillé : rien ne peut le relancer pendant l''association.'
                Write-Info 'Association en cours auprès du serveur (jusqu''à quelques minutes)...'
                $a = Invoke-Association -Racine $Racine -Url $ServeurUrl -NomWorker $Nom -CodeAssociation $Code
                if ($a.Code -eq 0) {
                    Write-Ok 'Le serveur a accepté l''association.'
                    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'association acceptée par le serveur'
                    Remove-MarqueurDeconnexion -Racine $Racine -Outil $NomOutil
                    $resultat = 0
                } else {
                    Write-Erreur $(if ($a.Message) { $a.Message } else { 'Association impossible (aucun détail renvoyé).' })
                    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('association refusée : ' + $a.Message)
                }
            }
        } finally {
            $Code = ''
            # Rendre le verrou AVANT la relance : sinon notre propre superviseur sortirait en « déjà actif ».
            if ($verrou) { $verrou.Dispose(); $verrou = $null }
            Start-SuperviseurCache -Racine $Racine
            Write-Ok 'Superviseur du Worker relancé en arrière-plan (sans droits administrateur).'
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'superviseur relancé'
        }
    }

    # ------------------------------------------------------------ vérification
    Write-Section 'Vérification'
    $configApres = Get-ConfigSure -Racine $Racine
    $change = ($configApres.Associe -and $configApres.WorkerIdCourt -and ($configApres.WorkerIdCourt -ne $idAvant))
    if ($change) { Write-Etat -Libelle 'Identifiant du Worker' -Valeur ($idAvant + '...  ->  ' + $configApres.WorkerIdCourt + '...  (nouvelle association)') -Niveau ok }
    else { Write-Etat -Libelle 'Identifiant du Worker' -Valeur ($configApres.WorkerIdCourt + '...  (INCHANGÉ : l''ancienne configuration est restée)') -Niveau erreur }
    # Un moteur déjà ouvert maintenant a survécu à l'arrêt (orphelin) : il ne prouve pas que le nouvel agent tourne.
    $agent = Wait-Agent -Racine $Racine -DelaiSecondes 90 -MoteursDejaOuverts @(Get-MoteursOuverts -Racine $Racine)
    switch ($agent) {
        'confirme' { Write-Etat -Libelle 'Agent' -Valeur 'il répond' -Niveau ok }
        'probable' { Write-Etat -Libelle 'Agent' -Valeur 'il semble reparti' -Niveau ok }
        default    { Write-Etat -Libelle 'Agent' -Valeur 'aucun signe en 90 s : vérifie dans le site' -Niveau alerte }
    }
    # Relecture APRÈS le retour de l'agent : un agent relancé avec l'ancienne identité en mémoire
    # aurait réécrit config.json. La nouvelle association doit avoir tenu.
    if ($change) {
        $configFinale = Get-ConfigSure -Racine $Racine
        if (-not $configFinale.Associe -or $configFinale.WorkerIdCourt -ne $configApres.WorkerIdCourt) {
            $change = $false
            Write-Etat -Libelle 'Identifiant du Worker' -Valeur ('ÉCRASÉ après coup : ' + $configFinale.WorkerIdCourt + '...  (la nouvelle association n''a pas tenu)') -Niveau erreur
        } else {
            Write-Etat -Libelle 'Identifiant après relance' -Valeur ($configFinale.WorkerIdCourt + '...  (la nouvelle association a tenu)') -Niveau ok
        }
    }
    $modeApres = Get-ModeLancement -Racine $Racine
    if ($modeApres.Mode -eq 'service') {
        $niveau = 'ok'
        if ($modeApres.Service.Etat -ne 'Running' -or $modeApres.Service.Demarrage -ne 'Auto') { $niveau = 'erreur' }
        Write-Etat -Libelle 'Service AlpineWorker' -Valeur ($modeApres.Service.Etat + ', démarrage ' + $modeApres.Service.Demarrage) -Niveau $niveau
        if ($niveau -eq 'erreur') { Write-Conseil ('Lance ' + (Get-NomEntree 12) + ' : il remet le démarrage Automatique et démarre le service.') }
    }
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('fin : résultat = ' + $resultat + ', identifiant changé = ' + [bool]$change + ', agent = ' + $agent)

    Write-Host ''
    if ($resultat -eq 0 -and $change) {
        Write-Cadre -Titre 'WORKER RÉASSOCIÉ' -Couleur Green -Lignes @(
            'Il apparaît comme un NOUVEAU Worker dans « Mes Workers ».',
            'À faire maintenant dans le site :',
            '  1. vérifier que ses outils (images, 3D, Orca, PrintGuard) sont bien listés ;',
            '  2. rattacher les équipements (imprimantes, caméras) à ce nouveau Worker ;',
            '  3. refaire les partages avec les autres comptes, s''il y en avait.'
        )
        Exit-Outil -Code 0 -SansPause:$SansPause
    }
    Write-Erreur 'La réassociation n''a pas abouti. Le Worker a été relancé avec son ancienne configuration.'
    if ($resultat -ne 0 -and $besoinElevation) { Show-TraceOutil -Racine $Racine -Outil $NomOutil }
    Write-Conseil 'Causes fréquentes : code expiré (10 minutes) ou déjà utilisé, mauvaise adresse de site, pas d''internet.'
    Write-Conseil 'Génère un nouveau code, puis relance cet outil.'
    Exit-Outil -Code 1 -SansPause:$SansPause
} catch {
    $messageSur = Hide-CodeAssociation -Texte $_.Exception.Message -CodeAssociation $Code
    $Code = ''
    Write-Erreur ('Erreur inattendue : ' + $messageSur)
    if (-not $Simulation) { try { Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('erreur inattendue : ' + $messageSur) } catch { } }
    Exit-Outil -Code 1 -SansPause:$SansPause
}
