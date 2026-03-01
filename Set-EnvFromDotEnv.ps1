param(
    [string]$EnvFile = ".env"
)

if (-not (Test-Path -LiteralPath $EnvFile)) {
    throw "Env file not found: $EnvFile"
}

$lines = Get-Content -LiteralPath $EnvFile
$currentKey = $null
$currentValue = New-Object System.Collections.Generic.List[string]
$loaded = 0

function Set-SessionEnvVar {
    param(
        [Parameter(Mandatory = $true)][string]$Key,
        [Parameter(Mandatory = $true)][string]$Value
    )

    [System.Environment]::SetEnvironmentVariable($Key, $Value, "Process")
}

foreach ($line in $lines) {
    if ($null -ne $currentKey) {
        $currentValue.Add($line)

        if ($line -match '^\s*}\s*$') {
            $valueText = ($currentValue -join "`n")
            Set-SessionEnvVar -Key $currentKey -Value $valueText
            $loaded++
            $currentKey = $null
            $currentValue.Clear()
        }

        continue
    }

    if ($line -match '^\s*$' -or $line -match '^\s*#') {
        continue
    }

    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        $key = $matches[1]
        $value = $matches[2]

        if ($value -match '^\s*{\s*$') {
            $currentKey = $key
            $currentValue.Add($value)
            continue
        }

        Set-SessionEnvVar -Key $key -Value $value
        $loaded++
    }
}

if ($null -ne $currentKey) {
    throw "Unterminated multi-line value for key: $currentKey"
}

Write-Host "Loaded $loaded environment variable(s) from $EnvFile into the current session."