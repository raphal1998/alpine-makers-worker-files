<#
    Socle commun des outils du Worker Alpine Makers.

    Chaque outil du dossier outils\ commence par :
        . (Join-Path $PSScriptRoot 'commun.ps1')
        $Racine = Get-RacineWorker -Racine $Racine

    Ce fichier concentre les pièges déjà payés sur les PC d'atelier :
      - le profil Windows peut contenir un espace ET un accent : tout chemin est
        manipulé avec -LiteralPath et passé entre guillemets aux processus ;
      - sous PowerShell 5.1 un script accentué doit être en UTF-8 AVEC BOM ;
      - Start-Process -ArgumentList ne protège pas les espaces : les arguments
        sont cités explicitement (ConvertTo-ArgumentCite) ;
      - le Worker peut tourner en SERVICE (session 0) : sans élévation, la ligne
        de commande de ses processus est illisible et on ne peut ni les
        identifier ni les arrêter ; les outils le disent au lieu de deviner ;
      - jamais d'arrêt par nom d'exécutable : python.exe, powershell.exe et
        node.exe servent aussi au dashboard, à la boutique et au courrier.

    Rien ici ne lit ni n'affiche de secret : config.json n'est lu que pour des
    clés sans valeur sensible, jamais token ni identité.
#>

$script:DossierOutils = $PSScriptRoot
$script:ModeAscii = ($env:ALPINE_OUTILS_ASCII -eq '1')

# Ports des moteurs, tels que l'agent les impose (agent.py, _runtime_port).
$script:MoteursWorker = @(
    [pscustomobject]@{ Outil = 'image_generation'; Nom = 'ComfyUI (images)';        Port = 8188;  Http = $false }
    [pscustomobject]@{ Outil = 'ai3d';             Nom = 'Hunyuan3D (3D)';          Port = 8189;  Http = $false }
    [pscustomobject]@{ Outil = 'printguard';       Nom = 'PrintGuard';              Port = 8000;  Http = $false }
    [pscustomobject]@{ Outil = 'model-studio';     Nom = 'Bridge Orca';             Port = 15064; Http = $true  }
)

# ---------------------------------------------------------------------------
# Console et affichage
# ---------------------------------------------------------------------------

function Initialize-ConsoleWorker {
    param([string]$Titre = 'Outils du Worker Alpine Makers')
    try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
    try { $Host.UI.RawUI.WindowTitle = $Titre } catch { }
}

function Get-Trait {
    param([string]$Type)
    if ($script:ModeAscii) {
        switch ($Type) { 'h' { return '-' } 'v' { return '|' } 'hg' { return '+' } 'hd' { return '+' } 'bg' { return '+' } 'bd' { return '+' } 'puce' { return '*' } 'ok' { return '[OK]' } 'non' { return '[!!]' } 'att' { return '[??]' } 'info' { return '[..]' } }
    }
    switch ($Type) { 'h' { return [string][char]0x2550 } 'v' { return [string][char]0x2551 } 'hg' { return [string][char]0x2554 } 'hd' { return [string][char]0x2557 } 'bg' { return [string][char]0x255A } 'bd' { return [string][char]0x255D } 'puce' { return [string][char]0x2022 } 'ok' { return [string][char]0x221A } 'non' { return [string][char]0x00D7 } 'att' { return '!' } 'info' { return [string][char]0x203A } }
    return ''
}

function Write-Cadre {
    <# Bandeau encadré. -Lignes : sous-titres affichés sous le titre. #>
    param([string]$Titre, [string[]]$Lignes = @(), [int]$Largeur = 74, [ConsoleColor]$Couleur = 'Cyan')
    $h = Get-Trait 'h'; $v = Get-Trait 'v'
    Write-Host ''
    Write-Host ((Get-Trait 'hg') + ($h * $Largeur) + (Get-Trait 'hd')) -ForegroundColor $Couleur
    foreach ($texte in @($Titre) + $Lignes) {
        $t = [string]$texte
        if ($t.Length -gt ($Largeur - 2)) { $t = $t.Substring(0, $Largeur - 2) }
        Write-Host ($v + ' ' + $t.PadRight($Largeur - 1) + $v) -ForegroundColor $Couleur
    }
    Write-Host ((Get-Trait 'bg') + ($h * $Largeur) + (Get-Trait 'bd')) -ForegroundColor $Couleur
}

function Write-Section {
    param([string]$Titre)
    Write-Host ''
    Write-Host ('  ' + $Titre.ToUpper()) -ForegroundColor White
    Write-Host ('  ' + ('-' * [Math]::Min(70, [Math]::Max(8, $Titre.Length)))) -ForegroundColor DarkGray
}

# Largeur utile des outils : une console de 100 colonnes ne coupe alors jamais au milieu d'un mot.
$script:LargeurTexte = 96

function Split-TexteLargeur {
    <#
        Coupe un texte ENTRE DEUX MOTS pour tenir dans -Largeur colonnes. Un texte qui tient
        déjà est rendu tel quel (les espacements d'un tableau sont conservés). Un mot plus
        long que la largeur (chemin) est laissé entier sur sa ligne : on ne coupe pas un chemin.
    #>
    param([string]$Texte, [int]$Largeur = 80)
    if ($Largeur -lt 20) { $Largeur = 20 }
    $lignes = @()
    foreach ($bloc in @(([string]$Texte) -split "`r?`n")) {
        $reste = [string]$bloc
        while ($reste.Length -gt $Largeur) {
            $coupe = $reste.LastIndexOf(' ', $Largeur)
            if ($coupe -le 0) { $coupe = $reste.IndexOf(' ', $Largeur) }
            if ($coupe -le 0) { break }
            $lignes += $reste.Substring(0, $coupe).TrimEnd()
            $reste = $reste.Substring($coupe).TrimStart()
        }
        $lignes += $reste
    }
    return , $lignes
}

function Write-LignesCoupees {
    <# Première ligne après -Prefixe, suites alignées sous le texte (retrait propre du texte compris). #>
    param([string]$Prefixe, [string]$Texte, [ConsoleColor]$Couleur = 'Gray')
    $texteNu = ([string]$Texte).TrimStart(' ')
    $retrait = $Prefixe.Length + (([string]$Texte).Length - $texteNu.Length)
    $debut = $Prefixe + (' ' * ($retrait - $Prefixe.Length))
    # Une étiquette en tête (« [simulation] 3. ») reste sur la première ligne : les suites s'alignent sous le texte.
    $etiquette = [regex]::Match($texteNu, '^\[[^\]]+\]\s+(\d+\.\s+)?')
    if ($etiquette.Success -and $etiquette.Length -lt 24) { $debut += $etiquette.Value; $retrait += $etiquette.Length; $texteNu = $texteNu.Substring($etiquette.Length) }
    $lignes = Split-TexteLargeur -Texte $texteNu -Largeur ($script:LargeurTexte - $retrait)
    $premiere = $true
    foreach ($l in $lignes) {
        if ($premiere) { Write-Host ($debut + $l) -ForegroundColor $Couleur; $premiere = $false }
        else { Write-Host ((' ' * $retrait) + $l) -ForegroundColor $Couleur }
    }
}

function Write-Etat {
    <# Ligne « libellé ........ valeur » colorée. Niveau : ok | alerte | erreur | info. Une valeur longue continue dessous, alignée. #>
    param([string]$Libelle, [string]$Valeur, [ValidateSet('ok', 'alerte', 'erreur', 'info')][string]$Niveau = 'info')
    $couleur = 'Gray'; $marque = Get-Trait 'info'
    switch ($Niveau) {
        'ok'     { $couleur = 'Green';  $marque = Get-Trait 'ok' }
        'alerte' { $couleur = 'Yellow'; $marque = Get-Trait 'att' }
        'erreur' { $couleur = 'Red';    $marque = Get-Trait 'non' }
    }
    $debut = '   ' + $marque + ' '
    $libelleAligne = $Libelle.PadRight(30) + ' '
    $retrait = $debut.Length + $libelleAligne.Length
    $lignes = Split-TexteLargeur -Texte $Valeur -Largeur ($script:LargeurTexte - $retrait)
    Write-Host $debut -ForegroundColor $couleur -NoNewline
    Write-Host $libelleAligne -ForegroundColor Gray -NoNewline
    $premiere = $true
    foreach ($l in $lignes) {
        if ($premiere) { Write-Host $l -ForegroundColor $couleur; $premiere = $false }
        else { Write-Host ((' ' * $retrait) + $l) -ForegroundColor $couleur }
    }
}

function Write-Ok     { param([string]$Texte) Write-LignesCoupees -Prefixe ('   ' + (Get-Trait 'ok') + ' ') -Texte $Texte -Couleur Green }
function Write-Alerte { param([string]$Texte) Write-LignesCoupees -Prefixe ('   ' + (Get-Trait 'att') + ' ') -Texte $Texte -Couleur Yellow }
function Write-Erreur { param([string]$Texte) Write-LignesCoupees -Prefixe ('   ' + (Get-Trait 'non') + ' ') -Texte $Texte -Couleur Red }
function Write-Info   { param([string]$Texte) Write-LignesCoupees -Prefixe '   ' -Texte $Texte -Couleur Gray }
function Write-Conseil { param([string]$Texte) Write-LignesCoupees -Prefixe ('   ' + (Get-Trait 'puce') + ' ') -Texte $Texte -Couleur DarkCyan }

function Format-Taille {
    <# Taille lisible, une seule mise en forme pour tous les outils : octets, Kio, Mio, Gio. #>
    param([double]$Octets)
    if ($Octets -ge 1GB) { return ('{0:N2} Gio' -f ($Octets / 1GB)) }
    if ($Octets -ge 1MB) { return ('{0:N1} Mio' -f ($Octets / 1MB)) }
    if ($Octets -ge 1KB) { return ('{0:N0} Kio' -f ($Octets / 1KB)) }
    return ('{0:N0} octets' -f $Octets)
}

# Libellés EXACTS du menu (menu_worker.ps1, tableau $script:Entrees). Tout renvoi d'un outil vers
# une entrée passe par Get-NomEntree : l'utilisateur ne lit que des noms qu'il peut trouver.
# menu_worker.ps1 vérifie à chaque lancement que cette table et son catalogue disent la même chose.
$script:NomsEntrees = @{
    1 = 'État complet du Worker';  2 = 'Jobs bloquants';               3 = 'Journaux';                  4 = 'Rapport de diagnostic'
    5 = 'Reconnecter le Worker';   6 = 'Actualiser les adresses IP';   7 = 'Récupérer une inscription'; 8 = 'Réassocier le Worker'
    9 = 'Moteurs (état, arrêt, départ)'; 10 = 'Redémarrer l''agent';   11 = 'Éteindre le Worker (lui seul)'; 12 = 'Rallumer le Worker'
    13 = 'Sauvegarder le Worker';  14 = 'Nettoyage léger';             15 = 'Démarrage automatique';    16 = 'Libérer la mémoire du PC'
    17 = 'Supprimer les moteurs IA'
}
# Les entrées propres au PC du propriétaire (18 et suivantes) sont ajoutées à cette table par
# menu_proprietaire.ps1, livré seulement aux Workers du compte propriétaire.

function Get-NomEntree {
    <# « Rallumer le Worker » (12) : le libellé du menu suivi de son numéro. -Nu : sans guillemets (pour une liste). #>
    param([int]$Numero, [switch]$Nu)
    $nom = [string]$script:NomsEntrees[$Numero]
    if (-not $nom) { return ('entrée ' + $Numero + ' du menu') }
    if ($Nu) { return ($nom + ' (' + $Numero + ')') }
    return ('« ' + $nom + ' » (' + $Numero + ')')
}

function Write-Explication {
    <# Écran « avant de lancer » : ce que l'outil fait, ne fait pas, et ce qu'il exige. #>
    param([string]$Titre, [string[]]$Fait = @(), [string[]]$NeFaitPas = @(), [string[]]$Exige = @())
    Write-Cadre -Titre $Titre
    if ($Fait.Count)      { Write-Section 'Ce que fait cet outil';        foreach ($l in $Fait)      { Write-Conseil $l } }
    if ($NeFaitPas.Count) { Write-Section 'Ce qu''il ne fait pas';        foreach ($l in $NeFaitPas) { Write-Conseil $l } }
    if ($Exige.Count)     { Write-Section 'Ce qu''il lui faut';           foreach ($l in $Exige)     { Write-Conseil $l } }
    Write-Host ''
}

function Confirm-Action {
    <#
        Demande une confirmation. -MotExact impose de taper un mot précis (actions
        lourdes). -Oui (ou $script:OuiAutomatique) répond oui sans demander : sert
        aux essais et à l'enchaînement depuis le menu.
    #>
    param([string]$Question, [string]$MotExact = '', [switch]$Oui)
    if ($Oui -or $script:OuiAutomatique) { return $true }
    if ($MotExact) {
        $r = Read-Host ('   ' + $Question + ' Tape ' + $MotExact + ' pour confirmer')
        return ($r -ceq $MotExact)
    }
    $r = Read-Host ('   ' + $Question + ' [O/N]')
    return ($r -match '^(?i:o|oui|y|yes)$')
}

function Wait-FinOutil {
    param([switch]$SansPause)
    if ($SansPause) { return }
    Write-Host ''
    [void](Read-Host '   Appuie sur Entree pour continuer')
}

# ---------------------------------------------------------------------------
# Dossier du Worker, Python, configuration sans secret
# ---------------------------------------------------------------------------

function Initialize-ApiChemins {
    if ('AlpineOutils.Chemins' -as [type]) { return }
    Add-Type -Namespace AlpineOutils -Name Chemins -MemberDefinition @"
[DllImport("kernel32.dll", CharSet = CharSet.Unicode)] public static extern uint GetLongPathName(string source, System.Text.StringBuilder cible, uint taille);
[DllImport("kernel32.dll", CharSet = CharSet.Unicode)] public static extern uint GetShortPathName(string source, System.Text.StringBuilder cible, uint taille);
"@
}

function ConvertTo-CheminLong {
    <# « C:\Users\PRENOM~1\... » -> « C:\Users\Prénom Nom\... ». Sans effet si le chemin est déjà long. #>
    param([string]$Chemin)
    try {
        Initialize-ApiChemins
        $sb = New-Object System.Text.StringBuilder 2048
        $n = [AlpineOutils.Chemins]::GetLongPathName($Chemin, $sb, 2048)
        if ($n -gt 0 -and $n -lt 2048) { return $sb.ToString() }
    } catch { }
    return $Chemin
}

function ConvertTo-CheminCourt {
    <# Forme 8.3 : l'agent la passe à certains moteurs à cause de l'accent du profil. Peut être identique au chemin long. #>
    param([string]$Chemin)
    try {
        Initialize-ApiChemins
        $sb = New-Object System.Text.StringBuilder 2048
        $n = [AlpineOutils.Chemins]::GetShortPathName($Chemin, $sb, 2048)
        if ($n -gt 0 -and $n -lt 2048) { return $sb.ToString() }
    } catch { }
    return $Chemin
}

function Get-RacineWorker {
    <# Racine du Worker, en forme LONGUE : -Racine explicite, sinon le parent du dossier outils\. #>
    param([string]$Racine = '')
    $cible = $Racine
    if (-not $cible) { $cible = Split-Path -Parent $script:DossierOutils }
    $testRoot = Split-Path -Parent $script:DossierOutils
    $sandboxOnly = ($env:ALPINE_LOCAL_SANDBOX -eq '1') -or (Test-Path -LiteralPath (Join-Path $testRoot 'TEST_SANDBOX_ONLY.txt'))
    if (Test-Path -LiteralPath (Join-Path $testRoot 'config.json')) {
        $localConfig = Get-Content -LiteralPath (Join-Path $testRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
        $sandboxOnly = $sandboxOnly -or ($localConfig.local_sandbox -eq $true)
        $localConfig = $null
    }
    if ($sandboxOnly) {
        if ([IO.Path]::GetFullPath($cible).TrimEnd('\') -ne [IO.Path]::GetFullPath($testRoot).TrimEnd('\')) {
            throw "Outil TEST : une autre installation Worker ne peut pas etre ciblee."
        }
        $env:ALPINE_LOCAL_SANDBOX = '1'
        $env:ALPINE_SANDBOX_WORKER_ROOT = [IO.Path]::GetFullPath($testRoot)
        $env:ALPINE_TEST_DASHBOARD_PORT = '5157'
    }
    try { $cible = (Resolve-Path -LiteralPath $cible -ErrorAction Stop).ProviderPath } catch { throw "Dossier du Worker introuvable : $cible" }
    if (-not (Test-Path -LiteralPath (Join-Path $cible 'agent.py') -PathType Leaf)) {
        throw "Ce dossier n'est pas une installation du Worker (agent.py absent) : $cible"
    }
    return (ConvertTo-CheminLong $cible).TrimEnd('\')
}

function Get-PythonWorker {
    <# Interpréteur de l'agent : python_path.txt (écrit en UTF-8 avec BOM), sinon python.exe hors alias WindowsApps. #>
    param([string]$Racine)
    $fichier = Join-Path $Racine 'python_path.txt'
    if (Test-Path -LiteralPath $fichier -PathType Leaf) {
        $chemin = ([string](Get-Content -LiteralPath $fichier -Raw -Encoding UTF8)).Trim().Trim([char]0xFEFF)
        if ($chemin -and (Test-Path -LiteralPath $chemin -PathType Leaf)) { return $chemin }
    }
    $trouve = Get-Command python.exe -All -ErrorAction SilentlyContinue | Where-Object { $_.Source -notmatch '\\WindowsApps\\' } | Select-Object -First 1
    if ($trouve) { return $trouve.Source }
    return $null
}

function Get-ConfigSure {
    <#
        Lit config.json SANS jamais renvoyer token ni identité. Renvoie :
        Present, Associe, WorkerIdCourt (8 caractères), Nom, ServeurUrl, PortOrca.
    #>
    param([string]$Racine)
    $resultat = [pscustomobject]@{ Present = $false; Associe = $false; WorkerIdCourt = ''; Nom = ''; ServeurUrl = ''; PortOrca = 15064 }
    $fichier = Join-Path $Racine 'config.json'
    if (-not (Test-Path -LiteralPath $fichier -PathType Leaf)) { return $resultat }
    try {
        $c = Get-Content -LiteralPath $fichier -Raw -Encoding UTF8 | ConvertFrom-Json
        $resultat.Present = $true
        $resultat.Associe = [bool]($c.worker_id -and $c.token)
        if ($c.worker_id) { $resultat.WorkerIdCourt = ([string]$c.worker_id).Substring(0, [Math]::Min(8, ([string]$c.worker_id).Length)) }
        $resultat.Nom = [string]$c.name
        $resultat.ServeurUrl = [string]$c.server_url
        if ($c.orca_port) { $resultat.PortOrca = [int]$c.orca_port }
    } catch { }
    return $resultat
}

function Get-VersionInstallee {
    <# Version de l'agent installé, lue dans agent_manifest.json (écrit par la mise à jour). #>
    param([string]$Racine)
    $fichier = Join-Path $Racine 'agent_manifest.json'
    if (Test-Path -LiteralPath $fichier -PathType Leaf) {
        try { return [string]((Get-Content -LiteralPath $fichier -Raw -Encoding UTF8 | ConvertFrom-Json).agent_version) } catch { }
    }
    $agent = Join-Path $Racine 'agent.py'
    $ligne = Select-String -LiteralPath $agent -Pattern '^AGENT_VERSION\s*=\s*"([^"]+)"' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($ligne) { return $ligne.Matches[0].Groups[1].Value }
    return ''
}

function Write-JournalOutil {
    <# Trace d'exploitation des outils : <Worker>\logs\outils-worker.log. Jamais de secret ni de code d'association. #>
    param([string]$Racine, [string]$Outil, [string]$Message)
    try {
        $dossier = Join-Path $Racine 'logs'
        if (-not (Test-Path -LiteralPath $dossier)) { New-Item -ItemType Directory -Path $dossier -Force | Out-Null }
        $ligne = '{0}  {1,-22} {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Outil, $Message
        Add-Content -LiteralPath (Join-Path $dossier 'outils-worker.log') -Value $ligne -Encoding UTF8
    } catch { }
}

# ---------------------------------------------------------------------------
# Élévation
# ---------------------------------------------------------------------------

function Test-Administrateur {
    $identite = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($identite)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function ConvertTo-ArgumentCite {
    <# Start-Process ne cite pas ses arguments : un chemin avec espace arriverait coupé en deux. #>
    param([string]$Valeur)
    if ($Valeur -match '[\s"]') { return '"' + ($Valeur -replace '"', '\"') + '"' }
    return $Valeur
}

function Invoke-OutilEleve {
    <#
        Relance un outil dans une NOUVELLE fenêtre administrateur et attend sa fin.
        Renvoie le code de sortie, ou $null si l'élévation a été refusée.
        L'outil relancé reçoit -DejaEleve pour ne pas boucler.
    #>
    param([string]$Script, [string[]]$Arguments = @())
    if ($env:ALPINE_WORKER_GUI -eq '1') { return (Invoke-OutilEleveSansFenetre -Script $Script -Arguments $Arguments) }
    $liste = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (ConvertTo-ArgumentCite $Script), '-DejaEleve')
    foreach ($a in $Arguments) { $liste += (ConvertTo-ArgumentCite $a) }
    try {
        $p = Start-Process -FilePath 'powershell.exe' -ArgumentList $liste -Verb RunAs -Wait -PassThru -ErrorAction Stop
        if ($null -ne $p.ExitCode) { return [int]$p.ExitCode }
        return 0
    } catch {
        return $null
    }
}

function Invoke-OutilEleveSansFenetre {
    <#
        Variante pour worker.exe (application graphique, ALPINE_WORKER_GUI=1) : l'instance administrateur tourne
        SANS fenêtre et sans clavier (-NonInteractive : une question y lèverait une erreur au lieu de bloquer,
        invisible). Tout ce qu'elle écrit va dans un fichier du dossier logs, recopié ici à la fin pour que
        l'application l'affiche. Windows demande toujours l'accord UAC. Renvoie le code, ou $null si refusé.
    #>
    param([string]$Script, [string[]]$Arguments = @())
    $dossier = Join-Path (Split-Path -Parent (Split-Path -Parent $Script)) 'logs'
    try { if (-not (Test-Path -LiteralPath $dossier)) { New-Item -ItemType Directory -Path $dossier -Force | Out-Null } } catch { $dossier = $env:TEMP }
    $sortie = Join-Path $dossier ('outil-admin-' + [Guid]::NewGuid().ToString('N') + '.log')
    $cite = { param([string]$v) "'" + ($v -replace "'", "''") + "'" }
    $commande = '& ' + (& $cite $Script) + ' -DejaEleve'
    foreach ($a in $Arguments) { if ($a -match '^-[A-Za-z]+$') { $commande += ' ' + $a } else { $commande += ' ' + (& $cite $a) } }
    $commande += ' *>&1 | Out-File -LiteralPath ' + (& $cite $sortie) + ' -Encoding UTF8; exit $LASTEXITCODE'
    $encodee = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($commande))
    $code = $null
    try {
        $p = Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden', '-EncodedCommand', $encodee) -Verb RunAs -WindowStyle Hidden -Wait -PassThru -ErrorAction Stop
        $code = 0
        if ($null -ne $p.ExitCode) { $code = [int]$p.ExitCode }
    } catch { $code = $null }
    try {
        if (Test-Path -LiteralPath $sortie -PathType Leaf) {
            foreach ($ligne in @(Get-Content -LiteralPath $sortie -Encoding UTF8)) { Write-Host $ligne }
            Remove-Item -LiteralPath $sortie -Force -ErrorAction SilentlyContinue
        }
    } catch { }
    return $code
}

# ---------------------------------------------------------------------------
# Comment le Worker est lancé sur ce PC
# ---------------------------------------------------------------------------

function Get-ModeLancement {
    <#
        Service Windows (nssm « AlpineWorker »), tâche planifiée, ou rien.
        Renvoie : Mode (service|tache|aucun), Service (Nom, Etat, Demarrage, Compte,
        ViseCeDossier) ou $null, Taches (Nom, Etat, Dossier, DossierExiste,
        ViseCeDossier, Orpheline).
    #>
    param([string]$Racine)
    $service = $null
    $svc = Get-CimInstance Win32_Service -Filter "Name='AlpineWorker'" -ErrorAction SilentlyContinue
    if ($svc) {
        $dossier = ''
        try { $dossier = [string](Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Services\AlpineWorker\Parameters' -ErrorAction Stop).AppDirectory } catch { }
        $vise = $false
        # AppDirectory peut avoir été écrit en forme 8.3 (PRENOM~1) : on compare la forme LONGUE, comme pour les tâches.
        if ($dossier) { $vise = ((ConvertTo-CheminLong $dossier.TrimEnd('\')).TrimEnd('\') -ieq $Racine) }
        $service = [pscustomobject]@{ Nom = 'AlpineWorker'; Etat = [string]$svc.State; Demarrage = [string]$svc.StartMode; Compte = [string]$svc.StartName; Dossier = $dossier; ViseCeDossier = $vise }
    }
    $taches = @()
    foreach ($t in @(Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object { $_.TaskName -like 'Alpine Makers Worker*' })) {
        $arguments = [string](($t.Actions | Select-Object -First 1).Arguments)
        $dossierTache = ''
        if ($arguments -match '-File\s+"([^"]+)\\run_worker\.ps1"') { $dossierTache = $Matches[1] }
        elseif ($arguments -match '-File\s+(\S+)\\run_worker\.ps1') { $dossierTache = $Matches[1] }
        $existe = [bool]($dossierTache -and (Test-Path -LiteralPath $dossierTache))
        $longue = $dossierTache
        if ($existe) { $longue = ConvertTo-CheminLong $dossierTache }
        # Orpheline = dossier LU et inexistant. Une action illisible n'est pas une orpheline :
        # on ne propose jamais de retirer une tâche qu'on n'a pas su comprendre.
        $taches += [pscustomobject]@{
            Nom = $t.TaskName; Chemin = [string]$t.TaskPath; Etat = [string]$t.State; Dossier = $dossierTache; DossierExiste = $existe
            ViseCeDossier = ($longue.TrimEnd('\') -ieq $Racine); Illisible = (-not $dossierTache)
            Orpheline = ([bool]$dossierTache -and -not $existe)
        }
    }
    $mode = 'aucun'
    if ($service -and $service.ViseCeDossier) { $mode = 'service' }
    elseif (@($taches | Where-Object { $_.ViseCeDossier }).Count -gt 0) { $mode = 'tache' }
    return [pscustomobject]@{ Mode = $mode; Service = $service; Taches = $taches }
}

# ---------------------------------------------------------------------------
# Moteurs : ports et fiches
# ---------------------------------------------------------------------------

function Test-PortHttpSys {
    <# Le bridge Orca écoute par HttpListener = HTTP.sys : le port apparaît sous le PID 4 (System). On lit la file HTTP.sys. #>
    param([int]$Port)
    try { return [bool]((& netsh http show servicestate 2>$null | Select-String -SimpleMatch (':' + $Port + ':')) -ne $null) } catch { return $false }
}

function Get-EtatMoteurs {
    <#
        État local des moteurs, SANS élévation : port en écoute et fiche runtime\<outil>.json.
        Renvoie par moteur : Outil, Nom, Port, EnEcoute, PidPort, FichePresente.
        Une fiche présente avec un port fermé = moteur tombé ou en cours de démarrage.
    #>
    param([string]$Racine)
    $config = Get-ConfigSure -Racine $Racine
    $liste = @()
    foreach ($m in $script:MoteursWorker) {
        $port = $m.Port
        if ($m.Outil -eq 'model-studio') { $port = $config.PortOrca }
        $ecoute = $false; $pidPort = 0
        if ($m.Http) { $ecoute = Test-PortHttpSys -Port $port }
        else {
            $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($c) { $ecoute = $true; $pidPort = [int]$c.OwningProcess }
        }
        $fiche = Test-Path -LiteralPath (Join-Path $Racine ('runtime\' + $m.Outil + '.json')) -PathType Leaf
        $liste += [pscustomobject]@{ Outil = $m.Outil; Nom = $m.Nom; Port = $port; EnEcoute = $ecoute; PidPort = $pidPort; FichePresente = $fiche }
    }
    return $liste
}

function Test-AgentVivant {
    <# Indice sans élévation : le relais caméra 127.0.0.1:1984 est servi PAR l'agent. Absent = agent arrêté ou sans relais configuré. #>
    $c = Get-NetTCPConnection -LocalPort 1984 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    return [bool]$c
}

# ---------------------------------------------------------------------------
# Parler à l'agent vivant : boîte aux lettres locale
# ---------------------------------------------------------------------------

function Invoke-ActionLocale {
    <#
        Dépose une demande dans runtime\local-control\ et attend la réponse de l'agent.
        Actions : status | engines-stop | engines-start | reconnect | network.
        Renvoie : Ok, Resultat (objet), Erreur (texte), Code, NonSupporte, DelaiDepasse,
        EnCours, EnAttente.
        NonSupporte = $true quand l'agent installé est trop ancien pour cette action :
        l'appelant doit alors se rabattre sur les lectures locales, pas échouer.
        DelaiDepasse = pas de réponse dans le délai. EnCours = l'agent avait DÉJÀ pris la
        demande : il l'exécute encore, ce n'est pas un refus (suivre les ports). EnAttente =
        la demande est restée dans la boîte (actions reconnect / network seulement).
        -Progression : écrit un point toutes les 5 s pendant l'attente (arrêt des moteurs :
        jusqu'à 90 s sans autre signe de vie).

        Python est lancé par System.Diagnostics.Process, sorties lues à part : ni
        $ErrorActionPreference ni « 2>&1 » n'entrent en jeu, le refus d'argparse d'un ancien
        agent (« usage: … invalid choice ») est donc toujours reconnu.
    #>
    param([string]$Racine, [string]$Action, [int]$DelaiSecondes = 15, [switch]$Progression)
    $retour = [pscustomobject]@{ Ok = $false; Resultat = $null; Erreur = ''; Code = -1; NonSupporte = $false; DelaiDepasse = $false; EnCours = $false; EnAttente = $false }
    if ($Action -notmatch '^[a-z-]+$') { $retour.Erreur = 'Action locale invalide.'; return $retour }
    $python = Get-PythonWorker -Racine $Racine
    if (-not $python) { $retour.Erreur = 'Interpréteur Python du Worker introuvable (python_path.txt).'; return $retour }
    $script = Join-Path $Racine 'local_control.py'
    if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { $retour.Erreur = 'local_control.py est absent de ce Worker.'; return $retour }
    $texteSortie = ''; $texteErreur = ''
    $p = $null
    try {
        $info = New-Object System.Diagnostics.ProcessStartInfo
        $info.FileName = $python
        # $Racine ne finit jamais par « \ » (Get-RacineWorker) : le guillemet fermant n'est pas échappé.
        $info.Arguments = '"' + $script + '" --root "' + $Racine.TrimEnd('\') + '" --action ' + $Action + ' --json --timeout ' + [int]$DelaiSecondes
        $info.WorkingDirectory = $Racine
        $info.UseShellExecute = $false
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $info.CreateNoWindow = $true
        $p = [System.Diagnostics.Process]::Start($info)
        $lectureSortie = $p.StandardOutput.ReadToEndAsync()
        $lectureErreur = $p.StandardError.ReadToEndAsync()
        # local_control.py tient lui-même son délai ; la marge couvre le démarrage de Python.
        $limite = (Get-Date).AddSeconds($DelaiSecondes + 30)
        $points = $false
        while (-not $p.WaitForExit(5000)) {
            if ($Progression) { Write-Host '.' -NoNewline -ForegroundColor DarkGray; $points = $true }
            if ((Get-Date) -ge $limite) { try { $p.Kill() } catch { }; $retour.DelaiDepasse = $true; break }
        }
        if ($points) { Write-Host '' }
        if (-not $retour.DelaiDepasse) { $p.WaitForExit(); $retour.Code = [int]$p.ExitCode }
        if ($lectureSortie.Wait(5000)) { $texteSortie = [string]$lectureSortie.Result }
        if ($lectureErreur.Wait(5000)) { $texteErreur = [string]$lectureErreur.Result }
    } catch {
        $retour.Erreur = 'Impossible de lancer Python pour parler à l''agent : ' + [string]$_.Exception.Message
        return $retour
    } finally {
        if ($p) { try { $p.Dispose() } catch { } }
    }
    $texte = ($texteSortie + "`n" + $texteErreur).Trim()
    if ($texte -match 'invalid choice|unrecognized arguments|(?m)^\s*usage:') { $retour.NonSupporte = $true; $retour.Erreur = 'Agent trop ancien pour cette action : mets le Worker à jour.'; return $retour }
    if ($retour.DelaiDepasse) { $retour.Erreur = 'L''agent n''a pas répondu dans le délai.'; return $retour }
    $json = (@($texteSortie -split "`r?`n") | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1)
    if (-not $json) { $retour.Erreur = $(if ($texte) { $texte } else { 'Aucune réponse de l''agent.' }); return $retour }
    try { $reponse = $json | ConvertFrom-Json } catch { $retour.Erreur = 'Réponse illisible de l''agent.'; return $retour }
    $retour.Ok = [bool]$reponse.ok
    $champs = @($reponse.PSObject.Properties.Name)
    if ($champs -contains 'result') { $retour.Resultat = $reponse.result }
    if (-not $retour.Ok) {
        if ($champs -contains 'error') { $retour.Erreur = [string]$reponse.error }
        if ($retour.Erreur -eq 'timeout') {
            # local_control.py : {"ok":false,"error":"timeout","pending":bool[,"in_progress":true]}, code 2.
            $retour.DelaiDepasse = $true
            if ($champs -contains 'in_progress') { $retour.EnCours = [bool]$reponse.in_progress }
            if ($champs -contains 'pending') { $retour.EnAttente = [bool]$reponse.pending }
            if ($retour.EnCours) { $retour.Erreur = 'L''agent n''a pas répondu dans le délai : il exécute encore la demande.' }
            else { $retour.Erreur = 'L''agent n''a pas répondu dans le délai (arrêté, occupé, ou encore en démarrage).' }
        }
        # Fichiers à jour mais agent pas encore redémarré : l'ancien code répond « Action locale inconnue ».
        elseif ($retour.Erreur -match 'Action locale inconnue') {
            $retour.NonSupporte = $true
            $retour.Erreur = 'L''agent en mémoire est plus ancien que ses fichiers : redémarre-le avec ' + (Get-NomEntree 10) + ' pour activer cette action.'
        }
    }
    return $retour
}

# ---------------------------------------------------------------------------
# Processus du Worker (exige l'élévation si le Worker tourne en service)
# ---------------------------------------------------------------------------

function Get-ProcessusDuWorker {
    <#
        Processus appartenant à CE Worker, JAMAIS reconnus par leur nom seul :
          - superviseur : powershell -File « …\<Worker>\run_worker.ps1 » ;
          - agent       : python agent.py, enfant d'un superviseur ;
          - moteurs     : descendants de l'agent ; ou ORPHELINS (parent mort) reconnus par ce
                          qu'ils EXÉCUTENT : exécutable sous <Worker>\components\, python dont le
                          programme (ou le script lancé) est sous <Worker>\components\, ou
                          powershell -File <Worker>\orca_core_bridge.ps1.
        Une simple MENTION d'un chemin du Worker ne suffit jamais : un Bloc-notes, un éditeur, une
        sauvegarde ou une commande ouverts sur un fichier du Worker ne sont pas des moteurs.
        Une filiation n'est retenue que si l'enfant est né APRÈS son parent : Windows réutilise
        les PID, et le ParentProcessId d'un processus dont le parent est mort peut désigner un
        tout autre programme.
        Exclut le processus courant et ses ancêtres (le menu, la console, Claude).
        Illisibles = nombre de python/powershell dont la ligne de commande est vide :
        non nul sans élévation quand le Worker est un service.
    #>
    param([string]$Racine)
    # Chemin COMPLET, en forme longue ou courte : deux installations portant le même nom de
    # dossier à deux endroits ne doivent jamais être confondues.
    $formesBrutes = @($Racine)
    $courte = ConvertTo-CheminCourt $Racine
    if ($courte -and ($courte -ine $Racine)) { $formesBrutes += $courte }
    $feuille = '(?:' + (($formesBrutes | ForEach-Object { [regex]::Escape($_) }) -join '|') + ')'
    $tous = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $parPid = @{}
    foreach ($p in $tous) { $parPid[[int]$p.ProcessId] = $p }
    $exclus = New-Object 'System.Collections.Generic.HashSet[int]'
    $courant = [int]$PID
    while ($courant -and $parPid.ContainsKey($courant) -and $exclus.Add($courant)) { $courant = [int]$parPid[$courant].ParentProcessId }

    $trouves = @{}
    function Add-Trouve([object]$p, [string]$role) { if (-not $exclus.Contains([int]$p.ProcessId) -and -not $trouves.ContainsKey([int]$p.ProcessId)) { $trouves[[int]$p.ProcessId] = [pscustomobject]@{ ProcessId = [int]$p.ProcessId; ParentProcessId = [int]$p.ParentProcessId; Nom = [string]$p.Name; Role = $role; Creation = $p.CreationDate } } }
    # Vrai enfant : né après son parent. Date illisible = filiation non prouvée = refusée.
    function Test-EnfantDe([object]$enfant, [object]$parent) {
        if (-not $enfant -or -not $parent) { return $false }
        if ([int]$enfant.ParentProcessId -ne [int]$parent.ProcessId -or [int]$enfant.ProcessId -eq [int]$parent.ProcessId) { return $false }
        if (-not $enfant.CreationDate -or -not $parent.CreationDate) { return $false }
        return ($enfant.CreationDate -ge $parent.CreationDate)
    }
    # Est-ce un chemin SOUS <Worker>\components\ (forme longue ou courte) ?
    function Test-SousComposants([string]$chemin) {
        if (-not $chemin) { return $false }
        foreach ($forme in $formesBrutes) { if ($chemin.StartsWith($forme + '\components\', [System.StringComparison]::OrdinalIgnoreCase)) { return $true } }
        return $false
    }

    $superviseurs = @($tous | Where-Object { $_.CommandLine -and $_.Name -match '^(powershell|pwsh)\.exe$' -and $_.CommandLine -match ('-File\s+"?' + $feuille + '\\run_worker\.ps1') })
    foreach ($s in $superviseurs) { Add-Trouve $s 'superviseur' }
    $agents = @()
    foreach ($candidat in @($tous | Where-Object { $_.CommandLine -and $_.CommandLine -match 'agent\.py' })) {
        foreach ($s in $superviseurs) { if (Test-EnfantDe $candidat $s) { $agents += $candidat; break } }
    }
    foreach ($a in $agents) { Add-Trouve $a 'agent' }

    # Descendants de l'agent = moteurs et leurs enfants.
    $file = New-Object System.Collections.Queue
    foreach ($a in $agents) { $file.Enqueue([int]$a.ProcessId) }
    while ($file.Count -gt 0) {
        $parent = $parPid[[int]$file.Dequeue()]
        foreach ($enfant in @($tous | Where-Object { [int]$_.ParentProcessId -eq [int]$parent.ProcessId })) {
            if (-not (Test-EnfantDe $enfant $parent)) { continue }
            if (-not $trouves.ContainsKey([int]$enfant.ProcessId)) { Add-Trouve $enfant 'moteur'; $file.Enqueue([int]$enfant.ProcessId) }
        }
    }
    # Orphelins : le parent est mort, mais le processus EXÉCUTE un programme de ce Worker.
    $motifBridge = '-File\s+"?' + $feuille + '\\orca_core_bridge\.ps1'
    foreach ($p in $tous) {
        $orphelin = $false
        if (Test-SousComposants ([string]$p.ExecutablePath)) { $orphelin = $true }
        elseif ($p.CommandLine -and $p.Name -match '^(powershell|pwsh)\.exe$' -and $p.CommandLine -match $motifBridge) { $orphelin = $true }
        elseif ($p.CommandLine -and $p.Name -match '^pythonw?(\d[\d.]*)?\.exe$') {
            # Le python d'un venv relance le Python de base avec la MÊME ligne de commande : on regarde le
            # programme (1er élément) et le script lancé (2e élément), jamais une position quelconque.
            $m = [regex]::Match([string]$p.CommandLine, '^\s*(?:"([^"]*)"|(\S+))(?:\s+(?:"([^"]*)"|(\S+)))?')
            if ($m.Success) {
                $programme = $m.Groups[1].Value + $m.Groups[2].Value
                $premier = $m.Groups[3].Value + $m.Groups[4].Value
                if ((Test-SousComposants $programme) -or (Test-SousComposants $premier)) { $orphelin = $true }
            }
        }
        if ($orphelin) { Add-Trouve $p 'moteur' }
    }

    $illisibles = @($tous | Where-Object { $_.Name -match '^(python|pythonw|powershell)\.exe$' -and -not $_.CommandLine }).Count
    return [pscustomobject]@{ Processus = @($trouves.Values | Sort-Object Role, ProcessId); Illisibles = $illisibles }
}
