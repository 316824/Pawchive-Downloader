from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote, parse_qs
from html import escape
import re
import time
import tempfile
import shutil
import sys, random, math, uuid
import requests
from bs4 import BeautifulSoup
from PySide6.QtCore import QEvent, Qt, QThread, Signal, QTimer, QPointF, QRect, QRectF, QEasingCurve, QPropertyAnimation, Property, QSettings, QUrl, QSize
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QRadialGradient, QFont, QPixmap, QIcon, QLinearGradient, QGradient, QFontMetricsF, QPainterPath, QDesktopServices
from PySide6.QtWidgets import QStyle, QStyleOptionButton
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QFileDialog, QComboBox, QTextEdit, QFrame,
    QAbstractButton, QCheckBox, QDialog, QDialogButtonBox,
    QScrollArea, QGridLayout, QSpinBox, QSizePolicy, QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QListWidget, QListWidgetItem, QInputDialog,
    QMessageBox)
from background_store import BackgroundStore
from background_ui import BackgroundLayer, BackgroundSettingsDialog, load_preview
from desktop_visuals import rounded_icon
from link_utils import LinkInfo, extract_link
from download_scheduler import DownloadScheduler
from recycle_bin import RecycleBin
from windows_taskbar import TaskbarProgress
from window_chrome import install_window_chrome
from ui_i18n import language_for, t as ui_text

BASE_URL = 'https://pawchive.pw'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36'
PAGE_SIZE = 50
STALE_CACHE_AGE_SECONDS = 24 * 60 * 60
PROFILE_URL_RE = re.compile(
    r'https://pawchive\.pw/(?:fanbox|patreon|subscribestar|discord|boosty)/user/[^/?#]+/?'
)
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
IMAGE_EXTENSIONS = frozenset({
    '.avif', '.bmp', '.gif', '.heic', '.jpeg', '.jpg', '.png', '.tif',
    '.tiff', '.webp',
})
VIDEO_EXTENSIONS = frozenset({
    '.avi', '.flv', '.m4v', '.mkv', '.mov', '.mp4', '.mpeg', '.mpg',
    '.webm', '.wmv',
})
IMAGE_FOLDER = '图片文件'
VIDEO_FOLDER = '视频文件'
OTHER_FOLDER = '其它文件'
ATTACHMENT_FOLDER = '附件文件'


def attachment_category(name):
    suffix = Path(str(name or '')).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return 'image'
    if suffix in VIDEO_EXTENSIONS:
        return 'video'
    return 'other'


def attachment_name(anchor, fallback=''):
    """Return the filename advertised by a Pawchive attachment anchor.

    Pawchive uses two link shapes: image ``fileThumb`` links often carry a
    filename in the ``f`` query parameter, while downloadable attachments
    expose it through ``download`` or their visible label. The URL path is
    frequently an opaque hash, so looking only at its suffix loses the file
    category and makes the queue treat images as unknown attachments.
    """
    if anchor is None:
        return str(fallback or '')
    href = str(anchor.get('href') or '').strip()
    candidates = [str(anchor.get('download') or '').strip()]
    try:
        candidates.append((parse_qs(urlparse(href).query).get('f') or [''])[0].strip())
    except (TypeError, ValueError):
        pass
    candidates.append(str(fallback or '').strip())
    text = re.sub(r'^Download\s+', '', anchor.get_text(' ', strip=True), flags=re.I)
    candidates.append(text)
    try:
        candidates.append(Path(unquote(urlparse(href).path)).name)
    except (TypeError, ValueError):
        pass
    for value in candidates:
        value = unquote(str(value or '')).strip()
        if value and value not in {'#', '/'}:
            return value
    return ''


def attachment_info(anchor, index=1):
    """Normalize one HTML attachment into name, category and direct flag."""
    href = str(anchor.get('href') or '').strip() if anchor is not None else ''
    direct = bool(anchor is not None and 'post__attachment-link' in (anchor.get('class') or []))
    fallback = f'image_{index}.jpg' if not direct else f'attachment_{index}.bin'
    name = attachment_name(anchor, fallback=fallback)
    # ``fileThumb`` is the site's image stream even when its URL is an
    # opaque hash or the filename is absent.  ``post__attachment-link`` is
    # the separate direct-file stream and stays serial/downloadable as an
    # attachment even when somebody names it with an image suffix.
    category = 'image' if not direct else attachment_category(name)
    if direct and category == 'image':
        category = 'other'
    suffix = Path(name).suffix.lower()
    if not suffix:
        suffix = Path(unquote(urlparse(href).path)).suffix.lower()
    return name, category, direct, suffix


def cleanup_stale_cache_dirs(current=None, *, max_age=STALE_CACHE_AGE_SECONDS, now=None):
    """Remove old Pawchive preview directories left by interrupted runs.

    Only directories created by this application directly below the system
    temporary directory are considered.  The active run's directory is always
    preserved, recent directories are left alone, and junctions/symlinks are
    skipped so a cleanup click cannot traverse outside the temp root.
    Download folders and ``.part`` resume files are unrelated to this cache
    and are deliberately never touched here.
    """
    try:
        root = Path(tempfile.gettempdir()).resolve()
    except (OSError, RuntimeError):
        return 0
    try:
        active = Path(current).resolve() if current else None
    except (OSError, RuntimeError, TypeError):
        active = None
    try:
        timestamp = time.time() if now is None else float(now)
    except (TypeError, ValueError, OverflowError):
        timestamp = time.time()
    try:
        threshold = max(0.0, float(max_age))
    except (TypeError, ValueError, OverflowError):
        threshold = float(STALE_CACHE_AGE_SECONDS)
    protected=set()
    popular=sys.modules.get('popular_features')
    if popular is not None:
        getter=getattr(popular,'active_preview_paths',None)
        if callable(getter): protected={Path(path).resolve() for path in getter()}
    removed = 0
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0
    for entry in entries:
        # Only touch names created by Pawchive in the system temp root.  This
        # keeps the startup sweep useful without ever scanning arbitrary user
        # files or following a junction/symlink outside the root.
        owned_dir = entry.is_dir() and entry.name.startswith(('pawchive_cache_', 'pawchive_preview_'))
        owned_file = entry.is_file() and entry.name.startswith('pawchive_') and entry.suffix.lower() in {'.tmp', '.part', '.json'}
        if not owned_dir and not owned_file:
            continue
        if entry.is_symlink() or getattr(entry, 'is_junction', lambda: False)():
            continue
        try:
            resolved = entry.resolve()
            if resolved.parent != root or resolved in protected or (active is not None and resolved == active):
                continue
            age = timestamp - entry.stat().st_mtime
            if age < threshold:
                continue
            if owned_dir:
                shutil.rmtree(entry)
            else:
                entry.unlink(missing_ok=True)
        except (OSError, RuntimeError, ValueError):
            continue
        removed += 1
    return removed


def absolute_post_url(href):
    """Normalize a post link found in a card to an absolute Pawchive URL."""
    href = (href or '').strip()
    if not href:
        return ''
    parsed = urlparse(urljoin(f'{BASE_URL}/', href))
    if parsed.hostname != 'pawchive.pw' or parsed.scheme not in {'http', 'https'}:
        return ''
    return parsed._replace(scheme='https').geturl()


def safe_path_component(value, fallback='post', limit=180):
    """Make a title or attachment name safe as a single Windows path component."""
    value = str(value or '').replace('\\', '/').rsplit('/', 1)[-1]
    value = INVALID_FILENAME_RE.sub('_', value).strip(' .')
    if not value:
        value = fallback
    stem = value.split('.', 1)[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        value = f'_{value}'
    if len(value) > limit:
        suffix = Path(value).suffix
        value = value[:max(1, limit - len(suffix))].rstrip(' .') + suffix
    return value or fallback


def unique_path(directory, name):
    """Return a non-existing path by adding a numeric suffix before its extension."""
    directory=Path(directory)
    candidate=directory/name
    if not candidate.exists():
        return candidate
    base=Path(name)
    number=2
    while True:
        candidate=directory/f'{base.stem} ({number}){base.suffix}'
        if not candidate.exists():
            return candidate
        number+=1


def valid_download(path, extension, content_type=''):
    """Reject successful HTTP challenge pages and obvious file/signature mismatches."""
    ctype=(content_type or '').lower()
    if 'text/html' in ctype or 'application/json' in ctype:
        return False
    try:
        with open(path,'rb') as f: head=f.read(32)
        if not head: return False
        if head.lstrip().lower().startswith((b'<!doctype html',b'<html',b'<?xml')): return False
    except OSError: return False
    ext=extension.lower()
    checks={
        '.jpg':lambda b:b.startswith(b'\xff\xd8\xff'), '.jpeg':lambda b:b.startswith(b'\xff\xd8\xff'),
        '.png':lambda b:b.startswith(b'\x89PNG\r\n\x1a\n'), '.gif':lambda b:b.startswith((b'GIF87a',b'GIF89a')),
        '.webp':lambda b:b.startswith(b'RIFF') and b[8:12]==b'WEBP', '.zip':lambda b:b.startswith((b'PK\x03\x04',b'PK\x05\x06',b'PK\x07\x08')),
        '.7z':lambda b:b.startswith(b'7z\xbc\xaf\x27\x1c'), '.rar':lambda b:b.startswith((b'Rar!\x1a\x07\x00',b'Rar!\x1a\x07\x01\x00')),
        '.pdf':lambda b:b.startswith(b'%PDF-'), '.psd':lambda b:b.startswith(b'8BPS'),
        '.mp4':lambda b:len(b)>=8 and b[4:8]==b'ftyp', '.m4v':lambda b:len(b)>=8 and b[4:8]==b'ftyp',
        '.mov':lambda b:len(b)>=8 and b[4:8]==b'ftyp', '.webm':lambda b:b.startswith(b'\x1aE\xdf\xa3'),
        '.mkv':lambda b:b.startswith(b'\x1aE\xdf\xa3')
    }
    checker=checks.get(ext)
    return checker(head) if checker else True
TEXT = {
 '简体中文': {'title':'Pawchive 图片下载器','browse':'浏览…','start':'开始下载','ready':'准备就绪','working':'正在获取帖子附件…','done':'下载完成','open_folder':'打开文件夹','empty':'没有找到原图附件。','choose':'选择文件夹','error':'出错了'},
 'English': {'title':'Pawchive Image Downloader','browse':'Browse…','start':'Download images','ready':'Ready','working':'Finding post attachments…','done':'Download complete','open_folder':'Open folder','empty':'No original attachment links were found.','choose':'Choose a folder','error':'Something went wrong'},
 '日本語': {'title':'Pawchive 画像ダウンローダー','browse':'参照…','start':'ダウンロード','ready':'準備完了','working':'添付画像を確認中…','done':'ダウンロード完了','open_folder':'フォルダーを開く','empty':'元画像のリンクが見つかりません。','choose':'フォルダーを選択','error':'エラー'},
 '繁體中文': {'title':'Pawchive 圖片下載器','browse':'瀏覽…','start':'開始下載','ready':'準備就緒','working':'正在取得貼文附件…','done':'下載完成','open_folder':'開啟資料夾','empty':'找不到原圖附件。','choose':'選擇資料夾','error':'發生錯誤'}
}
AUX_TEXT = {
 '简体中文': {'profile_hint':'请输入画师主页链接。','profile_valid':'✓ 画师主页链接有效','folder_needed':'✓ 链接有效；请先选择图片保存文件夹','profile_invalid':'链接格式无效：请输入 pawchive.pw 下的完整画师主页链接。','note_titles':'默认：每个图集分别保存到以标题命名的文件夹中。','note_flat':'已关闭分文件夹；可选择按发布日期排序并为图片编号。','scan_status':'正在扫描画师主页标题…','scan_failed':'扫描失败','empty_scan':'没有扫描到完整图集标题。','close_wait':'正在停止并清理未完成的图集…','cache_cleared':'临时缓存和未使用背景已清理；下载文件未受影响。','cache_failed':'缓存清理失败：{}','log_copied':'日志已复制到剪贴板。','copy_tooltip':'复制日志','cache_tooltip':'清理临时缓存和未使用背景','choose_title':'选择要下载的图集','choose_summary':'扫描到 {count} 个图集。点击封面卡片选择。','select_all':'全选','select_none':'全不选','cover_missing':'封面暂不可用','no_direct':'无直链文件','back_top':'回到顶部','ok':'确定','cancel':'取消','scan_window':'正在寻找图集','scan_searching':'正在沿着画师主页寻找图集…','scan_paused':'扫描已暂停','scan_finishing':'正在完成当前图集…','scan_count':'已扫描 {pages} 页 · 找到 {posts} 个图集','unknown':'未知','cancelled':'用户已停止下载','bad_content':'响应内容与文件类型不匹配（Content-Type: {content_type}）','download_failed':'下载失败（已重试4次）：{title} / {filename} — {error}','album_failed':'下载中止：图集“{title}”的文件连续失败，已停止以避免留下不完整图集。','download_fail_prefix':'下载失败','no_selected':'没有找到所选图集。','pause':'暂停','resume':'继续','finish':'结束扫描','file_progress':'正在下载{count}：{name} · {current:.1f}/{total:.1f} MB','unknown_progress':'正在下载{count}：{name} · 已接收 {current:.1f} MB'},
 '繁體中文': {'profile_hint':'請輸入畫師首頁連結。','profile_valid':'✓ 畫師首頁連結有效','folder_needed':'✓ 連結有效；請先選擇圖片儲存資料夾','profile_invalid':'連結格式無效：請輸入 pawchive.pw 下的完整畫師首頁連結。','note_titles':'預設：每篇圖集分別存入以標題命名的資料夾。','note_flat':'已關閉分資料夾；可選擇按發布日期排序並為圖片編號。','scan_status':'正在掃描畫師首頁標題…','scan_failed':'掃描失敗','empty_scan':'沒有掃描到完整圖集標題。','close_wait':'正在停止並清理未完成的圖集…','cache_cleared':'暫存快取及未使用背景已清理；下載檔案未受影響。','cache_failed':'快取清理失敗：{}','log_copied':'日誌已複製到剪貼簿。','copy_tooltip':'複製日誌','cache_tooltip':'清理暫存快取及未使用背景','choose_title':'選擇要下載的圖集','choose_summary':'掃描到 {count} 個圖集。點擊封面卡片選擇。','select_all':'全選','select_none':'全不選','cover_missing':'封面暫不可用','no_direct':'無直連檔案','back_top':'回到頂端','ok':'確定','cancel':'取消','scan_window':'正在尋找圖集','scan_searching':'正在沿著畫師首頁尋找圖集…','scan_paused':'掃描已暫停','scan_finishing':'正在完成目前圖集…','scan_count':'已掃描 {pages} 頁 · 找到 {posts} 個圖集','unknown':'未知','cancelled':'使用者已停止下載','bad_content':'回應內容與檔案類型不符（Content-Type: {content_type}）','download_failed':'下載失敗（已重試4次）：{title} / {filename} — {error}','album_failed':'下載中止：圖集「{title}」的檔案連續失敗，已停止以避免留下不完整圖集。','download_fail_prefix':'下載失敗','no_selected':'找不到所選圖集。','pause':'暫停','resume':'繼續','finish':'結束掃描','file_progress':'正在下載{count}：{name} · {current:.1f}/{total:.1f} MB','unknown_progress':'正在下載{count}：{name} · 已接收 {current:.1f} MB'},
 'English': {'profile_hint':'Enter a creator profile URL.','profile_valid':'✓ Creator profile URL is valid','folder_needed':'✓ URL is valid; choose a save folder','profile_invalid':'Invalid URL. Enter a complete creator profile link from pawchive.pw.','note_titles':'Default: save each post in a folder named after its title.','note_flat':'Post folders are off; you can number files by publication date.','scan_status':'Scanning creator posts…','scan_failed':'Scan failed','empty_scan':'No complete posts were found.','close_wait':'Stopping and cleaning up the unfinished post…','cache_cleared':'Temporary cache and unused backgrounds cleared; downloaded files were not changed.','cache_failed':'Could not clear cache: {}','log_copied':'Log copied to the clipboard.','copy_tooltip':'Copy log','cache_tooltip':'Clear temporary cache and unused backgrounds','choose_title':'Choose posts to download','choose_summary':'Found {count} posts. Click cover cards to select.','select_all':'Select all','select_none':'Select none','cover_missing':'Cover unavailable','no_direct':'No direct-link files','back_top':'Back to top','ok':'OK','cancel':'Cancel','scan_window':'Finding posts','scan_searching':'Searching creator posts…','scan_paused':'Scan paused','scan_finishing':'Finishing the current post…','scan_count':'Scanned {pages} pages · found {posts} posts','unknown':'unknown','cancelled':'Download stopped by the user','bad_content':'The response does not match the file type (Content-Type: {content_type})','download_failed':'Download failed after 4 attempts: {title} / {filename} — {error}','album_failed':'Stopped because a file in post “{title}” failed repeatedly. The incomplete post was removed.','download_fail_prefix':'Download failed','no_selected':'The selected posts could not be found.','pause':'Pause','resume':'Resume','finish':'Finish scan','file_progress':'Downloading{count}: {name} · {current:.1f}/{total:.1f} MB','unknown_progress':'Downloading{count}: {name} · {current:.1f} MB received'},
 '日本語': {'profile_hint':'クリエイターページのURLを入力してください。','profile_valid':'✓ クリエイターページURLは有効です','folder_needed':'✓ URLは有効です。保存先を選択してください','profile_invalid':'URLが無効です。pawchive.pw の完全なクリエイターページURLを入力してください。','note_titles':'既定：投稿ごとにタイトル名のフォルダーへ保存します。','note_flat':'投稿別フォルダーは無効です。公開日順に番号を付けられます。','scan_status':'クリエイターの投稿をスキャン中…','scan_failed':'スキャンに失敗しました','empty_scan':'完全な投稿が見つかりませんでした。','close_wait':'停止して未完了の投稿を削除しています…','cache_cleared':'一時キャッシュと未使用背景を削除しました。ダウンロード済みファイルは変更されていません。','cache_failed':'キャッシュを削除できませんでした：{}','log_copied':'ログをクリップボードにコピーしました。','copy_tooltip':'ログをコピー','cache_tooltip':'一時キャッシュと未使用背景を削除','choose_title':'ダウンロードする投稿を選択','choose_summary':'{count} 件の投稿が見つかりました。カバーをクリックして選択します。','select_all':'すべて選択','select_none':'選択解除','cover_missing':'カバーを表示できません','no_direct':'直接リンクのファイルなし','back_top':'先頭へ戻る','ok':'決定','cancel':'キャンセル','scan_window':'投稿を検索中','scan_searching':'クリエイターの投稿を検索中…','scan_paused':'スキャンを一時停止しました','scan_finishing':'現在の投稿を完了しています…','scan_count':'{pages} ページを確認 · {posts} 件の投稿','unknown':'不明','cancelled':'ユーザーがダウンロードを停止しました','bad_content':'応答内容がファイル形式と一致しません（Content-Type: {content_type}）','download_failed':'4回再試行しましたが失敗しました：{title} / {filename} — {error}','album_failed':'投稿「{title}」のファイルが連続して失敗したため中止しました。未完了の投稿を削除しました。','download_fail_prefix':'ダウンロード失敗','no_selected':'選択した投稿が見つかりません。','pause':'一時停止','resume':'再開','finish':'スキャン終了','file_progress':'ダウンロード中{count}：{name} · {current:.1f}/{total:.1f} MB','unknown_progress':'ダウンロード中{count}：{name} · {current:.1f} MB 受信済み'}
}

class _LegacyWorker(QThread):
    progress = Signal(str, int, int)
    file_progress = Signal(int, int)
    finished_ok = Signal(int)
    failed = Signal(str)
    def __init__(self, folder, profile, order, by_title, selected_post_urls=None, rename_order=False, start_number=1, lang='简体中文'):
        super().__init__()
        self.folder = Path(folder)
        self.profile = profile.rstrip('/')
        self.order = order
        self.by_title = by_title
        self.selected_post_urls = set(selected_post_urls or [])
        self.rename_order = rename_order
        self.start_number = start_number
        self.text = AUX_TEXT[lang]
        self.current_album = None
        self.created_album = False
        self.completed_records = []
    def wait_interruptibly(self,seconds):
        deadline=time.monotonic()+seconds
        while not self.isInterruptionRequested():
            remaining=deadline-time.monotonic()
            if remaining<=0: return
            time.sleep(min(.1,remaining))
        raise RuntimeError(self.text['cancelled'])
    def run(self):
        current_temp=None
        session=None
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            s=session=requests.Session(); s.headers['User-Agent']=USER_AGENT
            posts=[]; offset=0; visited=set()
            while True:
                if self.isInterruptionRequested(): raise RuntimeError(self.text['cancelled'])
                page=f'{self.profile}?o={offset}' if offset else self.profile
                r=s.get(page,timeout=30); r.raise_for_status(); soup=BeautifulSoup(r.text,'html.parser')
                cards=soup.select('article.post-card')
                if not cards: break
                page_post_count=len(posts)
                for card in cards:
                    a=card.select_one('a[href*="/post/"]'); post_time=card.select_one('time.timestamp[datetime]')
                    if not a or not post_time: continue
                    post_url=absolute_post_url(a.get('href'))
                    if not post_url: continue
                    if post_url in visited: continue
                    visited.add(post_url); posts.append((post_time.get('datetime',''),post_url,card.select_one('.post-card__header').get_text(' ',strip=True) if card.select_one('.post-card__header') else 'post'))
                if len(cards)<PAGE_SIZE or len(posts)==page_post_count: break
                offset+=PAGE_SIZE
            if not posts: raise RuntimeError('EMPTY')
            if self.selected_post_urls:
                selected={str(url).rstrip('/') for url in self.selected_post_urls}
                posts=[p for p in posts if p[1].rstrip('/') in selected]
            if not posts: raise RuntimeError('NO_SELECTED_POSTS')
            posts.sort(key=lambda x:x[0],reverse=self.order=='newest')
            saved=0; total=len(posts); downloaded=[]; owners={}; duplicate_names=set()
            for post_index,(dt,post_url,title) in enumerate(posts,1):
                if self.isInterruptionRequested(): raise RuntimeError(self.text['cancelled'])
                r=s.get(post_url,timeout=30); r.raise_for_status(); soup=BeautifulSoup(r.text,'html.parser')
                title_el=soup.select_one('h1.post__title span')
                if title_el: title=title_el.get_text(' ',strip=True)
                safe=safe_path_component(title, limit=100)
                album_dest=self.folder/safe if self.by_title else self.folder
                self.created_album=self.by_title and not album_dest.exists(); self.current_album=album_dest if self.by_title else None
                album_dest.mkdir(parents=True,exist_ok=True)
                links=[]; link_urls=set()
                for a in soup.select('.post__files a.fileThumb[href], a.post__attachment-link[href]'):
                    u=a.get('href','').strip()
                    direct='post__attachment-link' in (a.get('class') or [])
                    if u.startswith(('https://file.pawchive.pw/','https://n2.pawchive.pw/')) and u not in link_urls:
                        name=attachment_name(a, fallback=f'image_{len(links) + 1}.jpg' if not direct else f'attachment_{len(links) + 1}.bin')
                        link_urls.add(u); links.append((u,name,direct))
                for j,(url,name,direct) in enumerate(links,1):
                    if j == 1:
                        self.file_progress.emit(0, len(links))
                    name=safe_path_component(unquote(name or Path(urlparse(url).path).name), fallback=f'image_{j}.jpg' if not direct else f'attachment_{j}.bin')
                    # The fileThumb class is authoritative for image
                    # attachments even when the generated fallback name has
                    # a ``.bin`` suffix; direct links remain non-image files.
                    category='image' if not direct else attachment_category(name)
                    if self.by_title:
                        # Keep images directly under the title folder while
                        # placing explicit direct-link attachments in a
                        # dedicated child folder. This preserves the familiar
                        # title layout and makes attachment downloads easy to
                        # distinguish from the image stream.
                        dest=album_dest / ATTACHMENT_FOLDER if direct else album_dest
                    elif category == 'image':
                        dest=self.folder/IMAGE_FOLDER
                    else:
                        group=VIDEO_FOLDER if category == 'video' else OTHER_FOLDER
                        dest=self.folder/group/safe
                    dest.mkdir(parents=True,exist_ok=True)
                    target=dest/name
                    if not self.by_title and name in duplicate_names:
                        target=unique_path(dest,safe_path_component(f'({safe}) - {name}',fallback=name,limit=240))
                    elif target.exists():
                        previous_album=owners.get(name)
                        if direct and not self.by_title and previous_album is not None and previous_album!=post_url:
                            duplicate_names.add(name)
                            for record_index,(saved_date,previous_path,album_url,album_title,original_name) in enumerate(downloaded):
                                if original_name!=name: continue
                                prefixed=safe_path_component(f'({album_title}) - {previous_path.name}',fallback=previous_path.name,limit=240)
                                renamed_path=unique_path(dest,prefixed)
                                previous_path.rename(renamed_path)
                                owners.pop(previous_path.name,None); owners[renamed_path.name]=album_url
                                downloaded[record_index]=(saved_date,renamed_path,album_url,album_title,original_name)
                            target=unique_path(dest,safe_path_component(f'({safe}) - {name}',fallback=name,limit=240))
                        elif direct and not self.by_title and previous_album is None:
                            target=unique_path(dest,safe_path_component(f'({safe}) - {name}',fallback=name,limit=240))
                        else:
                            target=unique_path(dest,name)
                    temp=target.with_name(target.name+'.part'); current_temp=temp
                    ext=Path(urlparse(url).path).suffix.lower()
                    # Large/non-image direct attachments are more likely to be rate limited.
                    # Keep them serial and add a modest randomized cooldown between attempts.
                    direct_file=category != 'image'
                    if direct_file and saved:
                        self.wait_interruptibly(random.uniform(1.8,3.8))
                    last_error=None; content_type=''
                    for attempt in range(1,5):
                        try:
                            if direct_file and attempt>1: self.wait_interruptibly(min(18,3*(2**(attempt-2))+random.uniform(.5,2.5)))
                            headers={'Referer':post_url,'Accept':'*/*'}
                            with s.get(url,stream=True,timeout=(20,45),headers=headers) as image:
                                image.raise_for_status()
                                content_type=image.headers.get('content-type','')
                                size=int(image.headers.get('content-length') or 0); received=0
                                self.progress.emit(f'({post_index}/{total}) {safe} / {target.name}',0,size)
                                last_progress=0.0
                                with temp.open('wb') as f:
                                    for chunk in image.iter_content(131072):
                                        if self.isInterruptionRequested(): raise RuntimeError(self.text['cancelled'])
                                        if chunk:
                                            f.write(chunk); received+=len(chunk)
                                            now=time.monotonic()
                                            if now-last_progress>=.08:
                                                self.progress.emit(f'({post_index}/{total}) {safe} / {target.name}',received,size)
                                                last_progress=now
                                self.progress.emit(f'({post_index}/{total}) {safe} / {target.name}',received,size)
                            if not valid_download(temp,Path(target.name).suffix,content_type):
                                raise ValueError(self.text['bad_content'].format(content_type=content_type or self.text['unknown']))
                            temp.replace(target); last_error=None; break
                        except (requests.RequestException, OSError, ValueError) as exc:
                            last_error=exc
                            try: temp.unlink(missing_ok=True)
                            except OSError: pass
                            if attempt<4: self.wait_interruptibly(1.5*attempt)
                    if last_error is not None:
                        self.progress.emit(self.text['download_failed'].format(title=safe,filename=target.name,error=last_error),len(visited),total)
                        raise RuntimeError(self.text['album_failed'].format(title=safe))
                    saved+=1; downloaded.append((dt,target,post_url,safe,name)); self.completed_records=list(downloaded); owners[target.name]=post_url; current_temp=None; self.progress.emit(f'✓  ({post_index}/{total}) {safe} / {target.name}',1,1); self.file_progress.emit(j, len(links))
                self.current_album=None; self.created_album=False
            if self.rename_order:
                # Number images only.  In title-folder mode each album gets
                # its own sequence; in categorized flat mode all images share
                # 图片文件 and therefore one global date order.
                groups={}
                for item in downloaded:
                    path=item[1]
                    if attachment_category(path.name) != 'image':
                        continue
                    directory=path.parent if self.by_title else self.folder/IMAGE_FOLDER
                    groups.setdefault(directory,[]).append(item)
                for directory,items in groups.items():
                    directory.mkdir(parents=True,exist_ok=True)
                    staged=[]
                    for _,path,_,_,_ in sorted(items,key=lambda item:(item[0],str(item[1]))):
                        extension=path.suffix
                        temporary=path.with_name(f'.pawchive-rename-{uuid.uuid4().hex}{extension}')
                        path.rename(temporary); staged.append((temporary,extension))
                    for index,(temporary,extension) in enumerate(staged,self.start_number):
                        renamed=directory/f'{index}{extension}'
                        bump=1
                        while renamed.exists():
                            renamed=directory/f'{index}_{bump}{extension}'; bump+=1
                        temporary.rename(renamed)
            self.completed_records=list(downloaded)
            self.finished_ok.emit(saved)
        except Exception as e:
            if current_temp:
                try: current_temp.unlink(missing_ok=True)
                except OSError: pass
            if self.current_album and self.created_album:
                shutil.rmtree(self.current_album,ignore_errors=True)
            self.failed.emit(str(e))
        finally:
            if session is not None:
                session.close()


class Worker(_LegacyWorker):
    # Python integers keep multi-gigabyte ZIP/video progress out of Qt's int32 range.
    progress = Signal(str,object,object)
    def __init__(self,folder,profile,order,by_title,selected_post_urls=None,rename_order=False,
                 start_number=1,lang='简体中文',journal=None,resume_job_id=None,layout_version=0,content_mode='all',resume_config=None,record_artist=True):
        self.journal=journal
        self.preserve_on_interrupt=False
        self.resume_saved_count=0
        if journal is not None and resume_job_id:
            if resume_config is None:
                job=journal.load(resume_job_id); config=job['config']
                saved_count=job.get('saved_count',0)
            else:
                config=dict(resume_config); saved_count=config.pop('_saved_count',0)
            folder=config['folder']; profile=config['profile']; order=config.get('order','newest')
            by_title=config.get('by_title',True); rename_order=config.get('rename_order',False)
            start_number=config.get('start_number',1); selected_post_urls=config.get('selected_post_urls')
            lang=config.get('lang',lang)
            self.resume_saved_count=saved_count
        super().__init__(folder,profile,order,by_title,selected_post_urls,rename_order,start_number,lang)
        self.folder=self.folder.resolve()
        self.job_id=None
        if journal is not None:
            self.job_id=resume_job_id or journal.create(dict(folder=str(self.folder),profile=self.profile,
                order=order,by_title=bool(by_title),selected_post_urls=sorted(self.selected_post_urls),
                rename_order=bool(rename_order),start_number=start_number,lang=lang,
                layout_version=int(layout_version or 0),content_mode=content_mode,record_artist=bool(record_artist)))
    def run(self):
        if self.journal is None:
            return super().run()
        from resumable_worker import DownloadEngine
        helpers=dict(absolute_post_url=absolute_post_url,safe_path_component=safe_path_component,
                     unique_path=unique_path,valid_download=valid_download,attachment_name=attachment_name,
                     PAGE_SIZE=PAGE_SIZE,USER_AGENT=USER_AGENT)
        DownloadEngine(self,helpers).run()


class Particle:
    def __init__(self,x,y,click=False):
        a=random.random()*math.tau; speed=random.uniform(1.1,3.5) if click else random.uniform(.3,1.4)
        self.x=x; self.y=y; self.vx=math.cos(a)*speed; self.vy=math.sin(a)*speed; self.life=random.randint(18,34); self.max=self.life; self.size=random.uniform(2,5) if click else random.uniform(1.5,3.5); self.click=click; self.hue=random.choice([24,32,48,350]) if click else random.choice([184,195,210,270])
    def move(self): self.x+=self.vx; self.y+=self.vy; self.vy+=.025; self.life-=1

class GlowButton(QPushButton):
    def __init__(self,*a):
        super().__init__(*a); self.setProperty('animatedGlow',True); self.hover=0.; self.hover_animation=QPropertyAnimation(self,b'hoverLevel',self); self.hover_animation.setDuration(240); self.hover_animation.setEasingCurve(QEasingCurve.Type.OutCubic); self.setMouseTracking(True)
    def getHover(self): return self.hover
    def setHover(self,value): self.hover=float(value); self.update()
    hoverLevel=Property(float,getHover,setHover)
    def animate_hover(self,target):
        self.hover_animation.stop(); self.hover_animation.setStartValue(self.hover); self.hover_animation.setEndValue(target); self.hover_animation.start()
    def enterEvent(self,e): self.animate_hover(1.0); super().enterEvent(e)
    def leaveEvent(self,e): self.animate_hover(0.0); super().leaveEvent(e)
    def paintEvent(self,e):
        super().paintEvent(e)
        # The stylesheet supplies the background; exactly one path supplies the border.
        level=max(0.,min(1.,self.hover))
        if self.hasFocus() and self.isEnabled(): level=max(level,.65)
        primary=self.objectName()=='primaryButton'
        base=QColor(self.property('borderColor') or '#2a3445')
        target=QColor('#ff4852' if not self.isEnabled() else self.property('glowColor') or '#b89af4')
        color=QColor.fromRgbF(base.redF()+(target.redF()-base.redF())*level,
            base.greenF()+(target.greenF()-base.greenF())*level,
            base.blueF()+(target.blueF()-base.blueF())*level)
        if primary: color.setAlphaF(level)
        if primary and level<=0: return
        painter=QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(color,1.+.6*level)); painter.setBrush(Qt.BrushStyle.NoBrush)
            radius=13. if primary else 11.
            rect=QRectF(self.rect()).adjusted(.9,.9,-.9,-.9)
            painter.drawRoundedRect(rect,radius-.9,radius-.9)
        finally: painter.end()

class ParticleOverlay(QWidget):
    """Transparent, mouse-through layer kept above all UI controls."""
    def __init__(self, parent):
        super().__init__(parent)
        self.host=parent; self.sparks=[]; self.rings=[]; self._last_tick=time.monotonic()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.sync()
        self.raise_()
    def sync(self):
        self.setGeometry(self.host.rect())
    @property
    def has_effects(self):
        return bool(self.sparks or self.rings)
    def launch_firework(self,point):
        palette=('#ff5b72','#ffb347','#ffe45c','#5bdb91','#45c8e8','#7187ff','#cc72ee')
        x,y=point.x(),point.y()
        for i in range(26):
            angle=math.tau*i/26+random.uniform(-.09,.09); speed=random.uniform(48,108)
            self.sparks.append({'x':x,'y':y,'px':x,'py':y,'vx':math.cos(angle)*speed,'vy':math.sin(angle)*speed,'age':0.0,'life':random.uniform(.68,.92),'color':QColor(random.choice(palette))})
        if len(self.sparks)>104: self.sparks=self.sparks[-104:]
        self.rings.append({'x':x,'y':y,'age':0.0,'life':.4,'color':QColor('#fff0a8')})
        if len(self.rings)>12: self.rings=self.rings[-12:]
        self._last_tick=time.monotonic(); self.sync(); self.raise_(); self.update()
        if not self.host.fx.isActive(): self.host.fx.start(16)
    def advance(self):
        now=time.monotonic(); dt=max(.001,min(.05,now-self._last_tick)); self._last_tick=now
        for spark in self.sparks:
            spark['px']=spark['x']; spark['py']=spark['y']
            spark['x']+=spark['vx']*dt; spark['y']+=spark['vy']*dt
            spark['vy']+=125*dt; spark['vx']*=max(0.0,1-0.38*dt); spark['age']+=dt
        self.sparks=[spark for spark in self.sparks if spark['age']<spark['life']]
        for ring in self.rings: ring['age']+=dt
        self.rings=[ring for ring in self.rings if ring['age']<ring['life']]
    def paintEvent(self,e):
        p=QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            for dot in self.host.particles:
                alpha=max(0,int(220*dot.life/dot.max)); c=QColor.fromHsv(dot.hue,220 if dot.click else 150,255,alpha)
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(c); p.drawEllipse(QPointF(dot.x,dot.y),dot.size,dot.size)
            for ring in self.rings:
                progress=min(1.0,ring['age']/ring['life']); fade=1-progress*progress*(3-2*progress)
                color=QColor(ring['color']); color.setAlphaF(.72*fade)
                p.setPen(QPen(color,max(.2,1.8*fade))); p.setBrush(Qt.BrushStyle.NoBrush)
                radius=4+27*progress; p.drawEllipse(QPointF(ring['x'],ring['y']),radius,radius)
            for spark in self.sparks:
                progress=min(1.0,spark['age']/spark['life']); fade=1-progress*progress*(3-2*progress)
                color=QColor(spark['color']); glow=QColor(color); glow.setAlphaF(.2*fade)
                p.setPen(QPen(glow,max(.3,3.4*fade))); p.drawLine(QPointF(spark['px'],spark['py']),QPointF(spark['x'],spark['y']))
                color.setAlphaF(.95*fade); p.setPen(QPen(color,max(.2,1.25*fade))); p.drawLine(QPointF(spark['px'],spark['py']),QPointF(spark['x'],spark['y']))
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(color); p.drawEllipse(QPointF(spark['x'],spark['y']),1.9*fade,1.9*fade)
        finally:
            p.end()

class ThemeSwitch(QAbstractButton):
    def __init__(self, callback):
        # Keep the original visible switch dimensions; expanded hit area is handled by App.eventFilter.
        super().__init__(); self.setFixedSize(76,36); self.setCursor(Qt.CursorShape.PointingHandCursor); self.dark=True; self.offset=4.0; self.callback=callback
        self.animation=QPropertyAnimation(self,b'offsetProp',self); self.animation.setDuration(420); self.animation.setEasingCurve(QEasingCurve.Type.InOutCubic); self.pressed.connect(self.toggle)
    def getOffset(self): return self.offset
    def setOffset(self,v): self.offset=float(v); self.update()
    offsetProp=Property(float,getOffset,setOffset)
    def toggle(self):
        self.dark=not self.dark; self.callback(self.dark); self.animation.stop(); self.animation.setStartValue(self.offset); self.animation.setEndValue(4.0 if self.dark else 44.0); self.animation.start()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor('#18315c' if self.dark else '#f2c94c')); p.drawRoundedRect(self.rect(),18,18)
        x=self.offset; p.setBrush(QColor('#263f70' if self.dark else '#ffdc70')); p.drawEllipse(int(x),4,28,28)
        if self.dark:
            moon=QPainterPath(); moon.addEllipse(QPointF(x+14,18),7.5,7.5)
            cut=QPainterPath(); cut.addEllipse(QPointF(x+18,14),7.1,7.1)
            p.setBrush(QColor('#dce9ff')); p.drawPath(moon.subtracted(cut))
        else:
            p.setBrush(QColor('#b67b12')); p.drawEllipse(QPointF(x+14,18),4.5,4.5)
            p.setPen(QPen(QColor('#b67b12'),1.5))
            for i in range(8):
                a=i*math.tau/8
                p.drawLine(QPointF(x+14+math.cos(a)*7,18+math.sin(a)*7),QPointF(x+14+math.cos(a)*9,18+math.sin(a)*9))
        p.end()

class GlowCheckBox(QCheckBox):
    def __init__(self,*args):
        super().__init__(*args); self.setAttribute(Qt.WidgetAttribute.WA_Hover,True); self._glow=0.0
        self.glow_anim=QPropertyAnimation(self,b'glowLevel',self); self.glow_anim.setDuration(420); self.glow_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._ripple=0.0; self.ripple_anim=QPropertyAnimation(self,b'rippleLevel',self); self.ripple_anim.setDuration(620); self.ripple_anim.setStartValue(0.0); self.ripple_anim.setEndValue(1.0); self.ripple_anim.setEasingCurve(QEasingCurve.Type.OutCubic); self.ripple_anim.finished.connect(self.finish_ripple); self.stateChanged.connect(self.checked_ripple)
    def getGlow(self): return self._glow
    def setGlow(self,value):
        self._glow=float(value); self.update(); self.refresh_effect()
    glowLevel=Property(float,getGlow,setGlow)
    def getRipple(self): return self._ripple
    def setRipple(self,value):
        self._ripple=float(value); self.update(); self.refresh_effect()
    rippleLevel=Property(float,getRipple,setRipple)
    def checked_ripple(self,state):
        if state:
            self.ripple_anim.stop(); self._ripple=0.0; self.ripple_anim.start()
    def finish_ripple(self):
        self.setRipple(0.0)
    def refresh_effect(self):
        from control_effects import refresh_checkbox_effect
        refresh_checkbox_effect(self)
    def enterEvent(self,event):
        self.glow_anim.stop(); self.glow_anim.setStartValue(self._glow); self.glow_anim.setEndValue(1.0); self.glow_anim.start(); super().enterEvent(event)
    def leaveEvent(self,event):
        self.glow_anim.stop(); self.glow_anim.setStartValue(self._glow); self.glow_anim.setEndValue(0.0); self.glow_anim.start(); super().leaveEvent(event)
    def paintEvent(self,event):
        super().paintEvent(event)
        opt=QStyleOptionButton(); self.initStyleOption(opt); indicator=self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator,opt,self)
        center=indicator.center()
        if self.isChecked():
            painter=QPainter(self)
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setPen(QPen(QColor('#ffffff' if self.property('lightTheme') else '#17223d'),1.9,Qt.PenStyle.SolidLine,Qt.PenCapStyle.RoundCap,Qt.PenJoinStyle.RoundJoin))
                mark=QPainterPath(); mark.moveTo(indicator.left()+4,center.y())
                mark.lineTo(indicator.left()+8,center.y()+4); mark.lineTo(indicator.right()-4,indicator.top()+5)
                painter.drawPath(mark)
            finally: painter.end()

class SoftComboBox(QComboBox):
    def __init__(self,*args):
        super().__init__(*args); self.popup_animation=None
    def paintEvent(self,event):
        super().paintEvent(event)
        p=QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(getattr(self,'arrow_color','#99a6bb')))
            arrow=QPainterPath(); x=self.width()-17; y=self.height()/2
            arrow.moveTo(x-4,y-2); arrow.lineTo(x+4,y-2); arrow.lineTo(x,y+3); arrow.closeSubpath()
            p.drawPath(arrow)
        finally: p.end()
    def showPopup(self):
        if self.view().window().isVisible(): return
        super().showPopup(); popup=self.view().window()
        if popup and popup.isVisible():
            final=popup.geometry(); start=QRect(final.x(),final.y()-12,final.width(),final.height()); popup.setGeometry(start)
            self.popup_animation=QPropertyAnimation(popup,b'geometry',self); self.popup_animation.setDuration(260); self.popup_animation.setStartValue(start); self.popup_animation.setEndValue(final); self.popup_animation.setEasingCurve(QEasingCurve.Type.OutCubic); self.popup_animation.start()

class RainbowProgress(QWidget):
    def __init__(self): super().__init__(); self.value=0; self.indeterminate=False; self.phase=0; self.setFixedHeight(24)
    def setValue(self,v): self.value=max(0,min(100,v)); self.indeterminate=False; self.update()
    def setIndeterminate(self): self.indeterminate=True; self.phase=(self.phase+17)%100; self.update()
    def paintEvent(self,e):
        p=QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing); r=self.rect().adjusted(1,5,-1,-5)
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(getattr(self,'track','#202735'))); p.drawRoundedRect(r,7,7)
        if self.indeterminate:
            colors=getattr(self,'colors',[QColor('#ff5c74'),QColor('#ffb547'),QColor('#ffe45c'),QColor('#58d68d'),QColor('#45c8e8'),QColor('#6686ff'),QColor('#bd70ef')])
            span=max(36,r.width()*.22); travel=max(1,r.width()-span-8); start=r.left()+4+travel*self.phase/100
            grad=QLinearGradient(start,0,start+span,0)
            for i,c in enumerate(colors): grad.setColorAt(i/(len(colors)-1),c)
            p.setBrush(grad); p.drawRoundedRect(start,r.top()+2,span,r.height()-4,6,6)
        elif self.value:
            span=(r.width()-8)*self.value/100; colors=getattr(self,'colors',[QColor('#ff5c74'),QColor('#ffb547'),QColor('#ffe45c'),QColor('#58d68d'),QColor('#45c8e8'),QColor('#6686ff'),QColor('#bd70ef')])
            grad=QLinearGradient(r.left()+4,0,r.right()-4,0)
            for i,c in enumerate(colors): grad.setColorAt(i/(len(colors)-1),c)
            p.setBrush(grad); p.drawRoundedRect(r.left()+4,r.top()+2,max(0,span),r.height()-4,6,6)
            # A spaced helical ribbon motif, with the number of loops growing with progress.
            p.setPen(QPen(QColor(255,255,255,32),.8)); loops=max(1,int(self.value/18))
            for i in range(loops):
                x=r.left()+10+i*max(12,(span-12)/max(1,loops))
                if x<r.left()+span: p.drawArc(int(x-4),r.top()+3,8,r.height()-6,0,2880)
        p.end()


class RainbowCredit(QLabel):
    """Animated creator credit with a capped hover flow and click fireworks."""
    fireworkRequested=Signal(QPointF)
    def __init__(self, text):
        super().__init__(text)
        self.phase=0.0; self.flow_speed=1.5; self.hovered=False
        self.bounce_index=None; self.bounce_started=0.0
        self._last_tick=time.monotonic()
        self.setAlignment(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
        self.setStyleSheet('background:transparent;font-size:13px;')
        self.setMouseTracking(True)
        self.animation_timer=QTimer(self); self.animation_timer.setInterval(24); self.animation_timer.timeout.connect(self.advance_animation); self.animation_timer.start()

    def enterEvent(self,event):
        super().enterEvent(event)

    def leaveEvent(self,event):
        self.hovered=False
        super().leaveEvent(event)

    def mouseMoveEvent(self,event):
        self.hovered=self.character_at(event.position().x(),event.position().y()) is not None
        super().mouseMoveEvent(event)

    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:
            point=event.position()
            index=self.character_at(point.x(),point.y())
            if index is not None:
                self.hovered=True
                self.bounce_index=index; self.bounce_started=time.monotonic()
                self.launch_firework(point)
        super().mousePressEvent(event)

    def text_geometry(self):
        text_rect=self.contentsRect().adjusted(0,0,-2,0)
        metrics=QFontMetricsF(self.font()); text=self.text(); width=metrics.horizontalAdvance(text)
        if self.alignment() & Qt.AlignmentFlag.AlignRight:
            x=text_rect.right()-width
        elif self.alignment() & Qt.AlignmentFlag.AlignHCenter:
            x=text_rect.left()+(text_rect.width()-width)/2
        else:
            x=text_rect.left()
        baseline=text_rect.top()+(text_rect.height()-metrics.height())/2+metrics.ascent()
        return text_rect,metrics,x,baseline

    def character_at(self,mouse_x,mouse_y=None):
        _,metrics,x,baseline=self.text_geometry(); text=self.text(); candidates=[]
        if mouse_y is not None and not baseline-metrics.ascent()-3<=mouse_y<=baseline+metrics.descent()+3: return None
        for index,char in enumerate(text):
            if char.isspace(): continue
            left=x+metrics.horizontalAdvance(text[:index]); right=x+metrics.horizontalAdvance(text[:index+1])
            candidates.append((max(left-mouse_x,0,mouse_x-right),index,left,right))
        if not candidates: return None
        distance,index,left,right=min(candidates,key=lambda item:item[0])
        return index if left-3<=mouse_x<=right+3 else None

    def launch_firework(self,point):
        host_point=self.mapTo(self.window(),point.toPoint())
        self.fireworkRequested.emit(QPointF(host_point))

    def advance_animation(self):
        now=time.monotonic(); dt=max(.001,min(.06,now-self._last_tick)); self._last_tick=now
        target_speed=4.6 if self.hovered else 1.5
        blend=min(1.0,dt/.32); self.flow_speed+=(target_speed-self.flow_speed)*blend
        self.phase=(self.phase+self.flow_speed*dt*50)%100
        if self.bounce_index is not None and now-self.bounce_started>.68: self.bounce_index=None
        self.update()

    def paintEvent(self,event):
        painter=QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            text_rect,metrics,x,baseline=self.text_geometry(); font=self.font(); text=self.text()
            gradient=QLinearGradient(text_rect.left()-self.phase,0,text_rect.left()+100-self.phase,0)
            gradient.setSpread(QGradient.Spread.RepeatSpread)
            for position,color in ((0,'#ff5470'),(.17,'#ffad45'),(.34,'#ffe45a'),(.51,'#55d990'),(.68,'#42c7e8'),(.85,'#6a82ff'),(1,'#ff5470')):
                gradient.setColorAt(position,QColor(color))
            path=QPainterPath()
            if self.bounce_index is None:
                path.addText(x,baseline,font,text)
            else:
                bounce_progress=min(1.0,max(0.0,(time.monotonic()-self.bounce_started)/.68))
                offset=-13*math.exp(-4.2*bounce_progress)*math.sin(math.tau*1.55*bounce_progress)
                for index,char in enumerate(text):
                    char_x=x+metrics.horizontalAdvance(text[:index])
                    path.addText(char_x,baseline+offset if index==self.bounce_index else baseline,font,char)
            painter.fillPath(path,QBrush(gradient))
        finally:
            painter.end()

class SnakeGameWidget(QWidget):
    """A quiet 20×20 scan-wait Snake with fragments and network glitches.

    The widget is deliberately independent from the scanner.  Its timer only
    advances this small canvas, while the parent dialog continues to expose
    scan counters and pause/finish controls.  Collisions are soft feedback:
    the snake loses two segments and a little score instead of entering a
    blocking game-over screen.
    """
    completionFinished = Signal()

    GRID = 20
    IDLE_SECONDS = 30.0
    WAKE_PROTECTION_SECONDS = 5.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(300, 220)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._rng = random.Random(7)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self.step)
        self._dissolve_timer = QTimer(self)
        self._dissolve_timer.setInterval(30)
        self._dissolve_timer.timeout.connect(self._advance_dissolve)
        self._paused = False
        self._stopped = False
        self._finishing = False
        self._finish_until = 0.0
        self._finish_count = 0
        self._dissolve_particles = []
        self._rebuild_until = 0.0
        self._invincible_until = 0.0
        self._collision_flash = 0.0
        self._fragment_clock = 0.0
        self._obstacle_clock = 0.0
        self._snake_clock = 0.0
        self._last_tick = time.monotonic()
        self._idle_since = time.monotonic()
        self._idle = False
        self._idle_turn = 0
        self._collision_until = 0.0
        self._previous_body = []
        self._canvas_cache = None
        self._ghost_until = 0.0
        self._magnet_until = 0.0
        self._overclock_until = 0.0
        self._buff_label = ''
        self._score = 10
        self._best = 10
        self._reset()
        self._timer.start()

    def _reset(self):
        center = (self.GRID // 2, self.GRID // 2)
        self.body = [center, (center[0] - 1, center[1]), (center[0] - 2, center[1])]
        self._previous_body = list(self.body)
        self.direction = (1, 0)
        self.next_direction = self.direction
        self.fragments = set()
        self.fragment_buffs = {}
        self.obstacles = []
        self._score = 10
        self._rebuild_until = 0.0
        self._invincible_until = time.monotonic() + 3.0
        self._ghost_until = 0.0
        self._magnet_until = 0.0
        self._overclock_until = 0.0
        self._buff_label = ''
        self._spawn_fragment()
        self._idle_since = time.monotonic()
        self._idle = False
        self._idle_turn = 0
        self._snake_clock = 0.0
        self._collision_until = 0.0
        self.update()

    def _occupied(self):
        occupied = set(self.body)
        for x, y in self.obstacles:
            # A moving tile can overlap two columns between grid positions.
            occupied.update((column, y) for column in (math.floor(x), math.ceil(x))
                            if 0 <= column < self.GRID)
        return occupied

    def _spawn_fragment(self):
        # A long scan can leave hundreds of shards on the grid. Compute the
        # occupied set once, rather than rebuilding it for all 400 candidates.
        occupied = self._occupied() | self.fragments
        free = [(x, y) for y in range(self.GRID) for x in range(self.GRID)
                if (x, y) not in occupied]
        if free:
            coordinate = self._rng.choice(free)
            self.fragments.add(coordinate)
            # Most fragments are ordinary score shards; a small subset carries
            # a short-lived protocol buff so the game remains readable while
            # still rewarding exploration.
            if self._rng.random() < .22:
                self.fragment_buffs[coordinate] = self._rng.choice(
                    ('ghost', 'magnet', 'overclock', 'pulse'))

    def _spawn_obstacle(self):
        occupied = self._occupied() | self.fragments
        rows = [y for y in range(self.GRID)
                if (self.GRID - 1, y) not in occupied]
        if rows:
            self.obstacles.append([float(self.GRID - 1), self._rng.choice(rows)])

    def set_paused(self, paused):
        self._paused = bool(paused)
        if self._paused:
            self._timer.stop()
        elif not self._stopped:
            self._last_tick = time.monotonic()
            self._timer.start()
        self.update()

    def stop(self):
        self._stopped = True
        self._timer.stop()
        self._dissolve_timer.stop()

    def start(self):
        self._stopped = False
        if not self._paused:
            self._last_tick = time.monotonic()
            self._timer.start()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def begin_scan_complete(self, count):
        """Start the soft completion dissolve used before the picker opens."""
        self._timer.stop()
        self._finishing = True
        self._finish_count = max(0, int(count or 0))
        self._finish_until = time.monotonic() + .85
        self._collision_flash = 0.0
        self._dissolve_particles = []
        for x, y in list(self.body) + list(self.fragments):
            angle = self._rng.random() * math.tau
            speed = .8 + self._rng.random() * 1.8
            self._dissolve_particles.append([
                float(x), float(y), math.cos(angle) * speed,
                math.sin(angle) * speed, 1.0])
        self._dissolve_timer.start()
        self.update()

    def _advance_dissolve(self):
        if not self._finishing:
            self._dissolve_timer.stop()
            return
        for particle in self._dissolve_particles:
            particle[0] += particle[2] * .08
            particle[1] += particle[3] * .08
            particle[4] = max(0.0, particle[4] - .035)
        now = time.monotonic()
        if now >= self._finish_until:
            self._finishing = False
            self._stopped = True
            self._dissolve_timer.stop()
            self._dissolve_particles.clear()
            self.completionFinished.emit()
        self.update()

    def _set_direction(self, direction):
        self._note_activity()
        if direction[0] + self.direction[0] == 0 and direction[1] + self.direction[1] == 0:
            return
        self.next_direction = direction

    def _note_activity(self):
        now = time.monotonic()
        if self._idle:
            self._invincible_until = max(self._invincible_until,
                                         now + self.WAKE_PROTECTION_SECONDS)
            self._idle_turn = 0
        self._idle_since = now
        self._idle = False

    def _activate_buff(self, kind):
        now = time.monotonic()
        if kind == 'ghost':
            self._ghost_until = max(self._ghost_until, now + 5.0)
            self._buff_label = '幽灵协议 · 5s'
        elif kind == 'magnet':
            self._magnet_until = max(self._magnet_until, now + 4.0)
            self._buff_label = '赛博磁铁 · 4s'
        elif kind == 'overclock':
            self._overclock_until = max(self._overclock_until, now + 6.0)
            self._buff_label = '超频引擎 · 6s'
        elif kind == 'pulse':
            self.obstacles.clear()
            self._collect_all_fragments()
            self._buff_label = '反制脉冲 · 清障完成'

    def _collect_fragment(self, coordinate):
        if self._idle or coordinate not in self.fragments:
            return False
        self.fragments.discard(coordinate)
        kind = self.fragment_buffs.pop(coordinate, None)
        self._score += 1
        self._best = max(self._best, self._score)
        if kind:
            self._activate_buff(kind)
        return True

    def _collect_all_fragments(self):
        for coordinate in list(self.fragments):
            self._collect_fragment(coordinate)

    def _apply_magnet(self, head):
        if self._idle or time.monotonic() >= self._magnet_until:
            return
        # Every existing shard moves one grid step toward the snake head on
        # each tick for the duration of the magnet. Preserve the buff carried
        # by a shard while it travels; collection happens on arrival so the
        # effect remains visible instead of becoming an instant score jump.
        occupied = self._occupied()
        moved = set()
        moved_buffs = {}
        pickups = []
        for coordinate in list(self.fragments):
            kind = self.fragment_buffs.get(coordinate)
            x, y = coordinate
            dx = 0 if x == head[0] else (1 if head[0] > x else -1)
            dy = 0 if y == head[1] else (1 if head[1] > y else -1)
            candidate = (x + dx, y + dy)
            if candidate == head:
                pickups.append(kind)
                continue
            if not (0 <= candidate[0] < self.GRID and 0 <= candidate[1] < self.GRID):
                candidate = coordinate
            if candidate in occupied or candidate in moved:
                candidate = coordinate
            moved.add(candidate)
            if kind:
                moved_buffs[candidate] = kind
        self.fragments = moved
        self.fragment_buffs = moved_buffs
        for kind in pickups:
            self._score += 1
            self._best = max(self._best, self._score)
            if kind:
                self._activate_buff(kind)

    def keyPressEvent(self, event):
        directions = {
            Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_A: (-1, 0),
            Qt.Key.Key_Right: (1, 0), Qt.Key.Key_D: (1, 0),
            Qt.Key.Key_Up: (0, -1), Qt.Key.Key_W: (0, -1),
            Qt.Key.Key_Down: (0, 1), Qt.Key.Key_S: (0, 1),
        }
        direction = directions.get(event.key())
        if direction is not None:
            self._set_direction(direction)
            event.accept()
            return
        # Any key wakes the gentle idle state; it never opens a result dialog.
        self._note_activity()
        event.accept()

    def mousePressEvent(self, event):
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._note_activity()
        if self._finishing:
            self.completionFinished.emit()
        event.accept()

    def _soft_collision(self):
        now = time.monotonic()
        if self._idle or now < self._invincible_until or now < self._ghost_until:
            return
        if now < self._collision_until:
            return
        if now < self._overclock_until:
            self._collision_flash = .45
            return
        self._collision_until = now + .45
        self.body = self.body[:max(1, len(self.body) - 2)]
        self._score -= 2
        self._collision_flash = .45
        if len(self.body) <= 1:
            center = (self.GRID // 2, self.GRID // 2)
            self.body = [center, (center[0] - 1, center[1]), (center[0] - 2, center[1])]
            self._previous_body = list(self.body)
            self._score -= 5
        if self._score <= 0:
            self._rebuild_until = now + 2.0
            self._score = 0

    def _check_obstacle_collision(self, previous_positions=None):
        """Test moving rectangles, including every snake segment.

        Glitches move between snake steps. Checking only the head at the next
        grid position missed vertical crossings and glitches passing a still
        segment; testing the swept horizontal extent closes both gaps.
        """
        if self._idle:
            return
        for index, (x, y) in enumerate(self.obstacles):
            previous_x = previous_positions[index] if previous_positions is not None else x
            left, right = min(x, previous_x), max(x, previous_x) + 1.0
            if any(y == sy and left < sx + 1.0 and right > sx for sx, sy in self.body):
                self._soft_collision()
                return

    def _move_snake(self):
        if self._idle:
            # A small, harmless screen-saver loop. Its cadence follows snake
            # steps instead of paint frames, so idle speed remains consistent.
            if self._idle_turn and self._idle_turn % 3 == 0:
                dx, dy = self.direction
                self.next_direction = (-dy, dx)
            self._idle_turn += 1
        self._previous_body = list(self.body)
        self.direction = self.next_direction
        head = self.body[0]
        new_head = ((head[0] + self.direction[0]) % self.GRID,
                    (head[1] + self.direction[1]) % self.GRID)
        body_hit = new_head in self.body[:-1]
        self.body.insert(0, new_head)
        fragment_hit = not self._idle and new_head in self.fragments
        if fragment_hit:
            self._collect_fragment(new_head)
        else:
            self.body.pop()
        if body_hit:
            self._soft_collision()
        self._check_obstacle_collision()
        self._apply_magnet(new_head)

    def step(self):
        if self._paused or self._stopped:
            return
        now = time.monotonic()
        dt = max(.01, min(.25, now - self._last_tick))
        self._last_tick = now
        if self._finishing:
            if now >= self._finish_until:
                self._finishing = False
                self.completionFinished.emit()
            self.update()
            return
        if self._rebuild_until:
            if now >= self._rebuild_until:
                self._reset()
                self._invincible_until = now + 3.0
            self.update()
            return
        self._fragment_clock += dt
        self._obstacle_clock += dt
        self._snake_clock += dt
        self._collision_flash = max(0.0, self._collision_flash - dt)
        now = time.monotonic()
        if now >= self._ghost_until and now >= self._magnet_until and now >= self._overclock_until:
            self._buff_label = ''
        if self._fragment_clock >= 1.0:
            self._fragment_clock %= 1.0
            self._spawn_fragment()
        if self._obstacle_clock >= 2.0:
            self._obstacle_clock %= 2.0
            self._spawn_obstacle()
        if now - self._idle_since >= self.IDLE_SECONDS:
            self._idle = True
        previous_positions = [item[0] for item in self.obstacles]
        for item in self.obstacles:
            item[0] -= 2.0 * dt
        self._check_obstacle_collision(previous_positions)
        self.obstacles = [item for item in self.obstacles if item[0] > -.8]
        move_interval = .07 if now < self._overclock_until else .14
        # Keep the remainder so 30 Hz rendering does not quantize both normal
        # and overclock movement to the same speed. Catch up at most one frame
        # budget after UI stalls; never run an unbounded event-loop workload.
        for _ in range(4):
            if self._snake_clock < move_interval or self._rebuild_until:
                break
            self._snake_clock -= move_interval
            self._move_snake()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cell_w, cell_h = self.width() / self.GRID, self.height() / self.GRID
        cache_key = (self.width(), self.height(), self.devicePixelRatioF())
        if self._canvas_cache is None or self._canvas_cache[0] != cache_key:
            canvas = QPixmap(self.size() * self.devicePixelRatioF())
            canvas.setDevicePixelRatio(self.devicePixelRatioF())
            canvas.fill(QColor('#091326'))
            grid = QPainter(canvas)
            grid.setPen(QPen(QColor(120, 145, 190, 20), 1))
            for index in range(self.GRID + 1):
                grid.drawLine(QPointF(index * cell_w, 0), QPointF(index * cell_w, self.height()))
                grid.drawLine(QPointF(0, index * cell_h), QPointF(self.width(), index * cell_h))
            grid.end()
            self._canvas_cache = (cache_key, canvas)
        painter.drawPixmap(0, 0, self._canvas_cache[1])
        now = time.monotonic()
        pulse = .65 + .35 * math.sin(now * 4.0)
        painter.setPen(Qt.PenStyle.NoPen)
        fragment_colors = {
            'ghost': QColor('#c3a5ff'), 'magnet': QColor('#ffe58a'),
            'overclock': QColor('#ffad6e'), 'pulse': QColor('#ff8fb3'),
        }
        fragment_marks = {'ghost': '◇', 'magnet': 'M', 'overclock': '»', 'pulse': '◎'}
        for fx, fy in self.fragments:
            rect = QRectF(fx * cell_w + 3, fy * cell_h + 3, cell_w - 6, cell_h - 6)
            glow = QRadialGradient(rect.center(), max(rect.width(), rect.height()) * .8)
            kind = self.fragment_buffs.get((fx, fy))
            core = fragment_colors.get(kind, QColor('#7ce9ff'))
            glow.setColorAt(0, QColor(core.red(), core.green(), core.blue(), int(235 * pulse)))
            glow.setColorAt(1, QColor(72, 128, 255, 0))
            painter.setBrush(glow)
            painter.drawEllipse(rect.adjusted(-5, -5, 5, 5))
            painter.setBrush(core)
            painter.drawRoundedRect(rect, 3, 3)
            if kind:
                painter.setPen(QColor('#20304d'))
                painter.setFont(QFont('Segoe UI', 7, QFont.Weight.Bold))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, fragment_marks[kind])
                painter.setPen(Qt.PenStyle.NoPen)
        for ox, oy in self.obstacles:
            rect = QRectF(ox * cell_w + 1, oy * cell_h + 1, cell_w - 2, cell_h - 2)
            painter.setBrush(QColor(188, 74, 112, 190))
            painter.drawRoundedRect(rect, 3, 3)
            painter.setPen(QColor(255, 184, 203, 160))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, '≋')
            painter.setPen(Qt.PenStyle.NoPen)
        opacity = .34 if self._idle else 1.0
        if now < self._invincible_until:
            opacity *= .35 + .65 * (.5 + .5 * math.sin(now * 14.0))
        elif now < self._ghost_until:
            opacity *= .45
        painter.setOpacity(max(0.15, opacity))
        move_interval = .07 if now < self._overclock_until else .14
        blend = min(1.0, self._snake_clock / move_interval)
        for index, (x, y) in enumerate(self.body):
            if index < len(self._previous_body) and not self._rebuild_until:
                px, py = self._previous_body[index]
                dx = (x - px + self.GRID / 2) % self.GRID - self.GRID / 2
                dy = (y - py + self.GRID / 2) % self.GRID - self.GRID / 2
                x, y = (px + dx * blend) % self.GRID, (py + dy * blend) % self.GRID
            painter.setBrush(QColor('#c9fff0' if index == 0 else '#65d8bc'))
            painter.drawRoundedRect(QRectF(x * cell_w + 1.5, y * cell_h + 1.5,
                                           cell_w - 3, cell_h - 3), 4, 4)
        painter.setOpacity(1.0)
        painter.setPen(QColor('#d7e7ff'))
        painter.setFont(QFont('Segoe UI', 8, QFont.Weight.DemiBold))
        painter.drawText(QRectF(7, 4, self.width() - 14, 15), Qt.AlignmentFlag.AlignLeft,
                         f'DATA {self._score}  /  BEST {self._best}')
        if self._buff_label:
            painter.setPen(QColor('#bfeaff'))
            painter.setFont(QFont('Segoe UI', 7, QFont.Weight.DemiBold))
            painter.drawText(QRectF(7, 20, self.width() - 14, 13),
                             Qt.AlignmentFlag.AlignLeft, ui_text(language_for(self),self._buff_label))
        if self._rebuild_until:
            painter.fillRect(self.rect(), QColor(24, 140, 255, 58))
            painter.setPen(QColor('#a8e8ff'))
            painter.setFont(QFont('Segoe UI', 10, QFont.Weight.DemiBold))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, ui_text(language_for(self),'核心受损，正在重组数据流...'))
            painter.setBrush(QColor(122, 228, 255, 220))
            center = QPointF(self.width() / 2, self.height() / 2)
            painter.drawEllipse(center, 8 + 3 * math.sin(now * 6), 8 + 3 * math.sin(now * 6))
        elif self._paused:
            painter.fillRect(self.rect(), QColor(5, 10, 23, 150))
            painter.setPen(QColor('#b8c9e8'))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, ui_text(language_for(self),'扫描暂停'))
        elif self._finishing:
            progress = min(1.0, max(0.0, (self._finish_until - now) / .85))
            for px, py, _, _, life in self._dissolve_particles:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(126, 226, 255, int(210 * life)))
                painter.drawEllipse(QPointF(px * cell_w + cell_w / 2,
                                            py * cell_h + cell_h / 2),
                                    2.0 + 3.0 * life, 2.0 + 3.0 * life)
            painter.fillRect(self.rect(), QColor(24, 39, 72, int(190 * progress)))
            painter.setPen(QColor('#d8ecff'))
            painter.setFont(QFont('Segoe UI', 10, QFont.Weight.DemiBold))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             ui_text(language_for(self),'扫描已完成，共获取 {count} 个图集',count=self._finish_count)+'\n'+ui_text(language_for(self),'（点击窗口任意一处位置查看图集）'))
        if self._collision_flash:
            painter.fillRect(self.rect(), QColor(255, 107, 153, int(80 * self._collision_flash / .45)))
        painter.end()


class ScanCompletionOverlay(QWidget):
    """Click-through completion card that dissolves the waiting game."""
    clicked = Signal()

    def __init__(self, count, parent=None):
        super().__init__(parent)
        self.setObjectName('scanCompletionOverlay')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            '#scanCompletionOverlay { background: rgba(7, 12, 25, 218); }'
            'QLabel { background: transparent; color: #edf3ff; }'
            'QLabel#scanCompletionTitle { color: #cdbbff; font-size: 18px; font-weight: 700; }'
            'QLabel#scanCompletionHint { color: #a9bad7; font-size: 12px; }')
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(9)
        title = QLabel(ui_text(language_for(parent),'扫描已完成，共获取 {count} 个图集',count=int(count)))
        title.setObjectName('scanCompletionTitle')
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint = QLabel(ui_text(language_for(parent),'（点击窗口任意一处位置查看图集）'))
        hint.setObjectName('scanCompletionHint')
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addWidget(hint)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        # Any key is an intentional equivalent of clicking the overlay.
        self.clicked.emit()
        event.accept()


class ScanAnimation(QDialog):
    """A small playable Snake easter egg shown while profile pages are scanned."""
    def __init__(self,parent):
        super().__init__(parent); self.setFixedSize(520,430); self.setModal(False); self._allow_close=False
        from background_ui import install_background_host
        install_background_host(self,getattr(parent,'background_store',None),'scan')
        self.phase=0; self.pages=0; self.posts=0; self.paused=False; self.finish_requested=False; self.lang='简体中文'; self.scanner=None
        self._completion_overlay=None; self._completion_callback=None; self._completion_closing=False
        layout=QVBoxLayout(self); layout.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.setSpacing(7)
        self.game=SnakeGameWidget(self); layout.addWidget(self.game,0,Qt.AlignmentFlag.AlignCenter)
        self.game_hint=QLabel('方向键 / WASD 控制 · 点击游戏区域获取焦点'); self.game_hint.setAlignment(Qt.AlignmentFlag.AlignCenter); self.game_hint.setStyleSheet('color:#91a7be;font-size:11px'); layout.addWidget(self.game_hint)
        self.message=QLabel(); self.message.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(self.message)
        self.counter=QLabel(); self.counter.setAlignment(Qt.AlignmentFlag.AlignCenter); self.counter.setStyleSheet('color:#91a7be'); layout.addWidget(self.counter)
        actions=QHBoxLayout(); self.pause_button=QPushButton(); self.resume_button=QPushButton(); self.finish_button=QPushButton(); actions.addWidget(self.pause_button); actions.addWidget(self.resume_button); actions.addWidget(self.finish_button); layout.addLayout(actions)
        self.pause_button.clicked.connect(self.pause_scan); self.resume_button.clicked.connect(self.resume_scan); self.finish_button.clicked.connect(self.finish_scan); self.resume_button.setEnabled(False)
    def pause_scan(self):
        self.paused=True
        if self.scanner: self.scanner.paused=True
        self.game.set_paused(True)
        self.pause_button.setEnabled(False); self.resume_button.setEnabled(True); self.message.setText(AUX_TEXT[self.lang]['scan_paused'])
    def resume_scan(self):
        self.paused=False
        if self.scanner: self.scanner.paused=False
        self.game.set_paused(False)
        self.pause_button.setEnabled(True); self.resume_button.setEnabled(False); self.message.setText(AUX_TEXT[self.lang]['scan_searching'])
    def finish_scan(self):
        self.finish_requested=True; self.paused=False
        if self.scanner: self.scanner.finish_requested=True; self.scanner.paused=False
        self.pause_button.setEnabled(False); self.resume_button.setEnabled(False); self.finish_button.setEnabled(False)
        self.message.setText(AUX_TEXT[self.lang]['scan_finishing'])

    def show_completion(self, count, callback):
        """Freeze the game behind a soft overlay until the user continues."""
        if self._completion_overlay is not None:
            return
        if getattr(self.game,'_rebuild_until',0) > time.monotonic():
            self._allow_close=True; self.game.stop(); self.close(); callback(); return
        self.finish_requested = True
        self.paused = False
        if self.scanner:
            self.scanner.finish_requested = True
            self.scanner.paused = False
        self.pause_button.setEnabled(False)
        self.resume_button.setEnabled(False)
        self.finish_button.setEnabled(False)
        begin_dissolve = getattr(self.game, 'begin_scan_complete', None)
        if callable(begin_dissolve):
            begin_dissolve(count)
        else:
            self.game.stop()
        self._completion_callback = callback
        overlay = ScanCompletionOverlay(count, self)
        overlay.setGeometry(self.rect())
        overlay.clicked.connect(self.finish_completion)
        overlay.raise_()
        effect = QGraphicsOpacityEffect(overlay)
        effect.setOpacity(0.0)
        overlay.setGraphicsEffect(effect)
        self._completion_overlay = overlay
        overlay.show()
        animation = QPropertyAnimation(effect, b'opacity', overlay)
        animation.setDuration(520)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        overlay._fade_animation = animation
        animation.start()

    def finish_completion(self):
        callback = self._completion_callback
        if callback is None or self._completion_closing:
            return
        self._completion_closing = True
        self._completion_callback = None
        if self._completion_overlay is not None:
            self._completion_overlay.deleteLater()
            self._completion_overlay = None
        self._allow_close = True
        self.close()
        callback()

    def showEvent(self, event):
        """Resume the game whenever the scan dialog becomes visible.

        Qt may deliver a transient hide event while applying native window
        state.  The hide handler intentionally stops the game, so without
        restarting it here the scanner keeps reporting progress while the
        canvas appears frozen.  Restarting is idempotent and still respects
        an explicit pause or completion transition.
        """
        super().showEvent(event)
        if not self.paused and not self.finish_requested:
            self.game.start()
        self.game.update()

    def set_language(self,lang):
        self.lang=lang; t=AUX_TEXT[lang]
        self.game_hint.setText(ui_text(lang,'方向键 / WASD 控制 · 点击游戏区域获取焦点'))
        self.setWindowTitle(t['scan_window']); self.pause_button.setText(t['pause']); self.resume_button.setText(t['resume']); self.finish_button.setText(t['finish'])
        message=t['scan_finishing'] if self.finish_requested else t['scan_paused'] if self.paused else t['scan_searching']
        self.message.setText(message); self.update_counts(self.pages,self.posts)
    def update_counts(self,pages,posts):
        self.pages=pages; self.posts=posts; self.counter.setText(AUX_TEXT[self.lang]['scan_count'].format(pages=pages,posts=posts))
    def closeEvent(self,event):
        if self.scanner and self.scanner.isRunning() and not self._allow_close:
            self.finish_scan()
            self.hide()
            event.ignore()
            return
        self.game.stop()
        callback = self._completion_callback
        self._completion_callback = None
        self._completion_overlay = None
        super().closeEvent(event)
        refresh=getattr(self.parent(),'refresh_background',None)
        if callable(refresh):QTimer.singleShot(0,refresh)
        if callback is not None and not self._completion_closing:
            self._completion_closing = True
            QTimer.singleShot(0, callback)
    def hideEvent(self,event):
        self.game.stop()
        super().hideEvent(event)

class ProfileScanWorker(QThread):
    counts=Signal(int,int); result=Signal(object); failed=Signal(str)
    def __init__(self,profile,cache_dir,selected_urls=None,after_ts=None):
        super().__init__(); self.profile=profile.rstrip('/'); self.cache_dir=Path(cache_dir); self.paused=False; self.finish_requested=False
        # Recovery scans can be limited to the albums that still have a
        # durable checkpoint.  The profile is still paged in order, but
        # unrelated cards do not trigger detail/cover requests or enter the
        # rebuilt queue.
        self.after_ts=after_ts
        self.selected_urls={str(value).rstrip('/') for value in (selected_urls or ()) if str(value).strip()}
    def run(self):
        session=requests.Session()
        session.headers['User-Agent']=USER_AGENT
        try:
            titles=[]; offset=0; seen=set(); previous_signature=None
            while True:
                while self.paused and not self.finish_requested and not self.isInterruptionRequested(): time.sleep(.08)
                if self.finish_requested or self.isInterruptionRequested(): break
                url=f'{self.profile}?o={offset}' if offset else self.profile
                response=session.get(url,timeout=20); response.raise_for_status(); soup=BeautifulSoup(response.text,'html.parser'); cards=soup.select('article.post-card')
                if self.isInterruptionRequested(): break
                if not cards: break
                page_post_count=len(titles)
                page_signature=tuple(
                    absolute_post_url(card.select_one('a[href*="/post/"]').get('href'))
                    if card.select_one('a[href*="/post/"]') else ''
                    for card in cards)
                self.counts.emit(offset//PAGE_SIZE+1,len(titles))
                for card in cards:
                    while self.paused and not self.finish_requested and not self.isInterruptionRequested(): time.sleep(.08)
                    if self.finish_requested or self.isInterruptionRequested(): break
                    link=card.select_one('a[href*="/post/"]'); title=card.select_one('.post-card__header'); img=card.select_one('img.post-card__image')
                    if not link or not title: continue
                    post_url=absolute_post_url(link.get('href'))
                    if not post_url: continue
                    post_key=post_url.rstrip('/')
                    if self.selected_urls and post_key not in self.selected_urls: continue
                    if post_key in seen: continue
                    date_node=card.select_one('time[datetime]')
                    post_date=str(date_node.get('datetime','')) if date_node else ''
                    if self.after_ts is not None:
                        from subscriptions import parse_site_datetime
                        timestamp=parse_site_datetime(post_date)
                        if timestamp is None or timestamp<=self.after_ts: continue
                    image_path=None; image_url=''
                    if img:
                        src=img.get('data-src') or img.get('src')
                        if src:
                            image_url=str(src)
                            try:
                                ir=session.get(src,timeout=8); ir.raise_for_status(); suffix=Path(unquote(urlparse(src).path)).suffix.lower(); suffix=suffix if re.fullmatch(r'\.[a-z0-9]{1,8}',suffix) else '.img'; image_path=self.cache_dir/f'{len(titles):06}{suffix}'; image_path.write_bytes(ir.content)
                            except requests.RequestException: pass
                    if self.isInterruptionRequested(): break
                    formats=[]; image_formats=[]; attachment_formats=[]; file_count=0; image_count=0; attachment_count=0
                    try:
                        detail=session.get(post_url,timeout=12); detail.raise_for_status(); ds=BeautifulSoup(detail.text,'html.parser')
                        attachments=ds.select('.post__files a.fileThumb[href], a.post__attachment-link[href]')
                        file_count=len(attachments)
                        for number, attachment in enumerate(attachments, 1):
                            _name, category, direct, ext = attachment_info(attachment, number)
                            if ext and ext not in formats: formats.append(ext)
                            if direct or category != 'image':
                                attachment_count += 1
                                if ext and ext not in attachment_formats: attachment_formats.append(ext)
                            else:
                                image_count += 1
                                if ext and ext not in image_formats: image_formats.append(ext)
                    except requests.RequestException: pass
                    if self.isInterruptionRequested(): break
                    seen.add(post_key); titles.append({'date':post_date,'title':title.get_text(' ',strip=True),'url':post_url,'image':str(image_path) if image_path else None,'image_url':image_url,'formats':formats,'image_formats':image_formats,'attachment_formats':attachment_formats,'image_count':image_count,'attachment_count':attachment_count,'direct':bool(attachment_count),'file_count':file_count}); self.counts.emit(offset//PAGE_SIZE+1,len(titles))
                    if self.finish_requested: break
                repeated_page=page_signature and page_signature==previous_signature
                if (self.finish_requested or len(cards)<PAGE_SIZE
                        or (not self.selected_urls and len(titles)==page_post_count)
                        or (self.selected_urls and (self.selected_urls.issubset(seen) or repeated_page))): break
                previous_signature=page_signature
                offset+=PAGE_SIZE
            self.result.emit(titles)
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            session.close()

class App(QWidget):
    _INPUT_EVENTS=frozenset((QEvent.Type.MouseButtonPress,QEvent.Type.MouseButtonRelease,
        QEvent.Type.MouseButtonDblClick,QEvent.Type.MouseMove,QEvent.Type.Wheel,
        QEvent.Type.DragEnter,QEvent.Type.DragMove,QEvent.Type.Drop,QEvent.Type.KeyPress))
    _DIALOG_EVENTS=frozenset((QEvent.Type.Polish,QEvent.Type.Show))
    def __init__(self,settings=None,services=None):
        super().__init__()
        self.checkbox_factory=GlowCheckBox
        self.services=services
        self.setAcceptDrops(True)
        from desktop_design import build_interface
        self.setObjectName('appRoot')
        self.lang=services.store.get_setting('language','简体中文') if services else '简体中文'
        if self.lang not in TEXT:self.lang='简体中文'
        self.reward_skin=services.store.get_setting('reward_skin','default') if services else 'default'
        self.particles=[]; self.setMouseTracking(True)
        # The normal presentation keeps the original wide minimum.  The
        # portrait toggle temporarily lowers this constraint while the two
        # primary panels are stacked.
        self.setMinimumSize(900,740); self.resize(1180,941)
        if QApplication.platformName()!='offscreen':
            available=QApplication.primaryScreen().availableGeometry()
            self.setMinimumHeight(min(740,max(520,available.height()-56)))
            self.resize(max(900,min(1180,available.width()-48)),max(self.minimumHeight(),min(941,available.height()-56)))
        self._normal_minimum_size=QSize(self.minimumWidth(),self.minimumHeight())
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
        icon_path=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))/'1.ico'
        self.setWindowIcon(rounded_icon(icon_path))
        self.taskbar_progress=TaskbarProgress(self)
        self._background_exit_timer=QTimer(self)
        self._background_exit_timer.setSingleShot(True)
        self._background_exit_timer.timeout.connect(self.close)
        # Sweep only Pawchive-owned temp roots before creating this run's
        # cache.  The 24-hour limit prevents abandoned previews from slowly
        # consuming the system drive while preserving an active session.
        cleanup_stale_cache_dirs()
        self.cache_dir=Path(tempfile.mkdtemp(prefix='pawchive_cache_'))
        self.busy=False; self._close_when_worker_stops=False; self._link_prompt_active=False; self._handling_link_input=False
        self.queue_scheduler=None; self._queue_scan_entries=[]; self._queue_profile=''
        self._queue_completed_files=0; self._queue_recycle_ids=set(); self._queue_job_ids={}
        self._queue_rewarded_ids=set(); self._queue_closing=False; self._queue_selected_ids=[]
        self._queue_view_pending=False
        self._queue_preparation=None; self._queue_finalizer=None; self._queue_finalizing=False
        self._queue_insert_timer=QTimer(self)
        self._queue_insert_timer.setSingleShot(True)
        self._queue_insert_timer.timeout.connect(self._queue_insert_batch)
        self._selected_content_mode='all'
        self._next_record_artist=True; self._scan_after_timestamp=None
        self._artist_avatar_workers={}
        self._artist_metadata_workers={}
        self._subscription_worker=None
        self._subscription_loading_profiles=set(); self._subscription_check_running=False
        self._subscription_spinner_phase=0
        self._subscription_badge_timer=QTimer(self)
        self._subscription_badge_timer.setInterval(120)
        self._subscription_badge_timer.timeout.connect(self.refresh_subscription_badge)
        try:
            from subscriptions import SubscriptionStore
            self.subscription_store=SubscriptionStore(services.store) if services else None
        except (ImportError, OSError, RuntimeError):
            self.subscription_store=None
        self.settings=settings if settings is not None else QSettings('PawchiveTools','PawchiveDownloader')
        self.completed_path=None; self.workflow_state='setup'; self.logs_hidden=False
        self._dark=True; self.files_saved=0; self._album_current=0; self._album_total=0; self._file_current=0; self._file_total=0
        self.portrait_mode=False
        self._restore_window_size=(self.width(), self.height())
        self._restore_maximized=False
        self.background_page='workspace'
        self._background_auto_portrait=False
        self._background_previous_portrait=False
        try:
            self.background_store=BackgroundStore(services.store.data_dir) if services else None
        except (OSError, ValueError):
            # A read-only profile should not prevent the downloader from
            # starting; background customization simply remains unavailable.
            self.background_store=None
        try:
            self.recycle_bin=RecycleBin(services.store.data_dir) if services else None
        except (OSError, ValueError, RuntimeError):
            self.recycle_bin=None
        self.recycle_timer=QTimer(self)
        self.recycle_timer.setInterval(60 * 60 * 1000)
        self.recycle_timer.timeout.connect(self.refresh_recycle_bin)
        if self.recycle_bin is not None:
            self.recycle_timer.start()
        self._transfer_sample=None; self._speed=0.; self.stop_requested=False
        self._scan_pages=0; self._scan_posts=0
        build_interface(self,ThemeSwitch=ThemeSwitch,SoftComboBox=SoftComboBox,
            GlowButton=GlowButton,GlowCheckBox=GlowCheckBox,RainbowProgress=RainbowProgress,
            RainbowCredit=RainbowCredit,languages=TEXT,icon_path=icon_path)
        from update_checker import install_update_checker
        self.update_controller=install_update_checker(self,self.update_button,services.store if services else None,
            current_version='v1.3',delay_ms=None if QApplication.platformName()=='offscreen' else 1800)
        install_window_chrome(self, title=TEXT[self.lang]['title'])
        self.background_layer=BackgroundLayer(self)
        self.background_layer.setGeometry(self.rect())
        self.background_layer.show(); self.background_layer.lower()
        if self.background_store:
            self.background_store.register_layer('workspace',self.background_layer)
        self.set_lang(self.lang)
        self.apply_theme(True)
        self.refresh_background()
        self.fx=QTimer(self); self.fx.timeout.connect(self.tick)
        self.overlay=ParticleOverlay(self); self.overlay.show(); self.overlay.raise_()
        self.credit.fireworkRequested.connect(self.overlay.launch_firework)
        self._last_particle=QPointF(-1000,-1000)
        self.profile.textChanged.connect(self._profile_text_changed)
        QApplication.instance().installEventFilter(self)
        # Feed checks are deliberately delayed until the first window paint;
        # startup and the first scan remain fully responsive.
        if self.subscription_store is not None:
            QTimer.singleShot(1800, self.start_subscription_check)

    def toggle_layout_mode(self):
        """Switch between the wide workspace and the stacked portrait view."""
        self.set_layout_mode(not self.portrait_mode)

    def set_layout_mode(self, portrait):
        """Apply a layout mode without rebuilding or losing active controls."""
        portrait=bool(portrait)
        if portrait==self.portrait_mode:
            return
        # A maximized/fullscreen surface cannot safely negotiate the narrow
        # minimums of portrait mode. Require returning to a normal window.
        if portrait and (self.isMaximized() or self.isFullScreen()):
            return
        from desktop_design import set_layout_mode
        if portrait:
            self._restore_window_size=QSize(self.width(),self.height())
            self._restore_maximized=False
            # Keep the portrait width comfortable for text fields while
            # respecting the available desktop.  Height is allowed to grow,
            # but never beyond the usable screen area.
            screen=QApplication.primaryScreen()
            available=screen.availableGeometry() if screen else self.geometry()
            width=min(820,max(680,available.width()-48))
            desired_height=min(1200,max(self.height()+120,980))
            height=min(desired_height,max(520,available.height()-32))
            self.setMinimumSize(640,520)
            set_layout_mode(self,True)
            self.resize(width,height)
            self._center_in_available(available)
            self._portrait_geometry=QRect(self.geometry())
        else:
            normal_minimum=self._normal_minimum_size
            was_maximized=self.isMaximized()
            if was_maximized:
                self.showNormal()
            self.setMinimumSize(normal_minimum)
            set_layout_mode(self,False)
            restored=self._restore_window_size
            width=max(normal_minimum.width(),restored.width())
            height=max(normal_minimum.height(),restored.height())
            screen=QApplication.primaryScreen()
            available=screen.availableGeometry() if screen else self.geometry()
            width=min(width,max(normal_minimum.width(),available.width()-48))
            height=min(height,max(normal_minimum.height(),available.height()-32))
            self.resize(width,height)
            self._center_in_available(available)
            if self._restore_maximized or was_maximized:
                self.showMaximized()

        self._sync_window_mode_controls()

    def _sync_window_mode_controls(self):
        expanded=self.isMaximized() or self.isFullScreen()
        button=getattr(self,'layout_button',None)
        if button is not None:
            button.setEnabled(not expanded)
        chrome=getattr(self,'_pawchive_window_chrome',None)
        if chrome is not None:
            chrome.sync_window_state()

    def showMaximized(self):
        if getattr(self,'portrait_mode',False):
            return
        super().showMaximized()

    def showFullScreen(self):
        if getattr(self,'portrait_mode',False):
            return
        super().showFullScreen()

    def changeEvent(self,event):
        super().changeEvent(event)
        if event.type()==QEvent.Type.WindowStateChange:
            # Native keyboard/Shell actions may bypass the caption buttons.
            if getattr(self,'portrait_mode',False) and (self.isMaximized() or self.isFullScreen()):
                QTimer.singleShot(0,self._restore_portrait_window)
            self._sync_window_mode_controls()

    def _restore_portrait_window(self):
        if not self.portrait_mode:
            return
        self.showNormal()
        geometry=getattr(self,'_portrait_geometry',None)
        if geometry is not None:
            self.setGeometry(geometry)
        self._sync_window_mode_controls()

    def _center_in_available(self, available):
        """Center a mode transition without moving the window off-screen."""
        frame=self.frameGeometry()
        x=available.x()+max(0,(available.width()-frame.width())//2)
        y=available.y()+max(0,(available.height()-frame.height())//2)
        self.move(x,y)

    def set_lang(self,l):
        from ui_i18n import install_qt_language, t as ui_text
        self.lang=l
        install_qt_language(l)
        if self.services:
            self.services.store.set_setting('language',l); self.services.update_language()
        if self.language.currentText()!=l:
            blocked=self.language.blockSignals(True); self.language.setCurrentText(l); self.language.blockSignals(blocked)
        t=TEXT[l]; self.setWindowTitle(t['title']); self.heading.setText('Pawchive'); self.browse.setText(t['browse']); self.start.setText(t['start'])
        chrome=getattr(self,'_pawchive_window_chrome',None)
        if chrome is not None:
            chrome.set_title(t['title'])
            refresh=getattr(chrome,'refresh_language',None)
            if callable(refresh): refresh()
        if hasattr(self,'content_label'):
            self.content_label.setText(ui_text(l,'下载内容'))
            blocked=self.content_combo.blockSignals(True)
            for index,source in enumerate(('图片与附件','仅图片','仅直链附件')):
                self.content_combo.setItemText(index,ui_text(l,source))
            self.content_combo.blockSignals(blocked)
        paused=self.queue_scheduler.has_paused_tasks if getattr(self,'queue_scheduler',None) else False
        self.queue_pause_all_button.setText(ui_text(l,'全部继续' if paused else '全部暂停'))
        failures=sum(task.state=='failed' or bool(task.metadata.get('failure_pinned')) for task in self.queue_scheduler.tasks) if getattr(self,'queue_scheduler',None) else 0
        self.queue_failures_button.setText(ui_text(l,'失败任务 ({count})',count=failures))
        if getattr(self,'update_controller',None): self.update_controller.refresh_language()
        self.open_folder_button.setText(t['open_folder'])
        en=l=='English'; ja=l=='日本語'
        self.profile_label.setText('请输入完整的 pawchive.pw 画师主页链接' if l=='简体中文' else '請輸入完整的 pawchive.pw 畫師首頁連結' if l=='繁體中文' else 'Enter the full Pawchive creator profile URL' if en else 'PawchiveのクリエイターページURLを入力')
        self.folder_label.setText('选择图片保存文件夹' if l=='简体中文' else '選擇圖片儲存資料夾' if l=='繁體中文' else 'Image save folder' if en else '画像の保存先フォルダー')
        self.default_path_check.setText('保存为默认路径' if l=='简体中文' else '設為預設路徑' if l=='繁體中文' else 'Save as default folder' if en else '既定の保存先にする')
        self.order_label.setText('日期顺序' if l=='简体中文' else '日期順序' if l=='繁體中文' else 'Date order' if en else '日付順')
        self.order_combo.setItemText(0,'从新到旧' if l=='简体中文' else '從新到舊' if l=='繁體中文' else 'Newest first' if en else '新しい順')
        self.order_combo.setItemText(1,'从旧到新' if l=='简体中文' else '從舊到新' if l=='繁體中文' else 'Oldest first' if en else '古い順')
        self.title_check.setText('按图集标题分别存入文件夹' if l=='简体中文' else '按圖集標題分別存入資料夾' if l=='繁體中文' else 'Save each post in a title folder' if en else '投稿ごとにタイトル別フォルダーへ保存')
        self.select_posts_check.setText('先扫描标题并选择下载图集' if l=='简体中文' else '先掃描標題並選擇下載圖集' if l=='繁體中文' else 'Scan titles and choose posts first' if en else 'タイトルをスキャンして投稿を選択')
        self.rename_check.setText('下载后按日期顺序编号重命名' if l=='简体中文' else '下載後按日期順序編號重新命名' if l=='繁體中文' else 'Rename files in date order' if en else '日付順に番号を付けて名前を変更')
        self.start_number_label.setText('起始编号' if l=='简体中文' else '起始編號' if l=='繁體中文' else 'Start number' if en else '開始番号')
        self.note.setText(AUX_TEXT[l]['note_titles' if self.title_check.isChecked() else 'note_flat'])
        self.clear_cache.setToolTip(AUX_TEXT[l]['cache_tooltip']); self.copy_log.setToolTip(AUX_TEXT[l]['copy_tooltip'])
        if not self.busy and self.workflow_state in {'setup','ready'}: self.status.setText(t['ready'])
        self.validate_profile()
        from desktop_design import translate_interface
        translate_interface(self,t,AUX_TEXT[l])
        self.update_plan(); self.set_workflow(self.workflow_state)
        if self.workflow_state=='complete':
            self.file_name.set_full_text(self.ui_text()['settled'])
            self.status.setText(f"{TEXT[l]['done']} · {self.files_saved}")
        elif self.workflow_state=='stopped': self.status.setText(AUX_TEXT[l]['cancelled'])
        elif self.workflow_state=='scan':
            self.file_name.set_full_text(self.ui_text()['scanned'].format(pages=self._scan_pages,posts=self._scan_posts))
            self.reflect_scan_state()
        elif not self.busy and self.files_saved==0: self.file_name.set_full_text(self.ui_text()['waiting'])
        self.refresh_reward_badge()
        if hasattr(self,'splash') and self.splash.isVisible(): self.splash.set_language(l)

    def _reward_theme_available(self):
        if not self.services:
            return False
        try:
            return len(self.services.store.reward_profile().get('skins', ())) > 1
        except Exception:
            return False

    def refresh_background_controls(self):
        controls=getattr(self,'background_controls',None)
        switch=getattr(self,'background_mode_switch',None)
        slider=getattr(self,'background_opacity',None)
        if controls is None or switch is None or slider is None:
            return
        store=self.background_store
        if store is None:
            controls.hide(); return
        has_custom=store.has_custom(); reward_available=self._reward_theme_available()
        controls.setVisible(has_custom or reward_available)
        mode=store.mode()
        switch.blockSignals(True); switch.setChecked(mode=='custom'); switch.blockSignals(False)
        switch.setEnabled(has_custom or reward_available)
        config=store.page_config(self.background_page)
        value=config.get('overlay',56) if config else 56
        slider.blockSignals(True); slider.setValue(int(value)); slider.blockSignals(False)
        slider.setEnabled(bool(config) and mode=='custom')
        self.background_opacity_value.setEnabled(slider.isEnabled())
        self.background_opacity_value.setText(f'{int(value)}%')

    def toggle_background_source(self, checked):
        store=self.background_store
        if store is None:
            return
        if checked:
            target='custom' if store.has_custom() else 'default'
        else:
            target='reward' if self._reward_theme_available() else 'default'
        try:
            store.set_mode(target)
        except Exception:
            return
        if target=='reward':
            self.apply_theme(self._dark)
        self.refresh_background()

    def set_background_opacity(self, value):
        value=int(value)
        if hasattr(self,'background_opacity_value'):
            self.background_opacity_value.setText(f'{value}%')
        store=self.background_store
        if store is None:
            return
        try:
            stored=store.set_overlay(self.background_page,value)
        except Exception:
            return
        if self.background_layer and not self.background_layer.pixmap.isNull():
            self.background_layer.overlay=stored; self.background_layer.update()

    def show_background_settings(self):
        if self.background_store is None:
            return
        dialog=BackgroundSettingsDialog(
            self,self.background_store,language=self.lang,
            reward_available=self._reward_theme_available(),
            current_page=self.background_page)
        dialog.changed.connect(self.refresh_background)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()
        self.refresh_background()

    def refresh_background(self):
        store=self.background_store
        if store is None:
            self._set_custom_background_surface(False)
            self.background_layer.clear_background()
            self.refresh_background_controls()
            return
        mode=store.mode()
        config=store.page_config(self.background_page) if mode=='custom' else None
        store.activate_page(self.background_page)
        pixmap=store.cached_preview(self.background_page,load_preview) if config else None
        if pixmap is None or config is None:
            self._set_custom_background_surface(False)
            self.background_layer.clear_background()
        else:
            self._set_custom_background_surface(True)
            store.release_except(self.background_page)
            self.background_layer.set_background(
                pixmap,config.get('overlay',56),config.get('aspect',1.0))
        self._apply_background_orientation(config)
        self.refresh_background_controls()

    def _set_custom_background_surface(self, active):
        """Toggle translucent work cards only while a custom image is active."""
        active=bool(active)
        if bool(self.property('customBackground'))==active:
            return
        self.setProperty('customBackground',active)
        style=self.style()
        if style is None:
            return
        style.unpolish(self); style.polish(self)
        # The selector is inherited from appRoot; repolish direct cards so a
        # mode switch is visible immediately without rebuilding the layout.
        for widget in self.findChildren(QWidget):
            if widget.objectName() in {'mainCard','taskCard','logCard','queueCard','queueItem'}:
                style.unpolish(widget); style.polish(widget); widget.update()

    def _apply_background_orientation(self, config):
        tall=bool(config and float(config.get('aspect',1.0)) < .82)
        if tall and (self.isMaximized() or self.isFullScreen()):
            return
        if tall and not self._background_auto_portrait:
            self._background_previous_portrait=self.portrait_mode
            self._background_auto_portrait=True
            if not self.portrait_mode:
                self.set_layout_mode(True)
        elif not tall and self._background_auto_portrait:
            previous=self._background_previous_portrait
            self._background_auto_portrait=False
            if not previous and self.portrait_mode:
                self.set_layout_mode(False)
    def update_note(self,checked=None):
        self.note.setText(AUX_TEXT[self.lang]['note_titles' if self.title_check.isChecked() else 'note_flat'])
    def set_busy_state(self,busy):
        self.busy=busy
        if hasattr(self,'content_combo'): self.content_combo.setEnabled(not busy)
        if busy: self.completed_path=None; self.open_folder_button.hide()
        controls=(self.profile,self.order_combo,self.title_check,self.select_posts_check,self.rename_check,self.start_number,self.default_path_check,self.browse,self.paste_button)
        for widget in controls: widget.setEnabled(not busy)
        self.clear_cache.setEnabled(not busy)
        self.validate_profile()
    def validate_profile(self,text=None):
        value=(text if text is not None else self.profile.text()).strip()
        valid=bool(PROFILE_URL_RE.fullmatch(value))
        has_folder=bool(self.path.text().strip())
        self.start.setEnabled(valid and has_folder and not self.busy)
        t=AUX_TEXT[self.lang]
        if valid:
            self.profile_hint.setText(t['profile_valid'] if has_folder else t['folder_needed']); self.profile_hint.setStyleSheet(('color:#83d5b1' if self._dark else 'color:#288160') if has_folder else ('color:#d7b77e' if self._dark else 'color:#9b7132'))
        elif value:
            self.profile_hint.setText(t['profile_invalid']); self.profile_hint.setStyleSheet('color:#f3a6b1' if self._dark else 'color:#b84860')
        else:
            self.profile_hint.setText(t['profile_hint']); self.profile_hint.setStyleSheet('color:#99a6bb' if self._dark else 'color:#68758a')
        if not self.busy and self.workflow_state in {'setup','ready'}:
            self.set_workflow('ready' if valid and has_folder else 'setup')
    def apply_theme(self,dark):
        from desktop_design import apply_style
        self._dark=dark
        skin=self.reward_skin
        if self.services:
            skin=self.services.store.reward_profile()['selected_skin']
            self.reward_skin=skin
        apply_style(self,dark,skin)
        for check in self.findChildren(GlowCheckBox): check.setProperty('lightTheme',not dark)
        self.validate_profile()
        if hasattr(self,'splash'): self.splash.counter.setStyleSheet(f'color:{self.palette_tokens["muted"]}')
        if hasattr(self,'overlay'): self.overlay.update()
        self.refresh_reward_badge()
        self.refresh_background_controls()

    def apply_reward_skin(self, skin):
        """Apply and persist an unlocked reward skin from the calendar."""
        if not self.services:
            return False
        try:
            self.services.store.set_reward_skin(skin)
        except (ValueError, TypeError):
            return False
        self.reward_skin=str(skin)
        self.apply_theme(self._dark)
        return True

    def refresh_reward_badge(self):
        badge=getattr(self,'reward_badge',None)
        if badge is None:
            return
        if not self.services:
            badge.setText(''); badge.setToolTip(''); return
        from feature_text import reward_tr
        profile=self.services.store.reward_profile()
        name=reward_tr(self.lang,'level_'+profile['level'])
        recent=profile['badges'][-1] if profile['badges'] else None
        badge_name=reward_tr(self.lang,'badge_'+recent) if recent else ''
        badge.setText(f'✦ {name}' + (f' · {badge_name}' if badge_name else ''))
        badge.setToolTip(reward_tr(self.lang,'downloads',files=profile['downloaded_files'],sessions=profile['download_sessions'])
                         + (f' · {badge_name}' if badge_name else ''))
        cards_button=getattr(self,'artist_cards_button',None)
        if cards_button is not None:
            from artist_cards import list_cards
            count=len(list_cards(self.services.store))
            cards_button.setText('✦' if count == 0 else f'✦ {count}')
            cards_button.setToolTip(('画师档案卡' if self.lang=='简体中文' else '畫師檔案卡' if self.lang=='繁體中文' else 'Artist cards' if self.lang=='English' else 'アーティストカード') + (f' · {count}' if count else ''))
        # Reward refreshes happen on theme/check-in changes too; reapply the
        # subscription marker afterward so an unread update is never hidden.
        if hasattr(self, 'subscription_store'):
            self.refresh_subscription_badge()

    def show_artist_cards(self):
        if not self.services:
            return
        from feature_dialogs import ArtistCardsDialog
        dialog=ArtistCardsDialog(self,self.services.store)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()
            self.refresh_background()

    def open_artist_profile(self, profile):
        record=self.subscription_store.get(profile) if self.subscription_store else None
        baseline=record.get('baseline_ts') if record and record.get('unread') else None
        return self.confirm_artist_download(profile,updates_after=baseline)

    def _apply_download_plan(self, plan, profile):
        self.handle_link_text(profile,prompt_post=False)
        self.path.setText(plan['folder'])
        self.title_check.setChecked(plan['by_title'])
        self.rename_check.setChecked(plan['rename_order'])
        self.content_combo.setCurrentIndex(max(0,self.content_combo.findData(plan['content_mode'])))

    def confirm_artist_download(self, profile, updates_after=None):
        if self.busy: return False
        from download_plan import ask_download_plan
        plan=ask_download_plan(self,profile,updates_after=updates_after)
        if plan is None: return False
        self._apply_download_plan(plan,profile)
        self.select_posts_check.setChecked(True)
        self._next_record_artist=True
        self._scan_after_timestamp=updates_after
        self.download()
        return True

    def confirm_recommended_download(self, data, kind):
        if self.busy: return False
        info=extract_link(str(data.get('url') or ''))
        if info is None: return False
        if kind!='post': return self.confirm_artist_download(info.profile_url)
        if not info.post_url: return False
        from download_plan import ask_download_plan
        plan=ask_download_plan(self,info.profile_url,post=info.post_url)
        if plan is None: return False
        self._apply_download_plan(plan,info.profile_url)
        self._next_record_artist=False
        entry=dict(data); entry.update(url=info.post_url,content_mode='images')
        return self._start_queue([entry],info.profile_url)

    @staticmethod
    def _latest_download_timestamp(records):
        from subscriptions import parse_site_datetime
        return max((parse_site_datetime(record.get('date')) or 0 for record in records
                    if isinstance(record,dict)),default=0)

    def refresh_subscription_badge(self):
        """Update the tiny feed notification without touching worker UI."""
        store=getattr(self,'subscription_store',None)
        button=getattr(self,'artist_cards_button',None)
        if store is None or button is None:
            return
        try:
            unread=store.unread_count()
        except Exception:
            unread=0
        try:
            from artist_cards import list_cards
            count=len(list_cards(self.services.store))
        except Exception:
            count=0
        if self._subscription_check_running:
            frames=('◐','◓','◑','◒')
            self._subscription_spinner_phase=(self._subscription_spinner_phase+1)%4
            button.setText(frames[self._subscription_spinner_phase]+' '+('✦' if count==0 else f'✦ {count}'))
            button.setToolTip(ui_text(self.lang,'正在检查画师更新…')); return
        button.setText(('● ' if unread else '') + ('✦' if count == 0 else f'✦ {count}'))
        button.setToolTip(ui_text(self.lang,'有画师更新' if unread else '画师档案卡'))

    def show_subscriptions(self):
        if not self.services:
            return
        from subscription_ui import SubscriptionDialog
        dialog=SubscriptionDialog(self,self.services.store)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater(); self.refresh_subscription_badge()

    def start_subscription_check(self):
        store=getattr(self,'subscription_store',None)
        if store is None or getattr(self,'_subscription_worker',None) is not None:
            return
        try:
            if not store.list():
                self.refresh_subscription_badge(); return
            from subscriptions import SubscriptionCheckWorker
            worker=SubscriptionCheckWorker(store,self)
            self._subscription_worker=worker
            self._subscription_loading_profiles={item['profile'] for item in store.list()}
            self._subscription_check_running=True
            self._subscription_badge_timer.start()
            worker.progress.connect(self._subscription_progress)
            worker.finished.connect(self._subscription_check_finished)
            worker.start()
        except Exception:
            self._subscription_worker=None
            self._subscription_check_running=False
            self._subscription_loading_profiles.clear()
            self._subscription_badge_timer.stop()

    def _subscription_progress(self,profile,payload):
        self._subscription_loading_profiles.discard(profile)
        if self.services and payload and not payload.get('error'):
            name=payload.get('name')
            if name and name!='Unknown Artist':
                try:
                    from artist_cards import update_artist_metadata
                    update_artist_metadata(self.services.store,profile,name=name)
                except (OSError,ValueError,RuntimeError): pass
        self.refresh_subscription_badge()

    def _subscription_check_finished(self):
        worker=self._subscription_worker; self._subscription_worker=None
        self._subscription_check_running=False
        self._subscription_loading_profiles.clear()
        self._subscription_badge_timer.stop()
        if worker is not None:
            worker.deleteLater()
        self.refresh_subscription_badge()

    def _maybe_artist_card(self, profile, posts=1, files=0, latest_download_ts=0):
        if not self.services:
            return
        try:
            from artist_cards import ArtistMetadataWorker, record_download
            card=record_download(self.services.store, profile, posts=posts, files=files,latest_download_ts=latest_download_ts)
        except (OSError, ValueError, RuntimeError):
            return
        if not card:
            self.refresh_reward_badge(); return
        # Every completed creator download creates one deduplicated profile
        # card. Metadata is fetched in a worker; avatar downloads wait until
        # a cabinet page is visible and share its bounded request pool.
        metadata = ArtistMetadataWorker(card['profile'], self)
        self._artist_avatar_workers['meta:' + card['profile']] = metadata
        metadata.result.connect(self._artist_metadata_finished)
        metadata.finished.connect(metadata.deleteLater)
        metadata.start()
        if getattr(self,'subscription_store',None) is not None:
            try:
                self.subscription_store.add(profile, name=card.get('name') or 'Unknown Artist',
                    baseline_ts=latest_download_ts or None)
                if latest_download_ts: self.subscription_store.advance_download(profile,latest_download_ts)
            except Exception:
                pass
        self.refresh_reward_badge(); self.refresh_subscription_badge()

    def _artist_metadata_finished(self, profile_url, name, avatar, error):
        self._artist_avatar_workers.pop('meta:'+profile_url,None)
        if not self.services: return
        try:
            from artist_cards import update_artist_metadata
            card=update_artist_metadata(self.services.store,profile_url,name=name if not error else None,
                avatar_status='missing' if error else None)
            if card is None: return
            if self.subscription_store is not None and name:
                self.subscription_store.add(profile_url,name=card.get('name') or 'Unknown Artist')
            self.refresh_reward_badge()
        except (OSError,ValueError,RuntimeError): return
    def ui_text(self):
        from desktop_design import COPY
        return COPY[self.lang]
    def set_workflow(self,state):
        self.workflow_state=state
        t=self.ui_text(); self.state_badge.setText(t.get(state,t['setup']))
        stage={'setup':0,'ready':0,'scan':1,'choose':1,'download':2,'stopping':2,'complete':3}.get(state,self.stage_rail.stage)
        style='error' if state=='error' else 'done' if state=='complete' else 'idle' if state in {'setup','stopped'} else 'active'
        self.stage_rail.set_stage(stage,style)
        p=getattr(self,'palette_tokens',{})
        color=('#f3a6b1' if self._dark else '#b84860') if state=='error' else ('#83d5b1' if self._dark else '#288160') if state=='complete' else p.get('accent','#9cb3ff')
        self.state_badge.setStyleSheet(f'color:{color}')
        self.stop_button.setVisible(state in {'download','stopping'} and self.busy)
        self.stop_button.setEnabled(state!='stopping')
    def update_plan(self,*args):
        if not hasattr(self,'plan'): return
        t=self.ui_text()
        self.plan.setText('  ·  '.join([t['newest' if self.order_combo.currentIndex()==0 else 'oldest'],
            t['folders' if self.title_check.isChecked() else 'flat'],
            t['selection' if self.select_posts_check.isChecked() else 'all_posts']]))
    @staticmethod
    def _mime_link_text(mime):
        values=[]
        if mime is None:
            return ''
        if mime.hasText():
            values.append(mime.text())
        if mime.hasUrls():
            values.extend(url.toString() for url in mime.urls())
        return '\n'.join(value for value in values if value)

    def handle_link_text(self, text, *, prompt_post=True):
        """Accept a profile/post link from paste, drag-and-drop, or the sentinel."""
        if self.busy:
            return None
        info=extract_link(text)
        if info is None:
            self.status.setText(self.ui_text()['paste_empty'])
            return None
        suppress=getattr(self.services,'suppress_clipboard_text',None)
        if callable(suppress):
            suppress(text)
        self._handling_link_input=True
        try:
            self.profile.setText(info.profile_url)
        finally:
            self._handling_link_input=False
        self.profile.setFocus()
        if info.post_url and prompt_post:
            self.offer_post_link(info)
        return info

    def _profile_text_changed(self, text):
        """Handle context-menu paste, which does not produce Ctrl+V key events."""
        if self.busy or self._handling_link_input or self._link_prompt_active:
            return
        info=extract_link(text)
        clipboard_info=extract_link(QApplication.clipboard().text())
        if (info is not None and info.post_url and clipboard_info is not None
                and clipboard_info.post_url==info.post_url):
            self.handle_link_text(text)

    def offer_post_link(self, info):
        if self._link_prompt_active or not isinstance(info, LinkInfo) or not info.post_url:
            return
        self._link_prompt_active=True
        try:
            copy=self.ui_text()
            dialog=QMessageBox(self)
            dialog.setIcon(QMessageBox.Icon.Question)
            dialog.setWindowTitle(copy['link_choice_title'])
            dialog.setText(copy['link_choice_detail'])
            batch=dialog.addButton(copy['link_choice_batch'],QMessageBox.ButtonRole.AcceptRole)
            single=dialog.addButton(copy['link_choice_single'],QMessageBox.ButtonRole.ActionRole)
            dialog.addButton(copy['link_choice_cancel'],QMessageBox.ButtonRole.RejectRole)
            dialog.exec()
            clicked=dialog.clickedButton()
            if clicked is batch:
                self.select_posts_check.setChecked(True)
                self.update_plan()
                self.download()
            elif clicked is single:
                self.download_single_post(info)
        finally:
            self._link_prompt_active=False

    def download_single_post(self, info):
        if not isinstance(info, LinkInfo) or not info.post_url:
            return
        if not self.path.text().strip():
            self.pick()
            if not self.path.text().strip():
                self.status.setText(AUX_TEXT[self.lang]['folder_needed'])
                self.path.setFocus()
                return
        # The existing Worker already knows how to fetch one selected post;
        # retain both slash variants because profile pages are inconsistent.
        selected={info.post_url.rstrip('/'), info.post_url.rstrip('/')+'/' }
        self.launch_worker(info.profile_url, selected)

    def paste_profile(self):
        if self.busy: return
        value=QApplication.clipboard().text().strip()
        if value:
            self.handle_link_text(value)
        else: self.status.setText(self.ui_text()['paste_empty'])

    def dragEnterEvent(self,event):
        if extract_link(self._mime_link_text(event.mimeData())) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self,event):
        info=self.handle_link_text(self._mime_link_text(event.mimeData()))
        if info is not None:
            event.acceptProposedAction()
        else:
            event.ignore()
    def toggle_logs(self):
        self.logs_hidden=not self.logs_hidden
        self.logs_card.setVisible(not self.logs_hidden)
        self.focus_button.setToolTip(self.ui_text()['expand' if self.logs_hidden else 'focus'])
        self.focus_button.setAccessibleName(self.focus_button.toolTip())
        self.footer_hint.setText(self.ui_text()['expand' if self.logs_hidden else 'focus'])
        self.focus_button.setProperty('selected',self.logs_hidden)
        self.focus_button.style().unpolish(self.focus_button); self.focus_button.style().polish(self.focus_button)
    def stop_download(self):
        if self.queue_scheduler is not None and self.busy:
            for task in list(self.queue_scheduler.active_tasks):
                self.queue_scheduler.pause(task.id)
            self.status.setText(self.ui_text()['paused'])
            return
        if not self.busy or self.workflow_state not in {'download','stopping'}: return
        self.stop_requested=True
        worker=getattr(self,'worker',None)
        if worker and worker.isRunning(): worker.requestInterruption()
        self.set_workflow('stopping'); self.status.setText(self.ui_text()['safe_stop'])
    def on_scan_counts(self,pages,posts):
        self._scan_pages=pages; self._scan_posts=posts
        self.album_value.setText(str(posts)); self.file_name.set_full_text(self.ui_text()['scanned'].format(pages=pages,posts=posts))
    def reflect_scan_state(self):
        if self.workflow_state!='scan': return
        splash=getattr(self,'splash',None)
        if splash and splash.finish_requested:
            self.status.setText(AUX_TEXT[self.lang]['scan_finishing'])
        elif splash and splash.paused:
            self.status.setText(self.ui_text()['paused']); self.state_badge.setText(self.ui_text()['paused'])
        else:
            self.status.setText(AUX_TEXT[self.lang]['scan_status']); self.state_badge.setText(self.ui_text()['scan'])
    def reset_dashboard(self,total=0):
        self.files_saved=0; self._album_current=0; self._album_total=total
        self._file_current=0; self._file_total=0
        self._transfer_sample=None; self._speed=0.; self.stop_requested=False
        self._scan_pages=0; self._scan_posts=0
        self.files_value.setText('0'); self.album_value.setText(f'— / {total}' if total else '— / —')
        self.speed_value.setText('—'); self.percent_label.setText('0'); self.percent_unit.show()
        self.file_name.set_full_text(self.ui_text()['waiting']); self.progress.setValue(0)
    def update_transfer(self,name,received,total):
        match=re.search(r'\((\d+)/(\d+)\)',name)
        if match:
            self._album_current,self._album_total=map(int,match.groups())
            self.album_value.setText(f'{self._album_current} / {self._album_total}')
        if name.startswith('✓'):
            self.files_saved+=1; self.files_value.setText(str(self.files_saved)); self.percent_label.setText('100')
            self.percent_unit.show(); self._transfer_sample=None; self._speed=0.; self.speed_value.setText('—')
            return
        current_file=name.rsplit(' / ',1)[-1]
        self.file_name.set_full_text(current_file)
        now=time.monotonic(); previous=self._transfer_sample
        if previous and previous[0]==name and received>=previous[1] and now>previous[2]:
            rate=(received-previous[1])/(now-previous[2])
            self._speed=rate if self._speed<=0 else .28*rate+.72*self._speed
            self.speed_value.setText(f'{self._speed/1048576:.1f} MB/s' if self._speed>=1048576 else f'{self._speed/1024:.0f} KB/s')
        elif not previous or previous[0]!=name or received<previous[1]:
            self._speed=0.; self.speed_value.setText('—')
        self._transfer_sample=(name,received,now)
        self.percent_label.setText(str(max(0,min(100,int(received*100/total)))) if total else '—')
        self.percent_unit.setVisible(bool(total))
    def pick(self):
        d=QFileDialog.getExistingDirectory(self,TEXT[self.lang]['choose'],self.path.text() or str(Path.home()),
            options=QFileDialog.Option.DontUseNativeDialog)
        if d:
            if not self.path.text() or Path(d)!=Path(self.path.text()): self.completed_path=None; self.open_folder_button.hide()
            self.path.setText(d)
            if self.default_path_check.isChecked(): self.settings.setValue('default_save_path',d)
            self.validate_profile()
    def save_default_path_setting(self,checked):
        if checked and self.path.text(): self.settings.setValue('default_save_path',self.path.text())
        elif not checked: self.settings.remove('default_save_path')
    def copy_log_text(self):
        if not self.log.toPlainText():
            self.status.setText(self.ui_text()['copy_empty']); return
        QApplication.clipboard().setText(self.log.toPlainText())
        self.status.setText(AUX_TEXT[self.lang]['log_copied'])
    def clear_cache_files(self):
        if self.busy: return
        try:
            if self.cache_dir.is_dir() and not self.cache_dir.is_symlink():
                for item in self.cache_dir.iterdir():
                    try:
                        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
                            item.unlink(missing_ok=True)
                        elif item.is_dir():
                            shutil.rmtree(item)
                        else:
                            item.unlink(missing_ok=True)
                    except OSError as exc:
                        self.log.append(AUX_TEXT[self.lang]['cache_failed'].format(exc))
            cleanup_stale_cache_dirs(current=self.cache_dir)
            from popular_features import cleanup_inactive_previews
            cleanup_inactive_previews()
            if self.background_store:
                remove_disabled=True
                if self.background_store.has_custom() and self.background_store.mode()!='custom':
                    prompt={
                        '简体中文':'当前未启用的自定义背景将被删除，之后需要重新编辑才能使用。继续清理吗？',
                        '繁體中文':'目前未啟用的自訂背景將被刪除，之後需要重新編輯才能使用。繼續清理嗎？',
                        'English':'Unused custom backgrounds will be removed and must be edited again to use them. Continue?',
                        '日本語':'現在使われていないカスタム背景を削除します。再び使うには編集し直す必要があります。続行しますか？',
                    }.get(self.lang,'Unused custom backgrounds will be removed. Continue?')
                    answer=QMessageBox.question(
                        self,'Pawchive',prompt,
                        QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No)
                    remove_disabled=answer==QMessageBox.StandardButton.Yes
                self.background_store.cleanup_unused(remove_disabled=remove_disabled)
                self.background_store.clear_cache()
                self.refresh_background()
            if self.services:
                try:
                    from artist_cards import cleanup_cache
                    cleanup_cache(self.services.store)
                except (OSError, ValueError, RuntimeError):
                    pass
            self.refresh_recycle_bin()
            self.status.setText(AUX_TEXT[self.lang]['cache_cleared'])
        except (OSError, ValueError, RuntimeError) as exc:
            self.log.append(AUX_TEXT[self.lang]['cache_failed'].format(exc))
    def open_download_folder(self):
        folder=Path(self.completed_path) if self.completed_path else None
        if folder and folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        else:
            self.completed_path=None; self.open_folder_button.hide()
    def choose_titles(self,titles):
        from post_picker import choose_posts
        return choose_posts(self,titles,GlowCheckBox,AUX_TEXT[self.lang])

    def _queue_worker_factory(self, task):
        """Create one journal-aware worker for a single selected collection."""
        selected={str(task.url).rstrip('/'), str(task.url).rstrip('/')+'/' }
        extra={}
        resume_id=(task.metadata or {}).get('job_id')
        if self.services:
            extra=dict(journal=self.services.journal, resume_job_id=resume_id,
                       resume_config=(task.metadata or {}).get('_worker_config'))
        order='newest' if self.order_combo.currentIndex()==0 else 'oldest'
        # Queue tasks are numbered once, after every album settles. Running a
        # per-album renamer here would make priority changes affect filenames.
        worker=Worker(self.path.text(), self._queue_profile or self.profile.text().strip(),
                      order, self.title_check.isChecked(), selected,
                      False, 1, self.lang, content_mode=(task.metadata or {}).get('content_mode','all'), **extra)
        if getattr(worker,'job_id',None):
            self._queue_job_ids[task.id]=worker.job_id
        return worker

    def _queue_view(self):
        scheduler=self.queue_scheduler
        if scheduler is None:
            return
        # Progress signals arrive several times per second from up to five
        # workers. Rebuilding every queue row synchronously made thumbnail
        # loading and style polishing compete with painting. Coalesce updates
        # onto the GUI event loop and render at a bounded cadence.
        if self._queue_view_pending:
            return
        self._queue_view_pending=True
        QTimer.singleShot(80,self._flush_queue_view)

    def _flush_queue_view(self):
        self._queue_view_pending=False
        scheduler=self.queue_scheduler
        if scheduler is None:
            return
        self.download_queue.set_items([task.public_copy() for task in scheduler.tasks])
        paused=scheduler.has_paused_tasks
        from ui_i18n import t as ui_text
        self.queue_pause_all_button.setText(ui_text(self.lang,'全部继续' if paused else '全部暂停'))
        failures=sum(t.state=='failed' or bool(t.metadata.get('failure_pinned')) for t in scheduler.tasks)
        self.queue_failures_button.setText(ui_text(self.lang,'失败任务 ({count})',count=failures))
        self.queue_count.setText(self.ui_text()['queue_count'].format(count=len(scheduler.tasks)))

    def _initialize_queue_task_progress(self, task):
        """Seed a restored row from its durable journal before it starts.

        A resumed checkpoint can already contain verified files while the
        freshly scanned queue entry supplies the complete attachment count.
        Keeping both values on the task makes the overview bar represent the
        remaining queue as a whole and keeps the row's cover/metadata intact.
        """
        records=task.metadata.pop('_journal_records',None)
        prepared=records is not None
        records=records or []
        job_id=(task.metadata or {}).get('job_id')
        if not prepared and self.services and job_id:
            try:
                job=self.services.journal.load(str(job_id))
                records=[dict(record) for record in job.get('files',())
                         if isinstance(record,dict)]
                task.metadata['completed_records']=[dict(record) for record in records
                                                    if record.get('status') in ('complete','ready')]
            except (KeyError, OSError, TypeError, ValueError):
                records=[]
        known_total=max(int(getattr(task,'files_total',0) or 0),len(records))
        if not known_total:
            return
        completed=sum(1 for record in records if record.get('status') in ('complete','ready'))
        task.files_total=known_total
        task.completed_files=min(known_total,completed)
        task.files_current=task.completed_files
        task.progress=min(1.0,task.completed_files/known_total)
        task.metadata['_counted_completed_files']=task.completed_files
        task.current=0
        task.total=0

    def _update_queue_aggregate_progress(self):
        scheduler=self.queue_scheduler
        if scheduler is None:
            return
        tasks=scheduler.tasks
        total=sum(max(0,int(task.files_total or 0)) for task in tasks)
        if total <= 0:
            return
        completed=sum(min(float(task.files_total), max(0.0, float(task.progress)) * task.files_total)
                      for task in tasks if task.files_total)
        value=max(0,min(100,int(round(completed * 100 / total))))
        self.progress.setValue(value)
        self.percent_label.setText(str(value))
        self.percent_unit.show()
        self.taskbar_progress.set_value(value)

    def _queue_task_updated(self, task):
        scheduler=self.queue_scheduler
        if scheduler is None:
            return
        job_id=self._queue_job_ids.get(task.id)
        actual=scheduler.task(task.id)
        if actual is not None and job_id:
            actual.metadata['job_id']=job_id
        self._queue_view()
        if task.state in {'running','pausing','cancelling'} and task.label:
            self.update_progress(task.label, task.current, task.total)
        self._update_queue_aggregate_progress()

    def _queue_task_finished(self, task):
        if task.state=='completed':
            metadata=task.metadata if isinstance(task.metadata,dict) else {}
            try:
                counted=max(0,int(metadata.get('_counted_completed_files',0) or 0))
            except (TypeError,ValueError):
                counted=0
            current=max(0,int(task.completed_files or 0))
            self._queue_completed_files += max(0,current-counted)
            metadata['_counted_completed_files']=max(counted,current)
        self._queue_view()
        self._update_queue_aggregate_progress()

    def _queue_payload_path(self, task):
        """Find a private album/partial path which can be moved to recycle bin."""
        folder=Path(self.path.text()).expanduser() if self.path.text().strip() else None
        by_title=bool((task.metadata or {}).get('by_title',self.title_check.isChecked()))
        if folder and by_title:
            try:
                candidate=folder/safe_path_component(task.title,limit=100)
                if candidate.exists() and candidate.is_dir():
                    return candidate
            except (OSError,ValueError):
                pass
        job_id=self._queue_job_ids.get(task.id) or (task.metadata or {}).get('job_id')
        if self.services and job_id:
            try:
                job=self.services.journal.load(job_id)
                album=job.get('album') or {}
                if album.get('path') and Path(album['path']).is_dir():
                    return Path(album['path'])
                for record in job.get('files',[]):
                    part=record.get('part')
                    if part and Path(part).is_file():
                        return Path(part)
            except (KeyError,ValueError,OSError):
                pass
        return None

    def _queue_task_recycled(self, task):
        if task.id in self._queue_recycle_ids:
            return
        self._queue_recycle_ids.add(task.id)
        if self.recycle_bin is not None:
            metadata=dict(task.metadata or {})
            metadata.update({'task_id':task.id,'sequence':int(task.sequence),
                             'formats':list(task.formats),'by_title':self.title_check.isChecked()})
            try:
                self.recycle_bin.add(
                    title=task.title,
                    profile_url=self._queue_profile or self.profile.text().strip(),
                    post_url=task.url,
                    download_url=task.url,
                    cover_path=task.thumbnail,
                    payload_path=self._queue_payload_path(task),
                    metadata=metadata)
            except (OSError,ValueError,RuntimeError) as exc:
                self.log.append(AUX_TEXT[self.lang]['cache_failed'].format(exc))
        job_id=self._queue_job_ids.pop(task.id,None) or (task.metadata or {}).get('job_id')
        if self.services and job_id:
            try:
                self.services.journal.discard(job_id)
            except (KeyError,ValueError,OSError):
                pass
        self.refresh_recycle_bin()
        self._queue_view()

    def _queue_worker_idle(self):
        scheduler=self.queue_scheduler
        if scheduler is None:
            return
        if self._queue_rename_completed(): return
        self._settle_queue_idle()

    def _settle_queue_idle(self, finalization_error=False):
        scheduler=self.queue_scheduler
        if scheduler is None: return
        tasks=list(scheduler.tasks)
        completed=(all(task.state=='completed' for task in tasks) if tasks else False) and not finalization_error
        paused=any(task.state=='paused' for task in tasks)
        failed=any(task.state=='failed' for task in tasks) or bool(finalization_error)
        cancelled=any(task.state=='cancelled' for task in tasks)
        self.set_busy_state(False)
        self.stop_button.hide()
        self.set_workflow('complete' if completed else 'stopped' if paused or cancelled else 'error' if failed else 'complete')
        if completed:
            self.status.setText(f"{TEXT[self.lang]['done']} · {self._queue_completed_files}")
            self.completed_path=str(Path(self.path.text()).resolve())
            self.open_folder_button.setVisible(Path(self.completed_path).is_dir())
            self.file_name.set_full_text(self.ui_text()['settled'])
            self.files_saved=self._queue_completed_files
            self.files_value.setText(str(self.files_saved))
            self.progress.setValue(100)
            self.percent_label.setText('100')
        elif paused:
            self.status.setText(self.ui_text()['paused'])
        elif cancelled:
            self.status.setText(AUX_TEXT[self.lang]['cancelled'])
        if self.services:
            try:
                newly_completed_tasks=[task for task in tasks
                                       if task.state=='completed' and task.id not in self._queue_rewarded_ids]
                newly_completed=sum(max(0,int(task.completed_files or 0)) for task in newly_completed_tasks)
                if newly_completed:
                    self.services.store.record_download(newly_completed)
                    self._queue_rewarded_ids.update(task.id for task in tasks if task.state=='completed')
                    artist_tasks=[task for task in newly_completed_tasks if (task.metadata or {}).get('_worker_config',{}).get('record_artist',True)]
                    if artist_tasks:
                        records=[r for task in artist_tasks for r in task.metadata.get('completed_records',[])]
                        self._maybe_artist_card(self._queue_profile,posts=len(artist_tasks),
                            files=sum(max(0,int(task.completed_files or 0)) for task in artist_tasks),
                            latest_download_ts=self._latest_download_timestamp(records))
                self.refresh_reward_badge()
                self.services.task_finished(completed, paused=paused)
            except Exception:
                pass
        if self._queue_closing:
            QTimer.singleShot(0,self.close)

    @staticmethod
    def _record_target(record):
        if isinstance(record,dict):
            return record.get('target')
        if isinstance(record,(tuple,list)) and len(record)>1:
            return record[1]
        return None

    @staticmethod
    def _file_extension(path):
        name=Path(path).name.lower()
        for suffix in ('.tar.gz','.tar.xz','.tar.bz2'):
            if name.endswith(suffix):
                return name[-len(suffix):]
        return Path(path).suffix

    def _queue_rename_completed(self):
        scheduler=self.queue_scheduler
        if scheduler is None or getattr(self,'_queue_finalizing',False): return False
        tasks=scheduler.tasks
        if any(t.state not in ('completed','cancelled') for t in tasks): return False
        ids=[(t.metadata or {}).get('job_id') for t in tasks if t.state=='completed']
        ids=[i for i in ids if i]
        if not self.services or not ids: return False
        from queue_operations import QueueFinalizeWorker
        worker=QueueFinalizeWorker(self.services.journal,ids,
            dict(unique_path=unique_path,safe_path_component=safe_path_component),self)
        self._queue_finalizer=worker; self._queue_finalizing=True
        self.status.setText(ui_text(self.lang,'正在整理图片编号…'))
        worker.result.connect(self._queue_finalize_done)
        worker.failed.connect(self._queue_finalize_failed)
        worker.start()
        return True

    def _queue_finalize_done(self, records):
        worker=self._queue_finalizer
        if worker is not None and worker.isRunning():
            QTimer.singleShot(10,lambda:self._queue_finalize_done(records)); return
        if worker is not None: worker.deleteLater()
        self._queue_finalizing=False; self._queue_finalizer=None
        for task in self.queue_scheduler.tasks:
            job_id=(task.metadata or {}).get('job_id')
            if job_id in records: task.metadata['completed_records']=records[job_id]
        self._settle_queue_idle()

    def _queue_finalize_failed(self, message):
        worker=self._queue_finalizer
        if worker is not None and worker.isRunning():
            QTimer.singleShot(10,lambda:self._queue_finalize_failed(message)); return
        if worker is not None: worker.deleteLater()
        self._queue_finalizing=False; self._queue_finalizer=None
        self._settle_queue_idle(finalization_error=True)
        self.set_workflow('error'); self.status.setText(ui_text(self.lang,'图片编号未完成，可重启后继续恢复'))
        self.log.append(str(message))

    def _start_queue(self, entries, profile):
        if self.busy:
            return False
        if not self.path.text().strip():
            self.pick()
            if not self.path.text().strip(): return False
        entries=[dict(entry) for entry in entries if isinstance(entry,dict) and entry.get('url')]
        if not entries: return False
        self._queue_insert_timer.stop()
        self._selected_content_mode=self.content_combo.currentData()
        self._queue_profile=str(profile).strip().rstrip('/')
        self._queue_completed_files=0
        self._queue_recycle_ids=set(); self._queue_job_ids={}; self._queue_rewarded_ids=set()
        self._queue_selected_ids=[]; self._queue_closing=False
        self.reset_dashboard(len(entries))
        self.set_busy_state(True); self.set_workflow('download')
        self.status.setText(ui_text(self.lang,'正在准备下载队列…'))
        from queue_operations import QueuePreparationWorker
        config=dict(folder=str(Path(self.path.text()).expanduser().resolve()),
            profile=self._queue_profile, order='newest' if self.order_combo.currentIndex()==0 else 'oldest',
            by_title=bool(self.title_check.isChecked()), rename_order=False, start_number=1,
            lang=self.lang, queue_task=True, queue_rename_order=bool(self.rename_check.isChecked()),
            queue_start_number=1,layout_version=2,record_artist=bool(self._next_record_artist),
            content_mode=getattr(self,'_selected_content_mode','all'))
        worker=QueuePreparationWorker(self.services.journal if self.services else None,entries,config,self)
        self._queue_preparation=worker
        worker.result.connect(self._queue_prepared)
        worker.failed.connect(self._queue_prepare_failed)
        worker.start()
        self._next_record_artist=True
        return True

    def _queue_prepare_failed(self, message):
        worker=self._queue_preparation
        if worker is not None and worker.isRunning():
            QTimer.singleShot(10,lambda:self._queue_prepare_failed(message)); return
        if worker is not None: worker.deleteLater()
        self._queue_preparation=None
        self.set_busy_state(False); self.set_workflow('error')
        self.status.setText(ui_text(self.lang,'准备下载失败')); self.log.append(str(message))
        if self._close_when_worker_stops: QTimer.singleShot(0,self.close)

    def _queue_prepared(self, entries):
        worker=self._queue_preparation
        if worker is not None and worker.isRunning():
            QTimer.singleShot(10,lambda:self._queue_prepared(entries)); return
        if worker is not None: worker.deleteLater()
        self._queue_preparation=None
        if self._close_when_worker_stops:
            self.set_busy_state(False); QTimer.singleShot(0,self.close); return
        self._queue_scan_entries=entries
        self.queue_scheduler=DownloadScheduler(self._queue_worker_factory,self)
        self.queue_scheduler.task_updated.connect(self._queue_task_updated)
        self.queue_scheduler.task_finished.connect(self._queue_task_finished)
        self.queue_scheduler.task_recycled.connect(self._queue_task_recycled)
        self.queue_scheduler.queue_changed.connect(self._queue_view)
        self.queue_scheduler.became_idle.connect(self._queue_worker_idle)
        # Show the queue in small batches so a large selection does not block
        # the first paint or allocate all thumbnail widgets in one event.
        self._queue_pending_entries=list(entries)
        self._queue_insert_batch()

    def _queue_insert_batch(self):
        from shiboken6 import isValid
        if not isValid(self) or self.queue_scheduler is None or not isValid(self.queue_scheduler): return
        if self._close_when_worker_stops:
            self._queue_pending_entries=[]
            self.prepare_queue_exit()
            if not self.busy: QTimer.singleShot(0,self.close)
            return
        entries=self._queue_pending_entries[:16]
        del self._queue_pending_entries[:16]
        for item in entries:
            task=self.queue_scheduler.add_task(item)
            job_id=(task.metadata or {}).get('job_id')
            if job_id: self._queue_job_ids[task.id]=job_id
            self._initialize_queue_task_progress(task)
        if self._queue_pending_entries:
            self._queue_insert_timer.start(0)
            return
        self._queue_completed_files=sum(max(0,int((t.metadata or {}).get('_counted_completed_files',0)))
                                        for t in self.queue_scheduler.tasks)
        self.files_saved=self._queue_completed_files
        self.files_value.setText(str(self.files_saved))
        self._update_queue_aggregate_progress(); self._queue_view()
        self.stop_button.hide()
        self.status.setText(TEXT[self.lang]['working'])
        if self._close_when_worker_stops:
            self.prepare_queue_exit(); QTimer.singleShot(0,self.close); return
        self.queue_scheduler.start()

    def resume_recoverable_jobs(self, jobs):
        """Re-scan recoverable queue posts before rebuilding the visible queue.

        Checkpoints intentionally keep only small metadata and durable file
        records.  Their old cover paths point into a previous temporary cache,
        so a restart must fetch fresh cards and attachment counts before the
        queue is shown again.  The scan runs off the GUI thread and only
        examines the post URLs represented by the recoverable jobs.
        """
        if self.busy or not jobs:
            return False
        first = jobs[0].get('config', {}) if isinstance(jobs[0], dict) else {}
        profile = str(first.get('profile', '')).strip().rstrip('/')
        selected = []
        for job in jobs:
            config = job.get('config', {}) if isinstance(job, dict) else {}
            urls = config.get('selected_post_urls') or []
            if urls:
                selected.append(str(urls[0]).rstrip('/'))
        if not profile or not selected:
            return False
        self.profile.setText(profile)
        self.path.setText(str(first.get('folder', '')))
        self.order_combo.setCurrentIndex(0 if first.get('order', 'newest') == 'newest' else 1)
        self.title_check.setChecked(bool(first.get('by_title', True)))
        self.rename_check.setChecked(bool(first.get('queue_rename_order', first.get('rename_order', False))))
        self.start_number.setValue(int(first.get('queue_start_number', first.get('start_number', 1))))
        self._selected_content_mode=first.get('content_mode','all')
        self.content_combo.setCurrentIndex(max(0,self.content_combo.findData(self._selected_content_mode)))
        self._resume_jobs = list(jobs)
        self._resume_profile = profile
        self._resume_scan_worker = ProfileScanWorker(profile, self.cache_dir, selected_urls=selected)
        self.reset_dashboard(len(jobs))
        self.set_busy_state(True)
        self.set_workflow('scan')
        self.status.setText(AUX_TEXT[self.lang]['scan_status'])
        self._resume_scan_worker.result.connect(self._finish_resume_recoverable_jobs)
        self._resume_scan_worker.failed.connect(self._fail_resume_recoverable_jobs)
        self.splash=ScanAnimation(self); self.splash.set_language(self.lang)
        self.splash.scanner=self._resume_scan_worker
        self._resume_scan_worker.counts.connect(self.on_scan_counts)
        self._resume_scan_worker.counts.connect(self.splash.update_counts)
        self.splash.show()
        self._resume_scan_worker.start()
        return True

    @staticmethod
    def _resume_url_key(value):
        return str(value or '').strip().rstrip('/')

    def _finish_resume_recoverable_jobs(self, scanned):
        worker = getattr(self, '_resume_scan_worker', None)
        if worker is not None and worker.isRunning():
            worker.wait()
        self._resume_scan_worker = None
        if hasattr(self,'splash'):
            self.splash._allow_close=True; self.splash.close()
        jobs = list(getattr(self, '_resume_jobs', []) or [])
        profile = str(getattr(self, '_resume_profile', '') or '').rstrip('/')
        by_url = {self._resume_url_key(item.get('url')): dict(item)
                  for item in (scanned or []) if isinstance(item, dict) and item.get('url')}
        entries = []
        for job in jobs:
            config = job.get('config', {}) if isinstance(job, dict) else {}
            selected = config.get('selected_post_urls') or []
            if not selected:
                continue
            post_url = self._resume_url_key(selected[0])
            found = by_url.get(post_url, {})
            image = str(found.get('image') or '')
            if not image or not Path(image).is_file():
                old_image = str(config.get('task_thumbnail') or '')
                image = old_image if old_image and Path(old_image).is_file() else ''
            title = str(found.get('title') or config.get('task_title') or post_url)
            formats = list(found.get('formats') or config.get('task_formats') or [])
            try:
                file_count = max(0, int(found.get('file_count') or config.get('task_file_count') or 0))
            except (TypeError, ValueError):
                file_count = 0
            metadata = {
                'job_id': job.get('id'),
                'queue_rename_order': bool(config.get('queue_rename_order', config.get('rename_order',False))),
                'content_mode': config.get('content_mode','all'),
                'queue_start_number': int(config.get('queue_start_number', 1)),
                'by_title': bool(config.get('by_title', True)),
            }
            entries.append({'title': title, 'url': post_url, 'image': image,
                            'formats': formats, 'file_count': file_count,
                            'metadata': metadata, 'image_count':found.get('image_count',0),
                            'attachment_count':found.get('attachment_count',0)})
            # Keep the durable checkpoint useful if another restart occurs;
            # the image itself remains in this run's bounded cache directory.
            try:
                job['config']['task_title'] = title
                job['config']['task_thumbnail'] = image
                job['config']['task_thumbnail_url'] = str(found.get('image_url') or config.get('task_thumbnail_url') or '')
                job['config']['task_formats'] = formats
                job['config']['task_file_count'] = file_count
                if self.services:
                    self.services.journal.save(job, dirty_records=[])
            except (KeyError, OSError, TypeError, ValueError, RuntimeError):
                pass
        self._resume_jobs = None
        self._resume_profile = ''
        if self._close_when_worker_stops:
            self.set_busy_state(False)
            QTimer.singleShot(0, self.close)
            return
        self.set_busy_state(False)
        if not entries or not self._start_queue(entries, profile):
            self.set_workflow('error')
            self.status.setText(AUX_TEXT[self.lang]['scan_failed'])
            return

    def _fail_resume_recoverable_jobs(self, message):
        worker = getattr(self, '_resume_scan_worker', None)
        if worker is not None and worker.isRunning():
            worker.wait()
        self._resume_scan_worker = None
        if hasattr(self,'splash'):
            self.splash._allow_close=True; self.splash.close()
        # Keep the checkpoint recoverable and surface the network error. The
        # user can retry from the normal resume prompt after connectivity is
        # restored; no queue is started with an unknown progress denominator.
        self._resume_jobs = None
        self._resume_profile = ''
        self.set_busy_state(False)
        if self._close_when_worker_stops:
            QTimer.singleShot(0, self.close)
            return
        self.set_workflow('error')
        self.status.setText(AUX_TEXT[self.lang]['scan_failed'])
        self.log.append(str(message))

    def handle_queue_action(self, item_id, action):
        scheduler=self.queue_scheduler
        if scheduler is None:
            return
        action=str(action)
        actual=scheduler.task(item_id)
        if actual is not None and self._queue_job_ids.get(str(item_id)):
            actual.metadata['job_id']=self._queue_job_ids[str(item_id)]
        if action in ('resume','retry'):
            self.set_busy_state(True); self.set_workflow('download')
        if action=='pause':
            scheduler.pause(item_id)
        elif action=='resume':
            scheduler.resume(item_id)
        elif action=='retry':
            scheduler.retry(item_id)
        elif action=='cancel':
            scheduler.cancel(item_id)
        self._queue_view()

    def toggle_queue_pause_all(self):
        scheduler=self.queue_scheduler
        if scheduler is None: return
        if scheduler.has_paused_tasks:
            self.set_busy_state(True); self.set_workflow('download')
            scheduler.resume_all()
        else:
            scheduler.pause_all()
        self._queue_view()

    def retry_all_failed(self):
        if self.queue_scheduler is None: return
        if any(t.state=='failed' for t in self.queue_scheduler.tasks):
            self.set_busy_state(True); self.set_workflow('download')
            self.queue_scheduler.retry_all_failed()
        self._queue_view()

    def show_failed_queue(self):
        from queue_failure_ui import FailedQueueDialog
        dialog=FailedQueueDialog(self)
        try: dialog.exec()
        finally: dialog.deleteLater()

    def handle_queue_reorder(self, order):
        if self.queue_scheduler is not None:
            self.queue_scheduler.set_order(order)
            self._queue_view()

    def handle_queue_selection(self, item_ids):
        self._queue_selected_ids=[str(item_id) for item_id in item_ids or []]

    def handle_queue_selection_action(self, action):
        scheduler=self.queue_scheduler
        selected=list(self._queue_selected_ids)
        if scheduler is None or not selected:
            return
        action=str(action)
        if action=='top':
            scheduler.move_to_top(selected)
        elif action=='cancel':
            scheduler.cancel_many(selected)
        elif action=='pause':
            for item_id in selected:
                scheduler.pause(item_id)
        self._queue_view()

    def prepare_queue_exit(self):
        scheduler=self.queue_scheduler
        if scheduler is None:
            return
        self._queue_closing=True
        for task in scheduler.tasks:
            if task.state in {'queued','running','pausing'}:
                scheduler.pause(task.id)
        if (not scheduler.active_tasks and not getattr(self,'_queue_finalizing',False)
                and getattr(self,'_queue_preparation',None) is None
                and not getattr(self,'_queue_pending_entries',[])):
            self.set_busy_state(False)

    def refresh_recycle_bin(self):
        if self.recycle_bin is None:
            return
        try:
            self.recycle_bin.cleanup()
        except (OSError,ValueError,RuntimeError):
            return

    def _recycle_task_from_entry(self, entry):
        metadata=dict(entry.metadata or {})
        task_meta=metadata.get('task') if isinstance(metadata.get('task'),dict) else {}
        formats=metadata.get('formats') or task_meta.get('formats') or []
        return {
            'title':entry.title,
            'url':entry.post_url or entry.download_url,
            'image':entry.cover_path or '',
            'formats':formats,
            'metadata':{'recycled_from':entry.id,'profile_url':entry.profile_url,
                        'by_title':metadata.get('by_title',self.title_check.isChecked())},
        }

    def restore_recycle_entry(self, identifier):
        if self.recycle_bin is None:
            return False
        try:
            entry=self.recycle_bin.get(identifier)
        except (OSError,ValueError,RuntimeError):
            return False
        if entry is None or self.busy:
            return False
        # Keep the cover alive in the application cache after removing the
        # private recycle directory.
        cover=''
        if entry.cover_path and Path(entry.cover_path).is_file():
            try:
                destination=self.cache_dir/f'recycle-{entry.id}{Path(entry.cover_path).suffix.lower()}'
                shutil.copy2(entry.cover_path,destination); cover=str(destination)
            except OSError:
                cover=''
        task=self._recycle_task_from_entry(entry)
        task['image']=cover
        profile=entry.profile_url or self.profile.text().strip()
        if not self._start_queue([task],profile):
            return False
        try:
            self.recycle_bin.take(entry.id)
        except (OSError,ValueError,RuntimeError):
            pass
        self.refresh_recycle_bin()
        return True

    def remove_recycle_entry(self, identifier):
        if self.recycle_bin is None:
            return False
        try:
            return self.recycle_bin.remove(identifier) is not None
        except (OSError,ValueError,RuntimeError):
            return False

    def show_recycle_bin(self):
        if self.recycle_bin is None:
            return
        self.refresh_recycle_bin()
        try:
            entries=self.recycle_bin.list_entries()
        except (OSError,ValueError,RuntimeError):
            entries=[]
        dialog=QDialog(self)
        dialog.setWindowTitle({'简体中文':'下载回收站','繁體中文':'下載回收站','English':'Download recycle bin','日本語':'ダウンロードごみ箱'}.get(self.lang,'Download recycle bin'))
        dialog.setMinimumSize(560,360)
        layout=QVBoxLayout(dialog)
        list_widget=QListWidget(dialog)
        list_widget.setObjectName('recycleList')
        layout.addWidget(list_widget,1)
        if not entries:
            item=QListWidgetItem({'简体中文':'回收站目前为空','繁體中文':'回收站目前為空','English':'The recycle bin is empty','日本語':'ごみ箱は空です'}.get(self.lang,'The recycle bin is empty'))
            list_widget.addItem(item)
        for entry in entries:
            row=QListWidgetItem(f'{entry.title}\n{entry.download_url}')
            row.setData(Qt.ItemDataRole.UserRole,entry.id)
            if entry.cover_path and Path(entry.cover_path).is_file():
                cover=QPixmap(entry.cover_path)
                if not cover.isNull():
                    row.setIcon(QIcon(cover.scaled(64,64,Qt.AspectRatioMode.KeepAspectRatio,
                                                  Qt.TransformationMode.SmoothTransformation)))
            list_widget.addItem(row)
        buttons=QHBoxLayout()
        restore=QPushButton({'简体中文':'恢复下载','繁體中文':'恢復下載','English':'Restore download','日本語':'ダウンロードを復元'}.get(self.lang,'Restore download'))
        remove=QPushButton({'简体中文':'删除选中项','繁體中文':'刪除選取項目','English':'Delete selected','日本語':'選択項目を削除'}.get(self.lang,'Delete selected'))
        close=QPushButton({'简体中文':'关闭','繁體中文':'關閉','English':'Close','日本語':'閉じる'}.get(self.lang,'Close'))
        buttons.addWidget(restore); buttons.addWidget(remove); buttons.addStretch(1); buttons.addWidget(close)
        layout.addLayout(buttons)
        def selected_id():
            current=list_widget.currentItem()
            return current.data(Qt.ItemDataRole.UserRole) if current else None
        restore.clicked.connect(lambda: (self.restore_recycle_entry(selected_id()) if selected_id() else False, dialog.accept()))
        remove.clicked.connect(lambda: (self.remove_recycle_entry(selected_id()) if selected_id() else False, dialog.accept()))
        close.clicked.connect(dialog.reject)
        dialog.exec()
        dialog.deleteLater()
        self.refresh_recycle_bin()
    @staticmethod
    def _set_card_selection(index,state,selected,widget,colors):
        if state:
            selected.add(index); color=random.choice(colors); widget.setStyleSheet(f'QFrame#coverCard{{background:rgba(255,255,255,8);border:3px solid {color};border-radius:12px}}')
        else:
            selected.discard(index); widget.setStyleSheet('QFrame#coverCard{background:transparent;border:3px solid transparent;border-radius:12px}')
    def download(self):
        if self.busy: return
        if not self.path.text(): self.pick()
        if not self.path.text():return
        profile=self.profile.text().strip()
        if not PROFILE_URL_RE.fullmatch(profile):
            self.status.setText(AUX_TEXT[self.lang]['profile_invalid']); return
        selected=None
        if self.select_posts_check.isChecked():
            self.reset_dashboard(); self.set_busy_state(True); self.set_workflow('scan'); self.status.setText(AUX_TEXT[self.lang]['scan_status'])
            self.splash=ScanAnimation(self); self.splash.set_language(self.lang); self.splash.show()
            self.scanner=ProfileScanWorker(profile,self.cache_dir,after_ts=self._scan_after_timestamp); self._scan_after_timestamp=None; self.splash.scanner=self.scanner
            for button in (self.splash.pause_button,self.splash.resume_button,self.splash.finish_button): button.clicked.connect(self.reflect_scan_state)
            self.scanner.counts.connect(self.on_scan_counts); self.scanner.counts.connect(self.splash.update_counts); self.scanner.result.connect(self.scan_finished); self.scanner.failed.connect(self.scan_failed); self.scanner.start(); return
        self.launch_worker(profile,selected)
    def scan_finished(self,titles):
        if hasattr(self,'scanner') and self.scanner.isRunning(): self.scanner.wait()
        if self._close_when_worker_stops:
            self.splash._allow_close=True
            self.splash.close()
            self.busy=False
            QTimer.singleShot(0,self.close)
            return
        if not titles:
            self.splash._allow_close=True
            self.splash.close()
            self.scan_failed(AUX_TEXT[self.lang]['empty_scan']); return
        def continue_to_picker():
            self.set_workflow('choose')
            selected=self.choose_titles(titles)
            if self._close_when_worker_stops:
                self.busy=False
                QTimer.singleShot(0,self.close)
                return
            if selected is None or not selected:
                self.set_busy_state(False); self.set_workflow('ready'); self.status.setText(TEXT[self.lang]['ready']); return
            selected_entries=[dict(entry) for entry in titles if str(entry.get('url','')) in selected]
            self.set_busy_state(False)
            if not self._start_queue(selected_entries,self.profile.text().strip()):
                self.set_busy_state(False); self.set_workflow('ready'); self.status.setText(TEXT[self.lang]['ready'])
        splash=getattr(self,'splash',None)
        if splash is not None and hasattr(splash,'show_completion'):
            splash.show_completion(len(titles),continue_to_picker)
        else:
            continue_to_picker()
    def scan_failed(self,message):
        if hasattr(self,'scanner') and self.scanner.isRunning(): self.scanner.wait()
        if hasattr(self,'splash'): self.splash._allow_close=True; self.splash.close()
        self.set_busy_state(False); self.set_workflow('error'); self.status.setText(AUX_TEXT[self.lang]['scan_failed']); self.log.append(str(message))
        if self._close_when_worker_stops: QTimer.singleShot(0,self.close)
    def launch_worker(self,profile,selected):
        self.reset_dashboard(len(selected) if selected else 0)
        self.set_busy_state(True); self.set_workflow('download'); self.progress.setValue(0); self.log.clear()
        self.status.setText(TEXT[self.lang]['working'])
        order='newest' if self.order_combo.currentIndex()==0 else 'oldest'
        extra={}
        if self.services:
            extra=dict(journal=self.services.journal,resume_job_id=self.services.resume_job_id,
                       layout_version=2)
        extra['content_mode']=self.content_combo.currentData()
        extra['record_artist']=bool(self._next_record_artist)
        self.worker=None
        try:
            self.worker=Worker(self.path.text(),profile,order,self.title_check.isChecked(),selected,
                self.rename_check.isChecked(),1,self.lang,**extra)
            self._next_record_artist=True
            self.files_saved=getattr(self.worker,'resume_saved_count',0)
            self.files_value.setText(str(self.files_saved))
            self.worker.progress.connect(self.update_progress); self.worker.finished_ok.connect(self.done)
            self.worker.file_progress.connect(self.update_file_progress)
            self.worker.failed.connect(self.error)
            if self.services:self.services.worker_created(self.worker)
            self.worker.start()
        except Exception as error:
            if self.services and self.worker and self.worker.job_id and not self.worker.isRunning():
                try:
                    job=self.services.journal.load(self.worker.job_id)
                    job.update(status='failed',error=str(error)[:1000])
                    self.services.journal.save(job,dirty_records=[])
                except Exception:pass  # Report the original startup failure even if its journal is unavailable.
            self.error(str(error))
    def update_progress(self,name,i,total):
        failure_prefixes=tuple(text['download_fail_prefix'] for text in AUX_TEXT.values())
        if not name.startswith(failure_prefixes): self.update_transfer(name,i,total)
        if name.startswith('✓'):
            if self._file_total:
                self.progress.setValue(max(0,min(100,int(round(self._file_current * 100 / self._file_total)))))
            else:
                self.progress.setValue(100)
            self.log.append(name); return
        if name.startswith(failure_prefixes):
            self.log.append(name); return
        if total:
            if self._file_total:
                fraction=self._file_current + min(1.0,max(0.0,float(i)/float(total)))
                self.progress.setValue(max(0,min(100,int(round(fraction * 100 / self._file_total)))))
            else:
                self.progress.setValue(int(i*100/total))
        else:
            self.progress.setIndeterminate()
        if not self.progress.indeterminate:
            value = self.progress.value() if callable(getattr(self.progress, 'value', None)) else self.progress.value
            self.taskbar_progress.set_value(value)
        match=re.search(r'\((\d+/\d+)\)',name); count=f' {match.group(0)}' if match else ''
        message='file_progress' if total else 'unknown_progress'
        self.status.setToolTip(name)
        self.status.setText(self.ui_text()['received' if total else 'unknown_size'].format(current=i/1048576,total=total/1048576))

    def update_file_progress(self,current,total):
        try:
            self._file_current=max(0,int(current or 0))
        except (TypeError,ValueError):
            self._file_current=0
        try:
            self._file_total=max(0,int(total or 0))
        except (TypeError,ValueError):
            self._file_total=0
        if self._file_total:
            value=max(0,min(100,int(round(self._file_current * 100 / self._file_total))))
            self.progress.setValue(value); self.percent_label.setText(str(value)); self.percent_unit.show()
            self.taskbar_progress.set_value(value)
    def done(self,n):
        worker=getattr(self,'worker',None)
        if worker and worker.isRunning(): worker.wait()
        self.set_busy_state(False); self.status.setText(f"{TEXT[self.lang]['done']} · {n}"); folder=Path(self.path.text()) if self.path.text().strip() else None; self.completed_path=str(folder) if n>0 and folder and folder.is_dir() else None; self.open_folder_button.setVisible(bool(self.completed_path))
        self.set_workflow('complete'); self.stop_requested=False; self.files_saved=n; self.files_value.setText(str(n)); self.speed_value.setText('—')
        self.progress.setValue(100 if n else 0); self.percent_label.setText('100' if n else '0'); self.percent_unit.show()
        self.taskbar_progress.set_value(100 if n else 0)
        self.file_name.set_full_text(self.ui_text()['settled'])
        if self.services:
            try:
                self.services.store.record_download(n)
                job=worker.journal.load(worker.job_id) if worker and worker.journal and worker.job_id else {}
                if n and job.get('config',{}).get('record_artist',True):
                    self._maybe_artist_card(self.profile.text().strip(),posts=len(job.get('done_posts',[])) or 1,files=n,
                        latest_download_ts=self._latest_download_timestamp(job.get('files',[])))
            except Exception:
                # A transient local-state failure must not turn a completed
                # download into a visible task error; rewards can catch up on
                # the next successful session.
                pass
            self.refresh_reward_badge()
            self.services.task_finished(True)
        if self._close_when_worker_stops: QTimer.singleShot(0,self.close)
    def error(self,e):
        worker=getattr(self,'worker',None)
        if worker and worker.isRunning(): worker.wait()
        self.set_busy_state(False); self.completed_path=None; self.open_folder_button.hide(); self.status.setText(TEXT[self.lang]['error']); self.log.append(TEXT[self.lang]['empty'] if e=='EMPTY' else AUX_TEXT[self.lang]['no_selected'] if e=='NO_SELECTED_POSTS' else e)
        self.set_workflow('stopped' if self.stop_requested else 'error'); self.speed_value.setText('—')
        self.taskbar_progress.clear()
        if self.stop_requested: self.status.setText(AUX_TEXT[self.lang]['cancelled'])
        if self.services:
            paused=bool(getattr(getattr(self,'worker',None),'preserve_on_interrupt',False))
            self.services.task_finished(False,e,paused=paused)
            if paused:
                from feature_text import tr
                self.status.setText(tr(self.lang,'saved_progress'))
        if self._close_when_worker_stops: QTimer.singleShot(0,self.close)
    def closeEvent(self,event):
        if self.services and not self._close_when_worker_stops and not self.services.request_close():
            event.ignore(); return
        async_busy=any(w is not None and w.isRunning() for w in
                       (getattr(self,'_queue_preparation',None),getattr(self,'_queue_finalizer',None)))
        if self.busy or async_busy or getattr(self,'_queue_pending_entries',[]):
            event.ignore(); self._close_when_worker_stops=True; self.status.setText(AUX_TEXT[self.lang]['close_wait'])
            if self.services:
                from feature_text import tr
                self.status.setText(tr(self.lang,'saving'))
            if self.queue_scheduler is not None:
                self.prepare_queue_exit()
            else:
                worker=getattr(self,'worker',None)
                if worker and worker.isRunning(): worker.requestInterruption()
            if hasattr(self,'scanner') and self.scanner.isRunning():
                self.scanner.requestInterruption()
                if hasattr(self,'splash'): self.splash._allow_close=True; self.splash.hide()
            resume_scan=getattr(self,'_resume_scan_worker',None)
            if resume_scan is not None and resume_scan.isRunning():
                resume_scan.requestInterruption()
            return
        self.update_controller.close()
        background=[worker for worker in self.findChildren(QThread) if worker.isRunning()]
        if background:
            for worker in background:
                worker.requestInterruption()
                stop=getattr(worker,'stop',None)
                if callable(stop): stop()
            self._close_when_worker_stops=True
            self._subscription_badge_timer.stop()
            self.status.setText(ui_text(self.lang,'正在等待后台请求结束并安全退出…'))
            self._background_exit_timer.start(100)
            event.ignore(); return
        QApplication.instance().removeEventFilter(self)
        self.taskbar_progress.clear()
        self.taskbar_progress.close()
        self.credit.animation_timer.stop(); self.fx.stop()
        if hasattr(self,'background_layer'):
            self.background_layer.clear_background()
        if self.background_store:
            self.background_store.release_except(None)
        if self.recycle_timer.isActive():
            self.recycle_timer.stop()
        if self.recycle_bin:
            self.recycle_bin.close()
        shutil.rmtree(self.cache_dir,ignore_errors=True); super().closeEvent(event)
        if self.services:self.services.finish_exit()
    def eventFilter(self,obj,event):
        event_type=event.type()
        if event_type not in self._INPUT_EVENTS and event_type not in self._DIALOG_EVENTS:
            return super().eventFilter(obj,event)
        if event_type in self._DIALOG_EVENTS and isinstance(obj,QDialog) and obj is not self:
            from window_chrome import owns_window
            if not owns_window(self,obj): return super().eventFilter(obj,event)
            native_dialog = isinstance(obj, (QFileDialog, QInputDialog, QMessageBox))
            from ui_i18n import language_for, localize_dialog
            localize_dialog(obj, language_for(obj))
            obj.setAttribute(Qt.WidgetAttribute.WA_NoMousePropagation, True)
            if not native_dialog and not obj.property('pawchiveChromeInstalling'):
                # Install before the native window is shown: changing flags
                # in Show used to hide the dialog and queue a stale show()
                # after it had already closed or left its modal exec loop.
                if event.type() == QEvent.Type.Polish or hasattr(obj,'_pawchive_window_chrome'):
                    install_window_chrome(obj, title=obj.windowTitle())
        if event_type not in self._INPUT_EVENTS:
            # Paint/destruction events do not need input hit testing. In
            # particular don't inspect QWidget state midway through teardown.
            return super().eventFilter(obj,event)
        # A parented dialog is an ancestor's child in Qt, but it is still a
        # different input surface. Never use isAncestorOf for main-window
        # effects, wide theme hit testing, or URL drops.
        # A few existing canvas/caption widgets keep an owner in .window;
        # call the Qt base method so that attribute cannot shadow window().
        main_surface = isinstance(obj, QWidget) and QWidget.window(obj) is self
        application = QApplication.instance()
        input_blocked = main_surface and (
            application.activeModalWidget() not in (None, self) or
            application.activePopupWidget() not in (None, self))
        if main_surface and event.type() in (
                QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseMove,
                QEvent.Type.Wheel):
            hit = application.widgetAt(event.globalPosition().toPoint())
            if hit is not None and QWidget.window(hit) is not self:
                input_blocked = True
        if input_blocked and event.type() in (
                QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease,
                QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseMove,
                QEvent.Type.Wheel, QEvent.Type.DragEnter, QEvent.Type.DragMove,
                QEvent.Type.Drop):
            return True
        main_input = main_surface and not input_blocked
        if main_input:
            if event.type() in (QEvent.Type.DragEnter,QEvent.Type.DragMove):
                if extract_link(self._mime_link_text(event.mimeData())) is not None:
                    event.acceptProposedAction()
                    return True
            elif event.type()==QEvent.Type.Drop:
                info=self.handle_link_text(self._mime_link_text(event.mimeData()))
                if info is not None:
                    event.acceptProposedAction()
                    return True
        if main_input and obj is getattr(self,'profile',None):
            if event.type()==QEvent.Type.KeyPress and event.key()==Qt.Key.Key_V and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.paste_profile()
                event.accept()
                return True
        if main_input and event.type()==event.Type.MouseMove:
            gp=event.globalPosition().toPoint(); local=self.mapFromGlobal(gp)
            dx=local.x()-self._last_particle.x(); dy=local.y()-self._last_particle.y()
            if self.rect().contains(local) and random.random()<.24 and abs(dx)+abs(dy)>8 and len(self.particles)<100:
                self.particles.append(Particle(local.x(),local.y())); self._last_particle=QPointF(local)
                if not self.fx.isActive(): self.fx.start(16)
        elif main_input and event.type()==event.Type.MouseButtonPress:
            gp=event.globalPosition().toPoint(); local=self.mapFromGlobal(gp)
            theme_origin=self.theme.mapToGlobal(self.theme.rect().topLeft())
            theme_rect=QRect(theme_origin,self.theme.size())
            if theme_rect.adjusted(-2,-4,2,4).contains(gp) and not theme_rect.contains(gp):
                self.theme.click()
                return True
            credit_local=self.credit.mapFromGlobal(gp)
            credit_character_hit=self.credit.character_at(credit_local.x(),credit_local.y()) is not None
            if self.rect().contains(local) and not credit_character_hit:
                for _ in range(min(9, max(0, 100-len(self.particles)))): self.particles.append(Particle(local.x(),local.y(),True))
                if self.particles and not self.fx.isActive(): self.fx.start(16)
        return super().eventFilter(obj,event)
    def tick(self):
        for p in self.particles:p.move()
        self.particles=[p for p in self.particles if p.life>0]
        self.overlay.advance()
        if self.particles or self.overlay.has_effects:
            self.overlay.sync(); self.overlay.raise_(); self.overlay.update()
        else:
            self.fx.stop()
    def resizeEvent(self,e):
        super().resizeEvent(e)
        if hasattr(self,'edition'): self.edition.setVisible(self.width()>=1100)
        if hasattr(self,'background_opacity'):
            compact=self.width()<1000
            self.background_opacity.setVisible(not compact)
            self.background_opacity_value.setVisible(not compact)
        if hasattr(self,'background_layer'):
            self.background_layer.setGeometry(self.rect()); self.background_layer.lower(); self.background_layer.update()
        if hasattr(self,'overlay'): self.overlay.sync(); self.overlay.raise_()
    def showEvent(self,e):
        super().showEvent(e)
        try:
            self.taskbar_progress.ensure()
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        if hasattr(self,'background_layer'):
            self.background_layer.setGeometry(self.rect()); self.background_layer.lower(); self.background_layer.update()
        if hasattr(self,'overlay'): self.overlay.sync(); self.overlay.raise_()

if __name__=='__main__':
    if '--self-test-output' in sys.argv:
        import argparse
        from smoke_test import run
        parser=argparse.ArgumentParser(description='Offline startup check; never downloads files.')
        parser.add_argument('--self-test-output',type=Path,required=True)
        args=parser.parse_args()
        sys.exit(run(App,args.self_test_output))
    from desktop_entry import run
    sys.exit(run(App))








