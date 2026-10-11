<#
    Contrôler les doublons : demande à l'agent de chercher les fichiers IDENTIQUES (même contenu) parmi les
    moteurs, les modèles et les caches de ce Worker. Lecture seule : rien n'est supprimé, lié ni déplacé.
    Le contrôle tourne dans l'agent ; cet outil le lance, le suit, l'annule ou en montre le résultat.

    Usage :
      controler_doublons.ps1               montre le dernier contrôle, puis propose d'en lancer un [O/N]
      controler_doublons.ps1 -Oui          lance le contrôle sans demander (ou reprend le suivi de celui en cours)
      controler_doublons.ps1 -Etat         affiche l'état et sort
      controler_doublons.ps1 -Annuler      annule le contrôle en cours
      -Simulation : montre l'état, ne demande rien à l'agent.   -SansPause : pas de pause finale.
    Sous worker.exe (ALPINE_WORKER_GUI=1) l'outil ne suit jamais un contrôle jusqu'au bout : il le lance,
    montre l'état et rend la main. Relancé pendant un contrôle, il propose de l'annuler.
    Codes : 0 contrôle terminé, lancé ou état affiché ; 1 agent muet ou trop ancien, contrôle en échec ;
            2 annulé ou refusé.
#>
param(
    [string]$Racine = '',
    [switch]$SansPause,
    [switch]$Oui,
    [switch]$Simulation,
    [switch]$Etat,
    [switch]$Annuler
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'commun.ps1')
. (Join-Path $PSScriptRoot 'commun_cycle.ps1')
. (Join-Path $PSScriptRoot 'commun_gestion.ps1')
Initialize-ConsoleWorker -Titre 'Contrôler les doublons'
$NomOutil = 'controler_doublons'
# worker.exe : la sortie est un tube et une seule action tourne à la fois dans le panneau.
$SousPanneau = ($env:ALPINE_WORKER_GUI -eq '1')
$script:PointsOuverts = $false

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
    if ($Message -match 'suppression des doublons est déjà en cours') { return ('Attends sa fin : ' + (Get-NomEntree 20) + ' en montre l''avancement.') }
    if ($Message -match 'contrôle des doublons est (déjà )?en cours') { return 'C''est sans doute un contrôle lancé depuis le site (Mes Workers) : attends sa fin, puis relance cet outil.' }
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

function Write-Avancement {
    <# Une ligne ENTIÈRE par appel (jamais de ligne réécrite : sous worker.exe la sortie est un tube). #>
    param($Audit)
    $pourcent = ConvertTo-EntierGestion (Get-ChampGestion $Audit 'progress') 100
    $message = ([string](Get-ChampGestion $Audit 'message' '')).Trim()
    $ligne = 'Contrôle en cours : ' + $pourcent + ' %'
    if ($message) { $ligne += ' - ' + $message }
    Close-Points
    Write-Info $ligne
}

function Write-Rapport {
    <# Résultat du dernier contrôle fait depuis ce PC : des nombres, jamais un chemin. #>
    param($Rapport)
    if (-not [bool](Get-ChampGestion $Rapport 'present' $false)) {
        Write-Etat -Libelle 'Dernier contrôle' -Valeur 'aucun contrôle fait depuis ce PC pour l''instant' -Niveau info
        return
    }
    $date = ConvertTo-DateLocale (Get-ChampGestion $Rapport 'finished_at')
    $quand = 'date inconnue'
    if ($null -ne $date) { $quand = Format-DateEtat -Date $date }
    $age = Get-NombreDoublons (Get-ChampGestion $Rapport 'age_seconds')
    if ([bool](Get-ChampGestion $Rapport 'consumed' $false)) { $quand += ', déjà utilisé par ' + (Get-NomEntree 20) }
    elseif ($age -gt 86400) { $quand += ', vieux de plus de 24 heures' }
    $groupes = [int64](Get-NombreDoublons (Get-ChampGestion $Rapport 'groups'))
    $nettoyables = [int64](Get-NombreDoublons (Get-ChampGestion $Rapport 'in_scope_groups'))
    $hors = [int64](Get-NombreDoublons (Get-ChampGestion $Rapport 'out_of_scope_groups'))
    $octets = Get-NombreDoublons (Get-ChampGestion $Rapport 'in_scope_reclaimable_bytes')
    Write-Etat -Libelle 'Dernier contrôle' -Valeur $quand -Niveau info
    Write-Etat -Libelle 'Fichiers identiques' -Valeur ('' + $groupes + ' groupe(s), dont ' + $nettoyables + ' dans le périmètre nettoyable') -Niveau info
    Write-Etat -Libelle 'Place récupérable' -Valeur ((Format-Taille $octets) + ' (modèles ComfyUI du Worker seulement)') -Niveau info
    if ($hors -gt 0) { Write-Etat -Libelle 'Hors périmètre' -Valeur ('' + $hors + ' groupe(s) : moteurs, caches, autres dossiers ; jamais touchés') -Niveau info }
    if (-not [bool](Get-ChampGestion $Rapport 'complete' $false)) {
        # Pas un échec : sur un Worker récent, les dossiers de cache n'existent pas encore.
        Write-Etat -Libelle 'Emplacements non lus' -Valeur ('' + [int64](Get-NombreDoublons (Get-ChampGestion $Rapport 'errors')) + ' (souvent des dossiers pas encore créés) : le résultat reste utilisable') -Niveau info
    }
}

function Write-BilanControle {
    <# Fin d'un contrôle (ou rappel du dernier) : la phrase de résultat, puis la suite possible. #>
    param($Rapport)
    $groupes = [int64](Get-NombreDoublons (Get-ChampGestion $Rapport 'groups'))
    $nettoyables = [int64](Get-NombreDoublons (Get-ChampGestion $Rapport 'in_scope_groups'))
    $octets = Get-NombreDoublons (Get-ChampGestion $Rapport 'in_scope_reclaimable_bytes')
    $nonLus = [int64](Get-NombreDoublons (Get-ChampGestion $Rapport 'errors'))
    Write-Ok ('' + $groupes + ' groupe(s) identiques, dont ' + $nettoyables + ' dans le périmètre nettoyable (' + (Format-Taille $octets) + ' récupérables)')
    if (-not [bool](Get-ChampGestion $Rapport 'complete' $false)) {
        # Pas un échec : sur un Worker récent, les dossiers de cache n'existent pas encore.
        Write-Info ('' + $nonLus + ' emplacement(s) n''ont pas pu être lus : le plus souvent des dossiers qui n''existent pas encore (caches d''un Worker récent, moteur jamais lancé), parfois un fichier occupé. Tout ce qui a pu être lu a été comparé ; le résultat reste utilisable.')
    }
    Write-Info 'Aucun fichier n''a été modifié.'
    if ($nettoyables -gt 0) { Write-Conseil ('Pour libérer la place : ' + (Get-NomEntree 20)) }
    else { Write-Info 'Rien à libérer parmi les modèles ComfyUI de ce Worker.' }
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message ('résultat : ' + $groupes + ' groupe(s), ' + $nettoyables + ' nettoyable(s), ' + [int64]$octets + ' octets récupérables, ' + $nonLus + ' emplacement(s) non lus')
}

function Write-FinControle {
    <# Affiche l'issue d'un contrôle d'après le dernier état lu. Rend le code de sortie. #>
    param($EtatLu)
    if (-not $EtatLu -or -not $EtatLu.Ok) {
        $brute = $null
        if ($EtatLu) { $brute = $EtatLu.Reponse }
        [void](Write-RefusAgent $brute)
        Write-Info 'Le contrôle continue peut-être dans l''agent : relance cet outil pour le savoir.'
        return 1
    }
    $situation = [string](Get-ChampGestion $EtatLu.Audit 'state' '')
    Write-Host ''
    switch ($situation) {
        'done' {
            Write-BilanControle $EtatLu.Rapport
            return 0
        }
        'cancelled' {
            Write-Alerte 'Contrôle annulé avant sa fin. Aucun fichier n''a été modifié.'
            Write-Info 'Le résultat du contrôle précédent, s''il y en a un, reste en place.'
            return 2
        }
        'failed' {
            $erreur = ([string](Get-ChampGestion $EtatLu.Audit 'error' '')).Trim()
            if (-not $erreur) { $erreur = 'Le contrôle s''est arrêté sur une erreur.' }
            Write-Erreur $erreur
            Write-Info 'Aucun fichier n''a été modifié. Le résultat du contrôle précédent, s''il y en a un, reste en place.'
            Write-Conseil ('Relance cet outil ; si l''échec se répète, regarde ' + (Get-NomEntree 3) + '.')
            return 1
        }
        'running' {
            Write-Info ('Le contrôle continue en arrière-plan. Relance ' + (Get-NomEntree 19) + ' pour voir l''avancement ou l''annuler.')
            Write-Info 'Aucun fichier n''a été modifié.'
            return 0
        }
    }
    Write-Alerte 'L''agent a redémarré pendant le contrôle : il s''est arrêté sans résultat. Aucun fichier n''a été modifié.'
    Write-Conseil 'Relance cet outil pour refaire le contrôle.'
    return 1
}

function Watch-Controle {
    <#
        Suit le contrôle : état relu toutes les 3 s, un point toutes les 10 s, une ligne entière (pourcentage
        et message de l'agent) toutes les 60 s. Rend le dernier état lu ; cinq lectures manquées de suite :
        l'agent ne répond plus, on s'arrête là.
    #>
    param([int]$PauseSecondes = 3, [int]$PointSecondes = 10, [int]$LigneSecondes = 60)
    $dernierPoint = Get-Date
    $derniereLigne = Get-Date
    $manquees = 0
    while ($true) {
        Start-Sleep -Seconds $PauseSecondes
        $lu = Get-EtatDoublons
        if (-not $lu.Ok) {
            $manquees++
            if ($manquees -ge 5) { Close-Points; return $lu }
            continue
        }
        $manquees = 0
        if ([string](Get-ChampGestion $lu.Audit 'state' '') -ne 'running') { Close-Points; return $lu }
        $maintenant = Get-Date
        if (($maintenant - $derniereLigne).TotalSeconds -ge $LigneSecondes) {
            Write-Avancement $lu.Audit
            $derniereLigne = $maintenant; $dernierPoint = $maintenant
        }
        elseif (($maintenant - $dernierPoint).TotalSeconds -ge $PointSecondes) {
            Write-Host '.' -NoNewline -ForegroundColor DarkGray
            $script:PointsOuverts = $true
            $dernierPoint = $maintenant
        }
    }
}

function Stop-Controle {
    <# « duplicates-cancel », puis 30 s au plus pour lire l'état « annulé ». Rend le code de sortie. #>
    $reponse = $null
    try { $reponse = Invoke-ActionLocale -Racine $Racine -Action 'duplicates-cancel' -DelaiSecondes 15 } catch { }
    if (-not ($reponse -and $reponse.Ok -and $reponse.Resultat)) { return (Write-RefusAgent $reponse) }
    Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'annulation du contrôle demandée'
    if (-not [bool](Get-ChampGestion $reponse.Resultat 'cancel_requested' $false)) {
        Write-Info 'Le contrôle venait de se terminer : il n''y avait plus rien à annuler.'
        return (Write-FinControle (Get-EtatDoublons))
    }
    Write-Info 'Annulation demandée : le contrôle s''arrête à sa prochaine étape.'
    $limite = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $limite) {
        Start-Sleep -Seconds 2
        $lu = Get-EtatDoublons
        if ($lu.Ok -and ([string](Get-ChampGestion $lu.Audit 'state' '') -ne 'running')) { return (Write-FinControle $lu) }
    }
    Write-Host ''
    Write-Alerte 'L''annulation est demandée, mais le contrôle n''a pas encore atteint une étape où s''arrêter (lecture d''un gros dossier).'
    Write-Info 'Il s''arrêtera seul, sans rien modifier. Relance cet outil pour le vérifier.'
    return 2
}

try { $Racine = Get-RacineWorker -Racine $Racine } catch { Write-Erreur ([string]$_.Exception.Message); Exit-Outil -Code 1 -SansPause:$SansPause }

try {
    Write-Explication -Titre 'CONTRÔLER LES DOUBLONS' -Fait @(
        'Demande à l''agent de chercher les fichiers IDENTIQUES (même contenu, octet pour octet) parmi les moteurs, les modèles et les caches de ce Worker, à partir de 1 Mio.',
        'Montre le résultat : combien de groupes de fichiers identiques, et la place récupérable parmi les modèles ComfyUI du Worker.',
        'Le contrôle tourne dans l''agent : tu peux fermer cet outil, il continue. Relance l''outil pour voir où il en est, ou pour l''annuler.'
    ) -NeFaitPas @(
        'Il ne supprime, ne déplace et ne modifie AUCUN fichier.',
        ('Il ne libère aucune place : c''est le rôle de ' + (Get-NomEntree 20) + ', qui demande un contrôle de moins de 24 heures.'),
        'Il n''arrête ni les calculs ni les moteurs. Il lit beaucoup le disque : un calcul en cours peut en être ralenti.'
    ) -Exige @(
        'Un Worker en marche, avec un agent à jour.',
        'Du temps : de quelques minutes à plus d''une heure, selon la taille des modèles et la vitesse du disque.',
        'Aucun droit administrateur.'
    )

    $agent = Get-EtatAgent -Racine $Racine
    if (-not $agent.Vivant -or -not $agent.Capable) {
        Write-RefusRelais -Resultat $agent
        Write-Info 'Rien n''a été demandé. Aucun fichier n''a été modifié.'
        Exit-Outil -Code 1 -SansPause:$SansPause
    }

    $lu = Get-EtatDoublons
    if (-not $lu.Ok) {
        $code = Write-RefusAgent $lu.Reponse
        Exit-Outil -Code $code -SansPause:$SansPause
    }
    $situation = [string](Get-ChampGestion $lu.Audit 'state' '')
    $enCours = ($situation -eq 'running')

    Write-Section 'État'
    if ($enCours) { Write-Avancement $lu.Audit }
    Write-Rapport $lu.Rapport
    if ($situation -eq 'failed') { Write-Etat -Libelle 'Dernière tentative' -Valeur 'en échec, sans résultat : le contrôle précédent reste en place' -Niveau alerte }
    elseif ($situation -eq 'cancelled') { Write-Etat -Libelle 'Dernière tentative' -Valeur 'annulée avant sa fin : le contrôle précédent reste en place' -Niveau info }
    if ([string](Get-ChampGestion $lu.Application 'state' '') -eq 'running') {
        Write-Etat -Libelle 'Suppression des doublons' -Valeur ('en cours : suis-la avec ' + (Get-NomEntree 20)) -Niveau alerte
    }

    # ------------------------------------------------------------------ annuler, ou seulement regarder
    if ($Annuler) {
        if (-not $enCours) {
            Write-Host ''
            Write-Info 'Aucun contrôle n''est en cours : rien à annuler. Aucun fichier n''a été modifié.'
            Exit-Outil -Code 0 -SansPause:$SansPause
        }
        if ($Simulation) {
            Write-Host ''
            Write-Info '[simulation] L''annulation n''a pas été demandée : le contrôle continue.'
            Exit-Outil -Code 0 -SansPause:$SansPause
        }
        $code = Stop-Controle
        Exit-Outil -Code $code -SansPause:$SansPause
    }
    if ($Etat -or $Simulation) {
        Write-Host ''
        if ($Simulation) { Write-Info '[simulation] Rien n''a été demandé à l''agent : aucun contrôle lancé, aucun contrôle annulé.' }
        if ($enCours) { Write-Info ('Le contrôle continue en arrière-plan. Relance ' + (Get-NomEntree 19) + ' pour voir l''avancement ou l''annuler.') }
        Write-Info 'Aucun fichier n''a été modifié.'
        Exit-Outil -Code 0 -SansPause:$SansPause
    }

    # ------------------------------------------------------------------ un contrôle est déjà en cours
    if ($enCours) {
        $choix = 'S'
        if (-not $Oui) {
            Write-Host ''
            if ($SousPanneau) { Write-Alerte '[A] annuler le contrôle   [Q] quitter (le contrôle continue)' }
            else { Write-Alerte '[S] suivre l''avancement   [A] annuler le contrôle   [Q] quitter (le contrôle continue)' }
            $choix = (Read-Reponse).ToUpper()
        }
        if ($choix -eq 'A') {
            $code = Stop-Controle
            Exit-Outil -Code $code -SansPause:$SansPause
        }
        if ($SousPanneau -or $choix -ne 'S') {
            Write-Info ('Le contrôle continue en arrière-plan. Relance ' + (Get-NomEntree 19) + ' pour voir l''avancement ou l''annuler.')
            Write-Info 'Aucun fichier n''a été modifié.'
            Exit-Outil -Code 0 -SansPause:$SansPause
        }
    }
    # ------------------------------------------------------------------ lancer un contrôle
    else {
        if (-not $Oui) {
            Write-Host ''
            Write-Info 'Lancer un nouveau contrôle ? Il peut durer de quelques minutes à plus d''une heure selon le disque.'
            Write-Info 'Réponds O pour le lancer, N pour ne rien faire.'
        }
        if (-not (Confirm-Action -Question 'Lancer un nouveau contrôle ?' -Oui:$Oui)) {
            Write-Info 'Annulé : aucun contrôle lancé. Aucun fichier n''a été modifié.'
            Exit-Outil -Code 2 -SansPause:$SansPause
        }
        $depart = $null
        try { $depart = Invoke-ActionLocale -Racine $Racine -Action 'duplicates-audit' -DelaiSecondes 15 } catch { }
        if (-not ($depart -and $depart.Ok -and $depart.Resultat)) {
            $code = Write-RefusAgent $depart
            Write-Info 'Aucun contrôle lancé. Aucun fichier n''a été modifié.'
            Exit-Outil -Code $code -SansPause:$SansPause
        }
        Write-JournalOutil -Racine $Racine -Outil $NomOutil -Message 'contrôle des doublons lancé'
        Write-Host ''
        Write-Ok 'Contrôle lancé. Il ne modifie aucun fichier.'
        if ($SousPanneau) {
            # Le panneau ne mène qu'une action à la fois : on montre où en est le contrôle et on rend la main.
            Start-Sleep -Seconds 2
            $apres = Get-EtatDoublons
            if ($apres.Ok -and ([string](Get-ChampGestion $apres.Audit 'state' '') -ne 'running')) {
                $code = Write-FinControle $apres
                Exit-Outil -Code $code -SansPause:$SansPause
            }
            if ($apres.Ok) { Write-Avancement $apres.Audit }
            Write-Info ('Le contrôle continue en arrière-plan. Relance ' + (Get-NomEntree 19) + ' pour voir l''avancement ou l''annuler.')
            Exit-Outil -Code 0 -SansPause:$SansPause
        }
    }

    # ------------------------------------------------------------------ suivi (console seulement)
    Write-Info 'Suivi du contrôle : un point toutes les 10 secondes, une ligne par minute.'
    Write-Info ('Tu peux fermer cette fenêtre : le contrôle continue dans l''agent. Relance ' + (Get-NomEntree 19) + ' pour reprendre le suivi ou l''annuler.')
    $final = Watch-Controle
    $code = Write-FinControle $final
    Exit-Outil -Code $code -SansPause:$SansPause
} catch {
    Close-Points
    Write-Erreur ('Erreur inattendue : ' + [string]$_.Exception.Message)
    Exit-Outil -Code 1 -SansPause:$SansPause
}
