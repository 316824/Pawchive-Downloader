"""Bounded GitHub release checks and checkpoint-safe, dual-track update UI.

Integration: create a footer QPushButton and call ``install_update_checker``
after the window layout exists. The returned controller must be closed from
the application's final-exit path (closing to the tray need not stop it).

GitHub's public latest-release endpoint needs no credentials and excludes
draft/prerelease releases: https://docs.github.com/en/rest/releases/releases#get-the-latest-release
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import total_ordering
import json
import re
import sys
import threading
import time
import weakref
import webbrowser
from html import escape
from urllib.parse import quote, unquote, urlsplit

import requests
from ui_i18n import t, language_for
from PySide6.QtCore import QByteArray, QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QTextDocument
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QProgressBar


CURRENT_VERSION = 'v1.4'
LATEST_RELEASE_API = 'https://api.github.com/repos/316824/Pawchive-Downloader/releases/latest'
RELEASE_ROOT = 'https://github.com/316824/Pawchive-Downloader/releases/tag/'
IGNORED_VERSION_KEY = 'update_checker.ignored_version'
REQUEST_TIMEOUT = 3
MAX_RESPONSE_BYTES = 512 * 1024
MAX_BODY_CHARS = 64_000
EMPTY_NOTES = '此版本暂无详细更新说明，请前往 GitHub 发布页面查看更新内容和下载文件。'
TRUNCATED_NOTES = '\n\n（更新说明较长，完整内容请在发布页面查看。）'

_VERSION = re.compile(
    r'^[vV]?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?'
    r'(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?'
    r'(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$', re.ASCII)


@total_ordering
@dataclass(frozen=True, eq=False)
class SemVersion:
    core: tuple[int, int, int]
    prerelease: tuple[str, ...] = ()

    @property
    def canonical(self):
        base = '.'.join(str(part) for part in self.core)
        return base + ('-' + '.'.join(self.prerelease) if self.prerelease else '')

    def __eq__(self, other):
        if not isinstance(other, SemVersion):
            return NotImplemented
        return self.core == other.core and self.prerelease == other.prerelease

    def __hash__(self):
        return hash((self.core, self.prerelease))

    def __lt__(self, other):
        if not isinstance(other, SemVersion):
            return NotImplemented
        if self.core != other.core:
            return self.core < other.core
        if not self.prerelease or not other.prerelease:
            return bool(self.prerelease) and not other.prerelease
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            if left.isdigit() and right.isdigit():
                return int(left) < int(right)
            if left.isdigit() != right.isdigit():
                return left.isdigit()
            return left < right
        return len(self.prerelease) < len(other.prerelease)


def parse_version(value):
    """Parse SemVer numerically; support the project's historical ``v1.3`` tag.

    Only a missing patch component is normalized to zero. Leading zeroes,
    arbitrary version suffixes and non-ASCII numbers are rejected. Build
    metadata is valid but does not affect version precedence.
    """
    if not isinstance(value, str) or len(value) > 128:
        return None
    text = value.strip()
    # Published desktop tags historically included the platform prefix.
    # Android tags intentionally do not participate in desktop comparisons.
    if text.lower().startswith('windows-'):
        text = text[8:]
    match = _VERSION.fullmatch(text)
    if match is None:
        return None
    prerelease = tuple(match[4].split('.')) if match[4] else ()
    if any(part.isdigit() and len(part) > 1 and part.startswith('0') for part in prerelease):
        return None
    return SemVersion(tuple(int(match[n] or '0') for n in (1, 2, 3)), prerelease)


def safe_release_url(value, tag=None):
    """Return only a concrete HTTPS release page in this exact official repo."""
    if (not isinstance(value, str) or len(value) > 1024 or
            any(ord(character) <= 32 for character in value)):
        return None
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.netloc.lower() != 'github.com'
                or parsed.query or parsed.fragment):
            return None
        parts = parsed.path.split('/')
        if (len(parts) != 6 or parts[1].lower() != '316824'
                or parts[2].lower() != 'pawchive-downloader'
                or parts[3:5] != ['releases', 'tag']):
            return None
        release_tag = unquote(parts[5])
        if parse_version(release_tag) is None or (tag is not None and release_tag != tag):
            return None
        return RELEASE_ROOT + quote(release_tag, safe='')
    except (ValueError, UnicodeError):
        return None


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int
    digest: str = ''


def safe_asset_url(value, tag, name):
    if not isinstance(value,str) or len(value)>2048 or any(ord(c)<=32 for c in value):return None
    if not isinstance(name,str) or not name or any(c in name for c in '/\\\x00\r\n') or name in {'.','..'}:return None
    try:
        url=urlsplit(value)
        expected='/316824/Pawchive-Downloader/releases/download/'+quote(tag,safe='')+'/'+quote(name,safe='')
        if (url.scheme!='https' or url.netloc.lower()!='github.com' or url.query or url.fragment
                or unquote(url.path)!=unquote(expected)):return None
        return 'https://github.com'+expected
    except (ValueError,UnicodeError):return None


@dataclass(frozen=True)
class ReleaseInfo:
    tag: str
    version: SemVersion
    name: str
    body: str
    url: str
    assets: tuple[ReleaseAsset, ...] = ()
    mandatory: bool = False


def requires_update(release, current):
    return bool(release.mandatory or '[MUST_UPDATE]' in release.body
                or release.version.core[:2] != current.core[:2])


def parse_release(payload):
    if not isinstance(payload, dict):
        return None
    tag = payload.get('tag_name')
    version = parse_version(tag)
    if (version is None or version.prerelease or payload.get('draft') is not False
            or payload.get('prerelease') is not False):
        return None
    url = safe_release_url(payload.get('html_url'), tag)
    if url is None:
        return None
    name = payload.get('name')
    name = name.strip()[:180] if isinstance(name, str) else ''
    body = payload.get('body')
    body = body.strip() if isinstance(body, str) else ''
    mandatory='[MUST_UPDATE]' in body
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + TRUNCATED_NOTES
    assets=[]
    for asset in payload.get('assets',[]) if isinstance(payload.get('assets'),list) else []:
        if not isinstance(asset,dict):continue
        asset_name=asset.get('name');asset_url=safe_asset_url(asset.get('browser_download_url'),tag,asset_name)
        size=asset.get('size')
        if asset_url is None or not isinstance(size,int) or isinstance(size,bool) or size<0:continue
        digest=asset.get('digest')
        assets.append(ReleaseAsset(asset_name,asset_url,size,digest if isinstance(digest,str) else ''))
    return ReleaseInfo(tag, version, name or tag, body or EMPTY_NOTES, url,tuple(assets),mandatory)


def fetch_latest_release(*, interrupted=None):
    """One bounded GET; any network/JSON failure is intentionally silent."""
    interrupted = interrupted or (lambda: False)
    response = None
    try:
        if interrupted():
            return None
        deadline = time.monotonic() + REQUEST_TIMEOUT
        response = requests.get(LATEST_RELEASE_API, timeout=REQUEST_TIMEOUT,
            stream=True, allow_redirects=False, headers={
                'Accept': 'application/vnd.github+json',
                'X-GitHub-Api-Version': '2026-03-10',
                'User-Agent': 'PawchiveDownloader/1.4 (release-check)',
            })
        if response.status_code != 200 or interrupted():
            return None
        content_type = str(response.headers.get('Content-Type', '')).split(';', 1)[0].strip().lower()
        if content_type not in ('application/json', 'application/vnd.github+json'):
            return None
        length = response.headers.get('Content-Length')
        if length is not None and not 0 <= int(length) <= MAX_RESPONSE_BYTES:
            return None
        chunks, received = [], 0
        for chunk in response.iter_content(chunk_size=8192):
            if interrupted() or time.monotonic() > deadline:
                return None
            if not chunk:
                continue
            received += len(chunk)
            if received > MAX_RESPONSE_BYTES:
                return None
            chunks.append(chunk)
        if interrupted():
            return None
        payload = json.loads(b''.join(chunks).decode('utf-8'))
        return parse_release(payload)
    except (requests.RequestException, OSError, ValueError, TypeError, OverflowError, RecursionError):
        return None
    finally:
        if response is not None:
            response.close()


class ReleaseCheckWorker(QObject):
    """A daemon HTTP worker with Qt signals, and no native-QThread exit hazard.

    A request may still be in its socket timeout when the user closes the app.
    Keeping network I/O in a daemon Python thread lets final exit stay prompt;
    it never owns widgets or application state and only emits immutable data.
    """
    result = Signal(object)
    finished = Signal()

    def __init__(self):
        super().__init__()
        self._stop = threading.Event()
        self._thread = None
        self.finished.connect(self.deleteLater, Qt.ConnectionType.QueuedConnection)

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self.run, name='PawchiveReleaseCheck', daemon=True)
        self._thread.start()

    def run(self):
        try:
            release = fetch_latest_release(interrupted=self._stop.is_set)
            if not self._stop.is_set():
                self.result.emit(release)
        finally:
            self.finished.emit()

    def requestInterruption(self):
        self._stop.set()

    def isRunning(self):
        return bool(self._thread and self._thread.is_alive())

    def wait(self, milliseconds=3000):
        if self._thread is not None:
            self._thread.join(max(0, milliseconds) / 1000)
        return not self.isRunning()


class ReleaseNotesBrowser(QTextBrowser):
    """Render Markdown without automatically reading network OR local resources."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.setReadOnly(True)

    def loadResource(self, resource_type, name):
        # This includes file://, UNC paths, remote images, CSS and data: URIs.
        # Do not call the base implementation for an untrusted release body.
        return QByteArray()


def _theme_tokens(window):
    tokens=dict(card='#191f2a',field='#101722',text='#edf3ff',muted='#a7b6d0',
                line='#303b50',glow='#ad9bfa',button='#a6bbff',button_text='#10213b')
    palette=getattr(window,'palette_tokens',None)
    if isinstance(palette,dict):
        tokens.update({key:palette[key] for key in tokens if key in palette})
    return tokens


class UpdateDialog(QDialog):
    ignored = Signal(str)
    update_requested = Signal()
    exit_requested = Signal()

    def __init__(self, parent, release, *, mandatory=False):
        super().__init__(parent)
        self.release=release;self.mandatory=bool(mandatory);self._allow_close=False
        self.lang=language_for(parent)
        self.setWindowTitle(t(self.lang,'必须更新 Pawchive' if mandatory else '发现 Pawchive 新版本'))
        self.resize(650,520)
        self.setWindowModality(Qt.WindowModality.ApplicationModal if mandatory else Qt.WindowModality.WindowModal)
        layout=QVBoxLayout(self);layout.setContentsMargins(22,22,22,22)
        heading=QLabel(f'Pawchive {release.tag}');heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setWordWrap(True);heading.setStyleSheet('font-size:21px;font-weight:600');layout.addWidget(heading)
        title=QLabel(release.name);title.setTextFormat(Qt.TextFormat.PlainText);title.setWordWrap(True);layout.addWidget(title)
        self.notes=ReleaseNotesBrowser(self)
        body=t(self.lang,release.body) if release.body==EMPTY_NOTES else release.body
        if body.endswith(TRUNCATED_NOTES):
            body=body[:-len(TRUNCATED_NOTES)]+t(self.lang,TRUNCATED_NOTES)
        self.notes.document().setMarkdown(body,QTextDocument.MarkdownFeature.MarkdownDialectGitHub |
            QTextDocument.MarkdownFeature.MarkdownNoHTML)
        self.notes.anchorClicked.connect(self._open_notes_link);layout.addWidget(self.notes,1)
        colors=_theme_tokens(parent)
        self.notes.setStyleSheet('QTextBrowser {background:'+colors['field']+';color:'+colors['text']+';'
            'border:1px solid '+colors['line']+';border-radius:11px;padding:10px;}')
        self.status=QLabel();self.status.setWordWrap(True);self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.progress=QProgressBar();self.progress.setRange(0,1000);self.progress.hide();layout.addWidget(self.progress)
        self.progress.setStyleSheet('QProgressBar {background:'+colors['field']+';color:'+colors['text']+';'
            'border:1px solid '+colors['line']+';border-radius:7px;text-align:center;min-height:16px;}'
            'QProgressBar::chunk {background:'+colors['button']+';border-radius:6px;}')
        self.fallback_link=QLabel(self)
        self.fallback_link.setTextFormat(Qt.TextFormat.RichText)
        self.fallback_link.setOpenExternalLinks(False)
        self.fallback_link.setText('<a href="'+escape(release.url,quote=True)+'" style="color:'+
            escape(colors['glow'],quote=True)+'">'+escape(t(self.lang,'前往 GitHub 下载'))+'</a>')
        self.fallback_link.linkActivated.connect(self._download)
        self.fallback_link.hide();layout.addWidget(self.fallback_link)
        row=QHBoxLayout();self.ignore_button=None;self.later_button=None
        if self.mandatory:
            self.status.setText(t(self.lang,'此版本需要更新后才能继续使用，请下载新版本或退出软件。'))
            self.download_button=QPushButton(t(self.lang,'前往 GitHub 下载'))
            self.exit_button=QPushButton(t(self.lang,'退出软件'))
            self.download_button.clicked.connect(self._download);self.exit_button.clicked.connect(self.exit_requested)
            row.addStretch(1);row.addWidget(self.download_button);row.addWidget(self.exit_button)
        else:
            self.ignore_button=QPushButton(t(self.lang,'忽略此版本'));self.later_button=QPushButton(t(self.lang,'稍后再说'))
            self.download_button=QPushButton(t(self.lang,'立即更新'));self.download_button.setObjectName('primaryButton')
            row.addWidget(self.ignore_button);row.addStretch(1);row.addWidget(self.later_button);row.addWidget(self.download_button)
            self.ignore_button.clicked.connect(self._ignore);self.later_button.clicked.connect(self.reject)
            self.download_button.clicked.connect(self.update_requested)
        layout.addLayout(row)
        from window_chrome import install_window_chrome
        chrome=install_window_chrome(self,controls='close')
        if self.mandatory:chrome.close.hide()

    def reject(self):
        if not self.mandatory or self._allow_close:super().reject()

    def accept(self):
        if not self.mandatory or self._allow_close:super().accept()

    def closeEvent(self,event):
        if self.mandatory and not self._allow_close:event.ignore();return
        super().closeEvent(event)

    def keyPressEvent(self,event):
        if self.mandatory and event.key()==Qt.Key.Key_Escape:event.accept();return
        super().keyPressEvent(event)

    def force_close(self):
        self._allow_close=True;super().reject()

    @Slot()
    def _ignore(self):
        self.ignored.emit(self.release.version.canonical);self.accept()

    @Slot()
    def _download(self):
        url=safe_release_url(self.release.url,self.release.tag)
        if url:webbrowser.open(url)

    @Slot(QUrl)
    def _open_notes_link(self,url):
        safe=safe_release_url(url.toString())
        if safe:webbrowser.open(safe)

    def downloading(self,current,total):
        self.fallback_link.hide()
        self.progress.show()
        if total>0:self.progress.setRange(0,1000);self.progress.setValue(min(1000,int(current*1000/total)))
        else:self.progress.setRange(0,0)
        self.status.setText(t(self.lang,'正在下载更新：{current} / {total} MB',
            current=f'{current/1048576:.1f}',total=f'{total/1048576:.1f}' if total else '?'))

    def set_update_busy(self,busy):
        self.download_button.setEnabled(not busy)
        if self.ignore_button:self.ignore_button.setEnabled(not busy)
        if self.later_button:self.later_button.setText(t(self.lang,'取消下载' if busy else '稍后再说'))

    def show_failure(self,message):
        self.status.setText(str(message));self.progress.hide();self.fallback_link.show()


class UpdateController(QObject):
    update_available=Signal(object)
    availability_changed=Signal(bool)

    def __init__(self,window,store=None,*,current_version=CURRENT_VERSION):
        super().__init__(window)
        self._window_ref=weakref.ref(window);self.store=store
        self.current_version=parse_version(current_version);self.release=None;self.worker=None
        self._dialog=None;self._closed=False;self._started=False;self._checking=False;self._manual=False
        self._auto_attempts=0;self._ignored=self._get_ignored()
        self._ota_worker=None;self._ota_dialog=None;self._ota_handoff=False;self._install_pending=False
        self._staged_update=None
        self._retry_timer=QTimer(self);self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._retry_auto)
        self._toast=None

    @property
    def window(self):return self._window_ref()

    def refresh_language(self):
        language=language_for(self.window)
        button=getattr(self,'footer_button',None)
        if button is not None:
            button.setToolTip(t(language,'查看新版本的更新说明和下载页面'))
            if self.release is not None:button.setText(t(language,'● 发现新版本 {tag}',tag=self.release.tag))
        manual=getattr(self,'manual_button',None)
        if manual is not None:
            manual.setText(t(language,'正在检查...' if self._checking else '检查更新'))
            manual.setEnabled(not self._checking and not self._closed
                and self._ota_worker is None and not self._install_pending)

    def _get_ignored(self):
        settings=getattr(self.window,'settings',None)
        try:
            raw=settings.value(IGNORED_VERSION_KEY,'',type=str) if settings is not None else ''
            if not raw and self.store is not None:raw=self.store.get_setting(IGNORED_VERSION_KEY,'')
            value=parse_version(raw);return value.canonical if value else ''
        except (OSError,RuntimeError,ValueError,TypeError):return ''

    @Slot()
    def start(self):
        if self._started:return
        self._started=True;self._begin_check(False)

    @Slot()
    def manual_check(self):
        if self._closed or self._checking or self._install_pending or self._ota_worker is not None:return
        timer=getattr(self,'startup_timer',None)
        if timer is not None:timer.stop()
        self._started=True;self._retry_timer.stop();self._begin_check(True)

    @Slot()
    def _retry_auto(self):self._begin_check(False)

    def _begin_check(self,manual):
        if self._closed or self._checking or self.current_version is None or self._ota_worker is not None or self._install_pending:return
        self._checking=True;self._manual=bool(manual)
        if not manual:self._auto_attempts+=1
        self.refresh_language()
        self.worker=ReleaseCheckWorker()
        self.worker.result.connect(self._received,Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self._worker_finished,Qt.ConnectionType.QueuedConnection)
        try:self.worker.start()
        except RuntimeError:
            worker=self.worker
            self._received(None);self._worker_finished()
            worker.deleteLater()

    @Slot(object)
    def _received(self,release):
        if self._closed or self.current_version is None or self._install_pending or self._ota_worker is not None:return
        if not isinstance(release,ReleaseInfo):
            if self._manual:self.toast(t(language_for(self.window),'暂时无法检查更新，请确认网络后重试。'))
            elif self._auto_attempts<3:self._retry_timer.start(30000*self._auto_attempts)
            return
        self._retry_timer.stop()
        if release.version<=self.current_version:
            if self._manual:self.toast(t(language_for(self.window),'当前已是最新版本 {version}',version=CURRENT_VERSION if self.current_version==parse_version(CURRENT_VERSION) else 'v'+self.current_version.canonical))
            return
        mandatory=requires_update(release,self.current_version)
        ignored=parse_version(self._ignored)
        if not mandatory and ignored is not None and release.version<=ignored and not self._manual:return
        if self._staged_update is not None and parse_version(self._staged_update.version)!=release.version:
            self._discard_staged()
        if self._dialog is not None and (self._dialog.release!=release or
                self._dialog.mandatory!=mandatory):
            # A new response can change the selected version while notes are
            # open. Never display old notes with the new package's actions.
            self._dialog.force_close()
        self.release=release;self.update_available.emit(release);self.availability_changed.emit(True)
        if mandatory:
            gate=getattr(self.window,'set_update_required',None)
            if callable(gate):gate(True)
        if mandatory or self._manual:self.show_dialog()

    @Slot()
    def _worker_finished(self):
        self.worker=None;self._checking=False;self._manual=False
        if not self._closed:self.refresh_language()

    @Slot()
    def show_dialog(self):
        if self._closed or self.release is None or self.window is None:return
        if self._dialog is not None:
            self._dialog.raise_();self._dialog.activateWindow();return
        mandatory=requires_update(self.release,self.current_version)
        self._dialog=UpdateDialog(self.window,self.release,mandatory=mandatory)
        self._dialog.ignored.connect(self.ignore_version)
        self._dialog.update_requested.connect(self._start_update)
        self._dialog.exit_requested.connect(self._exit_required)
        self._dialog.finished.connect(self._dialog_finished)
        if self._ota_worker is not None and not mandatory:
            self._dialog.set_update_busy(True)
            self._dialog.status.setText(t(self._dialog.lang,'正在取消上次更新下载，请稍候…'))
        # QDialog.open() changes modality to WindowModal. Mandatory updates
        # must also block already-open sibling tool windows.
        if mandatory:self._dialog.show()
        else:self._dialog.open()

    @Slot()
    def _exit_required(self):
        exit_request=getattr(self.window,'request_update_exit',None)
        if callable(exit_request):exit_request()
        else:
            from PySide6.QtWidgets import QApplication
            QApplication.quit()

    @Slot(int)
    def _dialog_finished(self,result):
        dialog,self._dialog=self._dialog,None
        if self._ota_worker is not None and not self._ota_handoff:self._ota_worker.requestInterruption()
        if dialog is not None:dialog.deleteLater()

    @Slot(str)
    def ignore_version(self,version):
        parsed=parse_version(version)
        if (parsed is None or self.release is None or parsed!=self.release.version
                or requires_update(self.release,self.current_version)):return
        self._ignored=parsed.canonical
        try:
            settings=getattr(self.window,'settings',None)
            if settings is not None:settings.setValue(IGNORED_VERSION_KEY,self._ignored);settings.sync()
            if self.store is not None:self.store.set_setting(IGNORED_VERSION_KEY,self._ignored)
        except (OSError,RuntimeError,ValueError,TypeError):pass
        self._discard_staged()
        self.release=None;self.availability_changed.emit(False)

    def _discard_staged(self):
        staged,self._staged_update=self._staged_update,None
        if staged is not None:self._cleanup_stage(staged)

    @staticmethod
    def _cleanup_stage(staged):
        try:
            from ota_update import cleanup_staged
            cleanup_staged(staged)
        except (OSError,ValueError,RuntimeError):
            # Cleanup failure must not break a Qt slot or a future update.
            # The bounded stale sweep can retry an owned orphan later.
            pass

    def toast(self,message):
        window=self.window
        if window is None or self._closed:return
        if self._toast is not None:self._toast.deleteLater()
        label=QLabel(message,window);label.setObjectName('updateToast');label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,True)
        colors=_theme_tokens(window)
        label.setStyleSheet('QLabel#updateToast {background:'+colors['card']+';color:'+colors['text']+';'
            'border:1px solid '+colors['glow']+';border-radius:12px;padding:12px 16px;}')
        label.setMaximumWidth(max(220,min(480,window.width()-40)));label.adjustSize()
        label.move(max(12,(window.width()-label.width())//2),max(12,window.height()-label.height()-42))
        label.show();label.raise_();self._toast=label
        timer=QTimer(label);timer.setSingleShot(True);timer.timeout.connect(label.hide);timer.start(5000)

    @Slot()
    def _start_update(self):
        if self._closed or self._ota_worker is not None or self._install_pending or self._dialog is None:return
        dialog=self._dialog
        if self.release is None or dialog.mandatory or dialog.release!=self.release:return
        if not getattr(sys,'frozen',False):
            dialog.show_failure(t(dialog.lang,'当前为脚本运行模式，请前往 GitHub 下载完整 EXE。'));return
        self._retry_timer.stop()
        if self._staged_update is not None and parse_version(self._staged_update.version)!=self.release.version:
            self._discard_staged()
        if self._staged_update is not None:
            self._ota_dialog=dialog
            self._update_ready(self._staged_update)
            return
        try:
            from ota_update import OtaDownloadWorker
            worker=OtaDownloadWorker(self.release)
        except (ImportError,OSError,ValueError,RuntimeError) as error:
            dialog.show_failure(error);return
        self._ota_worker=worker;self._ota_dialog=dialog;self._ota_handoff=False
        dialog.set_update_busy(True);dialog.downloading(0,0)
        worker.progress.connect(self._update_progress,Qt.ConnectionType.QueuedConnection)
        worker.ready.connect(self._update_ready,Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._update_failed,Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._update_finished,Qt.ConnectionType.QueuedConnection)
        self.refresh_language()
        worker.start()

    @Slot(object,object)
    def _update_progress(self,current,total):
        if not self._closed and self._dialog is self._ota_dialog and self._dialog is not None:
            self._dialog.downloading(current,total)

    @Slot(object)
    def _update_ready(self,staged):
        if self._closed or self._dialog is None or self._dialog is not self._ota_dialog:
            self._cleanup_stage(staged);return
        if (self.release is None or self._dialog.release!=self.release or
                parse_version(getattr(staged,'version',None))!=self.release.version):
            self._cleanup_stage(staged)
            self._update_failed(t(language_for(self.window),'更新版本已变化，请重新检查更新。'))
            return
        install=getattr(self.window,'request_update_install',None)
        if not callable(install):
            self._cleanup_stage(staged);self._update_failed(t(language_for(self.window),'无法启动更新，请前往 GitHub 下载。'));return
        self._ota_handoff=True;self._install_pending=True
        self._staged_update=staged
        self._dialog.status.setText(t(self._dialog.lang,'正在保存任务并重启…'))
        self._dialog.set_update_busy(True)
        if self._dialog.later_button:self._dialog.later_button.setEnabled(False)
        self.refresh_language()
        try:accepted=install(staged)
        except (OSError,ValueError,RuntimeError) as error:
            accepted=False;self.install_failed(str(error))
        if accepted is False:
            self._ota_handoff=False;self._install_pending=False;self._staged_update=None;self._cleanup_stage(staged)
            self.install_failed(t(language_for(self.window),'无法启动更新，请前往 GitHub 下载。'))

    @Slot(str)
    def _update_failed(self,message):
        if not self._closed and self._dialog is self._ota_dialog and self._dialog is not None:
            self._dialog.show_failure(message);self._dialog.set_update_busy(False)

    def install_failed(self,message):
        self._install_pending=False;self._ota_handoff=False
        if self._dialog is not None:
            self._dialog.show_failure(message);self._dialog.set_update_busy(False)
            if self._dialog.later_button:self._dialog.later_button.setEnabled(True)
        self.refresh_language()

    @Slot()
    def _update_finished(self):
        worker,self._ota_worker=self._ota_worker,None
        if worker is not None:worker.deleteLater()
        if not self._closed and not self._install_pending and self._dialog is not None:
            self._dialog.set_update_busy(False)
        if not self._closed:self.refresh_language()

    @Slot()
    def close(self):
        self._closed=True;self._retry_timer.stop()
        timer=getattr(self,'startup_timer',None)
        if timer is not None:timer.stop()
        if self.worker is not None:self.worker.requestInterruption()
        if self._ota_worker is not None and not self._ota_handoff:self._ota_worker.requestInterruption()
        if not self._ota_handoff:self._discard_staged()
        if self._dialog is not None:self._dialog.force_close()


def install_update_checker(window,footer_button=None,store=None,*,current_version=CURRENT_VERSION,
                           delay_ms=1800,manual_button=None):
    existing=getattr(window,'update_controller',None)
    if isinstance(existing,UpdateController):return existing
    if store is None:store=getattr(getattr(window,'services',None),'store',None)
    controller=UpdateController(window,store,current_version=current_version);window.update_controller=controller
    if footer_button is not None:
        controller.footer_button=footer_button;footer_button.hide()
        footer_button.setCursor(Qt.CursorShape.PointingHandCursor)
        footer_button.setStyleSheet('QPushButton {color:#f18491;background:transparent;border:0;padding:4px 7px;}'
            'QPushButton:hover {color:#ffa5af;}')
        footer_button.clicked.connect(controller.show_dialog)
        controller.update_available.connect(lambda _:controller.refresh_language())
        controller.availability_changed.connect(footer_button.setVisible)
    if manual_button is not None:
        controller.manual_button=manual_button;manual_button.clicked.connect(controller.manual_check)
        manual_button.setCursor(Qt.CursorShape.PointingHandCursor)
    controller.refresh_language()
    timer=QTimer(controller);timer.setSingleShot(True);timer.timeout.connect(controller.start)
    if delay_ms is not None:timer.start(max(0,int(delay_ms)))
    controller.startup_timer=timer
    return controller
