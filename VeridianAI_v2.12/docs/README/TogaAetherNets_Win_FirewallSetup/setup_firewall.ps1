<#
  setup_firewall.ps1  --  VeridianAI -- Toga/Aether inbound firewall rule for the API port

  Configures the WINDOWS DEFENDER FIREWALL side only. This is INDEPENDENT of the
  port-forward on your ISP modem/router: for internet peers to reach you, BOTH
  must allow the traffic --
      Internet  --(router port-forward  WAN:PORT -> this PC's LAN IP:PORT)-->  PC
                --(this Windows rule: allow inbound TCP PORT)-->  OracleAI
  This script only does the second arrow. Set the router forward in its own admin
  page (forward TCP PORT to this PC's LAN IPv4, which you can see with `ipconfig`).

  WHY YOUR CURRENT RULE IS PROBABLY WRONG (the usual three causes):
    1. Profile mismatch -- the rule is bound to "Private" but Windows has the
       active network classified as "Public" (or vice-versa), so it never applies.
       This script uses -Profile Any to sidestep that entirely.
    2. A leftover/duplicate rule, or a BLOCK rule that wins (block beats allow).
       This script lists what exists and removes its own old rule before adding.
    3. Wrong direction/protocol (an Outbound or UDP rule). This creates the
       correct Inbound + TCP rule.

  RUN ELEVATED: right-click PowerShell -> "Run as administrator", then:

      # See what already exists for the port (no changes are made):
      .\setup_firewall.ps1 -Mode Show

      # Public Aether (recommended): allow the port; gating is done app-side by
      # the 404-cloak + denylist + lockdown allowlist + auto-ban + peer signing.
      .\setup_firewall.ps1 -Mode Open

      # Defense-in-depth: only these remote addresses may reach the port at all.
      # New peers must be added here too, so prefer -Mode Open + the in-app
      # lockdown allowlist unless you want OS-level scoping.
      .\setup_firewall.ps1 -Mode Scoped -TrustedRemotes @("198.51.100.7","203.0.113.0/24","192.168.0.0/16")

      # Remove the OracleAI rule entirely:
      .\setup_firewall.ps1 -Mode Remove

  NOTE: -TrustedRemotes values here are EXAMPLES (RFC 5737 documentation ranges).
  Replace them with your real peers' public IPs / CIDRs and your LAN range.
#>
[CmdletBinding()]
param(
    [int]      $Port           = 8000,
    [ValidateSet("Show", "Open", "Scoped", "Remove")]
    [string]   $Mode           = "Show",
    [string[]] $TrustedRemotes = @(),
    [string]   $RuleName       = "VeridianAI Aether (inbound)"
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $id  = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pri = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $pri.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Error "This action needs an ELEVATED PowerShell (Run as administrator)."
        exit 1
    }
}

function Remove-OldRule {
    $old = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    if ($old) {
        $old | Remove-NetFirewallRule
        Write-Host "Removed existing rule named '$RuleName'." -ForegroundColor DarkYellow
    }
}

# --- Always show what currently targets this port (read-only, no admin needed) -
Write-Host "== Existing INBOUND rules touching TCP $Port ==" -ForegroundColor Cyan
$hits = Get-NetFirewallPortFilter -Protocol TCP -ErrorAction SilentlyContinue |
    Where-Object { "$($_.LocalPort)" -eq "$Port" } |
    ForEach-Object { $_ | Get-NetFirewallRule -ErrorAction SilentlyContinue } |
    Where-Object { $_.Direction -eq "Inbound" }
if ($hits) {
    $hits | Sort-Object DisplayName -Unique |
        Format-Table DisplayName, Enabled, Action, Profile -AutoSize
} else {
    Write-Host "  (no exact single-port inbound rules found for $Port)"
}

switch ($Mode) {
    "Show" {
        Write-Host "`nShow-only. Re-run with -Mode Open (or Scoped) to apply." -ForegroundColor Yellow
    }
    "Remove" {
        Assert-Admin
        Remove-OldRule
        Write-Host "Done. Port $Port is no longer allowed by the '$RuleName' rule." -ForegroundColor Green
    }
    "Open" {
        Assert-Admin
        Remove-OldRule
        New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $Port -Profile Any | Out-Null
        Write-Host "OK: OPEN inbound allow for TCP $Port on all profiles." -ForegroundColor Green
        Write-Host "Security is enforced app-side (404-cloak + denylist + lockdown + auto-ban + peer signing)."
    }
    "Scoped" {
        Assert-Admin
        if (-not $TrustedRemotes -or $TrustedRemotes.Count -eq 0) {
            Write-Error "Scoped mode requires -TrustedRemotes, e.g. @('198.51.100.7','192.168.0.0/16')."
            exit 1
        }
        Remove-OldRule
        New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $Port -Profile Any -RemoteAddress $TrustedRemotes | Out-Null
        Write-Host "OK: SCOPED inbound allow for TCP $Port from:" -ForegroundColor Green
        $TrustedRemotes | ForEach-Object { Write-Host "    $_" }
        Write-Host "Reminder: add new peers here too, or use -Mode Open + the in-app lockdown allowlist."
    }
}
