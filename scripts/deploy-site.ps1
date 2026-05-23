# Deploy exsender site to VPS (run from repo root in PowerShell)
# Example:
#   .\scripts\deploy-site.ps1 -ServerHost 178.236.252.6 -User root

param(
    [Parameter(Mandatory = $true)]
    [string]$ServerHost,

    [string]$User = "root",
    [int]$Port = 22,
    [string]$KeyPath = "",
    [string]$Domain = "exsender.top"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Tmp = Join-Path $env:TEMP "exsender-site.tar.gz"

Write-Host "==> Packing site from $Root"

$tarArgs = @(
    "-czf", $Tmp,
    "-C", $Root,
    "web", "frontend", "deploy/site"
)

& tar @tarArgs
if ($LASTEXITCODE -ne 0) { throw "tar failed" }

$sshOpts = @("-o", "StrictHostKeyChecking=accept-new")
if ($KeyPath) { $sshOpts = @("-i", $KeyPath) + $sshOpts }
$sshArgs = @("-p", $Port) + $sshOpts
$scpArgs = @("-P", $Port) + $sshOpts

$remote = "${User}@${ServerHost}"
Write-Host "==> Upload to $remote"

& scp @scpArgs $Tmp "${remote}:/tmp/exsender-site.tar.gz"
& scp @scpArgs (Join-Path $Root "deploy/site/install-on-server.sh") "${remote}:/tmp/install-on-server.sh"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }

Write-Host "==> Install on server (nginx + systemd + certbot)"
& ssh @sshArgs $remote "chmod +x /tmp/install-on-server.sh && bash /tmp/install-on-server.sh $Domain /tmp/exsender-site.tar.gz"

Write-Host ""
Write-Host "Done. Open https://$Domain/login"
Write-Host "If SSL failed: point DNS A record $Domain -> $ServerHost and run on server:"
Write-Host "  certbot --nginx -d $Domain -d www.$Domain"
