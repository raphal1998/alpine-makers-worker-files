<#
    Contrôle total et réparation du Worker (entrée 18 du menu), et moteur de « Réparer » (entrée 24).

    Usage :
        controle_total.ps1                       contrôle complet, verdict, puis une question par réparation
        controle_total.ps1 -SansReparation       contrôle et verdict seulement : rien n'est lancé
        controle_total.ps1 -Simulation           contrôle et verdict, puis ce qui SERAIT lancé : rien n'est lancé
        controle_total.ps1 -Oui -SansPause       sans clavier : accepte les réparations rapides, jamais les lourdes
        controle_total.ps1 -Rapide               ce que fait « Réparer » (reparer.ps1) : un résumé, UNE question,
                                                 réparations rapides et sûres seulement
    Codes de sortie : 0 = contrôle fait (réparé, ou rien à réparer) ; 1 = une réparation lancée a échoué,
    ou contrôle impossible ; 2 = tout a été refusé.

    Ce que l'outil fait lui-même : lire. Le contrôle local est celui d'« État complet du Worker »
    (etat_worker.ps1 -Json, lancé à part) ; ce que le site voit est demandé à l'agent, qui interroge le site.
    Chaque réparation est faite par un autre : un outil existant du menu (lancé dans un PowerShell à part par
    Invoke-OutilEnfant, jamais dans ce processus, après ton accord), ou le site (demande portée par l'agent).
    Aucune élévation ici : c'est l'outil appelé qui la demande, s'il en a besoin.
    -Oui ne lève JAMAIS la confirmation des réparations lourdes (mot LOURD à taper).
#>
param(
    [string]$Racine = '',
    [switch]$SansPause,
    [switch]$Oui,
    [switch]$Simulation,
    [switch]$SansReparation,
    [switch]$Rapide
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'commun_cycle.ps1')
. (Join-Path $PSScriptRoot 'commun_gestion.ps1')
$NomOutil = 'controle_total'
$TitreOutil = 'Contrôle total et réparation du Worker'
if ($Rapide) { $NomOutil = 'reparer'; $TitreOutil = 'Réparer le Worker' }
Initialize-ConsoleWorker -Titre $TitreOutil

# ---------------------------------------------------------------------------
# Tables : quel code appelle quelle réparation
# ---------------------------------------------------------------------------

# Outils existants du menu, dans l'ordre où ils sont lancés. Classe R : proposé aussi par « Réparer » (24) ;
# classe T : proposé par « Contrôle total » (18) seulement. Tous reçoivent -Racine, et -Oui -SansPause
# quand ils les déclarent : la question est posée ICI, avant leur lancement et avant toute élévation.
# Admin : l'outil peut ouvrir une fenêtre administrateur. Demarre : il peut (re)démarrer le Worker.
$script:OutilsLocaux = @(
    [pscustomobject]@{ Cle = 'orphelines';  Classe = 'R'; Entree = 15; Fichier = 'demarrage_auto.ps1';   Arguments = @('-RetirerOrphelines', '-Oui', '-SansPause'); Objet = 'retirer les tâches planifiées orphelines';                       Admin = $true;  Demarre = $false }
    [pscustomobject]@{ Cle = 'doublon';     Classe = 'R'; Entree = 15; Fichier = 'demarrage_auto.ps1';   Arguments = @('-RetirerDoublon', '-Oui', '-SansPause');    Objet = 'retirer la tâche planifiée qui fait doublon avec le service';     Admin = $true;  Demarre = $false }
    [pscustomobject]@{ Cle = 'reactiver';   Classe = 'T'; Entree = 15; Fichier = 'demarrage_auto.ps1';   Arguments = @('-ReactiverTache', '-Oui', '-SansPause');    Objet = 'réactiver la tâche planifiée de ce dossier';                      Admin = $true;  Demarre = $false }
    [pscustomobject]@{ Cle = 'nettoyage';   Classe = 'T'; Entree = 14; Fichier = 'nettoyage_leger.ps1';  Arguments = @('-Appliquer', '-Oui', '-SansPause');         Objet = 'supprimer les petits fichiers temporaires de plus de 7 jours';    Admin = $false; Demarre = $false }
    [pscustomobject]@{ Cle = 'rallumer';    Classe = 'R'; Entree = 12; Fichier = 'rallumer_worker.ps1';  Arguments = @('-Oui', '-SansPause');                        Objet = 'remettre le Worker en marche';                                    Admin = $true;  Demarre = $true }
    [pscustomobject]@{ Cle = 'reconnecter'; Classe = 'T'; Entree = 5;  Fichier = 'reconnecter.ps1';      Arguments = @('-SansPause');                                Objet = 'lever la déconnexion demandée depuis le site';                    Admin = $false; Demarre = $true }
    [pscustomobject]@{ Cle = 'redemarrer';  Classe = 'T'; Entree = 10; Fichier = 'redemarrer_agent.ps1'; Arguments = @('-Oui', '-SansPause');                        Objet = 'arrêter puis relancer l''agent';                                  Admin = $true;  Demarre = $true }
)

# Codes du contrôle local (etat_worker.ps1) -> réparation. « site » : étape sûre du plan du site ; « fichiers » :
# réinstallation des fichiers de l'agent par le site. Un code absent de cette table se traite à la main.
$script:CodesReparation = @{
    'L-SERVICE-ARRETE' = 'rallumer'; 'L-SERVICE-PAS-AUTO' = 'rallumer'; 'L-AGENT-ARRETE' = 'rallumer'
    'L-TACHE-ORPHELINE' = 'orphelines'; 'L-DOUBLE-LANCEUR' = 'doublon'; 'L-TACHE-DESACTIVEE' = 'reactiver'
    'L-DISQUE' = 'nettoyage'; 'L-DECONNECTE' = 'reconnecter'; 'L-AGENT-MUET' = 'redemarrer'; 'L-BOITE-BLOQUEE' = 'redemarrer'
    'L-MOTEUR-MUET' = 'site'; 'L-FICHIERS-AGENT' = 'fichiers'
}

# Problèmes vus par le site dont la réparation est une étape sûre de son plan (mise à jour ou redémarrage de
# l'agent, relance d'un moteur). Les autres se réparent par une étape lourde, ou à la main depuis Mes Workers.
$script:CodesSiteSurs = @('agent_outdated', 'job_unconfirmed', 'engine_crashed', 'engine_restarting', 'engine_stopped')

$script:Points = New-Object System.Collections.ArrayList   # le verdict : un point par problème
$script:Bilan = New-Object System.Collections.ArrayList    # ce qui a été lancé, et son issue

# ---------------------------------------------------------------------------
# Fonctions
# ---------------------------------------------------------------------------

function ConvertTo-TexteSur {
    <# Texte venu du site ou d'un fichier, sûr à afficher : une ligne, sans caractère de contrôle, borné. #>
    param($Valeur, [int]$Maximum = 200)
    if ($null -eq $Valeur) { return '' }
    $texte = (([string]$Valeur) -replace '[\x00-\x1F\x7F]', ' ').Trim()
    if ($texte.Length -gt $Maximum) { $texte = $texte.Substring(0, $Maximum - 3) + '...' }
    return $texte
}

function Confirm-ActionVisible {
    <#
        Confirm-Action du socle, avec la question TOUJOURS visible. Quand l'entrée est un tube (worker.exe, qui
        écrit la réponse par son champ « Réponse à l'outil »), PowerShell n'affiche pas l'invite de Read-Host :
        la question est alors écrite ici, avant d'attendre la réponse. Dans une console, Read-Host l'affiche.
        -Oui répond oui sans demander ; il n'est JAMAIS passé pour la confirmation par mot exact (LOURD).
    #>
    param([string]$Question, [string]$MotExact = '', [switch]$Oui)
    if ($Oui) { return $true }
    $entreeRedirigee = $false
    try { $entreeRedirigee = [Console]::IsInputRedirected } catch { }
    if ($entreeRedirigee) {
        if ($MotExact) { Write-Host ('   ' + $Question + ' Tape ' + $MotExact + ' pour confirmer :') -ForegroundColor White }
        else { Write-Host ('   ' + $Question + ' [O/N] :') -ForegroundColor White }
    }
    return (Confirm-Action -Question $Question -MotExact $MotExact)
}

function Get-Liste {
    <# Tableau sans élément nul, que la valeur soit absente, seule ou multiple. #>
    param($Valeur)
    return , @(@($Valeur) | Where-Object { $null -ne $_ })
}

function Get-ControleLocal {
    <#
        Lance etat_worker.ps1 -Json dans un PowerShell à part (lecture seule) et rend son objet.
        Sortie lue en UTF-8 par System.Diagnostics.Process : rien ne dépend de la console de l'appelant.
        Rend : Ok, Etat (objet JSON), Erreur.
    #>
    param([string]$Racine)
    $r = [pscustomobject]@{ Ok = $false; Etat = $null; Erreur = '' }
    $outil = Join-Path $PSScriptRoot 'etat_worker.ps1'
    if (-not (Test-Path -LiteralPath $outil -PathType Leaf)) { $r.Erreur = 'etat_worker.ps1 est absent du dossier outils.'; return $r }
    $texte = ''
    $depasse = $false
    $p = $null
    try {
        $info = New-Object System.Diagnostics.ProcessStartInfo
        $info.FileName = 'powershell.exe'
        # $Racine ne finit jamais par « \ » (Get-RacineWorker) : le guillemet fermant n'est pas échappé.
        $info.Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $outil + '" -Racine "' + $Racine.TrimEnd('\') + '" -Json -SansPause'
        $info.WorkingDirectory = $Racine
        $info.UseShellExecute = $false
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $info.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
        $info.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
        $info.CreateNoWindow = $true
        $p = [System.Diagnostics.Process]::Start($info)
        $lectureSortie = $p.StandardOutput.ReadToEndAsync()
        $lectureErreur = $p.StandardError.ReadToEndAsync()
        $limite = (Get-Date).AddSeconds(180)
        $progression = $false
        while (-not $p.WaitForExit(5000)) {
            Write-Host '.' -NoNewline -ForegroundColor DarkGray; $progression = $true
            if ((Get-Date) -ge $limite) { try { $p.Kill() } catch { }; $depasse = $true; break }
        }
        if ($progression) { Write-Host '' }
        if (-not $depasse) { $p.WaitForExit() }
        if ($lectureSortie.Wait(5000)) { $texte = [string]$lectureSortie.Result }
        [void]$lectureErreur.Wait(5000)
    } catch {
        $r.Erreur = 'Impossible de lancer le contrôle local : ' + [string]$_.Exception.Message
        return $r
    } finally {
        if ($p) { try { $p.Dispose() } catch { } }
    }
    if ($depasse) { $r.Erreur = 'Le contrôle local n''a pas fini en 3 minutes.'; return $r }
    $ligne = [string](@($texte -split "`r?`n") | Where-Object { $_.TrimStart().StartsWith('{') } | Select-Object -Last 1)
    if (-not $ligne) { $r.Erreur = 'Le contrôle local n''a rien rendu de lisible.'; return $r }
    $etat = $null
    try { $etat = $ligne | ConvertFrom-Json } catch { $r.Erreur = 'Le contrôle local a rendu une réponse illisible.'; return $r }
    if (-not [bool](Get-ChampGestion $etat 'ok' $false)) { $r.Erreur = ConvertTo-TexteSur (Get-ChampGestion $etat 'erreur' 'Contrôle local impossible.'); return $r }
    $r.Ok = $true
    $r.Etat = $etat
    return $r
}

function Wait-AgentGestion {
    <# Attend que l'agent réponde : Get-EtatAgent toutes les 5 secondes, jusqu'au délai. Rend le dernier état lu. #>
    param([string]$Racine, [int]$DelaiSecondes = 60)
    $fin = (Get-Date).AddSeconds($DelaiSecondes)
    $etat = $null
    $progression = $false
    while ($true) {
        $tour = Get-Date
        $etat = Get-EtatAgent -Racine $Racine -DelaiSecondes 4
        if ($etat.Vivant -or $etat.NonSupporte -or (Get-Date) -ge $fin) { break }
        Write-Host '.' -NoNewline -ForegroundColor DarkGray; $progression = $true
        $reste = 5 - ((Get-Date) - $tour).TotalSeconds
        if ($reste -gt 0) { Start-Sleep -Milliseconds ([int]($reste * 1000)) }
    }
    if ($progression) { Write-Host '' }
    return $etat
}

function Get-AccesSite {
    <#
        Que dit le site de ce Worker, et peut-on lui demander une réparation ? Rend :
        Lu (l'état vient d'être lu sur le site), Sante (son bloc health), Reparable (il a des étapes sûres),
        Lourdes (nombre d'étapes lourdes), Maj (version proposée si elle est plus récente que celle de l'agent
        en marche, sinon ''), Possible (une réparation peut lui être demandée), Raison (pourquoi non, en quelques
        mots : elle ouvre la ligne « non tenté » du bilan).
    #>
    param($Agent, $Site)
    $r = [pscustomobject]@{ Lu = $false; Possible = $false; Raison = ''; Sante = $null; Reparable = $false; Lourdes = 0; Maj = '' }
    if (-not $Agent -or (-not $Agent.Vivant -and -not $Agent.NonSupporte)) { $r.Raison = 'Agent arrêté'; return $r }
    if (-not $Agent.Capable) {
        if ($Agent.ARedemarrer) { $r.Raison = 'Agent à redémarrer avec ' + (Get-NomEntree 10 -Nu) + ' (il est plus ancien que ses fichiers)' }
        else { $r.Raison = 'Agent trop ancien (mets le Worker à jour depuis le site)' }
        return $r
    }
    if ($null -eq $Site) { $r.Raison = 'État de ce Worker non lu sur le site'; return $r }
    if (-not $Site.Connu) {
        if ($Site.AgentMuet) { $r.Raison = 'Agent arrêté' }
        elseif ($Site.AgentARedemarrer) { $r.Raison = 'Agent à redémarrer avec ' + (Get-NomEntree 10 -Nu) + ' (il est plus ancien que ses fichiers)' }
        elseif ($Site.AgentTropAncien) { $r.Raison = 'Agent trop ancien pour le site (mets le Worker à jour)' }
        elseif ($Site.SiteSansFonction) { $r.Raison = 'Le site n''a pas encore cette fonction' }
        elseif ($Site.SiteInjoignable) { $r.Raison = 'Site injoignable' }
        else { $r.Raison = 'Le site n''a pas donné l''état de ce Worker' }
        return $r
    }
    # Lire reste permis à un compte suspendu ou pendant une maintenance du site : seules les demandes sont refusées.
    $r.Lu = $true
    $r.Sante = Get-ChampGestion $Site.Site 'health'
    $r.Reparable = ((Get-ChampGestion $r.Sante 'repairable' $false) -eq $true)
    $r.Lourdes = ConvertTo-EntierGestion (Get-ChampGestion $r.Sante 'heavy_repairs')
    $proposee = ([string](Get-ChampGestion (Get-ChampGestion $Site.Site 'worker') 'available_agent_version' '')).Trim()
    $ordre = Compare-VersionWorker $proposee ([string](Get-ChampGestion $Agent.Statut 'agent_version' ''))
    if ($null -ne $ordre -and $ordre -gt 0) { $r.Maj = $proposee }
    $compte = Get-ChampGestion $Site.Site 'account'
    if ((Get-ChampGestion $compte 'can_act' $true) -eq $false -or (Get-ChampGestion $compte 'maintenance' $false) -eq $true) {
        $r.Raison = 'Compte propriétaire suspendu ou site en maintenance'
        return $r
    }
    $r.Possible = $true
    return $r
}

function Add-Point {
    <#
        Un point du verdict. Classe : rapide | lourde | main. Cle : ce qui le répare ici (clé d'un outil local,
        « site », « site-lourd », « fichiers »), ou '' quand rien n'est lancé par cet outil pour ce point.
    #>
    param([string]$Code, [string]$Gravite, [string]$Texte, [string]$Consigne, [string]$Cle = '', [string]$Classe = 'main')
    [void]$script:Points.Add([pscustomobject]@{ Code = $Code; Gravite = $Gravite; Texte = $Texte; Consigne = $Consigne; Cle = $Cle; Classe = $Classe })
}

function Add-Bilan {
    <# Issue : fait | echec | refuse | simule. #>
    param([string]$Nom, [string]$Issue, [string]$Detail = '')
    [void]$script:Bilan.Add([pscustomobject]@{ Nom = $Nom; Issue = $Issue; Detail = $Detail })
}

function Get-NomCommande {
    <# Commande du plan de réparation du site, en clair. #>
    param([string]$Commande)
    switch ($Commande) {
        'agent.update'   { return 'mise à jour de l''agent' }
        'agent.restart'  { return 'redémarrage de l''agent' }
        'tool.start'     { return 'relance d''un moteur' }
        'tool.repair'    { return 'réparation d''un moteur' }
        'model.repair'   { return 'nouveau téléchargement d''un modèle' }
        'tool.uninstall' { return 'retrait d''un moteur avant sa réinstallation' }
        'tool.install'   { return 'réinstallation d''un moteur' }
    }
    return (ConvertTo-TexteSur $Commande 40)
}

function Add-PointsSite {
    <# Problèmes que le site voit sur ce Worker (bloc health lu à l'instant), un point chacun. #>
    param($Sante, [bool]$AvecLourdes)
    foreach ($pb in (Get-Liste (Get-ChampGestion $Sante 'problems'))) {
        $code = [string](Get-ChampGestion $pb 'code' '')
        if ($code -cnotmatch '^[a-z0-9_]{1,60}$' -or $code -eq 'offline') { continue }
        $gravite = 'alerte'
        if ([string](Get-ChampGestion $pb 'severity' '') -eq 'bloquant') { $gravite = 'erreur' }
        $titre = ConvertTo-TexteSur (Get-ChampGestion $pb 'title' '')
        if (-not $titre) { $titre = 'Problème signalé par le site.' }
        if ($script:CodesSiteSurs -contains $code) {
            $consigne = 'par le site : ' + (Get-NomCommande 'tool.start')
            if ($code -eq 'agent_outdated') { $consigne = 'par le site : ' + (Get-NomCommande 'agent.update') + ' (ou ' + (Get-NomEntree 23 -Nu) + ')' }
            elseif ($code -eq 'job_unconfirmed') { $consigne = 'par le site : ' + (Get-NomCommande 'agent.restart') }
            Add-Point ('S-' + $code) $gravite $titre $consigne 'site' 'rapide'
        }
        elseif ([string](Get-ChampGestion $pb 'weight' '') -eq 'heavy') {
            if ($AvecLourdes) { Add-Point ('S-' + $code) $gravite $titre 'par le site, sur confirmation (mot LOURD), quand il sait la faire ; sinon depuis Mes Workers > Installations' 'site-lourd' 'lourde' }
            else { Add-Point ('S-' + $code) $gravite $titre ((Get-NomEntree 18 -Nu) + ', ou Mes Workers > Installations') '' 'lourde' }
        }
        else { Add-Point ('S-' + $code) $gravite $titre 'aucune réparation automatique : regarde la carte de ce Worker dans Mes Workers' }
    }
}

function Add-PointsAgent {
    <#
        Ce que l'agent signale lui-même (codes de son indicateur, les mêmes que ceux du bandeau du menu).
        -SiteLu : les problèmes « S-… » viennent d'être lus sur le site, ceux de l'indicateur sont laissés.
        L'indicateur range ses erreurs en tête et en donne le nombre : un code sans gravité connue ici (problème
        du site, code d'une version plus récente de l'agent) est une erreur s'il est dans ce nombre.
    #>
    param($Statut, [bool]$SiteLu)
    $dejaVus = @($script:Points | ForEach-Object { [string]$_.Code })
    $indicateur = Get-ChampGestion $Statut 'indicator'
    $nombreErreurs = ConvertTo-EntierGestion (Get-ChampGestion $indicateur 'errors')
    $rang = -1
    foreach ($code in (Get-Liste (Get-ChampGestion $indicateur 'codes'))) {
        $rang++
        if ($code -isnot [string] -or $code -cnotmatch '^[AS]-[A-Za-z0-9_-]{1,62}$') { continue }
        if ($dejaVus -contains $code) { continue }
        $graviteRang = 'alerte'
        if ($rang -lt $nombreErreurs) { $graviteRang = 'erreur' }
        switch -Regex ($code) {
            '^A-FICHIERS-AGENT$' {
                if ($dejaVus -contains 'L-FICHIERS-AGENT') { break }
                if ($Rapide) { Add-Point $code 'erreur' 'Des fichiers de l''agent manquent ou ont changé.' ('réinstallation des fichiers de l''agent, proposée par ' + (Get-NomEntree 18 -Nu)) '' 'rapide' }
                else { Add-Point $code 'erreur' 'Des fichiers de l''agent manquent ou ont changé.' 'réinstaller les fichiers de l''agent par le site (l''agent redémarre tout seul)' 'fichiers' 'rapide' }
                break
            }
            '^A-SITE-INJOIGNABLE$' {
                if ($dejaVus -contains 'L-SITE-INJOIGNABLE') { break }
                Add-Point $code 'alerte' 'L''agent n''arrive plus à joindre le site depuis plusieurs minutes.' 'Aucun outil ne répare Internet : vérifie que le site s''ouvre dans un navigateur sur ce PC'
                break
            }
            '^A-LIAISON-REFUSEE$' {
                Add-Point $code 'erreur' 'Le site refuse les demandes de ce Worker depuis plusieurs essais de suite.' ('Vérifie l''heure du PC, puis ' + (Get-NomEntree 7 -Nu) + ' ; si sa fiche a été supprimée du site : ' + (Get-NomEntree 8 -Nu))
                break
            }
            '^A-INSTALLATION-INCERTAINE$' {
                Add-Point $code 'erreur' 'Une installation précédente est encore active, ou son arrêt n''est pas confirmé : l''agent bloque les nouveaux calculs et la maintenance en attendant.' ('Attends quelques minutes : l''agent lève le blocage quand l''installation est finie. Si rien ne change : ' + (Get-NomEntree 3 -Nu))
                break
            }
            '^A-MOTEUR-TOMBE$' {
                # Le site, quand il vient d'être lu, nomme lui-même le moteur tombé (S-engine_crashed) et sait le relancer.
                if ($dejaVus -notcontains 'S-engine_crashed') { Add-Point $code 'erreur' 'Un moteur est tombé et l''agent a épuisé ses relances automatiques.' ((Get-NomEntree 9 -Nu) + ' : arrêt puis départ du moteur ; ou le site (Mes Workers, « Réparer »)') }
                break
            }
            '^A-MAINTENANCE-ENV$' {
                Add-Point $code 'alerte' 'La dernière maintenance des programmes du Worker (Python isolé, pip) a échoué.' (Get-NomEntree 3 -Nu)
                break
            }
            '^S-' {
                if (-not $SiteLu) { Add-Point $code $graviteRang 'Problème vu par le site à sa dernière lecture par l''agent (le site n''a pas pu être relu à l''instant).' 'Regarde la carte de ce Worker dans Mes Workers' }
                break
            }
            default { Add-Point $code $graviteRang 'Problème signalé par l''agent, que cet outil ne sait pas encore expliquer.' ((Get-NomEntree 4 -Nu) + ', à joindre à ta demande d''aide') }
        }
    }
}

function Write-PointsVerdict {
    <# Liste numérotée : code, texte, réparation, classe. #>
    $n = 0
    foreach ($pt in @($script:Points)) {
        $n++
        Write-Host ''
        $ligne = [string]$n + '. [' + $pt.Code + '] ' + $pt.Texte
        if ($pt.Gravite -eq 'erreur') { Write-Erreur $ligne } else { Write-Alerte $ligne }
        switch ($pt.Classe) {
            'rapide' { Write-Conseil ('Réparation rapide : ' + $pt.Consigne) }
            'lourde' { Write-Conseil ('Réparation lourde : ' + $pt.Consigne) }
            default  { Write-Conseil ('À faire à la main : ' + $pt.Consigne) }
        }
    }
}

function Invoke-ReparationSite {
    <#
        Demande au site, par l'agent, de mettre ses réparations en file : les étapes sûres, ou avec -Lourd les
        étapes lourdes aussi. Dit ce que le site a mis en file. Rend : Issue (fait | rien | echec), Maj (une mise à
        jour de l'agent fait partie des étapes).
    #>
    param([string]$Racine, [switch]$Lourd)
    $retour = [pscustomobject]@{ Issue = 'echec'; Maj = $false }
    $r = Invoke-RelaisSite -Racine $Racine -Action 'site-repair' -Arguments @{ include_heavy = [bool]$Lourd } -DelaiSecondes 60
    if (-not $r.Ok) {
        Write-RefusRelais -Resultat $r
        return $retour
    }
    $enFile = Get-Liste (Get-ChampGestion $r.Resultat 'queued')
    $nonLancees = Get-Liste (Get-ChampGestion $r.Resultat 'errors')
    foreach ($c in $enFile) {
        $commande = [string](Get-ChampGestion $c 'command' '')
        if ($commande -eq 'agent.update') { $retour.Maj = $true }
        Write-Ok ('Demandé au site : ' + (Get-NomCommande $commande) + ' (' + (ConvertTo-TexteSur (Get-ChampGestion $c 'title' '')) + ').')
    }
    foreach ($e in $nonLancees) {
        Write-Alerte ('Non lancé : ' + (Get-NomCommande ([string](Get-ChampGestion $e 'command' ''))) + '. Raison donnée par le site : ' + (ConvertTo-TexteSur (Get-ChampGestion $e 'error' '')))
    }
    if ($enFile.Count -eq 0) {
        if ($nonLancees.Count -gt 0) { return $retour }
        Write-Info 'Le site n''avait aucune réparation à lancer.'
        $retour.Issue = 'rien'
        return $retour
    }
    Write-Info 'L''agent les exécute dans les minutes qui viennent : suis-les sur la carte de ce Worker, dans Mes Workers.'
    if ($retour.Maj) {
        Write-Info 'La mise à jour redémarre l''agent tout seul : il reste une à deux minutes hors ligne.'
        if ($env:ALPINE_WORKER_GUI -eq '1') { Write-Conseil 'Le panneau lui-même sera peut-être remplacé par cette mise à jour : ferme et rouvre worker.exe dans quelques minutes pour utiliser sa nouvelle version.' }
    }
    $retour.Issue = 'fait'
    return $retour
}

# ---------------------------------------------------------------------------
# Déroulement
# ---------------------------------------------------------------------------

try {
    $Racine = Get-RacineWorker -Racine $Racine

    if ($Rapide) {
        Write-Explication -Titre 'RÉPARER LE WORKER' -Fait @(
            'Contrôle le Worker, puis liste ce qui peut être réparé vite et sans risque.',
            'Te pose UNE question, puis lance : les outils du menu qui remettent le Worker en marche ou retirent une tâche planifiée en trop, et les réparations rapides proposées par le site (relancer un moteur, redémarrer ou mettre à jour l''agent).',
            'Finit par un bilan : ce qui est fait, ce qui a échoué, ce qui n''a pas pu être tenté.'
        ) -NeFaitPas @(
            'Aucune réparation lourde : ni réinstallation d''un moteur, ni nouveau téléchargement d''un modèle, ni réinstallation des fichiers de l''agent.',
            ('Pour celles-là et pour le contrôle détaillé : ' + (Get-NomEntree 18) + '.'),
            'Ne rallume jamais un Worker éteint volontairement.'
        ) -Exige @(
            'Rien pour contrôler. L''agent en marche et le site joignable pour les réparations faites par le site.',
            'Une réparation choisie peut ouvrir une fenêtre administrateur : Windows te demandera ton accord.'
        )
    } else {
        Write-Explication -Titre 'CONTRÔLE TOTAL ET RÉPARATION' -Fait @(
            'Contrôle tout : état local, fichiers de l''agent, droits du dossier, lanceur, Python et Git, moteurs, inscription et connexion au site, ce que le site voit, mise à jour.',
            'Donne un verdict : chaque problème avec son code, sa réparation, et si elle est rapide, lourde, ou à faire à la main.',
            'Propose ensuite chaque réparation, une question à la fois : un outil du menu, ou une demande au site.',
            'Finit par un bilan : ce qui est fait, ce qui a échoué, ce qui n''a pas pu être tenté.'
        ) -NeFaitPas @(
            'Ne répare rien sans ton accord. Les réparations lourdes exigent en plus le mot LOURD.',
            'Ne répare ni Python, ni Git, ni un agent qui ne démarre plus : il le dit, avec la marche à suivre.',
            'Ne rallume jamais un Worker éteint volontairement.'
        ) -Exige @(
            'Rien pour contrôler. L''agent en marche et le site joignable pour les réparations faites par le site.',
            'Une réparation choisie peut ouvrir une fenêtre administrateur : Windows te demandera ton accord.'
        )
    }
    if ($Simulation) { Write-Alerte 'MODE SIMULATION : rien ne sera lancé ni demandé.' }
    if ($SansReparation) { Write-Alerte 'Contrôle seul : aucune réparation ne sera proposée.' }

    # ------------------------------------------------------------ 1. contrôle
    Write-Section 'Contrôle en cours'
    Write-Info 'Contrôle local, le même que « État complet du Worker » (il peut prendre une minute)...'
    $controle = Get-ControleLocal -Racine $Racine
    if (-not $controle.Ok) {
        Write-Erreur ('Contrôle impossible : ' + $controle.Erreur)
        Write-Conseil ('Lance ' + (Get-NomEntree 1) + ' pour voir ce qui bloque.')
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    $local = $controle.Etat
    $agentLocal = Get-ChampGestion $local 'agent'
    # L'agent n'a pas répondu au contrôle local : inutile de l'attendre une seconde fois.
    $agent = [pscustomobject]@{ Vivant = $false; Statut = $null; NonSupporte = $false; Capable = $false; ARedemarrer = $false; Message = (Get-MessageRelais 'muet') }
    if ([bool](Get-ChampGestion $agentLocal 'statut_disponible' $false) -or [bool](Get-ChampGestion $agentLocal 'non_supporte' $false)) { $agent = Get-EtatAgent -Racine $Racine }
    $site = $null
    if ($agent.Vivant -and $agent.Capable) {
        Write-Info 'Lecture de l''état de ce Worker sur le site (20 secondes au plus)...'
        $site = Get-EtatSite -Racine $Racine -Rafraichir -DelaiSecondes 20
    }
    $acces = Get-AccesSite -Agent $agent -Site $site

    $identite = Get-ChampGestion $local 'identite'
    $problemesLocaux = Get-Liste (Get-ChampGestion $local 'problemes')
    $codesLocaux = @($problemesLocaux | ForEach-Object { [string](Get-ChampGestion $_ 'code' '') })
    $nonAssocie = ($codesLocaux -contains 'L-NON-ASSOCIE')
    # Éteint par « Éteindre le Worker » (11) : service en démarrage manuel, ou mémo d'extinction laissé par l'outil.
    $eteintVolontaire = (($codesLocaux -contains 'L-ETEINT-VOLONTAIRE') -or (Test-Path -LiteralPath (Join-Path $Racine 'runtime\local-control\extinction.json') -PathType Leaf))
    $versionEnMarche = ConvertTo-TexteSur (Get-ChampGestion $agent.Statut 'agent_version' '') 40
    $versionInstallee = ConvertTo-TexteSur (Get-ChampGestion $identite 'version' '') 40
    # Un moteur muet n'est relancé par le site que si le site le voit tombé lui aussi.
    $siteVoitMoteur = $false
    if ($acces.Lu) {
        foreach ($pb in (Get-Liste (Get-ChampGestion $acces.Sante 'problems'))) {
            if (@('engine_crashed', 'engine_restarting', 'engine_stopped') -contains [string](Get-ChampGestion $pb 'code' '')) { $siteVoitMoteur = $true }
        }
    }

    # ------------------------------------------------------------ 2. verdict : un point par problème
    foreach ($p in $problemesLocaux) {
        $code = [string](Get-ChampGestion $p 'code' '')
        if (-not $code) { $code = 'L-SANS-CODE' }
        $gravite = 'alerte'
        if ([string](Get-ChampGestion $p 'gravite' '') -eq 'erreur') { $gravite = 'erreur' }
        $texte = ConvertTo-TexteSur (Get-ChampGestion $p 'probleme' '') 400
        $consigneEtat = ConvertTo-TexteSur (Get-ChampGestion $p 'outil' '') 400
        $cle = [string]$script:CodesReparation[$code]
        $outil = $script:OutilsLocaux | Where-Object { $_.Cle -eq $cle } | Select-Object -First 1
        if ($outil) {
            $nomEntree = Get-NomEntree $outil.Entree -Nu
            if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $outil.Fichier) -PathType Leaf)) { Add-Point $code $gravite $texte $consigneEtat }
            elseif ($nonAssocie -and $outil.Demarre) { Add-Point $code $gravite $texte ('Associe d''abord le Worker à un compte : ' + (Get-NomEntree 8 -Nu)) }
            elseif ($eteintVolontaire -and @('rallumer', 'redemarrer', 'reactiver') -contains $cle) { Add-Point $code $gravite $texte ('Le Worker a été éteint volontairement : cet outil ne le rallume pas à ta place. Pour le rallumer : ' + (Get-NomEntree 12 -Nu)) }
            elseif ($Rapide -and $outil.Classe -ne 'R') { Add-Point $code $gravite $texte ($nomEntree + ' (' + $outil.Objet + '), proposé par ' + (Get-NomEntree 18 -Nu)) '' 'rapide' }
            else { Add-Point $code $gravite $texte ($nomEntree + ' : ' + $outil.Objet) $cle 'rapide' }
        }
        elseif ($cle -eq 'site') {
            if ($siteVoitMoteur) { Add-Point $code $gravite $texte ('par le site : ' + (Get-NomCommande 'tool.start')) 'site' 'rapide' }
            else { Add-Point $code $gravite $texte $consigneEtat }
        }
        elseif ($cle -eq 'fichiers') {
            if ($Rapide) { Add-Point $code $gravite $texte ('réinstallation des fichiers de l''agent, proposée par ' + (Get-NomEntree 18 -Nu)) '' 'rapide' }
            else { Add-Point $code $gravite $texte 'réinstaller les fichiers de l''agent par le site (l''agent redémarre tout seul) ; il faut l''agent en marche' 'fichiers' 'rapide' }
        }
        else { Add-Point $code $gravite $texte $consigneEtat }
    }
    if ($acces.Lu) { Add-PointsSite -Sante $acces.Sante -AvecLourdes (-not $Rapide) }
    if ($agent.Vivant -and $agent.Capable) { Add-PointsAgent -Statut $agent.Statut -SiteLu $acces.Lu }
    # Erreurs d'abord, puis ce qui est à surveiller ; l'ordre de chaque source est gardé.
    $tries = @(@($script:Points | Where-Object { $_.Gravite -eq 'erreur' }) + @($script:Points | Where-Object { $_.Gravite -ne 'erreur' }))
    $script:Points = New-Object System.Collections.ArrayList
    foreach ($pt in $tries) { [void]$script:Points.Add($pt) }
    $nbErreurs = @($script:Points | Where-Object { $_.Gravite -eq 'erreur' }).Count
    $nbAlertes = $script:Points.Count - $nbErreurs

    # Dernier état écrit par l'agent : utile seulement quand il ne répond pas (c'est ce que le bandeau du menu affiche alors).
    $dernierEtat = $null
    if (-not $agent.Vivant) { $dernierEtat = Read-FichierEtat -Racine $Racine }

    # ------------------------------------------------------------ sections (contrôle total seulement)
    if (-not $Rapide) {
        Write-Section 'État local'
        Write-Etat -Libelle 'Dossier du Worker' -Valeur $Racine
        if ($versionInstallee) { Write-Etat -Libelle 'Version installée' -Valeur $versionInstallee } else { Write-Etat -Libelle 'Version installée' -Valeur 'inconnue' -Niveau alerte }
        if ($agent.Vivant -and $agent.Capable) { Write-Etat -Libelle 'Agent' -Valeur ('répond (version ' + $versionEnMarche + ')') -Niveau ok }
        elseif ($agent.Vivant -or $agent.NonSupporte) { Write-Etat -Libelle 'Agent' -Valeur 'répond, mais il est trop ancien pour les actions de gestion (ou à redémarrer)' -Niveau alerte }
        elseif ($eteintVolontaire) { Write-Etat -Libelle 'Agent' -Valeur 'éteint volontairement' -Niveau alerte }
        else { Write-Etat -Libelle 'Agent' -Valeur 'ne répond pas' -Niveau erreur }
        if ($dernierEtat -and $dernierEtat.Present) {
            $resumeEtat = [string]$dernierEtat.Erreurs + ' erreur(s), ' + $dernierEtat.Alertes + ' alerte(s)'
            if (@($dernierEtat.Codes).Count -gt 0) { $resumeEtat += ' : ' + (@($dernierEtat.Codes) -join ', ') }
            Write-Etat -Libelle 'Son dernier état écrit' -Valeur ((Format-DateEtat -Date $dernierEtat.Date) + ' - ' + $resumeEtat)
        }
        if ($agent.Vivant -and $agent.Statut) {
            $nbCalculs = (Get-Liste (Get-ChampGestion $agent.Statut 'jobs_active')).Count
            if ($nbCalculs -gt 0) { Write-Etat -Libelle 'Calculs en cours' -Valeur ([string]$nbCalculs) -Niveau alerte } else { Write-Etat -Libelle 'Calculs en cours' -Valeur 'aucun' -Niveau ok }
            if ([bool](Get-ChampGestion $agent.Statut 'maintenance_active' $false)) { Write-Etat -Libelle 'Maintenance' -Valeur 'en cours' -Niveau alerte }
        }
        $disque = Get-ChampGestion $local 'disque'
        if ($null -ne (Get-ChampGestion $disque 'libre_gio')) {
            $niveauDisque = 'ok'
            if ($codesLocaux -contains 'L-DISQUE') { $niveauDisque = 'alerte' }
            Write-Etat -Libelle 'Disque' -Valeur ([string](Get-ChampGestion $disque 'libre_gio') + ' Gio libres sur ' + [string](Get-ChampGestion $disque 'total_gio') + ' (' + (ConvertTo-TexteSur (Get-ChampGestion $disque 'lecteur' '') 10) + ')') -Niveau $niveauDisque
        }
        $cartes = Get-Liste (Get-ChampGestion (Get-ChampGestion $local 'gpu') 'cartes')
        if ($cartes.Count -eq 0) { Write-Etat -Libelle 'Carte graphique' -Valeur 'non mesurée (pas de carte NVIDIA, ou nvidia-smi muet)' }
        foreach ($carte in $cartes) {
            $niveauCarte = 'ok'
            if ($codesLocaux -contains 'L-VRAM') { $niveauCarte = 'alerte' }
            Write-Etat -Libelle 'Carte graphique' -Valeur ((ConvertTo-TexteSur (Get-ChampGestion $carte 'nom' '') 60) + ' : ' + [string](Get-ChampGestion $carte 'memoire_utilisee_mio') + ' / ' + [string](Get-ChampGestion $carte 'memoire_totale_mio') + ' Mio') -Niveau $niveauCarte
        }
        $journauxLus = Get-Liste (Get-ChampGestion $local 'journaux')
        $lignesErreur = 0
        foreach ($j in $journauxLus) { $lignesErreur += ConvertTo-EntierGestion (Get-ChampGestion $j 'lignes_erreur') 100000 }
        $niveauJournaux = 'ok'
        if ($codesLocaux -contains 'L-JOURNAUX') { $niveauJournaux = 'alerte' }
        Write-Etat -Libelle 'Journaux' -Valeur ([string]$journauxLus.Count + ' lu(s), ' + $lignesErreur + ' ligne(s) d''erreur dans leurs 200 dernières lignes') -Niveau $niveauJournaux

        Write-Section 'Fichiers de l''agent'
        $fichiers = Get-ChampGestion $local 'fichiers_agent'
        $manquants = Get-Liste (Get-ChampGestion $fichiers 'manquants')
        $modifies = Get-Liste (Get-ChampGestion $fichiers 'modifies')
        if (-not [bool](Get-ChampGestion $fichiers 'connu' $false)) { Write-Etat -Libelle 'Contre agent_manifest.json' -Valeur 'non vérifiables (manifeste absent ou illisible)' }
        elseif (($manquants.Count + $modifies.Count) -eq 0) { Write-Etat -Libelle 'Contre agent_manifest.json' -Valeur ([string](Get-ChampGestion $fichiers 'total' 0) + ' fichiers contrôlés, tous conformes') -Niveau ok }
        else {
            Write-Etat -Libelle 'Contre agent_manifest.json' -Valeur ([string]$manquants.Count + ' manquant(s), ' + $modifies.Count + ' modifié(s), sur ' + [string](Get-ChampGestion $fichiers 'total' 0)) -Niveau erreur
            foreach ($f in @($manquants | Select-Object -First 5)) { Write-Info ('      manquant : ' + (ConvertTo-TexteSur $f 120)) }
            foreach ($f in @($modifies | Select-Object -First 5)) { Write-Info ('      modifié  : ' + (ConvertTo-TexteSur $f 120)) }
        }

        Write-Section 'Droits du dossier'
        $droits = Get-ChampGestion $local 'droits_dossier'
        if (-not [bool](Get-ChampGestion $droits 'connu' $false)) { Write-Etat -Libelle 'Écriture par d''autres comptes' -Valeur 'non vérifiable' }
        elseif ([bool](Get-ChampGestion $droits 'ouvert' $false)) {
            $comptes = Get-Liste (Get-ChampGestion $droits 'comptes')
            Write-Etat -Libelle 'Écriture par d''autres comptes' -Valeur ('OUI : ' + (@($comptes | ForEach-Object { ConvertTo-TexteSur $_ 80 }) -join ', ')) -Niveau alerte
        }
        else { Write-Etat -Libelle 'Écriture par d''autres comptes' -Valeur 'non : le dossier leur est fermé' -Niveau ok }

        Write-Section 'Lanceur'
        $lancement = Get-ChampGestion $local 'lancement'
        $service = Get-ChampGestion $lancement 'Service'
        switch ([string](Get-ChampGestion $lancement 'Mode' '')) {
            'service' { Write-Etat -Libelle 'Mode' -Valeur ('service Windows : ' + [string](Get-ChampGestion $service 'Etat' '') + ', démarrage ' + [string](Get-ChampGestion $service 'Demarrage' '')) -Niveau $(if (@('L-SERVICE-ARRETE', 'L-SERVICE-PAS-AUTO', 'L-ETEINT-VOLONTAIRE') | Where-Object { $codesLocaux -contains $_ }) { 'alerte' } else { 'ok' }) }
            'tache'   { Write-Etat -Libelle 'Mode' -Valeur 'tâche planifiée (démarre à l''ouverture de session)' -Niveau ok }
            'session' { Write-Etat -Libelle 'Mode' -Valeur 'entrée de démarrage de session (registre, sans droits administrateur)' -Niveau ok }
            'aucun'   { Write-Etat -Libelle 'Mode' -Valeur 'lancement manuel : rien ne démarre le Worker tout seul' -Niveau alerte }
            default   { Write-Etat -Libelle 'Mode' -Valeur 'illisible sur ce PC' -Niveau alerte }
        }
        $taches = Get-Liste (Get-ChampGestion $lancement 'Taches')
        $orphelines = @($taches | Where-Object { [bool](Get-ChampGestion $_ 'Orpheline' $false) }).Count
        $niveauTaches = 'ok'
        if (@('L-TACHE-ORPHELINE', 'L-TACHE-DESACTIVEE', 'L-DOUBLE-LANCEUR') | Where-Object { $codesLocaux -contains $_ }) { $niveauTaches = 'alerte' }
        $texteTaches = [string]$taches.Count + ' tâche(s) planifiée(s) du Worker sur ce PC'
        if ($orphelines -gt 0) { $texteTaches += ', dont ' + $orphelines + ' orpheline(s)' }
        if ($codesLocaux -contains 'L-DOUBLE-LANCEUR') { $texteTaches += ' ; DOUBLE LANCEUR : le service et une tâche lancent ce même dossier' }
        Write-Etat -Libelle 'Tâches planifiées' -Valeur $texteTaches -Niveau $niveauTaches

        Write-Section 'Environnement'
        if ([bool](Get-ChampGestion $identite 'python_path_valide' $false)) { Write-Etat -Libelle 'Python' -Valeur 'python_path.txt valide' -Niveau ok }
        elseif ([string](Get-ChampGestion $identite 'python' '')) { Write-Etat -Libelle 'Python' -Valeur 'python_path.txt absent ou invalide : un Python de secours est utilisé' -Niveau alerte }
        else { Write-Etat -Libelle 'Python' -Valeur 'introuvable : l''agent ne peut pas démarrer' -Niveau erreur }
        switch ([string](Get-ChampGestion $identite 'git_path' '')) {
            'valide'   { Write-Etat -Libelle 'Git' -Valeur 'git_path.txt valide' -Niveau ok }
            'invalide' { Write-Etat -Libelle 'Git' -Valeur 'git_path.txt désigne un fichier qui n''existe plus' -Niveau alerte }
            default    { Write-Etat -Libelle 'Git' -Valeur 'git_path.txt absent (utile seulement pour installer des moteurs)' }
        }

        Write-Section 'Moteurs'
        $moteurs = Get-Liste (Get-ChampGestion $local 'moteurs')
        if ($moteurs.Count -eq 0) { Write-Etat -Libelle 'Moteurs' -Valeur 'aucun moteur lu' }
        foreach ($m in $moteurs) {
            $libelleMoteur = (ConvertTo-TexteSur (Get-ChampGestion $m 'nom' '') 26)
            if ([bool](Get-ChampGestion $m 'en_ecoute' $false)) { Write-Etat -Libelle $libelleMoteur -Valeur ('en marche (port ' + [string](Get-ChampGestion $m 'port') + ')') -Niveau ok }
            elseif ([bool](Get-ChampGestion $m 'fiche_presente' $false)) { Write-Etat -Libelle $libelleMoteur -Valeur ('port ' + [string](Get-ChampGestion $m 'port') + ' fermé alors qu''il est noté comme lancé : tombé, ou en démarrage') -Niveau alerte }
            else { Write-Etat -Libelle $libelleMoteur -Valeur 'arrêté' }
        }

        Write-Section 'Inscription et connexion au site'
        if (-not [bool](Get-ChampGestion $identite 'config_presente' $false)) { Write-Etat -Libelle 'Association' -Valeur 'aucune configuration : Worker jamais associé' -Niveau erreur }
        elseif (-not [bool](Get-ChampGestion $identite 'associe' $false)) { Write-Etat -Libelle 'Association' -Valeur 'non associé à un compte' -Niveau erreur }
        else { Write-Etat -Libelle 'Association' -Valeur ('associé (identifiant ' + (ConvertTo-TexteSur (Get-ChampGestion $identite 'worker_id_court' '') 8) + '...)') -Niveau ok }
        $marqueurs = Get-ChampGestion $local 'marqueurs'
        if ([bool](Get-ChampGestion $marqueurs 'detache' $false)) { Write-Etat -Libelle 'Fiche sur le site' -Valeur 'SUPPRIMÉE du site : il ne se reconnectera pas tout seul' -Niveau erreur }
        if ([bool](Get-ChampGestion $marqueurs 'deconnecte' $false)) { Write-Etat -Libelle 'Déconnexion' -Valeur 'demandée depuis le site : il reste hors ligne' -Niveau alerte }
        $reseau = Get-ChampGestion $local 'reseau'
        if (-not [bool](Get-ChampGestion $reseau 'teste' $false)) { Write-Etat -Libelle 'Serveur' -Valeur 'non testé : son adresse n''est pas connue de ce Worker' }
        elseif (-not [bool](Get-ChampGestion $reseau 'joignable' $false)) { Write-Etat -Libelle 'Serveur' -Valeur 'INJOIGNABLE depuis ce PC' -Niveau erreur }
        elseif ((ConvertTo-EntierGestion (Get-ChampGestion $reseau 'code_http')) -ne 200) { Write-Etat -Libelle 'Serveur' -Valeur ('répond, mais avec le code ' + (ConvertTo-EntierGestion (Get-ChampGestion $reseau 'code_http'))) -Niveau alerte }
        else { Write-Etat -Libelle 'Serveur' -Valeur 'joignable depuis ce PC' -Niveau ok }
        $ecart = Get-ChampGestion $reseau 'ecart_horloge_secondes'
        if ($null -ne $ecart) {
            if ($codesLocaux -contains 'L-HORLOGE') { Write-Etat -Libelle 'Horloge de ce PC' -Valeur ('DÉCALÉE de ' + [string]$ecart + ' s : les demandes signées seront refusées') -Niveau erreur }
            else { Write-Etat -Libelle 'Horloge de ce PC' -Valeur ('à l''heure (écart ' + [string]$ecart + ' s)') -Niveau ok }
        }
        $liaison = Get-ChampGestion $agent.Statut 'heartbeat'
        if ($null -ne $liaison) {
            $echecs = ConvertTo-EntierGestion (Get-ChampGestion $liaison 'failures')
            if ((Get-ChampGestion $liaison 'ok') -eq $true) { Write-Etat -Libelle 'Liaison de l''agent' -Valeur 'son dernier contact avec le site a réussi' -Niveau ok }
            elseif ((Get-ChampGestion $liaison 'ok') -eq $false) { Write-Etat -Libelle 'Liaison de l''agent' -Valeur ([string]$echecs + ' contact(s) de suite en échec') -Niveau alerte }
            else { Write-Etat -Libelle 'Liaison de l''agent' -Valeur 'pas encore de contact (il vient de démarrer)' }
        }

        Write-Section 'Ce que le site voit'
        if ($acces.Lu) {
            $vus = @($script:Points | Where-Object { ([string]$_.Code).StartsWith('S-') })
            if ($vus.Count -eq 0) { Write-Etat -Libelle 'Problèmes' -Valeur 'aucun' -Niveau ok }
            foreach ($pt in $vus) {
                $niveauVu = 'alerte'
                if ($pt.Gravite -eq 'erreur') { $niveauVu = 'erreur' }
                Write-Etat -Libelle $pt.Code -Valeur $pt.Texte -Niveau $niveauVu
            }
            if (-not $acces.Possible) { Write-Etat -Libelle 'Demandes au site' -Valeur 'refusées pour l''instant : compte propriétaire suspendu, ou site en maintenance' -Niveau alerte }
        }
        elseif ($site -and -not $site.Connu) { Write-RefusRelais -Resultat $site }
        elseif ($agent.Vivant -or $agent.NonSupporte) { Write-RefusRelais -Resultat $agent }
        else { Write-Etat -Libelle 'Problèmes' -Valeur 'non lus : l''agent ne répond pas, lui seul peut interroger le site' -Niveau alerte }

        Write-Section 'Mise à jour'
        if ($versionEnMarche) { Write-Etat -Libelle 'Version en marche' -Valeur $versionEnMarche } else { Write-Etat -Libelle 'Version installée' -Valeur $versionInstallee }
        if ($acces.Lu) {
            if ($acces.Maj) { Write-Etat -Libelle 'Version proposée' -Valeur ($acces.Maj + ' : plus récente') -Niveau alerte }
            else { Write-Etat -Libelle 'Version proposée' -Valeur 'aucune plus récente : le Worker est à jour' -Niveau ok }
        }
        elseif ($dernierEtat -and $dernierEtat.Present -and $dernierEtat.Maj -and (Compare-VersionWorker $dernierEtat.Maj $versionInstallee) -eq 1) { Write-Etat -Libelle 'Version proposée' -Valeur ($dernierEtat.Maj + ' (d''après le dernier état écrit par l''agent)') -Niveau alerte }
        else { Write-Etat -Libelle 'Version proposée' -Valeur 'non lue (il faut l''agent en marche et le site joignable)' }
    }

    # ------------------------------------------------------------ verdict
    if ($script:Points.Count -eq 0) {
        Write-Cadre -Titre 'VERDICT : tout va bien' -Lignes @('Aucun problème détecté par ce contrôle.') -Couleur Green
    } else {
        $couleur = 'Yellow'
        if ($nbErreurs -gt 0) { $couleur = 'Red' }
        Write-Cadre -Titre ('VERDICT : ' + $script:Points.Count + ' point(s) à traiter') -Lignes @(([string]$nbErreurs + ' bloquant(s), ' + $nbAlertes + ' à surveiller.')) -Couleur $couleur
        Write-PointsVerdict
    }
    if (-not $acces.Lu -and $acces.Raison) {
        Write-Host ''
        Write-Alerte ($acces.Raison + ' : ce que le site voit de ce Worker n''a pas pu être lu.')
    }

    # ------------------------------------------------------------ 3. ce qui peut être lancé
    $plan = @()
    foreach ($outil in $script:OutilsLocaux) {
        $codesOutil = @($script:Points | Where-Object { $_.Cle -eq $outil.Cle } | ForEach-Object { [string]$_.Code } | Select-Object -Unique)
        # Nom : l'entrée du menu. Titre : l'entrée et ce qu'elle va faire (une même entrée sert à plusieurs réparations).
        if ($codesOutil.Count -gt 0) { $plan += [pscustomobject]@{ Outil = $outil; Codes = $codesOutil; Nom = (Get-NomEntree $outil.Entree); Titre = ((Get-NomEntree $outil.Entree) + ' : ' + $outil.Objet) } }
    }
    $clesFaites = @()   # ce qui a réellement été réparé, ou demandé au site, pendant ce passage
    $planDemarre = (@($plan | Where-Object { $_.Outil.Demarre }).Count -gt 0)
    $siteRapide = ($acces.Possible -and $acces.Reparable)
    $pointFichiers = (@($script:Points | Where-Object { $_.Cle -eq 'fichiers' }).Count -gt 0)
    $nomSite = 'Réparations rapides par le site'
    $nomLourd = 'Réparations lourdes par le site'
    $nomFichiers = 'Réinstallation des fichiers de l''agent'

    $faireReparations = (-not $SansReparation)
    $accordGlobal = $false

    # « Réparer » : UNE question, qui liste tout ce qui va être lancé, posée avant tout lancement et toute élévation.
    if ($faireReparations -and $Rapide -and ($plan.Count -gt 0 -or $siteRapide)) {
        Write-Section 'Ce que « Réparer » va lancer'
        $n = 0
        foreach ($etape in $plan) {
            $n++
            $suffixe = ''
            if ($etape.Outil.Admin) { $suffixe = ' Windows peut demander ton accord (fenêtre administrateur).' }
            Write-Info ([string]$n + '. ' + $etape.Titre + '.' + $suffixe)
        }
        if ($siteRapide) {
            $n++
            Write-Info ([string]$n + '. Par le site : ses réparations rapides (relancer un moteur, redémarrer ou mettre à jour l''agent).')
            if ($acces.Maj) { Write-Info ('      La mise à jour de l''agent vers ' + $acces.Maj + ' en fait partie : il redémarrera tout seul (une à deux minutes hors ligne).') }
        }
        elseif ($planDemarre) {
            $n++
            Write-Info ([string]$n + '. Puis, si l''agent répond : les réparations rapides que le site propose (relancer un moteur, redémarrer ou mettre à jour l''agent).')
        }
        if (-not $Simulation) {
            Write-Host ''
            if (Confirm-ActionVisible -Question 'Lancer ces réparations ?' -Oui:$Oui) { $accordGlobal = $true }
            else {
                foreach ($etape in $plan) { Add-Bilan $etape.Titre 'refuse' }
                if ($siteRapide) { Add-Bilan $nomSite 'refuse' }
                $faireReparations = $false
                Write-Info 'Annulé : rien n''a été lancé.'
            }
        }
    }

    # ------------------------------------------------------------ 3. réparations par les outils du menu
    $agentRelance = $false     # un outil lancé a pu (re)démarrer le Worker
    $rallumageTente = $false
    if ($faireReparations -and $plan.Count -gt 0) {
        if (-not $Rapide) { Write-Section 'Réparations par les outils du menu' }
        foreach ($etape in $plan) {
            $outil = $etape.Outil
            # L'état a pu changer depuis le contrôle : on ne relance pas ce qu'un outil précédent a déjà réglé.
            if ($outil.Cle -eq 'reconnecter' -and -not (Test-Path -LiteralPath (Join-Path $Racine '.worker-disconnected'))) {
                Write-Info ($etape.Nom + ' : inutile, la déconnexion a déjà été levée.')
                $clesFaites += $outil.Cle
                continue
            }
            if ($outil.Cle -eq 'redemarrer' -and $agentRelance) {
                Write-Info ($etape.Nom + ' : inutile, l''agent vient d''être relancé.')
                $clesFaites += $outil.Cle
                continue
            }
            if ($Simulation) {
                Write-Info ('[simulation] serait lancé, après ton accord : ' + $etape.Titre + '.')
                Add-Bilan $etape.Titre 'simule'
                continue
            }
            if (-not $accordGlobal) {
                Write-Host ''
                Write-Info ($etape.Titre + '   [' + ($etape.Codes -join ', ') + ']')
                if ($outil.Admin) { Write-Info 'Windows peut demander ton accord (fenêtre administrateur).' }
                if (-not (Confirm-ActionVisible -Question ('Lancer ' + $etape.Nom + ' ?') -Oui:$Oui)) {
                    Add-Bilan $etape.Titre 'refuse'
                    continue
                }
            }
            Write-Section ('Réparation : ' + (Get-NomEntree $outil.Entree -Nu))
            # Sous worker.exe, « Interrompre » arrête l'outil et tout ce qu'il a lancé : tant que l'outil appelé n'a pas fini,
            # le Worker qu'il vient de démarrer en fait partie.
            if ($outil.Demarre -and $env:ALPINE_WORKER_GUI -eq '1') { Write-Info 'Laisse-le aller au bout : l''interrompre (ou fermer ce panneau) pendant qu''il remet le Worker en marche peut arrêter le Worker avec lui.' }
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('lancement de ' + $outil.Fichier + ' ' + ($outil.Arguments -join ' ') + ' pour ' + ($etape.Codes -join ','))
            $codeOutil = Invoke-OutilEnfant -Script (Join-Path $PSScriptRoot $outil.Fichier) -Arguments (@('-Racine', $Racine) + $outil.Arguments)
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ($outil.Fichier + ' terminé, code ' + $codeOutil)
            Write-Host ''
            if ($outil.Demarre) { $agentRelance = $true }
            if ($outil.Cle -eq 'rallumer') { $rallumageTente = $true }
            switch ($codeOutil) {
                0 { Write-Ok ($etape.Nom + ' : terminé.'); Add-Bilan $etape.Titre 'fait'; $clesFaites += $outil.Cle }
                2 { Write-Alerte ($etape.Nom + ' a refusé d''agir, ou a été annulé : lis son message ci-dessus.'); Add-Bilan $etape.Titre 'refuse' 'refusé par l''outil lui-même' }
                3 { Write-Alerte ($etape.Nom + ' : autorisation administrateur refusée, rien n''a été fait.'); Add-Bilan $etape.Titre 'refuse' 'autorisation administrateur refusée' }
                default { Write-Erreur ($etape.Nom + ' s''est terminé en erreur (code ' + $codeOutil + ') : lis ses messages ci-dessus.'); Add-Bilan $etape.Titre 'echec' ('code ' + $codeOutil) }
            }
        }
    }

    # Un outil a pu (re)démarrer le Worker : on attend l'agent, puis on relit ce que le site voit et propose.
    if ($agentRelance) {
        Write-Host ''
        Write-Info 'Attente de l''agent (60 secondes au plus) avant de passer aux réparations faites par le site...'
        $agent = Wait-AgentGestion -Racine $Racine -DelaiSecondes 60
        $versionEnMarche = ConvertTo-TexteSur (Get-ChampGestion $agent.Statut 'agent_version' '') 40
        $site = $null
        if ($agent.Vivant -and $agent.Capable) {
            Write-Info 'Lecture de l''état de ce Worker sur le site (20 secondes au plus)...'
            $site = Get-EtatSite -Racine $Racine -Rafraichir -DelaiSecondes 20
        }
        $acces = Get-AccesSite -Agent $agent -Site $site
        $siteRapide = ($acces.Possible -and $acces.Reparable)
        if ($acces.Lu) {
            $vusMaintenant = Get-Liste (Get-ChampGestion $acces.Sante 'problems')
            Write-Info ('Le site voit maintenant ' + $vusMaintenant.Count + ' problème(s) sur ce Worker.')
            foreach ($pb in $vusMaintenant) { Write-Info ('      ' + (ConvertTo-TexteSur (Get-ChampGestion $pb 'title' ''))) }
        }
    }

    # ------------------------------------------------------------ 4. réparations par le site
    $majDemandee = $false
    $siteEnPanne = $false
    if ($faireReparations -and $siteRapide) {
        Write-Section $nomSite
        if ($Simulation) {
            Write-Info '[simulation] seraient demandées au site, après ton accord : ses réparations rapides (relancer un moteur, redémarrer ou mettre à jour l''agent).'
            Add-Bilan $nomSite 'simule'
        } else {
            $accordSite = $accordGlobal
            if (-not $accordSite) {
                Write-Info 'Le site peut relancer un moteur tombé, redémarrer l''agent ou le mettre à jour. Il ne réinstalle rien à cette étape.'
                if ($acces.Maj) { Write-Info ('La mise à jour de l''agent vers ' + $acces.Maj + ' en fait partie : il redémarrera tout seul (une à deux minutes hors ligne).') }
                $accordSite = Confirm-ActionVisible -Question 'Demander au site ses réparations rapides ?' -Oui:$Oui
            }
            if (-not $accordSite) { Add-Bilan $nomSite 'refuse' }
            else {
                Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'réparations rapides demandées au site'
                $retourSite = Invoke-ReparationSite -Racine $Racine
                Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('réparations rapides : ' + $retourSite.Issue)
                $majDemandee = [bool]$retourSite.Maj
                switch ($retourSite.Issue) {
                    'fait'  { Add-Bilan $nomSite 'fait' 'demandées : l''agent les exécute dans les minutes qui viennent'; $clesFaites += 'site' }
                    'rien'  { }
                    default { Add-Bilan $nomSite 'echec'; $siteEnPanne = $true }
                }
            }
        }
    }

    if ($faireReparations -and -not $Rapide -and $acces.Possible -and $acces.Lourdes -gt 0 -and -not $siteEnPanne) {
        Write-Section $nomLourd
        Write-Alerte ([string]$acces.Lourdes + ' réparation(s) lourde(s) proposée(s) par le site : réinstallation d''un moteur, ou nouveau téléchargement d''un modèle.')
        Write-Info 'Plusieurs gigaoctets peuvent être téléchargés de nouveau, et le moteur concerné reste arrêté pendant l''opération.'
        Write-Info 'Le compte propriétaire reçoit une notification sur le site.'
        if ($Simulation) {
            Write-Info '[simulation] seraient demandées au site seulement après le mot LOURD.'
            Add-Bilan $nomLourd 'simule'
        }
        # Jamais levée par -Oui : le mot exact doit être tapé.
        elseif (Confirm-ActionVisible -Question 'Lancer les réparations lourdes ?' -MotExact 'LOURD') {
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'réparations LOURDES demandées au site (mot LOURD tapé)'
            $retourLourd = Invoke-ReparationSite -Racine $Racine -Lourd
            Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('réparations lourdes : ' + $retourLourd.Issue)
            if ($retourLourd.Maj) { $majDemandee = $true }
            switch ($retourLourd.Issue) {
                'fait'  { Add-Bilan $nomLourd 'fait' 'demandées : l''agent les exécute, cela peut durer longtemps'; $clesFaites += 'site-lourd' }
                'rien'  { }
                default { Add-Bilan $nomLourd 'echec' }
            }
        } else {
            Write-Info 'Mot LOURD non tapé : aucune réparation lourde n''a été demandée.'
            Add-Bilan $nomLourd 'refuse'
        }
    }

    # ------------------------------------------------------------ 5. fichiers de l'agent
    $fichiersNonTraites = ''
    if ($pointFichiers -and -not $Rapide) {
        $outilMaj = Join-Path $PSScriptRoot 'mettre_a_jour.ps1'
        if (-not ($agent.Vivant -and $agent.Capable)) {
            # Toute réinstallation passe par l'agent vivant.
            if ($rallumageTente) { $fichiersNonTraites = 'Des fichiers de l''agent manquent ou ont changé et l''agent ne démarre pas : réinstalle le Worker par-dessus avec l''installateur téléchargé depuis Mes Workers (l''association et les moteurs sont conservés).' }
            else { $fichiersNonTraites = 'Des fichiers de l''agent manquent ou ont changé, et l''agent ne répond pas : rallume-le avec ' + (Get-NomEntree 12) + ', puis relance cet outil. S''il ne démarre plus : réinstalle le Worker par-dessus avec l''installateur téléchargé depuis Mes Workers (l''association et les moteurs sont conservés).' }
        }
        elseif (-not $faireReparations) { }
        elseif ($majDemandee) {
            Write-Host ''
            Write-Info 'Fichiers de l''agent : la mise à jour demandée au site les remplace tous, rien de plus à lancer.'
            $clesFaites += 'fichiers'
        }
        elseif (-not (Test-Path -LiteralPath $outilMaj -PathType Leaf)) { $fichiersNonTraites = 'Des fichiers de l''agent manquent ou ont changé, et mettre_a_jour.ps1 est absent de ce Worker : réinstalle-les depuis le site (Mes Workers > ce Worker > « Mettre à jour »).' }
        else {
            Write-Section $nomFichiers
            $questionFichiers = 'Réinstaller les fichiers de l''agent (version ' + $versionEnMarche + ') ?'
            if ($acces.Maj) { $questionFichiers = 'Mettre à jour l''agent vers ' + $acces.Maj + ' (tous ses fichiers sont remplacés) ?' }
            Write-Info 'Le site renvoie tous les fichiers de l''agent, qui redémarre ensuite tout seul (une à deux minutes hors ligne).'
            if ($Simulation) {
                Write-Info ('[simulation] serait lancé, après ton accord : ' + (Get-NomEntree 23) + '.')
                Add-Bilan $nomFichiers 'simule'
            }
            elseif (Confirm-ActionVisible -Question $questionFichiers -Oui:$Oui) {
                Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'lancement de mettre_a_jour.ps1 -Reinstaller (fichiers de l''agent)'
                $codeMaj = Invoke-OutilEnfant -Script $outilMaj -Arguments @('-Racine', $Racine, '-Reinstaller', '-Oui', '-SansPause')
                Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('mettre_a_jour.ps1 terminé, code ' + $codeMaj)
                Write-Host ''
                switch ($codeMaj) {
                    0 { Add-Bilan $nomFichiers 'fait'; $clesFaites += 'fichiers' }
                    2 { Add-Bilan $nomFichiers 'refuse' 'refusée : lis le message ci-dessus' }
                    default { Add-Bilan $nomFichiers 'echec' ('code ' + $codeMaj) }
                }
            }
            else { Add-Bilan $nomFichiers 'refuse' }
        }
    }

    # ------------------------------------------------------------ 6. bilan
    Write-Section 'Bilan'
    $faits = @($script:Bilan | Where-Object { $_.Issue -eq 'fait' })
    $echecs = @($script:Bilan | Where-Object { $_.Issue -eq 'echec' })
    $refuses = @($script:Bilan | Where-Object { $_.Issue -eq 'refuse' })
    foreach ($b in $faits) {
        $texteFait = 'Fait : ' + $b.Nom
        if ($b.Detail) { $texteFait += ' (' + $b.Detail + ')' }
        Write-Ok ($texteFait + '.')
    }
    foreach ($b in $echecs) {
        $texteEchec = 'Échec : ' + $b.Nom
        if ($b.Detail) { $texteEchec += ' (' + $b.Detail + ')' }
        Write-Erreur ($texteEchec + '.')
    }
    foreach ($b in $refuses) {
        $texteRefus = 'Non lancé : ' + $b.Nom
        if ($b.Detail) { $texteRefus += ' (' + $b.Detail + ')' } else { $texteRefus += ' (refusé)' }
        Write-Alerte ($texteRefus + '.')
    }
    if ($script:Points.Count -eq 0) { Write-Ok 'Rien à réparer.' }
    if ($SansReparation) { Write-Info 'Contrôle seul : rien n''a été lancé.' }
    if ($Simulation) { Write-Info 'Simulation : rien n''a été lancé ni demandé.' }
    # Nommément, ce qui n'a pas pu être fait.
    if (-not $acces.Possible -and $acces.Raison) { Write-Alerte ($acces.Raison + ' : réparations par le site non tentées.') }
    if ($fichiersNonTraites) { Write-Alerte $fichiersNonTraites }
    if (-not $agent.Vivant -and $dernierEtat -and $dernierEtat.Present -and @($dernierEtat.Codes).Count -gt 0) {
        Write-Alerte ('Les constats de l''agent avant son arrêt (' + (@($dernierEtat.Codes) -join ', ') + ', écrits le ' + (Format-DateEtat -Date $dernierEtat.Date) + ') n''ont pas pu être revus : rallume-le, puis relance cet outil.')
    }
    # Chaque point que ce passage n'a pas réparé, avec sa consigne.
    $restants = @($script:Points | Where-Object { -not $_.Cle -or ($clesFaites -notcontains $_.Cle) })
    if ($restants.Count -gt 0) {
        Write-Host ''
        Write-Info 'Reste à faire :'
        foreach ($pt in $restants) { Write-Conseil ('[' + $pt.Code + '] ' + $pt.Consigne) }
    }
    if ($Rapide) {
        Write-Host ''
        Write-Conseil ('Pour un contrôle complet et les réparations lourdes : ' + (Get-NomEntree 18))
    }

    $codeFinal = 0
    if ($echecs.Count -gt 0) { $codeFinal = 1 }
    elseif ($faits.Count -eq 0 -and $refuses.Count -gt 0) { $codeFinal = 2 }
    # Un contrôle qui n'a rien lancé ne laisse aucune trace dans le dossier du Worker.
    if (($faits.Count + $echecs.Count + $refuses.Count) -gt 0) {
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('fin : ' + $script:Points.Count + ' point(s), ' + $faits.Count + ' fait(s), ' + $echecs.Count + ' échec(s), ' + $refuses.Count + ' non lancé(s), code ' + $codeFinal)
    }
    Exit-Outil -Code $codeFinal -SansPause:$SansPause
} catch {
    Write-Erreur ('Erreur inattendue : ' + [string]$_.Exception.Message)
    Exit-Outil -Code 1 -SansPause:$SansPause
}
