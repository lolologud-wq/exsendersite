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

if (-not (Test-Path (Join-Path $Root "bot\requirements.txt"))) {
    throw "bot/requirements.txt not found - run from exsenderV2 repo root"
}

$tarArgs = @(
    "-czf", $Tmp,
    "--exclude=web/.env",
    "--exclude=web/users.json",
    "--exclude=web/bots.json",
    "--exclude=web/invoices.json",
    "--exclude=web/promos.json",
    "--exclude=web/notifications.json",
    "--exclude=web/admin_audit.json",
    "--exclude=web/security_state.json",
    "--exclude=web/.venv",
    "--exclude=web/__pycache__",
    "-C", $Root,
    "web", "frontend", "bot", "deploy/site"
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
if ($LASTEXITCODE -ne 0) { throw "scp archive failed" }

$InstallSh = Join-Path $Root "deploy/site/install-on-server.sh"
$InstallShLf = Join-Path $env:TEMP "install-on-server.sh"
(Get-Content -Raw -Encoding UTF8 $InstallSh) -replace "`r`n", "`n" | Set-Content -NoNewline -Encoding UTF8 $InstallShLf
& scp @scpArgs $InstallShLf "${remote}:/tmp/install-on-server.sh"
if ($LASTEXITCODE -ne 0) { throw "scp install script failed" }

Write-Host "==> Install on server (nginx + systemd + certbot)"
$installCmd = "sed -i 's/\r$//' /tmp/install-on-server.sh; chmod +x /tmp/install-on-server.sh; bash /tmp/install-on-server.sh $Domain /tmp/exsender-site.tar.gz"
& ssh @sshArgs $remote $installCmd
if ($LASTEXITCODE -ne 0) { throw "remote install failed (exit $LASTEXITCODE)" }

Write-Host ""
Write-Host "Done. Open https://$Domain/login"
Write-Host "If SSL failed: point DNS A record $Domain -> $ServerHost and run on server:"
Write-Host "  certbot --nginx -d $Domain -d www.$Domain"
