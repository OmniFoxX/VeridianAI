# =====================================================================
#  bundle_playwright.ps1 -- put Playwright's Chromium INSIDE the tree
#
#  WHY
#  browser_tool.py imports playwright.async_api at module level, so without
#  the package the [BROWSE:] tool cannot load -- that is what "plugin needed"
#  meant on the test laptops. And the package alone is not enough: Playwright
#  downloads its browser separately, at runtime, into a per-user cache.
#
#  A runtime download is exactly what we cannot have:
#    * the Store build may not fetch executables at runtime
#    * a first-run user should not wait on a 150 MB download to browse
#    * an offline machine would simply fail, with a message about a browser
#      it has never heard of
#
#  So the browser ships in the package. ~155 MB against ~300 MB of headroom.
#
#  WHY NOT JUST USE EDGE
#  Playwright can drive an installed Chrome/Edge via channel=, and
#  browser_tool already falls back that way (Brave -> Chrome -> msedge ->
#  bundled). That works on most machines and is not something to DEPEND on:
#  people do remove Edge, and "works unless you uninstalled a Microsoft app"
#  is not a support answer. The fallback chain stays; this makes the last
#  link real.
#
#  RUN
#      powershell -ExecutionPolicy Bypass -File tools\bundle_playwright.ps1
#
#  Works in either tree. Uses the bundled python\python.exe when present
#  (Store tree), otherwise the pinned/system interpreter (portable tree).
# =====================================================================

$ErrorActionPreference = 'Stop'

function Say($m) { Write-Host "  $m" -ForegroundColor Cyan }
function Ok ($m) { Write-Host "  $m" -ForegroundColor Green }
function Die($m) { Write-Host "  ERROR: $m" -ForegroundColor Red; exit 1 }

$Root        = Split-Path -Parent $PSScriptRoot
$BrowsersDir = Join-Path $Root 'playwright-browsers'
$Req         = Join-Path $Root 'backend\requirements.txt'

Write-Host ""
Write-Host "  Bundling Playwright + Chromium" -ForegroundColor White
Write-Host "  root: $Root"
Write-Host ""

# --- pick the interpreter this tree will actually run on ------------------
# Must match the runtime interpreter: Playwright's Python package is installed
# per-interpreter, and installing into the wrong one produces a bundle that
# looks complete and imports nothing.
$py = Join-Path $Root 'python\python.exe'
if (Test-Path $py) {
    Say "Using bundled interpreter: $py"
} elseif ($env:VERIDIAN_PYTHON -and (Test-Path $env:VERIDIAN_PYTHON)) {
    $py = $env:VERIDIAN_PYTHON
    Say "Using pinned interpreter: $py"
} else {
    $py = 'py'
    Say "Using launcher: py (no bundled interpreter, no VERIDIAN_PYTHON pin)"
}

# --- 1. the Python package ------------------------------------------------
Say "Installing the playwright package..."
& $py -m pip install --no-warn-script-location --no-cache-dir "playwright>=1.47,<1.60" "playwright-stealth>=1.1.0"
if ($LASTEXITCODE -ne 0) { Die "pip install playwright failed" }

# Both are imported at module level by browser_tool.py. Installing only the
# first produced a bundle that looked complete -- playwright present, Chromium
# present, launch test passing -- and still failed every browse on
# "No module named 'playwright_stealth'". Verify what we import, not what we
# meant to install.
& $py -c "import playwright, playwright_stealth"
if ($LASTEXITCODE -ne 0) { Die "playwright / playwright_stealth not importable after install" }
Ok "package installed"

# --- 2. the browser, INTO the tree ---------------------------------------
# PLAYWRIGHT_BROWSERS_PATH redirects the download away from the per-user cache
# (%USERPROFILE%\AppData\Local\ms-playwright) and into the package, so the
# browser travels with the app instead of being something each machine has to
# acquire for itself.
Say "Downloading Chromium into $BrowsersDir (~150 MB, one time)..."
$env:PLAYWRIGHT_BROWSERS_PATH = $BrowsersDir
New-Item -ItemType Directory -Force -Path $BrowsersDir | Out-Null

& $py -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Die "playwright install chromium failed" }

# --- 3. prove it actually landed -----------------------------------------
# "The command exited 0" is not evidence. Check for a real executable.
$exe = Get-ChildItem -Path $BrowsersDir -Recurse -Filter 'chrome.exe' -ErrorAction SilentlyContinue |
       Select-Object -First 1
if (-not $exe) {
    $exe = Get-ChildItem -Path $BrowsersDir -Recurse -Filter 'headless_shell.exe' -ErrorAction SilentlyContinue |
           Select-Object -First 1
}
if (-not $exe) { Die "no chromium executable found under $BrowsersDir after install" }

$sizeMB = [math]::Round((Get-ChildItem $BrowsersDir -Recurse -File |
                         Measure-Object -Property Length -Sum).Sum / 1MB)
Ok "chromium present: $($exe.FullName)"
Ok "bundle size: $sizeMB MB"

# --- 4. launch it once, for real -----------------------------------------
# The only check that matters. An installed-but-unlaunchable browser is the
# failure this script exists to prevent, and it is invisible until someone
# tries to browse.
Say "Launching bundled Chromium once to verify..."
$test = @"
import asyncio, os, sys
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = r'$BrowsersDir'
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page()
        await page.set_content('<h1>ok</h1>')
        t = await page.inner_text('h1')
        await b.close()
        print('LAUNCH_OK' if t == 'ok' else 'LAUNCH_BAD')
asyncio.run(main())
"@
$tmp = Join-Path $env:TEMP 'vai_pw_check.py'
$test | Out-File -FilePath $tmp -Encoding utf8
$result = & $py $tmp 2>&1
Remove-Item $tmp -ErrorAction SilentlyContinue
if ($result -match 'LAUNCH_OK') {
    Ok "bundled Chromium launched and rendered"
} else {
    Write-Host "  WARNING: verification launch did not report OK:" -ForegroundColor Yellow
    Write-Host "  $result"
    Write-Host "  The files are present but the browser may not run on this machine." -ForegroundColor Yellow
}

Write-Host ""
Ok "Done."
Write-Host "  Next: the runtime sets PLAYWRIGHT_BROWSERS_PATH to this folder"
Write-Host "        automatically (store_launch.py / start.bat), but ONLY when it"
Write-Host "        exists -- so a tree without it still uses a system browser."
Write-Host ""
