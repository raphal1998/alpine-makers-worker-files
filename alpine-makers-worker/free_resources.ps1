# =====================================================================================
#  Libérer la VRAM et la RAM d'un Worker Alpine Makers
#
#  Ferme les applications et scripts inutiles de la session, puis rend un rapport JSON
#  (mémoire avant/après, liste de ce qui a été fermé et de ce qui a été gardé).
#
#  TOUJOURS CONSERVÉS :
#   - Windows, pilotes, sécurité, pare-feu, accès distant et VPN, accessibilité ;
#   - le pilotage du watercooling, des ventilateurs et de la carte mère
#     (iCUE et services Corsair, MSI Center, OMEN Command Center, FanControl, HWiNFO…) ;
#   - les périphériques d'entrée et l'audio, l'onduleur ;
#   - Claude et tout ce qu'il a lancé ;
#   - TOUT ce qui fait tourner Alpine Makers : agent Worker et ses moteurs, dashboard,
#     boutique et son API, base MongoDB, messagerie Stalwart, webmail SnappyMail,
#     service d'entrée de courrier, MQTT, Docker et les tunnels Cloudflare.
#
#  Rien n'est désinstallé ni désactivé : un redémarrage du PC remet tout en place.
#  Les règles de classement viennent de Liberer-ressources.bat, avec une différence
#  voulue : ce script NE COUPE PAS les serveurs locaux d'Alpine Makers.
#
#  Usage : powershell -NoProfile -ExecutionPolicy Bypass -File free_resources.ps1 [-DryRun]
# =====================================================================================
[CmdletBinding()]
param(
  [switch]$DryRun,
  [int]$TimeoutSeconds = 240
)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$debut = Get-Date

# ------------------------------- Règles ----------------------------------------------
$reSysteme = '^(system|system idle process|idle|registry|secure system|memory compression|smss|csrss|wininit|winlogon|services|lsass|lsaiso|svchost|fontdrvhost|dwm|explorer|sihost|taskhostw|ctfmon|textinputhost|startmenuexperiencehost|shellexperiencehost|shellhost|searchhost|searchapp|searchindexer|searchprotocolhost|searchfilterhost|lockapp|logonui|userinit|runtimebroker|applicationframehost|dllhost|conhost|openconsole|windowsterminal|wt|wmiprvse|wmiapsrv|audiodg|spoolsv|wudfhost|dashost|sppsvc|sgrmbroker|backgroundtaskhost|taskmgr|procexp|procexp64|perfmon|resmon|mmc|msiexec|trustedinstaller|tiworker|dismhost|wuauclt|musnotification|musnotifyicon|monotificationux|mousocoreworker|usocoreworker|consent|smartscreen|rundll32|git|git-remote-https|git-credential-manager|ssh|ssh-agent|gpg-agent|vmcompute|vmwp|vmms|vmmem|wslservice|nvdisplay\.container|gameinputsvc|gameinputredistservice|midisrv|audiosrv)$'
$reSecurite = 'msmpeng|nissrv|mpdefender|securityhealth|sechealth|microsoftsecurityapp|mssense|sensecncproxy|avast|^avg|aswengsrv|bdagent|bdservicehost|vsserv|bitdefender|^ekrn$|^egui$|eset|^avp|kaspersky|mbam|malwarebytes|norton|mcafee|^mfe|sophos|sentinel|cylance|csfalcon|crowdstrike|webroot|^wrsa$|trendmicro|f-secure|avira|avguard|emsisoft|portmaster|simplewall|tinywall|glasswire|bitwarden|keepass|1password|^windefend$|^wdnissvc$|^mdcoresvc$|^wscsvc$|^mpssvc$|^bfe$|webthreatdef'
$reCheminSecurite = '\\(windows defender|microsoft security client|avast software|avg|bitdefender|eset|kaspersky lab|malwarebytes|norton|mcafee|sophos|sentinelone|crowdstrike|webroot|trend micro|f-secure|avira|emsisoft)\\'
$reDistant = 'teamviewer|anydesk|rustdesk|parsec|sunshine|nomachine|^nx(service|node|server)|vnc|remoting_host|chromoting|splashtop|meshagent|screenconnect|radmin|dwagent|tailscale|zerotier|wireguard|openvpn|nordvpn|protonvpn|expressvpn|surfshark|mullvad|warp|forticlient|fortitray|vpnui|vpnagent|pangp|windscribe|cyberghost|hamachi'
$reAccessibilite = '^(nvda|narrator|magnify|osk|jfw|zoomtext|natspeak|tobii.*)$'
$reInstallation = 'setup|installer|^unins\d+$|^install$'
$reEntrees = 'lghub(?!.*updat)|logioptions|^logi_|razer|synapse|rzsdk|steelseries|ggengine|hyperx|ngenuity|wacom|wtablet|xppen|pentablet|huion|syntpenh|synaptics|etdctrl|^elan|thrustmaster(?!.*install)|targetgui|rtkaud|ravbg|ravcpl|rtkngui|realtek|nahimic|dolby|^waves|maxxaudio|sonic ?studio|powerchute|powerpanel|eaton|cyberpower'
$reCheminEntrees = '\\(logitech\\lghub|razer|steelseries|wacom|realtek|nahimic|thrustmaster\\target)'
# Watercooling, ventilateurs et carte mère : jamais fermés.
$reRefroidissement = 'icue|corsair|omencommandcenter|^omen|fancontrol|argusmonitor|speedfan|afterburner|^rtss|throttlestop|msi_case|fanxpert|asusfan|msi_center|msi_central|msi center|msi_companion|msi\.terminalserver|powermodewatcher|acpowernotification|hwinfo|aida64|libre ?hardware|openhardware|nzxt|cam|aquasuite|aquacomputer|ekwb|ek-?connect|lian ?li|l-?connect|thermaltake|tt ?rgb|deepcool|arctic|noctua|alphacool'
$reCheminRefroidissement = '\\corsair\\|omencommandcenter|\\fancontrol|\\msi afterburner|\\rivatuner|\\nzxt|\\aquacomputer|\\ekwb|\\lian li|\\thermaltake'
# Logiciels constructeur non essentiels : éclairage, agents, VR, casque, surcouches, mises à jour.
$reCompagnons = 'armoury|^asus|^rog|atkex|lightingservice|lightkeeper|ledkeeper|mystic_light|^msi[._ ]|dragoncenter|turtle beach|swarm|^aac\d|samsung|^saclient|^ovr|oculus|nvsphelper|nvidia (overlay|share|app|web helper)|^nvcontainer$|directoutput|saidoutput|updat'
$reCheminCompagnons = '\\asus|armourycrate|\\rog |\\turtle beach|\\samsung|\\oculus|meta horizon|\\nvidia corporation\\(nvidia app|nvcontainer)'
$reWindowsFermables = '^(cmd|powershell|pwsh|wscript|cscript|mshta|notepad|mspaint|write|wordpad|charmap|mstsc|crossdeviceresume|phoneexperiencehost|widgets|widgetservice|widgetboard|gamebar|gamebarftserver|xboxpcappft|msrdc|appactions|systemsettings|useroobebroker|photos|calculatorapp|snippingtool)$'
# Services Windows non essentiels. Docker reste actif : la messagerie Alpine Makers tourne dedans.
$reServicesNonEssentiels = '^(wsearch|sysmain|diagtrack|dmwappushservice|dps|wdiservicehost|wdisystemhost|pcasvc|trkwks|inventorysvc|whesvc|wersvc|cdpsvc|cdpusersvc|pimindexmaintenancesvc|unistoresvc|userdatasvc|onesyncsvc|messagingservice|bcastdvruserservice|ssdpsrv|upnphost|fdphost|fdrespub|lmhosts|stisvc|qwave|dusmsvc|mapsbroker|lfsvc|retaildemo|fax|wmpnetworksvc|xblauthmanager|xblgamesave|xboxnetapisvc|phonesvc|smsrouter|walletservice|semgrsvc|icssvc|wisvc|wpcmonsvc|diagsvc|aarsvc|wsaifabricsvc|spectrum|mixedrealityopenxrsvc|edgeupdate|edgeupdatem|microsoftedgeelevationservice|asussoftwaremanager)(_[0-9a-f]+)?$'
# Alpine Makers : agent Worker, moteurs IA, dashboard, boutique, messagerie, tunnels.
$reAlpine = '^(mongod|cloudflared|stalwart|stalwart-mail|mosquitto|nginx|dockerd|vpnkit|com\.docker\.backend|com\.docker\.build|docker|node|php|php-cgi|httpd)$'
$reCheminAlpine = '\\alpine-makers-worker\\|\\p1s_local_dashboard\\|\\lockyourdrink|\\alpine-mail\\|\\alpine-makers\\|\\snappymail|\\stalwart|\\mongodb|\\cloudflared|\\mosquitto'
$reCommandeAlpine = 'agent\.py|app\.py|alpine-?makers|lockyourdrink|alpine-mail|snappymail|stalwart|next[\\/]dist|index\.js|npm-cli|orca_core_bridge|comfyui|hunyuan|printguard|freecad|lancer-api|demarrer-site|start_dashboard|start_mail|run_worker|reconnect worker'
# Socle qui fait tourner les sites Alpine Makers : conteneurs (messagerie, webmail),
# base de données, tunnels. Les fermer couperait le webmail et la boutique.
$reInfraAlpine = '^(docker desktop|docker|dockerd|docker-agent|docker-index|docker-credential-[a-z]+|wsl|wslhost|wslrelay|wslservice|wslg|vpnkit|mongod|cloudflared|stalwart|stalwart-mail|mosquitto|nginx|httpd|git-sync|git-sync-tauri|gitsync)$'
$reClaude = '\\claude_[a-z0-9]+\\|\\anthropic|chrome-native-host'
$windir = ($env:SystemRoot.TrimEnd('\') + '\').ToLower()

function Exe-De($ligne) {
  if (-not $ligne) { return '' }
  if ($ligne -match '^\s*"([^"]+)"') { return $Matches[1] }
  if ($ligne -match '^\s*(\S+?\.exe)') { return $Matches[1] }
  return ($ligne -split ' ')[0]
}

# ------------------------------- Mesures ---------------------------------------------
function Mesurer {
  $m = [ordered]@{ ram_used_mb = $null; ram_total_mb = $null; vram_used_mb = $null; vram_total_mb = $null; cpu = $null }
  try {
    $os = Get-CimInstance Win32_OperatingSystem
    $mem = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
    $m.ram_total_mb = [math]::Round($os.TotalVisibleMemorySize / 1KB)
    $m.ram_used_mb = [math]::Round($os.TotalVisibleMemorySize / 1KB - $mem.AvailableMBytes)
  } catch {}
  try { $m.cpu = [math]::Round((Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average) } catch {}
  if (Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue) {
    try {
      $ligne = & nvidia-smi.exe --query-gpu=memory.used,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1
      if ($ligne) { $v = $ligne -split ','; $m.vram_used_mb = [double]$v[0].Trim(); $m.vram_total_mb = [double]$v[1].Trim() }
    } catch {}
  }
  if ($null -eq $m.vram_used_mb) {
    try { $m.vram_used_mb = [math]::Round(((Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUAdapterMemory | Measure-Object DedicatedUsage -Sum).Sum) / 1MB) } catch {}
  }
  return [pscustomobject]$m
}

# ------------------------------- Inventaire ------------------------------------------
$tous = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
$parPid = @{}
foreach ($p in $tous) { $parPid[[int]$p.ProcessId] = $p }
$maSession = ($parPid[[int]$PID]).SessionId

# Protection de l'arbre du script : l'agent Worker, PowerShell, et leurs parents.
$proteges = New-Object 'System.Collections.Generic.HashSet[int]'
$courant = $parPid[[int]$PID]
$garde = 0
while ($courant -and $garde -lt 50) {
  # Ne pas remonter au-delà du shell : explorer protégerait toute la session.
  if (([string]$courant.Name) -match '(?i)^(explorer|userinit|winlogon|wininit|services|svchost|smss|csrss)\.exe$') { break }
  [void]$proteges.Add([int]$courant.ProcessId)
  $suivant = $parPid[[int]$courant.ParentProcessId]
  # Même contrôle de date que Parent-De : un PID recyclé n'est pas un vrai parent.
  if ($suivant -and $suivant.CreationDate -and $courant.CreationDate -and $suivant.CreationDate -gt $courant.CreationDate) { break }
  $courant = $suivant
  $garde++
}

function Parent-De($p) {
  $pp = $parPid[[int]$p.ParentProcessId]
  if ($pp -and $pp.CreationDate -and $p.CreationDate -and $pp.CreationDate -le $p.CreationDate) { return $pp }
  return $null
}

function Classer($p) {
  $pidP = [int]$p.ProcessId
  $court = ([string]$p.Name -replace '(?i)\.exe$', '').ToLower()
  $chemin = ([string]$p.ExecutablePath).ToLower()
  $ligne = ([string]$p.CommandLine).ToLower()
  if ($proteges.Contains($pidP)) { return @('garder', 'agent Worker Alpine Makers') }
  if ([int]$p.SessionId -ne $maSession) { return @('garder', 'processus hors session') }
  # Alpine Makers d'abord : dashboard, boutique, messagerie, tunnels, moteurs du Worker.
  # L'identité d'un lanceur est dans ses ARGUMENTS : cmd, powershell et node sont génériques.
  if ($chemin -match $reCheminAlpine -or $ligne -match $reCheminAlpine -or $ligne -match $reCommandeAlpine) { return @('garder', 'service Alpine Makers') }
  if ($court -match $reInfraAlpine -or $court -match '^com\.docker') { return @('garder', 'socle des sites Alpine Makers (Docker, WSL, base, tunnel)') }
  if ($court -match '^claude' -or $chemin -match $reClaude -or $ligne -match $reClaude) { return @('garder', 'Claude') }
  if ($chemin -match '\\git\\(usr\\bin|bin|mingw64\\bin|cmd)\\') { return @('garder', 'Claude (outils du terminal)') }
  $a = Parent-De $p; $n = 0
  while ($a -and $n -lt 50) {
    if (([string]$a.Name) -match '(?i)^claude') { return @('garder', 'lancé par Claude') }
    if ($proteges.Contains([int]$a.ProcessId)) { return @('garder', 'lancé par l''agent Worker') }
    $a = Parent-De $a; $n++
  }
  if ($court -match $reSysteme) { return @('garder', 'Windows') }
  if ($court -match $reSecurite -or $chemin -match $reCheminSecurite) { return @('garder', 'sécurité') }
  if ($court -match $reDistant) { return @('garder', 'accès distant / VPN') }
  if ($court -match $reAccessibilite) { return @('garder', 'accessibilité') }
  if ($court -match $reInstallation) { return @('garder', 'installation en cours') }
  if ($court -match $reEntrees -or $chemin -match $reCheminEntrees) { return @('garder', 'pilote audio ou périphérique') }
  if ($court -match $reRefroidissement -or $chemin -match $reCheminRefroidissement) { return @('garder', 'watercooling et ventilateurs') }
  if ($court -match $reCompagnons -or $chemin -match $reCheminCompagnons) { return @('fermer', 'logiciel constructeur non essentiel') }
  if (-not $chemin) { return @('garder', 'non identifiable') }
  if ($court -eq 'msrdc' -and $ligne -match 'wslg|wsl\.exe|\\wsl\\') { return @('garder', 'affichage des applications WSL') }
  if ($chemin.StartsWith($windir) -and $court -notmatch $reWindowsFermables) { return @('garder', 'Windows') }
  if ($court -eq 'msedgewebview2') {
    $b = Parent-De $p; $k = 0
    while ($b -and ([string]$b.Name).ToLower() -eq 'msedgewebview2.exe' -and $k -lt 20) { $b = Parent-De $b; $k++ }
    if ($b) { return (Classer $b) }
  }
  return @('fermer', 'application ou script')
}

$avant = Mesurer
$fermes = @()
$gardes = @{}
$erreurs = @()

# Première passe : classement de chaque processus.
$verdicts = @{}
foreach ($p in $tous) { $verdicts[[int]$p.ProcessId] = (Classer $p) }

# Deuxième passe : un processus qui a lancé un service gardé est gardé lui aussi.
# Sans cela, fermer la console d'un lanceur (cmd /c lancer-api.cmd) tuerait le
# serveur qu'elle supervise, alors qu'il est explicitement protégé.
$reGardeHeritee = '^(service Alpine Makers|socle des sites Alpine Makers|agent Worker Alpine Makers|Claude|lancé par Claude|affichage des applications WSL)'
foreach ($p in $tous) {
  $verdict = $verdicts[[int]$p.ProcessId]
  if ($verdict[0] -ne 'garder' -or $verdict[1] -notmatch $reGardeHeritee) { continue }
  $a = Parent-De $p; $n = 0
  while ($a -and $n -lt 50) {
    if (([string]$a.Name) -match '(?i)^(explorer|userinit|winlogon|wininit|services|svchost|smss|csrss)\.exe$') { break }
    $vu = $verdicts[[int]$a.ProcessId]
    if ($vu -and $vu[0] -eq 'garder') { break }
    $verdicts[[int]$a.ProcessId] = @('garder', 'lanceur d''un service gardé')
    $a = Parent-De $a; $n++
  }
}

foreach ($p in $tous) {
  $verdict = $verdicts[[int]$p.ProcessId]
  if ($verdict[0] -eq 'garder') {
    $cle = $verdict[1]
    if (-not $gardes.ContainsKey($cle)) { $gardes[$cle] = 0 }
    $gardes[$cle]++
    continue
  }
  $nom = [string]$p.Name
  $memoire = [math]::Round(([double]$p.WorkingSetSize) / 1MB)
  if ($DryRun) {
    $fermes += [pscustomobject]@{ name = $nom; pid = [int]$p.ProcessId; reason = $verdict[1]; ram_mb = $memoire; closed = $false }
    continue
  }
  try {
    $proc = Get-Process -Id ([int]$p.ProcessId) -ErrorAction Stop
    if (-not $proc.HasExited -and $proc.MainWindowHandle -ne 0) { [void]$proc.CloseMainWindow() }
  } catch {}
  Start-Sleep -Milliseconds 120
  $vivant = $null
  try { $vivant = Get-Process -Id ([int]$p.ProcessId) -ErrorAction SilentlyContinue } catch {}
  if ($vivant) {
    try { Stop-Process -Id ([int]$p.ProcessId) -Force -ErrorAction Stop }
    catch { $erreurs += ('{0} (PID {1}) : {2}' -f $nom, $p.ProcessId, $_.Exception.Message); continue }
  }
  $fermes += [pscustomobject]@{ name = $nom; pid = [int]$p.ProcessId; reason = $verdict[1]; ram_mb = $memoire; closed = $true }
  if (((Get-Date) - $debut).TotalSeconds -gt $TimeoutSeconds) { $erreurs += 'Délai dépassé : fermeture interrompue.'; break }
}

# ------------------------------- Services --------------------------------------------
$servicesArretes = @()
$servicesIgnores = ''
$estAdmin = $false
try { $estAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) } catch {}
if (-not $DryRun -and -not $estAdmin) {
  $servicesIgnores = 'Droits administrateur absents : les services Windows non essentiels ont été laissés en place.'
}
if (-not $DryRun -and $estAdmin) {
  foreach ($s in @(Get-Service -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Running' })) {
    $nom = ([string]$s.Name).ToLower()
    if ($nom -notmatch $reServicesNonEssentiels) { continue }
    try {
      Stop-Service -Name $s.Name -Force -ErrorAction Stop
      $servicesArretes += [string]$s.Name
    } catch { $erreurs += ('service {0} : {1}' -f $s.Name, $_.Exception.Message) }
  }
}

Start-Sleep -Milliseconds 800
$apres = Mesurer
$libereRam = if ($avant.ram_used_mb -ne $null -and $apres.ram_used_mb -ne $null) { [math]::Max(0, $avant.ram_used_mb - $apres.ram_used_mb) } else { $null }
$libereVram = if ($avant.vram_used_mb -ne $null -and $apres.vram_used_mb -ne $null) { [math]::Max(0, $avant.vram_used_mb - $apres.vram_used_mb) } else { $null }

$rapport = [ordered]@{
  ok = $true
  dry_run = [bool]$DryRun
  before = $avant
  after = $apres
  freed_ram_mb = $libereRam
  freed_vram_mb = $libereVram
  closed = @($fermes | Sort-Object -Property ram_mb -Descending | Select-Object -First 60)
  closed_count = @($fermes).Count
  services_stopped = $servicesArretes
  services_skipped = $servicesIgnores
  elevated = $estAdmin
  kept = ($gardes.GetEnumerator() | Sort-Object -Property Value -Descending | ForEach-Object { [pscustomobject]@{ reason = $_.Key; count = $_.Value } })
  errors = @($erreurs | Select-Object -First 20)
  duration_seconds = [math]::Round(((Get-Date) - $debut).TotalSeconds, 1)
}
$rapport | ConvertTo-Json -Depth 5 -Compress
