<#
    Fabrique un fichier ZIP de diagnostic à envoyer au support, SANS aucun secret.

    Usage :
        rapport_diagnostic.ps1                          zip sur le Bureau
        rapport_diagnostic.ps1 -Dossier "D:\Rapports"   zip dans ce dossier
        rapport_diagnostic.ps1 -SansPause               sans attendre la touche Entrée

    Contenu : état complet (JSON), jobs bloquants (JSON), 300 dernières lignes de chaque
    journal, storage.json, agent_manifest.json, liste des fichiers de la racine, mode de
    lancement, sortie de nvidia-smi.
    Jamais inclus : config.json, config\, tout fichier *token*, le journal SQLite.
    Masqué partout : lignes contenant token/authorization/bearer/password, codes 123-456.

    Codes de sortie : 0 = zip créé, 1 = échec.
#>
param(
    [string]$Racine = '',
    [string]$Dossier = '',
    [switch]$SansPause
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'diagnostic_commun.ps1')
Initialize-ConsoleWorker -Titre 'Rapport de diagnostic du Worker'
try { $Racine = Get-RacineWorker -Racine $Racine } catch { Write-Erreur ([string]$_.Exception.Message); Wait-FinOutil -SansPause:$SansPause; exit 1 }

Write-Explication -Titre 'Rapport de diagnostic' -Fait @(
    'Rassemble dans UN fichier ZIP tout ce qu''il faut pour comprendre une panne :',
    'état complet, jobs bloquants, fin des journaux, version, mode de lancement, carte graphique.',
    'Dépose le ZIP sur le Bureau : tu peux l''envoyer tel quel au support.'
) -NeFaitPas @(
    'N''inclut JAMAIS config.json, le dossier config\, un fichier de token ni le journal SQLite.',
    'Masque toute ligne parlant de token ou de mot de passe, et tout code d''association.',
    'Ne modifie rien dans le dossier du Worker, n''arrête et ne démarre rien.'
) -Exige @(
    'Rien. Aucune fenêtre administrateur. Compte 10 à 20 secondes.'
)

# --- Destination ------------------------------------------------------------
function Get-CheminLong {
    <#
        Chemin absolu en noms LONGS, SANS rien créer. « C:\Users\PRENOM~1\Desktop » et
        « C:\Users\Prénom Nom\Desktop » désignent le même dossier : pour comparer deux
        chemins il faut la même écriture des deux côtés. Chaque composant EXISTANT est
        remplacé par son vrai nom, tel que le donne son dossier parent ; la partie qui
        n'existe pas encore est gardée telle quelle. Lève une exception si le chemin est invalide.
    #>
    param([string]$Chemin)
    $complet = [System.IO.Path]::GetFullPath($Chemin)
    $racineLecteur = [System.IO.Path]::GetPathRoot($complet)
    $courant = $racineLecteur
    $existe = $true
    foreach ($partie in @($complet.Substring($racineLecteur.Length).Split([char]92) | Where-Object { $_ })) {
        if ($partie.IndexOfAny([char[]]'*?') -ge 0) { throw 'Caractère générique interdit dans un chemin.' }
        $nom = $partie
        if ($existe) {
            $trouve = @()
            try { $trouve = @((New-Object System.IO.DirectoryInfo($courant)).GetFileSystemInfos($partie)) } catch { $trouve = @() }
            if ($trouve.Count -ge 1) { $nom = $trouve[0].Name } else { $existe = $false }
        }
        $courant = Join-Path $courant $nom
    }
    return $courant.TrimEnd([char]92)
}

if (-not $Dossier) { $Dossier = [Environment]::GetFolderPath('Desktop') }
if (-not $Dossier) { $Dossier = $env:USERPROFILE }
# Ordre voulu : 1. calculer la destination SANS la créer ; 2. la refuser si elle est dans le
# Worker ; 3. seulement alors la créer. Un refus ne doit laisser aucune trace dans le Worker.
$cible = ''
$racineLongue = ''
try {
    $cible = Get-CheminLong -Chemin $Dossier
    $racineLongue = Get-CheminLong -Chemin $Racine
} catch { Write-Erreur ('Dossier de destination inutilisable : ' + $Dossier); Wait-FinOutil -SansPause:$SansPause; exit 1 }
if ($cible -ieq $racineLongue -or $cible.ToLower().StartsWith($racineLongue.ToLower() + '\')) {
    Write-Erreur 'Choisis une destination HORS du dossier du Worker (le Bureau par exemple).'
    Write-Info 'Rien n''a été créé.'
    Wait-FinOutil -SansPause:$SansPause; exit 1
}
try {
    if (-not (Test-Path -LiteralPath $cible -PathType Container)) { [void][System.IO.Directory]::CreateDirectory($cible) }
} catch { Write-Erreur ('Dossier de destination inutilisable : ' + $cible); Wait-FinOutil -SansPause:$SansPause; exit 1 }
$Dossier = $cible
$horodatage = Get-Date -Format 'yyyy-MM-dd_HH-mm'
$zip = Join-Path $Dossier ('rapport-worker-' + $horodatage + '.zip')
$travail = Join-Path ([System.IO.Path]::GetTempPath()) ('rapport-worker-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $travail -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $travail 'journaux') -Force | Out-Null

$utf8 = New-Object System.Text.UTF8Encoding($true)
$contenu = New-Object System.Collections.ArrayList
function Save-Texte {
    <# Tout texte passe par le masquage avant d'être écrit. #>
    param([string]$Nom, [string[]]$Lignes, [string]$Description)
    $propre = Protect-TexteRapport -Lignes @($Lignes)
    [System.IO.File]::WriteAllLines((Join-Path $travail $Nom), [string[]]$propre, $utf8)
    [void]$contenu.Add(('{0,-44} {1}' -f $Nom, $Description))
}
function Invoke-OutilFils {
    <# Lance un outil voisin dans un PowerShell séparé et renvoie sa sortie (lignes). #>
    param([string]$Script, [string[]]$Arguments)
    $ErrorActionPreference = 'Continue'
    $sortie = @()
    try { $sortie = @(& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Script @Arguments 2>$null | ForEach-Object { [string]$_ }) } catch { }
    return ,@($sortie)
}
function ConvertTo-JsonLisible {
    <# Une clé par ligne : le masquage ligne à ligne ne supprime alors que la ligne fautive. #>
    param([string[]]$Lignes)
    $ligne = [string](@($Lignes | Where-Object { ([string]$_).TrimStart().StartsWith('{') }) | Select-Object -Last 1)
    if (-not $ligne) { return ,@('{"ok": false, "erreur": "aucune sortie"}') }
    try { return ,@((($ligne | ConvertFrom-Json) | ConvertTo-Json -Depth 12) -split "`r?`n") } catch { return ,@($ligne) }
}

$echecs = 0
$zipEntame = $false
try {
    # 1. État complet
    Write-Section 'Collecte'
    Write-Info 'État complet du Worker...'
    $etat = Invoke-OutilFils -Script (Join-Path $PSScriptRoot 'etat_worker.ps1') -Arguments @('-Racine', $Racine, '-Json', '-SansPause')
    Save-Texte -Nom 'etat_worker.json' -Lignes (ConvertTo-JsonLisible -Lignes $etat) -Description 'État complet (sortie de etat_worker -Json)'
    Write-Ok 'État complet'

    # 2. Jobs bloquants
    $python = Get-PythonWorker -Racine $Racine
    if ($python) {
        $ErrorActionPreference = 'Continue'
        $jobs = @()
        try { $jobs = @(& $python (Join-Path $PSScriptRoot 'jobs_bloquants.py') --root $Racine --json 2>$null | ForEach-Object { [string]$_ }) } catch { }
        $ErrorActionPreference = 'Stop'
        Save-Texte -Nom 'jobs_bloquants.json' -Lignes (ConvertTo-JsonLisible -Lignes $jobs) -Description 'Jobs actifs du journal (lecture seule)'
        Write-Ok 'Jobs bloquants'
    } else {
        Save-Texte -Nom 'jobs_bloquants.json' -Lignes @('{"ok": false, "erreur": "python introuvable"}') -Description 'Jobs actifs : Python introuvable'
        Write-Alerte 'Jobs bloquants : Python introuvable, section vide'
        $echecs++
    }

    # 3. Journaux : 300 dernières lignes de chacun
    foreach ($j in @(Get-JournauxConnus -Racine $Racine | Where-Object { $_.Existe })) {
        $fin = Get-FinFichier -Chemin $j.Chemin -Lignes 300
        $entete = @('# ' + $j.Nom, '# ' + $j.Chemin, '# taille totale : ' + (Format-Taille $j.Taille) + ' ; ' + $fin.Count + ' dernières lignes', '')
        Save-Texte -Nom ('journaux\' + $j.Cle + '.log.txt') -Lignes ($entete + $fin) -Description ('Fin du journal : ' + $j.Nom)
    }
    # Journaux de logs\ qui ne figurent pas dans la liste connue (moteur ajouté plus tard).
    $connus = @(Get-JournauxConnus -Racine $Racine | ForEach-Object { $_.Chemin.ToLower() })
    $dossierLogs = Join-Path $Racine 'logs'
    if (Test-Path -LiteralPath $dossierLogs -PathType Container) {
        foreach ($f in @(Get-ChildItem -LiteralPath $dossierLogs -Filter '*.log' -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -notmatch '(?i)token' -and ($connus -notcontains $_.FullName.ToLower()) })) {
            $fin = Get-FinFichier -Chemin $f.FullName -Lignes 300
            Save-Texte -Nom ('journaux\autre-' + ($f.BaseName -replace '[^A-Za-z0-9_-]', '_') + '.log.txt') -Lignes (@('# ' + $f.FullName, '') + $fin) -Description ('Fin du journal : ' + $f.Name)
        }
    }
    Write-Ok 'Journaux (300 dernières lignes chacun)'

    # 4. Fichiers de description, sans secret par construction, masqués quand même
    foreach ($nom in @('storage.json', 'agent_manifest.json')) {
        $source = Join-Path $Racine $nom
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            $lignes = @(([string](Get-Content -LiteralPath $source -Raw -Encoding UTF8)) -split "`r?`n")
            Save-Texte -Nom $nom -Lignes $lignes -Description ('Copie de ' + $nom)
        }
    }
    Write-Ok 'storage.json et agent_manifest.json'

    # 5. Liste des fichiers de la racine (noms, tailles, dates : aucun contenu)
    $liste = @('Type;Nom;Taille;Modifie')
    foreach ($e in @(Get-ChildItem -LiteralPath $Racine -Force -ErrorAction SilentlyContinue | Sort-Object @{ Expression = { -not $_.PSIsContainer } }, Name)) {
        $type = 'fichier'; $taille = ''
        if ($e.PSIsContainer) { $type = 'dossier' } else { $taille = [string]$e.Length }
        $liste += ($type + ';' + $e.Name + ';' + $taille + ';' + $e.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))
    }
    [System.IO.File]::WriteAllLines((Join-Path $travail 'fichiers_racine.csv'), [string[]]$liste, $utf8)
    [void]$contenu.Add(('{0,-44} {1}' -f 'fichiers_racine.csv', 'Noms, tailles et dates de la racine (aucun contenu)'))
    Write-Ok 'Liste des fichiers de la racine'

    # 6. Mode de lancement
    $modeTexte = @()
    try { $modeTexte = @((Get-ModeLancement -Racine $Racine | ConvertTo-Json -Depth 6) -split "`r?`n") } catch { $modeTexte = @('illisible : ' + $_.Exception.Message) }
    Save-Texte -Nom 'mode_lancement.json' -Lignes $modeTexte -Description 'Service, tâches planifiées (Get-ModeLancement)'
    Write-Ok 'Mode de lancement'

    # 7. nvidia-smi
    $smi = $null
    $commande = Get-Command 'nvidia-smi.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($commande) { $smi = $commande.Source } elseif (Test-Path -LiteralPath (Join-Path $env:SystemRoot 'System32\nvidia-smi.exe')) { $smi = Join-Path $env:SystemRoot 'System32\nvidia-smi.exe' }
    if ($smi) {
        $ErrorActionPreference = 'Continue'
        $gpu = @()
        try { $gpu = @(& $smi 2>&1 | ForEach-Object { [string]$_ }) } catch { $gpu = @('nvidia-smi a échoué : ' + $_.Exception.Message) }
        $ErrorActionPreference = 'Stop'
        Save-Texte -Nom 'nvidia-smi.txt' -Lignes $gpu -Description 'Sortie de nvidia-smi'
        Write-Ok 'Carte graphique (nvidia-smi)'
    } else {
        Save-Texte -Nom 'nvidia-smi.txt' -Lignes @('nvidia-smi introuvable sur ce PC.') -Description 'nvidia-smi absent'
        Write-Info 'nvidia-smi introuvable : noté dans le rapport'
    }

    # 8. Sommaire
    $systeme = ''
    try { $os = Get-CimInstance Win32_OperatingSystem; $systeme = [string]$os.Caption + ' ' + [string]$os.Version } catch { }
    $sommaire = @(
        'Rapport de diagnostic du Worker Alpine Makers',
        ('Créé le        : ' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')),
        ('PC             : ' + $env:COMPUTERNAME),
        ('Système        : ' + $systeme),
        ('PowerShell     : ' + [string]$PSVersionTable.PSVersion),
        ('Dossier Worker : ' + $Racine),
        ('Version agent  : ' + (Get-VersionInstallee -Racine $Racine)),
        ('Administrateur : ' + (Test-Administrateur)),
        '',
        'Ce rapport ne contient ni config.json, ni le dossier config\, ni fichier de token,',
        'ni le journal SQLite. Les lignes sensibles et les codes d''association sont masqués.',
        '',
        'Contenu :'
    ) + @($contenu | ForEach-Object { '  ' + $_ })
    [System.IO.File]::WriteAllLines((Join-Path $travail 'LISEZ-MOI.txt'), [string[]]$sommaire, $utf8)

    # 9. Contrôle final avant de zipper : aucun fichier interdit, aucun code d'association restant.
    $interdits = @(Get-ChildItem -LiteralPath $travail -Recurse -File | Where-Object { $_.Name -match '(?i)token|^config\.json$|\.sqlite3?$|\.key$|\.pem$' })
    if ($interdits.Count -gt 0) { throw ('Fichier interdit dans le rapport : ' + (($interdits | ForEach-Object { $_.Name }) -join ', ')) }
    foreach ($f in @(Get-ChildItem -LiteralPath $travail -Recurse -File | Where-Object { $_.Name -ne 'fichiers_racine.csv' -and $_.Name -ne 'LISEZ-MOI.txt' })) {
        $texte = [System.IO.File]::ReadAllText($f.FullName)
        if ($texte -match '\b\d{3}-\d{3}\b' -or $texte -match '(?i)bearer\s+[A-Za-z0-9]') { throw ('Masquage incomplet dans ' + $f.Name + ' : rapport abandonné par prudence.') }
    }

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path -LiteralPath $zip) { $zip = Join-Path $Dossier ('rapport-worker-' + $horodatage + '_' + (Get-Date -Format 'ss') + '.zip') }
    # Entrées ajoutées une à une avec des « / » : CreateFromDirectory du .NET Framework écrit des « \ »,
    # que les outils hors Windows lisent mal.
    Add-Type -AssemblyName System.IO.Compression
    $zipEntame = $true
    $archive = [System.IO.Compression.ZipFile]::Open($zip, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($f in @(Get-ChildItem -LiteralPath $travail -Recurse -File | Sort-Object FullName)) {
            $nomEntree = $f.FullName.Substring($travail.Length).TrimStart([char]92).Replace([string][char]92, '/')
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $f.FullName, $nomEntree, [System.IO.Compression.CompressionLevel]::Optimal)
        }
    } finally { $archive.Dispose() }
} catch {
    Write-Erreur ('Rapport non créé : ' + $_.Exception.Message)
    # Un zip entamé par CETTE exécution est inutilisable : on ne laisse pas de fichier trompeur.
    try { if ($zipEntame -and (Test-Path -LiteralPath $zip -PathType Leaf)) { Remove-Item -LiteralPath $zip -Force -Confirm:$false -ErrorAction SilentlyContinue } } catch { }
    try { Remove-Item -LiteralPath $travail -Recurse -Force -Confirm:$false -ErrorAction SilentlyContinue } catch { }
    Wait-FinOutil -SansPause:$SansPause
    exit 1
}
# Le dossier de travail est dans le dossier temporaire de Windows, jamais dans le Worker.
try { Remove-Item -LiteralPath $travail -Recurse -Force -Confirm:$false -ErrorAction SilentlyContinue } catch { }

$taille = Format-Taille ([long](Get-Item -LiteralPath $zip).Length)
Write-Cadre -Titre 'Rapport créé' -Lignes @((Split-Path -Leaf $zip), ('Taille : ' + $taille + '   (' + $contenu.Count + ' éléments)')) -Couleur Green
Write-Info ('Dossier : ' + (Split-Path -Parent $zip))
Write-Conseil 'Tu peux l''envoyer tel quel : il ne contient aucun secret.'
Write-Conseil 'Tu peux l''ouvrir pour vérifier : LISEZ-MOI.txt en décrit le contenu.'
if ($echecs -gt 0) { Write-Alerte ([string]$echecs + ' section(s) incomplète(s) : c''est noté dans le rapport.') }
# Outil en lecture seule : volontairement aucune trace écrite dans le dossier du Worker.
Wait-FinOutil -SansPause:$SansPause
exit 0
