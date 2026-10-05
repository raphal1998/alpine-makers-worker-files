param([string]$ServerUrl="",[string]$PairingCode="",[string]$Name="",[ValidateSet("Ask","Yes","No")][string]$AutoStart="Ask",[string]$InstallDir="",[switch]$RecoverExisting)
$ErrorActionPreference = "Stop"
$sandboxOnly = ($env:ALPINE_LOCAL_SANDBOX -eq '1') -or (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'TEST_SANDBOX_ONLY.txt'))
if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'config.json')) {
  $localConfig = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  $sandboxOnly = $sandboxOnly -or ($localConfig.local_sandbox -eq $true)
  $localConfig = $null
}
if ($sandboxOnly) { throw "Paquet TEST : l'installateur standard est desactive pour proteger le Worker en service. Utilise le lanceur isole de la dashboard TEST." }
if ($RecoverExisting -and ($PairingCode -or $Name)) { throw "La recuperation conserve le Worker existant. N'utilise pas -PairingCode ni -Name ; le code de migration sera demande de facon masquee." }
if ($RecoverExisting -and !$InstallDir) {
  $recoveryCandidates = @($PSScriptRoot, (Join-Path ([Environment]::GetFolderPath("Desktop")) "alpine-makers-worker"), (Join-Path $env:PUBLIC "AlpineMakersWorker"), (Join-Path $env:ProgramData "AlpineMakersWorker")) |
    Select-Object -Unique | Where-Object { (Test-Path -LiteralPath (Join-Path $_ "config.json")) -or (Test-Path -LiteralPath (Join-Path $_ "config\identity\private.key")) }
  Write-Host "Recuperation d'un Worker existant, sans nouvelle inscription."
  Write-Host "Utilise le meme compte Windows que lors de l'installation. Attends la fin des installations/calculs avant de reparer les fichiers du programme."
  foreach ($recoveryCandidate in $recoveryCandidates) { Write-Host "Dossier detecte : $recoveryCandidate" }
  $defaultRecovery = if (@($recoveryCandidates).Count -eq 1) { [string]@($recoveryCandidates)[0] } else { "" }
  $InstallDir = Read-Host $(if ($defaultRecovery) { "Chemin COMPLET du Worker a recuperer [Entree = $defaultRecovery]" } else { "Chemin COMPLET du Worker a recuperer (obligatoire, pas le dossier des telechargements)" })
  if (!$InstallDir) { $InstallDir = $defaultRecovery }
  if (!$InstallDir) { throw "Plusieurs Workers ou aucun dossier reconnu : indique explicitement -InstallDir. Aucun Worker modifie." }
}
if ($AutoStart -eq "Ask") {
  do { $answer = Read-Host "Demarrer automatiquement le Worker a l'ouverture de session Windows ? [O/N]" } while ($answer -notmatch '^(?i:o|n)$')
  $AutoStart = if ($answer -match '^(?i:o)$') { "Yes" } else { "No" }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$isAdministrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

function Test-WorkerPython {
  param([string]$Candidate)
  if ([string]::IsNullOrWhiteSpace($Candidate) -or !(Test-Path -LiteralPath $Candidate -PathType Leaf)) {
    return $false
  }
  # Ignore the Microsoft Store execution alias: it exists as a file but is not
  # a usable interpreter until Python has actually been installed.
  if ($Candidate -match "\\WindowsApps\\python(?:3)?\.exe$") {
    return $false
  }
  try {
    & $Candidate -c "import sys, venv, ensurepip; raise SystemExit(0 if sys.version_info >= (3, 9) else 3)" 2>$null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

function Find-WorkerPython {
  $candidates = New-Object System.Collections.Generic.List[string]
  $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
  if ($launcher) {
    try {
      $launchedPath = & $launcher.Source -3 -c "import sys; print(sys.executable) if sys.version_info >= (3, 9) else sys.exit(3)" 2>$null
      if ($LASTEXITCODE -eq 0 -and $launchedPath) {
        $candidates.Add(([string]$launchedPath).Trim()) | Out-Null
      }
    } catch {}
  }
  foreach ($commandName in @("python.exe", "python3.exe")) {
    $command = Get-Command $commandName -ErrorAction SilentlyContinue
    if ($command -and $command.Source) {
      $candidates.Add([string]$command.Source) | Out-Null
    }
  }
  $searchRoots = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python"),
    $env:ProgramFiles,
    ${env:ProgramFiles(x86)},
    $env:SystemDrive
  ) | Where-Object { ![string]::IsNullOrWhiteSpace($_) -and (Test-Path -LiteralPath $_) }
  foreach ($root in $searchRoots) {
    Get-ChildItem -LiteralPath $root -Directory -Filter "Python3*" -ErrorAction SilentlyContinue |
      Sort-Object Name -Descending |
      ForEach-Object { $candidates.Add((Join-Path $_.FullName "python.exe")) | Out-Null }
  }
  foreach ($candidate in ($candidates | Select-Object -Unique)) {
    if (Test-WorkerPython -Candidate $candidate) {
      return (Resolve-Path -LiteralPath $candidate).Path
    }
  }
  return $null
}

function Test-WorkerGit {
  param([string]$Candidate)
  if ([string]::IsNullOrWhiteSpace($Candidate) -or !(Test-Path -LiteralPath $Candidate -PathType Leaf)) {
    return $false
  }
  try {
    & $Candidate --version 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

function Find-WorkerGit {
  $candidates = New-Object System.Collections.Generic.List[string]
  $command = Get-Command git.exe -ErrorAction SilentlyContinue
  if ($command -and $command.Source) {
    $candidates.Add([string]$command.Source) | Out-Null
  }
  foreach ($candidate in @(
    (Join-Path $env:ProgramFiles "Git\cmd\git.exe"),
    $(if (${env:ProgramFiles(x86)}) { Join-Path ${env:ProgramFiles(x86)} "Git\cmd\git.exe" }),
    (Join-Path $env:LOCALAPPDATA "Programs\Git\cmd\git.exe")
  )) {
    if (![string]::IsNullOrWhiteSpace($candidate)) {
      $candidates.Add([string]$candidate) | Out-Null
    }
  }
  foreach ($candidate in ($candidates | Select-Object -Unique)) {
    if (Test-WorkerGit -Candidate $candidate) {
      return (Resolve-Path -LiteralPath $candidate).Path
    }
  }
  return $null
}

function Refresh-ProcessPath {
  $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  $env:Path = (@($machinePath, $userPath) | Where-Object { $_ }) -join ";"
}

function Install-WorkerPython {
  Write-Host "Python 3.9 ou supérieur est absent. Installation automatique de Python 3.12..." -ForegroundColor Cyan
  $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
  if ($winget) {
    $wingetArguments = @(
      "install", "--id", "Python.Python.3.12", "--exact", "--silent",
      "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity",
      "--scope", $(if ($isAdministrator) { "machine" } else { "user" })
    )
    & $winget.Source @wingetArguments
    Refresh-ProcessPath
    $detected = Find-WorkerPython
    if ($detected) {
      return $detected
    }
    Write-Host "WinGet n'a pas fourni d'interpréteur utilisable. Téléchargement depuis python.org..." -ForegroundColor Yellow
  }

  $pythonVersion = "3.12.10"
  $installerName = if ([Environment]::Is64BitOperatingSystem) { "python-$pythonVersion-amd64.exe" } else { "python-$pythonVersion.exe" }
  $installerUrl = "https://www.python.org/ftp/python/$pythonVersion/$installerName"
  $temporaryInstaller = Join-Path ([IO.Path]::GetTempPath()) ("alpine-makers-" + [Guid]::NewGuid().ToString("N") + "-python.exe")
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $installerUrl -OutFile $temporaryInstaller -UseBasicParsing
    $installerArguments = @(
      "/quiet", "InstallAllUsers=" + $(if ($isAdministrator) { "1" } else { "0" }),
      "PrependPath=1", "Include_launcher=1", "Include_pip=1", "Include_test=0", "Include_doc=0"
    )
    if (!$isAdministrator) {
      $installerArguments += "InstallLauncherAllUsers=0"
    }
    $process = Start-Process -FilePath $temporaryInstaller -ArgumentList ($installerArguments -join " ") -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -notin @(0, 3010)) {
      throw "L'installateur Python a retourné le code $($process.ExitCode)."
    }
  } finally {
    Remove-Item -LiteralPath $temporaryInstaller -Force -ErrorAction SilentlyContinue
  }
  Refresh-ProcessPath
  $detected = Find-WorkerPython
  if (!$detected) {
    throw "Python a été installé mais aucun interpréteur Python 3.9+ n'a pu être détecté. Redémarre Windows puis relance ce fichier."
  }
  return $detected
}

function Install-WorkerGit {
  Write-Host "Git est absent. Installation automatique de Git for Windows..." -ForegroundColor Cyan
  $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
  if ($winget) {
    $wingetArguments = @(
      "install", "--id", "Git.Git", "--exact", "--silent",
      "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity",
      "--scope", $(if ($isAdministrator) { "machine" } else { "user" })
    )
    & $winget.Source @wingetArguments
    Refresh-ProcessPath
    $detected = Find-WorkerGit
    if ($detected) {
      return $detected
    }
    Write-Host "WinGet n'a pas fourni Git. Téléchargement depuis le dépôt officiel Git for Windows..." -ForegroundColor Yellow
  }

  $temporaryInstaller = Join-Path ([IO.Path]::GetTempPath()) ("alpine-makers-" + [Guid]::NewGuid().ToString("N") + "-git.exe")
  try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $headers = @{ "User-Agent" = "AlpineMakersWorkerInstaller/1.0"; "Accept" = "application/vnd.github+json" }
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/git-for-windows/git/releases/latest" -Headers $headers -UseBasicParsing
    $architecture = if ([Environment]::Is64BitOperatingSystem) { "64" } else { "32" }
    $asset = @($release.assets) | Where-Object { $_.name -match "^Git-.+-$architecture-bit\.exe$" } | Select-Object -First 1
    if (!$asset -or !$asset.browser_download_url) {
      throw "Aucun installateur Git for Windows compatible avec l'architecture $architecture bits n'a été trouvé."
    }
    Invoke-WebRequest -Uri $asset.browser_download_url -Headers @{ "User-Agent" = "AlpineMakersWorkerInstaller/1.0" } -OutFile $temporaryInstaller -UseBasicParsing
    $installerArguments = @("/VERYSILENT", "/NORESTART", "/NOCANCEL", "/SP-", "/CLOSEAPPLICATIONS")
    $installerArguments += $(if ($isAdministrator) { "/ALLUSERS" } else { "/CURRENTUSER" })
    $process = Start-Process -FilePath $temporaryInstaller -ArgumentList ($installerArguments -join " ") -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -notin @(0, 3010)) {
      throw "L'installateur Git a retourné le code $($process.ExitCode)."
    }
  } finally {
    Remove-Item -LiteralPath $temporaryInstaller -Force -ErrorAction SilentlyContinue
  }
  Refresh-ProcessPath
  $detected = Find-WorkerGit
  if (!$detected) {
    throw "Git a été installé mais git.exe reste introuvable. Redémarre Windows puis relance ce fichier."
  }
  return $detected
}

# Resolve the target before bootstrapping dependencies or copying any program.
$agentRoot = if ($InstallDir) {
  [IO.Path]::GetFullPath($InstallDir)
} else {
  Join-Path ([Environment]::GetFolderPath("Desktop")) "alpine-makers-worker"
}
$normalizedRoot = $agentRoot.TrimEnd([IO.Path]::DirectorySeparatorChar,[IO.Path]::AltDirectorySeparatorChar)
$broadRoots = @([IO.Path]::GetPathRoot($agentRoot), $env:USERPROFILE, $env:ProgramData, $env:PUBLIC, $env:WINDIR, $env:ProgramFiles, ${env:ProgramFiles(x86)})
if (@($broadRoots | Where-Object { $_ -and $_.TrimEnd([IO.Path]::DirectorySeparatorChar,[IO.Path]::AltDirectorySeparatorChar) -eq $normalizedRoot }).Count) { throw "Choisis un sous-dossier dedie au Worker." }
for ($workerParent = [IO.DirectoryInfo]::new($agentRoot); $null -ne $workerParent; $workerParent = $workerParent.Parent) {
  if ($workerParent.Exists -and ($workerParent.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Un dossier Worker ne doit pas traverser un lien ou une jonction. Aucun fichier modifie." }
}
if ((Test-Path -LiteralPath $agentRoot) -and !(Test-Path -LiteralPath (Join-Path $agentRoot "agent.py")) -and @(Get-ChildItem -LiteralPath $agentRoot -Force).Count) { throw "Le dossier contient des fichiers sans installation Worker reconnue. Aucun fichier modifie." }
if ($RecoverExisting -and (Test-Path -LiteralPath (Join-Path $agentRoot ".worker-detached"))) { throw "Ce Worker a ete supprime du site. Reinscris-le explicitement ; la recuperation ne contourne pas une suppression." }
$python = Find-WorkerPython
if (!$python) {
  $python = Install-WorkerPython
}
Write-Host "Python détecté : $python" -ForegroundColor Green
$git = Find-WorkerGit
if (!$git -and !$RecoverExisting) {
  $git = Install-WorkerGit
}
if ($git) { Write-Host "Git détecté : $git" -ForegroundColor Green }
New-Item -ItemType Directory -Force -Path $agentRoot | Out-Null
# Neither Public nor ProgramData's inherited ACLs protect a bearer token or an
# elevated agent from other local accounts. Set the boundary before copying
# executable files or saving the pairing credentials. A failure aborts setup.
$workerAcl = New-Object Security.AccessControl.DirectorySecurity
$workerAcl.SetOwner($identity.User)
$workerAcl.SetAccessRuleProtection($true, $false)
$workerSids = @(
  $identity.User,
  [Security.Principal.SecurityIdentifier]::new("S-1-5-18"),
  [Security.Principal.SecurityIdentifier]::new("S-1-5-32-544")
)
foreach ($workerSid in $workerSids) {
  $workerRule = [Security.AccessControl.FileSystemAccessRule]::new(
    $workerSid, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow"
  )
  $workerAcl.AddAccessRule($workerRule)
}
# API .NET et non Set-Acl : sur un dossier Worker deja installe, Set-Acl exige SeSecurityPrivilege
# (droits administrateur) et faisait echouer toute reinstallation ; l'API ecrit proprietaire + DACL seulement.
[IO.Directory]::SetAccessControl($agentRoot, $workerAcl)
$preservedWorkerItems = @("config.json", "storage.json", "components", "cache", "temp", "runtime", "logs", "data", "outputs", "workspace", "jobs", "config", "python_path.txt", "git_path.txt", "supervisor.lock", ".worker-detached")
if ([IO.Path]::GetFullPath($PSScriptRoot) -ne [IO.Path]::GetFullPath($agentRoot)) {
  foreach ($workerItem in Get-ChildItem -LiteralPath $PSScriptRoot -Force) {
    if ($workerItem.Name -in $preservedWorkerItems) { continue }
    Copy-Item -LiteralPath $workerItem.FullName -Destination $agentRoot -Recurse -Force
  }
}
# Raccourcis vers worker.exe (application graphique du menu) : Bureau et menu Demarrer du compte qui installe.
# Jamais bloquant : sans eux, worker.exe et MENU-WORKER.bat restent dans le dossier du Worker.
$workerManager = Join-Path $agentRoot "worker.exe"
if (Test-Path -LiteralPath $workerManager -PathType Leaf) {
  try {
    $workerIcon = Join-Path $agentRoot "branding\alpine-makers-worker.ico"
    $shell = New-Object -ComObject WScript.Shell
    foreach ($shortcutFolder in @([Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("Programs"))) {
      if (!$shortcutFolder -or !(Test-Path -LiteralPath $shortcutFolder)) { continue }
      $shortcut = $shell.CreateShortcut((Join-Path $shortcutFolder "Alpine Makers Worker.lnk"))
      $shortcut.TargetPath = $workerManager
      $shortcut.WorkingDirectory = $agentRoot
      $shortcut.Description = "Gestionnaire du Worker Alpine Makers"
      if (Test-Path -LiteralPath $workerIcon -PathType Leaf) { $shortcut.IconLocation = "$workerIcon,0" } else { $shortcut.IconLocation = "$workerManager,0" }
      $shortcut.Save()
    }
    Write-Host "Raccourci « Alpine Makers Worker » cree sur le Bureau et dans le menu Demarrer." -ForegroundColor Green
  } catch {
    Write-Host "Raccourci non cree ($($_.Exception.Message)) : ouvre worker.exe dans $agentRoot." -ForegroundColor Yellow
  }
}
$storageSetup = Join-Path $agentRoot "storage_paths.py"
& $python $storageSetup --root $agentRoot --initialize
if ($LASTEXITCODE -ne 0) { throw "Création de l’arborescence Worker impossible. Aucun moteur ni modèle IA n’a été téléchargé." }
$pythonPathFile = Join-Path $agentRoot "python_path.txt"
& $python (Join-Path $agentRoot "identity_setup.py")
if ($LASTEXITCODE -ne 0) { throw "Installation de la securite Worker impossible. Identite existante conservee." }
Set-Content -LiteralPath $pythonPathFile -Value $python -Encoding UTF8
$gitPathFile = Join-Path $agentRoot "git_path.txt"
if ($git) { Set-Content -LiteralPath $gitPathFile -Value $git -Encoding UTF8 }
$config = Join-Path $agentRoot "config.json"
if ($RecoverExisting) {
  $recoveryArguments = @((Join-Path $agentRoot "recover_existing_worker.py"), "--root", $agentRoot)
  if ($ServerUrl) { $recoveryArguments += @("--server-url", $ServerUrl) }
  & $python @recoveryArguments
  if ($LASTEXITCODE -ne 0) { throw "Recuperation non terminee. Aucun nouvel appairage ni changement de demarrage automatique. Lis le message ci-dessus." }
  & (Join-Path $agentRoot "configure_autostart.ps1") -AutoStart $AutoStart
  Write-Host "Identite existante verifiee dans $agentRoot. Avec AutoStart=No, lance MENU-WORKER.bat > 5 (Reconnecter le Worker) manuellement."
  exit 0
}
$existingIdentity = if (Test-Path -LiteralPath $config) { Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
if ($existingIdentity.worker_id -and $existingIdentity.token) {
  if ($ServerUrl -and $ServerUrl.TrimEnd('/') -ne ([string]$existingIdentity.server_url).TrimEnd('/')) { throw "Ce Worker est deja associe a un autre site. Association conservee." }
  Write-Host "Identite existante conservee : aucune nouvelle association. Utilise MENU-WORKER.bat > 5 (Reconnecter le Worker) pour relancer, ou > 7 (Recuperer une inscription) si le site refuse l'identite."
  & (Join-Path $agentRoot "configure_autostart.ps1") -AutoStart $AutoStart
  exit 0
}
if ($existingIdentity.worker_id) {
  throw "Une cle Worker existe deja mais la configuration est incomplete. Utilise MENU-WORKER.bat > 7 (Recuperer une inscription), ou install_windows.ps1 -RecoverExisting -InstallDir `"$agentRoot`" ; ne cree pas une nouvelle inscription."
}
if (Test-Path -LiteralPath (Join-Path $agentRoot "config\identity\private.key")) {
  Write-Host "Une cle locale existe. Si ce PC est deja inscrit sur le site, annule avec Ctrl+C et utilise MENU-WORKER.bat > 7 (Recuperer une inscription). Si un premier appairage a simplement echoue, son code peut etre retente sans changer de cle." -ForegroundColor Yellow
}
if (!$ServerUrl) { $ServerUrl = Read-Host "URL HTTPS du dashboard" }
if (!$PairingCode) { $PairingCode = Read-Host "Code d'association du dashboard" }
$pairArguments = @("agent.py", "--config", "config.json", "--pair", $ServerUrl, $PairingCode)
if (![string]::IsNullOrWhiteSpace($Name)) {
  $pairArguments += @("--name", $Name.Trim())
}
Push-Location $agentRoot
$pairExitCode = 1
try {
  & $python @pairArguments
  $pairExitCode = $LASTEXITCODE
} finally {
  Pop-Location
}
if($pairExitCode -ne 0){
  Write-Host "Association du Worker impossible. Vérifie le code et crée-en un nouveau s'il a plus de 10 minutes." -ForegroundColor Red
  exit $pairExitCode
}
& (Join-Path $agentRoot "configure_autostart.ps1") -AutoStart $AutoStart
Write-Host "Worker associe dans $agentRoot. Choix de demarrage automatique : $AutoStart."
