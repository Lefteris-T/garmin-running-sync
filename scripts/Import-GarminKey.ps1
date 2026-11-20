param([string]$Distribution)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\CredentialSupport.ps1"
$credentialDir = Join-Path $env:LOCALAPPDATA 'GarminRunningSync'
$credentialPath = Join-Path $credentialDir 'api_key.dpapi'
$keyText = $null
try {
    if (Test-Path -LiteralPath $credentialPath) { throw 'Credential already exists.' }
    Initialize-CredentialDirectory $credentialDir
    $keyText = Read-WslCredential -Distribution $Distribution
    Save-ProtectedCredential $credentialPath $keyText
    Write-Output 'Credential encrypted; decryption and final file ACL verified.'
    Write-Output ('Storage: ' + $credentialPath)
} catch {
    Write-Error 'Import stopped. Existing credentials are never overwritten. An existing directory must already have the required owner-only ACL; its permissions are never changed. WSL and credential error details are suppressed to protect secrets.'
    exit 1
} finally { $keyText = $null }
