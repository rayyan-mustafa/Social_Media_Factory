# Redact StreamCast / Eagle streaming-app session stream keys on this Windows PC.
# - Backs up sessions.json locally (never uploads)
# - Clears stream_key / streamKey / rtmp_key style fields in-place
# - Does NOT print key values
#
# Usage:
#   .\scripts\redact_streamcast_sessions.ps1
#   .\scripts\redact_streamcast_sessions.ps1 -WhatIf
#   .\scripts\redact_streamcast_sessions.ps1 -SessionsPath "D:\path\sessions.json"

[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$SessionsPath = "",
    [switch]$DeleteSessionsFile
)

$ErrorActionPreference = "Stop"

function Get-DefaultSessionsPath {
    $candidates = @(
        (Join-Path $env:APPDATA "com.eagle.streaming-app\sessions.json"),
        (Join-Path $env:APPDATA "com.eagle.streamcast\sessions.json"),
        (Join-Path $env:LOCALAPPDATA "com.eagle.streaming-app\sessions.json")
    )
    foreach ($p in $candidates) {
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $candidates[0]
}

function Clear-SecretFields {
    param([Parameter(Mandatory = $true)]$Node)

    $cleared = 0
    $namePat = '(?i)(stream[_-]?key|rtmp[_-]?key|streamkey|ingest[_-]?key|live[_-]?key|stream[_-]?url|rtmp[_-]?url|server[_-]?url)'

    if ($Node -is [System.Collections.IDictionary] -or $Node -is [hashtable]) {
        $keys = @($Node.Keys)
        foreach ($k in $keys) {
            $v = $Node[$k]
            if ($k -match $namePat) {
                if ($null -ne $v -and "$v".Length -gt 0) {
                    $Node[$k] = ""
                    $cleared++
                }
            }
            elseif ($null -ne $v) {
                $cleared += Clear-SecretFields -Node $v
            }
        }
    }
    elseif ($Node -is [System.Collections.IEnumerable] -and -not ($Node -is [string])) {
        foreach ($item in $Node) {
            if ($null -ne $item) {
                $cleared += Clear-SecretFields -Node $item
            }
        }
    }
    elseif ($Node -is [pscustomobject]) {
        foreach ($prop in $Node.PSObject.Properties) {
            if ($prop.Name -match $namePat) {
                if ($null -ne $prop.Value -and "$($prop.Value)".Length -gt 0) {
                    $prop.Value = ""
                    $cleared++
                }
            }
            elseif ($null -ne $prop.Value) {
                $cleared += Clear-SecretFields -Node $prop.Value
            }
        }
    }
    return $cleared
}

if ([string]::IsNullOrWhiteSpace($SessionsPath)) {
    $SessionsPath = Get-DefaultSessionsPath
}

Write-Host "Target: $SessionsPath"
Write-Host "(Key values are never printed.)"

if (-not (Test-Path -LiteralPath $SessionsPath)) {
    Write-Host "No sessions file found at that path. Nothing to redact."
    Write-Host "If StreamCast used a different folder, pass -SessionsPath explicitly."
    exit 0
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = "$SessionsPath.bak-$stamp"

if ($PSCmdlet.ShouldProcess($SessionsPath, "Backup then redact stream keys")) {
    Copy-Item -LiteralPath $SessionsPath -Destination $backup -Force
    Write-Host "Backup: $backup"

    if ($DeleteSessionsFile) {
        Remove-Item -LiteralPath $SessionsPath -Force
        Write-Host "Deleted sessions file (backup kept). Rotate YouTube keys next."
        exit 0
    }

    $raw = Get-Content -LiteralPath $SessionsPath -Raw -Encoding UTF8
    try {
        $json = $raw | ConvertFrom-Json
    }
    catch {
        Write-Error "sessions.json is not valid JSON. Backup left at $backup. Aborting without overwrite."
        exit 1
    }

    $n = Clear-SecretFields -Node $json
    $out = $json | ConvertTo-Json -Depth 100
    # Avoid dumping secrets: write via .NET without Write-Host of content
    [System.IO.File]::WriteAllText($SessionsPath, $out + [Environment]::NewLine)

    Write-Host "Redacted field count: $n"
    Write-Host "Done. Next: rotate YouTube Live stream keys in Studio, then put NEW keys only on VPS .env."
    Write-Host "See: python -m src.cli.vod_loop key-hygiene"
}
