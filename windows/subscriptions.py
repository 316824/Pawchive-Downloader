"""Local artist subscriptions and feed update checks.

The subscription record intentionally contains only a normalized Pawchive
profile URL and a timestamp.  Network work is performed by
``SubscriptionCheckWorker`` so the main Qt thread remains responsive.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import random
import re
import threading
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from PySide6.QtCore import QThread, Signal


PROFILE_RE = re.compile(
    r"^https://pawchive\.pw/(fanbox|patreon|subscribestar|discord|boosty)/user/([^/?#\s]+)$",
    re.IGNORECASE,
)
USER_ID_RE = re.compile(r"/user/([^/?#]+)", re.IGNORECASE)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PawchiveDownloader/1.3"


def normalize_profile(value: str) -> str:
    match = PROFILE_RE.fullmatch(str(value or "").strip().rstrip("/"))
    if not match:
        # Accept a bare numeric id as a convenience in the add dialog.
        if re.fullmatch(r"\d{1,20}", str(value or "").strip()):
            return f"https://pawchive.pw/fanbox/user/{str(value).strip()}"
        return ""
    return f"https://pawchive.pw/{match.group(1).lower()}/user/{match.group(2)}"


def profile_id(profile: str) -> str:
    match = USER_ID_RE.search(normalize_profile(profile))
    return match.group(1) if match else ""


def parse_site_datetime(value: str | None) -> float | None:
    """Parse Pawchive's timestamp into a UTC epoch.

    Pawchive currently emits ``YYYY-mm-dd HH:MM[:SS]`` without an offset.
    Such values are interpreted as UTC, while ISO values containing an offset
    retain their explicit timezone.  Comparing numbers avoids lexical date
    and local timezone bugs.
    """
    text = str(value or "").strip()
    if not text:
        return None
    # HTML timestamps occasionally omit zero padding (``2026-10-9``).  Pad
    # the date components before ISO parsing so chronological comparisons stay
    # numeric and timezone-safe.
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(.*)$", text)
    if match:
        text = f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}{match.group(4)}"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return float(parsed.timestamp())


def _safe_record(value):
    if not isinstance(value, dict):
        return None
    profile = normalize_profile(value.get("profile", ""))
    if not profile:
        return None
    try:
        baseline = float(value.get("baseline_ts", 0) or 0)
        checked = float(value.get("last_checked_ts", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        baseline = checked = 0.0
    try:
        latest = max(0.0, float(value.get("latest_ts", 0) or 0))
    except (TypeError, ValueError, OverflowError):
        latest = 0.0
    return {
        "profile": profile,
        "id": profile_id(profile),
        "name": str(value.get("name") or "Unknown Artist")[:160],
        "avatar_path": str(value.get("avatar_path") or "")[:512],
        "baseline_ts": max(0.0, baseline),
        "last_checked_ts": max(0.0, checked),
        "latest_ts": latest,
        "unread": bool(value.get("unread", False)),
        "enabled": bool(value.get("enabled", True)),
    }


class SubscriptionStore:
    """Thread-safe settings-backed subscription records."""

    KEY = "artist_subscriptions"

    def __init__(self, state_store):
        self.store = state_store
        self._lock = threading.RLock()

    def list(self):
        with self._lock:
            raw = self.store.get_setting(self.KEY, [])
            values = [_safe_record(item) for item in raw] if isinstance(raw, list) else []
            values = [item for item in values if item]
            # Deduplicate by normalized profile, retaining the newest record.
            unique = {item["profile"]: item for item in values}
            return list(unique.values())

    def _save(self, records):
        self.store.set_setting(self.KEY, [_safe_record(item) for item in records if _safe_record(item)])

    def get(self, profile):
        normalized = normalize_profile(profile)
        return next((item for item in self.list() if item["profile"] == normalized), None)

    def add(self, profile, *, name="Unknown Artist", avatar_path="", baseline_ts=None):
        normalized = normalize_profile(profile)
        if not normalized:
            raise ValueError("请输入有效的 Pawchive 画师主页或数字 ID")
        with self._lock:
            records = self.list()
            now = time.time()
            existing = next((item for item in records if item["profile"] == normalized), None)
            if existing is None:
                # A new subscription starts at the current latest result (or
                # the supplied baseline), so it does not immediately notify.
                existing = {
                    "profile": normalized, "id": profile_id(normalized),
                    "name": str(name or "Unknown Artist")[:160],
                    "avatar_path": str(avatar_path or "")[:512],
                    "baseline_ts": float(baseline_ts if baseline_ts is not None else now),
                    "latest_ts": float(baseline_ts if baseline_ts is not None else now),
                    "last_checked_ts": 0.0, "unread": False, "enabled": True,
                }
                records.append(existing)
            else:
                if name and existing.get("name") == "Unknown Artist":
                    existing["name"] = str(name)[:160]
                if avatar_path:
                    existing["avatar_path"] = str(avatar_path)[:512]
                existing["enabled"] = True
            self._save(records)
            return dict(existing)

    def remove(self, profile):
        normalized = normalize_profile(profile)
        with self._lock:
            records = [item for item in self.list() if item["profile"] != normalized]
            self._save(records)

    def set_baseline(self, profile, latest_ts, *, name=None, avatar_path=None, unread=None):
        normalized = normalize_profile(profile)
        if not normalized:
            return None
        with self._lock:
            records = self.list()
            record = next((item for item in records if item["profile"] == normalized), None)
            # A background check may finish after the user removed the
            # subscription.  Do not recreate that record from a stale worker
            # snapshot; only an explicit add operation may create it.
            if record is None:
                return None
            if latest_ts is not None:
                record["latest_ts"] = max(0.0, float(latest_ts))
            # A completed background fetch becomes the next comparison point;
            # preserving this marker lets a newly added subscription establish
            # its baseline without creating a false unread notification.
            record["last_checked_ts"] = time.time()
            if name:
                record["name"] = str(name)[:160]
            if avatar_path:
                record["avatar_path"] = str(avatar_path)[:512]
            if unread is not None:
                record["unread"] = bool(unread)
            self._save(records)
            return dict(record)

    def mark_read(self, profile):
        with self._lock:
            record = self.get(profile)
            if not record:
                return
            record["unread"] = False
            record["baseline_ts"] = max(record["baseline_ts"], record.get("latest_ts", 0.0))
            self._save([record if item["profile"] == record["profile"] else item for item in self.list()])

    def unread_count(self):
        return sum(1 for item in self.list() if item.get("unread"))


def fetch_latest(profile, *, timeout=(8, 20), session=None):
    """Fetch one profile's newest post timestamp and display metadata."""
    normalized = normalize_profile(profile)
    if not normalized:
        raise ValueError("invalid_profile")
    owned = session is None
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", USER_AGENT)
    try:
        response = session.get(normalized, timeout=timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        times = []
        for node in soup.select("time.timestamp[datetime], time[datetime]"):
            ts = parse_site_datetime(node.get("datetime"))
            if ts is not None:
                times.append(ts)
        latest = max(times) if times else 0.0
        name_node = soup.select_one("a.user-header__profile [itemprop='name'], a.user-header__profile span[itemprop='name']")
        name = name_node.get_text(" ", strip=True) if name_node else "Unknown Artist"
        avatar = ""
        avatar_node = soup.select_one(".user-header__profile-image img, img.fancy-image__image")
        if avatar_node and avatar_node.get("src"):
            avatar = urljoin(normalized, avatar_node.get("src"))
        return {"profile": normalized, "latest_ts": latest, "name": name[:160], "avatar_url": avatar}
    finally:
        if owned:
            session.close()


class SubscriptionCheckWorker(QThread):
    """Bounded background checker: at most three requests at once."""

    progress = Signal(str, object)
    changed = Signal(str, object)
    finished_all = Signal(object)

    def __init__(self, subscription_store, parent=None, *, seed=None):
        super().__init__(parent)
        self.subscription_store = subscription_store
        self.seed = seed
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        records = [item for item in self.subscription_store.list() if item.get("enabled", True)]
        results = []
        if not records:
            self.finished_all.emit(results)
            return
        rng = random.Random(self.seed)
        def check(record):
            if self._stop.is_set():
                return record, None, "cancelled"
            try:
                result = fetch_latest(record["profile"])
                return record, result, ""
            except Exception as error:  # network errors must not kill the UI
                return record, None, str(error)[:160]

        # Keep at most three requests active.  A new request is submitted only
        # after a prior one completes and a random 1–3 second cooldown has
        # elapsed, which avoids bursty refreshes while retaining concurrency.
        with ThreadPoolExecutor(max_workers=min(3, len(records))) as pool:
            pending = {}
            next_index = 0
            while next_index < len(records) and len(pending) < 3:
                record = records[next_index]; next_index += 1
                pending[pool.submit(check, record)] = record
            while pending and not self._stop.is_set():
                done, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                for future in done:
                    record = pending.pop(future)
                    if self._stop.is_set():
                        break
                    result_record, result, error = future.result()
                    payload = result or {"profile": result_record["profile"], "latest_ts": result_record.get("latest_ts", 0.0)}
                    payload["error"] = error
                    is_new = not result_record.get("last_checked_ts")
                    payload["updated"] = bool(
                        result and not is_new
                        and result.get("latest_ts", 0.0) > result_record.get("baseline_ts", 0.0)
                    )
                    if result:
                        self.subscription_store.set_baseline(
                            result_record["profile"], result.get("latest_ts", 0.0),
                            name=result.get("name") or "Unknown Artist",
                            unread=payload["updated"],
                        )
                    self.progress.emit(result_record["profile"], payload)
                    results.append(payload)
                    if next_index < len(records) and not self._stop.is_set():
                        time.sleep(rng.uniform(1.0, 3.0))
                        record = records[next_index]; next_index += 1
                        pending[pool.submit(check, record)] = record
        self.finished_all.emit(results)


__all__ = [
    "SubscriptionStore", "SubscriptionCheckWorker", "normalize_profile",
    "profile_id", "parse_site_datetime", "fetch_latest",
]
