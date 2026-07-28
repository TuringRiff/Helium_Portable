[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-HeliumId {
    param([AllowNull()][object]$Value)

    if ($null -eq $Value) { return $false }
    return ([string]$Value) -match '(?i)^Helium(?:(?:HTM|HTML|PDF)?[._-].+)?$'
}

function Convert-ToNativeRegistryPath {
    param([string]$Path)

    $providerPrefix = 'Microsoft.PowerShell.Core\Registry::'
    if ($Path.StartsWith($providerPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        return $Path.Substring($providerPrefix.Length)
    }
    if ($Path.StartsWith('HKCU:\', [StringComparison]::OrdinalIgnoreCase)) {
        return 'HKEY_CURRENT_USER\' + $Path.Substring(6)
    }
    if ($Path.StartsWith('HKLM:\', [StringComparison]::OrdinalIgnoreCase)) {
        return 'HKEY_LOCAL_MACHINE\' + $Path.Substring(6)
    }
    throw "Unsupported registry path: $Path"
}

$script:ChangeCount = 0
$script:FailureCount = 0
$script:BackupCount = 0
$script:BackedUpPaths = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$script:AppIds = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$script:ProgIds = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)

$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path $env:LOCALAPPDATA "HeliumPortable\RegistryBackups\$timestamp"
$logPath = Join-Path $backupRoot 'cleanup.log'

function Write-Log {
    param([string]$Message)

    Write-Host $Message
    if (-not $DryRun) {
        Add-Content -LiteralPath $logPath -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $Message) -Encoding UTF8
    }
}

function Backup-RegistryKey {
    param([string]$Path)

    if ($DryRun -or -not (Test-Path -LiteralPath $Path)) { return }
    if (-not $script:BackedUpPaths.Add($Path)) { return }

    $script:BackupCount++
    $safeName = ($Path -replace '[:\\/]+', '_') -replace '[^A-Za-z0-9_.-]', '_'
    if ($safeName.Length -gt 150) { $safeName = $safeName.Substring(0, 150) }
    $destination = Join-Path $backupRoot ('{0:D3}_{1}.reg' -f $script:BackupCount, $safeName)
    $nativePath = Convert-ToNativeRegistryPath $Path
    & reg.exe export $nativePath $destination /y *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not back up registry key: $nativePath"
    }
}

function Remove-RegistryTree {
    param(
        [string]$Path,
        [string]$Reason
    )

    if (-not (Test-Path -LiteralPath $Path)) { return }
    if ($DryRun) {
        Write-Log "[DRY RUN] Remove key: $Path ($Reason)"
        $script:ChangeCount++
        return
    }

    try {
        Backup-RegistryKey $Path
        Remove-Item -LiteralPath $Path -Recurse -Force
        Write-Log "Removed key: $Path ($Reason)"
        $script:ChangeCount++
    }
    catch {
        Write-Warning "Failed to remove key: $Path - $($_.Exception.Message)"
        $script:FailureCount++
    }
}

function Remove-RegistryValue {
    param(
        [string]$Path,
        [string]$Name,
        [string]$Reason
    )

    if (-not (Test-Path -LiteralPath $Path)) { return }
    if ($DryRun) {
        Write-Log "[DRY RUN] Remove value: $Path -> $Name ($Reason)"
        $script:ChangeCount++
        return
    }

    try {
        Backup-RegistryKey $Path
        Remove-ItemProperty -LiteralPath $Path -Name $Name -Force
        Write-Log "Removed value: $Path -> $Name ($Reason)"
        $script:ChangeCount++
    }
    catch {
        Write-Warning "Failed to remove value: $Path -> $Name - $($_.Exception.Message)"
        $script:FailureCount++
    }
}

function Get-RegistryValueNames {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) { return @() }
    $item = Get-Item -LiteralPath $Path
    return @($item.GetValueNames())
}

function Get-RegistryValue {
    param(
        [string]$Path,
        [string]$Name
    )

    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    return (Get-Item -LiteralPath $Path).GetValue($Name, $null)
}

function Test-KnownHeliumId {
    param([AllowNull()][object]$Value)

    if ($null -eq $Value) { return $false }
    $text = [string]$Value
    return (Test-HeliumId $text) -or $script:AppIds.Contains($text) -or $script:ProgIds.Contains($text)
}

if ($env:OS -ne 'Windows_NT') {
    throw 'This script can only run on Windows.'
}

if (-not (Test-IsAdministrator) -and -not $DryRun) {
    if ($Elevated) { throw 'Administrator privileges were requested but not granted.' }

    $arguments = @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"{0}"' -f $PSCommandPath), '-Elevated')
    if ($DryRun) { $arguments += '-DryRun' }
    try {
        $process = Start-Process -FilePath 'powershell.exe' -ArgumentList $arguments -Verb RunAs -Wait -PassThru
        exit $process.ExitCode
    }
    catch {
        Write-Error 'Administrator permission was cancelled. No registry entries were changed.'
        exit 1
    }
}

if (-not $DryRun) {
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
}

Write-Log 'Scanning Helium browser registrations...'

$startMenuRoots = @(
    'HKCU:\Software\Clients\StartMenuInternet',
    'HKLM:\Software\Clients\StartMenuInternet',
    'HKLM:\Software\WOW6432Node\Clients\StartMenuInternet'
)
$registeredAppRoots = @(
    'HKCU:\Software\RegisteredApplications',
    'HKLM:\Software\RegisteredApplications',
    'HKLM:\Software\WOW6432Node\RegisteredApplications'
)
$classRoots = @(
    'HKCU:\Software\Classes',
    'HKLM:\Software\Classes',
    'HKLM:\Software\WOW6432Node\Classes'
)

# Collect IDs from browser capabilities before removing their registration trees.
foreach ($root in $startMenuRoots) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    foreach ($key in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {
        $capabilitiesPath = Join-Path $key.PSPath 'Capabilities'
        $applicationName = $null
        if (Test-Path -LiteralPath $capabilitiesPath) {
            $applicationName = Get-RegistryValue $capabilitiesPath 'ApplicationName'
        }
        if (-not ((Test-HeliumId $key.PSChildName) -or ([string]$applicationName -eq 'Helium'))) { continue }

        [void]$script:AppIds.Add($key.PSChildName)
        foreach ($associationGroup in @('FileAssociations', 'URLAssociations')) {
            $associationPath = Join-Path $capabilitiesPath $associationGroup
            foreach ($name in @(Get-RegistryValueNames $associationPath)) {
                $value = (Get-Item -LiteralPath $associationPath).GetValue($name)
                if (Test-HeliumId $value) { [void]$script:ProgIds.Add([string]$value) }
            }
        }
    }
}

# Also discover IDs directly from Classes so older/newer installer suffixes are covered.
foreach ($root in $classRoots) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    foreach ($key in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {
        if (Test-HeliumId $key.PSChildName) {
            if ($key.PSChildName -match '(?i)^Helium(?:HTM|HTML|PDF)[._-]') {
                [void]$script:ProgIds.Add($key.PSChildName)
            }
            elseif ($key.PSChildName -ne 'helium') {
                [void]$script:AppIds.Add($key.PSChildName)
            }
        }
    }
}

foreach ($root in $registeredAppRoots) {
    foreach ($name in @(Get-RegistryValueNames $root)) {
        $value = (Get-Item -LiteralPath $root).GetValue($name)
        if ((Test-KnownHeliumId $name) -or ([string]$value -match '(?i)\\Helium[^\\]*\\Capabilities$')) {
            [void]$script:AppIds.Add($name)
            Remove-RegistryValue $root $name 'Default Apps registration'
        }
    }
}

foreach ($root in $startMenuRoots) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    foreach ($key in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue)) {
        if ($script:AppIds.Contains($key.PSChildName) -or (Test-HeliumId $key.PSChildName)) {
            Remove-RegistryTree $key.PSPath 'Start menu browser registration'
        }
    }
}

foreach ($root in $classRoots) {
    if (-not (Test-Path -LiteralPath $root)) { continue }

    foreach ($id in @($script:ProgIds) + @($script:AppIds) + @('helium')) {
        if (Test-HeliumId $id) {
            Remove-RegistryTree (Join-Path $root $id) 'Helium protocol or ProgId registration'
        }
    }

    foreach ($extensionKey in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName.StartsWith('.') })) {
        $openWithProgids = Join-Path $extensionKey.PSPath 'OpenWithProgids'
        foreach ($name in @(Get-RegistryValueNames $openWithProgids)) {
            if (Test-KnownHeliumId $name) {
                Remove-RegistryValue $openWithProgids $name 'Helium Open With ProgId'
            }
        }
    }
}

$urlAssociations = 'HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations'
if (Test-Path -LiteralPath $urlAssociations) {
    foreach ($protocol in @(Get-ChildItem -LiteralPath $urlAssociations -ErrorAction SilentlyContinue)) {
        foreach ($choice in @(Get-ChildItem -LiteralPath $protocol.PSPath -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -like 'UserChoice*' })) {
            $progId = Get-RegistryValue $choice.PSPath 'ProgId'
            if (Test-KnownHeliumId $progId) {
                Remove-RegistryTree $choice.PSPath "Reset $($protocol.PSChildName) default association"
            }
        }
    }
}

$fileExts = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts'
if (Test-Path -LiteralPath $fileExts) {
    foreach ($extension in @(Get-ChildItem -LiteralPath $fileExts -ErrorAction SilentlyContinue)) {
        foreach ($choice in @(Get-ChildItem -LiteralPath $extension.PSPath -ErrorAction SilentlyContinue | Where-Object { $_.PSChildName -like 'UserChoice*' })) {
            $progId = Get-RegistryValue $choice.PSPath 'ProgId'
            if (Test-KnownHeliumId $progId) {
                Remove-RegistryTree $choice.PSPath "Reset $($extension.PSChildName) default association"
            }
        }

        $openWithProgids = Join-Path $extension.PSPath 'OpenWithProgids'
        foreach ($name in @(Get-RegistryValueNames $openWithProgids)) {
            if (Test-KnownHeliumId $name) {
                Remove-RegistryValue $openWithProgids $name 'Helium Open With ProgId cache'
            }
        }

        $openWithList = Join-Path $extension.PSPath 'OpenWithList'
        $removedLetters = @()
        foreach ($name in @(Get-RegistryValueNames $openWithList)) {
            if ($name -eq 'MRUList') { continue }
            $value = (Get-Item -LiteralPath $openWithList).GetValue($name)
            if (Test-KnownHeliumId $value) {
                Remove-RegistryValue $openWithList $name 'Helium Open With application cache'
                $removedLetters += $name
            }
        }
        if ($removedLetters.Count -gt 0 -and (Test-Path -LiteralPath $openWithList)) {
            $mru = Get-RegistryValue $openWithList 'MRUList'
            if ($null -ne $mru) {
                $newMru = [string]$mru
                foreach ($letter in $removedLetters) { $newMru = $newMru.Replace($letter, '') }
                if ($DryRun) {
                    Write-Log "[DRY RUN] Update value: $openWithList -> MRUList (remove Helium entries)"
                }
                else {
                    Backup-RegistryKey $openWithList
                    Set-ItemProperty -LiteralPath $openWithList -Name 'MRUList' -Value $newMru
                    Write-Log "Updated value: $openWithList -> MRUList (removed Helium entries)"
                }
                $script:ChangeCount++
            }
        }
    }
}

$toastPath = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\ApplicationAssociationToasts'
foreach ($name in @(Get-RegistryValueNames $toastPath)) {
    $prefix = $name.Split('_')[0]
    if (Test-KnownHeliumId $prefix) {
        Remove-RegistryValue $toastPath $name 'Helium association notification cache'
    }
}

if (-not $DryRun) {
    try {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class ShellAssociationRefresh {
    [DllImport("shell32.dll")]
    public static extern void SHChangeNotify(uint eventId, uint flags, IntPtr item1, IntPtr item2);
}
'@
        [ShellAssociationRefresh]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero)
    }
    catch {
        Write-Warning 'Registry cleanup succeeded, but Windows Shell refresh notification failed.'
    }
}

Write-Host ''
if ($DryRun) {
    Write-Host "Dry run complete. Planned changes: $script:ChangeCount"
}
elseif ($script:FailureCount -eq 0) {
    Write-Host "Cleanup complete. Registry changes: $script:ChangeCount"
    Write-Host "Backup folder: $backupRoot"
    Write-Host 'Windows may temporarily fall back to Microsoft Edge until you choose a default browser.'
}
else {
    Write-Warning "Cleanup finished with $script:FailureCount failure(s). Review: $logPath"
}

if ($Elevated -and [Environment]::UserInteractive) {
    Write-Host ''
    Read-Host 'Press Enter to close this administrator window'
}

if ($script:FailureCount -gt 0) { exit 2 }
exit 0
