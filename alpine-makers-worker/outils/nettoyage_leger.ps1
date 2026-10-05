<#
    Nettoyage léger du Worker : petits fichiers temporaires de plus de 7 jours.
    Rien n'est jamais supprimé sans un accord explicite.

    Usage :
      nettoyage_leger.ps1                 (menu, double-clic) liste, PUIS propose la suppression réelle : une question O/N
      nettoyage_leger.ps1 -SansPause      liste seule (rien n'est supprimé, aucune question)
      nettoyage_leger.ps1 -Appliquer      liste, vérifie, puis supprime après confirmation
      -Simulation : force la simulation même avec -Appliquer.  -Oui / -SansPause : sans clavier.
      -Jours N : ancienneté minimale (7 au minimum).
#>
param(
    [string]$Racine = '',
    [switch]$SansPause,
    [switch]$Oui,
    [switch]$Simulation,
    [switch]$Appliquer,
    [ValidateRange(7, 3650)][int]$Jours = 7,
    [switch]$DejaEleve
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
Initialize-ConsoleWorker -Titre 'Nettoyage léger du Worker'
$Racine = Get-RacineWorker -Racine $Racine   # déjà en forme LONGUE (un nom court PRENOM~1 fausserait les comparaisons de dossiers)

$NomOutil = 'nettoyage_leger'
$reel = ($Appliquer -and -not $Simulation)
$limite = (Get-Date).AddDays(-$Jours)
# Fichiers à ne JAMAIS retirer, même s'ils correspondaient un jour à un motif.
$NomsProteges = @('engines-memo.json', 'extinction.json', 'cleanup-preview.json', 'worker-journal.sqlite3', 'worker-journal.sqlite3-wal', 'worker-journal.sqlite3-shm')

# Format-Taille : commun.ps1.

function Test-Lien {
    param($Element)
    return [bool]($Element.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Get-BilanArbre {
    <# Taille, date la plus récente et présence de liens, SANS suivre aucun lien ni jonction. #>
    param([string]$Chemin)
    $bilan = [pscustomobject]@{ Octets = [double]0; PlusRecent = [datetime]::MinValue; Liens = 0; Illisible = $false }
    $pile = New-Object System.Collections.Stack
    $pile.Push($Chemin)
    while ($pile.Count -gt 0) {
        $courant = [string]$pile.Pop()
        $elements = @()
        try { $elements = @(Get-ChildItem -LiteralPath $courant -Force -ErrorAction Stop) } catch { $bilan.Illisible = $true; continue }
        foreach ($e in $elements) {
            if (Test-Lien $e) { $bilan.Liens++; continue }
            if ($e.LastWriteTime -gt $bilan.PlusRecent) { $bilan.PlusRecent = $e.LastWriteTime }
            if ($e.PSIsContainer) { $pile.Push($e.FullName) } else { $bilan.Octets += [double]$e.Length }
        }
    }
    return $bilan
}

function Test-CibleAutorisee {
    <#
        Dernier garde-fou avant toute suppression : le chemin doit être dans l'une des zones prévues,
        ET aucun dossier entre la racine du Worker et lui ne doit être un lien ou une jonction
        (GetFullPath ne résout pas les jonctions : sans ce contrôle, runtime\local-control remplacé par
        une jonction ferait supprimer des fichiers HORS du Worker).
    #>
    param([string]$Chemin, [string]$Type)
    $complet = [IO.Path]::GetFullPath($Chemin)
    $nom = Split-Path -Leaf $complet
    if ($NomsProteges -contains $nom) { return $false }
    $parent = (Split-Path -Parent $complet).TrimEnd('\')
    # GetFullPath développe les noms courts (PRENOM~1) : la racine suit le même chemin pour rester comparable.
    $racineLongue = [IO.Path]::GetFullPath($Racine).TrimEnd('\')
    if (-not $parent.StartsWith($racineLongue + '\', [System.StringComparison]::OrdinalIgnoreCase)) { return $false }
    $courant = $parent
    while ($courant -and ($courant.Length -gt $racineLongue.Length)) {
        try { if (Test-Lien (Get-Item -LiteralPath $courant -Force -ErrorAction Stop)) { return $false } } catch { return $false }
        $courant = Split-Path -Parent $courant
    }
    switch ($Type) {
        'demande'   { return (($parent -ieq (Join-Path $racineLongue 'runtime\local-control')) -and ($nom -like '*.request.json')) }
        'reponse'   { return (($parent -ieq (Join-Path $racineLongue 'runtime\local-control')) -and ($nom -like '*.result.json')) }
        'pip'       { return (($parent -ieq (Join-Path $racineLongue 'temp')) -and ($nom -like 'pip-*')) }
        'workspace' { return (($parent -ieq (Join-Path $racineLongue 'workspace')) -and ($nom -like '_raphal_*.json' -or $nom -like '_orca_*_stdout.txt' -or $nom -like '_orca_*_stderr.txt' -or $nom -like '_freecad_*.txt')) }
    }
    return $false
}

function Remove-ArbreSansLien {
    <# Supprime un dossier fichier par fichier. Un lien ou une jonction n'est ni suivi ni supprimé : le dossier reste alors en place. #>
    param([string]$Chemin)
    $echecs = 0
    $elements = @()
    try { $elements = @(Get-ChildItem -LiteralPath $Chemin -Force -ErrorAction Stop) } catch { return 1 }
    foreach ($e in $elements) {
        if (Test-Lien $e) { $echecs++; continue }
        if ($e.PSIsContainer) { $echecs += (Remove-ArbreSansLien -Chemin $e.FullName) }
        else { try { Remove-Item -LiteralPath $e.FullName -Force -ErrorAction Stop } catch { $echecs++ } }
    }
    if ($echecs -eq 0) { try { [IO.Directory]::Delete($Chemin, $false) } catch { $echecs++ } }
    return $echecs
}

function Get-Blocage {
    <#
        Y a-t-il un job actif de l'identité courante ou une maintenance ?
        Renvoie : Connu ($true si on a pu le savoir), Bloque, Raison, Source.
    #>
    $r = [pscustomobject]@{ Connu = $false; Bloque = $false; Raison = ''; Source = ''; AgentTropAncien = $false }
    $etat = Invoke-ActionLocale -Racine $Racine -Action 'status' -DelaiSecondes 12
    $r.AgentTropAncien = [bool]$etat.NonSupporte
    if ($etat.Ok -and $etat.Resultat) {
        $r.Connu = $true; $r.Source = 'agent (status)'
        $s = $etat.Resultat
        $jobs = @($s.jobs_active | Where-Object { $_ })
        if ($s.maintenance_active -or $s.maintenance_recovery) { $r.Bloque = $true; $r.Raison = 'une maintenance du Worker est en cours' }
        elseif ($jobs.Count -gt 0) { $r.Bloque = $true; $r.Raison = ([string]$jobs.Count + ' tâche(s) en cours sur ce Worker') }
        return $r
    }
    # Repli : jobs_bloquants.py --json, s'il est livré à côté des outils ou à la racine.
    $python = Get-PythonWorker -Racine $Racine
    foreach ($candidat in @((Join-Path $PSScriptRoot 'jobs_bloquants.py'), (Join-Path $Racine 'jobs_bloquants.py'))) {
        if (-not $python) { break }
        if (-not (Test-Path -LiteralPath $candidat -PathType Leaf)) { continue }
        $ancien = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        $sortie = & $python $candidat --root $Racine --json 2>$null
        $code = $LASTEXITCODE
        $ErrorActionPreference = $ancien
        $json = ($sortie | ForEach-Object { [string]$_ } | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1)
        if (-not $json) { continue }
        try { $o = $json | ConvertFrom-Json } catch { continue }
        # Contrat de jobs_bloquants.py : ok, error, active_current (identité courante), active_previous.
        if (-not $o.ok) {
            # Pas de journal du tout = Worker jamais lancé = aucune tâche possible. Toute autre erreur = état inconnu.
            if ([string]$o.error -eq 'journal_absent') { $r.Connu = $true; $r.Source = 'journal du Worker (absent)' }
            return $r
        }
        $r.Connu = $true; $r.Source = 'journal du Worker (jobs_bloquants.py)'
        $actifs = 0
        try { $actifs = [int]$o.active_current } catch { $actifs = 1 }
        if ($actifs -gt 0) { $r.Bloque = $true; $r.Raison = ([string]$actifs + ' tâche(s) non terminée(s) dans le journal du Worker') }
        return $r
    }
    return $r
}

function Test-WorkerEteint {
    <# Sans élévation : service arrêté (ou aucun processus lisible), aucun port de moteur ouvert, relais caméra fermé. #>
    $mode = Get-ModeLancement -Racine $Racine
    if ($mode.Mode -eq 'service' -and $mode.Service.Etat -ne 'Stopped') { return $false }
    if (Test-AgentVivant) { return $false }
    if (@(Get-EtatMoteurs -Racine $Racine | Where-Object { $_.EnEcoute }).Count -gt 0) { return $false }
    if (@((Get-ProcessusDuWorker -Racine $Racine).Processus).Count -gt 0) { return $false }
    return $true
}

# ---------------------------------------------------------------------------

Write-Explication -Titre 'NETTOYAGE LÉGER DU WORKER' -Fait @(
    ('Liste les petits fichiers temporaires vieux de plus de ' + $Jours + ' jours : réponses et demandes jamais lues de la boîte aux lettres locale, restes d''installation pip (temp\pip-*), traces Orca / FreeCAD du dossier workspace\.'),
    'Montre d''abord la liste, puis te demande si tu veux vraiment supprimer. Sans ton accord, rien n''est supprimé.'
) -NeFaitPas @(
    'Ne touche jamais aux moteurs (components\), aux tâches (jobs\), aux résultats (outputs\), aux réglages (config\, config.json), aux journaux (logs\) ni au journal durable.',
    'Ne libère pas des dizaines de Gio : pour cela, utilise le nettoyage du stockage depuis le dashboard.',
    'Ne suit aucun raccourci, lien ou jonction. N''arrête rien.'
) -Exige @(
    'Aucune tâche en cours et aucune maintenance sur ce Worker (vérifié avant de supprimer).',
    'Aucun droit administrateur.'
)

# --- Recherche des cibles ----------------------------------------------------
$cibles = @()
$ignoresLiens = 0

$dossierRuntime = Join-Path $Racine 'runtime'
$dossierReponses = Join-Path $dossierRuntime 'local-control'
if (Test-Path -LiteralPath $dossierReponses -PathType Container) {
    # Même règle que temp\ et workspace\ : une zone qui est (ou passe par) un lien ou une jonction est ignorée en entier.
    if ((Test-Lien (Get-Item -LiteralPath $dossierRuntime -Force)) -or (Test-Lien (Get-Item -LiteralPath $dossierReponses -Force))) { $ignoresLiens++ }
    else {
        foreach ($f in @(Get-ChildItem -LiteralPath $dossierReponses -Filter '*.result.json' -File -Force -ErrorAction SilentlyContinue)) {
            if (Test-Lien $f) { $ignoresLiens++; continue }
            if ($f.LastWriteTime -ge $limite) { continue }
            $cibles += [pscustomobject]@{ Type = 'reponse'; Groupe = 'Réponses de la boîte aux lettres locale'; Chemin = $f.FullName; Octets = [double]$f.Length; Date = $f.LastWriteTime; Dossier = $false }
        }
        # Demandes jamais lues par l'agent : après ce délai elles sont échues. Les retirer empêche aussi
        # qu'une vieille demande soit exécutée des jours plus tard, au prochain démarrage de l'agent.
        foreach ($f in @(Get-ChildItem -LiteralPath $dossierReponses -Filter '*.request.json' -File -Force -ErrorAction SilentlyContinue)) {
            if (Test-Lien $f) { $ignoresLiens++; continue }
            if ($f.LastWriteTime -ge $limite) { continue }
            $cibles += [pscustomobject]@{ Type = 'demande'; Groupe = 'Demandes locales jamais lues par l''agent (échues)'; Chemin = $f.FullName; Octets = [double]$f.Length; Date = $f.LastWriteTime; Dossier = $false }
        }
    }
}

$dossierTemp = Join-Path $Racine 'temp'
if ((Test-Path -LiteralPath $dossierTemp -PathType Container) -and -not (Test-Lien (Get-Item -LiteralPath $dossierTemp -Force))) {
    foreach ($d in @(Get-ChildItem -LiteralPath $dossierTemp -Filter 'pip-*' -Directory -Force -ErrorAction SilentlyContinue)) {
        if (Test-Lien $d) { $ignoresLiens++; continue }
        if ($d.LastWriteTime -ge $limite -or $d.CreationTime -ge $limite) { continue }
        $bilan = Get-BilanArbre -Chemin $d.FullName
        if ($bilan.PlusRecent -ge $limite) { continue }
        if ($bilan.Liens -gt 0) { $ignoresLiens++; continue }
        $cibles += [pscustomobject]@{ Type = 'pip'; Groupe = 'Restes d''installation pip (temp\pip-*)'; Chemin = $d.FullName; Octets = $bilan.Octets; Date = $d.LastWriteTime; Dossier = $true }
    }
}

$dossierTravail = Join-Path $Racine 'workspace'
if ((Test-Path -LiteralPath $dossierTravail -PathType Container) -and -not (Test-Lien (Get-Item -LiteralPath $dossierTravail -Force))) {
    foreach ($f in @(Get-ChildItem -LiteralPath $dossierTravail -File -Force -ErrorAction SilentlyContinue)) {
        $n = $f.Name
        if (-not ($n -like '_raphal_*.json' -or $n -like '_orca_*_stdout.txt' -or $n -like '_orca_*_stderr.txt' -or $n -like '_freecad_*.txt')) { continue }
        if (Test-Lien $f) { $ignoresLiens++; continue }
        if ($f.LastWriteTime -ge $limite) { continue }
        $cibles += [pscustomobject]@{ Type = 'workspace'; Groupe = 'Traces Orca / FreeCAD (workspace\)'; Chemin = $f.FullName; Octets = [double]$f.Length; Date = $f.LastWriteTime; Dossier = $false }
    }
}

# Garde-fou : tout ce qui ne passe pas le contrôle de zone est écarté, et on le dit.
$ecartes = @($cibles | Where-Object { -not (Test-CibleAutorisee -Chemin $_.Chemin -Type $_.Type) })
$cibles = @($cibles | Where-Object { Test-CibleAutorisee -Chemin $_.Chemin -Type $_.Type })

# --- Présentation -----------------------------------------------------------
$total = [double]0
foreach ($c in $cibles) { $total += $c.Octets }
foreach ($groupe in @($cibles | Group-Object Groupe)) {
    Write-Section $groupe.Name
    $somme = [double]0
    foreach ($c in $groupe.Group) { $somme += $c.Octets }
    Write-Etat -Libelle 'Éléments' -Valeur ([string]$groupe.Count + '  (' + (Format-Taille $somme) + ')') -Niveau info
    $montres = 0
    foreach ($c in @($groupe.Group | Sort-Object Octets -Descending)) {
        if ($montres -ge 12) { Write-Info ('     ... et ' + ($groupe.Count - $montres) + ' autre(s)'); break }
        $relatif = $c.Chemin.Substring($Racine.Length + 1)
        Write-Info ('     ' + $c.Date.ToString('dd.MM.yyyy') + '  ' + (Format-Taille $c.Octets).PadLeft(10) + '  ' + $relatif)
        $montres++
    }
}
Write-Section 'Bilan'
Write-Etat -Libelle 'Éléments de plus de' -Valeur ([string]$Jours + ' jours : ' + $cibles.Count) -Niveau info
Write-Etat -Libelle 'Place récupérable' -Valeur (Format-Taille $total) -Niveau info
if ($ignoresLiens -gt 0) { Write-Alerte ([string]$ignoresLiens + ' élément(s) ignoré(s) car ce sont (ou contiennent) des liens ou jonctions : jamais suivis, jamais supprimés.') }
if ($ecartes.Count -gt 0) { Write-Alerte ([string]$ecartes.Count + ' élément(s) écarté(s) par le garde-fou de zone.') }

if ($cibles.Count -eq 0) {
    Write-Ok 'Rien à nettoyer : le Worker est déjà propre.'
    Wait-FinOutil -SansPause:$SansPause
    exit 0
}

# Lancé sans paramètre (depuis le menu) : après la liste, l'outil propose lui-même de passer au réel.
$interactif = (-not $Appliquer) -and (-not $Simulation) -and (-not $SansPause) -and (-not $Oui)
if ($interactif) {
    Write-Host ''
    Write-Info 'Pour l''instant rien n''a été supprimé : ceci n''était que la liste.'
    if (Confirm-Action -Question 'Veux-tu passer à la suppression réelle (une vérification est faite avant) ?') { $reel = $true }
}
if (-not $reel) {
    Write-Host ''
    Write-Alerte 'Rien n''a été supprimé : ceci n''était que la liste.'
    Write-Conseil ('Pour supprimer réellement : relance ' + (Get-NomEntree 14) + ' depuis le menu et réponds O à la question, ou lance outils\nettoyage_leger.bat -Appliquer.')
    Wait-FinOutil -SansPause:$SansPause
    exit 0
}

# --- Contrôle : pas de job, pas de maintenance ------------------------------------
Write-Section 'Vérification avant suppression'
$blocage = Get-Blocage
if ($blocage.Connu) {
    if ($blocage.Bloque) {
        Write-Erreur ('Nettoyage refusé : ' + $blocage.Raison + '.')
        Write-Conseil 'Attends la fin, puis relance cet outil. Rien n''a été supprimé.'
        Wait-FinOutil -SansPause:$SansPause
        exit 2
    }
    Write-Ok ('Aucune tâche ni maintenance en cours (source : ' + $blocage.Source + ').')
    if ($blocage.AgentTropAncien) { Write-Info 'L''agent installé ne connaît pas encore la demande « status » : la vérification a lu directement le journal du Worker. Mets le Worker à jour pour une vérification complète (maintenance comprise).' }
} else {
    if (Test-WorkerEteint) {
        Write-Ok 'Le Worker est éteint : aucune tâche ne peut être en cours.'
    } else {
        Write-Erreur 'Impossible de savoir si une tâche est en cours : l''agent installé ne répond pas à la demande « status ».'
        Write-Conseil ('Mets le Worker à jour depuis le site (Compte > Mes PC / Workers > ce Worker > « Mettre à jour » : ce n''est pas une entrée du menu), ou éteins-le avec ' + (Get-NomEntree 11) + ', puis relance ce nettoyage.')
        Write-Info 'Rien n''a été supprimé.'
        Wait-FinOutil -SansPause:$SansPause
        exit 2
    }
}

if (-not (Confirm-Action -Question ('Supprimer ces ' + $cibles.Count + ' éléments (' + (Format-Taille $total) + ') ?') -Oui:($Oui -or $interactif))) {
    Write-Info 'Annulé : rien n''a été supprimé.'
    Wait-FinOutil -SansPause:$SansPause
    exit 2
}

# --- Suppression -----------------------------------------------------------
$retires = 0; $echecs = 0; $octetsRetires = [double]0
foreach ($c in $cibles) {
    if (-not (Test-CibleAutorisee -Chemin $c.Chemin -Type $c.Type)) { $echecs++; continue }
    try {
        $element = Get-Item -LiteralPath $c.Chemin -Force -ErrorAction Stop
        if (Test-Lien $element) { $echecs++; continue }
        if ($c.Dossier) {
            $restes = Remove-ArbreSansLien -Chemin $c.Chemin
            if ($restes -gt 0) { $echecs++; continue }
        } else {
            Remove-Item -LiteralPath $c.Chemin -Force -ErrorAction Stop
        }
        $retires++; $octetsRetires += $c.Octets
    } catch { $echecs++ }
}
Write-Section 'Résultat'
Write-Ok ([string]$retires + ' élément(s) supprimé(s), ' + (Format-Taille $octetsRetires) + ' récupérés.')
if ($echecs -gt 0) { Write-Alerte ([string]$echecs + ' élément(s) n''ont pas pu être supprimés (ouverts, protégés ou contenant un lien). Ils sont restés en place.') }
Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('Nettoyage appliqué : ' + $retires + ' supprimés, ' + $echecs + ' échecs, ' + (Format-Taille $octetsRetires))
Wait-FinOutil -SansPause:$SansPause
if ($echecs -gt 0) { exit 1 }
exit 0
