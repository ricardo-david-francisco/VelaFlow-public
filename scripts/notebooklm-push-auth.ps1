<#
.SYNOPSIS
    Push NotebookLM auth cookies from Windows to a Proxmox LXC container.

.DESCRIPTION
    Uses your existing notebooklm login session (already authenticated on this
    Windows machine) to push the cookie file directly into the LXC container
    via SSH + pct exec. No VNC or browser needed on the LXC side.

    Run this after 'notebooklm login' on Windows, or when cookies expire and
    you've re-authenticated on your desktop.

.PARAMETER ProxmoxHost
    Hostname or IP of your Proxmox host (e.g. 192.168.1.10).

.PARAMETER CTID
    LXC container ID (default: 200).

.PARAMETER ProxmoxUser
    SSH user on the Proxmox host (default: root).

.PARAMETER TargetPath
    Where to write the cookie file inside the LXC (default: /opt/brain/.notebooklm/storage_state.json).

.PARAMETER CookieSource
    Path to the local storage_state.json (default: %USERPROFILE%\.notebooklm\storage_state.json).

.EXAMPLE
    .\scripts\notebooklm-push-auth.ps1 -ProxmoxHost 192.168.1.10

.EXAMPLE
    .\scripts\notebooklm-push-auth.ps1 -ProxmoxHost proxmox.lan -CTID 201 -ProxmoxUser admin
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProxmoxHost,

    [Parameter()]
    [int]$CTID = 200,

    [Parameter()]
    [string]$ProxmoxUser = "root",

    [Parameter()]
    [string]$TargetPath = "/opt/brain/.notebooklm/storage_state.json",

    [Parameter()]
    [string]$CookieSource = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Resolve cookie source path
if (-not $CookieSource) {
    $CookieSource = Join-Path $env:USERPROFILE ".notebooklm\storage_state.json"
}

# Validate cookie file exists
if (-not (Test-Path $CookieSource)) {
    Write-Error @"
Cookie file not found: $CookieSource

Run 'notebooklm login' first to authenticate, then re-run this script.
"@
    exit 1
}

# Validate it's valid JSON (basic sanity check)
try {
    $null = Get-Content $CookieSource -Raw | ConvertFrom-Json
}
catch {
    Write-Error "Cookie file is not valid JSON: $CookieSource`nTry running 'notebooklm login' again."
    exit 1
}

Write-Host ""
Write-Host "Pushing NotebookLM cookies to LXC $CTID on $ProxmoxHost..." -ForegroundColor Cyan
Write-Host "  Source : $CookieSource"
Write-Host "  Target : [LXC $CTID] $TargetPath"
Write-Host ""

# Read and base64-encode the cookie file
$cookieContent = Get-Content $CookieSource -Raw -Encoding UTF8
$b64 = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($cookieContent))

# Build the remote command:
# - Create directory in LXC
# - Write decoded cookie file
# - Set secure permissions
$targetDir = ($TargetPath -split "/")[0..($TargetPath.Split("/").Length - 2)] -join "/"
$remoteCmd = @"
pct exec $CTID -- bash -c 'mkdir -p $targetDir && chmod 700 $targetDir' && echo '$b64' | base64 -d | pct exec $CTID -- bash -c 'cat > $TargetPath && chmod 600 $TargetPath && chown brain:brain $TargetPath'
"@

try {
    $result = ssh "${ProxmoxUser}@${ProxmoxHost}" $remoteCmd
    if ($LASTEXITCODE -ne 0) {
        Write-Error "SSH command failed (exit $LASTEXITCODE)"
        exit 1
    }
}
catch {
    Write-Error "SSH connection failed: $_`nEnsure SSH key auth is set up for ${ProxmoxUser}@${ProxmoxHost}"
    exit 1
}

Write-Host "Cookie transferred successfully." -ForegroundColor Green
Write-Host ""
Write-Host "Next steps in LXC (or via: pct exec $CTID -- bash -c '...'):" -ForegroundColor Yellow
Write-Host "  1. Run first sync:"
Write-Host "     sudo -u brain NOTEBOOKLM_HOME=/opt/brain/.notebooklm /opt/brain/venv/bin/brain notebooklm-sync --stdout"
Write-Host ""
Write-Host "  2. Enable weekly auto-sync timer:"
Write-Host "     pct exec $CTID -- systemctl enable --now brain-notebooklm.timer"
Write-Host ""
Write-Host "Cookie renewal: re-run 'notebooklm login' on Windows, then re-run this script." -ForegroundColor DarkGray
Write-Host ""
