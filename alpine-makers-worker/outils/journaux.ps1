<#
    Consulter les journaux du Worker, en LECTURE SEULE.

    Usage :
        journaux.ps1                                   choix interactif du journal puis du mode
        journaux.ps1 -Liste -SansPause                 liste des journaux présents, avec leur clé
        journaux.ps1 -Fichier printguard -Mode erreurs -SansPause
        journaux.ps1 -Fichier service -Mode suivre     défilement en direct (Ctrl+C pour sortir)

    Clés : images, 3d, printguard, orca, orca-bridge, outils, service, service-erreurs,
           service-precedent, service-erreurs-precedent, supervision.
           « service-precedent » et « service-erreurs-precedent » = le dernier journal daté non
           vide du service : après un plantage suivi d'une relance, la cause est LÀ, car le
           journal courant repart vide à chaque redémarrage du service.
    Le nom technique d'un moteur est accepté aussi : image_generation, ai3d, model-studio.
    Modes : fin (60 dernières lignes), erreurs (lignes d'erreur des 2000 dernières),
            ouvrir (Bloc-notes), suivre (en direct).

    Codes de sortie : 0 = fait, 1 = journal inconnu ou absent, 2 = annulé.
#>
param(
    [string]$Racine = '',
    [string]$Fichier = '',
    [ValidateSet('', 'fin', 'erreurs', 'ouvrir', 'suivre')][string]$Mode = '',
    [int]$Lignes = 60,
    [switch]$Liste,
    [switch]$SansPause
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'diagnostic_commun.ps1')
Initialize-ConsoleWorker -Titre 'Journaux du Worker'
try { $Racine = Get-RacineWorker -Racine $Racine } catch { Write-Erreur ([string]$_.Exception.Message); Wait-FinOutil -SansPause:$SansPause; exit 1 }

Write-Explication -Titre 'Journaux du Worker' -Fait @(
    'Montre ce que les moteurs, le service et les outils ont écrit :',
    'la fin d''un journal, seulement ses lignes d''erreur, ou son défilement en direct.',
    'Lit un journal même pendant que le moteur écrit dedans.'
) -NeFaitPas @(
    'Ne modifie et ne vide aucun journal.',
    'N''ouvre jamais la configuration, l''identité ni un fichier de token.',
    'Masque à l''écran toute ligne sensible et tout code d''association.'
) -Exige @(
    'Rien. Aucune fenêtre administrateur.'
)

$journaux = @(Get-JournauxConnus -Racine $Racine)
$presents = @($journaux | Where-Object { $_.Existe })

function Show-ListeJournaux {
    Write-Section 'Journaux présents sur ce PC'
    $n = 0
    foreach ($j in $presents) {
        $n++
        $modifie = ''
        try { $modifie = (Get-Item -LiteralPath $j.Chemin -Force).LastWriteTime.ToString('yyyy-MM-dd HH:mm') } catch { }
        Write-Host ('   ' + ([string]$n).PadLeft(2) + '. ') -ForegroundColor Cyan -NoNewline
        Write-Host ($j.Cle.PadRight(26) + ' ') -ForegroundColor White -NoNewline
        Write-Host ($j.Nom.PadRight(34) + ' ' + (Format-Taille $j.Taille).PadLeft(10) + '   ' + $modifie) -ForegroundColor Gray
    }
    $absents = @($journaux | Where-Object { -not $_.Existe })
    if (@($presents | Where-Object { $_.Cle -like 'service*precedent' }).Count -gt 0) {
        Write-Host ''
        Write-Conseil 'Le Worker a planté puis a été relancé ? Regarde « service-erreurs-precedent » : le journal'
        Write-Conseil 'd''erreurs courant repart vide à chaque redémarrage du service.'
    }
    if ($absents.Count -gt 0) { Write-Host ''; Write-Info ('Absents (normal si le moteur n''a jamais tourné ou si le Worker n''est pas un service) : ' + (($absents | ForEach-Object { $_.Cle }) -join ', ')) }
}

if ($presents.Count -eq 0) {
    Write-Alerte 'Aucun journal n''existe encore : le Worker n''a jamais lancé de moteur sur ce PC.'
    Wait-FinOutil -SansPause:$SansPause
    exit 0
}

# --- Choix du journal -------------------------------------------------------
$choisi = $null
# Le nom technique d'un moteur (celui qu'affichent le dashboard et l'état du Worker) vaut sa clé.
$aliasJournaux = @{ 'image_generation' = 'images'; 'ai3d' = '3d'; 'model-studio' = 'orca'; 'orca_bridge' = 'orca-bridge' }
if ($Fichier -and $aliasJournaux.ContainsKey($Fichier.Trim().ToLower())) { $Fichier = $aliasJournaux[$Fichier.Trim().ToLower()] }
if ($Fichier) {
    $choisi = @($journaux | Where-Object { $_.Cle -ieq $Fichier.Trim() }) | Select-Object -First 1
    if (-not $choisi) {
        Write-Erreur ('Journal inconnu : ' + $Fichier)
        Write-Conseil ('Clés possibles : ' + (($journaux | ForEach-Object { $_.Cle }) -join ', '))
        Wait-FinOutil -SansPause:$SansPause
        exit 1
    }
    if (-not $choisi.Existe) {
        Write-Alerte ('Le journal « ' + $choisi.Nom + ' » n''existe pas sur ce PC.')
        if ($choisi.Chemin) { Write-Info ('Emplacement attendu : ' + $choisi.Chemin) }
        else { Write-Info 'Aucun ancien journal non vide du service sur ce PC (service jamais redémarré, ou Worker lancé sans service).' }
        Wait-FinOutil -SansPause:$SansPause
        exit 1
    }
} else {
    Show-ListeJournaux
    if ($Liste -or $SansPause) {
        Write-Host ''
        Write-Conseil 'Pour en lire un sans clavier : journaux.ps1 -Fichier <clé> -Mode fin|erreurs -SansPause'
        Wait-FinOutil -SansPause:$SansPause
        exit 0
    }
    Write-Host ''
    $saisie = ([string](Read-Host '   Numéro ou clé du journal (Entrée pour annuler)')).Trim()
    if (-not $saisie) { exit 2 }
    $numero = 0
    if ([int]::TryParse($saisie, [ref]$numero) -and $numero -ge 1 -and $numero -le $presents.Count) { $choisi = $presents[$numero - 1] }
    else {
        if ($aliasJournaux.ContainsKey($saisie.ToLower())) { $saisie = $aliasJournaux[$saisie.ToLower()] }
        $choisi = @($presents | Where-Object { $_.Cle -ieq $saisie }) | Select-Object -First 1
    }
    if (-not $choisi) { Write-Erreur 'Choix non reconnu.'; Wait-FinOutil -SansPause:$SansPause; exit 1 }
}

# --- Choix du mode ----------------------------------------------------------
if (-not $Mode) {
    if ($Fichier -or $SansPause) { $Mode = 'fin' }
    else {
        Write-Section 'Que veux-tu voir ?'
        Write-Host '    1. fin       les 60 dernières lignes' -ForegroundColor Gray
        Write-Host '    2. erreurs   seulement les lignes d''erreur (parmi les 2000 dernières)' -ForegroundColor Gray
        Write-Host '    3. ouvrir    le fichier entier dans le Bloc-notes' -ForegroundColor Gray
        Write-Host '    4. suivre    le défilement en direct (touche Q pour revenir)' -ForegroundColor Gray
        Write-Host ''
        $m = ([string](Read-Host '   Ton choix [1]')).Trim().ToLower()
        switch ($m) {
            ''  { $Mode = 'fin' } '1' { $Mode = 'fin' } 'fin' { $Mode = 'fin' }
            '2' { $Mode = 'erreurs' } 'erreurs' { $Mode = 'erreurs' }
            '3' { $Mode = 'ouvrir' } 'ouvrir' { $Mode = 'ouvrir' }
            '4' { $Mode = 'suivre' } 'suivre' { $Mode = 'suivre' }
            default { Write-Erreur 'Choix non reconnu.'; Wait-FinOutil -SansPause:$SansPause; exit 1 }
        }
    }
}

function Write-LigneJournal {
    param([string]$Texte)
    if (Test-LigneErreur $Texte) { Write-Host ('   ' + $Texte) -ForegroundColor Red }
    elseif ($Texte -match '(?i)warn') { Write-Host ('   ' + $Texte) -ForegroundColor Yellow }
    else { Write-Host ('   ' + $Texte) -ForegroundColor Gray }
}

Write-Section ($choisi.Nom + '  [' + $Mode + ']')
Write-Info $choisi.Chemin
Write-Host ''

switch ($Mode) {
    'fin' {
        if ($Lignes -lt 1) { $Lignes = 60 }
        $fin = Get-FinFichier -Chemin $choisi.Chemin -Lignes $Lignes
        if ($fin.Count -eq 0) { Write-Info '(journal vide)' }
        foreach ($l in (Protect-TexteRapport -Lignes $fin)) { Write-LigneJournal $l }
        Write-Host ''
        Write-Info ([string]$fin.Count + ' ligne(s) affichée(s).')
    }
    'erreurs' {
        $fin = Get-FinFichier -Chemin $choisi.Chemin -Lignes 2000
        $erreurs = @($fin | Where-Object { Test-LigneErreur $_ })
        if ($erreurs.Count -eq 0) { Write-Ok ('Aucune ligne d''erreur dans les ' + $fin.Count + ' dernières lignes.') }
        else {
            foreach ($l in (Protect-TexteRapport -Lignes $erreurs)) { Write-LigneJournal $l }
            Write-Host ''
            Write-Alerte ([string]$erreurs.Count + ' ligne(s) d''erreur sur ' + $fin.Count + ' lues.')
            Write-Conseil 'Un « Traceback » Python s''étale sur plusieurs lignes : pour le lire en entier, utilise le mode fin ou ouvrir.'
        }
    }
    'ouvrir' {
        Write-Info 'Ouverture dans le Bloc-notes (le fichier n''est pas modifié tant que tu n''enregistres pas).'
        Start-Process -FilePath 'notepad.exe' -ArgumentList (ConvertTo-ArgumentCite $choisi.Chemin)
    }
    'suivre' {
        # Même test d'encodage que les modes fin et erreurs. Tant qu'aucun accent ne tranche
        # ('ascii'), on suit la règle constatée : journaux du service en Windows-1252 (ANSI,
        # « Default » sous PowerShell 5.1), les autres en UTF-8.
        $encodageSuivi = 'UTF8'
        switch (Get-EncodageJournal -Chemin $choisi.Chemin) {
            'utf16' { $encodageSuivi = 'Unicode' }
            '1252'  { $encodageSuivi = 'Default' }
            'ascii' { if ($choisi.Cle -like 'service*') { $encodageSuivi = 'Default' } }
        }
        # Lancé depuis le menu, l'outil tourne DANS le processus du menu : Ctrl+C fermerait tout le menu.
        # Avec un clavier, on suit donc le fichier par relecture et on sort sur une touche.
        $clavier = $false
        if (-not $SansPause) { try { $clavier = (-not [Console]::IsInputRedirected) } catch { $clavier = $false } }
        if (-not $clavier) {
            Write-Info 'Défilement en direct, sans clavier : Ctrl+C (ou la fermeture de la fenêtre) pour sortir.'
            Write-Host ''
            Get-Content -LiteralPath $choisi.Chemin -Tail 20 -Wait -Encoding $encodageSuivi | ForEach-Object { foreach ($l in (Protect-TexteRapport -Lignes @([string]$_))) { Write-LigneJournal $l } }
        } else {
            Write-Info 'Défilement en direct. Appuie sur Q (ou Entrée, ou Échap) pour arrêter et revenir. N''utilise pas Ctrl+C : il fermerait le menu.'
            Write-Host ''
            foreach ($l in (Protect-TexteRapport -Lignes (Get-FinFichier -Chemin $choisi.Chemin -Lignes 20))) { Write-LigneJournal $l }
            $encodage = [Text.Encoding]::UTF8
            if ($encodageSuivi -eq 'Unicode') { $encodage = [Text.Encoding]::Unicode } elseif ($encodageSuivi -eq 'Default') { $encodage = [Text.Encoding]::Default }
            $decodeur = $encodage.GetDecoder()
            $position = [long]0
            try { $position = [long](Get-Item -LiteralPath $choisi.Chemin -Force).Length } catch { }
            $reste = ''
            $tampon = New-Object byte[] 65536
            $caracteres = New-Object char[] ($encodage.GetMaxCharCount($tampon.Length))
            while ([Console]::KeyAvailable) { [void][Console]::ReadKey($true) }
            $sortir = $false
            while (-not $sortir) {
                while ([Console]::KeyAvailable) {
                    $touche = [Console]::ReadKey($true)
                    if ($touche.Key -eq [ConsoleKey]::Q -or $touche.Key -eq [ConsoleKey]::Enter -or $touche.Key -eq [ConsoleKey]::Escape) { $sortir = $true }
                }
                if ($sortir) { break }
                $flux = $null
                try {
                    # Partage complet : le moteur continue d'écrire (et nssm de faire tourner le fichier) pendant la lecture.
                    $flux = [IO.File]::Open($choisi.Chemin, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
                    if ($flux.Length -lt $position) { $position = 0; $reste = ''; $decodeur.Reset(); Write-Info '(le journal a été recommencé : reprise au début)' }
                    if ($flux.Length -gt $position) {
                        [void]$flux.Seek($position, [IO.SeekOrigin]::Begin)
                        while ($true) {
                            $lus = $flux.Read($tampon, 0, $tampon.Length)
                            if ($lus -le 0) { break }
                            $n = $decodeur.GetChars($tampon, 0, $lus, $caracteres, 0)
                            if ($n -gt 0) { $reste += [string]::new($caracteres, 0, $n) }
                        }
                        $position = $flux.Position
                    }
                } catch { } finally { if ($flux) { $flux.Dispose() } }
                if ($reste.IndexOf("`n") -ge 0) {
                    $morceaux = @($reste -split "`r?`n")
                    $reste = [string]$morceaux[$morceaux.Count - 1]
                    $completes = @()
                    if ($morceaux.Count -gt 1) { $completes = @($morceaux[0..($morceaux.Count - 2)]) }
                    foreach ($l in (Protect-TexteRapport -Lignes $completes)) { Write-LigneJournal $l }
                }
                Start-Sleep -Milliseconds 500
            }
            Write-Host ''
            Write-Info 'Suivi arrêté.'
        }
    }
}
Wait-FinOutil -SansPause:$SansPause
exit 0
