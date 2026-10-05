<#
    Sauvegarde du Worker Alpine Makers (réglages, identité, journal, fiches) dans une
    archive datée. Ne sauvegarde PAS les moteurs IA (components\, des centaines de Gio,
    réinstallables) ni les dossiers de travail.

    Usage :
      sauvegarder_worker.ps1                       crée une sauvegarde
      sauvegarder_worker.ps1 -Lister               liste les sauvegardes + marche à suivre pour restaurer
      sauvegarder_worker.ps1 -Destination D:\...   autre dossier de destination
      -Simulation : montre ce qui serait fait, n'écrit rien.  -Oui / -SansPause : sans clavier.
      -SansJournal : n'écrit pas la ligne de trace dans logs\outils-worker.log.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause,
    [switch]$Oui,
    [switch]$Simulation,
    [string]$Destination = '',
    [switch]$Lister,
    [switch]$SansJournal,
    [switch]$DejaEleve
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
Initialize-ConsoleWorker -Titre 'Sauvegarder le Worker'
$Racine = Get-RacineWorker -Racine $Racine   # déjà en forme LONGUE (un nom court PRENOM~1 fausserait les comparaisons de dossiers)

$NomOutil = 'sauvegarder_worker'
# Dossiers exclus AU PREMIER NIVEAU seulement (moteurs, caches, travail en cours).
$DossiersExclus = @('components', 'cache', 'temp', 'outputs', 'workspace', 'jobs', '__pycache__')
# Caches de navigateur embarqué (PrintGuard/WebView2) : exclus PAR NOM à tout niveau, ils pèsent
# des centaines de Mio et se reconstruisent seuls. Les réglages du même profil sont gardés.
$CachesExclus = @('Cache_Data', 'Code Cache', 'GPUCache', 'GrShaderCache', 'ShaderCache', 'DawnCache')
# Fichiers jamais copiés à chaud : le journal SQLite (pris à part, proprement) et le verrou du superviseur.
$FichiersExclus = @('runtime\worker-journal.sqlite3', 'runtime\worker-journal.sqlite3-wal', 'runtime\worker-journal.sqlite3-shm', 'runtime\worker-journal.sqlite3-journal', 'supervisor.lock')

# Format-Taille : commun.ps1.

function Get-TailleArbre {
    <# Taille d'un dossier SANS suivre les liens ni les jonctions. #>
    param([string]$Chemin)
    $total = [double]0
    if ($CachesExclus -contains (Split-Path -Leaf $Chemin)) { return $total }
    $elements = @()
    try { $elements = @(Get-ChildItem -LiteralPath $Chemin -Force -ErrorAction Stop) } catch { return $total }
    foreach ($e in $elements) {
        if ($e.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        if ($e.PSIsContainer) { $total += (Get-TailleArbre -Chemin $e.FullName) } else { $total += [double]$e.Length }
    }
    return $total
}

function Get-DossiersCandidats {
    <# Dossiers où des sauvegardes peuvent se trouver (sans rien créer). #>
    $liste = @()
    if ($Destination) { $liste += [IO.Path]::GetFullPath($Destination).TrimEnd('\') }
    $liste += (Join-Path $env:ProgramData 'AlpineMakers\sauvegardes-worker')
    if ($env:LOCALAPPDATA) { $liste += (Join-Path $env:LOCALAPPDATA 'AlpineMakers\sauvegardes-worker') }
    return @($liste | Select-Object -Unique)
}

function Test-DossierInscriptible {
    <# Crée le dossier si besoin et y écrit un fichier témoin, aussitôt retiré. #>
    param([string]$Dossier)
    try {
        if (-not (Test-Path -LiteralPath $Dossier -PathType Container)) { New-Item -ItemType Directory -Path $Dossier -Force -ErrorAction Stop | Out-Null }
        $temoin = Join-Path $Dossier ('.essai-ecriture-' + [Guid]::NewGuid().ToString('N') + '.tmp')
        [IO.File]::WriteAllText($temoin, 'ok')
        Remove-Item -LiteralPath $temoin -Force -ErrorAction SilentlyContinue
        return $true
    } catch { return $false }
}

function Get-SidsAutorises {
    <# Seuls lecteurs admis d'un dossier de sauvegardes : SYSTEM, Administrateurs, et TON compte. #>
    return @('S-1-5-18', 'S-1-5-32-544', [string]([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value)
}

function Get-LecteursEnTrop {
    <#
        Comptes ou groupes, AUTRES que SYSTEM / Administrateurs / toi, qui ont un droit sur ce
        dossier (exemple : « BUILTIN\Utilisateurs », hérité de C:\ProgramData). Renvoie leurs noms ;
        $null si les droits sont illisibles.
    #>
    param([string]$Dossier)
    try {
        $acl = Get-Acl -LiteralPath $Dossier -ErrorAction Stop
        $admis = Get-SidsAutorises
        $enTrop = @()
        foreach ($regle in @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))) {
            if ($regle.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
            $sid = [string]$regle.IdentityReference.Value
            if ($admis -contains $sid) { continue }
            # CREATEUR PROPRIETAIRE / GROUPE CREATEUR : simples modèles d'héritage (droits de chacun sur SES propres fichiers), pas des lecteurs.
            if ($sid -eq 'S-1-3-0' -or $sid -eq 'S-1-3-1') { continue }
            $nom = $sid
            try { $nom = [string]$regle.IdentityReference.Translate([Security.Principal.NTAccount]).Value } catch { }
            if ($enTrop -notcontains $nom) { $enTrop += $nom }
        }
        return , $enTrop
    } catch { return $null }
}

function Protect-DossierSauvegardes {
    <#
        Réserve le dossier des sauvegardes à SYSTEM, aux Administrateurs et à TON compte : coupe
        l'héritage de C:\ProgramData (qui donne la lecture à tous les comptes du PC), puis VÉRIFIE
        le résultat. N'est appelée que pour ...\AlpineMakers\sauvegardes-worker, jamais pour le
        dossier parent (partagé avec le superviseur) ni pour un -Destination choisi par toi.
        Renvoie $true seulement si, après coup, personne d'autre n'a de droit sur le dossier ET si
        son PROPRIÉTAIRE est SYSTEM, Administrateurs ou toi. C:\ProgramData\AlpineMakers laisse tout
        compte créer un sous-dossier : un autre compte qui aurait créé « sauvegardes-worker » à l'avance
        en resterait propriétaire et pourrait se redonner la lecture après coup. Dans ce cas : $false,
        et la sauvegarde va sous ton profil Windows.
    #>
    param([string]$Dossier)
    if ((Split-Path -Leaf $Dossier) -ine 'sauvegardes-worker') { return $false }
    $sidUtilisateur = [string]([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
    $ancien = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & icacls.exe $Dossier /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' ('*' + $sidUtilisateur + ':(OI)(CI)F') 2>&1 | Out-Null
    $codeIcacls = $LASTEXITCODE
    $ErrorActionPreference = $ancien
    if ($codeIcacls -ne 0) { return $false }
    $enTrop = Get-LecteursEnTrop -Dossier $Dossier
    if ($null -eq $enTrop) { return $false }
    if (@($enTrop).Count -ne 0) { return $false }
    try {
        $proprietaire = [string](Get-Acl -LiteralPath $Dossier -ErrorAction Stop).GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ((Get-SidsAutorises) -notcontains $proprietaire) { return $false }
    } catch { return $false }
    return $true
}

function Get-Sauvegardes {
    $trouvees = @()
    foreach ($d in (Get-DossiersCandidats)) {
        if (-not (Test-Path -LiteralPath $d -PathType Container)) { continue }
        $trouvees += @(Get-ChildItem -LiteralPath $d -Filter 'worker-*.zip' -File -ErrorAction SilentlyContinue)
    }
    return @($trouvees | Sort-Object LastWriteTime -Descending)
}

function Show-Sauvegardes {
    $liste = @(Get-Sauvegardes)
    Write-Section 'Sauvegardes présentes'
    if ($liste.Count -eq 0) {
        Write-Info 'Aucune sauvegarde trouvée pour l''instant.'
        foreach ($d in (Get-DossiersCandidats)) { Write-Info ('  dossier regardé : ' + $d) }
        return
    }
    $total = [double]0
    foreach ($f in $liste) {
        $total += [double]$f.Length
        Write-Etat -Libelle $f.LastWriteTime.ToString('dd.MM.yyyy HH:mm') -Valeur ((Format-Taille $f.Length).PadLeft(10) + '   ' + $f.FullName) -Niveau info
    }
    Write-Host ''
    Write-Etat -Libelle 'Nombre de sauvegardes' -Valeur ([string]$liste.Count) -Niveau info
    Write-Etat -Libelle 'Place occupée au total' -Valeur (Format-Taille $total) -Niveau info
    Write-Conseil 'Rien n''est supprimé automatiquement. Si la place manque, supprime toi-même les plus anciennes archives depuis l''Explorateur.'
    foreach ($d in @($liste | ForEach-Object { $_.DirectoryName } | Select-Object -Unique)) {
        $enTrop = Get-LecteursEnTrop -Dossier $d
        if ($null -ne $enTrop -and @($enTrop).Count -gt 0) {
            Write-Alerte ('Ces sauvegardes sont LISIBLES par d''autres comptes de ce PC (' + (@($enTrop) -join ', ') + ') : ' + $d)
            Write-Conseil 'Elles contiennent l''identité du Worker. La prochaine sauvegarde dans le dossier par défaut le réserve à ton compte ; pour un dossier choisi par toi, règle ses droits (Propriétés > Sécurité).'
        }
    }
}

function Show-MarcheRestauration {
    Write-Section 'Restaurer une sauvegarde (à faire à la main)'
    Write-Info ('1. Éteins le Worker avec ' + (Get-NomEntree 11) + ' (ne restaure jamais pendant qu''il tourne).')
    Write-Info '2. Ouvre l''archive worker-....zip (double-clic) : c''est un dossier compressé ordinaire.'
    Write-Info '3. Copie SEULEMENT ce dont tu as besoin vers le dossier du Worker, par exemple :'
    Write-Info '     config.json et le dossier config\   = réglages et identité du Worker ;'
    Write-Info '     runtime\worker-journal.sqlite3       = journal durable des tâches et des moteurs ;'
    Write-Info '     storage.json, python_path.txt        = emplacements et interpréteur.'
    Write-Info '   Ne remplace pas agent.py ni les scripts : la mise à jour du Worker s''en charge.'
    Write-Info ('4. Rallume le Worker avec ' + (Get-NomEntree 12) + ', puis vérifie qu''il est en ligne sur le site.')
    Write-Host ''
    Write-Alerte 'La clé d''identité du Worker est liée à TON compte Windows sur CE PC.'
    Write-Info  ('Restaurée sur un autre PC ou sous un autre compte, elle ne fonctionnera pas : il faudra refaire l''association avec ' + (Get-NomEntree 8) + ', et un code créé sur le site (Compte > Mes PC / Workers > bouton « Associer un PC »).')
    Write-Alerte 'L''archive contient config.json et l''identité : ne la partage jamais et ne la pousse jamais sur GitHub.'
}

# ---------------------------------------------------------------------------

Write-Explication -Titre 'SAUVEGARDER LE WORKER' -Fait @(
    'Crée une archive datée worker-<date>_<heure>.zip avec les réglages, l''identité, les scripts, les fiches des moteurs, les journaux et le journal durable des tâches.',
    'Copie le journal SQLite par la méthode officielle de sauvegarde SQLite : le Worker peut rester allumé.',
    'Choix 2 : montre les sauvegardes présentes, leur taille, et comment restaurer à la main.'
) -NeFaitPas @(
    'Ne sauvegarde pas les moteurs IA (components\) ni cache\, temp\, outputs\, workspace\, jobs\ : ils sont énormes et se réinstallent.',
    'Laisse aussi de côté les caches du navigateur embarqué de PrintGuard (ils se reconstruisent seuls).',
    'N''arrête rien, ne modifie rien dans le dossier du Worker, ne supprime aucune ancienne sauvegarde.',
    'Ne restaure rien tout seul : la restauration se fait à la main, Worker éteint.'
) -Exige @(
    'Quelques centaines de Mio libres sur le disque de destination.',
    'Le dossier par défaut est réservé à ton compte (plus les administrateurs et Windows) avant toute copie ; s''il ne peut pas l''être, la sauvegarde va sous ton profil Windows.',
    'Aucun droit administrateur.'
)

# Lancé sans paramètre (depuis le menu) : l'outil propose lui-même les deux usages.
if ((-not $Lister) -and (-not $Oui) -and (-not $SansPause) -and (-not $Simulation)) {
    Write-Section 'Que veux-tu faire ?'
    Write-Info '  1. Créer une sauvegarde maintenant'
    Write-Info '  2. Voir les sauvegardes présentes et comment restaurer'
    Write-Info '  0. Rien, revenir en arrière'
    $choix = (Read-Host '   Ton choix [1/2/0]').Trim()
    if ($choix -eq '2') { $Lister = $true }
    elseif ($choix -eq '1') { $Oui = $true }      # le choix 1 vaut confirmation
    else {
        Write-Info 'Annulé : aucune sauvegarde créée.'
        Wait-FinOutil -SansPause:$SansPause
        exit 2
    }
}

if ($Lister) {
    Show-Sauvegardes
    Show-MarcheRestauration
    Wait-FinOutil -SansPause:$SansPause
    exit 0
}

# --- Destination -----------------------------------------------------------
# Qui pourra lire l'archive : reserve (dossier par défaut, droits restreints et vérifiés),
# profil (repli sous ton profil Windows), choisi (-Destination : droits laissés tels quels).
$protection = 'simulation'
if ($Destination) { $protection = 'choisi' }
$dossierSauvegardes = ''
if ($Destination) {
    $dossierSauvegardes = $Destination
} else {
    $dossierSauvegardes = Join-Path $env:ProgramData 'AlpineMakers\sauvegardes-worker'
}
$racineAvecBarre = [IO.Path]::GetFullPath($Racine).TrimEnd([char]92) + [char]92
$destComplete = [IO.Path]::GetFullPath($dossierSauvegardes).TrimEnd('\')
if (($destComplete + '\').StartsWith($racineAvecBarre, [StringComparison]::OrdinalIgnoreCase)) {
    Write-Erreur 'La destination ne doit pas se trouver DANS le dossier du Worker. Choisis un autre dossier.'
    Wait-FinOutil -SansPause:$SansPause
    exit 1
}
$dossierSauvegardes = $destComplete

if (-not $Simulation) {
    if (-not (Test-DossierInscriptible -Dossier $dossierSauvegardes)) {
        if ($Destination) {
            Write-Erreur ('Impossible d''écrire dans ' + $dossierSauvegardes + '. Choisis un autre dossier avec -Destination.')
            Wait-FinOutil -SansPause:$SansPause
            exit 1
        }
        $repli = Join-Path $env:LOCALAPPDATA 'AlpineMakers\sauvegardes-worker'
        Write-Alerte ('Impossible d''écrire dans ' + $dossierSauvegardes + ' : repli sur ' + $repli)
        if (-not (Test-DossierInscriptible -Dossier $repli)) {
            Write-Erreur 'Le dossier de repli n''est pas inscriptible non plus. Sauvegarde abandonnée.'
            Wait-FinOutil -SansPause:$SansPause
            exit 1
        }
        $dossierSauvegardes = $repli
    }
    # C:\ProgramData donne la LECTURE à tous les comptes du PC, et l'archive contient l'identité du
    # Worker : le dossier par défaut est réservé AVANT d'y copier quoi que ce soit.
    $dossierParDefaut = [IO.Path]::GetFullPath((Join-Path $env:ProgramData 'AlpineMakers\sauvegardes-worker')).TrimEnd('\')
    if ((-not $Destination) -and ($dossierSauvegardes -ieq $dossierParDefaut)) {
        if (Protect-DossierSauvegardes -Dossier $dossierSauvegardes) {
            $protection = 'reserve'
        } else {
            $repli = Join-Path $env:LOCALAPPDATA 'AlpineMakers\sauvegardes-worker'
            Write-Alerte ('Impossible de réserver ' + $dossierSauvegardes + ' à ton compte : les autres comptes du PC pourraient lire l''archive.')
            Write-Alerte ('Repli sur ton profil Windows, privé par nature : ' + $repli)
            if (-not (Test-DossierInscriptible -Dossier $repli)) {
                Write-Erreur 'Le dossier de repli n''est pas inscriptible. Sauvegarde abandonnée.'
                Wait-FinOutil -SansPause:$SansPause
                exit 1
            }
            $dossierSauvegardes = $repli
            $protection = 'profil'
        }
    } elseif ($Destination) {
        $protection = 'choisi'
    } else {
        $protection = 'profil'
    }
}

# --- Estimation ------------------------------------------------------------
Write-Section 'Ce qui sera sauvegardé'
$aCopier = [double]0
$premierNiveau = @(Get-ChildItem -LiteralPath $Racine -Force -ErrorAction SilentlyContinue)
foreach ($e in $premierNiveau) {
    if ($e.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
    if ($e.PSIsContainer) {
        if ($DossiersExclus -contains $e.Name) { continue }
        $aCopier += (Get-TailleArbre -Chemin $e.FullName)
    } else {
        $aCopier += [double]$e.Length
    }
}
$journalSource = Join-Path $Racine 'runtime\worker-journal.sqlite3'
$journalPresent = Test-Path -LiteralPath $journalSource -PathType Leaf
Write-Etat -Libelle 'Dossier du Worker' -Valeur $Racine -Niveau info
Write-Etat -Libelle 'Volume à copier (environ)' -Valeur (Format-Taille $aCopier) -Niveau info
Write-Etat -Libelle 'Dossiers laissés de côté' -Valeur ($DossiersExclus -join ', ') -Niveau info
if ($journalPresent) { Write-Etat -Libelle 'Journal durable (SQLite)' -Valeur 'présent : copié par la sauvegarde SQLite' -Niveau ok }
else { Write-Etat -Libelle 'Journal durable (SQLite)' -Valeur 'absent (Worker jamais lancé ?)' -Niveau alerte }
Write-Etat -Libelle 'Destination' -Valeur $dossierSauvegardes -Niveau info
switch ($protection) {
    'reserve'    { Write-Etat -Libelle 'Qui peut lire ce dossier' -Valeur 'toi, les administrateurs et Windows (héritage de ProgramData coupé, vérifié)' -Niveau ok }
    'profil'     { Write-Etat -Libelle 'Qui peut lire ce dossier' -Valeur 'ton profil Windows : privé par nature' -Niveau ok }
    'choisi'     {
        $enTrop = Get-LecteursEnTrop -Dossier $dossierSauvegardes
        if (-not (Test-Path -LiteralPath $dossierSauvegardes -PathType Container)) { Write-Etat -Libelle 'Qui peut lire ce dossier' -Valeur 'dossier pas encore créé : il prendra les droits de son dossier parent' -Niveau info }
        elseif ($null -eq $enTrop) { Write-Etat -Libelle 'Qui peut lire ce dossier' -Valeur 'droits illisibles : vérifie-les toi-même' -Niveau alerte }
        elseif (@($enTrop).Count -eq 0) { Write-Etat -Libelle 'Qui peut lire ce dossier' -Valeur 'toi, les administrateurs et Windows' -Niveau ok }
        else { Write-Etat -Libelle 'Qui peut lire ce dossier' -Valeur ('AUSSI : ' + (@($enTrop) -join ', ')) -Niveau alerte }
        Write-Info 'Dossier choisi par toi (-Destination) : cet outil ne touche pas à ses droits. L''archive contient l''identité du Worker.'
    }
    default      { Write-Etat -Libelle 'Qui peut lire ce dossier' -Valeur 'simulation : le dossier par défaut serait réservé à ton compte avant la copie' -Niveau info }
}

$libre = $null
try {
    $lecteur = [IO.Path]::GetPathRoot($dossierSauvegardes)
    $libre = [double](New-Object IO.DriveInfo($lecteur)).AvailableFreeSpace
} catch { }
if ($null -ne $libre) {
    $niveau = 'ok'
    if ($libre -lt ($aCopier * 2.2)) { $niveau = 'erreur' }
    Write-Etat -Libelle 'Place libre sur ce disque' -Valeur (Format-Taille $libre) -Niveau $niveau
    if ($niveau -eq 'erreur') {
        Write-Erreur 'Pas assez de place : il faut environ deux fois le volume à copier (copie intermédiaire + archive).'
        Write-Conseil 'Libère de la place ou choisis un autre disque avec -Destination.'
        Wait-FinOutil -SansPause:$SansPause
        exit 1
    }
}

$horodatage = Get-Date -Format 'yyyy-MM-dd_HH-mm-ss'
$archive = Join-Path $dossierSauvegardes ('worker-' + $horodatage + '.zip')
$intermediaire = Join-Path $dossierSauvegardes ('_copie-' + $horodatage)

if ($Simulation) {
    Write-Host ''
    Write-Alerte 'SIMULATION : rien n''a été copié ni créé.'
    Write-Info ('Archive qui serait créée : ' + $archive)
    Wait-FinOutil -SansPause:$SansPause
    exit 0
}

if (-not (Confirm-Action -Question 'Créer la sauvegarde maintenant ?' -Oui:$Oui)) {
    Write-Info 'Annulé : aucune sauvegarde créée.'
    Wait-FinOutil -SansPause:$SansPause
    exit 2
}

$codeFinal = 0
$journalRobocopy = Join-Path $dossierSauvegardes ('_copie-' + $horodatage + '.log')
try {
    # --- 1. Copie intermédiaire par robocopy (tolère les fichiers ouverts) ---
    Write-Section 'Copie des fichiers'
    $argsRobocopy = @($Racine, $intermediaire, '/E', '/R:0', '/W:0', '/XJ', '/COPY:DAT', '/DCOPY:DAT', '/NP', '/NFL', '/NDL', '/NJH', ('/UNILOG:' + $journalRobocopy))
    $argsRobocopy += '/XD'
    foreach ($d in $DossiersExclus) { $argsRobocopy += (Join-Path $Racine $d) }     # chemins COMPLETS : premier niveau seulement
    foreach ($d in $CachesExclus) { $argsRobocopy += $d }                           # simples NOMS : caches exclus à tout niveau
    $argsRobocopy += '/XF'
    foreach ($f in $FichiersExclus) { $argsRobocopy += (Join-Path $Racine $f) }
    & robocopy.exe @argsRobocopy | Out-Null
    $codeRobocopy = $LASTEXITCODE
    if ($codeRobocopy -ge 16) { throw ('robocopy a échoué (code ' + $codeRobocopy + '). Détail : ' + $journalRobocopy) }
    if ($codeRobocopy -ge 8) {
        $codeFinal = 1
        Write-Alerte ('Certains fichiers n''ont pas pu être copiés (ouverts par un autre programme). Code robocopy : ' + $codeRobocopy)
        Write-Info ('Le détail est gardé dans : ' + $journalRobocopy)
    } else {
        Write-Ok ('Fichiers copiés (code robocopy ' + $codeRobocopy + ' = succès).')
    }

    # --- 2. Journal SQLite par l'API de sauvegarde (copie cohérente, à chaud) ---
    if ($journalPresent) {
        Write-Section 'Journal durable'
        $python = Get-PythonWorker -Racine $Racine
        if (-not $python) {
            $codeFinal = 1
            Write-Alerte 'Python du Worker introuvable : le journal durable n''est PAS dans cette sauvegarde.'
        } else {
            $dossierRuntime = Join-Path $intermediaire 'runtime'
            if (-not (Test-Path -LiteralPath $dossierRuntime)) { New-Item -ItemType Directory -Path $dossierRuntime -Force | Out-Null }
            $journalCopie = Join-Path $dossierRuntime 'worker-journal.sqlite3'
            # Aucun guillemet double dans ce code : PowerShell 5.1 les abîme en passant l'argument.
            # as_uri() encode correctement l'espace et l'accent du profil Windows ; mode=ro = lecture seule.
            $codePython = @'
import sys, sqlite3, pathlib
uri = pathlib.Path(sys.argv[1]).resolve().as_uri() + '?mode=ro'
source = sqlite3.connect(uri, uri=True, timeout=30)
copie = sqlite3.connect(sys.argv[2])
try:
    source.backup(copie)
    etat = copie.execute('PRAGMA integrity_check').fetchone()[0]
finally:
    copie.close()
    source.close()
print('INTEGRITE=' + str(etat))
sys.exit(0 if etat == 'ok' else 1)
'@
            $ancien = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            $sortiePython = & $python -c $codePython $journalSource $journalCopie 2>&1
            $codePythonSortie = $LASTEXITCODE
            $ErrorActionPreference = $ancien
            $texte = ($sortiePython | ForEach-Object { [string]$_ }) -join ' '
            if ($codePythonSortie -eq 0 -and $texte -match 'INTEGRITE=ok') {
                Write-Ok ('Journal durable copié et vérifié (' + (Format-Taille (Get-Item -LiteralPath $journalCopie).Length) + ').')
            } else {
                $codeFinal = 1
                Write-Alerte 'La copie du journal durable a échoué : il n''est PAS garanti dans cette sauvegarde.'
                if ($texte) { Write-Info ('Détail : ' + $texte.Substring(0, [Math]::Min(300, $texte.Length))) }
                if (Test-Path -LiteralPath $journalCopie) { Remove-Item -LiteralPath $journalCopie -Force -ErrorAction SilentlyContinue }
            }
        }
    }

    # --- 3. Archive -------------------------------------------------------------
    Write-Section 'Création de l''archive'
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory($intermediaire, $archive, [IO.Compression.CompressionLevel]::Optimal, $false)
    $infoArchive = Get-Item -LiteralPath $archive
    Write-Ok ('Archive créée : ' + $infoArchive.FullName)
    Write-Etat -Libelle 'Taille de l''archive' -Valeur (Format-Taille $infoArchive.Length) -Niveau ok
    if (-not $SansJournal) { Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('Sauvegarde créée : ' + $infoArchive.FullName + ' (' + (Format-Taille $infoArchive.Length) + ')') }
} catch {
    $codeFinal = 1
    Write-Erreur ('La sauvegarde a échoué : ' + $_.Exception.Message)
    if (Test-Path -LiteralPath $archive) { Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue }
} finally {
    # La copie intermédiaire vit dans le dossier des sauvegardes, jamais dans le Worker.
    $feuille = Split-Path -Leaf $intermediaire
    if ($feuille -like '_copie-*' -and (Test-Path -LiteralPath $intermediaire -PathType Container) -and -not (([IO.Path]::GetFullPath($intermediaire) + '\').StartsWith($racineAvecBarre, [StringComparison]::OrdinalIgnoreCase))) {
        Remove-Item -LiteralPath $intermediaire -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($codeFinal -eq 0 -and (Test-Path -LiteralPath $journalRobocopy)) { Remove-Item -LiteralPath $journalRobocopy -Force -ErrorAction SilentlyContinue }
}

Write-Host ''
Write-Cadre -Titre 'ATTENTION : ARCHIVE CONFIDENTIELLE' -Lignes @(
    'Elle contient config.json et l''identité du Worker.',
    'Ne la partage jamais. Ne l''envoie pas par e-mail.',
    'Ne la pousse jamais sur GitHub.',
    'Ne la copie pas dans un dossier lisible par d''autres comptes du PC.'
) -Couleur Yellow

Show-Sauvegardes
Write-Conseil ('Pour restaurer : relance ' + (Get-NomEntree 13) + ' et choisis 2 (sans le menu : outils\sauvegarder_worker.bat -Lister).')
Wait-FinOutil -SansPause:$SansPause
exit $codeFinal
