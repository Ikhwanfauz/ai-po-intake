$demoEnvPath = Join-Path $PSScriptRoot '../.env.milestone3'
if (-not (Test-Path -LiteralPath $demoEnvPath -PathType Leaf)) {
    throw 'Copy .env.milestone3.example to .env.milestone3 and configure the demo settings first.'
}
foreach ($demoLine in Get-Content -LiteralPath $demoEnvPath) {
    $demoSetting = $demoLine.Trim()
    if ($demoSetting -eq '' -or $demoSetting.StartsWith('#')) { continue }
    $demoName, $demoValue = $demoSetting -split '=', 2
    if ($demoName -notmatch '^[A-Z][A-Z0-9_]*$' -or $null -eq $demoValue) {
        throw 'Each setting must be written as NAME=value.'
    }
    [Environment]::SetEnvironmentVariable($demoName, $demoValue, 'Process')
}
