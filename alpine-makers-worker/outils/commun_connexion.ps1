<#
    Connexion du propriétaire au menu du Worker (MENU-WORKER.bat) : EXACTEMENT le chemin de worker.exe.

    À charger APRÈS commun.ps1 et commun_gestion.ps1 (commun_gestion.ps1 le charge lui-même quand il est présent).

    Chemin d'une connexion :
      1. le menu demande pseudo, mot de passe (saisie masquée) puis, si le compte l'a activée, le code 2FA (masqué) ;
      2. ces valeurs ne sont JAMAIS passées en ligne de commande ni écrites dans un journal : elles vont dans
         runtime\local-control\<32 hex>.args.json, créé d'un coup (CreateNew) avec une ACL fermée (ce compte, SYSTEM,
         Administrateurs, et le compte du service AlpineWorker s'il en a un autre), lu UNE fois puis effacé par l'agent ;
         le menu l'efface lui-même s'il est encore là ;
      3. seul le nom de ce fichier (32 hexadécimaux) part sur la ligne de commande de local_control.py : l'action
         locale « login » de l'agent signe la demande avec la clé Ed25519 du Worker (restée dans config\, jamais lue
         ici) et appelle la route du site self/login, comme worker.exe ;
      4. le site juge seul : propriétaire, co-propriétaire autorisé et ses droits, 2FA, verrou après trois échecs
         (déblocage par le lien reçu par e-mail ou Mes Workers), notifications au propriétaire et aux administrateurs ;
      5. le jeton de session rendu ne vit qu'en mémoire de CE processus ($global:AlpineSessionWorker). Chaque action
         qui modifie quelque chose sur le site le joint par un fichier d'arguments du même type ; le site refuse
         l'action sans lui (agent qui annonce worker_menu_session_v1) et relit les droits du co-propriétaire ;
      6. appareil de confiance (worker_login_trust_v1) : après un code réussi et « O » à « Ne plus demander le code sur
         ce PC pendant 7 jours ? », le jeton rendu par le site est gardé chiffré (DPAPI, ce compte Windows) et part
         ensuite à la place du code, par le même fichier d'arguments (section « Appareil de confiance » ci-dessous).

    Ce fichier ne contient aucune adresse, aucune clé, aucun identifiant du site ni de base de données : tout ce qui
    touche au site est demandé à l'agent.
    Mode hors ligne (site ou agent injoignable) : seules les entrées locales sûres restent proposées
    ($script:EntreesHorsLigne) ; rien de ce qui touche au site ou détruit des données ne s'ouvre sans connexion.
#>

$script:MessageVerrouConnexion = 'Application bloquée après 3 échecs : utilise le lien reçu par e-mail ou Mes Workers sur le site pour la débloquer.'
$script:MessageRefusConnexion = 'Ce compte n''est pas le propriétaire de ce Worker.'
$script:CodeTotpRequis = 'TOTP_REQUIRED'
$script:CodeVerrou = 'WORKER_EXE_LOCKED'
$script:CodeRefus = 'WORKER_EXE_REFUSED'
$script:CodesSessionAbsente = @('WORKER_EXE_SESSION_REQUIRED', 'WORKER_EXE_SESSION_INVALID')
# Droits worker.exe qu'un propriétaire accorde à un co-propriétaire (EXE_RIGHTS du site).
$script:DroitsWorker = @('status', 'installations', 'engines', 'jobs', 'equipment', 'maintenance', 'console')
# Entrées ouvertes SANS connexion (site ou agent injoignable) : diagnostic en lecture, remise en route de l'agent et
# des moteurs, ménage léger. Jamais : partage, mise à jour, réassociation, sauvegarde, démarrage automatique, zone rouge.
# 24 (Réparer) reste ouvert : ses réparations locales servent justement quand le site ne répond pas ; sa demande au
# site, elle, exige la session (refusée par le site sans elle).
$script:EntreesHorsLigne = @(1, 2, 3, 4, 5, 6, 9, 10, 11, 12, 14, 16, 19, 24)
$script:SessionControleMinutes = 5      # au-delà, une action sensible fait d'abord contrôler la session par le site
$script:SidsArgumentsWorker = $null

if (-not (Get-Variable -Name AlpineSessionWorker -Scope Global -ErrorAction SilentlyContinue)) { $global:AlpineSessionWorker = $null }
if (-not (Get-Variable -Name AlpineModeHorsLigne -Scope Global -ErrorAction SilentlyContinue)) { $global:AlpineModeHorsLigne = $false }

# ---------------------------------------------------------------------------
# Session en mémoire
# ---------------------------------------------------------------------------

function Test-JetonSessionValide {
    <# Forme d'un jeton de session (base64url, 16 à 200 caractères) : rien d'autre n'est jamais écrit dans un fichier. #>
    param([string]$Jeton)
    return ([string]$Jeton -cmatch '^[A-Za-z0-9_-]{16,200}$')
}

function Set-SessionWorker {
    param([string]$Jeton, [string]$Role, [string[]]$Droits = @(), [string]$Utilisateur = '', [string]$Source = 'menu')
    if ($Role -ne 'co_owner') { $Role = 'owner' }
    $propres = @()
    foreach ($d in @($Droits)) { if ([string]$d -cmatch '^[a-z*_]{1,40}$' -and $propres -notcontains [string]$d) { $propres += [string]$d } }
    if ($Role -eq 'owner' -and $propres.Count -eq 0) { $propres = @('*') }
    if ($Jeton -and -not (Test-JetonSessionValide $Jeton)) { $Jeton = '' }
    $global:AlpineSessionWorker = [pscustomobject]@{
        Jeton = [string]$Jeton; Role = $Role; Droits = $propres; Utilisateur = ([string]$Utilisateur).Substring(0, [Math]::Min(120, ([string]$Utilisateur).Length))
        Source = $Source; Depuis = Get-Date; DernierControle = Get-Date
    }
    $global:AlpineModeHorsLigne = $false
}

function Clear-SessionWorker {
    $global:AlpineSessionWorker = $null
}

function Get-SessionWorker { return $global:AlpineSessionWorker }

function Import-SessionTransmise {
    <#
        worker.exe lance « menu_worker.ps1 -Choix N » après SA connexion : il transmet le jeton par l'environnement du
        processus (ALPINE_WORKER_SESSION, jamais par la ligne de commande), avec le rôle et les droits (non secrets).
        La variable est retirée aussitôt de l'environnement : les processus lancés ensuite (Python…) ne l'héritent pas.
    #>
    $jeton = [string]$env:ALPINE_WORKER_SESSION
    $role = [string]$env:ALPINE_WORKER_SESSION_ROLE
    $droits = @(([string]$env:ALPINE_WORKER_SESSION_RIGHTS).Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    foreach ($nom in @('ALPINE_WORKER_SESSION', 'ALPINE_WORKER_SESSION_ROLE', 'ALPINE_WORKER_SESSION_RIGHTS')) {
        try { [Environment]::SetEnvironmentVariable($nom, $null, 'Process') } catch { }
    }
    if ((Test-JetonSessionValide $jeton) -and -not $global:AlpineSessionWorker) {
        Set-SessionWorker -Jeton $jeton -Role $role -Droits $droits -Source 'worker.exe'
        return $true
    }
    return $false
}

function Test-DroitWorker {
    <# Droit worker.exe de la session courante. Propriétaire : tout. Co-propriétaire : '*' ou le droit nommé. '' = propriétaire seul. #>
    param([string]$Droit)
    $s = $global:AlpineSessionWorker
    if (-not $s) { return $false }
    if ($s.Role -ne 'co_owner') { return $true }
    if (-not $Droit) { return $false }
    return (@($s.Droits) -contains '*' -or @($s.Droits) -contains $Droit)
}

function Get-DroitEntreeMenu {
    <# Droit exigé d'un co-propriétaire pour une entrée du menu : MÊME table que ToolRight de worker.exe. '' = propriétaire seul. #>
    param([int]$Numero, [string]$Groupe = '')
    if ($Groupe -eq 'rouge') { return '' }
    switch ($Numero) {
        { $_ -in 1, 2, 3, 4 } { return 'status' }          # État complet, Jobs bloquants, Journaux, Rapport
        { $_ -in 5, 6 } { return 'maintenance' }           # Reconnecter, Actualiser les adresses IP
        9 { return 'engines' }                             # Moteurs
        { $_ -in 10, 11, 12 } { return 'maintenance' }     # Redémarrer l'agent, Éteindre, Rallumer
        { $_ -in 14, 16 } { return 'maintenance' }         # Nettoyage léger, Libérer la mémoire
        { $_ -in 18, 23, 24 } { return 'maintenance' }     # Contrôle total, Mettre à jour, Réparer
        19 { return 'status' }                             # Contrôler les doublons (ne supprime rien)
        21 { return 'jobs' }                               # Arrêter tous les jobs
        26 { return 'jobs' }                               # Jobs et ressources
    }
    return ''                                              # 7, 8, 13, 15, 22, zone rouge, entrées du PC du propriétaire
}

function Get-LibelleDroitWorker {
    param([string]$Droit)
    switch ($Droit) {
        'status' { return 'état, journaux et diagnostic' }
        'installations' { return 'installations' }
        'engines' { return 'moteurs et modules' }
        'jobs' { return 'arrêt des jobs' }
        'equipment' { return 'équipements partagés' }
        'maintenance' { return 'maintenance' }
        'console' { return 'console et outils avancés' }
    }
    return $Droit
}

function Get-ResumeSessionWorker {
    <# « propriétaire X » ou « co-propriétaire X · droits : … » ; '' sans session. #>
    $s = $global:AlpineSessionWorker
    if (-not $s) { return '' }
    $qui = $s.Utilisateur
    if (-not $qui) { $qui = 'connecté par ' + $s.Source }
    if ($s.Role -ne 'co_owner') { return ('propriétaire ' + $qui) }
    $libelles = @()
    foreach ($d in $script:DroitsWorker) { if (Test-DroitWorker $d) { $libelles += (Get-LibelleDroitWorker $d) } }
    if ($libelles.Count -eq 0) { $libelles = @('aucun') }
    return ('co-propriétaire ' + $qui + ' · droits : ' + ($libelles -join ', '))
}

function Test-EntreeAutoriseeSession {
    <#
        L'entrée peut-elle être ouverte dans CET état de connexion ?
          - menu lancé par worker.exe (ALPINE_WORKER_GUI=1) : worker.exe a déjà filtré selon les droits ;
          - Worker non associé : aucune connexion possible, tout reste ouvert (association d'abord) ;
          - mode hors ligne : seulement $script:EntreesHorsLigne ;
          - connecté : propriétaire = tout ; co-propriétaire = la table de worker.exe.
        Le site reste le juge de toute action qui le touche : ce filtre n'en est que l'affichage.
    #>
    param([int]$Numero, [string]$Groupe = '', [bool]$ConnexionExigee = $true)
    if ($env:ALPINE_WORKER_GUI -eq '1') { return $true }
    if (-not $ConnexionExigee) { return $true }
    if ($global:AlpineModeHorsLigne -or -not $global:AlpineSessionWorker) { return ($script:EntreesHorsLigne -contains $Numero) }
    return (Test-DroitWorker (Get-DroitEntreeMenu -Numero $Numero -Groupe $Groupe))
}

# ---------------------------------------------------------------------------
# Fichiers d'arguments : seul passage des secrets vers l'agent
# ---------------------------------------------------------------------------

function Get-SidsAccesArguments {
    <# Comptes qui peuvent lire un fichier d'arguments : ce compte, SYSTEM, Administrateurs, et le compte du service AlpineWorker. #>
    param([string]$Racine)
    if ($script:SidsArgumentsWorker) { return $script:SidsArgumentsWorker }
    $sids = New-Object System.Collections.ArrayList
    [void]$sids.Add([Security.Principal.WindowsIdentity]::GetCurrent().User)
    [void]$sids.Add([Security.Principal.SecurityIdentifier]::new([Security.Principal.WellKnownSidType]::LocalSystemSid, $null))
    [void]$sids.Add([Security.Principal.SecurityIdentifier]::new([Security.Principal.WellKnownSidType]::BuiltinAdministratorsSid, $null))
    try {
        # Registre plutôt que WMI : lecture instantanée, sans service WMI qui peut faire attendre la connexion.
        $compte = ''
        try { $compte = [string](Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Services\AlpineWorker' -Name 'ObjectName' -ErrorAction Stop).ObjectName } catch { $compte = '' }
        if ($compte -and $compte -notmatch '^(?i)(LocalSystem|NT AUTHORITY\\SYSTEM|\.\\LocalSystem)$') {
            if ($compte.StartsWith('.\')) { $compte = $env:COMPUTERNAME + '\' + $compte.Substring(2) }
            $sid = ([Security.Principal.NTAccount]::new($compte)).Translate([Security.Principal.SecurityIdentifier])
            if (@($sids | Where-Object { $_.Value -eq $sid.Value }).Count -eq 0) { [void]$sids.Add($sid) }
        }
    } catch { }
    $script:SidsArgumentsWorker = $sids.ToArray()
    return $script:SidsArgumentsWorker
}

function ConvertFrom-SecretWorker {
    <# SecureString -> texte, le temps de l'écrire dans le fichier d'arguments. La copie non gérée est effacée aussitôt. #>
    param([Security.SecureString]$Secret)
    if ($null -eq $Secret -or $Secret.Length -eq 0) { return '' }
    $pointeur = [IntPtr]::Zero
    try {
        $pointeur = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secret)
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointeur)
    } finally {
        if ($pointeur -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointeur) }
    }
}

function Get-CheminArgumentsWorker {
    param([string]$Racine, [string]$Jeton)
    if ($Jeton -cnotmatch '^[0-9a-f]{32}$') { throw 'Nom de fichier d''arguments invalide.' }
    return (Join-Path (Join-Path $Racine 'runtime\local-control') ($Jeton + '.args.json'))
}

function Write-FichierArgumentsWorker {
    <#
        Écrit runtime\local-control\<32 hex>.args.json et rend les 32 hexadécimaux (paramètre « args_file » de l'agent).
        -Contenu : valeurs non secrètes ; -Secrets : SecureString, convertis ici au dernier moment. Le fichier est créé
        d'un coup (jamais par-dessus un fichier ou un lien existant) avec une ACL fermée, sans héritage ; le tampon
        d'octets est remis à zéro. Refuse un lien ou une jonction sur runtime\ ou local-control\.
    #>
    param([string]$Racine, [hashtable]$Contenu = @{}, [hashtable]$Secrets = @{})
    $dossierRuntime = Join-Path $Racine 'runtime'
    $dossier = Join-Path $dossierRuntime 'local-control'
    foreach ($d in @($dossierRuntime, $dossier)) {
        if ((Test-Path -LiteralPath $d) -and (([IO.File]::GetAttributes($d) -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw 'Lien ou jonction sur le dossier des demandes locales : connexion refusée.'
        }
    }
    if (-not (Test-Path -LiteralPath $dossier -PathType Container)) { New-Item -ItemType Directory -Path $dossier -Force | Out-Null }
    $jeton = [Guid]::NewGuid().ToString('N')
    $chemin = Get-CheminArgumentsWorker -Racine $Racine -Jeton $jeton
    $securite = New-Object Security.AccessControl.FileSecurity
    $securite.SetAccessRuleProtection($true, $false)
    foreach ($sid in @(Get-SidsAccesArguments -Racine $Racine)) {
        $securite.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, [Security.AccessControl.FileSystemRights]::FullControl, [Security.AccessControl.AccessControlType]::Allow))
    }
    $donnees = @{}
    foreach ($cle in @($Contenu.Keys)) { $donnees[[string]$cle] = $Contenu[$cle] }
    $octets = $null; $flux = $null; $cree = $false
    try {
        foreach ($cle in @($Secrets.Keys)) { $donnees[[string]$cle] = ConvertFrom-SecretWorker -Secret $Secrets[$cle] }
        $octets = (New-Object System.Text.UTF8Encoding($false)).GetBytes([string](ConvertTo-Json -InputObject $donnees -Compress))
        $flux = [IO.FileStream]::new($chemin, [IO.FileMode]::CreateNew, [Security.AccessControl.FileSystemRights]::FullControl, [IO.FileShare]::None, 4096, [IO.FileOptions]::None, $securite)
        $cree = $true
        $flux.Write($octets, 0, $octets.Length)
        $flux.Flush()
    } catch {
        if ($flux) { try { $flux.Dispose() } catch { }; $flux = $null }
        if ($cree) { try { [IO.File]::Delete($chemin) } catch { } }
        throw ('Fichier d''arguments non écrit : ' + $_.Exception.GetType().Name + '.')
    } finally {
        if ($flux) { $flux.Dispose() }
        if ($octets) { [Array]::Clear($octets, 0, $octets.Length) }
        foreach ($cle in @($donnees.Keys)) { $donnees[$cle] = $null }
        $donnees.Clear()
    }
    return $jeton
}

function Remove-FichierArgumentsWorker {
    <# Efface le fichier s'il est encore là (l'agent l'a normalement effacé à la lecture). Ne lève jamais. #>
    param([string]$Racine, [string]$Jeton)
    if (-not $Jeton) { return }
    try {
        $chemin = Get-CheminArgumentsWorker -Racine $Racine -Jeton $Jeton
        if ([IO.File]::Exists($chemin)) { [IO.File]::Delete($chemin) }
    } catch { }
}

function Invoke-ActionSecreteWorker {
    <#
        Action locale dont un paramètre est secret : le contenu passe par un fichier d'arguments (args_file), jamais
        par --params-b64. -Parametres : autres paramètres, non secrets. Le fichier est effacé ici quoi qu'il arrive.
    #>
    param([string]$Racine, [string]$Action, [hashtable]$Contenu = @{}, [hashtable]$Secrets = @{}, [hashtable]$Parametres = @{}, [int]$DelaiSecondes = 30)
    $jeton = ''
    try {
        $jeton = Write-FichierArgumentsWorker -Racine $Racine -Contenu $Contenu -Secrets $Secrets
        $arguments = @{ args_file = $jeton }
        foreach ($cle in @($Parametres.Keys)) { $arguments[[string]$cle] = $Parametres[$cle] }
        return (Invoke-ActionLocale -Racine $Racine -Action $Action -DelaiSecondes $DelaiSecondes -Arguments $arguments)
    } catch {
        return [pscustomobject]@{ Ok = $false; Resultat = $null; Erreur = [string]$_.Exception.Message; Code = -1; NonSupporte = $false; DelaiDepasse = $false; EnCours = $false; EnAttente = $false }
    } finally {
        Remove-FichierArgumentsWorker -Racine $Racine -Jeton $jeton
    }
}

function New-FichierSessionWorker {
    <# Fichier {"session_token"} de la session courante, pour un relais qui modifie ; '' sans session (le site décidera). #>
    param([string]$Racine)
    $s = $global:AlpineSessionWorker
    if (-not $s -or -not (Test-JetonSessionValide $s.Jeton)) { return '' }
    return (Write-FichierArgumentsWorker -Racine $Racine -Contenu @{ session_token = [string]$s.Jeton })
}

# ---------------------------------------------------------------------------
# Appareil de confiance : « Ne plus demander le code sur ce PC pendant 7 jours » (capacité worker_login_trust_v1)
# ---------------------------------------------------------------------------
# Après une connexion réussie AVEC le code et « O » à la question, le site rend « <id 32 hex>.<secret> » et son
# échéance. Le jeton est gardé chiffré par DPAPI pour l'utilisateur Windows qui lance le menu (ProtectedData,
# CurrentUser, entropie propre au client et au compte) dans runtime\local-control\trust-menu-<empreinte>.bin : créé
# d'un coup, ACL fermée sans héritage (ce compte, SYSTEM, Administrateurs ; ni le service du Worker, ni « Utilisateurs »),
# jamais écrit ni lu à travers un lien, nom sans le pseudo (SHA-256 du SID Windows et du pseudo : MÊME calcul que
# LoginTrust de worker.exe, qui garde trust-exe-<empreinte>.bin). Il ne part que dans le fichier d'arguments de « login »,
# jamais en ligne de commande ni dans un journal. Effacé quand le site le refuse (device_trust_rejected), à son échéance,
# s'il est illisible, ou par « Oublier ce PC » (touche O du menu, proposé aussi à la déconnexion L).

$script:ClientConfiance = 'menu'
$script:ClientsConfiance = @('exe', 'menu')
$script:DomaineConfiance = 'alpine-worker-trust-v1'
$script:OctetsConfianceMax = 4096

function Test-JetonConfianceValide {
    <# Forme d'un jeton d'appareil de confiance : « <id 32 hex>.<secret base64url> », 200 caractères au plus. #>
    param([string]$Jeton)
    return (([string]$Jeton).Length -le 200 -and ([string]$Jeton -cmatch '\A[0-9A-Fa-f]{32}\.[A-Za-z0-9_-]{16,167}\z'))
}

function Test-DpapiConfiance {
    <# DPAPI (ProtectedData, System.Security.dll) disponible ? Sans lui, rien n'est lu, écrit ni effacé. #>
    try { Add-Type -AssemblyName System.Security -ErrorAction Stop; return $true } catch { return $false }
}

function Get-EmpreinteConfiance {
    <# 32 hexadécimaux : SHA-256 du domaine, du SID Windows de ce compte et du pseudo en minuscules (le nom du fichier ne révèle pas le pseudo). #>
    param([string]$Utilisateur)
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $texte = $script:DomaineConfiance + "`n" + $sid + "`n" + ([string]$Utilisateur).Trim().ToLowerInvariant()
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $empreinte = $sha.ComputeHash((New-Object System.Text.UTF8Encoding($false)).GetBytes($texte)) } finally { $sha.Dispose() }
    return (-join ($empreinte[0..15] | ForEach-Object { $_.ToString('x2') }))
}

function Get-CheminConfianceWorker {
    <# runtime\local-control\trust-<client>-<empreinte>.bin ; -Client 'menu' (ce menu) ou 'exe' (worker.exe). #>
    param([string]$Racine, [string]$Utilisateur, [string]$Client = $script:ClientConfiance)
    if ($script:ClientsConfiance -cnotcontains $Client) { throw 'Client de connexion inconnu.' }
    return (Join-Path (Join-Path $Racine 'runtime\local-control') ('trust-' + $Client + '-' + (Get-EmpreinteConfiance -Utilisateur $Utilisateur) + '.bin'))
}

function Get-EntropieConfiance {
    param([string]$Utilisateur, [string]$Client = $script:ClientConfiance)
    return ,((New-Object System.Text.UTF8Encoding($false)).GetBytes($script:DomaineConfiance + '|' + $Client + '|' + (Get-EmpreinteConfiance -Utilisateur $Utilisateur)))
}

function Test-LienConfiance {
    <# Vrai pour un lien ou une jonction (jamais suivis) ; faux pour un chemin absent. #>
    param([string]$Chemin)
    try { return ((([IO.File]::GetAttributes($Chemin)) -band [IO.FileAttributes]::ReparsePoint) -ne 0) } catch { return $false }
}

function Test-DossierConfianceLie {
    param([string]$Racine)
    return ((Test-LienConfiance (Join-Path $Racine 'runtime')) -or (Test-LienConfiance (Join-Path $Racine 'runtime\local-control')))
}

function Format-DateConfiance {
    <# « 01.11.2026 à 14:02 », heure du PC. #>
    param([datetime]$Date)
    return $Date.ToString("dd'.'MM'.'yyyy' à 'HH':'mm", [Globalization.CultureInfo]::InvariantCulture)
}

function Remove-ConfianceWorker {
    <#
        Efface le jeton de ce compte : celui du menu (refus du site, jeton échu ou illisible) ou, avec -TousClients,
        aussi celui de worker.exe (« Oublier ce PC »). Rend $true si un fichier a été effacé. Ne lève jamais.
    #>
    param([string]$Racine, [string]$Utilisateur, [switch]$TousClients)
    if (-not ([string]$Utilisateur).Trim()) { return $false }
    $efface = $false
    $clients = @($script:ClientConfiance)
    if ($TousClients) { $clients = $script:ClientsConfiance }
    foreach ($c in $clients) {
        try {
            $chemin = Get-CheminConfianceWorker -Racine $Racine -Utilisateur $Utilisateur -Client $c
            if ([IO.File]::Exists($chemin) -or (Test-LienConfiance $chemin)) { [IO.File]::Delete($chemin); $efface = $true }   # un lien est retiré, jamais suivi
        } catch { }
    }
    return $efface
}

function Test-ConfianceWorker {
    <# Un jeton est-il gardé pour ce compte sur ce PC (menu ou worker.exe) ? Seul le nom du fichier est regardé. #>
    param([string]$Racine, [string]$Utilisateur)
    if (-not ([string]$Utilisateur).Trim()) { return $false }
    foreach ($c in $script:ClientsConfiance) {
        try { if ([IO.File]::Exists((Get-CheminConfianceWorker -Racine $Racine -Utilisateur $Utilisateur -Client $c))) { return $true } } catch { }
    }
    return $false
}

function Read-ConfianceWorker {
    <#
        Jeton mémorisé par le menu pour ce compte : [pscustomobject]@{ Jeton ; Expire (date locale) }, ou $null.
        Échu, illisible (DPAPI d'un autre compte, contenu abîmé) ou d'un autre compte : effacé. Jamais à travers un lien.
    #>
    param([string]$Racine, [string]$Utilisateur)
    if (-not ([string]$Utilisateur).Trim()) { return $null }
    $chemin = ''
    try { $chemin = Get-CheminConfianceWorker -Racine $Racine -Utilisateur $Utilisateur } catch { return $null }
    if (-not [IO.File]::Exists($chemin) -or (Test-DossierConfianceLie -Racine $Racine) -or (Test-LienConfiance $chemin)) { return $null }
    if (-not (Test-DpapiConfiance)) { return $null }
    $clair = $null
    try {
        $taille = (New-Object IO.FileInfo($chemin)).Length
        if ($taille -le 0 -or $taille -gt $script:OctetsConfianceMax) { [void](Remove-ConfianceWorker -Racine $Racine -Utilisateur $Utilisateur); return $null }
        $clair = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($chemin), (Get-EntropieConfiance -Utilisateur $Utilisateur), [Security.Cryptography.DataProtectionScope]::CurrentUser)
        $valeur = (New-Object System.Text.UTF8Encoding($false)).GetString($clair) | ConvertFrom-Json
        $jeton = [string](Get-ChampGestion $valeur 'device_trust' '')
        $expire = ConvertTo-DateLocale (Get-ChampGestion $valeur 'expires_at')
        $proprietaire = [string](Get-ChampGestion $valeur 'username' '')
        if ([string](Get-ChampGestion $valeur 'client' '') -cne $script:ClientConfiance -or $proprietaire -cne ([string]$Utilisateur).Trim().ToLowerInvariant() -or
            -not (Test-JetonConfianceValide $jeton) -or $null -eq $expire -or $expire -le (Get-Date)) {
            [void](Remove-ConfianceWorker -Racine $Racine -Utilisateur $Utilisateur)
            return $null
        }
        return [pscustomobject]@{ Jeton = $jeton; Expire = $expire }
    }
    catch [System.IO.IOException] { return $null }                 # fichier tenu par un autre programme : gardé
    catch [System.UnauthorizedAccessException] { return $null }
    catch { [void](Remove-ConfianceWorker -Racine $Racine -Utilisateur $Utilisateur); return $null }
    finally { if ($clair) { [Array]::Clear($clair, 0, $clair.Length) } }
}

function Save-ConfianceWorker {
    <# Mémorise le jeton rendu par le site après un code réussi et « O » à la question. Lève une exception si rien n'est écrit. #>
    param([string]$Racine, [string]$Utilisateur, [string]$Jeton, $Expire)
    if (-not ([string]$Utilisateur).Trim() -or -not (Test-JetonConfianceValide $Jeton)) { throw 'jeton illisible' }
    $date = ConvertTo-DateLocale $Expire
    if ($null -eq $date -or $date -le (Get-Date)) { throw 'échéance illisible' }
    if (-not (Test-DpapiConfiance)) { throw 'DPAPI indisponible' }
    if (Test-DossierConfianceLie -Racine $Racine) { throw 'lien ou jonction sur le dossier des demandes locales' }
    $dossier = Join-Path $Racine 'runtime\local-control'
    if (-not (Test-Path -LiteralPath $dossier -PathType Container)) { New-Item -ItemType Directory -Path $dossier -Force | Out-Null }
    $chemin = Get-CheminConfianceWorker -Racine $Racine -Utilisateur $Utilisateur
    $temporaire = $chemin + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    $securite = New-Object Security.AccessControl.FileSecurity
    $securite.SetAccessRuleProtection($true, $false)
    $proprietaires = @([Security.Principal.WindowsIdentity]::GetCurrent().User,
        [Security.Principal.SecurityIdentifier]::new([Security.Principal.WellKnownSidType]::LocalSystemSid, $null),
        [Security.Principal.SecurityIdentifier]::new([Security.Principal.WellKnownSidType]::BuiltinAdministratorsSid, $null))
    foreach ($sid in $proprietaires) {
        $securite.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, [Security.AccessControl.FileSystemRights]::FullControl, [Security.AccessControl.AccessControlType]::Allow))
    }
    $epoque = New-Object DateTime 1970, 1, 1, 0, 0, 0, ([DateTimeKind]::Utc)
    $contenu = [ordered]@{ v = 1; client = $script:ClientConfiance; username = ([string]$Utilisateur).Trim().ToLowerInvariant(); device_trust = $Jeton
                           expires_at = [long][Math]::Floor([double]$Expire); saved_at = [long][Math]::Floor(([DateTime]::UtcNow - $epoque).TotalSeconds) }
    $clair = $null; $flux = $null
    try {
        $clair = (New-Object System.Text.UTF8Encoding($false)).GetBytes([string](ConvertTo-Json -InputObject $contenu -Compress))
        $scelle = [Security.Cryptography.ProtectedData]::Protect($clair, (Get-EntropieConfiance -Utilisateur $Utilisateur), [Security.Cryptography.DataProtectionScope]::CurrentUser)
        $flux = [IO.FileStream]::new($temporaire, [IO.FileMode]::CreateNew, [Security.AccessControl.FileSystemRights]::FullControl, [IO.FileShare]::None, 4096, [IO.FileOptions]::WriteThrough, $securite)
        $flux.Write($scelle, 0, $scelle.Length)
        $flux.Flush()
        $flux.Dispose(); $flux = $null
        if ([IO.File]::Exists($chemin) -or (Test-LienConfiance $chemin)) { [IO.File]::Delete($chemin) }   # un lien est retiré, jamais suivi
        [IO.File]::Move($temporaire, $chemin)   # même volume : l'ACL fermée du fichier temporaire le suit
    } finally {
        if ($flux) { try { $flux.Dispose() } catch { } }
        if ($clair) { [Array]::Clear($clair, 0, $clair.Length) }
        foreach ($cle in @($contenu.Keys)) { $contenu[$cle] = $null }
        try { if ([IO.File]::Exists($temporaire)) { [IO.File]::Delete($temporaire) } } catch { }
    }
}

function Invoke-OublierCePc {
    <#
        « Oublier ce PC » (touche O du menu ; proposé à la déconnexion L avec -Proposer) : jetons d'appareil de confiance
        du compte effacés ici (menu et worker.exe) après confirmation ; le code 2FA sera de nouveau demandé. Le site garde
        l'appareil jusqu'à son échéance (ou son retrait dans le compte) mais ne reçoit plus jamais ce jeton.
        -Confirmer : lecteur remplaçable (essais), Confirm-Action par défaut. Rend 'oublie', 'garde', 'aucun' ou 'non_connecte'.
    #>
    param([string]$Racine, [string]$Utilisateur, [switch]$Proposer, [scriptblock]$Confirmer = $null)
    if (-not ([string]$Utilisateur).Trim()) {
        if (-not $Proposer) { Write-Info 'Connecte-toi d''abord (C) : O oublie ce PC pour le compte connecté.' }
        return 'non_connecte'
    }
    if (-not (Test-ConfianceWorker -Racine $Racine -Utilisateur $Utilisateur)) {
        if (-not $Proposer) { Write-Info 'Ce PC n''est pas mémorisé pour ce compte : le code 2FA est déjà demandé à chaque connexion.' }
        return 'aucun'
    }
    if (-not $Confirmer) { $Confirmer = { param($Question) Confirm-Action $Question } }
    $question = 'Oublier ce PC ? Le code 2FA sera redemandé à la prochaine connexion (menu et worker.exe).'
    if ($Proposer) { $question = 'Oublier aussi ce PC ? Le code 2FA sera redemandé à la prochaine connexion (menu et worker.exe).' }
    $oui = $false
    try { $oui = [bool](& $Confirmer $question) } catch { $oui = $false }   # pas de clavier : rien n'est effacé
    if (-not $oui) {
        if (-not $Proposer) { Write-Info 'Rien n''a été changé : ce PC reste de confiance pour ce compte.' }
        return 'garde'
    }
    if (Remove-ConfianceWorker -Racine $Racine -Utilisateur $Utilisateur -TousClients) {
        Write-Ok 'Ce PC est oublié pour ce compte : le code 2FA sera de nouveau demandé.'
        return 'oublie'
    }
    Write-Alerte 'Rien n''a pu être effacé : vérifie les droits du dossier runtime\local-control.'
    return 'garde'
}

function Read-ChoixMemoriserPc {
    <# Après le code : « O » = demander au site de mémoriser ce PC ; Entrée, N, toute autre réponse ou pas de clavier = non. #>
    param([scriptblock]$Lire)
    $saisie = ''
    try { $saisie = [string](& $Lire 'Ne plus demander le code sur ce PC pendant 7 jours ? [O/N, Entrée = non]') } catch { $saisie = '' }
    return ($saisie.Trim() -match '^(?i:o|oui)$')
}

function Write-ResultatConfiance {
    <# Après une connexion réussie : code non demandé (jeton accepté), PC mémorisé (jeton reçu), ou pourquoi il ne l'est pas. #>
    param([string]$Racine, [string]$Utilisateur, $Reponse, $Confiance, [bool]$Memoriser)
    if ($Reponse.PcDeConfiance) {
        if ($Confiance) { Write-Ok ('Code non demandé : ce PC est de confiance jusqu''au ' + (Format-DateConfiance $Confiance.Expire) + '. O dans le menu pour l''oublier.') }
        else { Write-Ok 'Code non demandé : ce PC est de confiance. O dans le menu pour l''oublier.' }
        return
    }
    if (-not $Memoriser) { return }
    if ($Reponse.JetonConfiance) {
        try {
            Save-ConfianceWorker -Racine $Racine -Utilisateur $Utilisateur -Jeton $Reponse.JetonConfiance -Expire $Reponse.ExpireConfiance
            Write-Ok ('Ce PC est de confiance jusqu''au ' + (Format-DateConfiance (ConvertTo-DateLocale $Reponse.ExpireConfiance)) + ' : le code 2FA ne sera plus demandé ici. O dans le menu pour l''oublier.')
        } catch { Write-Alerte ('Ce PC n''a pas pu être mémorisé (' + $_.Exception.GetType().Name + ') : le code 2FA sera redemandé.') }
    }
    elseif ($Reponse.ConfianceNonSupportee) { Write-Alerte 'Le site ne propose pas encore « Ne plus demander le code » : le code 2FA sera redemandé à la prochaine connexion.' }
    else { Write-Alerte 'Le site n''a pas mémorisé ce PC (vérifie l''option dans les paramètres 2FA de ton compte) : le code 2FA sera redemandé.' }
}

# ---------------------------------------------------------------------------
# Dialogue avec l'agent : verrou, connexion, contrôle, déconnexion
# ---------------------------------------------------------------------------

function Get-EtatVerrouConnexion {
    <# Verrou de connexion (3 échecs) lu par l'agent sur le site. Rend : Verrouille, Connu, AgentMuet, Injoignable, Erreur. #>
    param([string]$Racine, [int]$DelaiSecondes = 15)
    $r = [pscustomobject]@{ Verrouille = $false; Connu = $false; AgentMuet = $false; Injoignable = $false; Erreur = '' }
    $limite = (Get-Date).AddSeconds($DelaiSecondes)
    $rafraichir = $true
    while ($true) {
        $reponse = $null
        try { $reponse = Invoke-ActionLocale -Racine $Racine -Action 'login-state' -DelaiSecondes 8 -Arguments @{ refresh = $rafraichir } } catch { }
        $rafraichir = $false
        if (-not $reponse -or -not $reponse.Ok -or -not $reponse.Resultat) {
            if ((Get-ClasseEchecLocal $reponse) -ne 'refus') { $r.AgentMuet = $true }
            if ($reponse) { $r.Erreur = [string]$reponse.Erreur }
            return $r
        }
        $etat = $reponse.Resultat
        if (-not [bool](Get-ChampGestion $etat 'refreshing' $false) -or (Get-Date) -ge $limite) {
            $r.Verrouille = [bool](Get-ChampGestion $etat 'locked' $false)
            $r.Connu = [bool](Get-ChampGestion $etat 'known' $false)
            $r.Injoignable = [bool](Get-ChampGestion $etat 'unreachable' $false)
            $erreur = Get-ChampGestion $etat 'error'
            if ($null -ne $erreur) { $r.Erreur = [string](Get-ChampGestion $erreur 'message' '') }
            return $r
        }
        Start-Sleep -Milliseconds 700
    }
}

function Invoke-ConnexionCompte {
    <#
        Une tentative : fichier d'arguments {username, password, totp_code, client=menu[, device_trust][, remember_device]}
        puis action locale « login ». -JetonConfiance : jeton mémorisé de ce PC, envoyé à la place du code (jamais avec
        lui) ; -Memoriser : « Ne plus demander le code sur ce PC pendant 7 jours », envoyé avec le code seulement.
    #>
    param([string]$Racine, [string]$Utilisateur, [Security.SecureString]$MotDePasse, [Security.SecureString]$Code = $null,
          [string]$JetonConfiance = '', [switch]$Memoriser)
    $secrets = @{ password = $MotDePasse }
    $contenu = @{ username = $Utilisateur; client = 'menu' }
    if ($null -ne $Code -and $Code.Length -gt 0) {
        $secrets['totp_code'] = $Code
        if ($Memoriser) { $contenu['remember_device'] = $true }
    }
    elseif ($JetonConfiance -and (Test-JetonConfianceValide $JetonConfiance)) { $contenu['device_trust'] = $JetonConfiance }
    $reponse = Invoke-ActionSecreteWorker -Racine $Racine -Action 'login' -Contenu $contenu -Secrets $secrets -DelaiSecondes 45
    $contenu.Clear()
    $r = [pscustomobject]@{ Ok = $false; Code = ''; Erreur = ''; Injoignable = $false; AgentMuet = $false; SiteSansFonction = $false; Resultat = $null
                            PcDeConfiance = $false; JetonConfiance = ''; ExpireConfiance = $null; ConfianceRefusee = $false; ConfianceNonSupportee = $false }
    if (-not $reponse -or -not $reponse.Ok -or -not $reponse.Resultat) {
        $classe = Get-ClasseEchecLocal $reponse
        if ($classe -ne 'refus') { $r.AgentMuet = $true }
        if ($reponse) { $r.Erreur = [string]$reponse.Erreur }
        if ($r.Erreur -eq 'timeout') { $r.Erreur = '' }
        return $r
    }
    $resultat = $reponse.Resultat
    $r.Resultat = $resultat
    $r.Ok = [bool](Get-ChampGestion $resultat 'ok' $false)
    $r.Code = [string](Get-ChampGestion $resultat 'code' '')
    $r.Erreur = [string](Get-ChampGestion $resultat 'error' '')
    $r.Injoignable = [bool](Get-ChampGestion $resultat 'unreachable' $false)
    $r.SiteSansFonction = [bool](Get-ChampGestion $resultat 'site_missing' $false)
    # Appareil de confiance (agent qui annonce worker_login_trust_v1 ; absents d'un agent plus ancien = valeurs neutres).
    $r.PcDeConfiance = [bool](Get-ChampGestion $resultat 'trusted_device' $false)
    $r.ConfianceRefusee = [bool](Get-ChampGestion $resultat 'device_trust_rejected' $false)
    $r.ConfianceNonSupportee = [bool](Get-ChampGestion $resultat 'device_trust_unsupported' $false)
    $jeton = [string](Get-ChampGestion $resultat 'device_trust' '')
    if (Test-JetonConfianceValide $jeton) {
        $r.JetonConfiance = $jeton
        $r.ExpireConfiance = Get-ChampGestion $resultat 'device_trust_expires_at'
    }
    return $r
}

function Set-SessionDepuisReponse {
    param($Resultat, [string]$Source = 'menu')
    Set-SessionWorker -Jeton ([string](Get-ChampGestion $Resultat 'session_token' '')) -Role ([string](Get-ChampGestion $Resultat 'role' '')) `
        -Droits @(Get-ChampGestion $Resultat 'rights' @()) -Utilisateur ([string](Get-ChampGestion $Resultat 'username' '')) -Source $Source
}

function Test-SessionWorker {
    <#
        La session vaut-elle encore sur le site ? Rend : Valide, Injoignable, Erreur. Met à jour rôle et droits
        (le propriétaire a pu changer ceux d'un co-propriétaire). Sans jeton : Valide = $false.
    #>
    param([string]$Racine)
    $r = [pscustomobject]@{ Valide = $false; Injoignable = $false; Erreur = '' }
    $s = $global:AlpineSessionWorker
    if (-not $s -or -not (Test-JetonSessionValide $s.Jeton)) { return $r }
    $reponse = Invoke-ActionSecreteWorker -Racine $Racine -Action 'session-check' -Contenu @{ session_token = [string]$s.Jeton } -DelaiSecondes 25
    if (-not $reponse -or -not $reponse.Ok -or -not $reponse.Resultat) {
        $r.Injoignable = $true
        if ($reponse) { $r.Erreur = [string]$reponse.Erreur }
        return $r
    }
    $resultat = $reponse.Resultat
    if ([bool](Get-ChampGestion $resultat 'unreachable' $false) -or [bool](Get-ChampGestion $resultat 'site_missing' $false) -or -not [bool](Get-ChampGestion $resultat 'ok' $false)) {
        $r.Injoignable = $true
        $r.Erreur = [string](Get-ChampGestion $resultat 'error' '')
        return $r
    }
    $r.Valide = [bool](Get-ChampGestion $resultat 'valid' $false)
    if ($r.Valide) {
        $s.Role = $(if ([string](Get-ChampGestion $resultat 'role' '') -eq 'co_owner') { 'co_owner' } else { 'owner' })
        $droits = @(Get-ChampGestion $resultat 'rights' @() | ForEach-Object { [string]$_ } | Where-Object { $_ -cmatch '^[a-z*_]{1,40}$' })
        if ($s.Role -eq 'owner' -and $droits.Count -eq 0) { $droits = @('*') }
        $s.Droits = $droits
        $s.DernierControle = Get-Date
    }
    return $r
}

function Close-SessionWorker {
    <# Ferme la session sur le site (fichier d'arguments, action « logout »), puis l'oublie ici. Ne lève jamais. #>
    param([string]$Racine)
    $s = $global:AlpineSessionWorker
    Clear-SessionWorker
    if (-not $s -or $s.Source -ne 'menu' -or -not (Test-JetonSessionValide $s.Jeton)) { return $false }
    try {
        $reponse = Invoke-ActionSecreteWorker -Racine $Racine -Action 'logout' -Contenu @{ session_token = [string]$s.Jeton } -DelaiSecondes 20
        return [bool]($reponse -and $reponse.Ok -and $reponse.Resultat -and [bool](Get-ChampGestion $reponse.Resultat 'ended' $false))
    } catch { return $false }
}

# ---------------------------------------------------------------------------
# Écran de connexion
# ---------------------------------------------------------------------------

function Read-SecretWorker {
    <# Saisie masquée. Entrée redirigée (pas de clavier) : rien n'est lu, jamais un mot de passe depuis un tube. #>
    param([string]$Invite)
    if (Test-EntreeRedirigee) { return $null }
    return (Read-Host ('   ' + $Invite) -AsSecureString)
}

function Read-ChoixConnexion {
    <# Choix après un refus ou un blocage. Rend 'R', 'N', 'H', 'Q' ou 'S' (saisir de nouveau). #>
    param([scriptblock]$Lire, [switch]$SansSaisie, [switch]$SansNavigateur)
    $lignes = @()
    if (-not $SansSaisie) { $lignes += '[S] saisir pseudo et mot de passe' }
    $lignes += '[R] revérifier l''état de la connexion'
    if (-not $SansNavigateur) { $lignes += '[N] connexion par navigateur / téléphone' }
    $lignes += '[H] mode hors ligne (actions locales sûres seulement)'
    $lignes += '[Q] quitter'
    foreach ($l in $lignes) { Write-Conseil $l }
    for ($i = 0; $i -lt 10; $i++) {
        $saisie = ([string](& $Lire 'Ton choix')).Trim().ToUpper()
        if (@('R', 'N', 'H', 'Q', 'S') -contains $saisie) {
            if ($saisie -eq 'S' -and $SansSaisie) { continue }
            if ($saisie -eq 'N' -and $SansNavigateur) { continue }
            return $saisie
        }
        if ($saisie -eq '' -and -not $SansSaisie) { return 'S' }
    }
    return 'Q'
}

function Invoke-ConnexionAppareil {
    <#
        Connexion par navigateur ou téléphone (même parcours que le bouton « Navigateur / téléphone… » de worker.exe) :
        l'agent obtient du site un lien et un code, le propriétaire valide depuis son compte (2FA et « connexion assistée
        par l'appareil » activées), le menu interroge l'agent toutes les deux secondes. Échap annule.
        Rend $true quand la session est ouverte.
    #>
    param([string]$Racine)
    $depart = $null
    try { $depart = Invoke-ActionLocale -Racine $Racine -Action 'device-login-start' -DelaiSecondes 30 } catch { }
    if (-not $depart -or -not $depart.Ok -or -not $depart.Resultat -or -not [bool](Get-ChampGestion $depart.Resultat 'ok' $false)) {
        $raison = ''
        if ($depart -and $depart.Resultat) { $raison = [string](Get-ChampGestion $depart.Resultat 'error' '') }
        elseif ($depart) { $raison = [string]$depart.Erreur }
        if (-not $raison) { $raison = 'le site ou l''agent ne répond pas.' }
        Write-Alerte ('Connexion par navigateur impossible : ' + $raison)
        return $false
    }
    $demande = [string](Get-ChampGestion $depart.Resultat 'request_id' '')
    $secret = [string](Get-ChampGestion $depart.Resultat 'poll_token' '')
    $lien = [string](Get-ChampGestion $depart.Resultat 'url' '')
    $codeAffiche = [string](Get-ChampGestion $depart.Resultat 'code' '')
    $duree = ConvertTo-EntierGestion (Get-ChampGestion $depart.Resultat 'expires_in') -Maximum 300
    if ($duree -lt 1) { $duree = 120 }
    if ($demande -cnotmatch '^[0-9a-f]{32}$' -or $secret -cnotmatch '^[A-Za-z0-9_-]{43}$') { Write-Alerte 'Réponse inattendue du site : connexion par navigateur annulée.'; return $false }
    Write-Section 'Connexion par navigateur / téléphone'
    if ($lien -match '^https://\S+$') { Write-Info ('Ouvre ce lien depuis un appareil connecté à ton compte : ' + $lien) }
    if ($codeAffiche) { Write-Info ('Vérifie que le site affiche le code : ' + $codeAffiche) }
    Write-Conseil 'Valide la connexion sur le site. Échap pour annuler.'
    $limite = (Get-Date).AddSeconds($duree)
    while ((Get-Date) -lt $limite) {
        $annuler = $false
        try { while (-not (Test-EntreeRedirigee) -and [Console]::KeyAvailable) { if ([Console]::ReadKey($true).Key -eq 'Escape') { $annuler = $true } } } catch { }
        $suivi = Invoke-ActionSecreteWorker -Racine $Racine -Action 'device-login-poll' -Contenu @{ request_id = $demande; poll_token = $secret; cancel = $annuler } -DelaiSecondes 25
        if ($annuler) { $secret = $null; Write-Info 'Connexion par navigateur annulée.'; return $false }
        if ($suivi -and $suivi.Ok -and $suivi.Resultat) {
            $etat = [string](Get-ChampGestion $suivi.Resultat 'state' '')
            if ($etat -eq 'cancelled') { Write-Alerte 'Demande refusée ou annulée sur le site.'; return $false }
            if ($etat -ne 'pending') {
                if ([bool](Get-ChampGestion $suivi.Resultat 'ok' $false) -and [string](Get-ChampGestion $suivi.Resultat 'session_token' '')) {
                    $secret = $null
                    Set-SessionDepuisReponse -Resultat $suivi.Resultat -Source 'menu'
                    return $true
                }
                $raison = [string](Get-ChampGestion $suivi.Resultat 'error' '')
                if ([string](Get-ChampGestion $suivi.Resultat 'code' '') -eq $script:CodeVerrou) { $raison = $script:MessageVerrouConnexion }
                Write-Alerte ('Connexion refusée : ' + $(if ($raison) { $raison } else { 'raison inconnue.' }))
                return $false
            }
        }
        Start-Sleep -Seconds 2
    }
    $secret = $null
    Write-Alerte 'Délai dépassé : la demande de connexion a expiré.'
    return $false
}

function Invoke-ConnexionProprietaire {
    <#
        Écran de connexion du menu. Rend 'connecte' (session en mémoire), 'hors_ligne' (choix explicite : entrées
        locales sûres seulement) ou 'quitter'. -LireTexte / -LireSecret : lecteurs remplaçables (essais) ; par défaut
        la console, et la saisie du mot de passe et du code 2FA est masquée.
    #>
    param([string]$Racine, [scriptblock]$LireTexte = $null, [scriptblock]$LireSecret = $null, [switch]$Reconnexion, [int]$ToursMax = 12)
    if (-not $LireTexte) { $LireTexte = { param($Invite) Read-Host ('   ' + $Invite) } }
    if (-not $LireSecret) { $LireSecret = { param($Invite) Read-SecretWorker $Invite } }
    $titre = 'CONNEXION DU PROPRIÉTAIRE'
    if ($Reconnexion) { $titre = 'SESSION EXPIRÉE : NOUVELLE CONNEXION' }
    Write-Cadre -Titre $titre -Lignes @('Ce Worker est associé à un compte Alpine Makers. Identifie-toi avec ce compte',
        'pour ouvrir le menu : même connexion que worker.exe (2FA, co-propriétaires, verrou).') -Largeur 96
    $verifier = $true
    for ($tour = 0; $tour -lt $ToursMax; $tour++) {
        if ($verifier) {
            $verifier = $false
            Write-Info 'Vérification de l''état de la connexion...'
            $verrou = Get-EtatVerrouConnexion -Racine $Racine
            $choix = ''
            if ($verrou.AgentMuet) {
                Write-Alerte 'L''agent du Worker ne répond pas : c''est lui qui signe la connexion auprès du site.'
                Write-Conseil ('En mode hors ligne, ' + (Get-NomEntree 12) + ' ou ' + (Get-NomEntree 10) + ' le remettent en route, puis C pour te connecter.')
                $choix = Read-ChoixConnexion -Lire $LireTexte -SansNavigateur
            }
            elseif ($verrou.Verrouille) {
                Write-Erreur $script:MessageVerrouConnexion
                $choix = Read-ChoixConnexion -Lire $LireTexte -SansSaisie -SansNavigateur
            }
            elseif ($verrou.Erreur) { Write-Alerte ('État non vérifié (' + $verrou.Erreur + '). Tu peux tenter la connexion.') }
            switch ($choix) {
                'R' { $verifier = $true; continue }
                'H' { Clear-SessionWorker; $global:AlpineModeHorsLigne = $true; return 'hors_ligne' }
                'Q' { return 'quitter' }
            }
            if ($verifier) { continue }
        }
        Write-Host ''
        $utilisateur = ([string](& $LireTexte 'Pseudo ou e-mail du compte (Entrée seule : autres choix)')).Trim()
        if (-not $utilisateur) {
            $choix = Read-ChoixConnexion -Lire $LireTexte
            switch ($choix) {
                'R' { $verifier = $true; continue }
                'N' { if (Invoke-ConnexionAppareil -Racine $Racine) { Write-Ok ('Connecté : ' + (Get-ResumeSessionWorker) + '.'); return 'connecte' }; continue }
                'H' { Clear-SessionWorker; $global:AlpineModeHorsLigne = $true; return 'hors_ligne' }
                'Q' { return 'quitter' }
            }
            continue
        }
        if ($utilisateur.Length -gt 120) { Write-Alerte 'Pseudo ou e-mail trop long.'; continue }
        $motDePasse = & $LireSecret 'Mot de passe (saisie masquée)'
        if ($null -eq $motDePasse -or $motDePasse.Length -eq 0) { Write-Alerte 'Indique le mot de passe.'; continue }
        $code = $null
        $memoriser = $false
        # Appareil de confiance : jeton gardé (chiffré DPAPI) pour ce compte sur ce PC, envoyé à la place du code.
        $confiance = Read-ConfianceWorker -Racine $Racine -Utilisateur $utilisateur
        $rejouer = $true
        while ($rejouer) {
            $rejouer = $false
            Write-Info 'Connexion en cours...'
            $jeton = ''
            if ($confiance -and $null -eq $code) { $jeton = [string]$confiance.Jeton }
            $r = Invoke-ConnexionCompte -Racine $Racine -Utilisateur $utilisateur -MotDePasse $motDePasse -Code $code -JetonConfiance $jeton -Memoriser:$memoriser
            $jetonEnvoye = [bool]$jeton
            $jeton = ''
            if ($r.ConfianceRefusee -or ($jetonEnvoye -and $r.Code -eq $script:CodeTotpRequis)) {
                # Jeton échu, invalide ou retiré depuis le site, ou qui n'a pas dispensé du code : effacé de ce PC.
                [void](Remove-ConfianceWorker -Racine $Racine -Utilisateur $utilisateur)
                $confiance = $null
            }
            if ($r.Ok) {
                $motDePasse.Dispose(); if ($code) { $code.Dispose() }
                Set-SessionDepuisReponse -Resultat $r.Resultat -Source 'menu'
                Write-Ok ('Connecté : ' + (Get-ResumeSessionWorker) + '.')
                Write-ResultatConfiance -Racine $Racine -Utilisateur $utilisateur -Reponse $r -Confiance $confiance -Memoriser $memoriser
                $r.JetonConfiance = ''; $confiance = $null
                return 'connecte'
            }
            if ($r.Code -eq $script:CodeTotpRequis) {
                if ($r.ConfianceRefusee) { Write-Alerte 'Ce PC n''est plus de confiance (jeton échu ou retiré depuis le site) : saisis le code 2FA.' }
                elseif ($jetonEnvoye -and $r.ConfianceNonSupportee) { Write-Alerte 'Le site ne propose pas encore « Ne plus demander le code » : saisis le code 2FA.' }
                elseif ($jetonEnvoye) { Write-Alerte 'Le site demande le code 2FA pour ce PC : saisis-le (O à la question suivante pour le mémoriser de nouveau).' }
                else { Write-Alerte 'Ce compte a la double authentification : saisis le code de ton application (ou un code de secours).' }
                if ($code) { $code.Dispose() }
                $code = & $LireSecret 'Code 2FA (saisie masquée)'
                if ($null -ne $code -and $code.Length -gt 0 -and $code.Length -le 16) {
                    # Question posée après chaque code saisi : le site ne mémorise ce PC qu'avec un code réussi ET « O ».
                    $memoriser = Read-ChoixMemoriserPc -Lire $LireTexte
                    $rejouer = $true
                } else { Write-Alerte 'Indique le code 2FA.' }
                continue
            }
            $motDePasse.Dispose(); if ($code) { $code.Dispose() }
            if ($r.Code -eq $script:CodeVerrou) {
                Write-Erreur $script:MessageVerrouConnexion
                $choix = Read-ChoixConnexion -Lire $LireTexte -SansSaisie -SansNavigateur
                switch ($choix) { 'H' { Clear-SessionWorker; $global:AlpineModeHorsLigne = $true; return 'hors_ligne' } 'Q' { return 'quitter' } }
                $verifier = $true
            }
            elseif ($r.Code -eq $script:CodeRefus) {
                if ($r.Erreur) { Write-Erreur $r.Erreur } else { Write-Erreur $script:MessageRefusConnexion }
                Write-Conseil 'Seuls le propriétaire du Worker et ses co-propriétaires autorisés à se connecter sont acceptés. Trois échecs de suite bloquent la connexion.'
            }
            elseif ($r.AgentMuet -or $r.Injoignable -or $r.SiteSansFonction -or $r.Code -eq 'LOGIN_TIMEOUT') {
                if ($r.AgentMuet) { Write-Alerte 'L''agent ne répond pas : vérifie qu''il est en marche (mode hors ligne, puis 12 ou 10).' }
                elseif ($r.SiteSansFonction) { Write-Alerte 'Le site n''a pas encore cette fonction : le dashboard doit être redémarré dans sa nouvelle version.' }
                else { Write-Alerte 'Le site est injoignable depuis ce PC : rien n''a été vérifié.' }
                $choix = Read-ChoixConnexion -Lire $LireTexte
                switch ($choix) {
                    'R' { $verifier = $true }
                    'N' { if (Invoke-ConnexionAppareil -Racine $Racine) { return 'connecte' } }
                    'H' { Clear-SessionWorker; $global:AlpineModeHorsLigne = $true; return 'hors_ligne' }
                    'Q' { return 'quitter' }
                }
            }
            else {
                $texte = $r.Erreur
                if (-not $texte) { $texte = 'Connexion refusée.' }
                Write-Erreur $texte
            }
        }
    }
    Write-Alerte 'Trop d''essais dans cette fenêtre : rouvre le menu pour recommencer.'
    return 'quitter'
}

function Confirm-SessionAvantAction {
    <#
        Avant une entrée qui touche au site : session contrôlée par le site si le dernier contrôle a plus de
        $script:SessionControleMinutes minutes. Expirée ou retirée : nouvelle connexion (rend $false si elle échoue).
        Site injoignable : on laisse faire (la demande échouera proprement, le site restant le juge).
    #>
    param([string]$Racine, [scriptblock]$LireTexte = $null, [scriptblock]$LireSecret = $null)
    $s = $global:AlpineSessionWorker
    if (-not $s -or $s.Source -ne 'menu') { return $true }
    if (((Get-Date) - $s.DernierControle).TotalMinutes -lt $script:SessionControleMinutes) { return $true }
    $controle = Test-SessionWorker -Racine $Racine
    if ($controle.Valide -or $controle.Injoignable) { return $true }
    Write-Alerte 'Ta session a expiré ou a été retirée sur le site : reconnecte-toi.'
    Clear-SessionWorker
    return ((Invoke-ConnexionProprietaire -Racine $Racine -LireTexte $LireTexte -LireSecret $LireSecret -Reconnexion) -eq 'connecte')
}

function Test-ReconnexionPossible {
    <# Une nouvelle connexion peut-elle être demandée ici ? Console avec clavier, hors worker.exe. #>
    return ($env:ALPINE_WORKER_GUI -ne '1' -and -not (Test-EntreeRedirigee))
}

[void](Import-SessionTransmise)
