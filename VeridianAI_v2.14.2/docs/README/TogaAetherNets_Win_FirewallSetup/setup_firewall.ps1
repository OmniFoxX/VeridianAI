<#
  setup_firewall.ps1  --  VeridianAI unified Windows Firewall setup

  ONE script for BOTH of VeridianAI's networks:

    * Aether  (internet relay) -- inbound  TCP  8000   (the API port)
    * Argo-Net (LAN mesh)      -- inbound  UDP  47490  (multicast discovery,
                                  public/DM messages, and signed revocations)

  Pick which with -Include (Aether | ArgoNet | Both). Default is Both.

  ----------------------------------------------------------------------------
  WHEN DO YOU NEED THIS?
    * Argo-Net: if two machines on the SAME LAN don't see each other as peers,
      or public messages don't cross, Windows Firewall is almost certainly
      dropping the inbound UDP multicast. Run this (Both or ArgoNet) on EACH
      machine. (DMs and discovery ride the same UDP, so this fixes those too.)
    * Aether: only if you expose the API port to internet peers. That ALSO needs
      a port-forward on your router (this script only does the Windows side).

  Argo-Net is LAN-only, so its rule is scoped to PRIVATE/DOMAIN profiles and, in
  Scoped mode, to your local subnets -- it never opens the mesh port to the
  internet.

  ----------------------------------------------------------------------------
  RUN ELEVATED: right-click PowerShell -> "Run as administrator", then:

      # See what already exists (no changes):
      .\setup_firewall.ps1 -Mode Show

      # Recommended for the LAN mesh -- allow discovery + messages on the LAN:
      .\setup_firewall.ps1 -Mode Open -Include ArgoNet

      # Both networks at once:
      .\setup_firewall.ps1 -Mode Open -Include Both

      # Defense-in-depth: only these remote addresses may reach the ports.
      .\setup_firewall.ps1 -Mode Scoped -Include Both `
          -TrustedRemotes @("192.168.0.0/16","198.51.100.7")

      # Remove VeridianAI's rules:
      .\setup_firewall.ps1 -Mode Remove -Include Both

  NOTE: -TrustedRemotes values are EXAMPLES (RFC 5737 ranges + a private CIDR).
  Replace with your real LAN range / peers.
#>
[CmdletBinding()]
param(
    [ValidateSet("Show", "Open", "Scoped", "Remove")]
    [string]   $Mode           = "Show",
    [ValidateSet("Aether", "ArgoNet", "Both")]
    [string]   $Include        = "Both",
    [int]      $AetherPort     = 8000,
    [int]      $ArgonetPort    = 47490,
    [string[]] $TrustedRemotes = @()
)

$ErrorActionPreference = "Stop"

$AetherRule  = "VeridianAI Aether (inbound)"
$ArgonetRule = "VeridianAI Argo-Net mesh (inbound)"

function Assert-Admin {
    $id  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pri = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $pri.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "This action needs an ELEVATED PowerShell (Run as administrator)."
        exit 1
    }
}

function Remove-Rule([string]$Name) {
    $old = Get-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue
    if ($old) {
        $old | Remove-NetFirewallRule
        Write-Host "Removed existing rule '$Name'." -ForegroundColor DarkYellow
    }
}

function Show-Port([string]$Protocol, [int]$Port) {
    Write-Host "== Existing INBOUND rules touching $Protocol $Port ==" -ForegroundColor Cyan
    $hits = Get-NetFirewallPortFilter -Protocol $Protocol -ErrorAction SilentlyContinue |
        Where-Object { "$($_.LocalPort)" -eq "$Port" } |
        ForEach-Object { $_ | Get-NetFirewallRule -ErrorAction SilentlyContinue } |
        Where-Object { $_.Direction -eq "Inbound" }
    if ($hits) {
        $hits | Sort-Object DisplayName -Unique |
            Format-Table DisplayName, Enabled, Action, Profile -AutoSize
    } else {
        Write-Host "  (none found)"
    }
}

$doAether  = ($Include -eq "Aether")  -or ($Include -eq "Both")
$doArgonet = ($Include -eq "ArgoNet") -or ($Include -eq "Both")

# --- Always show what currently exists (read-only, no admin needed) ----------
if ($doAether)  { Show-Port "TCP" $AetherPort }
if ($doArgonet) { Show-Port "UDP" $ArgonetPort }

switch ($Mode) {
    "Show" {
        Write-Host "`nShow-only. Re-run with -Mode Open (or Scoped) to apply." -ForegroundColor Yellow
    }

    "Remove" {
        Assert-Admin
        if ($doAether)  { Remove-Rule $AetherRule }
        if ($doArgonet) { Remove-Rule $ArgonetRule }
        Write-Host "Done. Selected VeridianAI rules removed." -ForegroundColor Green
    }

    "Open" {
        Assert-Admin
        if ($doAether) {
            Remove-Rule $AetherRule
            New-NetFirewallRule -DisplayName $AetherRule -Direction Inbound -Action Allow `
                -Protocol TCP -LocalPort $AetherPort -Profile Any | Out-Null
            Write-Host "OK: Aether  -> inbound TCP $AetherPort (all profiles)." -ForegroundColor Green
        }
        if ($doArgonet) {
            Remove-Rule $ArgonetRule
            # LAN mesh: Private/Domain only (never expose the mesh to the internet).
            New-NetFirewallRule -DisplayName $ArgonetRule -Direction Inbound -Action Allow `
                -Protocol UDP -LocalPort $ArgonetPort -Profile Private,Domain | Out-Null
            Write-Host "OK: Argo-Net -> inbound UDP $ArgonetPort (Private/Domain)." -ForegroundColor Green
            Write-Host "    (Discovery, public + DM messages, and revocations all use this port.)"
        }
    }

    "Scoped" {
        Assert-Admin
        if (-not $TrustedRemotes -or $TrustedRemotes.Count -eq 0) {
            Write-Error "Scoped mode requires -TrustedRemotes, e.g. @('192.168.0.0/16')."
            exit 1
        }
        if ($doAether) {
            Remove-Rule $AetherRule
            New-NetFirewallRule -DisplayName $AetherRule -Direction Inbound -Action Allow `
                -Protocol TCP -LocalPort $AetherPort -Profile Any -RemoteAddress $TrustedRemotes | Out-Null
            Write-Host "OK: Aether  -> SCOPED inbound TCP $AetherPort from your list." -ForegroundColor Green
        }
        if ($doArgonet) {
            Remove-Rule $ArgonetRule
            New-NetFirewallRule -DisplayName $ArgonetRule -Direction Inbound -Action Allow `
                -Protocol UDP -LocalPort $ArgonetPort -Profile Private,Domain -RemoteAddress $TrustedRemotes | Out-Null
            Write-Host "OK: Argo-Net -> SCOPED inbound UDP $ArgonetPort from your list." -ForegroundColor Green
        }
        $TrustedRemotes | ForEach-Object { Write-Host "    $_" }
    }
}

Write-Host "`nReminder: run this on EVERY machine that should join the mesh." -ForegroundColor Cyan
