# Upload bot/ to exsender site server (needed for VDS deploy from UI)
# Example:
#   .\scripts\sync-site-bot.ps1 -ServerHost 178.236.252.6 -User root

param(
    [Parameter(Mandatory = $true)]
    [string]$ServerHost,

    [string]$User = "root",
    [int]$Port = 22,
    [string]$KeyPath = "",
    [string]$RemoteRoot = "/opt/exsender"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$BotDir = Join-Path $Root "bot"
if (-not (Test-Path (Join-Path $BotDir "requirements.txt"))) {
    Write-Host "Local bot/ not found at $BotDir"
    Write-Host "Run from repo root: C:\Users\USER\Documents\GitHub\exsenderV2"
    throw "bot/requirements.txt missing"
}

$sshOpts = @("-o", "StrictHostKeyChecking=accept-new")
if ($KeyPath) { $sshOpts = @("-i", $KeyPath) + $sshOpts }
$scpArgs = @("-P", $Port) + $sshOpts
$sshArgs = @("-p", $Port) + $sshOpts
$remote = "${User}@${ServerHost}"

Write-Host "==> Upload bot/ + web/deployer.py to ${remote}:${RemoteRoot}"
& ssh @sshArgs $remote "mkdir -p ${RemoteRoot}/bot ${RemoteRoot}/web"
& scp @scpArgs -r "$BotDir\*" "${remote}:${RemoteRoot}/bot/"
& scp @scpArgs (Join-Path $Root "web\deployer.py") "${remote}:${RemoteRoot}/web/deployer.py"
& ssh @sshArgs $remote "systemctl restart exsender"
Write-Host "Done. Redeploy VDS from https://exsender.top"
