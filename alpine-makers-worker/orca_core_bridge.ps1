param(
  [string]$Workspace,
  [string]$OrcaDir,
  [int]$Port = 15064
)
$ErrorActionPreference="Stop"
$BridgeVersion = "19.26"
$LogPath = Join-Path $Workspace "orca_bridge.log"
function BridgeLog([string]$Message){
  try{
    Add-Content -Path $LogPath -Value ("{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message) -Encoding UTF8
  }catch{}
}
BridgeLog ("Bridge starting. Workspace={0} OrcaDir={1} Port={2}" -f $Workspace,$OrcaDir,$Port)

# --- PROFILS P1S (début) ---
# Profil filament : la P1S partage les profils « @BBL X1C » de Bambu. On ne retient qu'un profil qui se déclare
# compatible avec « Bambu Lab P1S <buse> nozzle » (compatible_printers), en préférant le profil Bambu du matériau
# puis le générique. L'ancien choix « premier nom qui contient le matériau » prenait « Bambu PLA Aero @BBL A1 »
# (débit 0,6) pour du PLA.
$FilamentPreferences=@{
  "PLA"=@("Bambu PLA Basic","Generic PLA");
  "PETG"=@("Generic PETG","Bambu PETG Basic");
  "PETG HF"=@("Bambu PETG HF","Generic PETG HF");
  "ABS"=@("Bambu ABS","Generic ABS");
  "ASA"=@("Bambu ASA","Generic ASA");
  "TPU"=@("Bambu TPU 95A","Bambu TPU 95A HF","Generic TPU");
  "PC"=@("Bambu PC","Generic PC")
}
function Test-ProfileCompatible([string]$Path,[string]$Printer){
  try{$json=Get-Content -LiteralPath $Path -Raw -Encoding UTF8|ConvertFrom-Json}catch{return $false}
  return (@($json.compatible_printers) -contains $Printer)
}
function Find-P1SFilamentProfile([string]$FilamentDir,[string]$Material,[string]$Nozzle){
  $printer="Bambu Lab P1S $Nozzle nozzle"
  $files=@(Get-ChildItem -LiteralPath $FilamentDir -File -Filter *.json -ErrorAction SilentlyContinue|Sort-Object Name)
  $material=([string]$Material).Trim()
  $names=$FilamentPreferences[$material.ToUpperInvariant()]
  if(!$names){$names=@("Bambu $material","Generic $material")}
  foreach($name in $names){
    foreach($file in $files){
      if($file.BaseName -like ($name+" @*") -and (Test-ProfileCompatible $file.FullName $printer)){return $file.FullName}
    }
  }
  # Tout autre profil de ce matériau déclaré compatible avec cette P1S et cette buse.
  foreach($file in $files){
    if($file.BaseName -like ("*"+$material+"*") -and (Test-ProfileCompatible $file.FullName $printer)){return $file.FullName}
  }
  return $null
}
# Plaque : Orca lit curr_bed_type ; la température du plateau vient de la clé de la plaque dans le profil filament.
$BedTypes=[ordered]@{
  "Textured PEI Plate"="textured_plate_temp";
  "Cool Plate"="cool_plate_temp";
  "Engineering Plate"="eng_plate_temp";
  "High Temp Plate"="hot_plate_temp"
}
function Set-FilamentBedTemperatures($Filament,[string]$BedType){
  # Le profil aplati reçoit aussi, pour toutes les plaques, les températures de la plaque choisie : le plateau est
  # donc à la bonne température même si la ligne de commande d'Orca ignorait curr_bed_type.
  $key=$BedTypes[$BedType]
  if(!$key){return}
  foreach($suffix in @("","_initial_layer")){
    $source=$Filament.PSObject.Properties[$key+$suffix]
    if($null -eq $source){continue}
    foreach($other in @($BedTypes.Values)){
      $name=$other+$suffix
      if($Filament.PSObject.Properties[$name]){$Filament.$name=$source.Value}
      else{$Filament|Add-Member -NotePropertyName $name -NotePropertyValue $source.Value}
    }
  }
}
# --- PROFILS P1S (fin) ---
Add-Type -AssemblyName System.Web

function JsonResponse($ctx,$code,$obj){
  $json=$obj|ConvertTo-Json -Depth 10 -Compress
  $bytes=[Text.Encoding]::UTF8.GetBytes($json)
  $ctx.Response.StatusCode=$code
  $ctx.Response.ContentType="application/json; charset=utf-8"
  $origin=[string]$ctx.Request.Headers["Origin"]
  if($origin -match '^http://(localhost|127\.0\.0\.1):5050$'){
    $ctx.Response.Headers.Add("Access-Control-Allow-Origin",$origin)
    $ctx.Response.Headers.Add("Vary","Origin")
  }
  $ctx.Response.Headers.Add("Access-Control-Allow-Headers","Content-Type")
  $ctx.Response.Headers.Add("Access-Control-Allow-Methods","GET,POST,OPTIONS")
  $ctx.Response.ContentLength64=$bytes.Length
  $ctx.Response.OutputStream.Write($bytes,0,$bytes.Length)
  $ctx.Response.OutputStream.Close()
}
function SafePath($name){
  $safe=[IO.Path]::GetFileName($name)
  $full=[IO.Path]::GetFullPath((Join-Path $Workspace $safe))
  $root=[IO.Path]::GetFullPath($Workspace)
  if(!$full.StartsWith($root,[StringComparison]::OrdinalIgnoreCase)){throw "Chemin invalide"}
  return $full
}
function Set-WorkerSliceState([string]$State,[bool]$Confirmed,$Result=$null,[string]$ErrorMessage=""){
  if(!$workerJobStatePath){return}
  $record=@{execution_id=$workerExecutionId;state=$State;engine_stop_confirmed=$Confirmed;updated_at=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()}
  if($null -ne $Result){$record.result=$Result}
  if($ErrorMessage){$record.error=$ErrorMessage}
  if($null -ne $workerSliceProcess){$record.pid=$workerSliceProcess.Id}
  $temporary=$workerJobStatePath+"."+[Guid]::NewGuid().ToString("N")+".tmp"
  $bytes=(New-Object Text.UTF8Encoding($false)).GetBytes(($record|ConvertTo-Json -Depth 10 -Compress))
  $stream=New-Object IO.FileStream($temporary,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
  try{$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)}finally{$stream.Dispose()}
  Move-Item -LiteralPath $temporary -Destination $workerJobStatePath -Force
}
$exe=(Get-ChildItem -Path $OrcaDir -Recurse -File -ErrorAction SilentlyContinue | Where-Object {$_.Name -match '^orca-slicer.*\.exe$'} | Select-Object -First 1).FullName
if(!$exe){throw "OrcaSlicer executable introuvable"}

$profileRoot=(Get-ChildItem -Path $OrcaDir -Recurse -Directory -ErrorAction SilentlyContinue | Where-Object {$_.FullName -match 'resources\\profiles\\BBL$'} | Select-Object -First 1).FullName
if(!$profileRoot){throw "Profils BBL OrcaSlicer introuvables"}

function Find-FreeCADCmd {
  $candidates = New-Object System.Collections.Generic.List[string]

  function Add-Candidate([string]$p){
    if([string]::IsNullOrWhiteSpace($p)){return}
    try{
      $full=[IO.Path]::GetFullPath($p)
      if(!$candidates.Contains($full)){$candidates.Add($full)}
    }catch{}
  }

  # 1) Explicit override
  Add-Candidate $env:FREECAD_CMD

  # 2) Current user's Winget / per-user install location
  if($env:LOCALAPPDATA){
    Add-Candidate (Join-Path $env:LOCALAPPDATA "Programs\FreeCAD 1.1\bin\FreeCADCmd.exe")
    Add-Candidate (Join-Path $env:LOCALAPPDATA "Programs\FreeCAD 1.1\bin\freecadcmd.exe")
    Add-Candidate (Join-Path $env:LOCALAPPDATA "Programs\FreeCAD 1.0\bin\FreeCADCmd.exe")
    Add-Candidate (Join-Path $env:LOCALAPPDATA "Programs\FreeCAD 1.0\bin\freecadcmd.exe")
    Add-Candidate (Join-Path $env:LOCALAPPDATA "Programs\FreeCAD\bin\FreeCADCmd.exe")
    Add-Candidate (Join-Path $env:LOCALAPPDATA "Programs\FreeCAD\bin\freecadcmd.exe")

    try{
      Get-ChildItem (Join-Path $env:LOCALAPPDATA "Programs") -Directory -Filter "FreeCAD*" -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object {
          Add-Candidate (Join-Path $_.FullName "bin\FreeCADCmd.exe")
          Add-Candidate (Join-Path $_.FullName "bin\freecadcmd.exe")
        }
    }catch{}
  }

  # 3) Machine-wide Program Files installs
  $programRoots=@()
  if($env:ProgramFiles){$programRoots += $env:ProgramFiles}
  if(${env:ProgramFiles(x86)}){$programRoots += ${env:ProgramFiles(x86)}}
  $programRoots += "C:\Program Files"

  foreach($root in $programRoots | Select-Object -Unique){
    Add-Candidate (Join-Path $root "FreeCAD 1.1\bin\FreeCADCmd.exe")
    Add-Candidate (Join-Path $root "FreeCAD 1.0\bin\FreeCADCmd.exe")
    Add-Candidate (Join-Path $root "FreeCAD 0.21\bin\FreeCADCmd.exe")
    Add-Candidate (Join-Path $root "FreeCAD\bin\FreeCADCmd.exe")
    try{
      Get-ChildItem $root -Directory -Filter "FreeCAD*" -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        ForEach-Object {
          Add-Candidate (Join-Path $_.FullName "bin\FreeCADCmd.exe")
          Add-Candidate (Join-Path $_.FullName "bin\freecadcmd.exe")
        }
    }catch{}
  }

  # 4) PATH / App execution aliases
  foreach($name in @("FreeCADCmd.exe","freecadcmd.exe","FreeCADCmd","freecadcmd")){
    try{
      $cmd=Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
      if($cmd -and $cmd.Source){Add-Candidate $cmd.Source}
    }catch{}
  }

  foreach($p in $candidates){
    try{
      if(Test-Path -LiteralPath $p){
        return [IO.Path]::GetFullPath($p)
      }
    }catch{}
  }
  return $null
}

$FreeCADCmd=Find-FreeCADCmd
$FreeCADWorker=Join-Path $PSScriptRoot "freecad_converter.py"
$FreeCADStatePath=Join-Path $Workspace ".freecad_service.json"
$FreeCADEnabled=$true
try{
  if(Test-Path -LiteralPath $FreeCADStatePath){
    $savedState=Get-Content -LiteralPath $FreeCADStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if($null -ne $savedState.enabled){$FreeCADEnabled=[bool]$savedState.enabled}
  }
}catch{
  BridgeLog ("FreeCAD state read failed: "+$_.Exception.Message)
}
function Set-FreeCADEnabled([bool]$Enabled){
  $script:FreeCADEnabled=$Enabled
  try{
    @{enabled=$Enabled}|ConvertTo-Json -Compress|Set-Content -LiteralPath $FreeCADStatePath -Encoding UTF8
  }catch{
    BridgeLog ("FreeCAD state write failed: "+$_.Exception.Message)
  }
}
if($FreeCADCmd){
  BridgeLog ("FreeCAD detected: "+$FreeCADCmd)
}else{
  BridgeLog "FreeCADCmd.exe not detected. Converter will remain unavailable until FreeCAD is installed."
}


$listener=New-Object Net.HttpListener
$listener.Prefixes.Add("http://127.0.0.1:$Port/")
$listener.Start()
BridgeLog ("Listening on http://127.0.0.1:{0}/" -f $Port)

while($listener.IsListening){
  $ctx=$listener.GetContext()
  $workerExecutionId="";$workerJobStatePath="";$workerCancelPath="";$workerSliceProcess=$null
  $workerReceiptPreexisting=$false;$workerLaunchAttempted=$false
  try{
    $origin=[string]$ctx.Request.Headers["Origin"]
    if($origin -and $origin -notmatch '^http://(localhost|127\.0\.0\.1):5050$'){
      JsonResponse $ctx 403 @{ok=$false;error="Origine refusée"};continue
    }
    if($ctx.Request.HttpMethod -eq "OPTIONS"){JsonResponse $ctx 200 @{ok=$true};continue}
    $path=$ctx.Request.Url.AbsolutePath
    if($path -eq "/ping"){
      $ver=(Get-Item $exe).VersionInfo.ProductVersion
      JsonResponse $ctx 200 @{ok=$true;version=("OrcaSlicer "+$ver);bridge_version=$BridgeVersion;worker_job_protocol_version=1}
      continue
    }
    if($path -eq "/freecad-status"){
      $FreeCADCmd=Find-FreeCADCmd
      if(!$FreeCADEnabled){
        JsonResponse $ctx 200 @{ok=$true;available=$false;enabled=$false;error="Alpine Forge est arrêté"}
      }elseif($FreeCADCmd){
        $item=Get-Item -LiteralPath $FreeCADCmd
        $v=$item.VersionInfo.ProductVersion
        JsonResponse $ctx 200 @{
          ok=$true;available=$true;enabled=$true;version=$v;path=$FreeCADCmd;
          install_scope=if($env:LOCALAPPDATA -and $FreeCADCmd.StartsWith($env:LOCALAPPDATA,[StringComparison]::OrdinalIgnoreCase)){"user"}else{"machine"};
          formats=@("FCStd","STEP","STP","IGES","IGS","BREP","BRP","STL","OBJ","PLY","OFF","AMF","DXF","SVG","DAE","IV","WRL","VRML")
        }
      }else{
        JsonResponse $ctx 200 @{
          ok=$true;available=$false;enabled=$true;error="FreeCADCmd.exe introuvable";
          searched_user_root=if($env:LOCALAPPDATA){Join-Path $env:LOCALAPPDATA "Programs"}else{""};
          hint="Winget peut installer FreeCAD dans %LOCALAPPDATA%\Programs\FreeCAD 1.1\bin\freecadcmd.exe"
        }
      }
      continue
    }

    if($path -eq "/freecad-start" -and $ctx.Request.HttpMethod -eq "POST"){
      Set-FreeCADEnabled $true
      $FreeCADCmd=Find-FreeCADCmd
      if($FreeCADCmd){
        JsonResponse $ctx 200 @{ok=$true;available=$true;enabled=$true;message="Alpine Forge est démarré";path=$FreeCADCmd}
      }else{
        JsonResponse $ctx 409 @{ok=$false;available=$false;enabled=$true;error="FreeCADCmd.exe introuvable"}
      }
      continue
    }

    if($path -eq "/freecad-stop" -and $ctx.Request.HttpMethod -eq "POST"){
      Set-FreeCADEnabled $false
      JsonResponse $ctx 200 @{ok=$true;available=$false;enabled=$false;message="Alpine Forge est arrêté"}
      continue
    }

    if($path -eq "/convert" -and $ctx.Request.HttpMethod -eq "POST"){
      if(!$FreeCADEnabled){throw "Alpine Forge est arrêté. Démarre-le avant de convertir un fichier."}
      $FreeCADCmd=Find-FreeCADCmd
      if(!$FreeCADCmd){throw "FreeCAD n'est pas installé ou FreeCADCmd.exe est introuvable."}
      if(!(Test-Path $FreeCADWorker)){throw "freecad_converter.py est introuvable dans le dashboard."}

      $reader=New-Object IO.StreamReader($ctx.Request.InputStream,[Text.Encoding]::UTF8)
      $body=$reader.ReadToEnd()|ConvertFrom-Json

      $inputPath=SafePath ([string]$body.file)
      if(!(Test-Path $inputPath)){throw "Fichier source introuvable."}

      $format=([string]$body.format).Trim().ToLowerInvariant().TrimStart(".")
      $allowed=@("fcstd","step","stp","iges","igs","brep","brp","stl","obj","ply","off","amf","dxf","svg","dae","iv","wrl","vrml")
      if($allowed -notcontains $format){throw "Format de sortie FreeCAD non autorisé: $format"}

      $base=[IO.Path]::GetFileNameWithoutExtension([IO.Path]::GetFileName($inputPath))
      # Handle .gcode.3mf and .zip.amf double suffixes more nicely.
      if($base.EndsWith(".gcode",[StringComparison]::OrdinalIgnoreCase)){$base=$base.Substring(0,$base.Length-6)}
      if($base.EndsWith(".zip",[StringComparison]::OrdinalIgnoreCase)){$base=$base.Substring(0,$base.Length-4)}

      $requested=[string]$body.output_name
      if([string]::IsNullOrWhiteSpace($requested)){
        $outputName=$base+"_converted."+$format
      }else{
        $requested=[IO.Path]::GetFileName($requested)
        if(!$requested.ToLowerInvariant().EndsWith("."+$format)){$requested+="."+$format}
        $outputName=$requested
      }
      $outputPath=SafePath $outputName

      $linear=0.10
      try{if($null -ne $body.linear_deflection){$linear=[double]$body.linear_deflection}}catch{}
      if($linear -lt 0.001){$linear=0.001}
      if($linear -gt 10){$linear=10}

      $angular=0.523599
      try{if($null -ne $body.angular_deflection){$angular=[double]$body.angular_deflection}}catch{}

      $maxCadFacets=250000
      try{if($null -ne $body.max_cad_facets){$maxCadFacets=[int]$body.max_cad_facets}}catch{}
      if($maxCadFacets -lt 25000){$maxCadFacets=25000}
      if($maxCadFacets -gt 1000000){$maxCadFacets=1000000}

      $jobId=[Guid]::NewGuid().ToString("N")
      $fcOut=Join-Path $Workspace ("_freecad_"+$jobId+"_stdout.txt")
      $fcErr=Join-Path $Workspace ("_freecad_"+$jobId+"_stderr.txt")
      Remove-Item $fcOut,$fcErr -Force -ErrorAction SilentlyContinue
      if(Test-Path $outputPath){Remove-Item $outputPath -Force}

      BridgeLog ("FreeCAD convert: "+$inputPath+" -> "+$outputPath)
      # Windows PowerShell 5.1 can turn text written by native programs to
      # stderr into NativeCommandError. Start-Process keeps stdout/stderr native
      # and lets us evaluate the real process exit code instead.
      # FreeCADCmd runs a .py file passed as its first positional argument.
      # Its -c switch only opens FreeCAD's console; it does not execute a
      # Python command string, which previously made every conversion exit.
      # FreeCAD 1.1's command runner terminates when its script/input arguments
      # contain this Windows profile's accented absolute path. Run inside the
      # dashboard root and pass workspace-relative paths instead.
      $workerArg=".\\"+[IO.Path]::GetFileName($FreeCADWorker)
      $workspaceName=[IO.Path]::GetFileName($Workspace.TrimEnd([char[]]@([char]92,[char]47)))
      $inputArg=".\\"+$workspaceName+"\\"+[IO.Path]::GetFileName($inputPath)
      $outputArg=".\\"+$workspaceName+"\\"+[IO.Path]::GetFileName($outputPath)
      # Start-Process joins an argument array with spaces. Quote every value
      # explicitly so each path stays one argument.
      $rawArgs=@($workerArg,$inputArg,$outputArg,[string]$linear,[string]$angular,[string]$maxCadFacets)
      $argList=($rawArgs | ForEach-Object {'"'+$_+'"'}) -join " "
      $proc=Start-Process -FilePath $FreeCADCmd -ArgumentList $argList -Wait -PassThru -NoNewWindow `
        -WorkingDirectory $PSScriptRoot -RedirectStandardOutput $fcOut -RedirectStandardError $fcErr
      $code=$proc.ExitCode

      $stdout=if(Test-Path $fcOut){Get-Content $fcOut -Raw -ErrorAction SilentlyContinue}else{""}
      $stderr=if(Test-Path $fcErr){Get-Content $fcErr -Raw -ErrorAction SilentlyContinue}else{""}
      BridgeLog ("FreeCAD exit="+$code)
      if($stderr){BridgeLog ("FreeCAD stderr: "+$stderr)}

      $resultLine=($stdout -split "`r?`n" | Where-Object {$_ -like "RAPHAL_FREECAD_RESULT=*"} | Select-Object -Last 1)
      $workerResult=$null
      if($resultLine){
        try{$workerResult=($resultLine.Substring("RAPHAL_FREECAD_RESULT=".Length)|ConvertFrom-Json)}catch{}
      }

      if($code -ne 0 -or !(Test-Path $outputPath)){
        $sourceMb=[math]::Round((Get-Item $inputPath).Length/1MB,1)
        $msg=if($workerResult -and $workerResult.error){
          [string]$workerResult.error
        }elseif($stderr -and $stderr -notmatch '(?i)^Application unexpectedly terminated\s*$'){
          $stderr
        }else{
          "FreeCAD s'est arrêté pendant la conversion. Source: "+$sourceMb+" MB. Les très gros maillages sont maintenant réduits automatiquement avant STEP/IGES/BREP; vérifie aussi que le fichier source n'est pas corrompu."
        }
        throw $msg
      }

      JsonResponse $ctx 200 @{
        ok=$true;output=[IO.Path]::GetFileName($outputPath);
        bytes=(Get-Item $outputPath).Length;
        freecad_version=if($workerResult){$workerResult.freecad_version}else{(Get-Item $FreeCADCmd).VersionInfo.ProductVersion};
        objects=if($workerResult){$workerResult.objects}else{$null}
      }
      continue
    }

    if(($path -ne "/slice" -and $path -ne "/slice-multi") -or $ctx.Request.HttpMethod -ne "POST"){
      JsonResponse $ctx 404 @{ok=$false;error="Endpoint inconnu"};continue
    }

    $reader=New-Object IO.StreamReader($ctx.Request.InputStream,[Text.Encoding]::UTF8)
    $body=$reader.ReadToEnd()|ConvertFrom-Json

    $workerExecutionId=[string]$body.worker_execution_id
    if($workerExecutionId){
      if($workerExecutionId -notmatch '^[A-Za-z0-9_-]{1,128}$'){throw "Identité Worker invalide"}
      $workerJobsRoot=Join-Path $Workspace "_worker_jobs"
      $workerJobRoot=Join-Path $workerJobsRoot $workerExecutionId
      New-Item -ItemType Directory -Path $workerJobRoot -Force|Out-Null
      foreach($directory in @($workerJobsRoot,$workerJobRoot)){
        if(((Get-Item -LiteralPath $directory).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw "Dossier Worker redirigé non autorisé"}
      }
      $workerJobStatePath=Join-Path $workerJobRoot "status.json"
      $workerCancelPath=Join-Path $workerJobRoot "cancel.json"
      if(Test-Path -LiteralPath $workerJobStatePath){
        $workerReceiptPreexisting=$true
        $previous=Get-Content -LiteralPath $workerJobStatePath -Raw -Encoding UTF8|ConvertFrom-Json
        if($previous.execution_id -ne $workerExecutionId){throw "Journal Worker incohérent"}
        if($previous.state -eq "completed" -and $previous.engine_stop_confirmed){JsonResponse $ctx 200 $previous.result;continue}
        JsonResponse $ctx 409 @{ok=$false;error="Exécution déjà reçue : consulter son journal, sans relancer le tranchage."};continue
      }
      Set-WorkerSliceState "accepted" $false
    }

    if($path -eq "/slice-multi"){
      $inputPaths=@()
      foreach($f in @($body.files)){
        $fp=SafePath ([string]$f)
        if(!(Test-Path $fp)){throw "Fichier multi-filament introuvable: $f"}
        $inputPaths+=$fp
      }
      if($inputPaths.Count -lt 1){throw "Aucun fichier multi-filament"}
      $inputPath=$inputPaths[0]
    }else{
      $inputPath=SafePath $body.file
      if(!(Test-Path $inputPath)){throw "Fichier introuvable"}
      $inputPaths=@($inputPath)
    }

    $nozzle=[string]$body.nozzle
    if([string]::IsNullOrWhiteSpace($nozzle)){$nozzle="0.4"}
    $machine=Join-Path $profileRoot "machine\Bambu Lab P1S $nozzle nozzle.json"
    if(!(Test-Path $machine)){throw "Profil P1S $nozzle introuvable"}

    # Process profile: choose nearest stock BBL quality, then override web values.
    $lh=[double]$body.layer_height
    $processName = if($lh -le .13){"0.12mm Fine @BBL X1C.json"} elseif($lh -le .18){"0.16mm Optimal @BBL X1C.json"} elseif($lh -le .22){"0.20mm Standard @BBL X1C.json"} else {"0.24mm Draft @BBL X1C.json"}
    $process=Join-Path $profileRoot ("process\"+$processName)
    if(!(Test-Path $process)){ $process=(Get-ChildItem (Join-Path $profileRoot "process") -File | Where-Object {$_.Name -like "*0.20mm*X1C*.json"} | Select-Object -First 1).FullName }
    if(!$process){throw "Profil process BBL introuvable"}

    $filaDir=Join-Path $profileRoot "filament"
    function Find-FilamentProfile([string]$mat){
      $found=Find-P1SFilamentProfile $filaDir $mat $nozzle
      if(!$found){throw ("Aucun profil filament « "+$mat+" » compatible avec la Bambu Lab P1S (buse "+$nozzle+" mm) dans OrcaSlicer : choisis un autre matériau ou mets OrcaSlicer à jour.")}
      return $found
    }
    $bedType=[string]$body.bed_type
    if(!$BedTypes.Contains($bedType)){$bedType="Textured PEI Plate"}
    if($path -eq "/slice-multi"){
      $filamentProfiles=@()
      foreach($m in @($body.materials)){
        $fp=Find-FilamentProfile ([string]$m)
        if(!$fp){throw "Profil filament introuvable: $m"}
        $filamentProfiles+=$fp
      }
      while($filamentProfiles.Count -lt $inputPaths.Count){$filamentProfiles+=$filamentProfiles[-1]}
      $filament=($filamentProfiles -join ";")
    }else{
      $mat=[string]$body.material
      $filament=Find-FilamentProfile $mat
      if(!$filament){throw "Profil filament introuvable"}
      $filamentProfiles=@($filament)
    }

    if($null -eq $body.outer_wall_line_width){$body|Add-Member -NotePropertyName outer_wall_line_width -NotePropertyValue 0.42 -Force}
    if($null -eq $body.support_threshold_angle){$body|Add-Member -NotePropertyName support_threshold_angle -NotePropertyValue 30 -Force}
    if($null -eq $body.outer_wall_speed){$body|Add-Member -NotePropertyName outer_wall_speed -NotePropertyValue 200 -Force}
    if($null -eq $body.sparse_infill_speed){$body|Add-Member -NotePropertyName sparse_infill_speed -NotePropertyValue 270 -Force}
    if($null -eq $body.travel_speed){$body|Add-Member -NotePropertyName travel_speed -NotePropertyValue 500 -Force}
    if($null -eq $body.seam_position){$body|Add-Member -NotePropertyName seam_position -NotePropertyValue "aligned" -Force}
    if($null -eq $body.ironing){$body|Add-Member -NotePropertyName ironing -NotePropertyValue $false -Force}
    if($null -eq $body.fuzzy_skin){$body|Add-Member -NotePropertyName fuzzy_skin -NotePropertyValue $false -Force}

    $base=if($path -eq "/slice-multi"){"raphal_multimaterial"}else{[IO.Path]::GetFileNameWithoutExtension($inputPath)}
    if($workerExecutionId){$base="worker_"+$workerExecutionId}
    $requestedName=$base+"_sliced.3mf"
    $requestedPath=Join-Path $Workspace $requestedName
    $finalName=$base+"_sliced.gcode.3mf"
    $finalPath=Join-Path $Workspace $finalName

    foreach($p in @($requestedPath,$finalPath)){
      if(Test-Path $p){Remove-Item $p -Force -ErrorAction SilentlyContinue}
    }

    # Build a custom process JSON. Orca profile values are strings in the bundled profiles.
      $sliceJobId=[Guid]::NewGuid().ToString("N")
      $customProcess=Join-Path $Workspace ("_raphal_process_"+$sliceJobId+".json")
    # The CLI loads a profile file verbatim: "inherits" is only resolved by the
    # GUI preset bundle. Without the parent chain the P1S bed became Orca's
    # 200x200x100 default and any part taller than 100 mm was refused.
    function Resolve-OrcaProfile([string]$ProfilePath){
      $dir=Split-Path $ProfilePath -Parent
      $chain=@();$current=$ProfilePath;$guard=0
      while($current -and (Test-Path -LiteralPath $current) -and $guard -lt 20){
        $node=Get-Content -LiteralPath $current -Raw -Encoding UTF8|ConvertFrom-Json
        $chain+=,$node
        $parent=[string]$node.inherits
        $current=if([string]::IsNullOrWhiteSpace($parent)){$null}else{Join-Path $dir ($parent+".json")}
        $guard++
      }
      $merged=[ordered]@{}
      for($i=$chain.Count-1;$i -ge 0;$i--){
        foreach($prop in $chain[$i].PSObject.Properties){if($prop.Name -ne "inherits"){$merged[$prop.Name]=$prop.Value}}
      }
      return [pscustomobject]$merged
    }
    $flatProfiles=@()
    function Write-OrcaProfile($Object,[string]$Label){
      $target=Join-Path $Workspace ("_raphal_"+$Label+"_"+$sliceJobId+".json")
      $Object|ConvertTo-Json -Depth 50|Set-Content -Path $target -Encoding UTF8
      $script:flatProfiles+=$target
      return $target
    }
    $machine=Write-OrcaProfile (Resolve-OrcaProfile $machine) "machine"
    $stockProcess=Write-OrcaProfile (Resolve-OrcaProfile $process) "process_stock"
    $flatFilaments=@();$filamentIndex=0
    foreach($fp in $filamentProfiles){
      $flatFilament=Resolve-OrcaProfile $fp
      Set-FilamentBedTemperatures $flatFilament $bedType
      $flatFilaments+=Write-OrcaProfile $flatFilament ("filament"+$filamentIndex);$filamentIndex++
    }
    $filament=($flatFilaments -join ";")
    $procObj=Resolve-OrcaProfile $process

    function Set-ProcessValue([string]$Name, [string]$Value){
      $existing=$procObj.PSObject.Properties[$Name]
      if($null -eq $existing){$procObj | Add-Member -NotePropertyName $Name -NotePropertyValue $Value}
      else{$procObj.$Name=$Value}
    }

    Set-ProcessValue "layer_height" ([string]$body.layer_height)
    Set-ProcessValue "outer_wall_line_width" ([string]$body.outer_wall_line_width)
    Set-ProcessValue "wall_loops" ([string]$body.wall_loops)
    Set-ProcessValue "top_shell_layers" ([string]$body.top_shell_layers)
    Set-ProcessValue "bottom_shell_layers" ([string]$body.bottom_shell_layers)
    Set-ProcessValue "sparse_infill_density" (([string]$body.sparse_infill_density)+"%")
    Set-ProcessValue "sparse_infill_pattern" ([string]$body.sparse_infill_pattern)
    Set-ProcessValue "enable_support" $(if([bool]$body.enable_support){"1"}else{"0"})
    Set-ProcessValue "support_threshold_angle" ([string]$body.support_threshold_angle)
    Set-ProcessValue "support_type" ([string]$body.support_type)
    Set-ProcessValue "brim_type" ([string]$body.brim_type)
    Set-ProcessValue "brim_width" ([string]$body.brim_width)
    Set-ProcessValue "outer_wall_speed" ([string]$body.outer_wall_speed)
    Set-ProcessValue "sparse_infill_speed" ([string]$body.sparse_infill_speed)
    Set-ProcessValue "travel_speed" ([string]$body.travel_speed)
    Set-ProcessValue "seam_position" ([string]$body.seam_position)
    Set-ProcessValue "ironing_type" $(if([bool]$body.ironing){"top"}else{"no ironing"})
    Set-ProcessValue "fuzzy_skin" $(if([bool]$body.fuzzy_skin){"external"}else{"none"})
    Set-ProcessValue "enable_prime_tower" $(if([bool]$body.prime_tower){"1"}else{"0"})
    Set-ProcessValue "curr_bed_type" $bedType

    # The advanced Model Studio panel is populated from the installed Bambu
    # profile chain.  Apply those validated profile keys after the compact
    # controls above so an advanced value is never silently ignored.
    if($null -ne $body.process_settings){
      foreach($setting in $body.process_settings.PSObject.Properties){
        $settingName=[string]$setting.Name
        if($settingName -notmatch '^[A-Za-z0-9_]+$'){continue}
        $settingValue=$setting.Value
        if($settingValue -is [bool]){$settingValue=if($settingValue){"1"}else{"0"}}
        if($null -ne $settingValue){Set-ProcessValue $settingName ([string]$settingValue)}
      }
    }

    $procObj | ConvertTo-Json -Depth 50 | Set-Content -Path $customProcess -Encoding UTF8

    function Invoke-OrcaSlice([string]$ProcessProfile,[string]$Mode){
      if($workerCancelPath -and (Test-Path -LiteralPath $workerCancelPath)){
        $cancel=Get-Content -LiteralPath $workerCancelPath -Raw -Encoding UTF8|ConvertFrom-Json
        if($cancel.execution_id -eq $workerExecutionId){Set-WorkerSliceState "cancelled" $true;throw "Tranchage annulé avant exécution"}
      }
      $stdout=Join-Path $Workspace ("_orca_"+$Mode+"_stdout.txt")
      $stderr=Join-Path $Workspace ("_orca_"+$Mode+"_stderr.txt")
      $resultJson=Join-Path $Workspace "result.json"
      Remove-Item $stdout,$stderr,$resultJson -Force -ErrorAction SilentlyContinue

      $orcaArgs=@(
        "--allow-newer-file",
        "--debug","3",
        "--load-settings",$machine,
        "--load-settings",$ProcessProfile,
        "--load-filaments",$filament
      )
      if($path -eq "/slice-multi"){
        $ids=1..$inputPaths.Count
        $orcaArgs+=@("--load-filament-ids",($ids -join ","))
      }
      if([bool]$body.orient){$orcaArgs+=@("--orient","1")}
      if([bool]$body.arrange){$orcaArgs+=@("--arrange","1")}
      $orcaArgs+=@(
        "--ensure-on-bed",
        "--slice","0",
        "--outputdir",$Workspace,
        # With --outputdir Orca expects a file name here. Passing the absolute
        # path made it build "<workspace>/C:\..." and abort the export.
        "--export-3mf",$requestedName
      )
      if($path -eq "/slice-multi"){$orcaArgs+=$inputPaths}else{$orcaArgs+=$inputPath}

      BridgeLog ("Slice "+$Mode+": "+$exe+" "+($orcaArgs -join " "))
      # orca-slicer.exe is a Windows GUI-subsystem executable. Invoking it with
      # `&` returns immediately and leaves $LASTEXITCODE empty, so the bridge
      # used to inspect the output before Orca had even started slicing.
      # Start-Process -Wait gives us the real process lifetime and exit code.
      $argumentList=($orcaArgs | ForEach-Object {
        '"'+([string]$_).Replace('"','\"')+'"'
      }) -join ' '
      $script:workerSliceProcess=$null
      $script:workerLaunchAttempted=$true
      $proc=Start-Process -FilePath $exe -ArgumentList $argumentList -PassThru `
        -WorkingDirectory $OrcaDir -WindowStyle Hidden `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr
      $script:workerSliceProcess=$proc
      Set-WorkerSliceState "running" $false
      while(!$proc.WaitForExit(250)){
        if($workerCancelPath -and (Test-Path -LiteralPath $workerCancelPath)){
          $cancel=Get-Content -LiteralPath $workerCancelPath -Raw -Encoding UTF8|ConvertFrom-Json
          if($cancel.execution_id -eq $workerExecutionId){
            # This Process object owns the exact slicer spawned above. Never
            # kill the bridge, another job, or an executable by global name.
            $proc.Kill();$proc.WaitForExit()
            Set-WorkerSliceState "cancelled" $true
            throw "Tranchage annulé ; arrêt du processus Orca confirmé"
          }
        }
      }
      $proc.WaitForExit()
      $code=$proc.ExitCode
      BridgeLog ("Slice "+$Mode+" exit="+$code)

      $text=""
      if(Test-Path $stderr){$text+=(Get-Content $stderr -Raw -ErrorAction SilentlyContinue)}
      if(Test-Path $stdout){$text+="`n"+(Get-Content $stdout -Raw -ErrorAction SilentlyContinue)}
      if(Test-Path $resultJson){
        $resultText=Get-Content $resultJson -Raw -ErrorAction SilentlyContinue
        $text+="`nresult.json:`n"+$resultText
        BridgeLog ("Orca result.json "+$Mode+": "+$resultText)
      }
      return @{code=$code;text=$text;mode=$Mode}
    }

    # Attempt 1: custom process from dashboard.
    $attempt=Invoke-OrcaSlice $customProcess "custom"

    # Detect any newly-created 3MF if Orca ignored the exact requested path.
    if(!(Test-Path $requestedPath)){
      $candidate=Get-ChildItem $Workspace -File -Filter "*.3mf" -ErrorAction SilentlyContinue |
        Where-Object {$_.LastWriteTime -gt (Get-Date).AddMinutes(-3) -and $_.Name -like ($base+"*")} |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
      if($candidate){$requestedPath=$candidate.FullName;$requestedName=$candidate.Name}
    }

    # Attempt 2: known-good stock BBL process if custom JSON is rejected.
    $fallbackUsed=$false
    if(!(Test-Path $requestedPath)){
      $fallbackUsed=$true
      $attempt=Invoke-OrcaSlice $stockProcess "stock"
      if(!(Test-Path $requestedPath)){
        $candidate=Get-ChildItem $Workspace -File -Filter "*.3mf" -ErrorAction SilentlyContinue |
          Where-Object {$_.LastWriteTime -gt (Get-Date).AddMinutes(-3) -and $_.Name -like ($base+"*")} |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if($candidate){$requestedPath=$candidate.FullName;$requestedName=$candidate.Name}
      }
    }

    if(!(Test-Path $requestedPath)){
      $err=[string]$attempt.text
      if($err.Length -gt 7000){$err=$err.Substring($err.Length-7000)}
      throw ("OrcaSlicer n'a produit aucun 3MF. Mode="+$attempt.mode+" ExitCode="+$attempt.code+"`n"+$err)
    }

    # Verify that this is actually a sliced Bambu/Orca 3MF (contains plate gcode).
    $isSliced=$false
    try{
      Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
      $zip=[IO.Compression.ZipFile]::OpenRead($requestedPath)
      try{
        foreach($entry in $zip.Entries){
          if($entry.FullName -match '^Metadata/plate_\d+\.gcode$'){$isSliced=$true;break}
        }
      }finally{$zip.Dispose()}
    }catch{}

    if(!$isSliced){
      throw ("OrcaSlicer a créé un 3MF, mais sans Metadata/plate_X.gcode. Fichier="+$requestedName+" Mode="+$attempt.mode)
    }

    # Normalize extension for the dashboard / P1S workflow.
    if($requestedPath -ne $finalPath){
      Move-Item -Path $requestedPath -Destination $finalPath -Force
    }
    Remove-Item $customProcess -Force -ErrorAction SilentlyContinue
    foreach($flat in $flatProfiles){Remove-Item $flat -Force -ErrorAction SilentlyContinue}

    $sliceResult=@{
      ok=$true
      output=$finalName
      exit_code=$attempt.code
      profile_mode=$attempt.mode
      fallback_used=$fallbackUsed
      bridge_version=$BridgeVersion
      bed_type=$bedType
      filament_profiles=@($filamentProfiles|ForEach-Object {[IO.Path]::GetFileNameWithoutExtension([string]$_)})
    }
    Set-WorkerSliceState "completed" $true $sliceResult
    JsonResponse $ctx 200 $sliceResult
  }catch{
    if($workerJobStatePath -and !$workerReceiptPreexisting){
      $previous=$null
      if(Test-Path -LiteralPath $workerJobStatePath){try{$previous=Get-Content -LiteralPath $workerJobStatePath -Raw -Encoding UTF8|ConvertFrom-Json}catch{}}
      if($previous.state -notin @("completed","cancelled","failed")){
        $confirmed=if($null -eq $workerSliceProcess){!$workerLaunchAttempted}else{$workerSliceProcess.HasExited}
        Set-WorkerSliceState $(if($confirmed){"failed"}else{"uncertain"}) $confirmed $null $_.Exception.Message
      }
    }
    JsonResponse $ctx 400 @{ok=$false;error=$_.Exception.Message}
  }
}
