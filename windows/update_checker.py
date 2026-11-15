"""Optional, read-only GitHub release checks and a safe release-notes dialog.

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
import threading
import time
import weakref
from urllib.parse import quote, unquote, urlsplit

import requests
from ui_i18n import t, language_for
from PySide6.QtCore import QByteArray, QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QTextDocument
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout


CURRENT_VERSION = 'v1.3'
LATEST_RELEASE_API = 'https://api.github.com/repos/316824/Pawchive-Downloader/releases/latest'
RELEASE_ROOT = 'https://github.com/316824/Pawchive-Downloader/releases/tag/'
IGNORED_VERSION_KEY = 'update_checker.ignored_version'
REQUEST_TIMEOUT = 3
MAX_RESPONSE_BYTES = 512 * 1024
MAX_BODY_CHARS = 64_000
EMPTY_NOTES = '此版本暂无详细更新说明，请前往 GitHub 发布页面查看更新内容和下载文件。'

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
    match = _VERSION.fullmatch(value.strip())
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
class ReleaseInfo:
    tag: str
    version: SemVersion
    name: str
    body: str
    url: str


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
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + '\n\n（更新说明较长，完整内容请在发布页面查看。）'
    return ReleaseInfo(tag, version, name or tag, body or EMPTY_NOTES, url)


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
                'User-Agent': 'PawchiveDownloader/1.3 (release-check)',
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
            if release is not None and not self._stop.is_set():
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


class UpdateDialog(QDialog):
    ignored = Signal(str)

    def __init__(self, parent, release):
        super().__init__(parent)
        self.release = release
        self.lang=language_for(parent)
        self.setWindowTitle(t(self.lang,'发现 Pawchive 新版本'))
        self.resize(650, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        heading = QLabel(f'Pawchive {release.tag}')
        heading.setTextFormat(Qt.TextFormat.PlainText)
        heading.setWordWrap(True)
        heading.setStyleSheet('font-size:21px;font-weight:600')
        layout.addWidget(heading)
        title = QLabel(release.name)
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        layout.addWidget(title)
        self.notes = ReleaseNotesBrowser(self)
        body=t(self.lang, release.body) if release.body==EMPTY_NOTES else release.body
        suffix='\n\n（更新说明较长，其余内容请在发布页面查看。）'
        if body.endswith(suffix): body=body[:-len(suffix)]+t(self.lang,suffix)
        self.notes.document().setMarkdown(body,
            QTextDocument.MarkdownFeature.MarkdownDialectGitHub |
            QTextDocument.MarkdownFeature.MarkdownNoHTML)
        self.notes.anchorClicked.connect(self._open_notes_link)
        layout.addWidget(self.notes, 1)
        row = QHBoxLayout()
        self.ignore_button = QPushButton(t(self.lang,'忽略该版本'))
        self.later_button = QPushButton(t(self.lang,'稍后再说'))
        self.download_button = QPushButton(t(self.lang,'前往下载'))
        row.addWidget(self.ignore_button)
        row.addStretch(1)
        row.addWidget(self.later_button)
        row.addWidget(self.download_button)
        layout.addLayout(row)
        self.ignore_button.clicked.connect(self._ignore)
        self.later_button.clicked.connect(self.reject)
        self.download_button.clicked.connect(self._download)

    @Slot()
    def _ignore(self):
        self.ignored.emit(self.release.version.canonical)
        self.accept()

    @Slot()
    def _download(self):
        # Revalidate at the action boundary, even when constructed by callers
        # other than the normal validated GitHub response path.
        url = safe_release_url(self.release.url, self.release.tag)
        if url is not None:
            QDesktopServices.openUrl(QUrl(url))

    @Slot(QUrl)
    def _open_notes_link(self, url):
        safe = safe_release_url(url.toString())
        if safe is not None:
            QDesktopServices.openUrl(QUrl(safe))


class UpdateController(QObject):
    update_available = Signal(object)
    availability_changed = Signal(bool)

    def __init__(self, window, store=None, *, current_version=CURRENT_VERSION):
        super().__init__(window)
        self._window_ref = weakref.ref(window)
        self.store = store
        self.current_version = parse_version(current_version)
        self.release = None
        self.worker = None
        self._dialog = None
        self._closed = False
        self._started = False
        self._ignored = self._get_ignored()

    @property
    def window(self):
        return self._window_ref()

    def refresh_language(self):
        button=getattr(self,'footer_button',None)
        if button is not None:
            language=language_for(self.window)
            button.setToolTip(t(language,'查看新版本的更新说明和下载页面'))
            if self.release is not None:
                button.setText(t(language,'● 发现新版本 {tag}',tag=self.release.tag))

    def _get_ignored(self):
        if self.store is not None:
            try:
                version = parse_version(self.store.get_setting(IGNORED_VERSION_KEY, ''))
                return version.canonical if version else ''
            except (OSError, RuntimeError, ValueError):
                pass
        return ''

    @Slot()
    def start(self):
        if self._closed or self._started or self.current_version is None:
            return
        self._started = True
        self.worker = ReleaseCheckWorker()
        self.worker.result.connect(self._received, Qt.ConnectionType.QueuedConnection)
        self.worker.finished.connect(self._worker_finished, Qt.ConnectionType.QueuedConnection)
        self.worker.start()

    @Slot(object)
    def _received(self, release):
        if (self._closed or not isinstance(release, ReleaseInfo)
                or self.current_version is None or release.version <= self.current_version
                or release.version.canonical == self._ignored):
            return
        self.release = release
        self.update_available.emit(release)
        self.availability_changed.emit(True)

    @Slot()
    def _worker_finished(self):
        self.worker = None

    @Slot()
    def show_dialog(self):
        if self._closed or self.release is None or self.window is None:
            return
        if self._dialog is not None:
            self._dialog.raise_()
            self._dialog.activateWindow()
            return
        self._dialog = UpdateDialog(self.window, self.release)
        self._dialog.ignored.connect(self.ignore_version)
        self._dialog.finished.connect(self._dialog_finished)
        # Window-modal but nonblocking: downloads and timer paints continue.
        self._dialog.open()

    @Slot(int)
    def _dialog_finished(self, result):
        dialog, self._dialog = self._dialog, None
        if dialog is not None:
            dialog.deleteLater()

    @Slot(str)
    def ignore_version(self, version):
        parsed = parse_version(version)
        if parsed is None or self.release is None or parsed != self.release.version:
            return
        self._ignored = parsed.canonical
        if self.store is not None:
            try:
                self.store.set_setting(IGNORED_VERSION_KEY, self._ignored)
            except (OSError, RuntimeError, ValueError):
                pass
        self.release = None
        self.availability_changed.emit(False)

    @Slot()
    def close(self):
        self._closed = True
        timer = getattr(self, 'startup_timer', None)
        if timer is not None:
            timer.stop()
        if self.worker is not None:
            self.worker.requestInterruption()
        if self._dialog is not None:
            self._dialog.reject()


def install_update_checker(window, footer_button=None, store=None, *,
                           current_version=CURRENT_VERSION, delay_ms=1800):
    """Install once; display a clickable red-dot footer only for newer releases.

    ``store`` should be the existing AppState instance (window.services.store).
    No extra database, Cookie, account token or automatic download is used.
    """
    existing = getattr(window, 'update_controller', None)
    if isinstance(existing, UpdateController):
        return existing
    if store is None:
        store = getattr(getattr(window, 'services', None), 'store', None)
    controller = UpdateController(window, store, current_version=current_version)
    window.update_controller = controller
    if footer_button is not None:
        controller.footer_button=footer_button
        footer_button.hide()
        footer_button.setCursor(Qt.CursorShape.PointingHandCursor)
        controller.refresh_language()
        footer_button.setStyleSheet('QPushButton {color:#f18491;background:transparent;'
            'border:0;padding:4px 7px;} QPushButton:hover {color:#ffa5af;}')
        footer_button.clicked.connect(controller.show_dialog)

        def update_button(release):
            controller.refresh_language()

        controller.update_available.connect(update_button)
        controller.availability_changed.connect(footer_button.setVisible)
    # A timer owned by the controller cancels naturally if the window closes
    # before the delayed request starts; singleShot(functor) would retain it.
    timer = QTimer(controller)
    timer.setSingleShot(True)
    timer.timeout.connect(controller.start)
    if delay_ms is not None:
        timer.start(max(0, int(delay_ms)))
    controller.startup_timer = timer
    return controller
