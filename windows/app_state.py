"""Small per-user state store; it never contains downloaded images or private keys."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid


ACTIVE_STATUSES = ('pending', 'queued', 'running', 'paused')
STATUSES = (*ACTIVE_STATUSES, 'completed', 'missed', 'failed', 'cancelled')
MAX_SCHEDULES = 10
_PROFILE = re.compile(r'https://pawchive\.pw/(?:fanbox|patreon|subscribestar|discord|boosty)/user/[^/?#\s]+/?')


class StateError(RuntimeError):
    pass


class ScheduleLimitError(StateError):
    pass


def schedule_id(value):
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError('Invalid schedule identifier.') from error


def utc_timestamp(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('A scheduled datetime must include its time zone.')
        value = value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Invalid schedule time.')
    # Also validate the supported datetime range before saving/installing it.
    datetime.fromtimestamp(value, timezone.utc)
    return float(value)


def normalize_snapshot(value):
    if not isinstance(value, dict):
        raise ValueError('A schedule needs download settings.')
    profile = value.get('profile', '')
    folder = value.get('folder', '')
    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile.strip()):
        raise ValueError('Enter a complete Pawchive creator profile URL.')
    if (not isinstance(folder, str) or not folder.strip() or
            any(ord(character) < 32 for character in folder) or not os.path.isabs(folder)):
        raise ValueError('Choose an absolute save folder.')
    order = value.get('order', 'newest')
    if order not in ('newest', 'oldest'):
        raise ValueError('Unknown date order.')
    by_title = value.get('by_title', True)
    rename_order = value.get('rename_order', False)
    if not isinstance(by_title, bool) or not isinstance(rename_order, bool):
        raise ValueError('Invalid folder or renaming option.')
    start = value.get('start_number', 1)
    if isinstance(start, bool) or not isinstance(start, int) or not 0 <= start <= 999999999:
        raise ValueError('Invalid starting number.')
    result = dict(profile=profile.strip().rstrip('/'), folder=str(Path(folder)), order=order,
                  by_title=by_title, rename_order=rename_order and not by_title, start_number=start)
    if 'lang' in value:
        if value['lang'] not in ('简体中文', '繁體中文', 'English', '日本語'):
            raise ValueError('Unknown interface language.')
        result['lang'] = value['lang']
    return result


def due_eligible(due_at, now, boot_time, grace_seconds=120):
    """A brief launch delay is allowed, but missed shutdown times never catch up."""
    due_at, now, boot_time = map(utc_timestamp, (due_at, now, boot_time))
    return boot_time <= due_at <= now and now - due_at <= max(0, grace_seconds)


@dataclass(frozen=True)
class Schedule:
    id: str
    due_at: float
    timezone: str
    snapshot: dict
    status: str
    label: str
    error: str
    created_at: float
    updated_at: float

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class CheckInResult:
    day: str
    created: bool
    total: int
    streak: int
    month_count: int
    longest_streak: int

    def as_dict(self):
        return asdict(self)


class AppState:
    def __init__(self, data_dir=None, *, clock=None):
        if data_dir is None:
            base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
            data_dir = base / 'PawchiveDownloader'
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / 'state.sqlite3'
        self._lock = threading.RLock()
        self._clock = clock or time.time
        self._closed = False
        self._db = sqlite3.connect(str(self.db_path), timeout=10, check_same_thread=False,
                                   isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=DELETE')
        self._db.execute('PRAGMA synchronous=FULL')
        self._db.executescript('''
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schedules(
                id TEXT PRIMARY KEY,due_at REAL NOT NULL,timezone TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,status TEXT NOT NULL,label TEXT NOT NULL DEFAULT '',
                error TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,updated_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS schedule_due ON schedules(status,due_at);
            CREATE TABLE IF NOT EXISTS checkins(day TEXT PRIMARY KEY,created_at REAL NOT NULL);
            PRAGMA user_version=1;
        ''')

    @contextmanager
    def _write(self):
        with self._lock:
            self._ensure_open()
            self._db.execute('BEGIN IMMEDIATE')
            try:
                yield
            except BaseException:
                self._db.execute('ROLLBACK')
                raise
            else:
                self._db.execute('COMMIT')

    def _ensure_open(self):
        if self._closed:
            raise StateError('The local state store is closed.')

    @staticmethod
    def _encode(value):
        result = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        if len(result.encode('utf-8')) > 1024 * 1024:
            raise ValueError('This state record is too large.')
        return result

    def get_setting(self, key, default=None):
        with self._lock:
            self._ensure_open()
            row = self._db.execute('SELECT value_json FROM settings WHERE key=?', (str(key),)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        encoded = self._encode(value)
        with self._write():
            self._db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json',
                             (str(key), encoded))

    def remove_setting(self, key):
        with self._write():
            self._db.execute('DELETE FROM settings WHERE key=?', (str(key),))

    @staticmethod
    def _schedule(row):
        if row is None:
            return None
        return Schedule(id=row['id'], due_at=row['due_at'], timezone=row['timezone'],
                        snapshot=normalize_snapshot(json.loads(row['snapshot_json'])),
                        status=row['status'], label=row['label'], error=row['error'],
                        created_at=row['created_at'], updated_at=row['updated_at'])

    def add_schedule(self, snapshot, due_at, *, timezone='', label=''):
        snapshot, due_at = normalize_snapshot(snapshot), utc_timestamp(due_at)
        now = self._clock()
        if due_at <= now:
            raise ValueError('Choose a time in the future.')
        identifier = str(uuid.uuid4())
        with self._write():
            count = self._db.execute("SELECT COUNT(*) FROM schedules WHERE status IN ('pending','queued','running','paused')").fetchone()[0]
            if count >= MAX_SCHEDULES:
                raise ScheduleLimitError('At most 10 pending or active download schedules are allowed.')
            self._db.execute('INSERT INTO schedules VALUES(?,?,?,?,?,?,?,?,?)',
                             (identifier, due_at, str(timezone)[:100], self._encode(snapshot),
                              'pending', str(label)[:500], '', now, now))
        return self.get_schedule(identifier)

    def get_schedule(self, identifier):
        identifier = schedule_id(identifier)
        with self._lock:
            self._ensure_open()
            return self._schedule(self._db.execute('SELECT * FROM schedules WHERE id=?', (identifier,)).fetchone())

    def list_schedules(self, active_only=False):
        with self._lock:
            self._ensure_open()
            condition = " WHERE status IN ('pending','queued','running','paused')" if active_only else ''
            return [self._schedule(row) for row in self._db.execute('SELECT * FROM schedules' + condition + ' ORDER BY due_at,created_at,id')]

    def update_schedule(self, identifier, *, snapshot=None, due_at=None, timezone=None, label=None):
        identifier = schedule_id(identifier)
        with self._write():
            old = self._schedule(self._db.execute('SELECT * FROM schedules WHERE id=?', (identifier,)).fetchone())
            if old is None:
                raise KeyError(identifier)
            if old.status != 'pending':
                raise StateError('Only a pending schedule can be edited.')
            changed_due = utc_timestamp(due_at) if due_at is not None else old.due_at
            if changed_due <= self._clock():
                raise ValueError('Choose a time in the future.')
            changed_snapshot = normalize_snapshot(snapshot) if snapshot is not None else old.snapshot
            self._db.execute('UPDATE schedules SET snapshot_json=?,due_at=?,timezone=?,label=?,updated_at=? WHERE id=?',
                             (self._encode(changed_snapshot), changed_due,
                              old.timezone if timezone is None else str(timezone)[:100],
                              old.label if label is None else str(label)[:500], self._clock(), identifier))
        return self.get_schedule(identifier)

    def set_schedule_status(self, identifier, status, *, error='', expected=None):
        if status not in STATUSES:
            raise ValueError('Unknown schedule state.')
        identifier = schedule_id(identifier)
        with self._write():
            row = self._db.execute('SELECT status FROM schedules WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            if expected is not None and row[0] not in ((expected,) if isinstance(expected, str) else expected):
                return None
            if status == 'running' and row[0] != 'running':
                if self._db.execute("SELECT 1 FROM schedules WHERE status='running' AND id<>? LIMIT 1", (identifier,)).fetchone():
                    raise StateError('Another scheduled download is already running.')
            if status in ACTIVE_STATUSES and row[0] not in ACTIVE_STATUSES:
                count = self._db.execute("SELECT COUNT(*) FROM schedules WHERE status IN ('pending','queued','running','paused')").fetchone()[0]
                if count >= MAX_SCHEDULES:
                    raise ScheduleLimitError('At most 10 pending or active download schedules are allowed.')
            self._db.execute('UPDATE schedules SET status=?,error=?,updated_at=? WHERE id=?',
                             (status, str(error)[:4000], self._clock(), identifier))
        return self.get_schedule(identifier)

    def delete_schedule(self, identifier):
        identifier = schedule_id(identifier)
        with self._write():
            row = self._db.execute('SELECT status FROM schedules WHERE id=?', (identifier,)).fetchone()
            if row and row[0] == 'running':
                raise StateError('Stop the running download before deleting its schedule.')
            cursor = self._db.execute('DELETE FROM schedules WHERE id=?', (identifier,))
            return bool(cursor.rowcount)

    def enqueue_due(self, now=None, boot_time=None, grace_seconds=120):
        now = utc_timestamp(self._clock() if now is None else now)
        # Callers in production provide Windows boot time. Omitted means this
        # process' observation rather than permission to catch up old tasks.
        boot_time = utc_timestamp(now if boot_time is None else boot_time)
        ready = []
        with self._write():
            rows = self._db.execute("SELECT * FROM schedules WHERE status IN ('pending','queued') AND due_at<=? ORDER BY due_at,id", (now,)).fetchall()
            for row in rows:
                if row['status'] == 'queued' and row['due_at'] >= boot_time:
                    continue  # Waiting behind this same session's active task.
                if due_eligible(row['due_at'], now, boot_time, grace_seconds):
                    status, reason = 'queued', ''
                    ready.append(row['id'])
                else:
                    status, reason = 'missed', 'The scheduled time was missed; it will not be run later.'
                self._db.execute('UPDATE schedules SET status=?,error=?,updated_at=? WHERE id=?',
                                 (status, reason, now, row['id']))
        return [self.get_schedule(identifier) for identifier in ready]

    def claim_next_schedule(self):
        with self._write():
            # Independent SQLite connections must not start two scheduled jobs.
            if self._db.execute("SELECT 1 FROM schedules WHERE status='running' LIMIT 1").fetchone():
                return None
            row = self._db.execute("SELECT * FROM schedules WHERE status='queued' ORDER BY due_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            self._db.execute("UPDATE schedules SET status='running',updated_at=? WHERE id=? AND status='queued'", (self._clock(), row['id']))
            identifier = row['id']
        return self.get_schedule(identifier)

    def _today(self):
        return datetime.fromtimestamp(self._clock()).date()

    @staticmethod
    def _date(value):
        if isinstance(value, datetime):
            return value.date()
        return value if isinstance(value, date) else date.fromisoformat(str(value))

    def check_in(self, day=None):
        current = self._today()
        day = current if day is None else self._date(day)
        if day != current:
            raise ValueError('Check-ins use today\'s local date; past and future dates cannot be checked in.')
        with self._write():
            cursor = self._db.execute('INSERT OR IGNORE INTO checkins VALUES(?,?)', (day.isoformat(), self._clock()))
            created = bool(cursor.rowcount)
        stats = self.checkin_stats(day)
        return CheckInResult(day.isoformat(), created, stats['total'], stats['streak'],
                             stats['month_count'], stats['longest_streak'])

    def checkin_dates(self, year, month):
        first = date(int(year), int(month), 1)
        last = date(first.year + 1, 1, 1) if first.month == 12 else date(first.year, first.month + 1, 1)
        with self._lock:
            self._ensure_open()
            return {row[0] for row in self._db.execute('SELECT day FROM checkins WHERE day>=? AND day<? ORDER BY day', (first.isoformat(), last.isoformat()))}

    def checkin_stats(self, day=None):
        day = self._today() if day is None else self._date(day)
        with self._lock:
            self._ensure_open()
            ordered = [date.fromisoformat(row[0]) for row in self._db.execute('SELECT day FROM checkins WHERE day<=? ORDER BY day', (day.isoformat(),))]
        days = set(ordered)
        cursor = day if day in days else day - timedelta(days=1)
        streak = 0
        while cursor in days:
            streak += 1
            cursor -= timedelta(days=1)
        longest = run = 0
        previous = None
        for item in ordered:
            run = run + 1 if previous and item == previous + timedelta(days=1) else 1
            longest = max(longest, run)
            previous = item
        total = len(ordered)
        milestones = (7, 30, 100, 365, 730, 1095)
        return dict(total=total, streak=streak, longest_streak=longest,
                    month_count=sum(item.year == day.year and item.month == day.month for item in ordered),
                    today_checked=day in days,
                    next_milestone=next((point for point in milestones if point > total), (total // 365 + 1) * 365))

    def storage_bytes(self):
        return self.db_path.stat().st_size if self.db_path.exists() else 0

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
