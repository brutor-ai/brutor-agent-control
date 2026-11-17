# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

# Brutor Agent Control Community Edition - start (Windows PowerShell).
#
# Optional: `docker compose up -d` is all a downloaded package needs. This
# script adds three things around it: it checks that Docker is there, it fills
# in any generated value that is still empty in .env (so a clone of the source
# repository works too), and it waits until the stack is healthy.
# It asks no questions.
$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$EnvFile = Join-Path $PSScriptRoot '.env'

function Fail($message) { Write-Host "ERROR: $message" -ForegroundColor Red; exit 1 }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail 'Docker is not installed. See https://docs.docker.com/get-docker/' }
docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker Compose v2 is required (the 'docker compose' command)." }
docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail 'Docker is installed but not running. Start it and run this again.' }
if (-not (Test-Path $EnvFile)) { Fail '.env is missing. Run this from the package folder.' }

function RandHex([int]$bytes) {
    $buffer = New-Object byte[] $bytes
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($buffer) } finally { $rng.Dispose() }
    return -join ($buffer | ForEach-Object { $_.ToString('x2') })
}

# Fill in KEY=<value> when the line is `KEY=` with nothing after it. A value
# that is already there is never touched.
$script:lines = [System.Collections.Generic.List[string]](Get-Content -Path $EnvFile)
$script:changed = $false
function Mint([string]$key, [string]$value) {
    for ($i = 0; $i -lt $script:lines.Count; $i++) {
        if ($script:lines[$i] -match "^$key=\s*$") {
            $script:lines[$i] = "$key=$value"
            $script:changed = $true
            Write-Host "  generated $key"
            return
        }
    }
}

Write-Host 'Checking .env'
Mint 'DB_PASSWORD'                    (RandHex 24)
Mint 'JWT_SECRET'                     (RandHex 32)
Mint 'ENCRYPTION_KEY'                 (RandHex 32)
Mint 'BRUTOR_GATEWAY_SERVICE_API_KEY' ('sk_brutor_api_' + (RandHex 24))
Mint 'BRUTOR_SKILL_RUNNER_JWT_SECRET' (RandHex 32)
Mint 'BRUTOR_EVIDENCE_SIGNING_KEY'    (RandHex 32)
Mint 'BRUTOR_AUDIT_SIGNING_KEY'       (RandHex 32)
Mint 'BRUTOR_WITNESS_SIGNING_KEY'     (RandHex 32)
Mint 'BRUTOR_DEPLOYMENT_ID'           ('brutor-' + (RandHex 8))
Mint 'BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD'  (RandHex 12)
if ($script:changed) {
    # UTF-8 without a byte-order mark and LF line ends: Docker Compose reads
    # a BOM as part of the first variable's name.
    [System.IO.File]::WriteAllText($EnvFile, (($script:lines -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding $false))
}

function ValueOf([string]$key, [string]$default) {
    foreach ($line in $script:lines) {
        if ($line -match "^$key=(.+)$") { return $Matches[1].Trim() }
    }
    return $default
}

# A value that is there is never replaced, so one that a component would
# refuse has to stop the start here, with its name: the compose file only
# notices an empty line. The value itself is not printed. Same rules as
# the bash script.
$script:badValues = 0
function Require([string]$key, [string]$pattern, [string]$rule) {
    $value = ValueOf $key ''
    if ($value -cnotmatch ('^(?:' + $pattern + ')$')) {
        Write-Host ('X ' + $key + ' in .env is not usable: it must be ' + $rule + '.') -ForegroundColor Red
        $script:badValues++
    }
}
# The core and the control plane refuse these, in any case.
function NoPlaceholder([string]$key) {
    $value = ValueOf $key ''
    if ($value -match 'your-|change-this|changeme|example') {
        Write-Host ('X ' + $key + ' in .env is not usable: it contains your-, change-this, changeme or example,') -ForegroundColor Red
        Write-Host '  which the services take for the value of an example file.' -ForegroundColor Red
        $script:badValues++
    }
}
Require 'DB_PASSWORD'                    '[A-Za-z0-9._~-]{8,}' 'at least 8 letters, digits, ., _, ~ or - (it is written into a database URL)'
Require 'JWT_SECRET'                     '.{32,}' 'at least 32 characters'
Require 'ENCRYPTION_KEY'                 '.{32,}' 'at least 32 characters'
Require 'BRUTOR_GATEWAY_SERVICE_API_KEY' 'sk_brutor_api_[A-Za-z0-9_-]{32,}' 'sk_brutor_api_ followed by at least 32 letters, digits, - or _'
Require 'BRUTOR_SKILL_RUNNER_JWT_SECRET' '.{32,}' 'at least 32 characters'
Require 'BRUTOR_EVIDENCE_SIGNING_KEY'    '[0-9a-fA-F]{64}' 'exactly 64 hex characters'
Require 'BRUTOR_AUDIT_SIGNING_KEY'       '[0-9a-fA-F]{64}' 'exactly 64 hex characters'
Require 'BRUTOR_WITNESS_SIGNING_KEY'     '[0-9a-fA-F]{64}' 'exactly 64 hex characters'
Require 'BRUTOR_DEPLOYMENT_ID'           '.+' 'not empty'
Require 'BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD'  '.{8,}' 'at least 8 characters'
NoPlaceholder 'JWT_SECRET'
NoPlaceholder 'ENCRYPTION_KEY'
if ($script:badValues -gt 0) {
    Write-Host ''
    Write-Host 'Nothing was started. For each line named above:'
    Write-Host '  - before the first start, empty it (NAME= with nothing after the equals'
    Write-Host '    sign) and run this script again to have a value generated, or set a'
    Write-Host '    value that fits the rule;'
    Write-Host '  - on an installation that has already run, put back the value it was'
    Write-Host '    started with: a new secret does not open data stored under the old one.'
    exit 1
}

# What Compose substitutes for ${NAME:-default} in docker-compose.yml: the
# shell's variable when it is set, else the line in .env, else the compose
# file's default.
function SettingOf([string]$key, [string]$default) {
    $fromShell = [Environment]::GetEnvironmentVariable($key)
    if ($fromShell) { return $fromShell }
    $value = (ValueOf $key '').Trim('"').Trim("'")
    if ($value) { return $value }
    return $default
}
$adminConsolePort = SettingOf 'BRUTOR_ADMIN_CONSOLE_PORT' '3002'
$controlPlanePort = SettingOf 'BRUTOR_CONTROL_PLANE_PORT' '5050'
$gatewayPort      = SettingOf 'BRUTOR_GATEWAY_PORT' '8100'
$adminUser   = SettingOf 'BRUTOR_DEFAULT_TENANT_ADMIN_USERNAME' 'admin'

Write-Host 'Starting (the first start pulls the images and sets up the database; allow a few minutes)'
docker compose up -d --wait
if ($LASTEXITCODE -ne 0) { Fail 'The stack did not become healthy. See: docker compose ps, docker compose logs' }

Write-Host ''
Write-Host 'Brutor Agent Control Community Edition is running.'
Write-Host ''
Write-Host "  Admin Console   http://localhost:$adminConsolePort"
Write-Host "  Control plane   http://localhost:$controlPlanePort"
Write-Host "  Core gateway    http://localhost:$gatewayPort"
Write-Host ''
Write-Host "Sign in to the Admin Console as `"$adminUser`". The password is the value of"
Write-Host 'BRUTOR_DEFAULT_TENANT_ADMIN_PASSWORD in the .env file in this folder.'
Write-Host ''
Write-Host 'Stop:    docker compose down'
Write-Host 'Remove:  docker compose down -v   (deletes all data)'
