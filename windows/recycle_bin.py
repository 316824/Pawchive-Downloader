"""Persistent recycle bin for downloads cancelled from the Windows queue.

The recycle bin stores a small manifest plus optional moved payloads and cover
thumbnails under the application's data directory.  It intentionally does
not touch arbitrary user files during cleanup: every path removed by this
module is resolved below its own ``recycle-bin`` directory.

Expiry uses a monotonic wall-clock floor.  The last observed UTC timestamp is
persisted in the manifest database, so moving the local clock backwards cannot
make an item expire early after a restart.  A caller may inject a trusted clock
for tests or a future Windows time service without changing the storage API.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
import uuid


RETENTION_SECONDS = 24 * 60 * 60
MAX_COVER_BYTES = 8 * 1024 * 1024
MAX_METADATA_BYTES = 256 * 1024
ALLOWED_COVER_EXTENSIONS = frozenset({'.jpg', '.jpeg', '.png', '.webp'})
ENTRY_ID = re.compile(r'^[a-f0-9]{32}$')


@dataclass(frozen=True)
class RecycleEntry:
    """Metadata displayed by the recycle-bin panel."""

    id: str
    title: str
    profile_url: str
    post_url: str
    download_url: str
    cover_path: str | None
    payload_path: str | None
    original_path: str | None
    created_at: float
    expires_at: float
    metadata: dict

    @property
    def remaining_seconds(self):
        return max(0.0, self.expires_at - self.created_at)


class RecycleBin:
    """A small SQLite-backed store for cancelled download entries.

    ``payload_path`` is optional.  When supplied to :meth:`add`, the payload is
    moved into the private recycle-bin directory and can later be restored by
    :meth:`restore`.  This makes cancellation safe for partial album folders
    while allowing queue implementations that already clean their files to
    store metadata and a re-download link only.
    """

    def __init__(self, data_dir=None, *, clock=None,
                 retention_seconds=RETENTION_SECONDS):
        if data_dir is None:
            base = Path(os.environ.get('LOCALAPPDATA') or
                        Path.home() / 'AppData' / 'Local')
            data_dir = base / 'PawchiveDownloader'
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.root = self.data_dir / 'recycle-bin'
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / 'recycle-bin.sqlite3'
        self._lock = threading.RLock()
        self._clock = clock or time.time
        try:
            retention = float(retention_seconds)
        except (TypeError, ValueError, OverflowError):
            retention = RETENTION_SECONDS
        self.retention_seconds = max(1.0, retention) if math.isfinite(retention) else RETENTION_SECONDS
        self._db = sqlite3.connect(str(self.db_path), timeout=15,
                                   check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=DELETE')
        self._db.execute('PRAGMA synchronous=FULL')
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS entries(
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                profile_url TEXT NOT NULL,
                post_url TEXT NOT NULL,
                download_url TEXT NOT NULL,
                cover_path TEXT,
                payload_path TEXT,
                original_path TEXT,
                payload_is_dir INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS recycle_expiry ON entries(expires_at);
        ''')
        self._db.commit()
        self._closed = False
        # This also cleans entries left behind while the application was closed.
        self.cleanup()

    def _ensure_open(self):
        if self._closed or self._db is None:
            raise RuntimeError('The recycle bin is closed.')

    def _now(self):
        """Return a persisted non-decreasing UTC timestamp.

        ``time.time`` is UTC epoch time on Windows.  The floor prevents a
        manual clock rollback from making a 24-hour entry disappear early on a
        later launch.  A trusted clock can be injected by the host if desired.
        """
        try:
            value = float(self._clock())
        except (TypeError, ValueError, OverflowError):
            value = 0.0
        if not math.isfinite(value):
            value = 0.0
        with self._lock:
            self._ensure_open()
            row = self._db.execute(
                'SELECT value FROM meta WHERE key=?', ('last_seen',)).fetchone()
            previous = float(row[0]) if row else 0.0
            value = max(value, previous)
            self._db.execute(
                'INSERT INTO meta(key,value) VALUES(?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('last_seen', repr(value)))
            self._db.commit()
        return value

    def _inside(self, path, *, allow_root=False):
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            raise ValueError('Recycle-bin paths must be absolute.')
        resolved = candidate.resolve()
        if resolved == self.root.resolve():
            if allow_root:
                return resolved
            raise ValueError('The recycle-bin root is not an entry.')
        if not resolved.is_relative_to(self.root.resolve()):
            raise ValueError('Recycle-bin path escapes its private directory.')
        return resolved

    @staticmethod
    def _text(value, limit=4096):
        if value is None:
            return ''
        return str(value).replace('\x00', '')[:limit]

    @staticmethod
    def _url(value, limit=4096):
        # Links are displayed and re-used by the downloader.  Keep validation
        # intentionally conservative without requiring the UI parser here.
        text = RecycleBin._text(value, limit).strip()
        if not text.startswith('https://') or any(c in text for c in '\r\n\x00'):
            return ''
        return text

    def _cover(self, source, destination):
        if not source:
            return None
        source = Path(source).expanduser()
        try:
            if source.is_symlink() or getattr(source, 'is_junction', lambda: False)():
                return None
            if not source.is_file() or source.stat().st_size > MAX_COVER_BYTES:
                return None
            extension = source.suffix.lower()
            if extension not in ALLOWED_COVER_EXTENSIONS:
                return None
            target = destination / ('cover' + extension)
            shutil.copy2(source, target)
            return str(target)
        except OSError:
            return None

    def _move_payload(self, source, destination):
        if not source:
            return None, None, None
        source = Path(source).expanduser()
        if not source.is_absolute():
            raise ValueError('The cancelled payload path must be absolute.')
        if not source.exists() or source.is_symlink() or getattr(source, 'is_junction', lambda: False)():
            return None, None, None
        source = source.resolve()
        if source == self.root or source.is_relative_to(self.root.resolve()):
            raise ValueError('The cancelled payload cannot be inside the recycle bin.')
        payload_is_dir = source.is_dir()
        target = destination / 'payload' / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(source), str(target))
        except OSError:
            # A failed move must not make cancellation fail; the metadata link
            # still gives the user a safe way to re-download the item.
            return None, None, None
        return str(target), str(source), payload_is_dir

    @staticmethod
    def _metadata(value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError('Recycle-bin metadata must be an object.')
        encoded = json.dumps(value, ensure_ascii=False, separators=(',', ':'),
                             allow_nan=False)
        if len(encoded.encode('utf-8')) > MAX_METADATA_BYTES:
            raise ValueError('Recycle-bin metadata is too large.')
        return dict(value)

    @classmethod
    def _entry(cls, row):
        if row is None:
            return None
        try:
            metadata = json.loads(row['metadata_json'])
        except (TypeError, ValueError):
            metadata = {}
        return RecycleEntry(
            id=row['id'], title=row['title'], profile_url=row['profile_url'],
            post_url=row['post_url'], download_url=row['download_url'],
            cover_path=row['cover_path'], payload_path=row['payload_path'],
            original_path=row['original_path'], created_at=float(row['created_at']),
            expires_at=float(row['expires_at']), metadata=metadata)

    def add(self, *, title, profile_url='', post_url='', download_url='',
            cover_path=None, payload_path=None, metadata=None,
            retention_seconds=None):
        """Store one cancelled album and return its :class:`RecycleEntry`.

        ``payload_path`` may be a file or album directory.  It is moved into a
        private folder.  If moving is not possible, the manifest is still kept
        so the download link remains recoverable.
        """
        self.cleanup()
        title = self._text(title, 500) or '未命名图集'
        profile_url = self._url(profile_url)
        post_url = self._url(post_url)
        download_url = self._url(download_url) or post_url or profile_url
        metadata = self._metadata(metadata)
        identifier = uuid.uuid4().hex
        now = self._now()
        retention = self.retention_seconds if retention_seconds is None else float(retention_seconds)
        if not math.isfinite(retention) or retention <= 0:
            retention = self.retention_seconds
        expires = now + retention
        entry_dir = self.root / identifier
        entry_dir.mkdir(parents=True, exist_ok=False)
        stored_cover = None
        stored_payload = None
        original = None
        payload_is_dir = False
        try:
            stored_cover = self._cover(cover_path, entry_dir)
            stored_payload, original, payload_is_dir = self._move_payload(payload_path, entry_dir)
            payload_is_dir = bool(payload_is_dir)
            with self._lock:
                self._ensure_open()
                self._db.execute(
                    'INSERT INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (identifier, title, profile_url, post_url, download_url,
                     stored_cover, stored_payload, original, int(payload_is_dir),
                     now, expires, json.dumps(metadata, ensure_ascii=False,
                                              separators=(',', ':'))))
                self._db.commit()
        except BaseException:
            # Preserve user data if inserting the manifest failed after a move.
            if stored_payload and original:
                try:
                    source = Path(stored_payload)
                    if source.exists() and not Path(original).exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(source), original)
                except OSError:
                    pass
            shutil.rmtree(entry_dir, ignore_errors=True)
            raise
        return self.get(identifier)

    def get(self, identifier):
        identifier = str(identifier)
        if not ENTRY_ID.fullmatch(identifier):
            raise ValueError('Invalid recycle-bin entry identifier.')
        with self._lock:
            self._ensure_open()
            row = self._db.execute('SELECT * FROM entries WHERE id=?',
                                   (identifier,)).fetchone()
        return self._entry(row)

    def list_entries(self):
        self.cleanup()
        with self._lock:
            self._ensure_open()
            rows = self._db.execute(
                'SELECT * FROM entries ORDER BY created_at DESC,id DESC').fetchall()
        return [self._entry(row) for row in rows]

    def _delete_entry(self, identifier):
        with self._lock:
            row = self._db.execute('SELECT * FROM entries WHERE id=?',
                                   (identifier,)).fetchone()
            if row is None:
                return None
            entry = self._entry(row)
            self._db.execute('DELETE FROM entries WHERE id=?', (identifier,))
            self._db.commit()
        try:
            shutil.rmtree(self.root / identifier, ignore_errors=False)
        except OSError:
            # Keep metadata if files could not be removed; the next cleanup can
            # retry without silently losing the user's recovery link.
            with self._lock:
                self._db.execute('INSERT OR REPLACE INTO entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                 (entry.id, entry.title, entry.profile_url,
                                  entry.post_url, entry.download_url,
                                  entry.cover_path, entry.payload_path,
                                  entry.original_path, 0, entry.created_at,
                                  entry.expires_at,
                                  json.dumps(entry.metadata, ensure_ascii=False,
                                             separators=(',', ':'))))
                self._db.commit()
            return None
        return entry

    def remove(self, identifier):
        """Permanently remove one entry and its private payload."""
        identifier = str(identifier)
        if not ENTRY_ID.fullmatch(identifier):
            raise ValueError('Invalid recycle-bin entry identifier.')
        return self._delete_entry(identifier)

    def take(self, identifier):
        """Remove one entry without restoring files, returning its metadata."""
        return self.remove(identifier)

    def restore(self, identifier, destination=None):
        """Restore a moved payload and remove its recycle entry.

        If the original location is unavailable, ``destination`` may point to
        an explicit file or directory.  Existing targets are never replaced;
        a numbered sibling is chosen instead.
        """
        identifier = str(identifier)
        if not ENTRY_ID.fullmatch(identifier):
            raise ValueError('Invalid recycle-bin entry identifier.')
        with self._lock:
            self._ensure_open()
            row = self._db.execute('SELECT * FROM entries WHERE id=?',
                                   (identifier,)).fetchone()
        entry = self._entry(row)
        if entry is None:
            return None
        payload = Path(entry.payload_path) if entry.payload_path else None
        if payload and payload.exists():
            target = Path(destination).expanduser() if destination else Path(entry.original_path or '')
            if not target.is_absolute():
                raise ValueError('Restore destination must be absolute.')
            if target.exists():
                stem, suffix = target.stem, target.suffix
                parent = target.parent
                index = 1
                while target.exists():
                    target = parent / f'{stem} ({index}){suffix}'
                    index += 1
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(payload), str(target))
            except OSError:
                return None
        removed = self._delete_entry(identifier)
        return removed

    def cleanup(self, *, now=None):
        """Delete entries expired by the non-decreasing UTC clock.

        Returns the number of entries successfully removed.  A caller may pass
        ``now`` for deterministic maintenance tests; production callers use
        the trusted/injected clock automatically.
        """
        if self._closed:
            return 0
        current = self._now() if now is None else max(float(now), self._last_seen())
        with self._lock:
            self._ensure_open()
            rows = self._db.execute(
                'SELECT id FROM entries WHERE expires_at<=? ORDER BY expires_at',
                (current,)).fetchall()
        removed = 0
        for row in rows:
            if self._delete_entry(row['id']) is not None:
                removed += 1
        return removed

    def _last_seen(self):
        with self._lock:
            row = self._db.execute('SELECT value FROM meta WHERE key=?',
                                   ('last_seen',)).fetchone()
            try:
                return float(row[0]) if row else 0.0
            except (TypeError, ValueError):
                return 0.0

    def purge(self):
        """Delete every recycle entry and its private payload."""
        with self._lock:
            rows = self._db.execute('SELECT id FROM entries').fetchall()
        removed = 0
        for row in rows:
            if self._delete_entry(row['id']) is not None:
                removed += 1
        return removed

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._db.close()
            self._db = None


__all__ = [
    'ALLOWED_COVER_EXTENSIONS', 'MAX_COVER_BYTES', 'RecycleBin',
    'RecycleEntry', 'RETENTION_SECONDS',
]
