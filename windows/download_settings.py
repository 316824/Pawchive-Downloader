"""Download preferences and bounded Windows destinations, independent of UI layout."""
import os
from pathlib import Path
import re
import tempfile
import unicodedata

import requests
from bs4 import BeautifulSoup
from PySide6.QtCore import QThread, Signal

CHECKBOX_SETTINGS = {
    'artist_folder_check': ('download/artist_folder', True),
    'title_check': ('download/title_folders', True),
    'select_posts_check': ('download/preselect', True),
    'rename_check': ('download/number_images', True),
    'default_path_check': ('download/default_path_enabled', False),
}
INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f\ud800-\udfff]')
RESERVED_NAME = re.compile(r'^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', re.I)


def sanitize_artist_name(name, fallback='Unknown Artist', limit=50):
    """Return one Unicode Windows component; never encode a filename as bytes."""
    value = INVALID_NAME.sub('_', unicodedata.normalize('NFC', str(name or ''))).strip(' .')
    if not value:
        value = INVALID_NAME.sub('_', str(fallback)).strip(' .') or 'Unknown Artist'
    if RESERVED_NAME.match(value):
        value = '_' + value
    return value[:max(1, min(50, int(limit)))].rstrip(' .') or '_'


def writable_directory(path):
    try:
        return bool(path and Path(path).is_dir() and os.access(path, os.W_OK))
    except (OSError, ValueError, RuntimeError, TypeError):
        return False


def browse_directory(settings, current='', *, fallbacks=None):
    """Recover a deleted history path by walking parents, then local fallbacks."""
    if fallbacks is None:
        home = Path.home()
        fallbacks = [home/'Desktop', home, Path(tempfile.gettempdir()), Path(home.anchor)]
    candidates = [settings.value('last_download_path', '', type=str), current, *fallbacks]
    visited = set()
    for value in candidates:
        if not str(value or '').strip() or any(ord(char)<32 for char in str(value)):
            continue
        try:
            path = Path(os.path.abspath(os.path.expanduser(str(value))))
            while path not in visited:
                visited.add(path)
                if writable_directory(path):
                    return str(path)
                if path.parent == path:
                    break
                path = path.parent
        except (OSError, ValueError, RuntimeError):
            continue
    # QFileDialog accepts an empty initial directory and chooses its own safe
    # location if even the local home/temp roots are unavailable.
    return ''


def install_preferences(window):
    settings = window.settings
    for attribute, (key, default) in CHECKBOX_SETTINGS.items():
        if attribute == 'default_path_check' and not settings.contains(key):
            default = bool(settings.value('default_save_path', '', type=str))
        check = getattr(window, attribute)
        blocked = check.blockSignals(True)
        check.setChecked(settings.value(key, default, type=bool))
        check.blockSignals(blocked)
        check.toggled.connect(lambda checked, k=key: settings.setValue(k, bool(checked)))
    if not window.default_path_check.isChecked():
        window.path.clear()
    window.order_combo.setCurrentIndex(1 if settings.value('download/order', 'newest', type=str) == 'oldest' else 0)
    window.content_combo.setCurrentIndex(max(0, window.content_combo.findData(
        settings.value('download/content', 'all', type=str))))
    window.order_combo.currentIndexChanged.connect(lambda index: settings.setValue('download/order', 'oldest' if index == 1 else 'newest'))
    window.content_combo.currentIndexChanged.connect(lambda _: settings.setValue('download/content', window.content_combo.currentData()))


def artist_name_from_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    node = soup.select_one("a.user-header__profile [itemprop='name'], .user-header [itemprop='name']")
    return node.get_text(' ', strip=True)[:160] if node else ''


def fetch_artist_name(profile, interrupted=lambda: False):
    from artist_cards import normalize_profile
    profile = normalize_profile(profile)
    if not profile:
        raise ValueError('invalid_profile')
    with requests.Session() as session:
        session.headers['User-Agent'] = 'PawchiveDownloader/1.3'
        with session.get(profile, timeout=(8, 20), stream=True) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            for chunk in response.iter_content(64*1024):
                if interrupted():
                    raise OSError('cancelled')
                size += len(chunk)
                if size > 2*1024*1024:
                    break
                chunks.append(chunk)
            return artist_name_from_html(b''.join(chunks).decode('utf-8', 'replace'))


def create_destination(base, profile, artist_folder, artist_name='', *, interrupted=lambda: False):
    base = Path(os.path.abspath(os.path.expanduser(str(base))))
    if not writable_directory(base):
        raise OSError('download_folder_unavailable')
    destination = base
    if artist_folder:
        if not artist_name or artist_name == 'Unknown Artist':
            artist_name = fetch_artist_name(profile, interrupted)
        fallback = 'Unknown Artist ' + str(profile).rstrip('/').rsplit('/', 1)[-1]
        component = sanitize_artist_name(artist_name, fallback)
        # Keep a modest component and leave space for the existing album/file
        # layout. Report exceptionally deep roots instead of failing in mkdir.
        if len(str(base/component).encode('utf-16-le'))//2 > 210:
            raise OSError('download_folder_too_long')
        destination = base/component
        if destination.is_symlink() or getattr(destination, 'is_junction', lambda: False)():
            raise OSError('download_folder_unavailable')
    if interrupted():
        raise OSError('cancelled')
    destination.mkdir(exist_ok=True)
    if artist_folder and destination.resolve().parent != base.resolve():
        raise OSError('download_folder_unavailable')
    # os.access is useful for the chooser but not authoritative under Windows
    # ACLs. Verify actual writes off the GUI thread, then leave no probe behind.
    with tempfile.TemporaryFile(dir=destination, prefix='.pawchive-write-'):
        pass
    return {'folder': str(destination), 'base_folder': str(base),
            'artist_folder': bool(artist_folder), 'artist_name': artist_name}


class DestinationPreparationWorker(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, base, profile, artist_folder, artist_name='', parent=None):
        super().__init__(parent)
        self.args = base, profile, artist_folder, artist_name

    def run(self):
        try:
            result = create_destination(*self.args, interrupted=self.isInterruptionRequested)
            if self.isInterruptionRequested():
                raise OSError('cancelled')
            self.result.emit(result)
        except Exception as error:
            self.failed.emit(str(error))
