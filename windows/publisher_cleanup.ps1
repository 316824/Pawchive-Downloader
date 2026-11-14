[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [switch]$Preview,
    [switch]$RemoveRegistry,
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-CurrentUserTaskPrefix {
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::ASCII.GetBytes($sid)
        $digest = $sha.ComputeHash($bytes)
    }
    finally {
        $sha.Dispose()
    }
    $hex = ([BitConverter]::ToString($digest) -replace '-', '').ToLowerInvariant()
    return "PawchiveDownloader-$($hex.Substring(0, 12))-"
}

function Get-ApplicationTasks {
    param([string]$Prefix)

    $result = [System.Collections.Generic.List[string]]::new()
    $pattern = '^\\?' + [Regex]::Escape($Prefix) + '[0-9a-f]{32}$'
    $lines = @()
    try {
        $lines = & schtasks.exe /Query /FO CSV /NH 2>$null
    }
    catch {
        Write-Warning "无法查询 Windows 计划任务：$($_.Exception.Message)"
        return $result
    }
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -match '"(?<name>\\?PawchiveDownloader-[^"]+)"') {
            $name = $Matches['name']
            if ($name -match $pattern -and -not $result.Contains($name)) {
                $result.Add($name)
            }
        }
    }
    return $result
}

function Remove-ApplicationTask {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param([string]$TaskName)

    if (-not $PSCmdlet.ShouldProcess($TaskName, '删除 Pawchive Downloader 计划任务')) {
        return
    }
    $output = & schtasks.exe /Delete /TN $TaskName /F 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "删除计划任务失败：$TaskName`n$($output -join [Environment]::NewLine)"
    }
    Write-Host "已删除计划任务：$TaskName" -ForegroundColor Green
}

function Get-BackgroundArtifacts {
    param([string]$StateDirectory)

    # BackgroundStore keeps only this manifest and bounded JPEG previews in
    # the application data directory.  Keep the paths explicit here so a
    # future state-directory layout change cannot silently leave them behind.
    $paths = @(
        (Join-Path $StateDirectory 'backgrounds.json'),
        (Join-Path $StateDirectory 'backgrounds')
    )
    return @($paths | Where-Object { Test-Path -LiteralPath $_ })
}

function Get-AbsoluteDirectoryPath {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    if ([string]::IsNullOrWhiteSpace($LiteralPath) -or
        -not [IO.Path]::IsPathRooted($LiteralPath)) {
        throw "拒绝相对目录：$LiteralPath"
    }
    $full = [IO.Path]::GetFullPath($LiteralPath)
    $root = [IO.Path]::GetPathRoot($full)
    $inputRoot = [IO.Path]::GetPathRoot($LiteralPath)
    if ($LiteralPath -match '^[A-Za-z]:(?:$|[^\\/])' -or
        $inputRoot -in @('\', '/') -or $full -eq $root) {
        throw "拒绝驱动器根目录或未限定路径：$LiteralPath"
    }
    return $full.TrimEnd([IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar)
}

function Assert-RemovalDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$ExpectedParent,
        [Parameter(Mandatory = $true)][string]$LeafPattern,
        [Nullable[datetime]]$BeforeUtc = $null
    )

    $full = Get-AbsoluteDirectoryPath -LiteralPath $LiteralPath
    $parent = Get-AbsoluteDirectoryPath -LiteralPath $ExpectedParent
    $actualParent = [IO.Directory]::GetParent($full).FullName
    $leaf = [IO.Path]::GetFileName($full)
    if (-not $actualParent.Equals($parent, [StringComparison]::OrdinalIgnoreCase) -or
        $leaf -notmatch $LeafPattern) {
        throw "拒绝超出清理范围的目录：$full"
    }
    $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
    if (-not $item.PSIsContainer) { throw "拒绝非目录清理目标：$full" }
    if ($null -ne $BeforeUtc -and $item.LastWriteTimeUtc -ge $BeforeUtc) {
        throw "临时目录未超过 24 小时，已保留：$full"
    }
    # Reject a junction/symlink in any existing ancestor as well as the target.
    # GetFullPath normalizes '..' but does not resolve reparse-point targets.
    $ancestor = $item
    while ($null -ne $ancestor) {
        if (($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "拒绝通过重解析点清理目录：$($ancestor.FullName)"
        }
        $ancestor = $ancestor.Parent
    }
    # Windows PowerShell and PowerShell 7 have differed in recursive junction
    # handling. Traverse one level at a time and refuse the complete operation
    # if any descendant is a reparse point; never recurse through that entry.
    $prefix = $full + [IO.Path]::DirectorySeparatorChar
    $pending = [System.Collections.Generic.Stack[string]]::new()
    $pending.Push($full)
    while ($pending.Count -gt 0) {
        $directory = $pending.Pop()
        foreach ($child in @(Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop)) {
            $childPath = [IO.Path]::GetFullPath($child.FullName)
            if (-not $childPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or
                ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "拒绝包含越界路径或重解析点的目录：$($child.FullName)"
            }
            if ($child.PSIsContainer) { $pending.Push($childPath) }
        }
    }
    return $full
}

function Split-WindowsCommandLine {
    param([string]$CommandLine)

    if (-not ('PawchiveCleanupCommandLine' -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class PawchiveCleanupCommandLine {
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CommandLineToArgvW(string line, out int count);
    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr memory);
    public static string[] Split(string line) {
        int count;
        IntPtr memory = CommandLineToArgvW(line, out count);
        if (memory == IntPtr.Zero) throw new InvalidOperationException("Cannot parse process command line.");
        try {
            string[] result = new string[count];
            for (int i = 0; i < count; i++)
                result[i] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(memory, i * IntPtr.Size));
            return result;
        } finally { LocalFree(memory); }
    }
}
"@
    }
    return [PawchiveCleanupCommandLine]::Split($CommandLine)
}

function Test-PawchivePythonCommand {
    param([string]$CommandLine)

    if ([string]::IsNullOrWhiteSpace($CommandLine)) { return $false }
    $arguments = @(Split-WindowsCommandLine -CommandLine $CommandLine)
    for ($index = 1; $index -lt $arguments.Count; $index++) {
        $argument = $arguments[$index]
        $leaf = ''
        try { $leaf = [IO.Path]::GetFileName($argument) }
        catch [ArgumentException] { } # A Python -c payload need not be a path.
        # This application-specific filename is safe to recognize even when a
        # user launches another checkout or passes a relative script path.
        if ($leaf -ieq 'download_pawchive_images.py') { return $true }
        if ($argument -eq '-m' -and $index + 1 -lt $arguments.Count -and
            $arguments[$index + 1] -match '^(?:windows\.)?download_pawchive_images$') { return $true }
        if ($argument -eq '-c' -and $index + 1 -lt $arguments.Count -and
            $arguments[$index + 1] -match '\bdownload_pawchive_images\b' -and
            $arguments[$index + 1] -match '\bdesktop_entry\b') { return $true }
        if ($leaf -ieq 'desktop_entry.py' -and [IO.Path]::IsPathRooted($argument)) {
            $directory = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($argument))
            $knownDirectories = @(
                [IO.Path]::GetFullPath($PSScriptRoot),
                [IO.Path]::GetFullPath((Join-Path (Split-Path $PSScriptRoot -Parent) 'PawchiveDownloader\windows'))
            )
            foreach ($known in $knownDirectories) {
                if ($directory.Equals($known, [StringComparison]::OrdinalIgnoreCase)) { return $true }
            }
        }
    }
    return $false
}

function Get-ApplicationProcesses {
    $applicationProcesses = [System.Collections.Generic.List[string]]::new()
    foreach ($process in @(Get-Process -Name 'PawchiveDownloader' -ErrorAction SilentlyContinue)) {
        $applicationProcesses.Add("PawchiveDownloader (PID $($process.Id))")
    }
    $complete = $true
    $python = @(Get-Process -Name 'python', 'pythonw', 'python3' -ErrorAction SilentlyContinue)
    if ($python.Count -gt 0) {
        try {
            $candidates = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name = 'python3.exe'" -ErrorAction Stop)
            foreach ($process in $candidates) {
                if ([string]::IsNullOrWhiteSpace($process.CommandLine)) {
                    $complete = $false
                    continue
                }
                if (Test-PawchivePythonCommand -CommandLine $process.CommandLine) {
                    $applicationProcesses.Add("$($process.Name) / Pawchive 脚本 (PID $($process.ProcessId))")
                }
            }
        }
        catch {
            $complete = $false
            Write-Warning "无法核实 Python 脚本进程：$($_.Exception.Message)"
        }
    }
    return [pscustomobject]@{ Processes = @($applicationProcesses.ToArray()); ScriptCheckComplete = $complete }
}

function Get-TemporaryDirectories {
    param([string[]]$Candidates)

    $seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $result = [System.Collections.Generic.List[string]]::new()
    foreach ($candidate in $Candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        try {
            $full = Get-AbsoluteDirectoryPath -LiteralPath $candidate
            if (Test-Path -LiteralPath $full) {
                $ancestor = Get-Item -LiteralPath $full -Force -ErrorAction Stop
                if (-not $ancestor.PSIsContainer) { throw "临时路径不是目录：$full" }
                while ($null -ne $ancestor) {
                    if (($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                        throw "临时路径包含重解析点：$($ancestor.FullName)"
                    }
                    $ancestor = $ancestor.Parent
                }
            }
            if ($seen.Add($full)) { $result.Add($full) }
        }
        catch { Write-Warning "已忽略不安全的临时根目录：$($_.Exception.Message)" }
    }
    return $result
}

function Assert-StaleTemporaryDirectory {
    param(
        [string]$LiteralPath,
        [string[]]$ApprovedRoots,
        [datetime]$BeforeUtc
    )

    $full = Get-AbsoluteDirectoryPath -LiteralPath $LiteralPath
    $parent = [IO.Directory]::GetParent($full).FullName
    if ($ApprovedRoots -notcontains $parent) {
        throw "拒绝不属于已批准临时根目录的缓存：$full"
    }
    return Assert-RemovalDirectory -LiteralPath $full -ExpectedParent $parent `
        -LeafPattern '^pawchive_(?:cache|preview)_[A-Za-z0-9_-]+$' -BeforeUtc $BeforeUtc
}

function Get-StaleCacheDirectories {
    param(
        [string]$TempDirectory,
        [datetime]$BeforeUtc
    )

    if (-not (Test-Path -LiteralPath $TempDirectory -PathType Container)) {
        return @()
    }
    $result = [System.Collections.Generic.List[string]]::new()
    try {
        $entries = Get-ChildItem -LiteralPath $TempDirectory -Directory -Force -ErrorAction Stop
        foreach ($entry in $entries) {
            if ($entry.Name -notmatch '^pawchive_(?:cache|preview)_[A-Za-z0-9_-]+$') { continue }
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
            if ($entry.LastWriteTimeUtc -ge $BeforeUtc) { continue }
            try {
                $validated = Assert-RemovalDirectory -LiteralPath $entry.FullName -ExpectedParent $TempDirectory `
                    -LeafPattern '^pawchive_(?:cache|preview)_[A-Za-z0-9_-]+$' -BeforeUtc $BeforeUtc
                $result.Add($validated)
            }
            catch { Write-Warning "已保留不安全的临时目录：$($_.Exception.Message)" }
        }
    }
    catch {
        Write-Warning "无法查询过期临时缓存：$($_.Exception.Message)"
    }
    return $result
}

$localDataDir = Get-AbsoluteDirectoryPath -LiteralPath ([Environment]::GetFolderPath('LocalApplicationData'))
$stateDir = Get-AbsoluteDirectoryPath -LiteralPath (Join-Path $localDataDir 'PawchiveDownloader')
# Python tempfile and .NET can select different roots when TEMP and TMP differ.
# Inventory each explicit OS/Python candidate once, never a recursive temp scan.
$tempDirectories = @(Get-TemporaryDirectories -Candidates @(
    $env:TEMP, $env:TMP, $env:TMPDIR, [IO.Path]::GetTempPath(),
    (Join-Path $localDataDir 'Temp')
))
$staleCacheBefore = [DateTime]::UtcNow.AddHours(-24)
$registryPath = 'HKCU:\Software\PawchiveTools\PawchiveDownloader'
$prefix = Get-CurrentUserTaskPrefix
$tasks = @(Get-ApplicationTasks -Prefix $prefix)
$backgroundArtifacts = @(Get-BackgroundArtifacts -StateDirectory $stateDir)
$staleCacheDirectories = @(@(
    foreach ($directory in $tempDirectories) {
        Get-StaleCacheDirectories -TempDirectory $directory -BeforeUtc $staleCacheBefore
    }
) | Sort-Object -Unique)
$processCheck = Get-ApplicationProcesses
$processes = @($processCheck.Processes)

Write-Host 'Pawchive Downloader · 发布者本机记录清理工具' -ForegroundColor Cyan
Write-Host "数据目录：$stateDir"
Write-Host '已批准的临时缓存根目录：'
$tempDirectories | ForEach-Object { Write-Host "  $_" }
if ($staleCacheDirectories.Count -eq 0) {
    Write-Host '超过 24 小时的 Pawchive 临时缓存：未发现'
}
else {
    Write-Host "超过 24 小时的 Pawchive 临时缓存：发现 $($staleCacheDirectories.Count) 项（随清理移除）"
    $staleCacheDirectories | ForEach-Object { Write-Host "  $_" }
}
if ($backgroundArtifacts.Count -eq 0) {
    Write-Host '背景图配置和缩略图缓存：未发现'
}
else {
    Write-Host "背景图配置和缩略图缓存：发现 $($backgroundArtifacts.Count) 项（随本机数据一并清理）"
    $backgroundArtifacts | ForEach-Object { Write-Host "  $_" }
}
Write-Host '本机状态范围：签到/成就/激活/日程/断点、背景、今日推荐缓存、画师档案/头像、订阅基线与更新忽略设置。'
Write-Host "注册表项：$registryPath"
Write-Host "当前用户任务前缀：$prefix"
if ($tasks.Count -eq 0) {
    Write-Host '未发现当前用户的 Pawchive 计划任务。'
}
else {
    Write-Host "发现计划任务 $($tasks.Count) 个："
    $tasks | ForEach-Object { Write-Host "  $_" }
}
if ($processes.Count -gt 0) {
    $processes | ForEach-Object { Write-Host "仍在运行：$_" -ForegroundColor Yellow }
    throw '检测到 PawchiveDownloader 或其 Python 脚本仍在运行（含托盘后台）。请先完全退出程序，再重新执行本工具。'
}

if (-not $processCheck.ScriptCheckComplete) {
    if ($Preview -or $WhatIfPreference) {
        Write-Warning '未能确认全部 Python 进程。当前仅预览；实际清理前必须完成进程检查。'
    }
    else {
        throw '无法确认 Python 脚本是否仍在使用 Pawchive 数据。请退出相关 Python 进程，或解决进程查询权限后重试。'
    }
}

# Validate the explicit state target before any task or filesystem mutation.
if (Test-Path -LiteralPath $stateDir) {
    $null = Assert-RemovalDirectory -LiteralPath $stateDir -ExpectedParent $localDataDir `
        -LeafPattern '^PawchiveDownloader$'
}

if ($Preview) {
    $WhatIfPreference = $true
    Write-Host '预览模式：不会删除任何内容。' -ForegroundColor Yellow
}
if ($Force) {
    $ConfirmPreference = 'None'
}

if (-not $Preview -and -not $Force -and -not $WhatIfPreference) {
    Write-Host ''
    Write-Host '本操作会删除签到/等级/徽章/皮肤、激活、日程、断点、背景、今日推荐缓存、画师档案/头像、订阅记录、更新忽略选择及其它本机设置。' -ForegroundColor Yellow
    Write-Host '同时清理超过 24 小时的扫描与图集预览临时目录。' -ForegroundColor Yellow
    Write-Host '不会删除用户已下载的图片、附件或下载目录中的 .part 文件。' -ForegroundColor Yellow
    $answer = Read-Host '确认清理请输入 CLEAN，其他输入取消'
    if ($answer -cne 'CLEAN') {
        Write-Host '已取消，没有修改任何内容。'
        exit 0
    }
}

# The user may have reopened the app while reviewing the confirmation prompt.
if (-not $Preview -and -not $WhatIfPreference) {
    $latestProcessCheck = Get-ApplicationProcesses
    if ($latestProcessCheck.Processes.Count -gt 0 -or -not $latestProcessCheck.ScriptCheckComplete) {
        throw '清理前再次检查发现程序仍在运行，或无法确认脚本进程。已停止清理，请完全退出软件后重试。'
    }
}

foreach ($task in $tasks) {
    Remove-ApplicationTask -TaskName $task
}

foreach ($cacheDirectory in $staleCacheDirectories) {
    # Recheck immediately before recursive deletion in case a directory changed
    # after the initial inventory or user confirmation.
    $cacheDirectory = Assert-StaleTemporaryDirectory -LiteralPath $cacheDirectory `
        -ApprovedRoots $tempDirectories -BeforeUtc $staleCacheBefore
    if ($PSCmdlet.ShouldProcess($cacheDirectory, '删除过期 Pawchive 临时缓存')) {
        Remove-Item -LiteralPath $cacheDirectory -Recurse -Force
        Write-Host "已删除过期临时缓存：$cacheDirectory" -ForegroundColor Green
    }
}

if (Test-Path -LiteralPath $stateDir) {
    $stateDir = Assert-RemovalDirectory -LiteralPath $stateDir -ExpectedParent $localDataDir `
        -LeafPattern '^PawchiveDownloader$'
    if ($PSCmdlet.ShouldProcess($stateDir, '删除 Pawchive 本机数据目录')) {
        Remove-Item -LiteralPath $stateDir -Recurse -Force
        Write-Host "已删除本机数据目录：$stateDir" -ForegroundColor Green
    }
}
else {
    Write-Host '本机数据目录不存在。'
}

if ($RemoveRegistry -and (Test-Path -LiteralPath $registryPath)) {
    if ($PSCmdlet.ShouldProcess($registryPath, '删除 Pawchive 默认路径设置')) {
        Remove-Item -LiteralPath $registryPath -Recurse -Force
        Write-Host "已删除注册表设置：$registryPath" -ForegroundColor Green
    }
}
elseif ($RemoveRegistry) {
    Write-Host '注册表设置不存在。'
}
else {
    Write-Host '已保留 Qt 默认路径设置；如需一并清除，请追加 -RemoveRegistry。'
}

Write-Host ''
if ($Preview -or $WhatIfPreference) {
    Write-Host '预览结束：未删除任何内容。' -ForegroundColor Yellow
}
else {
    Write-Host '清理完成。用户下载目录和邀请码签发私钥未被触碰。' -ForegroundColor Green
}
