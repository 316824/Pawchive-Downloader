"""Local subscription timestamps, portable pending posts and bounded feed checks."""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import date, datetime, timezone
import math
import random
import re
import threading
import time
from urllib.parse import urljoin
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from PySide6.QtCore import QThread, Signal


PROFILE_RE = re.compile(
    r"^https://pawchive\.pw/(fanbox|patreon|subscribestar|discord|boosty)/user/([^/?#\s]+)$",
    re.IGNORECASE,
)
USER_ID_RE = re.compile(r"/user/([^/?#]+)", re.IGNORECASE)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PawchiveDownloader/1.3"
MAX_UPDATE_POSTS = 10000


def _post_url(value, profile):
    url = str(value or '').strip().rstrip('/')
    if (re.fullmatch(re.escape(profile) + r'/post/[^/?#\\\s]{1,256}', url)
            and url.rsplit('/', 1)[-1] not in {'.', '..'}):
        return url
    return ''


def _safe_timestamp(value):
    try:
        stamp = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return stamp if math.isfinite(stamp) and 0 <= stamp <= 253402300799 else 0.0


def _safe_posts(values, profile):
    """Keep portable post metadata only; local preview files never enter state."""
    result = {}
    for item in values[:MAX_UPDATE_POSTS] if isinstance(values, list) else ():
        if not isinstance(item, dict):
            continue
        url = _post_url(item.get('url'), profile)
        if not url:
            continue
        stamp = parse_site_datetime(str(item.get('date') or ''))
        if stamp is None:
            try: stamp = float(item.get('timestamp', 0))
            except (TypeError,ValueError,OverflowError): stamp = 0.0
        if not math.isfinite(stamp) or not 0 < stamp <= 253402300799:
            continue
        cover = str(item.get('image_url') or '')[:4096]
        try:
            parsed = urlparse(cover)
            safe = (parsed.scheme == 'https' and not parsed.username and not parsed.password
                    and parsed.port in {None, 443} and
                    ((parsed.hostname or '') == 'pawchive.pw' or
                     (parsed.hostname or '').endswith('.pawchive.pw')))
        except ValueError:
            safe = False
        if not safe:
            cover = ''
        result[url] = dict(url=url, title=str(item.get('title') or 'Untitled collection')[:500],
                           date=str(item.get('date') or '')[:64], timestamp=stamp,
                           image_url=cover, image=None, formats=[], file_count=0)
    return sorted(result.values(), key=lambda item: (item['timestamp'], item['url']), reverse=True)


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
    baseline = _safe_timestamp(value.get('baseline_ts'))
    checked = _safe_timestamp(value.get('last_checked_ts'))
    latest = _safe_timestamp(value.get('latest_ts'))
    posts = _safe_posts(value.get('pending_posts'), profile)
    downloaded=[_post_url(url, profile) for url in value.get('downloaded_post_urls',[])[-MAX_UPDATE_POSTS:]
                if _post_url(url, profile)] if isinstance(value.get('downloaded_post_urls'),list) else []
    downloaded=list(dict.fromkeys(downloaded))
    posts=[item for item in posts if item['url'] not in downloaded]
    checked_date = str(value.get('last_check_date') or '')
    try: checked_date = date.fromisoformat(checked_date).isoformat()
    except ValueError: checked_date = ''
    return {
        "profile": profile,
        "id": profile_id(profile),
        "name": str(value.get("name") or "Unknown Artist")[:160],
        "avatar_path": str(value.get("avatar_path") or "")[:512],
        "baseline_ts": max(0.0, baseline),
        "last_checked_ts": max(0.0, checked),
        "latest_ts": latest,
        "unread": bool(posts or value.get("unread", False)),
        "enabled": bool(value.get("enabled", True)),
        "initialized": bool(value.get("initialized", False)),
        "last_check_date": checked_date,
        "pending_posts": posts,
        "update_count": len(posts) or int(bool(value.get('unread',False))),
        "downloaded_post_urls": downloaded,
    }


class SubscriptionStore:
    """Thread-safe settings-backed subscription records."""

    KEY = "artist_subscriptions"

    def __init__(self, state_store):
        self.store = state_store
        self._lock = getattr(state_store,'_lock',None) or threading.RLock()

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
                    "initialized": baseline_ts is not None,
                    "last_check_date": "", "pending_posts": [], "update_count": 0,"downloaded_post_urls":[],
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

    def update_avatar(self, profile, path):
        """Update display cache only, without advancing a subscription baseline.

        Avatar requests can outlive a dialog. They must never re-add a deleted
        subscription or change its unread, enabled or last-checked state.
        """
        normalized = normalize_profile(profile)
        if not normalized or not path:
            return None
        with self._lock:
            records = self.list()
            record = next((item for item in records if item['profile'] == normalized), None)
            if record is None:
                return None
            record['avatar_path'] = str(path)[:512]
            self._save(records)
            return dict(record)

    def set_baseline(self, profile, latest_ts, *, name=None, avatar_path=None, unread=None,
                     posts=None, check_date=None):
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
            was_initialized = bool(record.get('initialized',False))
            if latest_ts is not None:
                latest_value = _safe_timestamp(latest_ts)
                record["latest_ts"] = (max(record.get("latest_ts",0.0),latest_value)
                                       if record.get("initialized",False) else latest_value)
                # The first successful remote read establishes the real
                # server-side baseline.  ``add`` uses a local timestamp only
                # as a temporary placeholder while the network request is
                # still pending.
                if not record.get("initialized", False):
                    record["baseline_ts"] = latest_value
                record["initialized"] = True
            # A completed background fetch becomes the next comparison point;
            # preserving this marker lets a newly added subscription establish
            # its baseline without creating a false unread notification.
            record["last_checked_ts"] = time.time()
            record['last_check_date'] = str(check_date or date.today().isoformat())
            if name:
                record["name"] = str(name)[:160]
            if avatar_path:
                record["avatar_path"] = str(avatar_path)[:512]
            if posts is not None and was_initialized:
                merged = {item['url']:item for item in record.get('pending_posts',[])}
                downloaded=set(record.get('downloaded_post_urls',[]))
                for item in _safe_posts(posts, normalized):
                    previous=merged.get(item['url'])
                    if (item['timestamp'] > record.get('baseline_ts',0) and item['url'] not in downloaded
                            and (previous is None or item['timestamp']>=previous['timestamp'])):
                        merged[item['url']] = item
                record['pending_posts'] = _safe_posts(list(merged.values()),normalized)
            if not was_initialized: record['pending_posts'] = []
            record['unread'] = bool(record.get('pending_posts') or record.get('latest_ts',0)>record.get('baseline_ts',0))
            record['update_count'] = len(record.get('pending_posts',[])) or int(record['unread'])
            self._save(records)
            return dict(record)

    def advance_download(self,profile,timestamp,post_urls=None):
        """A completed URL clears itself; other pending posts survive newer timestamps.

        ``None`` retains the legacy timestamp-only API without clearing posts.
        An explicit empty/invalid URL list cannot advance downloaded history.
        """
        normalized=normalize_profile(profile)
        try: stamp=float(timestamp)
        except (ValueError,TypeError,OverflowError): return None
        if not normalized or not math.isfinite(stamp) or not 0<=stamp<=253402300799: return None
        finished = None
        if post_urls is not None:
            values = post_urls if isinstance(post_urls, (list, tuple, set, frozenset)) else ()
            finished = {_post_url(url, normalized) for url in values}
            finished.discard('')
            if not finished:
                return self.get(normalized)
        with self._lock:
            records=self.list()
            record=next((item for item in records if item['profile']==normalized),None)
            if record is None: return None
            record['baseline_ts']=(max(record.get('baseline_ts',0.0),stamp) if record.get('initialized') else stamp)
            record['latest_ts']=(max(record.get('latest_ts',0.0),record['baseline_ts'])
                                 if record.get('initialized') else stamp)
            record['initialized']=True
            if finished is not None:
                record['pending_posts']=[item for item in record.get('pending_posts',[]) if item['url'] not in finished]
                record['downloaded_post_urls']=list(dict.fromkeys([*record.get('downloaded_post_urls',[]),*sorted(finished)]))[-MAX_UPDATE_POSTS:]
            record['unread']=bool(record.get('pending_posts') or record['latest_ts']>record['baseline_ts'])
            record['update_count']=len(record.get('pending_posts',[])) or int(record['unread'])
            self._save(records)
            return dict(record)

    def mark_read(self, profile):
        with self._lock:
            record = self.get(profile)
            if not record:
                return
            record["unread"] = False
            record['pending_posts'] = []; record['update_count'] = 0
            record["baseline_ts"] = max(record["baseline_ts"], record.get("latest_ts", 0.0))
            self._save([record if item["profile"] == record["profile"] else item for item in self.list()])

    def unread_count(self):
        return sum(1 for item in self.list() if item.get("unread"))


def fetch_latest(profile, *, timeout=(8, 20), session=None, baseline_ts=None, interrupted=None):
    """Read newest metadata and, for known baselines, each newer post once.

    A brand-new subscription reads only the first page to establish its
    baseline. Existing subscriptions page newest-first until the downloaded
    timestamp; no post detail/image requests are made by background checks.
    """
    normalized = normalize_profile(profile)
    if not normalized:
        raise ValueError("invalid_profile")
    owned = session is None
    session = session or requests.Session()
    session.headers.setdefault("User-Agent", USER_AGENT)
    try:
        latest=0.0; name='Unknown Artist'; avatar=''; posts=[]; signatures=set(); offset=0
        while True:
            if interrupted is not None and interrupted(): raise OSError('cancelled')
            response=session.get(normalized+(f'?o={offset}' if offset else ''),timeout=timeout)
            try:
                response.raise_for_status(); markup=response.text
            finally:
                close=getattr(response,'close',None)
                if callable(close):close()
            if len(markup)>8*1024*1024:raise ValueError('subscription_page_too_large')
            soup=BeautifulSoup(markup,'html.parser')
            cards=soup.select('article.post-card')
            if not offset:
                times=[parse_site_datetime(node.get('datetime')) for node in soup.select('time[datetime]')]
                latest=max((stamp for stamp in times if stamp is not None),default=0.0)
                node=soup.select_one("a.user-header__profile [itemprop='name'], a.user-header__profile span[itemprop='name']")
                name=node.get_text(' ',strip=True)[:160] if node else 'Unknown Artist'
                node=soup.select_one('img.fancy-image__image, .user-header__profile-image img')
                avatar=urljoin(normalized,node.get('src')) if node and node.get('src') else ''
                if not cards and node is None and name=='Unknown Artist' and not latest:
                    raise ValueError('subscription_page_unavailable')
            reached=False; page=[]
            for card in cards:
                link=card.select_one('a[href*="/post/"]'); stamp_node=card.select_one('time[datetime]')
                if link is None or stamp_node is None: continue
                stamp=parse_site_datetime(stamp_node.get('datetime'))
                if stamp is None: continue
                if baseline_ts is not None and stamp<=baseline_ts: reached=True;continue
                title=card.select_one('.post-card__header'); image=card.select_one('img.post-card__image')
                page.append(dict(url=urljoin(normalized,link.get('href','')),
                    title=title.get_text(' ',strip=True) if title else link.get_text(' ',strip=True),
                    date=stamp_node.get('datetime',''),timestamp=stamp,
                    image_url=urljoin(normalized,(image.get('data-src') or image.get('src') or '')) if image else ''))
            signature=tuple(item['url'] for item in page)
            if signature in signatures:break
            signatures.add(signature); posts.extend(page)
            if len(posts)>MAX_UPDATE_POSTS:raise ValueError('subscription_update_limit')
            if baseline_ts is None or reached or len(cards)<50:break
            offset+=50
            if interrupted is not None:
                # Event.wait in the worker allows cancellation during cooldown;
                # plain callers still retain bounded sequential requests.
                deadline=time.monotonic()+random.uniform(1,3)
                while time.monotonic()<deadline:
                    if interrupted():raise OSError('cancelled')
                    time.sleep(min(.1,max(0,deadline-time.monotonic())))
            else:time.sleep(random.uniform(1,3))
        posts=_safe_posts(posts,normalized)
        return {'profile':normalized,'latest_ts':latest,'name':name,'avatar_url':avatar,
                'posts':posts,'update_count':len(posts)}
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
        today=date.today().isoformat()
        enabled=[item for item in self.subscription_store.list() if item.get('enabled',True)]
        records=[]
        results = []
        for record in enabled:
            if record.get('last_check_date')==today:
                payload=dict(profile=record['profile'],latest_ts=record.get('latest_ts',0),
                    updated=bool(record.get('unread')),update_count=record.get('update_count',0),error='',skipped=True)
                results.append(payload); self.progress.emit(record['profile'],payload)
            else: records.append(record)
        if not records:
            self.finished_all.emit(results)
            return
        rng = random.Random(self.seed)
        def check(record):
            if self._stop.is_set():
                return record, None, "cancelled"
            try:
                result = fetch_latest(record["profile"],baseline_ts=record.get('baseline_ts') if record.get('initialized') else None,
                                      interrupted=self._stop.is_set)
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
                done, _ = wait(tuple(pending), timeout=.1, return_when=FIRST_COMPLETED)
                for future in done:
                    record = pending.pop(future)
                    if self._stop.is_set():
                        break
                    result_record, result, error = future.result()
                    payload = result or {"profile": result_record["profile"], "latest_ts": result_record.get("latest_ts", 0.0)}
                    payload["error"] = error
                    is_new = not result_record.get("initialized", False)
                    payload["updated"] = bool(
                        result and not is_new
                        and result.get("latest_ts", 0.0) > result_record.get("baseline_ts", 0.0)
                    )
                    if result:
                        saved=self.subscription_store.set_baseline(
                            result_record["profile"], result.get("latest_ts", 0.0),
                            name=result.get("name") or "Unknown Artist",
                            unread=False if not result_record.get("initialized", False) else payload["updated"],
                            posts=result.get('posts'),check_date=today,
                        )
                        if saved:
                            payload['updated']=saved['unread']; payload['update_count']=saved.get('update_count',0)
                    self.progress.emit(result_record["profile"], payload)
                    results.append(payload)
                    if next_index < len(records) and not self._stop.is_set():
                        if self._stop.wait(rng.uniform(1.0, 3.0)):
                            break
                        record = records[next_index]; next_index += 1
                        pending[pool.submit(check, record)] = record
        self.finished_all.emit(results)


__all__ = [
    "SubscriptionStore", "SubscriptionCheckWorker", "normalize_profile",
    "profile_id", "parse_site_datetime", "fetch_latest",
]
