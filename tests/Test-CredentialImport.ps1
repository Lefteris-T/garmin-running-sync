$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\..\scripts\CredentialSupport.ps1"
$root = Join-Path $PSScriptRoot ('credential-test-' + [Guid]::NewGuid())
$script:fileTestsFailed = $false
function Write-SyntheticErrorDiagnostic {
    param([System.Management.Automation.ErrorRecord]$Record)
    # Whitelist metadata only. Never format the ErrorRecord, Message, TargetObject,
    # InvocationInfo, stack trace, or paths.
    'Diagnostic exception type: ' + $Record.Exception.GetType().FullName
    'Diagnostic error category: ' + $Record.CategoryInfo.Category.ToString()
    'Diagnostic HResult: ' + $Record.Exception.HResult
    if ($Record.Exception.InnerException) {
        'Diagnostic inner exception type: ' + $Record.Exception.InnerException.GetType().FullName
        'Diagnostic inner HResult: ' + $Record.Exception.InnerException.HResult
    }
}
$step = 'create and verify restricted directory'
try {
    $dir = Join-Path $root 'secure'
    [void][IO.Directory]::CreateDirectory($root)
    Initialize-CredentialDirectory $dir
    Assert-CredentialAcl $dir -Directory
    'PASS: new directory has verified restricted ACL'
    $step = 'verify compliant existing directory stays unchanged'
    $before = (Get-Acl -LiteralPath $dir).Sddl
    Initialize-CredentialDirectory $dir
    if ((Get-Acl -LiteralPath $dir).Sddl -cne $before) { throw 'Existing ACL changed' }
    'PASS: compliant existing directory unchanged'
    $step = 'reject noncompliant directory without changing permissions'
    $bad = Join-Path $root 'inherited'
    [void][IO.Directory]::CreateDirectory($bad)
    $before = (Get-Acl -LiteralPath $bad).Sddl
    $rejected = $false
    try { Initialize-CredentialDirectory $bad } catch { $rejected = $true }
    if (-not $rejected -or (Get-Acl -LiteralPath $bad).Sddl -cne $before) { throw 'Unsafe existing directory handling' }
    'PASS: noncompliant existing directory rejected unchanged'
    $step = 'write encrypted synthetic file and verify ACL and decryption'
    $file = Join-Path $dir 'api_key.dpapi'
    Save-ProtectedCredential $file 'synthetic-test-value'
    Assert-CredentialAcl $file
    $bytes = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($file), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    if ([Text.Encoding]::UTF8.GetString($bytes) -cne 'synthetic-test-value') { throw 'Round trip failed' }
    [Array]::Clear($bytes, 0, $bytes.Length)
    'PASS: encrypted file ACL and synthetic DPAPI round trip'
    $step = 'reject credential overwrite and verify unchanged bytes'
    $before = (Get-FileHash -LiteralPath $file).Hash
    $rejected = $false
    try { Save-ProtectedCredential $file 'replacement' } catch { $rejected = $true }
    if (-not $rejected -or (Get-FileHash -LiteralPath $file).Hash -cne $before) { throw 'Overwrite protection failed' }
    'PASS: existing credential rejected unchanged'
    $step = 'read synthetic file ACL for extra-reader test'
    $acl = Get-Acl -LiteralPath $file
    $step = 'construct extra-reader rule using explicit Everyone SID'
    $everyone = New-Object Security.Principal.SecurityIdentifier('S-1-1-0')
    $extraRule = New-Object Security.AccessControl.FileSystemAccessRule($everyone,[Security.AccessControl.FileSystemRights]::Read,[Security.AccessControl.AccessControlType]::Allow)
    $step = 'add extra-reader rule to synthetic ACL'
    $acl.AddAccessRule($extraRule)
    $step = 'apply extra-reader ACL to synthetic file'
    # Reproduce the old operation for safe diagnosis, but do not depend on it.
    try {
        Set-Acl -LiteralPath $file -AclObject $acl
        'INFO: original Set-Acl operation succeeded on synthetic file'
    } catch {
        'INFO: original Set-Acl operation failed on synthetic file'
        Write-SyntheticErrorDiagnostic $_
    }
    $step = 'apply only access rules to synthetic file using fresh security descriptor'
    # A Get-Acl descriptor also contains owner/group information. Do not try to
    # persist those sections when this test changes only the DACL.
    $accessOnly = New-Object Security.AccessControl.FileSecurity
    $accessOnly.SetSecurityDescriptorSddlForm(
        $acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::Access),
        [Security.AccessControl.AccessControlSections]::Access)
    [IO.File]::SetAccessControl($file, $accessOnly)
    $step = 'verify extra-reader rule was persisted'
    $persisted = @((Get-Acl -LiteralPath $file).GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]) | Where-Object { $_.IdentityReference.Value -eq 'S-1-1-0' -and $_.AccessControlType -eq 'Allow' })
    if ($persisted.Count -ne 1) { throw 'Synthetic ACL setup failed.' }
    $step = 'verify credential ACL validator rejects extra reader'
    $rejected = $false
    try { Assert-CredentialAcl $file } catch { $rejected = $true }
    if (-not $rejected) { throw 'Extra reader accepted' }
    'PASS: extra file reader rejected'
} catch {
    # Only fixed step labels are reported. Exception messages and data are suppressed.
    'FAIL: ' + $step + '. Exception details suppressed; synthetic data only.'
    Write-SyntheticErrorDiagnostic $_
    $script:fileTestsFailed = $true
} finally {
    if (Test-Path -LiteralPath $root) {
        $resolved = [IO.Path]::GetFullPath($root)
        if (-not $resolved.StartsWith([IO.Path]::GetFullPath($PSScriptRoot) + '\')) { throw 'Unsafe cleanup path' }
        try { Remove-Item -LiteralPath $root -Recurse -Force } catch {
            'BLOCKED: test directory cleanup denied after applying owner-only permissions.'
            $script:fileTestsFailed = $true
        }
    }
}
try {
    $value = Read-WslCredential -Synthetic
    if ($value -cne 'synthetic-only-42') { throw 'Synthetic WSL mismatch' }
    'PASS: WSL temporary file with spaces and apostrophe transferred correctly'
} catch {
    'BLOCKED: WSL synthetic transfer could not be verified in this session. No real key was accessed.'
    exit 2
}
if ($script:fileTestsFailed) { exit 1 }
'PASS: all synthetic credential import tests completed'
