Add-Type -AssemblyName System.Security
function Assert-CredentialAcl {
 param([string]$Path,[switch]$Directory)
 $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
 if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse point rejected.' }
 $acl = Get-Acl -LiteralPath $Path
 $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
 if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value -or -not $acl.AreAccessRulesProtected) { throw 'Owner or protection mismatch.' }
 $rules = @($acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier]))
 if ($rules.Count -ne 1) { throw 'Unexpected ACL entries.' }
 $r = $rules[0]
 $flags = if ($Directory) { [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit' } else { [Security.AccessControl.InheritanceFlags]::None }
 if ($r.IdentityReference.Value -ne $sid.Value -or $r.AccessControlType -ne 'Allow' -or $r.FileSystemRights -ne [Security.AccessControl.FileSystemRights]::FullControl -or $r.IsInherited -or $r.PropagationFlags -ne 'None' -or $r.InheritanceFlags -ne $flags) { throw 'Access rule mismatch.' }
}
function New-CredentialAcl {
 param([switch]$Directory)
 $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
 $acl = if ($Directory) { New-Object Security.AccessControl.DirectorySecurity } else { New-Object Security.AccessControl.FileSecurity }
 $acl.SetOwner($sid)
 $acl.SetAccessRuleProtection($true,$false)
 $flags = if ($Directory) { 'ContainerInherit,ObjectInherit' } else { 'None' }
 $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl',$flags,'None','Allow')))
 return $acl
}
function Initialize-CredentialDirectory {
 param([string]$Path)
 if (Test-Path -LiteralPath $Path) {
  if (-not (Get-Item -LiteralPath $Path).PSIsContainer) { throw 'Expected directory.' }
  Assert-CredentialAcl $Path -Directory
  return
 }
 $info = New-Object IO.DirectoryInfo($Path)
 $info.Create((New-CredentialAcl -Directory))
 Assert-CredentialAcl $Path -Directory
}
function Save-ProtectedCredential {
 param([string]$Path,[string]$Value)
 if (Test-Path -LiteralPath $Path) { throw 'Credential exists.' }
 $dir = Split-Path -Parent $Path
 Assert-CredentialAcl $dir -Directory
 $plain = $null; $verified = $null; $temp = $null; $stream = $null
 try {
  $plain = [Text.Encoding]::UTF8.GetBytes($Value)
  $encrypted = [Security.Cryptography.ProtectedData]::Protect($plain,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
  $temp = Join-Path $dir ([Guid]::NewGuid().ToString() + '.tmp')
  $stream = New-Object IO.FileStream($temp,[IO.FileMode]::CreateNew,[Security.AccessControl.FileSystemRights]::FullControl,[IO.FileShare]::None,4096,[IO.FileOptions]::None,(New-CredentialAcl))
  $stream.Write($encrypted,0,$encrypted.Length)
  $stream.Flush($true)
  $stream.Dispose(); $stream = $null
  Assert-CredentialAcl $temp
  $verified = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($temp),$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
  if ([Convert]::ToBase64String($plain) -cne [Convert]::ToBase64String($verified)) { throw 'Verification failed.' }
  [IO.File]::Move($temp,$Path)
  try { Assert-CredentialAcl $Path } catch { Remove-Item -LiteralPath $Path -Force; throw 'Final ACL failed; new credential removed.' }
 } finally {
  if ($stream) { $stream.Dispose() }
  if ($plain) { [Array]::Clear($plain,0,$plain.Length) }
  if ($verified) { [Array]::Clear($verified,0,$verified.Length) }
  if ($temp -and (Test-Path -LiteralPath $temp)) { Remove-Item -LiteralPath $temp -Force }
 }
}
function Read-WslCredential {
 param([string]$Distribution,[switch]$Synthetic)
 if ($Distribution -and $Distribution -notmatch '^[a-zA-Z0-9_.-]+$') { throw 'Invalid distribution.' }
 # Shell text is sent over stdin, avoiding nested Windows/Linux argument quoting.
 $program = if ($Synthetic) {
@'
set -eu
read_key() { cat -- "$1"; }
d=$(mktemp -d)
trap 'rm -rf -- "$d"' EXIT
p="$d/harmless value's file"
printf '%s' 'synthetic-only-42' > "$p"
read_key "$p"
'@
 } else {
@'
set -eu
read_key() { cat -- "$1"; }
read_key "$HOME/.config/garmin-sync/api_key"
'@
 }
 $start = New-Object Diagnostics.ProcessStartInfo
 $start.FileName = 'wsl.exe'
 $start.Arguments = if ($Distribution) { '-d ' + $Distribution + ' --exec sh -s' } else { '--exec sh -s' }
 $start.UseShellExecute = $false
 $start.CreateNoWindow = $true
 $start.RedirectStandardInput = $true
 $start.RedirectStandardOutput = $true
 $start.RedirectStandardError = $true
 $process = New-Object Diagnostics.Process
 $process.StartInfo = $start
 try {
  [void]$process.Start()
  $out = $process.StandardOutput.ReadToEndAsync()
  $err = $process.StandardError.ReadToEndAsync()
  $process.StandardInput.Write(($program.Replace("`r`n","`n") + "`n"))
  $process.StandardInput.Close()
  $process.WaitForExit()
  $value = $out.GetAwaiter().GetResult().Trim()
  [void]$err.GetAwaiter().GetResult()
  if ($process.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($value) -or $value -match '\s') { throw 'WSL read failed.' }
  return $value
 } finally { $process.Dispose() }
}
