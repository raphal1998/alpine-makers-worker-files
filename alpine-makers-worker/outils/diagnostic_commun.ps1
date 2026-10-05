<#
    Fonctions partagées par les outils de DIAGNOSTIC (etat_worker, journaux,
    jobs_bloquants, rapport_diagnostic). Tout est en lecture seule.

    À charger APRÈS commun.ps1 :
        . (Join-Path $PSScriptRoot 'commun.ps1')
        . (Join-Path $PSScriptRoot 'diagnostic_commun.ps1')

    Rien ici ne lit config.json, config\ ni un fichier *token*.
#>

# Mots qui signalent une ligne d'erreur dans un journal (insensible à la casse).
$script:MotifErreurJournal = '(?i)error|exception|traceback|failed|refus|denied|critical'

function Test-LigneErreur {
    <#
        Vrai si la ligne signale une erreur. Écarte les faux positifs connus :
        « uvicorn.error » est le NOM du journal d'uvicorn (il y écrit ses lignes INFO),
        et « errors: 0 » / « error=None » annoncent justement l'absence d'erreur.
    #>
    param([string]$Ligne)
    $t = $Ligne -replace '(?i)uvicorn\.error', '' -replace '(?i)errors?\s*[=:]\s*(0|none|null|false|\[\])', ''
    return ($t -match $script:MotifErreurJournal)
}

function Get-JournalServicePrecedent {
    <#
        nssm range le journal du service sous un nom daté à CHAQUE redémarrage
        (AlpineWorker-20260920T165429.494.log, AlpineWorker.err-...log) et repart d'un
        fichier vide : après un plantage, la cause est donc dans le fichier daté, pas dans
        AlpineWorker.err.log. Renvoie le plus récent fichier daté NON VIDE, ou '' s'il n'y
        en a pas. Filtre strict : jamais les journaux AlpineBoutique*, AlpineDashboard*...
    #>
    param([string]$Dossier, [switch]$Erreurs)
    if (-not (Test-Path -LiteralPath $Dossier -PathType Container)) { return '' }
    $motif = '^AlpineWorker-\d{8}T[\d.]+\.log$'
    if ($Erreurs) { $motif = '^AlpineWorker\.err-\d{8}T[\d.]+\.log$' }
    $trouve = @(Get-ChildItem -LiteralPath $Dossier -Filter 'AlpineWorker*.log' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match $motif -and $_.Length -gt 0 } |
        Sort-Object -Property LastWriteTime, Name -Descending) | Select-Object -First 1
    if ($trouve) { return [string]$trouve.FullName }
    return ''
}

function Get-JournauxConnus {
    <#
        Journaux consultables, dans l'ordre du menu. Renvoie par journal :
        Cle, Nom, Chemin, Existe, Taille. Jamais config\, config.json ni *token*.
        « service-precedent » et « service-erreurs-precedent » désignent le dernier journal
        daté non vide du service (Chemin vide et Existe faux s'il n'y en a aucun).
    #>
    param([string]$Racine)
    $commun = Join-Path $env:ProgramData 'AlpineMakers'
    $logsService = Join-Path $commun 'logs'
    $liste = @(
        @('images',         'ComfyUI (images)',                 (Join-Path $Racine 'logs\image_generation.log')),
        @('3d',             'Hunyuan3D (3D)',                   (Join-Path $Racine 'logs\ai3d.log')),
        @('printguard',     'PrintGuard',                       (Join-Path $Racine 'logs\printguard.log')),
        @('orca',           'Bridge Orca (moteur)',             (Join-Path $Racine 'logs\model-studio.log')),
        @('orca-bridge',    'Bridge Orca (détail)',             (Join-Path $Racine 'workspace\orca_bridge.log')),
        @('outils',         'Actions faites avec ces outils',   (Join-Path $Racine 'logs\outils-worker.log')),
        @('service',        'Service AlpineWorker (sortie)',    (Join-Path $commun 'logs\AlpineWorker.log')),
        @('service-erreurs','Service AlpineWorker (erreurs)',   (Join-Path $commun 'logs\AlpineWorker.err.log')),
        @('service-precedent',        'Service, fois précédente (sortie)',  (Get-JournalServicePrecedent -Dossier $logsService)),
        @('service-erreurs-precedent','Service, fois précédente (erreurs)', (Get-JournalServicePrecedent -Dossier $logsService -Erreurs)),
        @('supervision',    'Superviseur de la machine',        (Join-Path $commun 'supervision.log'))
    )
    $resultat = @()
    foreach ($j in $liste) {
        $chemin = [string]$j[2]
        if ($chemin -match '(?i)token' -or $chemin -match '(?i)\\config(\\|\.json$)') { continue }
        $existe = $false
        if ($chemin) { $existe = Test-Path -LiteralPath $chemin -PathType Leaf }
        $taille = 0
        if ($existe) { try { $taille = [long](Get-Item -LiteralPath $chemin -Force).Length } catch { } }
        $resultat += [pscustomobject]@{ Cle = [string]$j[0]; Nom = [string]$j[1]; Chemin = $chemin; Existe = $existe; Taille = $taille }
    }
    return $resultat
}

function ConvertFrom-OctetsJournal {
    <#
        Décode un bloc d'octets d'un journal SANS BOM UTF-16. Les moteurs écrivent en
        UTF-8 ; les journaux du service (C:\ProgramData\AlpineMakers\logs) sont en
        Windows-1252. On essaie l'UTF-8 STRICT : un octet 1252 isolé (ea = ê) y est
        invalide et lève une exception, auquel cas le MÊME bloc est relu en 1252.
        -DebutTronque : le bloc commence au milieu du fichier, donc peut-être au milieu
        d'un caractère UTF-8 ; ses octets de continuation de tête (10xxxxxx, 3 au plus)
        sont écartés pour ne pas faire échouer à tort le décodage strict. Ils
        appartiennent à la première ligne, tronquée, que l'appelant jette de toute façon.
        Renvoie Texte et Encodage ('utf8' ou '1252').
    #>
    param([byte[]]$Octets, [int]$Compte, [switch]$DebutTronque)
    $depart = 0
    if ($DebutTronque) { while ($depart -lt 3 -and $depart -lt $Compte -and (($Octets[$depart] -band 0xC0) -eq 0x80)) { $depart++ } }
    $strict = New-Object System.Text.UTF8Encoding($false, $true)
    try {
        return [pscustomobject]@{ Texte = $strict.GetString($Octets, $depart, $Compte - $depart); Encodage = 'utf8' }
    } catch [System.Text.DecoderFallbackException] {
        return [pscustomobject]@{ Texte = [System.Text.Encoding]::GetEncoding(1252).GetString($Octets, $depart, $Compte - $depart); Encodage = '1252' }
    }
}

function Get-EncodageJournal {
    <#
        Encodage constaté sur la fin d'un journal (64 Kio), pour le mode « suivre » :
        'utf16' (BOM), 'utf8', '1252', ou 'ascii' quand aucun octet ne tranche encore.
        Même test que Get-FinFichier. Renvoie 'ascii' si le fichier est vide ou illisible.
    #>
    param([string]$Chemin)
    $flux = $null
    try {
        $partage = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
        $flux = New-Object System.IO.FileStream($Chemin, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, $partage)
        $longueur = $flux.Length
        if ($longueur -le 0) { return 'ascii' }
        $entete = New-Object byte[] 2
        [void]$flux.Read($entete, 0, 2)
        if ($entete[0] -eq 0xFF -and $entete[1] -eq 0xFE) { return 'utf16' }
        $debut = [long][Math]::Max(0, $longueur - 65536)
        $taille = [int]($longueur - $debut)
        $octets = New-Object byte[] $taille
        [void]$flux.Seek($debut, [System.IO.SeekOrigin]::Begin)
        $lus = 0
        while ($lus -lt $taille) {
            $n = $flux.Read($octets, $lus, $taille - $lus)
            if ($n -le 0) { break }
            $lus += $n
        }
        $haut = $false
        for ($i = 0; $i -lt $lus; $i++) { if ($octets[$i] -ge 0x80) { $haut = $true; break } }
        if (-not $haut) { return 'ascii' }
        return (ConvertFrom-OctetsJournal -Octets $octets -Compte $lus -DebutTronque:($debut -gt 0)).Encodage
    } catch {
        return 'ascii'
    } finally {
        if ($flux) { $flux.Dispose() }
    }
}

function Get-FinFichier {
    <#
        Dernières lignes d'un fichier texte, MÊME s'il est ouvert en écriture par un
        autre processus (partage ReadWrite + Delete). Ne lit que la fin du fichier :
        un journal de plusieurs centaines de Mio ne bloque pas l'outil.
        Reconnaît l'UTF-16 (redirections PowerShell 5.1) à son BOM ; sinon UTF-8 strict,
        et Windows-1252 quand le contenu n'est pas de l'UTF-8 (journaux du service).
        Renvoie toujours un tableau de chaînes (vide si le fichier est illisible).
    #>
    param([string]$Chemin, [int]$Lignes = 60)
    $flux = $null
    try {
        $partage = [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete
        $flux = New-Object System.IO.FileStream($Chemin, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, $partage)
        $longueur = $flux.Length
        if ($longueur -le 0) { return ,@() }
        $entete = New-Object byte[] 2
        [void]$flux.Read($entete, 0, 2)
        $utf16 = ($entete[0] -eq 0xFF -and $entete[1] -eq 0xFE)
        $bloc = [long][Math]::Max(65536, $Lignes * 512)
        $plafond = [long]67108864
        $morceaux = @()
        while ($true) {
            $debut = [long][Math]::Max(0, $longueur - $bloc)
            if ($utf16 -and ($debut % 2) -ne 0) { $debut-- }
            $taille = [int]($longueur - $debut)
            $octets = New-Object byte[] $taille
            [void]$flux.Seek($debut, [System.IO.SeekOrigin]::Begin)
            $lus = 0
            while ($lus -lt $taille) {
                $n = $flux.Read($octets, $lus, $taille - $lus)
                if ($n -le 0) { break }
                $lus += $n
            }
            if ($utf16) { $texte = [System.Text.Encoding]::Unicode.GetString($octets, 0, $lus) }
            else { $texte = (ConvertFrom-OctetsJournal -Octets $octets -Compte $lus -DebutTronque:($debut -gt 0)).Texte }
            if ($debut -eq 0) { $texte = $texte.TrimStart([char]0xFEFF) }
            # Les codes de couleur ANSI des moteurs sont illisibles hors de leur console.
            $texte = $texte -replace ([string][char]27 + '\[[0-9;]*[A-Za-z]'), ''
            $morceaux = @($texte -split "`r?`n")
            # La première ligne d'un bloc pris au milieu du fichier est tronquée : on l'écarte.
            if ($debut -gt 0 -and $morceaux.Count -gt 1) { $morceaux = @($morceaux[1..($morceaux.Count - 1)]) }
            if ($debut -eq 0 -or $morceaux.Count -gt $Lignes -or $bloc -ge $plafond) { break }
            $bloc = $bloc * 4
        }
        if ($morceaux.Count -gt 0 -and $morceaux[$morceaux.Count - 1] -eq '') {
            if ($morceaux.Count -eq 1) { $morceaux = @() } else { $morceaux = @($morceaux[0..($morceaux.Count - 2)]) }
        }
        if ($morceaux.Count -gt $Lignes) { $morceaux = @($morceaux[($morceaux.Count - $Lignes)..($morceaux.Count - 1)]) }
        return ,@($morceaux)
    } catch {
        return ,@()
    } finally {
        if ($flux) { $flux.Dispose() }
    }
}

function Protect-TexteRapport {
    <#
        Masque ce qui ne doit jamais sortir du PC : toute ligne parlant de token,
        authorization, bearer ou password, et tout motif 123-456 (code d'association).
    #>
    param([string[]]$Lignes)
    $sortie = @()
    foreach ($l in @($Lignes)) {
        $t = [string]$l
        if ($t -match '(?i)token|authorization|bearer|password|passwd|secret|private.?key|api.?key') { $sortie += '[ligne masquée : donnée sensible possible]'; continue }
        $sortie += ([regex]::Replace($t, '\b\d{3}-\d{3}\b', '***-***'))
    }
    return ,@($sortie)
}

function Invoke-ActionLocaleSure {
    <# Relais d'Invoke-ActionLocale (commun.ps1) : le socle reconnaît lui-même l'agent trop ancien, quel que soit $ErrorActionPreference. #>
    param([string]$Racine, [string]$Action, [int]$DelaiSecondes = 8)
    return (Invoke-ActionLocale -Racine $Racine -Action $Action -DelaiSecondes $DelaiSecondes)
}

# Tailles lisibles : Format-Taille, dans commun.ps1 (une seule mise en forme pour tous les outils).
