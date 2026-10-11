<#
    Supprimer les doublons (ZONE ROUGE) : après un contrôle récent (« Contrôler les doublons »), les copies
    identiques de modèles ComfyUI de ce Worker sont remplacées par des liens physiques ; les copies manifestes
    rangées à côté de leur original peuvent être supprimées si le propriétaire le demande.
    C'est l'AGENT qui agit, et qui relit chaque fichier avant d'y toucher. Cet outil montre le plan, exige une
    phrase tapée, renvoie à l'agent l'empreinte du plan affiché, puis suit l'opération.

    Usage :
      supprimer_doublons.ps1               plan, question éventuelle [L/S], phrase à taper, opération, bilan
      supprimer_doublons.ps1 -Simulation   montre le plan et s'arrête : rien n'est demandé à l'agent
      -Oui est accepté (le menu connaît ce paramètre) mais ne remplace JAMAIS la phrase à taper :
      sans clavier, l'outil se termine en code 2.   -SansPause : pas de pause finale.
    Codes : 0 fait, ou rien à faire ; 1 échec (agent muet, opération incomplète) ; 2 refusé ou annulé.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause,
    [switch]$Oui,
    [switch]$Simulation
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'commun_cycle.ps1')
. (Join-Path $PSScriptRoot 'commun_gestion.ps1')
Initialize-ConsoleWorker -Titre 'Supprimer les doublons'
$NomOutil = 'supprimer_doublons'
$script:PointsOuverts = $false
$Etiquette = ''
if ($Simulation) { $Etiquette = '[simulation] ' }

# Pourquoi l'agent garde tel fichier plutôt que l'autre (champ keep_reason du plan).
$script:RaisonsGarde = @{
    'catalogue'            = 'nom connu du catalogue'
    'installe_par_le_site' = 'installé depuis le site'
    'deja_partage'         = 'déjà partagé par un lien'
    'nom_d_origine'        = 'nom d''origine, l''autre est une copie'
    'plus_ancien'          = 'fichier le plus ancien'
    'nom_le_plus_court'    = 'nom le plus court'
    'ordre'                = 'premier dans l''ordre alphabétique'
}

function Close-Points {
    <# Termine une ligne de points de progression avant d'écrire une ligne entière. #>
    if ($script:PointsOuverts) { Write-Host ''; $script:PointsOuverts = $false }
}

function Read-Reponse {
    <#
        Lit une réponse au clavier (ou dans le champ « Réponse à l'outil » de worker.exe). La question est
        TOUJOURS écrite avant, par une ligne ordinaire : quand la sortie est un tube, l'invite de Read-Host
        ne s'affiche pas. Entrée fermée ou absente : chaîne vide, jamais d'erreur.
    #>
    $saisie = $null
    try { $saisie = Read-Host '   Ta réponse' } catch { $saisie = $null }
    if ($null -eq $saisie) { return '' }
    return ([string]$saisie).Trim()
}

function Get-NombreDoublons {
    <# Nombre positif d'une réponse de l'agent (tailles en octets comprises) ; 0 pour toute autre valeur. #>
    param($Valeur)
    if ($null -eq $Valeur -or $Valeur -is [bool] -or $Valeur -is [string]) { return [double]0 }
    try {
        $nombre = [double]$Valeur
        if ([double]::IsNaN($nombre) -or [double]::IsInfinity($nombre) -or $nombre -lt 0) { return [double]0 }
        return $nombre
    } catch { return [double]0 }
}

function Get-FinValidite {
    <# Fin de validité d'un contrôle : 24 heures après sa fin (secondes Unix), en heure du PC ; $null si la date est illisible. #>
    param($FinControle)
    if ($null -eq (ConvertTo-DateLocale $FinControle)) { return $null }
    return (ConvertTo-DateLocale ([double]$FinControle + 86400))
}

function Get-ListeChamp {
    <#
        Liste portée par un champ JSON, TOUJOURS rendue comme un tableau (vide si le champ est absent ou
        n'est pas une liste). Lue sans passer par une valeur de retour intermédiaire : PowerShell y
        changerait une liste d'un seul élément en cet élément.
    #>
    param($Objet, [string]$Nom)
    $liste = New-Object System.Collections.ArrayList
    if ($null -ne $Objet -and -not ($Objet -is [string])) {
        $propriete = $Objet.PSObject.Properties[$Nom]
        if ($null -ne $propriete -and $null -ne $propriete.Value -and $propriete.Value -is [System.Array]) {
            foreach ($element in $propriete.Value) { [void]$liste.Add($element) }
        }
    }
    return , $liste.ToArray()
}

function Get-EtatDoublons {
    <# « duplicates-status » : Ok, Audit, Application, Rapport (objets de l'agent) et Reponse (brute, pour un refus). #>
    $reponse = $null
    try { $reponse = Invoke-ActionLocale -Racine $Racine -Action 'duplicates-status' -DelaiSecondes 15 } catch { }
    $lecture = [pscustomobject]@{ Ok = $false; Audit = $null; Application = $null; Rapport = $null; Reponse = $reponse }
    if ($reponse -and $reponse.Ok -and $reponse.Resultat) {
        $lecture.Ok = $true
        $lecture.Audit = Get-ChampGestion $reponse.Resultat 'audit'
        $lecture.Application = Get-ChampGestion $reponse.Resultat 'apply'
        $lecture.Rapport = Get-ChampGestion $reponse.Resultat 'report'
    }
    return $lecture
}

function Get-ConseilRefus {
    <# Ce qu'il faut faire après un refus de l'agent, reconnu à son texte. '' : le message se suffit. #>
    param([string]$Message)
    if ($Message -match 'Fichier de l.agent manquant') { return ('Lance ' + (Get-NomEntree 24) + ' pour remettre les fichiers de l''agent, puis relance cet outil.') }
    if ($Message -match 'Contrôle trop ancien ou déjà utilisé') { return ('Lance d''abord ' + (Get-NomEntree 19) + ', attends sa fin, puis relance cet outil.') }
    if ($Message -match 'contenu a changé depuis la confirmation') { return 'Relance cet outil : il refera le plan et te le montrera avant toute confirmation.' }
    if ($Message -match 'Confirmation absente ou inexacte') { return 'Relance cet outil et tape la phrase demandée, lettre pour lettre, en majuscules.' }
    if ($Message -match 'moteur d.images tourne') { return ('Arrête le moteur d''images : depuis le site (Mes Workers), ou avec ' + (Get-NomEntree 9) + ' dans le menu du dossier. Puis relance cet outil.') }
    if ($Message -match 'installation de ressource|installation ou une maintenance') { return 'Attends la fin de l''installation (elle se suit dans Mes Workers), puis relance cet outil.' }
    if ($Message -match 'nettoyage local est en cours') { return 'Attends la fin du nettoyage, puis relance cet outil.' }
    if ($Message -match 'suppression des doublons est déjà en cours') { return 'Relance cet outil : il reprendra le suivi de l''opération en cours.' }
    if ($Message -match 'contrôle des doublons est (déjà )?en cours') { return ('Attends la fin du contrôle, ou annule-le avec ' + (Get-NomEntree 19) + ', puis relance cet outil.') }
    if ($Message -match 'installation précédente est encore active') { return ('Lance ' + (Get-NomEntree 18) + ' pour voir ce qui bloque, puis relance cet outil.') }
    if ($Message -match '^Refusé') { return ('Un calcul est en cours, ou son arrêt n''est pas confirmé : regarde ' + (Get-NomEntree 2) + '. Attends sa fin ou arrête-le avec ' + (Get-NomEntree 21) + ', puis relance cet outil.') }
    return ''
}

function Write-RefusAgent {
    <#
        Une action « duplicates-* » n'a pas abouti : message figé si l'agent est muet, à redémarrer ou trop
        ancien ; sinon le refus de l'agent TEL QUEL, suivi de ce qu'il faut faire.
        Rend le code de sortie : 1 (l'agent n'a pas pu répondre, ou un de ses fichiers manque), 2 (refus).
    #>
    param($Reponse)
    $refus = [pscustomobject]@{ AgentMuet = $false; AgentARedemarrer = $false; AgentTropAncien = $false; Erreur = '' }
    Set-EchecLocal -Retour $refus -Reponse $Reponse
    Close-Points
    Write-Host ''
    Write-RefusRelais -Resultat $refus
    if ($refus.AgentMuet -or $refus.AgentARedemarrer -or $refus.AgentTropAncien) { return 1 }
    $conseil = Get-ConseilRefus -Message ([string]$refus.Erreur)
    if ($conseil) { Write-Conseil $conseil }
    if ([string]$refus.Erreur -match 'Fichier de l.agent manquant') { return 1 }
    return 2
}

function Read-PlanComplet {
    <#
        Plan entier écrit par l'agent dans data\duplicates-plan.json juste avant sa réponse (la réponse, elle,
        n'en porte que le début). $null si le fichier est absent, remplacé par un lien ou illisible.
    #>
    $dossier = Join-Path $Racine 'data'
    $fichier = Join-Path $dossier 'duplicates-plan.json'
    for ($essai = 0; $essai -lt 3; $essai++) {
        try {
            if (Test-Path -LiteralPath $fichier -PathType Leaf) {
                if (([IO.File]::GetAttributes($dossier) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return $null }
                if (([IO.File]::GetAttributes($fichier) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { return $null }
                $texte = [IO.File]::ReadAllText($fichier, (New-Object System.Text.UTF8Encoding($false)))
                $lu = $texte | ConvertFrom-Json
                if ($null -ne $lu -and -not ($lu -is [System.Array]) -and -not ($lu -is [string])) { return $lu }
            }
        } catch { }
        Start-Sleep -Milliseconds 300
    }
    return $null
}

function Get-PlanDoublons {
    <#
        Demande le plan à l'agent (« duplicates-plan ») puis relit le plan ENTIER dans data\. Ce qui sera affiché
        et l'empreinte qui sera renvoyée viennent du même objet, et cet objet doit concorder avec la réponse.
        Issue : 'ok' ; 'refus' (Refus = réponse brute de l'agent) ; 'erreur' (Erreur = texte à afficher).
    #>
    param([ValidateSet('link', 'delete')][string]$Mode = 'link')
    $plan = [pscustomobject]@{
        Issue = 'erreur'; Refus = $null; Erreur = ''; Mode = $Mode; Empreinte = ''; DateControle = $null; DateLimite = $null
        Operations = @(); HorsPerimetre = @(); Ecartes = @()
        NombreOperations = 0; Liens = 0; Suppressions = 0; Octets = [double]0; NombreHors = 0; NombreEcartes = 0; Supprimables = 0
    }
    $reponse = $null
    try { $reponse = Invoke-ActionLocale -Racine $Racine -Action 'duplicates-plan' -DelaiSecondes 30 -Arguments @{ same_folder = $Mode } } catch { }
    if (-not ($reponse -and $reponse.Ok -and $reponse.Resultat)) { $plan.Issue = 'refus'; $plan.Refus = $reponse; return $plan }
    $resume = $reponse.Resultat
    $empreinte = [string](Get-ChampGestion $resume 'digest' '')
    if ($empreinte -cnotmatch '^[0-9a-f]{64}$') { $plan.Erreur = 'Réponse inattendue de l''agent : le plan n''a pas d''empreinte.'; return $plan }
    $complet = Read-PlanComplet
    if ($null -eq $complet) { $plan.Erreur = 'Le plan écrit par l''agent (data\duplicates-plan.json) est absent ou illisible.'; return $plan }
    $operations = Get-ListeChamp $complet 'operations'
    $liens = 0; $suppressions = 0; $octets = [double]0
    foreach ($operation in @($operations)) {
        $type = [string](Get-ChampGestion $operation 'op' '')
        $garde = [string](Get-ChampGestion $operation 'keep' '')
        $cible = [string](Get-ChampGestion $operation 'target' '')
        if (@('link', 'delete') -cnotcontains $type -or -not $garde -or -not $cible) { $plan.Erreur = 'Le plan écrit par l''agent contient une ligne illisible.'; return $plan }
        if ($type -eq 'delete') { $suppressions++ } else { $liens++ }
        $octets += Get-NombreDoublons (Get-ChampGestion $operation 'size_bytes')
    }
    # Le plan relu doit être celui que l'agent vient de décrire : même empreinte, même mode, mêmes nombres,
    # et les mêmes premières lignes (la réponse de l'agent porte le début de la liste).
    $debut = Get-ListeChamp $resume 'operations'
    $memeDebut = (@($debut).Count -le @($operations).Count)
    for ($i = 0; $memeDebut -and $i -lt @($debut).Count; $i++) {
        foreach ($champ in @('op', 'sha256', 'keep', 'target')) {
            if ([string](Get-ChampGestion $debut[$i] $champ '') -cne [string](Get-ChampGestion $operations[$i] $champ '')) { $memeDebut = $false }
        }
    }
    $concorde = (
        $memeDebut -and
        ([string](Get-ChampGestion $complet 'digest' '') -ceq $empreinte) -and
        ([string](Get-ChampGestion $complet 'same_folder' '') -ceq $Mode) -and
        ([string](Get-ChampGestion $resume 'same_folder' '') -ceq $Mode) -and
        (@($operations).Count -eq [int64](Get-NombreDoublons (Get-ChampGestion $resume 'operations_count'))) -and
        ($liens -eq [int64](Get-NombreDoublons (Get-ChampGestion $resume 'links'))) -and
        ($suppressions -eq [int64](Get-NombreDoublons (Get-ChampGestion $resume 'deletions'))) -and
        ($Mode -eq 'delete' -or $suppressions -eq 0)
    )
    if (-not $concorde) { $plan.Erreur = 'Le plan écrit par l''agent ne correspond pas à sa réponse : il a changé entre-temps.'; return $plan }
    $plan.Issue = 'ok'
    $plan.Empreinte = $empreinte
    $plan.DateControle = ConvertTo-DateLocale (Get-ChampGestion $complet 'report_finished_at')
    $plan.DateLimite = Get-FinValidite (Get-ChampGestion $complet 'report_finished_at')
    $plan.Operations = $operations
    $plan.HorsPerimetre = Get-ListeChamp $complet 'out_of_scope'
    $plan.Ecartes = Get-ListeChamp $complet 'skipped'
    $plan.NombreOperations = @($operations).Count
    $plan.Liens = $liens
    $plan.Suppressions = $suppressions
    $plan.Octets = $octets
    $plan.NombreHors = [Math]::Max(@($plan.HorsPerimetre).Count, [int64](Get-NombreDoublons (Get-ChampGestion $resume 'out_of_scope_count')))
    $plan.NombreEcartes = @($plan.Ecartes).Count
    $plan.Supprimables = [int64](Get-NombreDoublons (Get-ChampGestion $resume 'deletable_count'))
    return $plan
}

function Write-Plan {
    <#
        Le plan en clair : ce qui devient un lien, ce qui est supprimé, ce qui n'est pas touché.
        Les chemins sont relatifs au dossier des modèles ComfyUI du Worker. Les lignes de fichiers sont écrites
        sans coupure automatique : un nom de modèle ne doit jamais être coupé en deux.
    #>
    param($Plan)
    Write-Section 'Le plan'
    if ($null -ne $Plan.DateControle -and $null -ne $Plan.DateLimite) {
        Write-Info ('D''après le contrôle du ' + (Format-DateEtat -Date $Plan.DateControle) + ', utilisable jusqu''au ' + (Format-DateEtat -Date $Plan.DateLimite) + '.')
    }
    Write-Info ($Etiquette + $Plan.Liens + ' copie(s) à remplacer par un lien : le nom et l''emplacement restent, la place en double est rendue.')
    if ($Plan.Suppressions -gt 0) { Write-Alerte ($Etiquette + $Plan.Suppressions + ' copie(s) à SUPPRIMER définitivement : leur nom disparaît, il n''y a pas de corbeille.') }
    else { Write-Info ($Etiquette + 'Aucune suppression : aucun nom de fichier ne disparaît.') }
    Write-Info ($Etiquette + 'Place à libérer : ' + (Format-Taille $Plan.Octets) + '.')

    if ($Plan.NombreOperations -gt 0) {
        Write-Section ('Ce qui change : ' + $Plan.NombreOperations + ' copie(s)')
        Write-Info 'Dossier : components\comfyui\models du Worker.'
        $rang = 0
        foreach ($operation in @($Plan.Operations | Select-Object -First 30)) {
            $rang++
            $raison = [string](Get-ChampGestion $operation 'keep_reason' '')
            if ($script:RaisonsGarde.ContainsKey($raison)) { $raison = [string]$script:RaisonsGarde[$raison] }
            $detail = Format-Taille (Get-NombreDoublons (Get-ChampGestion $operation 'size_bytes'))
            if ($raison) { $detail += ' ; ' + $raison }
            Write-Host ('   {0,2}. gardé    : {1}   ({2})' -f $rang, [string](Get-ChampGestion $operation 'keep' ''), $detail) -ForegroundColor Gray
            if ([string](Get-ChampGestion $operation 'op' '') -eq 'delete') { Write-Host ('       SUPPRIMÉ : ' + [string](Get-ChampGestion $operation 'target' '')) -ForegroundColor Red }
            else { Write-Host ('       lien     : ' + [string](Get-ChampGestion $operation 'target' '')) -ForegroundColor Green }
        }
        if ($Plan.NombreOperations -gt 30) {
            Write-Info ('... et ' + ($Plan.NombreOperations - 30) + ' autre(s), traitée(s) de la même façon. La liste entière : data\duplicates-plan.json, dans le dossier du Worker.')
        }
        Write-Info 'Un lien : deux noms pour un seul contenu sur le disque. Supprimer plus tard l''un des deux noms laisse l''autre intact.'
    }
    if ($Plan.Suppressions -gt 0) {
        Write-Section ('Noms supprimés définitivement : ' + $Plan.Suppressions)
        foreach ($operation in @($Plan.Operations)) {
            if ([string](Get-ChampGestion $operation 'op' '') -eq 'delete') { Write-Host ('   ' + (Get-Trait 'non') + ' ' + [string](Get-ChampGestion $operation 'target' '')) -ForegroundColor Red }
        }
        Write-Alerte ('Les ' + $Plan.Suppressions + ' nom(s) supprimés disparaîtront de Mes Workers.')
        Write-Info 'Leur contenu reste sur le disque sous le nom du fichier gardé, dans le même dossier.'
    }

    Write-Section 'Ce qui n''est pas touché'
    $parRaison = @{}
    $groupes = @{}
    $fichiers = [int64]0
    foreach ($hors in @($Plan.HorsPerimetre)) {
        $raison = ([string](Get-ChampGestion $hors 'reason' '')).Trim()
        if (-not $raison) { $raison = 'raison non précisée' }
        $copies = [int64](Get-NombreDoublons (Get-ChampGestion $hors 'copies'))
        if ($copies -lt 1) { $copies = 1 }
        if ($parRaison.ContainsKey($raison)) { $parRaison[$raison] = [int64]$parRaison[$raison] + $copies } else { $parRaison[$raison] = $copies }
        $groupes[[string](Get-ChampGestion $hors 'sha256' '')] = $true
        $fichiers += $copies
    }
    if ($fichiers -gt 0) {
        Write-Info ('' + $fichiers + ' fichier(s) identiques hors périmètre, dans ' + $groupes.get_Count() + ' groupe(s) : laissés tels quels.')
        foreach ($raison in @($parRaison.get_Keys() | Sort-Object)) { Write-Conseil ('' + $parRaison[$raison] + ' fichier(s) : ' + $raison) }
        if ($Plan.NombreHors -gt @($Plan.HorsPerimetre).Count) { Write-Info ('(compte fait sur les ' + @($Plan.HorsPerimetre).Count + ' plus gros cas ; il y en a ' + $Plan.NombreHors + ' en tout)') }
    }
    else { Write-Info 'Aucun autre fichier identique n''a été trouvé hors du périmètre.' }
    if ($Plan.NombreEcartes -gt 0) {
        Write-Info ('' + $Plan.NombreEcartes + ' fichier(s) écarté(s) du plan, laissé(s) tel(s) quel(s) :')
        foreach ($ecarte in @($Plan.Ecartes | Select-Object -First 20)) {
            Write-Host ('   ' + (Get-Trait 'puce') + ' ' + [string](Get-ChampGestion $ecarte 'path' '') + ' : ' + [string](Get-ChampGestion $ecarte 'reason' '')) -ForegroundColor DarkCyan
        }
        if ($Plan.NombreEcartes -gt 20) { Write-Info ('... et ' + ($Plan.NombreEcartes - 20) + ' autre(s).') }
    }
    Write-Info 'Tout fichier absent de la liste « Ce qui change » reste tel quel : moteurs, Hunyuan3D, caches, dossiers hors du Worker.'
}

function Watch-Application {
    <#
        Suit l'opération : état relu toutes les 2 s, un point toutes les 5 s, une ligne entière « faits/total »
        toutes les 30 s. Sans limite tant que l'agent avance ; dix minutes sans progrès : on rend la main.
        Rend Fin ('done' | 'bloque' | 'muet' | 'perdu') et Lu (dernier état lu).
    #>
    param([int]$PauseSecondes = 2, [int]$LimiteSansProgresSecondes = 600, [int]$PointSecondes = 5, [int]$LigneSecondes = 30)
    $dernierPoint = Get-Date
    $derniereLigne = Get-Date
    $dernierProgres = Get-Date
    $faits = -1; $courant = ''; $manquees = 0; $dernier = $null
    while ($true) {
        $lu = Get-EtatDoublons
        if (-not $lu.Ok) {
            $manquees++
            if ($manquees -ge 5) { Close-Points; return [pscustomobject]@{ Fin = 'muet'; Lu = $dernier } }
        }
        else {
            $manquees = 0
            $dernier = $lu
            $situation = [string](Get-ChampGestion $lu.Application 'state' '')
            if ($situation -eq 'done') { Close-Points; return [pscustomobject]@{ Fin = 'done'; Lu = $lu } }
            if ($situation -ne 'running') { Close-Points; return [pscustomobject]@{ Fin = 'perdu'; Lu = $lu } }
            $nombre = [int64](Get-NombreDoublons (Get-ChampGestion $lu.Application 'done'))
            $total = [int64](Get-NombreDoublons (Get-ChampGestion $lu.Application 'total'))
            $enCours = [string](Get-ChampGestion $lu.Application 'current' '')
            $maintenant = Get-Date
            if ($nombre -ne $faits -or $enCours -cne $courant) { $faits = $nombre; $courant = $enCours; $dernierProgres = $maintenant }
            elseif (($maintenant - $dernierProgres).TotalSeconds -ge $LimiteSansProgresSecondes) { Close-Points; return [pscustomobject]@{ Fin = 'bloque'; Lu = $lu } }
            if (($maintenant - $derniereLigne).TotalSeconds -ge $LigneSecondes) {
                Close-Points
                Write-Info ('' + $nombre + '/' + $total + ' copie(s) traitée(s)')
                $derniereLigne = $maintenant; $dernierPoint = $maintenant
            }
            elseif (($maintenant - $dernierPoint).TotalSeconds -ge $PointSecondes) {
                Write-Host '.' -NoNewline -ForegroundColor DarkGray
                $script:PointsOuverts = $true
                $dernierPoint = $maintenant
            }
        }
        Start-Sleep -Seconds $PauseSecondes
    }
}

function Write-BilanApplication {
    <# Bilan d'une opération suivie par Watch-Application. Rend le code de sortie : 0 sans échec, sinon 1. #>
    param($Issue)
    $application = $null; $rapport = $null
    if ($Issue.Lu) { $application = $Issue.Lu.Application; $rapport = $Issue.Lu.Rapport }
    $faits = [int64](Get-NombreDoublons (Get-ChampGestion $application 'done'))
    $total = [int64](Get-NombreDoublons (Get-ChampGestion $application 'total'))
    Write-Host ''
    if ($Issue.Fin -eq 'muet') {
        Write-Alerte 'L''agent ne répond plus : impossible de dire où en est l''opération.'
        Write-Info 'Chaque fichier est traité en entier ou pas du tout : aucun n''est laissé à moitié.'
        Write-Conseil ('Vérifie le Worker avec ' + (Get-NomEntree 1) + ', puis relance cet outil : il reprend le suivi si l''opération continue.')
        return 1
    }
    if ($Issue.Fin -eq 'perdu') {
        Write-Alerte 'L''agent a redémarré pendant l''opération : elle s''est arrêtée là où elle en était.'
        Write-Info 'Chaque fichier est traité en entier ou pas du tout : aucun n''est laissé à moitié.'
        Write-Conseil 'Relance cet outil pour voir ce qui reste à faire.'
        return 1
    }
    if ($Issue.Fin -eq 'bloque') {
        Write-Alerte ('Aucun progrès depuis 10 minutes (' + $faits + '/' + $total + ').')
        Write-Info 'L''opération continue peut-être dans l''agent (très gros fichier, disque lent).'
        Write-Conseil ('Relance cet outil plus tard : il reprend le suivi. Si rien ne bouge, regarde ' + (Get-NomEntree 3) + '.')
        return 1
    }
    $lies = [int64](Get-NombreDoublons (Get-ChampGestion $application 'linked'))
    $supprimes = [int64](Get-NombreDoublons (Get-ChampGestion $application 'deleted'))
    $echecs = [int64](Get-NombreDoublons (Get-ChampGestion $application 'failed'))
    $nonTraites = [int64](Get-NombreDoublons (Get-ChampGestion $application 'skipped'))
    $occupes = [int64](Get-NombreDoublons (Get-ChampGestion $application 'busy'))
    $liberes = Get-NombreDoublons (Get-ChampGestion $application 'freed_bytes')
    $consomme = [bool](Get-ChampGestion $rapport 'consumed' $false)
    Write-Section 'Bilan'
    $niveauLiens = 'info'
    if ($lies -gt 0) { $niveauLiens = 'ok' }
    Write-Etat -Libelle 'Copies remplacées par un lien' -Valeur ('' + $lies) -Niveau $niveauLiens
    if ($supprimes -gt 0) { Write-Etat -Libelle 'Copies supprimées' -Valeur ('' + $supprimes + ' (leur nom disparaîtra de Mes Workers)') -Niveau ok }
    Write-Etat -Libelle 'Place libérée' -Valeur (Format-Taille $liberes) -Niveau info
    if ($echecs -gt 0) {
        Write-Etat -Libelle 'Échecs' -Valeur ('' + $echecs + ' : fichier(s) laissé(s) tel(s) quel(s)') -Niveau erreur
        $erreurs = Get-ListeChamp $application 'errors'
        foreach ($erreur in @($erreurs)) {
            $cible = [string](Get-ChampGestion $erreur 'target' '')
            $raison = [string](Get-ChampGestion $erreur 'error' '')
            if ($cible) { Write-Host ('   ' + (Get-Trait 'non') + ' ' + $cible + ' : ' + $raison) -ForegroundColor Red }
            else { Write-Host ('   ' + (Get-Trait 'non') + ' ' + $raison) -ForegroundColor Red }
        }
    }
    if ($nonTraites -gt 0) {
        Write-Etat -Libelle 'Non traitées' -Valeur ('' + $nonTraites + ' : le moteur d''images a démarré pendant l''opération, ou l''agent s''arrêtait') -Niveau alerte
    }
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('bilan : ' + $lies + ' lien(s), ' + $supprimes + ' suppression(s), ' + $echecs + ' échec(s) dont ' + $occupes + ' fichier(s) occupé(s), ' + $nonTraites + ' non traitée(s), ' + [int64]$liberes + ' octets libérés')
    Write-Host ''
    if ($occupes -gt 0 -and -not $consomme) {
        $fin = Get-FinValidite (Get-ChampGestion $rapport 'finished_at')
        $jusque = ''
        if ($null -ne $fin) { $jusque = ' (valable jusqu''au ' + (Format-DateEtat -Date $fin) + ')' }
        Write-Conseil ('Tu peux relancer cet outil sans refaire le contrôle' + $jusque + '.')
    }
    elseif ($echecs -gt 0 -or $nonTraites -gt 0) {
        Write-Conseil ('Pour retenter ce qui reste : relance d''abord ' + (Get-NomEntree 19) + ', puis cet outil.')
    }
    if ($echecs -eq 0 -and $nonTraites -eq 0) {
        Write-Ok 'Opération terminée sans échec.'
        Write-Info ('Ce contrôle a servi : avant un nouveau passage, relance ' + (Get-NomEntree 19) + '.')
        return 0
    }
    Write-Alerte 'Opération terminée, mais incomplète : lis les lignes ci-dessus.'
    return 1
}

try { $Racine = Get-RacineWorker -Racine $Racine } catch { Write-Erreur ([string]$_.Exception.Message); Exit-Outil -Code 1 -SansPause:$SansPause }

try {
    Write-Explication -Titre 'SUPPRIMER LES DOUBLONS (ZONE ROUGE)' -Fait @(
        'Reprend le dernier contrôle des doublons (moins de 24 heures) et montre le plan AVANT d''agir : quel fichier est gardé, quelle copie est remplacée.',
        'Par défaut, chaque copie est remplacée par un LIEN vers le fichier gardé : son nom et son emplacement restent, la place en double est rendue.',
        'Si tu le demandes, les copies manifestes (« ... - Copie », « ... (1) ») rangées à côté de leur original sont supprimées pour de bon.',
        'L''agent relit chaque fichier (taille, date, contenu entier) juste avant d''y toucher : au moindre écart, le fichier est laissé tel quel.'
    ) -NeFaitPas @(
        'Il ne touche qu''aux modèles ComfyUI rangés dans le dossier de ce Worker (components\comfyui\models).',
        'Jamais les moteurs eux-mêmes, Hunyuan3D, les caches ni un dossier situé hors du Worker : ils sont listés « non touchés ».',
        'Il n''arrête rien : si le moteur d''images tourne, ou si un calcul ou une installation est en cours, l''agent refuse et rien ne change.',
        'Une suppression ne se rattrape pas : il n''y a pas de corbeille. Un lien, lui, ne fait disparaître aucun nom.'
    ) -Exige @(
        ('Un contrôle de moins de 24 heures fait avec ' + (Get-NomEntree 19) + ' : un contrôle lancé depuis le site ne compte pas.'),
        'Le moteur d''images arrêté ; aucun calcul ni installation en cours.',
        'Une phrase de confirmation tapée en entier. Aucun droit administrateur.'
    )

    # 1. L'agent répond-il, et connaît-il ces actions ?
    $agent = Get-EtatAgent -Racine $Racine
    if (-not $agent.Vivant -or -not $agent.Capable) {
        Write-RefusRelais -Resultat $agent
        Write-Info 'Rien n''a été demandé : aucun fichier modifié.'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }

    # 2. Où en est-on : opération déjà en cours, contrôle en cours, contrôle utilisable ?
    $lu = Get-EtatDoublons
    if (-not $lu.Ok) {
        $code = Write-RefusAgent $lu.Reponse
        Exit-Outil -Code $code -SansPause:$SansPause
    }
    if ([string](Get-ChampGestion $lu.Application 'state' '') -eq 'running') {
        Write-Host ''
        Write-Alerte 'Une suppression des doublons est déjà en cours sur ce Worker.'
        Write-Info ('' + [int64](Get-NombreDoublons (Get-ChampGestion $lu.Application 'done')) + '/' + [int64](Get-NombreDoublons (Get-ChampGestion $lu.Application 'total')) + ' copie(s) traitée(s)')
        if ($Simulation) {
            Write-Info '[simulation] Arrêt ici : aucune modification n''a été demandée à l''agent.'
            Exit-Outil -Code 0 -SansPause:$SansPause
        }
        Write-Info 'Cet outil en reprend le suivi : un point toutes les 5 secondes, une ligne toutes les 30 secondes.'
        $code = Write-BilanApplication (Watch-Application)
        Exit-Outil -Code $code -SansPause:$SansPause
    }
    if ([string](Get-ChampGestion $lu.Audit 'state' '') -eq 'running') {
        Write-Host ''
        Write-Alerte ('Un contrôle des doublons est en cours (' + (ConvertTo-EntierGestion (Get-ChampGestion $lu.Audit 'progress') 100) + ' %) : rien ne peut être supprimé pendant qu''il lit les fichiers.')
        Write-Conseil ('Attends sa fin, ou annule-le avec ' + (Get-NomEntree 19) + ', puis relance cet outil.')
        Write-Info 'Rien n''a changé.'
        Exit-Outil -Code 2 -SansPause:$SansPause
    }
    $rapport = $lu.Rapport
    $dateRapport = ConvertTo-DateLocale (Get-ChampGestion $rapport 'finished_at')
    $quand = 'date inconnue'
    if ($null -ne $dateRapport) { $quand = 'du ' + (Format-DateEtat -Date $dateRapport) }
    $motif = ''
    if (-not [bool](Get-ChampGestion $rapport 'present' $false)) { $motif = 'Aucun contrôle des doublons n''a encore été fait depuis ce PC.' }
    elseif ([bool](Get-ChampGestion $rapport 'consumed' $false)) { $motif = 'Le dernier contrôle (' + $quand + ') a déjà été utilisé par cet outil.' }
    elseif ((Get-NombreDoublons (Get-ChampGestion $rapport 'age_seconds')) -gt 86400) { $motif = 'Le dernier contrôle (' + $quand + ') a plus de 24 heures : les fichiers ont pu changer depuis.' }
    if ($motif) {
        Write-Host ''
        Write-Alerte $motif
        Write-Conseil ('Lance d''abord ' + (Get-NomEntree 19) + ', attends sa fin, puis reviens ici.')
        Write-Info 'Un contrôle lancé depuis le site (Mes Workers) ne compte pas : il faut celui de ce menu.'
        Write-Info 'Rien n''a changé.'
        Exit-Outil -Code 2 -SansPause:$SansPause
    }

    # 3. Le plan, affiché en entier avant toute question.
    $plan = Get-PlanDoublons -Mode 'link'
    if ($plan.Issue -eq 'refus') {
        $code = Write-RefusAgent $plan.Refus
        Write-Info 'Rien n''a changé.'
        Exit-Outil -Code $code -SansPause:$SansPause
    }
    if ($plan.Issue -ne 'ok') {
        Write-Host ''
        Write-Erreur $plan.Erreur
        Write-Conseil 'Relance cet outil. Aucune modification n''a été demandée à l''agent.'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }
    if (-not [bool](Get-ChampGestion $rapport 'complete' $false)) {
        # Pas un échec : sur un Worker récent, les dossiers de cache n'existent pas encore.
        Write-Info ('Le contrôle n''a pas pu lire ' + [int64](Get-NombreDoublons (Get-ChampGestion $rapport 'errors')) + ' emplacement(s), le plus souvent des dossiers qui n''existent pas encore : le plan ne porte que sur ce qui a été lu et comparé.')
    }
    Write-Plan $plan
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ($Etiquette + 'plan : ' + $plan.NombreOperations + ' copie(s), ' + $plan.Supprimables + ' supprimable(s), ' + [int64]$plan.Octets + ' octets, ' + $plan.NombreHors + ' cas hors périmètre, ' + $plan.NombreEcartes + ' écarté(s)')
    if ($plan.NombreOperations -eq 0) {
        Write-Host ''
        Write-Ok ($Etiquette + 'Rien à faire dans le périmètre : aucune copie de modèle ComfyUI à remplacer.')
        Write-Info 'Aucun fichier n''a été modifié.'
        Exit-Outil -Code 0 -SansPause:$SansPause
    }
    if ($Simulation) {
        Write-Host ''
        if ($plan.Supprimables -gt 0) { Write-Info ('[simulation] ' + $plan.Supprimables + ' de ces copie(s) sont des copies manifestes rangées à côté de leur original : hors simulation, l''outil propose de les supprimer au lieu de les lier.') }
        Write-Info '[simulation] Arrêt ici : aucune modification n''a été demandée à l''agent.'
        Exit-Outil -Code 0 -SansPause:$SansPause
    }

    # 4. Copies manifestes du même dossier : lien (défaut) ou suppression, au choix du propriétaire.
    if ($plan.Supprimables -gt 0) {
        Write-Host ''
        Write-Alerte ('Pour ces ' + $plan.Supprimables + ' copie(s) manifestes rangées dans le MÊME dossier que leur original : [L] les remplacer aussi par des liens (rien ne disparaît)  [S] les supprimer définitivement')
        $choix = (Read-Reponse).ToUpper()
        if ($choix -eq 'S') {
            $planSuppression = Get-PlanDoublons -Mode 'delete'
            if ($planSuppression.Issue -eq 'refus') {
                $code = Write-RefusAgent $planSuppression.Refus
                Write-Info 'Rien n''a changé.'
                Exit-Outil -Code $code -SansPause:$SansPause
            }
            if ($planSuppression.Issue -ne 'ok') {
                Write-Host ''
                Write-Erreur $planSuppression.Erreur
                Write-Conseil 'Relance cet outil. Aucune modification n''a été demandée à l''agent.'
                Exit-Outil -Code 1 -SansPause:$SansPause
            }
            if ($planSuppression.Suppressions -gt 0) {
                $plan = $planSuppression
                Write-Host ''
                Write-Alerte 'Nouveau plan, avec suppressions :'
                Write-Plan $plan
            }
            else { Write-Info 'Plus aucune copie à supprimer : le plan reste celui des liens, ci-dessus.' }
        }
        elseif ($choix -eq 'L') { Write-Info 'Ces copies seront remplacées par des liens : rien ne disparaît.' }
        else { Write-Info 'Réponse non reconnue : ces copies seront remplacées par des liens, rien ne disparaît.' }
    }

    # 5. Confirmation : une phrase tapée, que -Oui ne remplace jamais.
    $phrase = Get-PhraseDoublons -Mode $plan.Mode
    Write-Section 'Confirmation'
    if ($plan.Suppressions -gt 0) { Write-Alerte ('' + $plan.Liens + ' copie(s) deviendront des liens et ' + $plan.Suppressions + ' seront SUPPRIMÉE(S) définitivement.') }
    else { Write-Info ('' + $plan.Liens + ' copie(s) deviendront des liens. Aucun nom de fichier ne disparaît.') }
    Write-Alerte ('Pour lancer, tape exactement : ' + $phrase)
    Write-Info 'Toute autre réponse annule, et rien ne change.'
    if ($Oui) { Write-Info 'Le paramètre -Oui ne remplace pas cette phrase.' }
    $script:OuiAutomatique = $false
    if (-not (Confirm-Action -Question 'Confirmes-tu ?' -MotExact $phrase)) {
        Write-Info 'Annulé : rien n''a changé.'
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'confirmation non donnée : rien demandé à l''agent'
        Exit-Outil -Code 2 -SansPause:$SansPause
    }

    # 6. Demande à l'agent : l'empreinte du plan AFFICHÉ, le mode et la phrase tapée. Il refait le plan et compare.
    $depart = $null
    try { $depart = Invoke-ActionLocale -Racine $Racine -Action 'duplicates-apply' -DelaiSecondes 30 -Arguments @{ digest = $plan.Empreinte; same_folder = $plan.Mode; phrase = $phrase } } catch { }
    $parti = [bool]($depart -and $depart.Ok -and $depart.Resultat)
    if (-not $parti -and ((Get-ClasseEchecLocal $depart) -eq 'muet')) {
        # Pas de réponse dans le délai : l'agent a pu prendre la demande juste avant. Son état tranche.
        $apres = Get-EtatDoublons
        $parti = ($apres.Ok -and ([string](Get-ChampGestion $apres.Application 'state' '') -eq 'running'))
        if (-not $parti) {
            [void](Write-RefusAgent $depart)
            Write-Info 'L''agent n''a pas confirmé le départ. Relance cet outil : il dira si une opération est en cours.'
            Exit-Outil -Code 1 -SansPause:$SansPause
        }
    }
    if (-not $parti) {
        $code = Write-RefusAgent $depart
        Write-Info 'Aucun fichier n''a été modifié.'
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'demande refusée par l''agent'
        Exit-Outil -Code $code -SansPause:$SansPause
    }
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('opération lancée (' + $plan.Mode + ') : ' + $plan.NombreOperations + ' copie(s)')
    Write-Host ''
    Write-Ok ('Opération lancée : ' + $plan.NombreOperations + ' copie(s) à traiter. L''agent relit chaque fichier en entier avant d''y toucher.')
    Write-Info 'Suivi : un point toutes les 5 secondes, une ligne toutes les 30 secondes. Si tu fermes cet outil, l''opération continue dans l''agent.'

    # 7. Suivi, puis bilan.
    $code = Write-BilanApplication (Watch-Application)
    Exit-Outil -Code $code -SansPause:$SansPause
} catch {
    Close-Points
    Write-Erreur ('Erreur inattendue : ' + [string]$_.Exception.Message)
    Write-Info 'Cet outil ne modifie lui-même aucun fichier. Relance-le pour voir où en est l''agent.'
    Exit-Outil -Code 1 -SansPause:$SansPause
}
