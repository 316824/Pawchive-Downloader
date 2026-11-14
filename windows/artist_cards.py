"""Per-artist achievement cards and bounded avatar fetching.

Artist card data is deliberately small and stored in the existing settings
record.  Downloaded avatar bytes are kept in a dedicated cache directory and
are never used as a path supplied by a web page.
"""
from __future__ import annotations

import hashlib
import math
from contextlib import nullcontext
from functools import wraps
import re
from pathlib import Path
from urllib.parse import urlparse

import requests
from PySide6.QtCore import QObject, QThread, Signal, Slot, QTimer


PROFILE_RE = re.compile(
    r"^https://pawchive\.pw/(fanbox|patreon|subscribestar|discord|boosty)/user/([^/?#]+)$",
    re.IGNORECASE,
)
ACHIEVEMENTS = (
    ("first_visit", 1),
    ("collector", 5),
    ("curator", 10),
    ("master", 25),
)
MAX_AVATAR_BYTES = 4 * 1024 * 1024
ARTIST_ID_RE = re.compile(r"/user/([^/?#]+)", re.IGNORECASE)


def normalize_profile(value: str) -> str:
    match = PROFILE_RE.fullmatch(str(value or '').strip().rstrip('/'))
    if not match:
        return ''
    return f"https://pawchive.pw/{match.group(1).lower()}/user/{match.group(2)}"


def avatar_url(profile: str) -> str:
    normalized = normalize_profile(profile)
    if not normalized:
        return ''
    parsed = urlparse(normalized)
    source, _, artist_id = parsed.path.strip('/').split('/', 2)
    return f"https://pawchive.pw/icons/{source}/{artist_id}"


def profile_id(profile: str) -> str:
    match = ARTIST_ID_RE.search(normalize_profile(profile))
    return match.group(1) if match else ''


def artist_profile_url(value: str) -> str:
    """Accept either a Pawchive creator URL or a numeric id."""
    value = str(value or '').strip()
    if value.isdigit():
        return f'https://pawchive.pw/fanbox/user/{value}'
    return normalize_profile(value)


def _locked_mutation(method):
    @wraps(method)
    def call(store,*args,**kwargs):
        with getattr(store,'_lock',nullcontext()):
            return method(store,*args,**kwargs)
    return call


def _safe_timestamp(value):
    try: result=float(value or 0)
    except (ValueError,TypeError,OverflowError): return 0.0
    return result if math.isfinite(result) and result>=0 else 0.0


def _safe_cards(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for profile, card in value.items():
        profile = normalize_profile(profile)
        if not profile or not isinstance(card, dict):
            continue
        try:
            posts = max(0, min(1_000_000, int(card.get('posts', 0))))
            files = max(0, min(10_000_000, int(card.get('files', 0))))
        except (TypeError, ValueError, OverflowError):
            continue
        unlocked = [name for name, threshold in ACHIEVEMENTS if posts >= threshold]
        result[profile] = {
            'profile': profile,
            'artist_id': str(card.get('artist_id') or profile_id(profile)),
            'name': str(card.get('name') or 'Unknown Artist')[:160],
            'posts': posts,
            'files': files,
            'achievement': unlocked[-1] if unlocked else 'none',
            'unlocked': unlocked,
            'avatar_path': str(card.get('avatar_path') or ''),
            'avatar_status': str(card.get('avatar_status') or 'missing')[:24],
            'avatar_url': avatar_url(profile),
            'latest_download_ts': _safe_timestamp(card.get('latest_download_ts',card.get('download_ts',0))),
        }
    return result


@_locked_mutation
def record_download(store, profile: str, *, posts=1, files=0, latest_download_ts=None):
    """Increment an artist's local counters and return the card state.

    The counters are intentionally local and contain no download URLs or
    titles.  They are used only to unlock the visual card cabinet.
    """
    profile = normalize_profile(profile)
    if not profile:
        return None
    cards = _safe_cards(store.get_setting('artist_cards', {}))
    card = cards.setdefault(profile, {
        'profile': profile, 'artist_id': profile_id(profile), 'name': 'Unknown Artist', 'posts': 0, 'files': 0,
        'achievement': 'none', 'unlocked': [],
        'avatar_path': '', 'avatar_status': 'missing',
    })
    try:
        posts = max(0, min(100_000, int(posts)))
        files = max(0, min(1_000_000, int(files)))
    except (TypeError, ValueError, OverflowError):
        posts = files = 0
    if latest_download_ts is not None:
        card['latest_download_ts']=max(_safe_timestamp(card.get('latest_download_ts')), _safe_timestamp(latest_download_ts))
    previous = card.get('achievement', 'none')
    card['posts'] += posts
    card['files'] += files
    unlocked = [name for name, threshold in ACHIEVEMENTS if card['posts'] >= threshold]
    card['achievement'] = unlocked[-1] if unlocked else 'none'
    card['unlocked'] = unlocked
    if card['achievement'] == 'master' and not card.get('avatar_path'):
        card['avatar_status'] = 'pending'
    cards[profile] = card
    store.set_setting('artist_cards', cards)
    card = dict(card)
    card['new_achievement'] = previous != card['achievement']
    card['new_master'] = previous != 'master' and card['achievement'] == 'master'
    return card


@_locked_mutation
def update_artist_metadata(store, profile: str, *, name=None, avatar_path=None, avatar_status=None, create=False):
    """Merge display metadata into an existing deduplicated card.

    Metadata is optional and bounded; a failed page parse therefore leaves a
    usable card with ``Unknown Artist`` and the default icon fallback.
    """
    profile = normalize_profile(profile)
    if not profile:
        return None
    cards = _safe_cards(store.get_setting('artist_cards', {}))
    if profile not in cards and not create:
        return None  # A stale metadata worker must not recreate a deleted card.
    card = cards.setdefault(profile, {
        'profile': profile, 'artist_id': profile_id(profile), 'name': 'Unknown Artist',
        'posts': 0, 'files': 0, 'achievement': 'none', 'unlocked': [],
        'avatar_path': '', 'avatar_status': 'missing',
    })
    card['artist_id'] = profile_id(profile)
    if name:
        card['name'] = str(name).strip()[:160] or 'Unknown Artist'
    if avatar_path:
        card['avatar_path'] = str(avatar_path)[:512]
        card['avatar_status'] = 'ready'
    elif avatar_status:
        card['avatar_status'] = str(avatar_status)[:24]
    cards[profile] = card
    store.set_setting('artist_cards', cards)
    return dict(card)


@_locked_mutation
def remove_card(store, profile: str):
    profile = normalize_profile(profile)
    cards = _safe_cards(store.get_setting('artist_cards', {}))
    if profile in cards:
        cards.pop(profile, None); store.set_setting('artist_cards', cards); return True
    return False


def paged_cards(store, page=0, page_size=20):
    cards = sorted(list_cards(store), key=lambda item: (str(item.get('name') or '').lower(), item.get('artist_id', '')))
    page_size = max(1, min(20, int(page_size))); page = max(0, int(page))
    start = page * page_size
    return cards[start:start + page_size], len(cards)


class ArtistMetadataWorker(QThread):
    """Fetch artist display name and avatar URL without touching UI."""
    result = Signal(str, str, str, str)  # profile, name, avatar_url, error

    def __init__(self, profile, parent=None):
        super().__init__(parent); self.profile = normalize_profile(profile)

    def run(self):
        if not self.profile:
            self.result.emit('', 'Unknown Artist', '', 'invalid_profile'); return
        try:
            response = requests.get(self.profile, headers={'User-Agent': 'PawchiveDownloader/1.3'}, timeout=(8, 20))
            response.raise_for_status()
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, 'html.parser')
            node = soup.select_one("a.user-header__profile [itemprop='name'], a.user-header__profile span[itemprop='name']")
            name = node.get_text(' ', strip=True) if node else 'Unknown Artist'
            image = soup.select_one('.user-header__profile-image img, img.fancy-image__image')
            src = str(image.get('src') or '') if image else ''
            if src.startswith('/'):
                src = 'https://pawchive.pw' + src
            self.result.emit(self.profile, name[:160] or 'Unknown Artist', src, '')
        except Exception as error:
            self.result.emit(self.profile, 'Unknown Artist', '', str(error)[:120])


def list_cards(store):
    return list(_safe_cards(store.get_setting('artist_cards', {})).values())


def cleanup_cache(store):
    """Remove interrupted or unreferenced avatar files without touching cards."""
    root = (Path(store.data_dir).resolve() / 'artist-cards').resolve()
    if not root.is_dir() or root.is_symlink():
        return 0
    referenced = set()
    for card in list_cards(store):
        value = str(card.get('avatar_path') or '')
        if not value:
            continue
        try:
            candidate = Path(value).resolve()
            if candidate.parent == root:
                referenced.add(candidate)
        except (OSError, RuntimeError, ValueError):
            continue
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.is_file() or entry.resolve() in referenced:
            continue
        try:
            entry.unlink()
            removed += 1
        except OSError:
            pass
    return removed


class AvatarFetchWorker(QThread):
    """Fetch one avatar with strict host, size and content checks."""
    result = Signal(str, str, str)  # profile, local path, error code

    def __init__(self, data_dir, profile, parent=None):
        super().__init__(parent)
        self.data_dir = Path(data_dir).resolve()
        self.profile = normalize_profile(profile)

    def run(self):
        if not self.profile:
            self.result.emit('', '', 'invalid_profile')
            return
        target_dir = self.data_dir / 'artist-cards'
        try:
            if self.isInterruptionRequested(): raise OSError('cancelled')
            target_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(self.profile.encode('utf-8')).hexdigest()
            session = requests.Session()
            session.headers['User-Agent'] = 'PawchiveDownloader/1.0'
            try:
                response = session.get(avatar_url(self.profile), timeout=(8, 20), stream=True)
                response.raise_for_status()
                content_type = (response.headers.get('content-type') or '').lower()
                if not content_type.startswith('image/'):
                    raise ValueError('invalid_content_type')
                length = int(response.headers.get('content-length') or 0)
                if length < 0 or length > MAX_AVATAR_BYTES:
                    raise ValueError('avatar_too_large')
                suffix = '.png' if 'png' in content_type else '.webp' if 'webp' in content_type else '.jpg'
                target = (target_dir / f'{digest}{suffix}').resolve()
                if target.parent != target_dir:
                    raise ValueError('invalid_avatar_path')
                for old in target_dir.glob(f'{digest}.*'):
                    if old != target and old.is_file():
                        old.unlink(missing_ok=True)
                temporary = target.with_suffix('.tmp')
                total = 0
                with temporary.open('wb') as stream:
                    for chunk in response.iter_content(64 * 1024):
                        if self.isInterruptionRequested(): raise OSError('cancelled')
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_AVATAR_BYTES:
                            raise ValueError('avatar_too_large')
                        stream.write(chunk)
                if total == 0:
                    raise ValueError('empty_avatar')
                if self.isInterruptionRequested(): raise OSError('cancelled')
                temporary.replace(target)
                self.result.emit(self.profile, str(target), '')
            finally:
                session.close()
        except (requests.RequestException, OSError, ValueError) as error:
            try:
                (target_dir / f'{hashlib.sha256(self.profile.encode("utf-8")).hexdigest()}.tmp').unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass
            self.result.emit(self.profile, '', str(error)[:80])


class ArtistAvatarLoader(QObject):
    """Load only the visible cabinet page with three native workers at most.

    The loader and workers belong to the main window, so closing a cabinet
    never destroys a running native thread. Data updates use GUI-thread slots;
    closing merely discards the not-yet-started page queue.
    """
    changed = Signal(str, str, str)
    MAX_WORKERS = 3

    def __init__(self,store,parent=None,*,worker_class=None):
        super().__init__(parent)
        self.store=store
        self.worker_class=worker_class or AvatarFetchWorker
        self.workers={}
        self._wanted=set(); self._queue=[]; self._failed=set(); self._closed=False
        self._defer_timer=QTimer(self); self._defer_timer.setSingleShot(True)
        self._defer_timer.timeout.connect(self._pump)

    @staticmethod
    def _has_avatar(card):
        filename=str(card.get('avatar_path') or '')
        try: return bool(filename and Path(filename).is_file())
        except (ValueError,OSError): return False

    def request(self,profiles):
        if self._closed: return
        wanted=[]
        for value in profiles:
            profile=normalize_profile(value)
            if profile and profile not in wanted: wanted.append(profile)
            if len(wanted)>=20: break
        self._wanted=set(wanted)
        cards={item['profile']:item for item in list_cards(self.store)}
        self._queue=[profile for profile in wanted if profile in cards and profile not in self.workers
                     and profile not in self._failed and not self._has_avatar(cards[profile])]
        self._pump()

    def _pump(self):
        if self._closed: return
        if getattr(self.parent(),'_subscription_check_running',False):
            if self._queue: self._defer_timer.start(500)
            return
        self._defer_timer.stop()
        cards={item['profile']:item for item in list_cards(self.store)}
        while self._queue and len(self.workers)<self.MAX_WORKERS:
            profile=self._queue.pop(0)
            card=cards.get(profile)
            if profile not in self._wanted or card is None or profile in self.workers or self._has_avatar(card):
                continue
            worker=self.worker_class(self.store.data_dir,profile,self.parent())
            self.workers[profile]=worker
            worker.result.connect(self._on_result)
            worker.finished.connect(self._on_finished)
            worker.start()

    @Slot(str,str,str)
    def _on_result(self,profile,path,error):
        if error: self._failed.add(profile)
        # Explicit add/import creates cards. A deleted card stays deleted even
        # if an old in-flight avatar request succeeds a moment later.
        card=update_artist_metadata(self.store,profile,avatar_path=path or None,
                                   avatar_status='missing' if error else 'ready')
        if card is not None: self.changed.emit(profile,path,error)

    @Slot()
    def _on_finished(self):
        worker=self.sender()
        profile=getattr(worker,'profile','')
        self.workers.pop(profile,None)
        if worker is not None: worker.deleteLater()
        if self._closed:
            if not self.workers: self.deleteLater()
        else: self._pump()

    def close(self):
        self._closed=True; self._wanted.clear(); self._queue.clear(); self._defer_timer.stop()
        if not self.workers: self.deleteLater()
