"""Bounded GitHub OTA staging and an independent, rollback-capable Windows updater.

Only verified one-file Windows x64 EXEs are installed. A ZIP is a transport,
not permission to extract arbitrary files into the user's application folder.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import tempfile
import threading
import time
from urllib.parse import unquote, urljoin, urlsplit
import uuid
import zipfile

import requests
from PySide6.QtCore import QObject, Signal


PREFIX = 'pawchive_update_'
MAX_PACKAGE_BYTES = 256 * 1024 * 1024
MAX_CHECKSUM_BYTES = 16 * 1024
DOWNLOAD_TIMEOUT = 3
DOWNLOAD_DEADLINE = 600
CDN_HOSTS = frozenset({'github.com', 'release-assets.githubusercontent.com',
                       'objects.githubusercontent.com', 'github-releases.githubusercontent.com'})
_TAG = re.compile(r'^(?:Windows-)?v?[0-9]+\.[0-9]+(?:\.[0-9]+)?$', re.ASCII | re.I)
_PACKAGE = re.compile(r'^PawchiveDownloader(?:-Windows)?(?:-x64)?(?:-v?[0-9]+\.[0-9]+(?:\.[0-9]+)?)?\.(exe|zip)$', re.ASCII | re.I)


class UpdateError(OSError):
    """A recoverable update failure suitable for displaying in the update dialog."""


@dataclass(frozen=True)
class StagedUpdate:
    root: Path
    exe: Path
    version: str
    sha256: str = ''


def _digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(256 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def _asset_value(asset, name, default=None):
    return asset.get(name, default) if isinstance(asset, dict) else getattr(asset, name, default)


def safe_asset_url(value, tag=None, name=None):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) <= 32 for c in value):
        return False
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.netloc.lower() != 'github.com'
                or parsed.query or parsed.fragment):
            return False
        pieces = parsed.path.split('/')
        if len(pieces) != 7 or [p.lower() for p in pieces[1:5]] != ['316824', 'pawchive-downloader', 'releases', 'download']:
            return False
        actual_tag, actual_name = map(unquote, pieces[5:7])
        if not _TAG.fullmatch(actual_tag) or any(c in actual_name for c in '\\/:*?"<>|'):
            return False
        return (tag is None or actual_tag == tag) and (name is None or actual_name == name)
    except (ValueError, UnicodeError):
        return False


def _safe_download_url(url):
    try:
        parsed = urlsplit(url)
        return (len(url) <= 8192 and not any(ord(c) <= 32 for c in url)
                and parsed.scheme == 'https' and parsed.netloc.lower() in CDN_HOSTS
                and not parsed.fragment)
    except (ValueError, UnicodeError, TypeError):
        return False


def _read_asset(url, target, *, limit, expected_size=0, interrupted=None, progress=None):
    interrupted = interrupted or (lambda: False)
    deadline = time.monotonic() + DOWNLOAD_DEADLINE
    response = None
    try:
        for _ in range(6):
            if interrupted():
                raise UpdateError('更新已取消。')
            if not _safe_download_url(url):
                raise UpdateError('更新附件跳转到了非官方地址，请前往 GitHub 手动下载。')
            response = requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True,
                allow_redirects=False, headers={'Accept-Encoding': 'identity', 'User-Agent': 'PawchiveDownloader/1.3.2 (OTA)'})
            if response.status_code not in (301, 302, 303, 307, 308):
                break
            location = response.headers.get('Location', '')
            response.close(); response = None
            url = urljoin(url, location)
        else:
            raise UpdateError('更新附件跳转次数过多。')
        response.raise_for_status()
        if response.status_code != 200:
            raise UpdateError('更新附件未完整返回，请稍后重试。')
        if str(response.headers.get('Content-Type', '')).split(';')[0].lower() in ('text/html', 'application/xhtml+xml'):
            raise UpdateError('更新服务器返回了网页，未下载更新程序。')
        length = response.headers.get('Content-Length')
        length = int(length) if length is not None else 0
        if length < 0 or length > limit or expected_size < 0 or expected_size > limit:
            raise UpdateError('更新附件超过大小限制。')
        total = expected_size or length
        received = 0
        with Path(target).open('xb') as stream:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if interrupted() or time.monotonic() > deadline:
                    raise UpdateError('更新已取消或下载超时，请稍后重试。')
                if not chunk:
                    continue
                received += len(chunk)
                if received > limit or (total and received > total):
                    raise UpdateError('更新附件大小与发布信息不符。')
                stream.write(chunk)
                if progress:
                    progress(received, total)
        if not received or (total and received != total):
            raise UpdateError('更新附件下载不完整，请稍后重试。')
    finally:
        if response is not None:
            response.close()


def validate_exe(path):
    """Reject a webpage, DLL, wrong architecture or obviously incomplete PE."""
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 128 <= path.stat().st_size <= MAX_PACKAGE_BYTES:
        raise UpdateError('更新程序不是有效的 Windows x64 EXE。')
    with path.open('rb') as stream:
        dos = stream.read(64)
        if dos[:2] != b'MZ':
            raise UpdateError('更新程序不是有效的 Windows x64 EXE。')
        offset = struct.unpack_from('<I', dos, 60)[0]
        if not 64 <= offset <= min(1024 * 1024, path.stat().st_size - 94):
            raise UpdateError('更新程序的 PE 头不完整。')
        stream.seek(offset)
        header = stream.read(24)
        machine, sections = struct.unpack_from('<HH', header, 4)
        optional_size, characteristics = struct.unpack_from('<HH', header, 20)
        optional = stream.read(optional_size)
        if (header[:4] != b'PE\0\0' or machine != 0x8664 or not 1 <= sections <= 96
                or not characteristics & 2 or characteristics & 0x2000
                or len(optional) != optional_size or optional_size < 70
                or struct.unpack_from('<H', optional)[0] != 0x20b
                or struct.unpack_from('<H', optional, 68)[0] not in (2, 3)):
            raise UpdateError('更新程序不是可执行的 Windows x64 应用。')


def _owner(root):
    root = Path(root)
    temporary = Path(tempfile.gettempdir()).resolve()
    if (not re.fullmatch(re.escape(PREFIX) + r'[a-zA-Z0-9_-]{6,64}', root.name)
            or root.is_symlink() or getattr(root, 'is_junction', lambda: False)()
            or root.resolve().parent != temporary):
        return None
    try:
        marker = root / 'owner.json'
        if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 8192:
            return None
        value = json.loads(marker.read_text(encoding='utf-8-sig'))
        if (not isinstance(value, dict) or value.get('app') != 'PawchiveDownloader' or value.get('schema') != 1
                or value.get('root_name') != root.name
                or not re.fullmatch(r'[a-f0-9]{32}', str(value.get('token', '')))):
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def _process_start_filetime(pid):
    if os.name != 'nt':
        try:
            os.kill(int(pid), 0)
            return 1
        except (OSError, ValueError):
            return 0
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return 0
    values = [wintypes.FILETIME() for _ in range(4)]
    try:
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
            return 0
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def _write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def cleanup_staged(staged):
    root = Path(staged.root)
    if _owner(root) is None:
        return False
    shutil.rmtree(root)
    return True


def cleanup_stale_updates(*, max_age=24 * 3600, now=None):
    """Sweep only our marked, inactive temp roots; active installers are protected."""
    now = time.time() if now is None else float(now)
    removed = 0
    for root in Path(tempfile.gettempdir()).glob(PREFIX + '*'):
        try:
            owner = _owner(root)
            if owner is None or now - float(owner['created']) < max_age:
                continue
            pid = int(owner.get('owner_pid', 0))
            started = int(owner.get('owner_started', 0))
            # PID reuse is not a reason to retain an old installer forever.
            if pid > 0 and _process_start_filetime(pid) == started and started:
                continue
            if cleanup_staged(StagedUpdate(root, root/'verified.exe', '')):
                removed += 1
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return removed


def _choose_package(release):
    assets = tuple(getattr(release, 'assets', ()) or ())
    candidates = [asset for asset in assets if _PACKAGE.fullmatch(str(_asset_value(asset, 'name', '')))
                  and safe_asset_url(_asset_value(asset, 'url'), str(release.tag), _asset_value(asset, 'name'))]
    candidates.sort(key=lambda asset: (not str(_asset_value(asset, 'name')).lower().endswith('.exe'),
                                      str(_asset_value(asset, 'name')).lower() != 'pawchivedownloader-windows-x64.exe'))
    if not candidates:
        raise UpdateError('此版本没有可内更的 Windows x64 附件，请前往 GitHub 下载。')
    return candidates[0], assets


def _expected_hash(package, assets, root, tag, interrupted):
    digest = str(_asset_value(package, 'digest', '') or '')
    if re.fullmatch(r'sha256:[a-fA-F0-9]{64}', digest):
        return digest[7:].lower()
    name = str(_asset_value(package, 'name'))
    sidecars = [asset for asset in assets if _asset_value(asset, 'name') == name + '.sha256'
                and safe_asset_url(_asset_value(asset, 'url'), tag, name + '.sha256')]
    if len(sidecars) != 1:
        raise UpdateError('发布附件缺少 SHA256 校验信息，请前往 GitHub 手动下载。')
    sidecar = sidecars[0]
    path = root/'checksum.sha256'
    _read_asset(_asset_value(sidecar, 'url'), path, limit=MAX_CHECKSUM_BYTES,
                expected_size=int(_asset_value(sidecar, 'size', 0) or 0), interrupted=interrupted)
    values = []
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        match = re.fullmatch(r'([a-fA-F0-9]{64})(?:[ \t]+\*?(.+))?', line.strip())
        if match and (match[2] is None or match[2] == name):
            values.append(match[1].lower())
    if len(set(values)) != 1:
        raise UpdateError('SHA256 校验文件内容无效，请前往 GitHub 手动下载。')
    return values[0]


def _unpack_package(package_path, destination, interrupted):
    with zipfile.ZipFile(package_path) as archive:
        entries = archive.infolist()
        if len(entries) > 128 or sum(item.file_size for item in entries) > MAX_PACKAGE_BYTES:
            raise UpdateError('更新压缩包超出安全解压限制。')
        candidates = []
        for item in entries:
            path = PurePosixPath(item.filename)
            mode = item.external_attr >> 16
            if (path.is_absolute() or '..' in path.parts or '\\' in item.filename
                    or any(':' in piece for piece in path.parts)
                    or stat.S_ISLNK(mode) or (item.flag_bits & 1)
                    or item.file_size > max(item.compress_size, 1) * 200):
                raise UpdateError('更新压缩包包含不安全路径或异常内容。')
            if not item.is_dir() and _PACKAGE.fullmatch(path.name) and path.suffix.lower() == '.exe':
                candidates.append(item)
        if len(candidates) != 1:
            raise UpdateError('更新压缩包必须包含唯一的 Pawchive Windows EXE。')
        with archive.open(candidates[0]) as source, destination.open('xb') as target:
            total = 0
            while True:
                if interrupted():
                    raise UpdateError('更新已取消。')
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_PACKAGE_BYTES:
                    raise UpdateError('更新程序超过大小限制。')
                target.write(chunk)


def stage_release(release, *, interrupted=None, progress=None):
    interrupted = interrupted or (lambda: False)
    if not _TAG.fullmatch(str(getattr(release, 'tag', ''))):
        raise UpdateError('更新版本号无效。')
    package, assets = _choose_package(release)
    root = Path(tempfile.mkdtemp(prefix=PREFIX))
    try:
        _write_json(root/'owner.json', dict(app='PawchiveDownloader', schema=1, root_name=root.name,
            token=uuid.uuid4().hex, created=time.time(), owner_pid=os.getpid(),
            owner_started=_process_start_filetime(os.getpid())))
    except BaseException:
        shutil.rmtree(root)
        raise
    staged = StagedUpdate(root, root/'verified.exe', str(release.tag))
    try:
        expected = _expected_hash(package, assets, root, str(release.tag), interrupted)
        package_path = root/'package.bin'
        _read_asset(_asset_value(package, 'url'), package_path, limit=MAX_PACKAGE_BYTES,
                    expected_size=int(_asset_value(package, 'size', 0) or 0), interrupted=interrupted, progress=progress)
        if _digest(package_path) != expected:
            raise UpdateError('更新附件 SHA256 校验失败，已取消更新。')
        if str(_asset_value(package, 'name')).lower().endswith('.zip'):
            _unpack_package(package_path, staged.exe, interrupted)
        else:
            package_path.rename(staged.exe)
        validate_exe(staged.exe)
        if interrupted():
            raise UpdateError('更新已取消。')
        return StagedUpdate(root, staged.exe, staged.version, _digest(staged.exe))
    except BaseException:
        cleanup_staged(staged)
        raise


class OtaDownloadWorker(QObject):
    progress = Signal(object, object)
    ready = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, release):
        super().__init__()
        self.release = release
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, name='PawchiveOTA', daemon=True)
        self._thread.start()

    def run(self):
        try:
            staged = stage_release(self.release, interrupted=self._stop.is_set, progress=self.progress.emit)
            if self._stop.is_set():
                cleanup_staged(staged)
            else:
                self.ready.emit(staged)
        except Exception as error:
            if not self._stop.is_set():
                self.failed.emit(str(error))
        finally:
            try:self.finished.emit()
            except RuntimeError:pass  # UI may already be gone during final exit.

    def requestInterruption(self):
        self._stop.set()

    def isRunning(self):
        return bool(self._thread and self._thread.is_alive())

    def wait(self, milliseconds=3000):
        if self._thread is not None:
            self._thread.join(max(0, milliseconds)/1000)
        return not self.isRunning()


_POWERSHELL = r'''$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$backup = $null
$candidate = $null
$replaced = $false
$validated = $false
function Assert-Stage {
    $directory = Get-Item -LiteralPath $root -Force
    if (($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
        $directory.Name -notmatch '^pawchive_update_[A-Za-z0-9_-]{6,64}$' -or
        [IO.Directory]::GetParent($root).FullName -ne [IO.Path]::GetFullPath([string]$config.temp_root).TrimEnd('\')) {
        throw 'The update temporary directory is unsafe.'
    }
    $marker = Get-Item -LiteralPath (Join-Path $root 'owner.json') -Force
    if (($marker.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or $marker.Length -gt 8192) {
        throw 'The update ownership marker is unsafe.'
    }
    $owner = Get-Content -LiteralPath $marker.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($owner.app -ne 'PawchiveDownloader' -or $owner.schema -ne 1 -or
        $owner.root_name -ne $directory.Name -or $owner.token -ne $config.stage_token -or
        $owner.token -notmatch '^[a-f0-9]{32}$') { throw 'The update ownership marker changed.' }
    return $owner
}
function Start-Application([string]$FileName) {
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $FileName
    $start.WorkingDirectory = [IO.Path]::GetDirectoryName($FileName)
    $start.UseShellExecute = $false
    return [Diagnostics.Process]::Start($start)
}
function Get-Sha256([string]$LiteralPath) {
    # A GUI app can inherit PowerShell 7's PSModulePath. Windows PowerShell
    # cannot always autoload Get-FileHash from that path; use .NET directly.
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($LiteralPath)
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '') }
    finally { $stream.Dispose(); $algorithm.Dispose() }
}
try {
    $config = Get-Content -LiteralPath (Join-Path $root 'install.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $owner = Assert-Stage
    $validated = $true
    $target = [IO.Path]::GetFullPath([string]$config.target)
    $targetFolder = [IO.Path]::GetDirectoryName($target)
    $backup = [IO.Path]::GetFullPath([string]$config.backup)
    $candidate = [IO.Path]::GetFullPath([string]$config.candidate)
    if ([string]$config.source -ne (Join-Path $root 'verified.exe') -or
        [IO.Path]::GetExtension($target) -ne '.exe' -or
        [IO.Path]::GetDirectoryName($candidate) -ne $targetFolder -or
        [IO.Path]::GetDirectoryName($backup) -ne $targetFolder -or
        [IO.Path]::GetFileName($candidate) -notmatch '^\.pawchive-[a-f0-9]{32}\.new\.exe$' -or
        [IO.Path]::GetFileName($backup) -notmatch '^\.pawchive-[a-f0-9]{32}\.backup\.exe$' -or
        $config.sha256 -notmatch '^[a-fA-F0-9]{64}$' -or $config.old_sha256 -notmatch '^[a-fA-F0-9]{64}$') {
        throw 'The update installation paths are invalid.'
    }
    $targetItem = Get-Item -LiteralPath $target -Force
    if (($targetItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'The installed application became a link.' }
    $owner.owner_pid = $PID
    $owner.owner_started = [Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime().ToFileTimeUtc()
    $ownerJson = $owner | ConvertTo-Json -Compress
    [IO.File]::WriteAllText((Join-Path $root 'owner.tmp'), $ownerJson, (New-Object Text.UTF8Encoding($false)))
    [IO.File]::Replace((Join-Path $root 'owner.tmp'), (Join-Path $root 'owner.json'), [NullString]::Value)
    $parent = Get-Process -Id ([int]$config.parent_pid) -ErrorAction SilentlyContinue
    if ($parent -and $parent.StartTime.ToUniversalTime().ToFileTimeUtc() -eq [long]$config.parent_started) {
        if (-not $parent.WaitForExit(120000)) { throw 'Pawchive did not exit within two minutes. No files were changed.' }
    }
    if ((Get-Sha256 ([string]$config.source)) -ne $config.sha256) { throw 'Staged update checksum changed.' }
    if ((Get-Sha256 $target) -ne $config.old_sha256) { throw 'The installed application changed while waiting.' }
    Copy-Item -LiteralPath $config.source -Destination $candidate -ErrorAction Stop
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        try { [IO.File]::Replace($candidate, $target, $backup); $replaced = $true; break }
        catch { if ([DateTime]::UtcNow -ge $deadline) { throw }; Start-Sleep -Milliseconds 400 }
    } while ($true)
    $child = Start-Application $target
    if ($null -eq $child) { throw 'The updated app could not be launched.' }
    if ($child.WaitForExit(3000) -and $child.ExitCode -ne 0) { throw 'The updated app failed immediately.' }
    if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Force }
    [void](Assert-Stage)
    # Delete only our fixed files in the already revalidated stage. Never
    # enumerate paths in PowerShell and delegate recursive removal to cmd.
    foreach ($name in @('install.json', 'verified.exe', 'package.bin', 'checksum.sha256', 'updater.ps1', 'result.txt')) {
        $file = Join-Path $root $name
        if (Test-Path -LiteralPath $file) { Remove-Item -LiteralPath $file -Force }
    }
    Remove-Item -LiteralPath (Join-Path $root 'owner.json') -Force
    exit 0
} catch {
    $failure = $_.Exception.Message
    if ($replaced -and (Test-Path -LiteralPath $backup)) {
        try {
            [IO.File]::Replace($backup, $target, [NullString]::Value)
            [void](Start-Application $target)
        } catch { $failure += '; rollback: ' + $_.Exception.Message }
    }
    if ($validated) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { Remove-Item -LiteralPath $candidate -Force -ErrorAction SilentlyContinue }
        $failure | Set-Content -LiteralPath (Join-Path $root 'update-failed.txt') -Encoding UTF8
    }
    if ($env:PAWCHIVE_OTA_NO_ERROR_UI -ne '1') {
        try { Add-Type -AssemblyName System.Windows.Forms; [void][Windows.Forms.MessageBox]::Show($failure, 'Pawchive update failed') } catch {}
    }
    exit 1
}
'''


_BATCH = r'''@echo off
setlocal DisableDelayedExpansion
set "PawchiveUpdateScript=%~f0"
set "PawchiveUpdateRoot=%~dp0"
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0updater.ps1"
if errorlevel 1 exit /b 1
rem PowerShell already validated and removed only the known stage files.
rem This final parsed command removes this batch file and its empty directory.
rem This owns a dedicated cmd /c process: exit it instead of returning into a
rem batch file which no longer exists (exit /b otherwise reports path errors).
cd /d "%SystemRoot%"
del /q "%PawchiveUpdateScript%" & rmdir "%PawchiveUpdateRoot%" 2>nul & exit 0
'''



def launch_updater(staged, target, pid):
    """Validate then launch a hidden, independent installer; caller exits safely."""
    if os.name != 'nt':
        raise UpdateError('软件内更新仅适用于 Windows EXE。')
    root, source = Path(staged.root), Path(staged.exe)
    if _owner(root) is None or source != root/'verified.exe' or not _TAG.fullmatch(staged.version):
        raise UpdateError('更新临时目录无效，请重新下载更新。')
    validate_exe(source)
    if not re.fullmatch(r'[a-fA-F0-9]{64}', staged.sha256) or _digest(source) != staged.sha256.lower():
        raise UpdateError('更新程序校验失败，请重新下载。')
    target = Path(target).absolute()
    if (not target.is_file() or target.suffix.lower() != '.exe' or target.is_symlink()
            or target.resolve() != target or source.resolve() == target.resolve()
            or any(ord(c) < 32 for c in str(target))):
        raise UpdateError('当前 EXE 路径不支持自动替换，请手动下载新版。')
    if getattr(target.stat(), 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_READONLY', 1):
        raise UpdateError('当前 EXE 为只读文件，请调整权限或手动更新。')
    for ancestor in (target.parent, *target.parents):
        if getattr(ancestor, 'is_junction', lambda: False)() or ancestor.is_symlink():
            raise UpdateError('当前程序位于目录链接中，请手动更新。')
    started = _process_start_filetime(pid)
    if int(pid) <= 0 or not started:
        raise UpdateError('无法确认当前程序进程，已停止自动更新。')
    try:
        with tempfile.NamedTemporaryFile(prefix='.pawchive-write-check-', dir=target.parent):
            pass
    except OSError as error:
        raise UpdateError('程序目录没有写入权限，请将软件移到可写目录后更新。') from error
    token = uuid.uuid4().hex
    config = dict(target=str(target), source=str(source), sha256=staged.sha256.lower(),
        temp_root=str(root.resolve().parent), stage_token=_owner(root)['token'],
        old_sha256=_digest(target), parent_pid=int(pid), parent_started=started,
        candidate=str(target.parent/('.pawchive-'+token+'.new.exe')),
        backup=str(target.parent/('.pawchive-'+token+'.backup.exe')))
    _write_json(root/'install.json', config)
    (root/'updater.ps1').write_text(_POWERSHELL, encoding='utf-8-sig')
    (root/'updater.bat').write_text(_BATCH, encoding='ascii', newline='\r\n')
    system = Path(os.environ.get('SystemRoot', 'C:/Windows'))/'System32'
    return subprocess.Popen([str(system/'cmd.exe'), '/d', '/c', 'updater.bat'], cwd=root,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True)
