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
            if ($entry.Name -notlike 'pawchive_cache_*') { continue }
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { continue }
            if ($entry.LastWriteTimeUtc -ge $BeforeUtc) { continue }
            $result.Add($entry.FullName)
        }
    }
    catch {
        Write-Warning "无法查询过期临时缓存：$($_.Exception.Message)"
    }
    return $result
}

$stateDir = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'PawchiveDownloader'
$tempDir = [IO.Path]::GetTempPath()
$staleCacheBefore = [DateTime]::UtcNow.AddHours(-24)
$registryPath = 'HKCU:\Software\PawchiveTools\PawchiveDownloader'
$prefix = Get-CurrentUserTaskPrefix
$tasks = @(Get-ApplicationTasks -Prefix $prefix)
$backgroundArtifacts = @(Get-BackgroundArtifacts -StateDirectory $stateDir)
$staleCacheDirectories = @(Get-StaleCacheDirectories -TempDirectory $tempDir -BeforeUtc $staleCacheBefore)
$processes = @(Get-Process -Name 'PawchiveDownloader' -ErrorAction SilentlyContinue)

Write-Host 'Pawchive Downloader · 发布者本机记录清理工具' -ForegroundColor Cyan
Write-Host "数据目录：$stateDir"
Write-Host "临时缓存目录：$tempDir"
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
    throw '检测到 PawchiveDownloader 仍在运行（含托盘后台）。请先完全退出程序，再重新执行本工具。'
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
    Write-Host '本操作会删除签到/等级/徽章/皮肤、激活、日程、断点、背景配置/缩略图、超过 24 小时的临时缓存和本机设置。' -ForegroundColor Yellow
    Write-Host '不会删除用户已下载的图片、附件或下载目录中的 .part 文件。' -ForegroundColor Yellow
    $answer = Read-Host '确认清理请输入 CLEAN，其他输入取消'
    if ($answer -cne 'CLEAN') {
        Write-Host '已取消，没有修改任何内容。'
        exit 0
    }
}

foreach ($task in $tasks) {
    Remove-ApplicationTask -TaskName $task
}

foreach ($cacheDirectory in $staleCacheDirectories) {
    if ($PSCmdlet.ShouldProcess($cacheDirectory, '删除过期 Pawchive 临时缓存')) {
        Remove-Item -LiteralPath $cacheDirectory -Recurse -Force
        Write-Host "已删除过期临时缓存：$cacheDirectory" -ForegroundColor Green
    }
}

if (Test-Path -LiteralPath $stateDir) {
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
