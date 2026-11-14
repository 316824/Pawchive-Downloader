"""Daily recommendations, bounded lazy previews and explicit attachment downloads."""
from __future__ import annotations

from collections import deque
from datetime import date
import hashlib
import json
import os
import random
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from PySide6.QtCore import (QThread, Signal, Slot, Qt, QSize, QUrl, QTimer,
    QEvent, QPoint, QEasingCurve, QPropertyAnimation, QBuffer, QByteArray,
    QIODevice, QCoreApplication)
from PySide6.QtGui import QPixmap, QDesktopServices, QIcon, QImageReader
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea,
    QVBoxLayout, QFileDialog, QWidget, QGraphicsOpacityEffect, QGraphicsScene,
    QGraphicsView)
from feature_text import LANGUAGES
from ui_i18n import localize_dialog, t


_COPY={
    'recommendations':('今日画师推荐','今日畫師推薦','Today’s artist picks','今日のおすすめ作家'),
    'loading':('正在加载今日热搜…','正在載入今日熱搜…','Loading today’s popular collections…','今日の人気コレクションを読み込み中…'),
    'interrupted':('今日热搜数据流中断，请稍后再试','今日熱搜資料流中斷，請稍後再試','Today’s popular collections are unavailable. Please try again later.','今日の人気データを取得できません。後でもう一度お試しください。'),
    'picks':('今日推荐 · {count} 个图集','今日推薦 · {count} 個圖集','Today’s picks · {count} collections','今日のおすすめ · {count} コレクション'),
    'empty':('今日暂无可用推荐','今日暫無可用推薦','No recommendations are available today','今日のおすすめはまだありません'),
    'artist':('画师 ID {artist_id}','畫師 ID {artist_id}','Artist ID {artist_id}','作家ID {artist_id}'),
    'unknown':('未知','未知','Unknown','不明'),
    'untitled':('未命名图集','未命名圖集','Untitled collection','無題のコレクション'),
    'cover_loading':('加载封面…','載入封面…','Loading cover…','カバーを読み込み中…'),
    'cover_missing':('封面暂不可用','封面暫不可用','Cover unavailable','カバーを表示できません'),
    'preview':('图集预览','圖集預覽','Collection preview','コレクションのプレビュー'),
    'reading':('正在读取图集内容…','正在讀取圖集內容…','Reading collection contents…','コレクションを読み込み中…'),
    'post':('下载此图集','下載此圖集','Download collection','このコレクションをダウンロード'),
    'creator':('搜寻画师全部图集','搜尋畫師全部圖集','Find all artist collections','作家の全コレクションを検索'),
    'attachments':('附件下载','附件下載','Download attachments','添付ファイルをダウンロード'),
    'read_failed':('图集读取失败，请稍后重试','圖集讀取失敗，請稍後重試','Could not read the collection. Please try again later.','コレクションを読み込めません。後で再試行してください。'),
    'images':('共 {count} 张图片','共 {count} 張圖片','{count} images','画像 {count} 枚'),
    'attachment_count':(' · {count} 个附件',' · {count} 個附件',' · {count} attachments',' · 添付ファイル {count} 件'),
    'full_failed':('高清图片读取失败，请稍后重试','高畫質圖片讀取失敗，請稍後重試','Could not load the full-size image. Please try again later.','高解像度画像を読み込めません。後で再試行してください。'),
    'cache_failed':('高清预览缓存无法写入','高畫質預覽快取無法寫入','Could not write the full-size preview cache','高解像度プレビューのキャッシュに書き込めません'),
    'external_title':('浏览器确认附件','瀏覽器確認附件','Confirm attachment in browser','ブラウザーで添付ファイルを確認'),
    'external_question':('此附件需在浏览器中确认，是否打开？','此附件需在瀏覽器中確認，是否開啟？','This attachment must be checked in your browser. Open it now?','この添付ファイルはブラウザーでの確認が必要です。開きますか？'),
    'attachment_choices':('选择附件操作','選擇附件操作','Choose an attachment action','添付ファイルの操作を選択'),
    'download_attachments':('下载 {count} 个站内附件…','下載 {count} 個站內附件…','Download {count} site attachments…','サイト内の添付ファイル {count} 件をダウンロード…'),
    'browser':('浏览器查看：','瀏覽器查看：','View in browser: ','ブラウザーで表示：'),
    'attachment_folder':('选择附件保存位置','選擇附件儲存位置','Choose an attachment save folder','添付ファイルの保存先を選択'),
    'attachment_downloading':('正在下载附件…','正在下載附件…','Downloading attachments…','添付ファイルをダウンロード中…'),
    'attachment_progress':('正在下载附件 {index}/{total}：{name}','正在下載附件 {index}/{total}：{name}','Downloading attachment {index}/{total}: {name}','添付ファイルをダウンロード中 {index}/{total}：{name}'),
    'attachment_done':('附件下载完成：成功 {ok}，失败 {failed}','附件下載完成：成功 {ok}，失敗 {failed}','Attachments finished: {ok} saved, {failed} failed','添付ファイルの処理完了：成功 {ok} 件、失敗 {failed} 件'),
    'external_attachment_requires_browser':('请在浏览器中确认此附件','請在瀏覽器中確認此附件','Check this attachment in your browser','ブラウザーで添付ファイルを確認してください'),
    'attachment_is_error_page':('网站返回了错误页面','網站傳回了錯誤頁面','The site returned an error page','サイトからエラーページが返されました'),
    'attachment_cancelled':('附件下载已取消','附件下載已取消','Attachment download cancelled','添付ファイルのダウンロードを取り消しました'),
    'incomplete_attachment':('附件接收不完整，请重试','附件接收不完整，請重試','The attachment is incomplete; please retry','添付ファイルが不完全です。再試行してください'),
    'empty_attachment':('附件内容为空','附件內容為空','The attachment was empty','添付ファイルが空です'),
    'too_many_name_collisions':('同名文件过多，请选择其它目录','同名檔案過多，請選擇其他目錄','Too many files share this name; choose another folder','同名ファイルが多すぎます。別のフォルダーを選択してください'),
    'unsafe_download_redirect':('附件跳转到了不支持的地址','附件重新導向至不支援的位址','The attachment redirected to an unsupported address','添付ファイルの転送先に対応していません'),
    'too_many_redirects':('网站重定向次数过多','網站重新導向次數過多','The site redirected too many times','サイトの転送回数が多すぎます'),
}


def _copy(language,key,**values):
    index=LANGUAGES.index(language) if language in LANGUAGES else 0
    return _COPY[key][index].format(**values)


def _attachment_error(language,error):
    # Keep server filenames and HTTP diagnostics intact, but translate our
    # internal error codes before showing them to users.
    text=str(error or '')
    for key in ('external_attachment_requires_browser','attachment_is_error_page',
                'attachment_cancelled','incomplete_attachment','empty_attachment',
                'too_many_name_collisions','unsafe_download_redirect','too_many_redirects'):
        text=text.replace(key,_copy(language,key))
    return text

BASE_URL = 'https://pawchive.pw'
POPULAR_URL = BASE_URL + '/posts/popular?date={}&period=day'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) PawchiveDownloader/1.3'
USER_RE = re.compile(r'/user/(\d+)', re.I)
MAX_RECOMMENDATIONS = 10
MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_THUMB_BYTES = 8 * 1024 * 1024
MAX_FULL_BYTES = 24 * 1024 * 1024
_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.bmp', '.tif', '.tiff'}
_CACHE_LOCK = threading.RLock()
_PREVIEW_LOCK = threading.RLock()
_ACTIVE_PREVIEWS = set()


def active_preview_paths():
    """Directories held by open previews; cache sweepers must skip them."""
    with _PREVIEW_LOCK:
        return frozenset(_ACTIVE_PREVIEWS)


def cleanup_inactive_previews(*, temp_root=None):
    """Sweep only owned preview roots, never an active preview or symlink."""
    root = Path(temp_root or tempfile.gettempdir()).resolve()
    removed = 0
    try:
        candidates = list(root.iterdir())
    except OSError:
        return 0
    for path in candidates:
        if (path.is_symlink() or not re.fullmatch(r'pawchive_preview_[A-Za-z0-9_-]{6,64}', path.name)):
            continue
        try:
            resolved = path.resolve()
            with _PREVIEW_LOCK:
                if resolved.parent != root or resolved in _ACTIVE_PREVIEWS or not path.is_dir():
                    continue
                shutil.rmtree(path)
                removed += 1
        except OSError:
            continue
    return removed


def _day_key(day=None):
    value = day.isoformat() if hasattr(day, 'isoformat') else str(day or date.today())
    return date.fromisoformat(value).isoformat()


def popular_url(day=None):
    return POPULAR_URL.format(_day_key(day))


def _pawchive_host(host):
    value = str(host or '').lower()
    return value == 'pawchive.pw' or value.endswith('.pawchive.pw')


def _file_host(host):
    value = str(host or '').lower()
    return value == 'file.pawchive.pw' or bool(re.fullmatch(r'n\d+\.pawchive\.pw', value))


def _safe_url(value, *, host='pawchive.pw', subdomains=False):
    try:
        parsed = urlparse(urljoin(BASE_URL + '/', str(value or '').strip()))
        name = (parsed.hostname or '').lower()
        allowed = _pawchive_host(name) if subdomains else name == host and _pawchive_host(host)
        if (not str(value or '').strip() or parsed.scheme not in {'http', 'https'} or not allowed
                or parsed.username or parsed.password or parsed.port not in {None, 443}):
            return ''
        return parsed._replace(scheme='https', fragment='').geturl()
    except (ValueError, TypeError):
        return ''


def _external_url(value):
    try:
        parsed = urlparse(str(value or ''))
        return str(value) if (parsed.scheme == 'https' and parsed.hostname and not parsed.username
            and not parsed.password and parsed.port in {None, 443}) else ''
    except (ValueError, TypeError):
        return ''


def _creator_id(*values):
    for value in values:
        match = USER_RE.search(str(value or ''))
        if match:
            return match.group(1)
    return ''


def parse_popular_html(html, *, limit=MAX_RECOMMENDATIONS, day=None):
    """Rank-weighted, date-seeded sampling without replacement from page one."""
    if not isinstance(html, str) or len(html.encode('utf-8', 'ignore')) > MAX_HTML_BYTES:
        raise ValueError('popular_page_too_large')
    soup = BeautifulSoup(html, 'html.parser')
    nodes = soup.select('article.post-card') or soup.select("a[href*='/post/']")
    items, seen = [], set()
    for node in nodes:
        anchor = node if node.name == 'a' else node.select_one("a[href*='/post/']")
        post_url = _safe_url(anchor.get('href', '')) if anchor else ''
        if not post_url or '/post/' not in urlparse(post_url).path or post_url in seen:
            continue
        seen.add(post_url)
        title_node = node.select_one(".post-card__header, h2, h3, [itemprop='name']")
        image = node.select_one('img')
        title = title_node.get_text(' ', strip=True) if title_node else ''
        items.append(dict(rank=len(items) + 1, url=post_url,
            title=(title or (image.get('alt') if image else '') or 'Untitled post')[:512],
            image_url=_safe_url(image.get('src', ''), subdomains=True) if image else '',
            artist_id=_creator_id(post_url)))
    rng = random.Random(_day_key(day))
    pool, chosen = list(items), []
    count = min(max(0, int(limit)), len(items))
    while pool and len(chosen) < count:
        weights = [(len(items) + 1 - item['rank']) ** 1.35 for item in pool]
        pick = rng.random() * sum(weights)
        selected = len(pool) - 1
        for index, weight in enumerate(weights):
            pick -= weight
            if pick <= 0:
                selected = index
                break
        chosen.append(pool.pop(selected))
    return chosen


def _cache_directory(parent=None):
    visited = set()
    while parent is not None and id(parent) not in visited:
        visited.add(id(parent))
        store = getattr(parent, 'store', None) or getattr(getattr(parent, 'services', None), 'store', None)
        if getattr(store, 'data_dir', None):
            return Path(store.data_dir)
        parent = getattr(parent, 'parent_window', None) or parent.parent()
    return Path(os.environ.get('LOCALAPPDATA') or Path.home()) / 'PawchiveDownloader'


class DailyRecommendationCache:
    """Persist the first successful selection, even when today's ranking changes."""
    def __init__(self, directory):
        self.path = Path(directory) / 'popular-recommendations.json'

    def load(self, day):
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 256 * 1024:
                return None
            document = json.loads(self.path.read_text(encoding='utf-8'))
            items = document['items']
            if document.get('day') != _day_key(day) or not isinstance(items, list) or not 1 <= len(items) <= 10:
                return None
            urls = set()
            for item in items:
                if (not isinstance(item, dict) or not isinstance(item.get('url'), str)
                        or _safe_url(item['url']) != item['url']
                        or '/post/' not in urlparse(item['url']).path or item['url'] in urls
                        or not isinstance(item.get('title'), str) or len(item['title']) > 512):
                    return None
                urls.add(item['url'])
                item['image_url'] = _safe_url(item.get('image_url'), subdomains=True)
                item['artist_id'] = _creator_id(item['url'])
            return items
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def save(self, day, items):
        if not items:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.pawchive-popular-', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump(dict(day=_day_key(day), items=items), stream, ensure_ascii=False)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def extract_post_assets(html, base_url):
    """Separate image-stream links and declared attachments, including opaque URLs."""
    if len(str(html or '').encode('utf-8', 'ignore')) > MAX_HTML_BYTES:
        raise ValueError('post_page_too_large')
    soup = BeautifulSoup(html or '', 'html.parser')
    images, attachments, seen = [], [], set()
    nodes = soup.select('.post__files a.fileThumb[href], a.post__attachment-link[href]')
    if not nodes:
        nodes = soup.select("a[href^='https://file.pawchive.pw/'], a[href^='https://n2.pawchive.pw/']")
    for node in nodes:
        raw = urljoin(base_url, str(node.get('href') or ''))
        url = _safe_url(raw, subdomains=True)
        declared = 'post__attachment-link' in (node.get('class') or [])
        if not url and declared:
            url = _external_url(raw)
        if not url or url in seen:
            continue
        parsed = urlparse(url)
        name = (str(node.get('download') or '').strip() or
                (parse_qs(parsed.query).get('f') or [''])[0] or Path(parsed.path).name or 'attachment')
        image_stream = 'fileThumb' in (node.get('class') or [])
        if _pawchive_host(parsed.hostname) and not declared and (image_stream or Path(name).suffix.lower() in _IMAGE_EXTENSIONS):
            img = node.select_one('img')
            thumb = _safe_url(urljoin(base_url, str(img.get('src') or '')), subdomains=True) if img else ''
            if not Path(name).suffix:
                name = f'image_{len(images)+1}.jpg'
            images.append(dict(url=url, thumb_url=thumb or url, name=name))
        else:
            attachments.append(dict(url=url, name=name))
        seen.add(url)
    for img in soup.select('img[src]'):
        url = _safe_url(urljoin(base_url, str(img.get('src') or '')), subdomains=True)
        if url and '/img-original/' in url and url not in seen:
            images.append(dict(url=url, thumb_url=url, name=Path(urlparse(url).path).name))
            seen.add(url)
    return images, attachments


def _response(session, url, *, timeout, subdomains=True, file_hosts=False):
    """Validate every redirect before making a request to its destination."""
    current = _safe_url(url, subdomains=subdomains)
    if not current or (file_hosts and not _file_host(urlparse(current).hostname)):
        raise ValueError('invalid_download_url')
    for _ in range(4):
        response = session.get(current, timeout=timeout, stream=True, allow_redirects=False,
            headers={'User-Agent': USER_AGENT, 'Referer': BASE_URL + '/', 'Accept-Encoding': 'identity'})
        if response.status_code not in {301, 302, 303, 307, 308}:
            try:
                response.raise_for_status()
            except Exception:
                response.close()
                raise
            return response
        location = _safe_url(urljoin(current, response.headers.get('location', '')), subdomains=subdomains)
        response.close()
        if not location or (file_hosts and not _file_host(urlparse(location).hostname)):
            raise ValueError('unsafe_download_redirect')
        current = location
    raise ValueError('too_many_redirects')


def _read_bytes(session, url, worker, maximum, *, subdomains=True):
    response = _response(session, url, timeout=(8, 20), subdomains=subdomains)
    try:
        if int(response.headers.get('content-length') or 0) > maximum:
            raise ValueError('response_too_large')
        chunks, total, deadline = [], 0, time.monotonic() + 45
        for chunk in response.iter_content(64 * 1024):
            if worker.isInterruptionRequested() or time.monotonic() > deadline:
                raise InterruptedError('preview_cancelled')
            if chunk:
                total += len(chunk)
                if total > maximum:
                    raise ValueError('response_too_large')
                chunks.append(chunk)
        return b''.join(chunks), response.encoding or 'utf-8'
    finally:
        response.close()


class PopularFetchWorker(QThread):
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, day=None, parent=None, *, cache_dir=None):
        super().__init__(parent)
        self.day = day or date.today()
        self.cache = DailyRecommendationCache(cache_dir or _cache_directory(parent))

    def run(self):
        try:
            with _CACHE_LOCK:
                if self.isInterruptionRequested(): return
                cached = self.cache.load(self.day)
                if cached is not None:
                    if not self.isInterruptionRequested(): self.result.emit(cached)
                    return
                with requests.Session() as session:
                    payload, encoding = _read_bytes(session, popular_url(self.day), self, MAX_HTML_BYTES, subdomains=False)
                items = parse_popular_html(payload.decode(encoding, 'replace'), day=self.day)
                if self.isInterruptionRequested(): return
                try: self.cache.save(self.day, items)
                except OSError: pass
                self.result.emit(items)
        except Exception as error:
            if not self.isInterruptionRequested(): self.failed.emit(str(error)[:240])


class PostAssetsWorker(QThread):
    result = Signal(object, object, str)

    def __init__(self, post_url, parent=None):
        super().__init__(parent)
        self.post_url = _safe_url(post_url)

    def run(self):
        try:
            with requests.Session() as session:
                payload, encoding = _read_bytes(session, self.post_url, self, MAX_HTML_BYTES, subdomains=False)
            images, attachments = extract_post_assets(payload.decode(encoding, 'replace'), self.post_url)
            if not self.isInterruptionRequested(): self.result.emit(images, attachments, '')
        except Exception as error:
            if not self.isInterruptionRequested(): self.result.emit([], [], str(error)[:240])


class ImageBytesWorker(QThread):
    result = Signal(str, object, str)

    def __init__(self, url, parent=None, *, full=False):
        super().__init__(parent)
        self.url, self.full = str(url or ''), bool(full)

    def run(self):
        try:
            with requests.Session() as session:
                payload, _ = _read_bytes(session, self.url, self, MAX_FULL_BYTES if self.full else MAX_THUMB_BYTES)
            if not self.isInterruptionRequested(): self.result.emit(self.url, payload, '')
        except Exception as error:
            if not self.isInterruptionRequested(): self.result.emit(self.url, b'', str(error)[:240])


def _attachment_name(value, index):
    value = str(value or '').replace('\\', '/').split('/')[-1]
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')[:180]
    if not value or value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
            *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        value = f'attachment_{index}' + Path(value).suffix
    return value


def _publish_no_replace(temporary, folder, name):
    """Atomically reserve a destination; never replace an existing user file."""
    base = Path(name)
    for suffix in range(100000):
        target = folder / (name if suffix == 0 else f'{base.stem}_{suffix}{base.suffix}')
        try:
            os.link(temporary, target)
        except FileExistsError:
            continue
        except OSError:
            # FAT/exFAT may not support hard links. O_EXCL still prevents
            # overwrites; only our newly reserved file is removed on failure.
            try:
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(descriptor, 'wb') as output, temporary.open('rb') as source:
                    shutil.copyfileobj(source, output, 128 * 1024)
            except BaseException:
                target.unlink(missing_ok=True)
                raise
        temporary.unlink(missing_ok=True)
        return target
    raise OSError('too_many_name_collisions')


class AttachmentDownloadWorker(QThread):
    progress = Signal(int, int, str)
    result = Signal(int, int, str)

    def __init__(self, attachments, folder, parent=None):
        super().__init__(parent)
        self.attachments, self.folder = list(attachments or []), Path(folder)

    def run(self):
        ok = failed = 0
        errors = []
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with requests.Session() as session:
                for index, item in enumerate(self.attachments, 1):
                    if self.isInterruptionRequested(): break
                    temporary = None
                    name = _attachment_name(item.get('name'), index)
                    try:
                        url = _safe_url(item.get('url'), subdomains=True)
                        if not url or not _file_host(urlparse(url).hostname):
                            raise ValueError('external_attachment_requires_browser')
                        descriptor, path = tempfile.mkstemp(prefix='.pawchive-attachment-', suffix='.part', dir=self.folder)
                        temporary = Path(path)
                        with os.fdopen(descriptor, 'wb') as stream:
                            response = _response(session, url, timeout=(8, 20), file_hosts=True)
                            try:
                                declared, received = int(response.headers.get('content-length') or 0), 0
                                if any(kind in response.headers.get('content-type', '').lower()
                                       for kind in ('text/html', 'application/json')):
                                    raise ValueError('attachment_is_error_page')
                                for chunk in response.iter_content(128 * 1024):
                                    if self.isInterruptionRequested(): raise InterruptedError('attachment_cancelled')
                                    if chunk:
                                        stream.write(chunk); received += len(chunk)
                                if declared and received != declared: raise ValueError('incomplete_attachment')
                                if not received: raise ValueError('empty_attachment')
                            finally:
                                response.close()
                        _publish_no_replace(temporary, self.folder, name)
                        ok += 1
                    except Exception as error:
                        failed += 1; errors.append(f'{name}: {error}')
                    finally:
                        if temporary is not None: temporary.unlink(missing_ok=True)
                    self.progress.emit(index, len(self.attachments), name)
        except Exception as error:
            self.result.emit(ok, len(self.attachments) - ok, str(error)[:240]); return
        self.result.emit(ok, failed, '\n'.join(errors)[:1000])


class _WorkerDialog(QDialog):
    """Retain workers until native finished; never delete a running QThread."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._workers = set()
        self._closed = False
        self._closing_result = None
        self._pending_dispatch = None
        self.finished.connect(self._dispatch_after_closed)
        QCoreApplication.instance().aboutToQuit.connect(self._shutdown_workers)

    def _track(self, worker):
        worker.setParent(None)
        self._workers.add(worker)
        worker.finished.connect(self._worker_finished)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    @Slot()
    def _worker_finished(self):
        worker = self.sender()
        self._workers.discard(worker)
        self._after_worker_finished(worker)
        if self._closing_result is not None and not self._workers:
            result = self._closing_result
            self._closing_result = None
            super().done(result)

    def _after_worker_finished(self, worker):
        pass

    @Slot()
    def _shutdown_workers(self):
        self._cleanup_on_close()
        workers = list(self._workers)
        for worker in workers: worker.requestInterruption()
        for worker in workers: worker.wait()

    def _cleanup_on_close(self):
        pass

    def _close_and_dispatch(self, callback, data):
        self._pending_dispatch = (callback, dict(data))
        self.accept()

    @Slot(int)
    def _dispatch_after_closed(self, result):
        pending, self._pending_dispatch = self._pending_dispatch, None
        if pending is not None and result == QDialog.DialogCode.Accepted:
            callback, data = pending
            if callable(callback): callback(data)

    def done(self, result):
        self._closed = True
        self._closing_result = result
        self._cleanup_on_close()
        for worker in self._workers: worker.requestInterruption()
        if self._workers:
            self.hide()
        else:
            self._closing_result = None
            super().done(result)

    def closeEvent(self, event):
        event.ignore()
        self.reject()


def _preview_pixmap(payload):
    buffer = QBuffer()
    buffer.setData(QByteArray(payload)); buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer); size = reader.size()
    if not size.isValid() or size.width() > 10000 or size.height() > 10000 or size.width() * size.height() > 24000000:
        return QPixmap()
    image = reader.read()
    return QPixmap.fromImage(image) if not image.isNull() else QPixmap()


class RecommendationCard(QPushButton):
    """A translucent ID tag slides from the right edge on hover."""
    def __init__(self, artist_id='', parent=None, *, language='简体中文'):
        super().__init__(parent)
        self.artist_tag = QLabel(_copy(language,'artist',artist_id=artist_id or _copy(language,'unknown')), self)
        self.artist_tag.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.artist_tag.setStyleSheet('QLabel{background:rgba(20,24,36,190);color:#fff;padding:4px 7px;border-radius:7px;}')
        self.artist_tag.adjustSize()
        self._tag_effect = QGraphicsOpacityEffect(self.artist_tag)
        self.artist_tag.setGraphicsEffect(self._tag_effect); self._tag_effect.setOpacity(0)
        self._tag_motion = QPropertyAnimation(self.artist_tag, b'pos', self)
        self._tag_motion.setDuration(180); self._tag_motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._tag_fade = QPropertyAnimation(self._tag_effect, b'opacity', self)
        self._tag_fade.setDuration(180)

    def _tag_position(self):
        return QPoint(self.width() - self.artist_tag.width() - 7,
                      max(7, self.height() - self.artist_tag.height() - 7))

    def resizeEvent(self, event):
        super().resizeEvent(event); self.artist_tag.move(self._tag_position())

    def enterEvent(self, event):
        self.artist_tag.raise_()
        self._tag_motion.stop(); self._tag_fade.stop()
        self._tag_motion.setStartValue(self._tag_position() + QPoint(20, 0))
        self._tag_motion.setEndValue(self._tag_position())
        self._tag_fade.setStartValue(self._tag_effect.opacity()); self._tag_fade.setEndValue(1)
        self._tag_motion.start(); self._tag_fade.start(); super().enterEvent(event)

    def leaveEvent(self, event):
        self._tag_fade.stop()
        self._tag_fade.setStartValue(self._tag_effect.opacity()); self._tag_fade.setEndValue(0)
        self._tag_fade.start(); super().leaveEvent(event)


class PopularRecommendationsDialog(_WorkerDialog):
    def __init__(self, parent=None, *, language='简体中文', on_post=None, on_profile=None):
        super().__init__(parent)
        self.lang, self.on_post, self.on_profile = language, on_post, on_profile
        self.setWindowTitle(_copy(self.lang,'recommendations')); self.resize(860, 640)
        self._covers, self._cover_queue, self._cover_active = {}, deque(), set()
        layout = QVBoxLayout(self)
        self.status = QLabel(_copy(self.lang,'loading')); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.body = QWidget(); self.grid = QGridLayout(self.body); self.grid.setSpacing(12)
        self.scroll.setWidget(self.body); layout.addWidget(self.scroll, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject); layout.addWidget(close)
        localize_dialog(self,self.lang)
        self.load()

    def load(self):
        if self._workers: return
        self._worker = PopularFetchWorker(cache_dir=_cache_directory(self))
        self._worker.result.connect(self._populate); self._worker.failed.connect(self._failed)
        self._track(self._worker)

    @Slot(str)
    def _failed(self, error):
        if not self._closed and self._closing_result is None:
            self.status.setText(_copy(self.lang,'interrupted'))

    @Slot(object)
    def _populate(self, items):
        if self._closed or self._closing_result is not None: return
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        self._covers.clear(); self._cover_queue.clear()
        self.status.setText(_copy(self.lang,'picks',count=len(items)) if items else _copy(self.lang,'empty'))
        for index, data in enumerate(items):
            card = RecommendationCard(data.get('artist_id', ''),language=self.lang)
            card.setCursor(Qt.CursorShape.PointingHandCursor); card.setMinimumSize(245, 180)
            card.setStyleSheet('QPushButton{text-align:left;padding:8px;border-radius:12px;}QPushButton:hover{border:1px solid #b89af4;}')
            box = QVBoxLayout(card)
            image = QLabel(_copy(self.lang,'cover_loading')); image.setAlignment(Qt.AlignmentFlag.AlignCenter); image.setMinimumHeight(110)
            box.addWidget(image)
            title = QLabel(str(data.get('title') or _copy(self.lang,'untitled')))
            title.setTextFormat(Qt.TextFormat.PlainText); title.setWordWrap(True); box.addWidget(title)
            card.clicked.connect(lambda checked=False, item=dict(data): self.open_post(item))
            self.grid.addWidget(card, index // 3, index % 3)
            if data.get('image_url'):
                self._covers.setdefault(data['image_url'], []).append(image)
                if data['image_url'] not in self._cover_queue: self._cover_queue.append(data['image_url'])
        self._pump_covers()

    def _pump_covers(self):
        if self._closed or self._closing_result is not None: return
        while self._cover_queue and len(self._cover_active) < 3:
            url = self._cover_queue.popleft(); worker = ImageBytesWorker(url)
            worker.result.connect(self._cover_ready); self._cover_active.add(worker); self._track(worker)

    @Slot(str, object, str)
    def _cover_ready(self, url, payload, error):
        if self._closed or self._closing_result is not None: return
        pix = _preview_pixmap(payload) if payload else QPixmap()
        for label in self._covers.get(url, []):
            if pix.isNull(): label.setText(_copy(self.lang,'cover_missing'))
            else: label.setPixmap(pix.scaled(220, 110, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

    def _after_worker_finished(self, worker):
        self._cover_active.discard(worker); self._pump_covers()

    def _cleanup_on_close(self):
        self._cover_queue.clear()

    def open_post(self, data):
        dialog = PostPreviewDialog(self, data, language=self.lang,
            on_post=lambda item:self._close_and_dispatch(self.on_post,item),
            on_profile=lambda item:self._close_and_dispatch(self.on_profile,item))
        dialog.exec(); dialog.deleteLater()


class _ZoomView(QGraphicsView):
    def __init__(self, pixmap, parent=None):
        super().__init__(parent)
        scene = QGraphicsScene(self); scene.addPixmap(pixmap); self.setScene(scene)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._initial_fit = True

    def showEvent(self, event):
        super().showEvent(event)
        if self._initial_fit:
            self.fitInView(self.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio); self._initial_fit = False

    def wheelEvent(self, event):
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        if .02 <= self.transform().m11() * factor <= 8: self.scale(factor, factor)
        event.accept()


class PostPreviewDialog(_WorkerDialog):
    def __init__(self, parent, data, *, language='简体中文', on_post=None, on_profile=None):
        super().__init__(parent)
        self.data, self.lang, self.on_post, self.on_profile = dict(data), language, on_post, on_profile
        self.setWindowTitle(str(data.get('title') or _copy(self.lang,'preview'))); self.resize(900, 680)
        self._temp = Path(tempfile.mkdtemp(prefix='pawchive_preview_')).resolve()
        with _PREVIEW_LOCK: _ACTIVE_PREVIEWS.add(self._temp)
        self._images, self._attachments = [], []
        self._thumb_requested, self._image_queue, self._image_active = set(), deque(), {}
        self._full_requested, self._full_cache = {}, {}
        # Owned timers cannot keep a Python bound method alive after Qt has
        # already destroyed the list during a rapid close/reopen sequence.
        self._visible_timer=QTimer(self); self._visible_timer.setSingleShot(True)
        self._visible_timer.timeout.connect(self._load_visible)
        layout = QVBoxLayout(self)
        heading=QLabel(str(data.get('title') or _copy(self.lang,'preview')))
        heading.setTextFormat(Qt.TextFormat.PlainText); heading.setWordWrap(True); layout.addWidget(heading)
        self.status = QLabel(_copy(self.lang,'reading')); layout.addWidget(self.status)
        self.list = QListWidget(); self.list.setIconSize(QSize(150, 120)); self.list.itemClicked.connect(self._download_full)
        self.list.viewport().installEventFilter(self)
        self.list.verticalScrollBar().valueChanged.connect(self._load_visible)
        layout.addWidget(self.list, 1)
        buttons = QHBoxLayout()
        self.download = QPushButton(_copy(self.lang,'post')); self.creator = QPushButton(_copy(self.lang,'creator'))
        self.attachments = QPushButton(_copy(self.lang,'attachments')); self.attachments.hide(); close = QPushButton(t(self.lang,'关闭'))
        for button in (self.download, self.creator, self.attachments): buttons.addWidget(button)
        buttons.addStretch(1); buttons.addWidget(close); layout.addLayout(buttons)
        self.download.clicked.connect(lambda: self._dispatch('post')); self.creator.clicked.connect(lambda: self._dispatch('profile'))
        self.attachments.clicked.connect(self._open_attachment); close.clicked.connect(self.accept)
        self._worker = PostAssetsWorker(data.get('url', ''))
        self._worker.result.connect(self._assets_ready); self._track(self._worker)

    @Slot(object, object, str)
    def _assets_ready(self, images, attachments, error):
        if self._closed or self._closing_result is not None: return
        self._images, self._attachments = list(images or []), list(attachments or [])
        if error:
            self.status.setText(_copy(self.lang,'read_failed')); return
        self.status.setText(_copy(self.lang,'images',count=len(images)) + (_copy(self.lang,'attachment_count',count=len(attachments)) if attachments else ''))
        self.attachments.setVisible(bool(attachments))
        for index, image in enumerate(images):
            item = QListWidgetItem(image.get('name', f'image_{index+1}'))
            item.setData(Qt.ItemDataRole.UserRole, image); item.setSizeHint(QSize(180, 135)); self.list.addItem(item)
        if self.isVisible(): self._load_visible()
        self._visible_timer.start(0)

    def eventFilter(self, obj, event):
        if self._closed: return super().eventFilter(obj,event)
        if (hasattr(self, 'list') and event.type() in {QEvent.Type.Resize, QEvent.Type.Show}
                and obj is self.list.viewport()):
            self._visible_timer.start(0)
        return super().eventFilter(obj, event)

    @Slot()
    def _load_visible(self, *args):
        if self._closed or self._closing_result is not None: return
        viewport = self.list.viewport().rect()
        visible = set()
        for index in range(self.list.count()):
            item = self.list.item(index)
            if not self.list.visualItemRect(item).intersects(viewport): continue
            url = (item.data(Qt.ItemDataRole.UserRole) or {}).get('thumb_url')
            if url: visible.add(url)
        pending = deque()
        for url, full in self._image_queue:
            if full or url in visible:
                pending.append((url, full))
            else:
                self._thumb_requested.discard(url)
        self._image_queue = pending
        for url in sorted(visible):
            if url and url not in self._thumb_requested:
                self._thumb_requested.add(url); self._image_queue.append((url, False))
        self._pump_images()

    def _pump_images(self):
        if self._closed or self._closing_result is not None: return
        while self._image_queue and len(self._image_active) < 3:
            url, full = self._image_queue.popleft(); worker = ImageBytesWorker(url, full=full)
            worker.result.connect(self._image_ready); self._image_active[worker] = (url, full); self._track(worker)

    @Slot(str, object, str)
    def _image_ready(self, url, payload, error):
        if self._closed or self._closing_result is not None: return
        full = self._image_active.get(self.sender(), (url, False))[1]
        pix = _preview_pixmap(payload) if payload else QPixmap()
        if full:
            item = self._full_requested.pop(url, None)
            if pix.isNull():
                self.status.setText(_copy(self.lang,'full_failed')); return
            target = self._temp / hashlib.sha256(url.encode()).hexdigest()
            try: target.write_bytes(payload)
            except OSError:
                self.status.setText(_copy(self.lang,'cache_failed')); return
            self._full_cache[url] = target
            if item is not None: self._show_full(item.text(), pix)
        elif not pix.isNull():
            icon = QIcon(pix.scaled(150, 120, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            for index in range(self.list.count()):
                item = self.list.item(index)
                if (item.data(Qt.ItemDataRole.UserRole) or {}).get('thumb_url') == url: item.setIcon(icon)

    def _after_worker_finished(self, worker):
        self._image_active.pop(worker, None); self._pump_images()

    def _download_full(self, item):
        if self._closed or self._closing_result is not None: return
        data = item.data(Qt.ItemDataRole.UserRole) or {}; url = data.get('url', '')
        cached = self._full_cache.get(url)
        if cached and cached.is_file():
            pix = _preview_pixmap(cached.read_bytes())
            if not pix.isNull(): self._show_full(item.text(), pix)
            return
        if not url or url in self._full_requested: return
        self._full_requested[url] = item; self._image_queue.appendleft((url, True)); self._pump_images()

    def _show_full(self, title, pix):
        viewer = QDialog(self); viewer.lang=self.lang
        viewer.setProperty('pawchiveChromeControls','resize')
        viewer.setWindowTitle(title); viewer.resize(900, 700)
        body = QVBoxLayout(viewer); body.addWidget(_ZoomView(pix, viewer))
        close = QPushButton(t(self.lang,'关闭')); close.clicked.connect(viewer.accept); body.addWidget(close)
        viewer.exec(); viewer.deleteLater()

    def _dispatch(self, kind):
        callback = self.on_post if kind == 'post' else self.on_profile
        if callable(callback): self._close_and_dispatch(callback, self.data)

    def _open_external_attachment(self, item):
        url = _external_url(item.get('url'))
        if not url: return
        choice = QMessageBox.question(self, _copy(self.lang,'external_title'), _copy(self.lang,'external_question')+'\n'+url,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if choice == QMessageBox.StandardButton.Yes: QDesktopServices.openUrl(QUrl(url))

    def _open_attachment(self):
        dialog = QDialog(self); dialog.lang=self.lang
        dialog.setWindowTitle(_copy(self.lang,'attachment_choices')); layout = QVBoxLayout(dialog)
        downloadable = [item for item in self._attachments
                        if _file_host(urlparse(str(item.get('url') or '')).hostname)]
        unknown = [item for item in self._attachments if item not in downloadable]
        if downloadable:
            button = QPushButton(_copy(self.lang,'download_attachments',count=len(downloadable))); layout.addWidget(button)
            button.clicked.connect(lambda: self._start_attachments(downloadable, dialog))
        for item in unknown:
            button = QPushButton(_copy(self.lang,'browser') + str(item.get('name') or item.get('url')))
            button.clicked.connect(lambda checked=False, item=item: self._open_external_attachment(item)); layout.addWidget(button)
        close = QPushButton(t(self.lang,'关闭')); close.clicked.connect(dialog.accept); layout.addWidget(close)
        dialog.exec(); dialog.deleteLater()

    def _start_attachments(self, items, chooser=None):
        folder = QFileDialog.getExistingDirectory(self, _copy(self.lang,'attachment_folder'), '',
            options=QFileDialog.Option.DontUseNativeDialog)
        if not folder: return
        if chooser: chooser.accept()
        self.attachments.setEnabled(False); self.status.setText(_copy(self.lang,'attachment_downloading'))
        worker = AttachmentDownloadWorker(items, folder); self._attachment_worker = worker
        worker.progress.connect(self._attachment_progress); worker.result.connect(self._attachment_done); self._track(worker)

    @Slot(int, int, str)
    def _attachment_progress(self, index, total, name):
        if not self._closed and self._closing_result is None: self.status.setText(_copy(self.lang,'attachment_progress',index=index,total=total,name=name))

    @Slot(int, int, str)
    def _attachment_done(self, ok, failed, error):
        if not self._closed and self._closing_result is None:
            self.attachments.setEnabled(True)
            self.status.setText(_copy(self.lang,'attachment_done',ok=ok,failed=failed) + ('\n'+_attachment_error(self.lang,error) if error else ''))

    def _cleanup_on_close(self):
        self._visible_timer.stop()
        self._image_queue.clear(); self._full_requested.clear(); self._full_cache.clear()
        shutil.rmtree(self._temp, ignore_errors=True)
        with _PREVIEW_LOCK: _ACTIVE_PREVIEWS.discard(self._temp)
        cleanup_inactive_previews()


__all__ = ['popular_url', 'parse_popular_html', 'extract_post_assets', 'DailyRecommendationCache',
    'PopularFetchWorker', 'PostAssetsWorker', 'PopularRecommendationsDialog', 'PostPreviewDialog',
    'active_preview_paths', 'cleanup_inactive_previews']
