<#
    Jobs qui bloquent l'arrêt des moteurs, la maintenance ou la mise à jour.
    LECTURE SEULE : le journal durable n'est jamais modifié (ouvert en mode=ro).

    Usage :
        jobs_bloquants.ps1                 liste expliquée
        jobs_bloquants.ps1 -SansPause      idem, sans attendre la touche Entrée
        jobs_bloquants.ps1 -Json           sortie JSON brute de jobs_bloquants.py

    Codes de sortie : 0 = lecture faite, 1 = lecture impossible.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause,
    [switch]$Json
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'diagnostic_commun.ps1')
if ($Json) { function Write-Host { param([Parameter(ValueFromRemainingArguments = $true)]$Reste, $ForegroundColor, [switch]$NoNewline) } }

Initialize-ConsoleWorker -Titre 'Jobs bloquants du Worker'
try { $Racine = Get-RacineWorker -Racine $Racine } catch {
    if ($Json) { [Console]::Out.WriteLine('{"ok": false, "error": "racine_introuvable"}') } else { Write-Erreur ([string]$_.Exception.Message); Wait-FinOutil -SansPause:$SansPause }
    exit 1
}

Write-Explication -Titre 'Jobs bloquants' -Fait @(
    'Liste les travaux (générations, conversions) que le Worker considère encore',
    'comme « en cours » : tant qu''il en reste, l''agent refuse d''arrêter les moteurs,',
    'de se mettre à jour ou d''entrer en maintenance.',
    'Indique pour chacun quoi faire, ou qu''il n''y a rien à faire.'
) -NeFaitPas @(
    'Ne modifie JAMAIS le journal du Worker (ouvert en lecture seule).',
    'N''annule aucun job : l''annulation se fait depuis le site, page Mes Workers.'
) -Exige @(
    'Le Python du Worker (python_path.txt). Aucune fenêtre administrateur.'
)

$python = Get-PythonWorker -Racine $Racine
if (-not $python) {
    if ($Json) { [Console]::Out.WriteLine('{"ok": false, "error": "python_introuvable"}') }
    else { Write-Erreur 'Python du Worker introuvable (python_path.txt absent ou invalide).'; Write-Conseil 'Relance install_windows.bat (dans le dossier du Worker) : il retrouve Python et réécrit python_path.txt. Puis reviens ici.'; Wait-FinOutil -SansPause:$SansPause }
    exit 1
}

$script = Join-Path $PSScriptRoot 'jobs_bloquants.py'
$sortie = @()
$code = 1
$ErrorActionPreference = 'Continue'
try { $sortie = @(& $python $script --root $Racine --json 2>$null); $code = $LASTEXITCODE } catch { }
$ErrorActionPreference = 'Stop'
$ligneJson = [string](@($sortie | ForEach-Object { [string]$_ } | Where-Object { $_.TrimStart().StartsWith('{') }) | Select-Object -Last 1)

if ($Json) {
    if ($ligneJson) { [Console]::Out.WriteLine($ligneJson) } else { [Console]::Out.WriteLine('{"ok": false, "error": "aucune_sortie"}') }
    if ($code -eq 0 -and $ligneJson) { exit 0 }
    exit 1
}

$rapport = $null
if ($ligneJson) { try { $rapport = $ligneJson | ConvertFrom-Json } catch { } }
if (-not $rapport) {
    Write-Erreur 'Le lecteur du journal n''a rien renvoyé d''exploitable.'
    Wait-FinOutil -SansPause:$SansPause
    exit 1
}
if (-not $rapport.ok) {
    if ([string]$rapport.error -eq 'journal_absent') {
        Write-Ok 'Aucun journal durable : ce Worker n''a encore jamais traité de job. Rien ne bloque.'
        Wait-FinOutil -SansPause:$SansPause
        exit 0
    }
    Write-Erreur ('Journal illisible : ' + [string]$rapport.error)
    Write-Conseil 'Ne supprime pas le journal. Relance cet outil dans une minute (l''agent écrit peut-être dedans).'
    Wait-FinOutil -SansPause:$SansPause
    exit 1
}

Write-Section 'Résultat'
Write-Etat 'Jobs au journal' ([string]$rapport.jobs_total) 'info'
$niveauCourant = 'ok'
if ([int]$rapport.active_current -gt 0) { $niveauCourant = 'alerte' }
Write-Etat 'Actifs, identité courante' ([string]$rapport.active_current + '  (ce sont eux qui bloquent)') $niveauCourant
# Le correctif qui fait ignorer à l'agent les jobs d'anciennes identités est arrivé SANS
# changement de numéro de version : seul le code installé (agent.py) dit s'il est là.
# $true = présent ; $false = absent ; $null = agent.py illisible ou rapport d'un ancien lecteur.
$ignores = $null
if ($rapport.PSObject.Properties['previous_identity_ignored'] -and $null -ne $rapport.previous_identity_ignored) { $ignores = [bool]$rapport.previous_identity_ignored }
$nbPrecedents = [int]$rapport.active_previous
$precedentsBloquent = [bool]($nbPrecedents -gt 0 -and $ignores -ne $true)
if ($ignores -eq $true) { Write-Etat 'Actifs, identité précédente' ([string]$nbPrecedents + '  (ignorés par cet agent : vérifié dans son code)') 'info' }
elseif ($nbPrecedents -eq 0) { Write-Etat 'Actifs, identité précédente' '0' 'info' }
elseif ($ignores -eq $false) { Write-Etat 'Actifs, identité précédente' ([string]$nbPrecedents + '  (cet agent ne les ignore PAS encore : ils bloquent aussi)') 'alerte' }
else { Write-Etat 'Actifs, identité précédente' ([string]$nbPrecedents + '  (impossible de vérifier que cet agent les ignore)') 'alerte' }
if (-not $rapport.identity_known) { Write-Alerte 'Identité courante inconnue (Worker non associé) : impossible de distinguer les anciens jobs.' }
if ([int]$rapport.unreadable -gt 0) { Write-Alerte ([string]$rapport.unreadable + ' enregistrement(s) de job illisible(s) dans le journal.') }

$jobs = @($rapport.jobs)
if ($jobs.Count -eq 0) {
    Write-Host ''
    Write-Ok 'Aucun job actif : rien ne bloque l''arrêt des moteurs ni la mise à jour.'
    Wait-FinOutil -SansPause:$SansPause
    exit 0
}

foreach ($j in $jobs) {
    Write-Section ('Job ' + [string]$j.job_id)
    Write-Etat 'Moteur' ([string]$j.tool_id) 'info'
    Write-Etat 'État' ([string]$j.state) 'info'
    if ($j.engine_stop_confirmed) { Write-Etat 'Arrêt du moteur confirmé' 'oui' 'ok' } else { Write-Etat 'Arrêt du moteur confirmé' 'NON' 'alerte' }
    if ($j.cancel_requested) { Write-Etat 'Annulation demandée' 'oui' 'info' } else { Write-Etat 'Annulation demandée' 'non' 'info' }
    if ($j.runner_lock) { Write-Etat 'Verrou .runner.lock' 'présent dans le dossier du job' 'info' } else { Write-Etat 'Verrou .runner.lock' 'absent' 'info' }
    if ($j.updated_at) { try { Write-Etat 'Dernière écriture' ([DateTimeOffset]::FromUnixTimeSeconds([long][double]$j.updated_at).LocalDateTime.ToString('yyyy-MM-dd HH:mm')) 'info' } catch { } }
    switch ([string]$j.identity) {
        'courante' {
            Write-Etat 'Identité' 'COURANTE : ce job bloque' 'alerte'
            Write-Conseil 'Annule-le depuis le site : Mes Workers > carte de ce Worker > bouton « Arrêter les jobs » (ou « Arrêter » sur la ligne du calcul).'
            Write-Conseil 'Attends ensuite la confirmation du moteur (quelques secondes à une minute), puis relance cet outil.'
            Write-Conseil 'Ne supprime jamais le journal : le Worker perdrait la mémoire de tous ses travaux.'
        }
        'precedente' {
            if ($ignores -eq $true) {
                Write-Etat 'Identité' ('précédente (' + [string]$j.worker_id_prefix + '...) : sans effet') 'ok'
                Write-Conseil 'Ce job date d''une ancienne association. Cet agent l''ignore (correctif présent) : aucune action.'
            } elseif ($ignores -eq $false) {
                Write-Etat 'Identité' ('précédente (' + [string]$j.worker_id_prefix + '...) : ce job BLOQUE la maintenance') 'alerte'
                Write-Conseil 'Cet agent n''ignore PAS encore les jobs d''anciennes identités.'
                Write-Conseil 'Mets le Worker à jour depuis le site (Mes Workers > ce Worker > « Mettre à jour ») : l''agent à jour l''ignorera.'
                Write-Conseil 'Ne supprime jamais le journal pour t''en débarrasser.'
            } else {
                Write-Etat 'Identité' ('précédente (' + [string]$j.worker_id_prefix + '...) : effet non vérifiable') 'alerte'
                Write-Conseil 'Le code de l''agent (agent.py) n''a pas pu être lu : impossible de dire s''il ignore ce job.'
                Write-Conseil 'Si un arrêt des moteurs ou une maintenance est refusé, mets le Worker à jour depuis le dashboard.'
            }
        }
        default {
            Write-Etat 'Identité' 'inconnue (Worker non associé)' 'alerte'
            Write-Conseil ('Réassocie d''abord le Worker avec ' + (Get-NomEntree 8) + ' ; ce job sera alors classé automatiquement.')
        }
    }
}

Write-Host ''
if ([int]$rapport.active_current -gt 0) { Write-Alerte ([string]$rapport.active_current + ' job(s) à annuler depuis le site (Mes Workers > carte de ce Worker > « Arrêter les jobs ») avant d''arrêter les moteurs ou de mettre à jour.') }
if ($precedentsBloquent -and $ignores -eq $false) {
    Write-Alerte ([string]$nbPrecedents + ' job(s) d''une ancienne identité bloquent la maintenance et l''arrêt des moteurs : cet agent n''a pas le correctif.')
    Write-Conseil 'Mets le Worker à jour depuis le site (Mes Workers > ce Worker > « Mettre à jour »).'
    Write-Conseil ('Si la mise à jour est elle-même refusée à cause de ce job : lance ' + (Get-NomEntree 4) + ' et demande de l''aide.')
} elseif ($precedentsBloquent) {
    Write-Alerte ([string]$nbPrecedents + ' job(s) d''une ancienne identité : impossible de vérifier que cet agent les ignore (agent.py illisible).')
}
if ([int]$rapport.active_current -eq 0 -and -not $precedentsBloquent) { Write-Ok 'Aucun job de l''identité courante, et ceux des anciennes identités sont ignorés : rien ne bloque.' }
Wait-FinOutil -SansPause:$SansPause
exit 0
