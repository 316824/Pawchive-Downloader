"""Per-artist achievement cards and bounded avatar fetching.

Artist card data is deliberately small and stored in the existing settings
record.  Downloaded avatar bytes are kept in a dedicated cache directory and
are never used as a path supplied by a web page.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

import requests
from PySide6.QtCore import QThread, Signal


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
            'posts': posts,
            'files': files,
            'achievement': unlocked[-1] if unlocked else 'none',
            'unlocked': unlocked,
            'avatar_path': str(card.get('avatar_path') or ''),
            'avatar_status': str(card.get('avatar_status') or 'missing')[:24],
        }
    return result


def record_download(store, profile: str, *, posts=1, files=0):
    """Increment an artist's local counters and return the card state.

    The counters are intentionally local and contain no download URLs or
    titles.  They are used only to unlock the visual card cabinet.
    """
    profile = normalize_profile(profile)
    if not profile:
        return None
    cards = _safe_cards(store.get_setting('artist_cards', {}))
    card = cards.setdefault(profile, {
        'profile': profile, 'posts': 0, 'files': 0,
        'achievement': 'none', 'unlocked': [],
        'avatar_path': '', 'avatar_status': 'missing',
    })
    try:
        posts = max(0, min(100_000, int(posts)))
        files = max(0, min(1_000_000, int(files)))
    except (TypeError, ValueError, OverflowError):
        posts = files = 0
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
    finished = Signal(str, str, str)  # profile, local path, error code

    def __init__(self, data_dir, profile, parent=None):
        super().__init__(parent)
        self.data_dir = Path(data_dir).resolve()
        self.profile = normalize_profile(profile)

    def run(self):
        if not self.profile:
            self.finished.emit('', '', 'invalid_profile')
            return
        target_dir = self.data_dir / 'artist-cards'
        try:
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
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_AVATAR_BYTES:
                            raise ValueError('avatar_too_large')
                        stream.write(chunk)
                if total == 0:
                    raise ValueError('empty_avatar')
                temporary.replace(target)
                self.finished.emit(self.profile, str(target), '')
            finally:
                session.close()
        except (requests.RequestException, OSError, ValueError) as error:
            try:
                (target_dir / f'{hashlib.sha256(self.profile.encode("utf-8")).hexdigest()}.tmp').unlink(missing_ok=True)
            except (OSError, UnboundLocalError):
                pass
            self.finished.emit(self.profile, '', str(error)[:80])
