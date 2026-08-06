#Requires -Version 5.1
<#
  BDO Texture AIO - menu wrapper around tools\bdo_tex.py
  Everything real lives in the Python CLI; this only resolves Python and
  sequences the stages.

  Note: static menu strings use single quotes so PowerShell never treats
  characters like / as operators inside double-quoted expansions.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$AppRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Cli     = Join-Path $AppRoot 'tools\bdo_tex.py'
$Version = '1.3.0'

function Resolve-Python {
    foreach ($c in @('python', 'py')) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) {
            $v = & $cmd.Source -c "import sys;print(sys.version_info[0])" 2>$null
            if ($v -eq '3') { return $cmd.Source }
        }
    }
    throw 'Python 3 not found. Install it with:  winget install Python.Python.3.12'
}

function Invoke-Cli {
    param([string[]]$CliArgs)
    $env:PYTHONIOENCODING = 'utf-8'
    & $script:Python $Cli @CliArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host ('  (command exited {0})' -f $LASTEXITCODE) -ForegroundColor Yellow
    }
}

function Get-Config {
    $p = Join-Path $AppRoot 'config.json'
    if (-not (Test-Path $p)) {
        Copy-Item (Join-Path $AppRoot 'config.example.json') $p
        Write-Host 'Created config.json from the example - check the paths in it.' -ForegroundColor Yellow
    }
    # Strip BOM if present (Windows PowerShell utf8 often writes one).
    $raw = [System.IO.File]::ReadAllText($p)
    if ($raw.Length -gt 0 -and [int][char]$raw[0] -eq 0xFEFF) {
        $raw = $raw.Substring(1)
    }
    $cfg = $raw | ConvertFrom-Json
    # Isolate: relative workDir always under this app (shareable SSD install).
    if (-not $cfg.workDir -or [string]::IsNullOrWhiteSpace([string]$cfg.workDir)) {
        $cfg.workDir = 'work'
    }
    $wd = [string]$cfg.workDir
    if (-not [System.IO.Path]::IsPathRooted($wd)) {
        $cfg | Add-Member -NotePropertyName workDirResolved -NotePropertyValue (Join-Path $AppRoot $wd) -Force
    } else {
        $cfg | Add-Member -NotePropertyName workDirResolved -NotePropertyValue $wd -Force
    }
    $cfg
}

function Save-Config {
    param($Cfg)
    $p = Join-Path $AppRoot 'config.json'
    # Drop runtime-only fields; keep workDir portable ("work") when under the app.
    $toSave = $Cfg | Select-Object * -ExcludeProperty workDirResolved
    $wd = [string]$toSave.workDir
    if ($wd -and [System.IO.Path]::IsPathRooted($wd)) {
        $appFull = [System.IO.Path]::GetFullPath($AppRoot).TrimEnd('\')
        $workFull = [System.IO.Path]::GetFullPath($wd)
        if ($workFull.StartsWith($appFull, [System.StringComparison]::OrdinalIgnoreCase)) {
            $rel = $workFull.Substring($appFull.Length).TrimStart('\')
            if ([string]::IsNullOrWhiteSpace($rel)) { $rel = 'work' }
            $toSave.workDir = $rel.Replace('\', '/')
        }
    }
    # UTF-8 WITHOUT BOM - Set-Content -Encoding utf8 adds a BOM that breaks Python json.
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($p, ($toSave | ConvertTo-Json -Depth 6), $utf8NoBom)
}

function Set-ConfigValue {
    param([string]$Key, $Value)
    $cfg = Get-Config
    $cfg.$Key = $Value
    Save-Config $cfg
}

function Get-Presets {
    $p = Join-Path $AppRoot 'presets.json'
    if (-not (Test-Path $p)) { return $null }
    $raw = [System.IO.File]::ReadAllText($p)
    if ($raw.Length -gt 0 -and [int][char]$raw[0] -eq 0xFEFF) {
        $raw = $raw.Substring(1)
    }
    $raw | ConvertFrom-Json
}

function Get-ActivePresetName {
    param($Cfg, $Presets)
    if (-not $Presets) { return $null }
    foreach ($prop in $Presets.PSObject.Properties) {
        $pr = $prop.Value
        $sameTarget = [int]$Cfg.target -eq [int]$pr.target
        $sameMin    = [int]$Cfg.minSize -eq [int]$pr.minSize
        $sameMax    = [int]$Cfg.maxOutput -eq [int]$pr.maxOutput
        $sameModel  = [string]$Cfg.model -eq [string]$pr.model
        if ($sameTarget -and $sameMin -and $sameMax -and $sameModel) {
            return $prop.Name
        }
    }
    return $null
}

function Apply-Preset {
    param([string]$Name)
    $presets = Get-Presets
    if (-not $presets) {
        Write-Host '  presets.json missing.' -ForegroundColor Red
        return
    }
    $pr = $presets.$Name
    if (-not $pr) {
        Write-Host ('  Unknown preset: {0}' -f $Name) -ForegroundColor Red
        return
    }
    $cfg = Get-Config
    $cfg.target = [int]$pr.target
    $cfg.minSize = [int]$pr.minSize
    $cfg.maxOutput = [int]$pr.maxOutput
    $cfg.model = [string]$pr.model
    if ($cfg.PSObject.Properties.Name -contains 'activePreset') {
        $cfg.activePreset = $Name
    } else {
        $cfg | Add-Member -NotePropertyName activePreset -NotePropertyValue $Name -Force
    }
    Save-Config $cfg
    Write-Host ''
    Write-Host ('  Applied preset: {0}' -f $pr.label) -ForegroundColor Green
    Write-Host ('    target {0}px   min >{1}px   maxOut {2}px' -f $pr.target, $pr.minSize, $pr.maxOutput)
    Write-Host ('    model  {0}' -f $pr.model)
    Write-Host '  Re-run [1] Scan - the size gate depends on target.' -ForegroundColor Yellow
}

function Show-PresetMenu {
    $presets = Get-Presets
    if (-not $presets) {
        Write-Host '  presets.json missing.' -ForegroundColor Red
        return
    }
    $cfg = Get-Config
    $active = Get-ActivePresetName $cfg $presets
    $keys = @($presets.PSObject.Properties.Name)
    Write-Host ''
    Write-Host '  Quality presets' -ForegroundColor Cyan
    Write-Host '  ---------------'
    for ($i = 0; $i -lt $keys.Count; $i++) {
        $k = $keys[$i]
        $pr = $presets.$k
        $mark = ''
        $color = 'White'
        if ($active -eq $k) {
            $mark = ' *'
            $color = 'Green'
        }
        Write-Host (('  [{0}] {1}{2}' -f ($i + 1), $pr.label, $mark)) -ForegroundColor $color
        Write-Host (('      target {0}  min>{1}  maxOut {2}  model {3}' -f $pr.target, $pr.minSize, $pr.maxOutput, $pr.model)) -ForegroundColor DarkGray
        Write-Host (('      {0}' -f $pr.description)) -ForegroundColor DarkGray
        Write-Host ''
    }
    Write-Host '  [0] Cancel'
    Write-Host ''
    $sel = Read-Host '  choice'
    $n = 0
    if ([int]::TryParse($sel, [ref]$n) -and $n -ge 1 -and $n -le $keys.Count) {
        Apply-Preset $keys[$n - 1]
    }
}

function Show-Menu {
    $cfg = Get-Config
    $presets = Get-Presets
    $active = Get-ActivePresetName $cfg $presets
    if ($active -and $presets) {
        $presetLabel = [string]$presets.$active.label
    } else {
        $presetLabel = 'custom'
    }

    Clear-Host
    Write-Host '==================================================' -ForegroundColor Cyan
    Write-Host ('  BDO Texture AIO  v{0}' -f $Version) -ForegroundColor Cyan
    Write-Host '  World textures only - BDO-AIO body/pube choices always win' -ForegroundColor DarkGray
    Write-Host '==================================================' -ForegroundColor Cyan
    Write-Host ''
    $roots = @($cfg.roots)
    $charOn = $false
    foreach ($r in $roots) {
        if (("" + $r).ToLower().Replace('\', '/').StartsWith('character/texture')) {
            $charOn = $true
            break
        }
    }
    $charLabel = if ($charOn) { 'ON' } else { 'OFF (default)' }

    Write-Host ('  preset {0}' -f $presetLabel) -ForegroundColor Green
    Write-Host ('  target {0}px   min >{1}px   maxOut {2}px' -f $cfg.target, $cfg.minSize, $cfg.maxOutput) -ForegroundColor DarkGray
    Write-Host ('  model  {0}' -f $cfg.model) -ForegroundColor DarkGray
    Write-Host ('  character textures: {0}' -f $charLabel) -ForegroundColor $(if ($charOn) { 'Yellow' } else { 'DarkGray' })
    $workShow = if ($cfg.workDirResolved) { $cfg.workDirResolved } else { $cfg.workDir }
    Write-Host ('  work   {0}' -f $workShow) -ForegroundColor DarkGray
    Write-Host ''
    $matsOn = $true
    if ($null -ne $cfg.materialsEnabled) { $matsOn = [bool]$cfg.materialsEnabled }
    $matsLabel = if ($matsOn) { 'ON (existing only)' } else { 'OFF' }
    Write-Host ('  companion maps: {0}' -f $matsLabel) -ForegroundColor $(if ($matsOn) { 'Green' } else { 'DarkGray' })
    $lodOn = $false
    if ($null -ne $cfg.includeLodBillboards) { $lodOn = [bool]$cfg.includeLodBillboards }
    $lodLabel = if ($lodOn) { 'ON (high option)' } else { 'OFF (default)' }
    Write-Host ('  LOD/billboards: {0}' -f $lodLabel) -ForegroundColor $(if ($lodOn) { 'Yellow' } else { 'DarkGray' })
    Write-Host ''
    Write-Host '  [1] Scan game textures        (build the candidate list)'
    Write-Host '  [2] Extract to PNG'
    Write-Host '  [3] Upscale with Upscayl      BASIC - fast, GPU'
    Write-Host '  [4] Match companion maps      (resize existing _n/_sp/... only)' -ForegroundColor Yellow
    Write-Host '  [5] Pack to DDS               (albedo + companions)'
    Write-Host '  [6] Stage for Meta Injector'
    Write-Host ''
    Write-Host '  [7] Run 1-6 in one go' -ForegroundColor Green
    Write-Host ''
    Write-Host '  [A] Advanced: export for SwarmUI / ComfyUI'
    Write-Host '  [B] Advanced: pack from SwarmUI output'
    Write-Host ''
    Write-Host '  [P] Quality presets           (playtest, quality, balanced, ...)' -ForegroundColor Yellow
    Write-Host '  [T] Target size only         (1024, 1440, 2048, or custom)'
    Write-Host '  [M] Upscaler model only'
    Write-Host '  [C] Toggle character textures (NPC/monster - OFF by default)' -ForegroundColor Yellow
    Write-Host '  [L] Toggle LOD/billboards     (distance art - OFF by default)' -ForegroundColor Yellow
    Write-Host '  [N] Toggle companion-map matching' -ForegroundColor Yellow
    Write-Host '  [S] Status'
    Write-Host '  [V] Verify (run the self-checks)'
    Write-Host '  [R] Remove this app staged layer'
    Write-Host '  [Q] Quit'
    Write-Host ''
}

function Toggle-Materials {
    $cfg = Get-Config
    $cur = $true
    if ($null -ne $cfg.materialsEnabled) { $cur = [bool]$cfg.materialsEnabled }
    $cfg.materialsEnabled = -not $cur
    Save-Config $cfg
    if ($cfg.materialsEnabled) {
        Write-Host '  companion maps: ON' -ForegroundColor Green
        Write-Host '  Only resizes maps the archive already has (no invent).' -ForegroundColor DarkGray
    } else {
        Write-Host '  companion maps: OFF' -ForegroundColor Yellow
    }
}

function Toggle-LodBillboards {
    $cfg = Get-Config
    $cur = $false
    if ($null -ne $cfg.includeLodBillboards) { $cur = [bool]$cfg.includeLodBillboards }
    $cfg.includeLodBillboards = -not $cur
    Save-Config $cfg
    if ($cfg.includeLodBillboards) {
        Write-Host '  LOD/billboards: ON (high option)' -ForegroundColor Yellow
        Write-Host '  Distance LODs + SpeedTree billboards will be scanned.' -ForegroundColor DarkGray
        Write-Host '  Low visual ROI; more time/VRAM. Re-run [1] Scan.' -ForegroundColor DarkGray
    } else {
        Write-Host '  LOD/billboards: OFF (default - recommended)' -ForegroundColor Green
        Write-Host '  Re-run [1] Scan so the candidate list drops them.' -ForegroundColor DarkGray
    }
}

function Toggle-CharacterRoots {
    $cfg = Get-Config
    $list = [System.Collections.Generic.List[string]]::new()
    foreach ($r in @($cfg.roots)) {
        if ($null -ne $r -and "$r" -ne '') { [void]$list.Add([string]$r) }
    }
    $idx = -1
    for ($i = 0; $i -lt $list.Count; $i++) {
        $norm = $list[$i].ToLower().Replace('\', '/').TrimEnd('/') + '/'
        if ($norm.StartsWith('character/texture/')) { $idx = $i; break }
    }
    if ($idx -ge 0) {
        $list.RemoveAt($idx)
        Write-Host '  character/texture: OFF (world only)' -ForegroundColor Green
    } else {
        $list.Insert(0, 'character/texture/')
        Write-Host '  character/texture: ON (NPC/monster/mount skins)' -ForegroundColor Yellow
        Write-Host '  Playable-class p* prefixes stay excluded always.' -ForegroundColor DarkGray
    }
    $cfg.roots = @($list.ToArray())
    Save-Config $cfg
    Write-Host '  Re-run [1] Scan so the candidate list matches.' -ForegroundColor Yellow
}

function Invoke-Full {
    Write-Host ''
    Write-Host '--- 1/6 scan ---' -ForegroundColor Cyan
    Invoke-Cli @('scan')
    Write-Host ''
    Write-Host '--- 2/6 extract ---' -ForegroundColor Cyan
    Invoke-Cli @('extract')
    Write-Host ''
    Write-Host '--- 3/6 upscale ---' -ForegroundColor Cyan
    Invoke-Cli @('upscale')
    Write-Host ''
    Write-Host '--- 4/6 companion maps (existing only) ---' -ForegroundColor Cyan
    Invoke-Cli @('materials')
    Write-Host ''
    Write-Host '--- 5/6 pack ---' -ForegroundColor Cyan
    Invoke-Cli @('pack')
    Write-Host ''
    Write-Host '--- 6/6 stage ---' -ForegroundColor Cyan
    Invoke-Cli @('stage')
    Write-Host ''
    Write-Host 'Done. Now run Meta Injector on your PAZ folder to apply it.' -ForegroundColor Green
}

function Set-Target {
    Write-Host ''
    Write-Host '  Quick target only (prefer [P] presets for full recommended sets)'
    Write-Host '  [1] 1024   [2] 1440   [3] 2048   [4] custom'
    $c = Read-Host '  choice'
    switch ($c) {
        '1' { Set-ConfigValue 'target' 1024 }
        '2' { Set-ConfigValue 'target' 1440 }
        '3' { Set-ConfigValue 'target' 2048 }
        '4' {
            $v = Read-Host '  target long edge in pixels'
            $n = 0
            if ([int]::TryParse($v, [ref]$n)) {
                Set-ConfigValue 'target' $n
            } else {
                Write-Host '  not a number' -ForegroundColor Red
            }
        }
        default { return }
    }
    Write-Host '  Target changed - re-run [1] Scan, the size gate depends on it.' -ForegroundColor Yellow
}

function Set-Model {
    $cfg = Get-Config
    $models = @(Get-ChildItem (Join-Path $cfg.upscaylModels '*.param') -ErrorAction SilentlyContinue |
        ForEach-Object { $_.BaseName })
    if ($models.Count -eq 0) {
        Write-Host ('  No models found in {0}' -f $cfg.upscaylModels) -ForegroundColor Red
        return
    }
    Write-Host ''
    for ($i = 0; $i -lt $models.Count; $i++) {
        Write-Host ('  [{0}] {1}' -f ($i + 1), $models[$i])
    }
    $sel = Read-Host '  choice'
    $n = 0
    if ([int]::TryParse($sel, [ref]$n) -and $n -ge 1 -and $n -le $models.Count) {
        Set-ConfigValue 'model' $models[$n - 1]
        Write-Host ('  model = {0}' -f $models[$n - 1]) -ForegroundColor Green
    }
}

$script:Python = Resolve-Python

while ($true) {
    Show-Menu
    $choice = (Read-Host '  choice').Trim().ToUpperInvariant()
    switch ($choice) {
        '1' { Invoke-Cli @('scan') }
        '2' { Invoke-Cli @('extract') }
        '3' { Invoke-Cli @('upscale') }
        '4' { Invoke-Cli @('materials') }
        '5' { Invoke-Cli @('pack') }
        '6' { Invoke-Cli @('stage') }
        '7' { Invoke-Full }
        'A' { Invoke-Cli @('swarm-export') }
        'B' { Invoke-Cli @('pack', '--source', 'swarm') }
        'P' { Show-PresetMenu }
        'T' { Set-Target }
        'M' { Set-Model }
        'C' { Toggle-CharacterRoots }
        'L' { Toggle-LodBillboards }
        'N' { Toggle-Materials }
        'S' { Invoke-Cli @('status') }
        'V' {
            $env:PYTHONIOENCODING = 'utf-8'
            & $script:Python (Join-Path $AppRoot 'tools\test_bdo_tex.py')
        }
        'R' { Invoke-Cli @('unstage', '--yes') }
        'Q' { break }
        default { continue }
    }
    Write-Host ''
    Read-Host '  press Enter' | Out-Null
}
