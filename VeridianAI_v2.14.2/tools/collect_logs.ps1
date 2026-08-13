# =====================================================================
#  VeridianAI - diagnostic collector
#
#  Gathers everything needed to diagnose a failed or partial launch WITHOUT
#  ever reaching into C:\Program Files\WindowsApps (which is not accessible
#  and must not be taken ownership of). Everything here comes from logs,
#  process/port state, and the local API.
#
#  Run:
#     powershell -ExecutionPolicy Bypass -File "<path>\collect_logs.ps1"
#
#  Writes vai_logs.txt next to this script AND to %TEMP%.
# =====================================================================

$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference    = 'SilentlyContinue'

$lines = New-Object System.Collections.Generic.List[string]
function W([string]$s = '') { $lines.Add($s) | Out-Null }
function Section([string]$t) { W ''; W ('=' * 70); W "  $t"; W ('=' * 70) }

# --- Redaction --------------------------------------------------------
# Logs can contain the API bearer token. Never hand that to anyone.
function Scrub([string]$t) {
    if ($null -eq $t) { return '' }
    $t = [regex]::Replace($t, 'ora_[A-Za-z0-9_\-]{8,}', 'ora_<redacted>')
    $t = [regex]::Replace($t, '(?i)(bearer\s+)\S+',     '${1}<redacted>')
    $t = [regex]::Replace($t, '(?i)("?(api[_-]?key|token|secret)"?\s*[:=]\s*"?)[^"\s,}]+', '${1}<redacted>')
    return $t
}

Section "ENVIRONMENT"
W ("collected      : " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
W ("computer       : " + $env:COMPUTERNAME)
$osName = (Get-CimInstance Win32_OperatingSystem).Caption
$osBuild = [System.Environment]::OSVersion.Version.Build
W ("windows        : $osName build $osBuild")
W ("powershell     : " + $PSVersionTable.PSVersion)
W ("script dir     : " + $PSScriptRoot)

Section "INSTALLED PACKAGE"
$pkg = Get-AppxPackage -Name "*VeridianAI*"
if ($pkg) {
    foreach ($p in $pkg) {
        W ("name           : " + $p.Name)
        W ("version        : " + $p.Version)
        W ("architecture   : " + $p.Architecture)
        W ("status         : " + $p.Status)
        W ("install date   : " + $p.InstallDate)
    }
} else {
    W "no VeridianAI AppX package registered (portable install?)"
}

Section "MSVC RUNTIME (system-wide)"
# llama-server.exe hard-imports these. Absent system-wide is FINE as long as
# the app ships them beside the exe; this is here to tell the two cases apart.
foreach ($d in 'msvcp140.dll','vcruntime140.dll','vcruntime140_1.dll') {
    $f = Join-Path $env:SystemRoot "System32\$d"
    if (Test-Path $f) {
        W ("{0,-22} present  v{1}" -f $d, (Get-Item $f).VersionInfo.FileVersion)
    } else {
        W ("{0,-22} ABSENT   (app must supply it app-local)" -f $d)
    }
}

Section "TIER PORTS"
$ports = [ordered]@{
    8000  = 'backend (FastAPI)'
    11434 = 'Oracle  (Ollama)'
    11435 = 'Toga    (llama-server)'
    11436 = 'Daemon  (llama-server)'
    11437 = 'Embed   (llama-server / nomic)'
    11438 = 'NPU     (Lemonade)'
}
foreach ($p in $ports.Keys) {
    $c = Get-NetTCPConnection -LocalPort $p -State Listen
    W ("{0,-6} {1,-30} {2}" -f $p, $ports[$p], $(if ($c) { 'LISTENING' } else { '-' }))
}

Section "PROCESSES"
$procs = Get-Process -Name 'llama-server','python','VeridianAI','ollama'
if ($procs) {
    foreach ($p in $procs | Sort-Object ProcessName, Id) {
        $rss = [math]::Round($p.WorkingSet64 / 1MB)
        W ("{0,-16} pid {1,-8} rss {2,6} MB   started {3}" -f $p.ProcessName, $p.Id, $rss, $p.StartTime)
    }
} else {
    W "none of llama-server / python / VeridianAI / ollama are running"
}

# --- Log files --------------------------------------------------------
function Dump([string]$path, [int]$tail = 200, [string]$grep = '') {
    if (-not (Test-Path $path)) { W "  (not present: $path)"; return }
    $item = Get-Item $path
    W ("  file    : " + $item.FullName)
    W ("  size    : {0:N0} bytes    modified: {1}" -f $item.Length, $item.LastWriteTime)
    W ''
    $content = Get-Content $path -Tail $tail -Encoding UTF8
    if ($grep) { $content = $content | Select-String -Pattern $grep | ForEach-Object { $_.Line } }
    if (-not $content) { W '  (no matching lines)'; return }
    foreach ($l in $content) { W ('  | ' + (Scrub $l)) }
}

# Where the app writes its logs. %TEMP% is the historical location and is
# still written, but on 2026-08-12 two portable runs on the same machine both
# reported the boot log "not present" while the app was plainly running -- so
# looking in exactly one place is how a diagnostic tool comes back empty on the
# day it is needed. v2.14.1 also writes them beside sage_data; check both, and
# prefer whichever is NEWER rather than whichever is found first.
function Find-AppLog([string]$name) {
    $cands = @(
        (Join-Path $env:TEMP $name),
        (Join-Path $env:LOCALAPPDATA "Packages\ElectrumConsiliariusEtc.VeridianAI_f2xd2z2px3t38\LocalCache\Roaming\veridianai\sage_data\$name"),
        (Join-Path $env:APPDATA "veridianai\sage_data\$name")
    )
    # Portable layout: sage_data is a sibling of the extracted tree, so walk up
    # from this script (tools\ -> root -> ..\sage_data).
    try {
        $root = Split-Path -Parent $PSScriptRoot
        $cands += (Join-Path (Split-Path -Parent $root) "sage_data\$name")
        $cands += (Join-Path $root "..\sage_data\$name")
    } catch { }
    if ($env:VERIDIAN_DATA_DIR) { $cands += (Join-Path $env:VERIDIAN_DATA_DIR $name) }

    $hits = @()
    foreach ($c in $cands) {
        try { if (Test-Path $c) { $hits += (Get-Item $c) } } catch { }
    }
    if (-not $hits) { return (Join-Path $env:TEMP $name) }   # report the miss
    return ($hits | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}

Section "BOOT LOG  (Electron)"
$bootLog = Find-AppLog 'VeridianAI-boot.log'
W "  source: $bootLog"
Dump $bootLog 200

Section "BACKEND LOG  (store_launch / tier_launcher / uvicorn)"
$backendLog = Find-AppLog 'VeridianAI-backend.log'
W "  source: $backendLog"
Dump $backendLog 400

Section "PER-TIER LOGS  (windowless tier stdout/stderr)"
# An EMPTY tier log means the Windows loader rejected the exe before its own
# main() ran -- a missing or mismatched DLL. A POPULATED one means the program
# started and then refused its arguments. That distinction is the diagnosis.
# NOTE: an MSIX app writing to %APPDATA% is silently REDIRECTED by the package
# filesystem virtualisation into LocalCache\Roaming under its own package dir.
# So Electron reports "%APPDATA%\veridianai\sage_data" (it only COMPUTES the
# path) while the backend actually WRITES to the redirected one. Both must be
# searched, and the redirected copy is the live data.
$dataDirs = @()
if ($env:VERIDIAN_DATA_DIR) { $dataDirs += $env:VERIDIAN_DATA_DIR }
$dataDirs += (Join-Path $env:APPDATA 'veridianai\sage_data')
$pkgLocal = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Packages\*VeridianAI*') -Directory
foreach ($pl in $pkgLocal) {
    $dataDirs += (Join-Path $pl.FullName 'LocalCache\Roaming\veridianai\sage_data')
    $dataDirs += (Join-Path $pl.FullName 'LocalCache\Local\veridianai\sage_data')
}
$dataDirs += (Join-Path $PSScriptRoot '..\sage_data')

$found = @()
foreach ($d in $dataDirs) {
    $td = Join-Path $d 'logs\tiers'
    if (Test-Path $td) { $found += Get-ChildItem (Join-Path $td '*.log') }
}
$found += Get-ChildItem (Join-Path $env:TEMP 'VeridianAI-tier-*.log') `
            -ErrorAction SilentlyContinue
# Same reasoning as the boot log: also look beside sage_data. And FLAG stale
# ones -- both 2026-08-12 collections dumped tier logs from 08-10 without
# saying so, which reads as evidence from this run when it is not.
foreach ($d in @(
    (Join-Path $env:APPDATA 'veridianai\sage_data'),
    (Join-Path $env:LOCALAPPDATA 'Packages\ElectrumConsiliariusEtc.VeridianAI_f2xd2z2px3t38\LocalCache\Roaming\veridianai\sage_data'),
    (Join-Path (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) 'sage_data')
)) {
    try {
        $found += Get-ChildItem (Join-Path $d 'VeridianAI-tier-*.log') `
                    -ErrorAction SilentlyContinue
    } catch { }
}

if (-not $found) {
    W "  no per-tier logs found."
    W "  Searched:"
    foreach ($d in $dataDirs) { W ("    " + (Join-Path $d 'logs\tiers')) }
    W ("    " + (Join-Path $env:TEMP 'VeridianAI-tier-*.log'))
    W "  (If the build predates the v2.13 tier-logging change, this is expected.)"
} else {
    foreach ($f in ($found | Sort-Object FullName -Unique)) {
        W ''
        $age = (New-TimeSpan -Start $f.LastWriteTime -End (Get-Date))
        $stale = if ($age.TotalMinutes -gt 30) {
            "   *** STALE: {0:N1} hours old -- NOT from this run ***" -f $age.TotalHours
        } else { "" }
        W ("--- " + $f.Name + "  ({0:N0} bytes, {1}){2} ---" -f $f.Length, $f.LastWriteTime, $stale)
        if ($stale) {
            # Both 2026-08-12 collections printed tier logs from 08-10 with no
            # indication of it. Pages of plausible-looking output from a
            # different day is worse than no output: it invites conclusions
            # about a run that never produced it.
            W "  *** This file predates the run being diagnosed. Treat its"
            W "      contents as history, not as evidence of what just happened."
        }
        if ($f.Length -eq 0) {
            W "  *** EMPTY -> the process died before main(); the loader rejected it"
            W "      (missing/mismatched DLL), not the program rejecting its args."
        } else {
            foreach ($l in (Get-Content $f.FullName -Tail 40 -Encoding UTF8)) { W ('  | ' + (Scrub $l)) }
        }
    }
}

# --- Live endpoints ---------------------------------------------------
function Probe([string]$label, [string]$url, $body) {
    W ''
    W ("--- $label  ($url)")
    try {
        if ($null -eq $body) {
            $r = Invoke-WebRequest -Uri $url -TimeoutSec 6 -UseBasicParsing
        } else {
            $r = Invoke-WebRequest -Uri $url -TimeoutSec 20 -UseBasicParsing -Method POST -ContentType 'application/json' -Body $body
        }
        $txt = Scrub $r.Content
        if ($txt.Length -gt 600) { $txt = $txt.Substring(0, 600) + ' ...<truncated>' }
        W ("  HTTP " + [int]$r.StatusCode)
        W ("  " + $txt)
    } catch {
        W ("  FAILED: " + $_.Exception.Message)
    }
}

Section "LIVE ENDPOINTS"
Probe 'backend health' 'http://127.0.0.1:8000/api/health' $null
Probe 'embed tier /v1/embeddings' 'http://127.0.0.1:11437/v1/embeddings' '{"input":["hello"]}'
Probe 'daemon tier /health' 'http://127.0.0.1:11436/health' $null

# --- Write out --------------------------------------------------------
$text = ($lines -join "`r`n")
$targets = @((Join-Path $env:TEMP 'vai_logs.txt'))
if ($PSScriptRoot) { $targets += (Join-Path $PSScriptRoot 'vai_logs.txt') }

$written = @()
foreach ($t in $targets) {
    try { $text | Out-File -FilePath $t -Encoding utf8 -Force; $written += $t } catch { }
}

Write-Host ''
Write-Host 'VeridianAI diagnostics collected.' -ForegroundColor Green
foreach ($t in $written) { Write-Host ("  -> " + $t) }
Write-Host ''
Write-Host 'Attach one of those files. Tokens are already redacted.'
