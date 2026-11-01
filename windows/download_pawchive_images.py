from pathlib import Path
from urllib.parse import urlparse, urljoin, unquote, parse_qs
from html import escape
import re
import time
import tempfile
import shutil
import sys, random, math
import requests
from bs4 import BeautifulSoup
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QPointF, QRect, QRectF, QEasingCurve, QPropertyAnimation, Property, QSettings, QUrl
from PySide6.QtGui import QColor, QPainter, QPen, QBrush, QRadialGradient, QFont, QPixmap, QIcon, QLinearGradient, QGradient, QFontMetricsF, QPainterPath, QDesktopServices
from PySide6.QtWidgets import QStyle, QStyleOptionButton
from PySide6.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QFileDialog, QComboBox, QTextEdit, QFrame,
    QAbstractButton, QCheckBox, QDialog, QDialogButtonBox,
    QScrollArea, QGridLayout, QSpinBox, QSizePolicy, QGraphicsDropShadowEffect)

BASE_URL = 'https://pawchive.pw'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36'
PAGE_SIZE = 50
PROFILE_URL_RE = re.compile(
    r'https://pawchive\.pw/(?:fanbox|patreon|subscribestar|discord|boosty)/user/[^/?#]+/?'
)
INVALID_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}


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
 '简体中文': {'profile_hint':'请输入画师主页链接。','profile_valid':'✓ 画师主页链接有效','folder_needed':'✓ 链接有效；请先选择图片保存文件夹','profile_invalid':'链接格式无效：请输入 pawchive.pw 下的完整画师主页链接。','note_titles':'默认：每个图集分别保存到以标题命名的文件夹中。','note_flat':'已关闭分文件夹；可选择按发布日期排序并为图片编号。','scan_status':'正在扫描画师主页标题…','scan_failed':'扫描失败','empty_scan':'没有扫描到完整图集标题。','close_wait':'正在停止并清理未完成的图集…','cache_cleared':'临时缓存已清理；下载文件未受影响。','cache_failed':'缓存清理失败：{}','log_copied':'日志已复制到剪贴板。','copy_tooltip':'复制日志','cache_tooltip':'清理临时缓存','choose_title':'选择要下载的图集','choose_summary':'扫描到 {count} 个图集。点击封面卡片选择。','select_all':'全选','select_none':'全不选','cover_missing':'封面暂不可用','no_direct':'无直链文件','back_top':'回到顶部','ok':'确定','cancel':'取消','scan_window':'正在寻找图集','scan_searching':'正在沿着画师主页寻找图集…','scan_paused':'扫描已暂停','scan_finishing':'正在完成当前图集…','scan_count':'已扫描 {pages} 页 · 找到 {posts} 个图集','unknown':'未知','cancelled':'用户已停止下载','bad_content':'响应内容与文件类型不匹配（Content-Type: {content_type}）','download_failed':'下载失败（已重试4次）：{title} / {filename} — {error}','album_failed':'下载中止：图集“{title}”的文件连续失败，已停止以避免留下不完整图集。','download_fail_prefix':'下载失败','no_selected':'没有找到所选图集。','pause':'暂停','resume':'继续','finish':'结束扫描','file_progress':'正在下载{count}：{name} · {current:.1f}/{total:.1f} MB','unknown_progress':'正在下载{count}：{name} · 已接收 {current:.1f} MB'},
 '繁體中文': {'profile_hint':'請輸入畫師首頁連結。','profile_valid':'✓ 畫師首頁連結有效','folder_needed':'✓ 連結有效；請先選擇圖片儲存資料夾','profile_invalid':'連結格式無效：請輸入 pawchive.pw 下的完整畫師首頁連結。','note_titles':'預設：每篇圖集分別存入以標題命名的資料夾。','note_flat':'已關閉分資料夾；可選擇按發布日期排序並為圖片編號。','scan_status':'正在掃描畫師首頁標題…','scan_failed':'掃描失敗','empty_scan':'沒有掃描到完整圖集標題。','close_wait':'正在停止並清理未完成的圖集…','cache_cleared':'暫存快取已清理；下載檔案未受影響。','cache_failed':'快取清理失敗：{}','log_copied':'日誌已複製到剪貼簿。','copy_tooltip':'複製日誌','cache_tooltip':'清理暫存快取','choose_title':'選擇要下載的圖集','choose_summary':'掃描到 {count} 個圖集。點擊封面卡片選擇。','select_all':'全選','select_none':'全不選','cover_missing':'封面暫不可用','no_direct':'無直連檔案','back_top':'回到頂端','ok':'確定','cancel':'取消','scan_window':'正在尋找圖集','scan_searching':'正在沿著畫師首頁尋找圖集…','scan_paused':'掃描已暫停','scan_finishing':'正在完成目前圖集…','scan_count':'已掃描 {pages} 頁 · 找到 {posts} 個圖集','unknown':'未知','cancelled':'使用者已停止下載','bad_content':'回應內容與檔案類型不符（Content-Type: {content_type}）','download_failed':'下載失敗（已重試4次）：{title} / {filename} — {error}','album_failed':'下載中止：圖集「{title}」的檔案連續失敗，已停止以避免留下不完整圖集。','download_fail_prefix':'下載失敗','no_selected':'找不到所選圖集。','pause':'暫停','resume':'繼續','finish':'結束掃描','file_progress':'正在下載{count}：{name} · {current:.1f}/{total:.1f} MB','unknown_progress':'正在下載{count}：{name} · 已接收 {current:.1f} MB'},
 'English': {'profile_hint':'Enter a creator profile URL.','profile_valid':'✓ Creator profile URL is valid','folder_needed':'✓ URL is valid; choose a save folder','profile_invalid':'Invalid URL. Enter a complete creator profile link from pawchive.pw.','note_titles':'Default: save each post in a folder named after its title.','note_flat':'Post folders are off; you can number files by publication date.','scan_status':'Scanning creator posts…','scan_failed':'Scan failed','empty_scan':'No complete posts were found.','close_wait':'Stopping and cleaning up the unfinished post…','cache_cleared':'Temporary cache cleared; downloaded files were not changed.','cache_failed':'Could not clear cache: {}','log_copied':'Log copied to the clipboard.','copy_tooltip':'Copy log','cache_tooltip':'Clear temporary cache','choose_title':'Choose posts to download','choose_summary':'Found {count} posts. Click cover cards to select.','select_all':'Select all','select_none':'Select none','cover_missing':'Cover unavailable','no_direct':'No direct-link files','back_top':'Back to top','ok':'OK','cancel':'Cancel','scan_window':'Finding posts','scan_searching':'Searching creator posts…','scan_paused':'Scan paused','scan_finishing':'Finishing the current post…','scan_count':'Scanned {pages} pages · found {posts} posts','unknown':'unknown','cancelled':'Download stopped by the user','bad_content':'The response does not match the file type (Content-Type: {content_type})','download_failed':'Download failed after 4 attempts: {title} / {filename} — {error}','album_failed':'Stopped because a file in post “{title}” failed repeatedly. The incomplete post was removed.','download_fail_prefix':'Download failed','no_selected':'The selected posts could not be found.','pause':'Pause','resume':'Resume','finish':'Finish scan','file_progress':'Downloading{count}: {name} · {current:.1f}/{total:.1f} MB','unknown_progress':'Downloading{count}: {name} · {current:.1f} MB received'},
 '日本語': {'profile_hint':'クリエイターページのURLを入力してください。','profile_valid':'✓ クリエイターページURLは有効です','folder_needed':'✓ URLは有効です。保存先を選択してください','profile_invalid':'URLが無効です。pawchive.pw の完全なクリエイターページURLを入力してください。','note_titles':'既定：投稿ごとにタイトル名のフォルダーへ保存します。','note_flat':'投稿別フォルダーは無効です。公開日順に番号を付けられます。','scan_status':'クリエイターの投稿をスキャン中…','scan_failed':'スキャンに失敗しました','empty_scan':'完全な投稿が見つかりませんでした。','close_wait':'停止して未完了の投稿を削除しています…','cache_cleared':'一時キャッシュを削除しました。ダウンロード済みファイルは変更されていません。','cache_failed':'キャッシュを削除できませんでした：{}','log_copied':'ログをクリップボードにコピーしました。','copy_tooltip':'ログをコピー','cache_tooltip':'一時キャッシュを削除','choose_title':'ダウンロードする投稿を選択','choose_summary':'{count} 件の投稿が見つかりました。カバーをクリックして選択します。','select_all':'すべて選択','select_none':'選択解除','cover_missing':'カバーを表示できません','no_direct':'直接リンクのファイルなし','back_top':'先頭へ戻る','ok':'決定','cancel':'キャンセル','scan_window':'投稿を検索中','scan_searching':'クリエイターの投稿を検索中…','scan_paused':'スキャンを一時停止しました','scan_finishing':'現在の投稿を完了しています…','scan_count':'{pages} ページを確認 · {posts} 件の投稿','unknown':'不明','cancelled':'ユーザーがダウンロードを停止しました','bad_content':'応答内容がファイル形式と一致しません（Content-Type: {content_type}）','download_failed':'4回再試行しましたが失敗しました：{title} / {filename} — {error}','album_failed':'投稿「{title}」のファイルが連続して失敗したため中止しました。未完了の投稿を削除しました。','download_fail_prefix':'ダウンロード失敗','no_selected':'選択した投稿が見つかりません。','pause':'一時停止','resume':'再開','finish':'スキャン終了','file_progress':'ダウンロード中{count}：{name} · {current:.1f}/{total:.1f} MB','unknown_progress':'ダウンロード中{count}：{name} · {current:.1f} MB 受信済み'}
}

class _LegacyWorker(QThread):
    progress = Signal(str, int, int)
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
            if self.selected_post_urls: posts=[p for p in posts if p[1] in self.selected_post_urls]
            if not posts: raise RuntimeError('NO_SELECTED_POSTS')
            posts.sort(key=lambda x:x[0],reverse=self.order=='newest')
            saved=0; total=len(posts); downloaded=[]; owners={}; duplicate_names=set()
            for post_index,(dt,post_url,title) in enumerate(posts,1):
                if self.isInterruptionRequested(): raise RuntimeError(self.text['cancelled'])
                r=s.get(post_url,timeout=30); r.raise_for_status(); soup=BeautifulSoup(r.text,'html.parser')
                title_el=soup.select_one('h1.post__title span')
                if title_el: title=title_el.get_text(' ',strip=True)
                safe=safe_path_component(title, limit=100)
                dest=self.folder/safe if self.by_title else self.folder
                self.created_album=self.by_title and not dest.exists(); self.current_album=dest if self.by_title else None
                dest.mkdir(parents=True,exist_ok=True)
                links=[]; link_urls=set()
                for a in soup.select('.post__files a.fileThumb[href], a.post__attachment-link[href]'):
                    u=a.get('href','').strip()
                    direct='post__attachment-link' in (a.get('class') or [])
                    if u.startswith(('https://file.pawchive.pw/','https://n2.pawchive.pw/')) and u not in link_urls:
                        name=a.get('download','').strip()
                        if not name:
                            name=(parse_qs(urlparse(u).query).get('f') or [''])[0]
                        if not name:
                            name=re.sub(r'^Download\s+','',a.get_text(' ',strip=True),flags=re.I)
                        link_urls.add(u); links.append((u,name,direct))
                for j,(url,name,direct) in enumerate(links,1):
                    name=safe_path_component(unquote(name or Path(urlparse(url).path).name), fallback=f'image_{j}.bin')
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
                    direct_file=ext in {'.zip','.rar','.7z','.tar','.gz','.psd','.clip','.mp4','.mov','.webm','.mkv','.pdf'}
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
                    saved+=1; downloaded.append((dt,target,post_url,safe,name)); owners[target.name]=post_url; current_temp=None; self.progress.emit(f'✓  ({post_index}/{total}) {safe} / {target.name}',1,1)
                self.current_album=None; self.created_album=False
            if self.rename_order:
                for index,(_,path,_,_,_) in enumerate(sorted(downloaded,key=lambda item:item[0]),self.start_number):
                    extension=''.join(path.suffixes)
                    renamed=path.with_name(f'{index}{extension}')
                    bump=1
                    while renamed.exists() and renamed!=path:
                        renamed=path.with_name(f'{index}_{bump}{extension}'); bump+=1
                    if renamed!=path: path.rename(renamed)
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
                 start_number=1,lang='简体中文',journal=None,resume_job_id=None):
        self.journal=journal
        self.preserve_on_interrupt=False
        self.resume_saved_count=0
        if journal is not None and resume_job_id:
            job=journal.load(resume_job_id); config=job['config']
            folder=config['folder']; profile=config['profile']; order=config.get('order','newest')
            by_title=config.get('by_title',True); rename_order=config.get('rename_order',False)
            start_number=config.get('start_number',1); selected_post_urls=config.get('selected_post_urls')
            lang=config.get('lang',lang)
            self.resume_saved_count=job.get('saved_count',0)
        super().__init__(folder,profile,order,by_title,selected_post_urls,rename_order,start_number,lang)
        self.folder=self.folder.resolve()
        self.job_id=None
        if journal is not None:
            self.job_id=resume_job_id or journal.create(dict(folder=str(self.folder),profile=self.profile,
                order=order,by_title=bool(by_title),selected_post_urls=sorted(self.selected_post_urls),
                rename_order=bool(rename_order),start_number=start_number,lang=lang))
    def run(self):
        if self.journal is None:
            return super().run()
        from resumable_worker import DownloadEngine
        helpers=dict(absolute_post_url=absolute_post_url,safe_path_component=safe_path_component,
                     unique_path=unique_path,valid_download=valid_download,PAGE_SIZE=PAGE_SIZE,USER_AGENT=USER_AGENT)
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

class ScanAnimation(QDialog):
    """A small animated orbit shown while profile pages and covers are scanned."""
    def __init__(self,parent):
        super().__init__(parent); self.setFixedSize(390,250); self.setModal(False); self._allow_close=False
        self.phase=0; self.pages=0; self.posts=0; self.paused=False; self.finish_requested=False; self.lang='简体中文'; self.scanner=None
        layout=QVBoxLayout(self); layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.art=QLabel(); self.art.setFixedSize(220,120); self.art.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(self.art,0,Qt.AlignmentFlag.AlignCenter)
        self.message=QLabel(); self.message.setAlignment(Qt.AlignmentFlag.AlignCenter); layout.addWidget(self.message)
        self.counter=QLabel(); self.counter.setAlignment(Qt.AlignmentFlag.AlignCenter); self.counter.setStyleSheet('color:#91a7be'); layout.addWidget(self.counter)
        actions=QHBoxLayout(); self.pause_button=QPushButton(); self.resume_button=QPushButton(); self.finish_button=QPushButton(); actions.addWidget(self.pause_button); actions.addWidget(self.resume_button); actions.addWidget(self.finish_button); layout.addLayout(actions)
        self.pause_button.clicked.connect(self.pause_scan); self.resume_button.clicked.connect(self.resume_scan); self.finish_button.clicked.connect(self.finish_scan); self.resume_button.setEnabled(False)
        self.timer=QTimer(self); self.timer.timeout.connect(self.animate); self.timer.start(35); self.animate()
    def pause_scan(self):
        self.paused=True
        if self.scanner: self.scanner.paused=True
        self.pause_button.setEnabled(False); self.resume_button.setEnabled(True); self.message.setText(AUX_TEXT[self.lang]['scan_paused'])
    def resume_scan(self):
        self.paused=False
        if self.scanner: self.scanner.paused=False
        self.pause_button.setEnabled(True); self.resume_button.setEnabled(False); self.message.setText(AUX_TEXT[self.lang]['scan_searching'])
    def finish_scan(self):
        self.finish_requested=True; self.paused=False
        if self.scanner: self.scanner.finish_requested=True; self.scanner.paused=False
        self.pause_button.setEnabled(False); self.resume_button.setEnabled(False); self.finish_button.setEnabled(False)
        self.message.setText(AUX_TEXT[self.lang]['scan_finishing'])
    def set_language(self,lang):
        self.lang=lang; t=AUX_TEXT[lang]
        self.setWindowTitle(t['scan_window']); self.pause_button.setText(t['pause']); self.resume_button.setText(t['resume']); self.finish_button.setText(t['finish'])
        message=t['scan_finishing'] if self.finish_requested else t['scan_paused'] if self.paused else t['scan_searching']
        self.message.setText(message); self.update_counts(self.pages,self.posts)
    def update_counts(self,pages,posts):
        self.pages=pages; self.posts=posts; self.counter.setText(AUX_TEXT[self.lang]['scan_count'].format(pages=pages,posts=posts))
    def animate(self):
        self.phase=(self.phase+5)%360; pix=QPixmap(220,120); pix.fill(Qt.GlobalColor.transparent); painter=QPainter(pix); painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(95,135,175,90),2)); painter.setBrush(Qt.BrushStyle.NoBrush); painter.drawEllipse(54,9,112,102)
        palette=['#ff647c','#ffae52','#f3db59','#53d695','#43c8df','#668bff','#bf70eb']
        for i,color in enumerate(palette):
            angle=math.radians(self.phase+i*360/len(palette)); x=110+53*math.cos(angle); y=60+47*math.sin(angle); radius=5 if i%2 else 7
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor(color)); painter.drawEllipse(QPointF(x,y),radius,radius)
        painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QColor('#a7bcff' if getattr(self.parent(),'_dark',True) else '#536fd1'))
        star=QPainterPath(); star.moveTo(110,43); star.quadTo(113,57,127,60); star.quadTo(113,63,110,77)
        star.quadTo(107,63,93,60); star.quadTo(107,57,110,43); painter.drawPath(star)
        painter.end(); self.art.setPixmap(pix)
    def closeEvent(self,event):
        if self.scanner and self.scanner.isRunning() and not self._allow_close:
            self.finish_scan()
            self.hide()
            event.ignore()
            return
        self.timer.stop()
        super().closeEvent(event)
    def hideEvent(self,event):
        self.timer.stop()
        super().hideEvent(event)

class ProfileScanWorker(QThread):
    counts=Signal(int,int); result=Signal(object); failed=Signal(str)
    def __init__(self,profile,cache_dir):
        super().__init__(); self.profile=profile.rstrip('/'); self.cache_dir=Path(cache_dir); self.paused=False; self.finish_requested=False
    def run(self):
        session=requests.Session()
        session.headers['User-Agent']=USER_AGENT
        try:
            titles=[]; offset=0; seen=set()
            while True:
                while self.paused and not self.finish_requested and not self.isInterruptionRequested(): time.sleep(.08)
                if self.finish_requested or self.isInterruptionRequested(): break
                url=f'{self.profile}?o={offset}' if offset else self.profile
                response=session.get(url,timeout=20); response.raise_for_status(); soup=BeautifulSoup(response.text,'html.parser'); cards=soup.select('article.post-card')
                if self.isInterruptionRequested(): break
                if not cards: break
                page_post_count=len(titles)
                self.counts.emit(offset//PAGE_SIZE+1,len(titles))
                for card in cards:
                    while self.paused and not self.finish_requested and not self.isInterruptionRequested(): time.sleep(.08)
                    if self.finish_requested or self.isInterruptionRequested(): break
                    link=card.select_one('a[href*="/post/"]'); title=card.select_one('.post-card__header'); img=card.select_one('img.post-card__image')
                    if not link or not title: continue
                    post_url=absolute_post_url(link.get('href'))
                    if not post_url: continue
                    if post_url in seen: continue
                    image_path=None
                    if img:
                        src=img.get('data-src') or img.get('src')
                        if src:
                            try:
                                ir=session.get(src,timeout=8); ir.raise_for_status(); suffix=Path(unquote(urlparse(src).path)).suffix.lower(); suffix=suffix if re.fullmatch(r'\.[a-z0-9]{1,8}',suffix) else '.img'; image_path=self.cache_dir/f'{len(titles):06}{suffix}'; image_path.write_bytes(ir.content)
                            except requests.RequestException: pass
                    if self.isInterruptionRequested(): break
                    formats=[]
                    try:
                        detail=session.get(post_url,timeout=12); detail.raise_for_status(); ds=BeautifulSoup(detail.text,'html.parser')
                        for attachment in ds.select('a.post__attachment-link[href]'):
                            ext=Path(urlparse(attachment['href']).path).suffix.lower()
                            if ext and ext not in formats: formats.append(ext)
                    except requests.RequestException: pass
                    if self.isInterruptionRequested(): break
                    seen.add(post_url); titles.append({'title':title.get_text(' ',strip=True),'url':post_url,'image':str(image_path) if image_path else None,'formats':formats}); self.counts.emit(offset//PAGE_SIZE+1,len(titles))
                    if self.finish_requested: break
                if self.finish_requested or len(cards)<PAGE_SIZE or len(titles)==page_post_count: break
                offset+=PAGE_SIZE
            self.result.emit(titles)
        except Exception as exc: self.failed.emit(str(exc))
        finally:
            session.close()

class App(QWidget):
    def __init__(self,settings=None,services=None):
        super().__init__()
        self.services=services
        from desktop_design import build_interface
        self.setObjectName('appRoot')
        self.lang=services.store.get_setting('language','简体中文') if services else '简体中文'
        if self.lang not in TEXT:self.lang='简体中文'
        self.reward_skin=services.store.get_setting('reward_skin','default') if services else 'default'
        self.particles=[]; self.setMouseTracking(True)
        self.setMinimumSize(900,740); self.resize(1180,941)
        if QApplication.platformName()!='offscreen':
            available=QApplication.primaryScreen().availableGeometry()
            self.setMinimumHeight(min(740,max(520,available.height()-56)))
            self.resize(max(900,min(1180,available.width()-48)),max(self.minimumHeight(),min(941,available.height()-56)))
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
        icon_path=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))/'1.ico'
        self.setWindowIcon(QIcon(str(icon_path)))
        self.cache_dir=Path(tempfile.mkdtemp(prefix='pawchive_cache_'))
        self.busy=False; self._close_when_worker_stops=False
        self.settings=settings if settings is not None else QSettings('PawchiveTools','PawchiveDownloader')
        self.completed_path=None; self.workflow_state='setup'; self.logs_hidden=False
        self._dark=True; self.files_saved=0; self._album_current=0; self._album_total=0
        self._transfer_sample=None; self._speed=0.; self.stop_requested=False
        self._scan_pages=0; self._scan_posts=0
        build_interface(self,ThemeSwitch=ThemeSwitch,SoftComboBox=SoftComboBox,
            GlowButton=GlowButton,GlowCheckBox=GlowCheckBox,RainbowProgress=RainbowProgress,
            RainbowCredit=RainbowCredit,languages=TEXT,icon_path=icon_path)
        self.set_lang(self.lang)
        self.apply_theme(True)
        self.fx=QTimer(self); self.fx.timeout.connect(self.tick)
        self.overlay=ParticleOverlay(self); self.overlay.show(); self.overlay.raise_()
        self.credit.fireworkRequested.connect(self.overlay.launch_firework)
        self._last_particle=QPointF(-1000,-1000)
        QApplication.instance().installEventFilter(self)
    def set_lang(self,l):
        self.lang=l
        if self.services:
            self.services.store.set_setting('language',l); self.services.update_language()
        if self.language.currentText()!=l:
            blocked=self.language.blockSignals(True); self.language.setCurrentText(l); self.language.blockSignals(blocked)
        t=TEXT[l]; self.setWindowTitle(t['title']); self.heading.setText('Pawchive'); self.browse.setText(t['browse']); self.start.setText(t['start'])
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
    def update_note(self,checked=None):
        self.note.setText(AUX_TEXT[self.lang]['note_titles' if self.title_check.isChecked() else 'note_flat'])
    def set_busy_state(self,busy):
        self.busy=busy
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
    def paste_profile(self):
        if self.busy: return
        value=QApplication.clipboard().text().strip()
        if value:
            self.profile.setText(value); self.profile.setFocus()
        else: self.status.setText(self.ui_text()['paste_empty'])
    def toggle_logs(self):
        self.logs_hidden=not self.logs_hidden
        self.logs_card.setVisible(not self.logs_hidden)
        self.focus_button.setToolTip(self.ui_text()['expand' if self.logs_hidden else 'focus'])
        self.focus_button.setAccessibleName(self.focus_button.toolTip())
        self.footer_hint.setText(self.ui_text()['expand' if self.logs_hidden else 'focus'])
        self.focus_button.setProperty('selected',self.logs_hidden)
        self.focus_button.style().unpolish(self.focus_button); self.focus_button.style().polish(self.focus_button)
    def stop_download(self):
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
        d=QFileDialog.getExistingDirectory(self,TEXT[self.lang]['choose'],self.path.text() or str(Path.home()))
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
            for item in self.cache_dir.iterdir():
                shutil.rmtree(item) if item.is_dir() else item.unlink(missing_ok=True)
            self.status.setText(AUX_TEXT[self.lang]['cache_cleared'])
        except OSError as exc: self.log.append(AUX_TEXT[self.lang]['cache_failed'].format(exc))
    def open_download_folder(self):
        folder=Path(self.completed_path) if self.completed_path else None
        if folder and folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        else:
            self.completed_path=None; self.open_folder_button.hide()
    def choose_titles(self,titles):
        from post_picker import choose_posts
        return choose_posts(self,titles,GlowCheckBox,AUX_TEXT[self.lang])
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
            self.scanner=ProfileScanWorker(profile,self.cache_dir); self.splash.scanner=self.scanner
            for button in (self.splash.pause_button,self.splash.resume_button,self.splash.finish_button): button.clicked.connect(self.reflect_scan_state)
            self.scanner.counts.connect(self.on_scan_counts); self.scanner.counts.connect(self.splash.update_counts); self.scanner.result.connect(self.scan_finished); self.scanner.failed.connect(self.scan_failed); self.scanner.start(); return
        self.launch_worker(profile,selected)
    def scan_finished(self,titles):
        if hasattr(self,'scanner') and self.scanner.isRunning(): self.scanner.wait()
        self.splash._allow_close=True
        self.splash.close()
        if self._close_when_worker_stops:
            self.busy=False
            QTimer.singleShot(0,self.close)
            return
        if not titles:
            self.scan_failed(AUX_TEXT[self.lang]['empty_scan']); return
        self.set_workflow('choose')
        selected=self.choose_titles(titles)
        if self._close_when_worker_stops:
            self.busy=False
            QTimer.singleShot(0,self.close)
            return
        if selected is None or not selected:
            self.set_busy_state(False); self.set_workflow('ready'); self.status.setText(TEXT[self.lang]['ready']); return
        self.launch_worker(self.profile.text().strip(),selected)
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
            extra=dict(journal=self.services.journal,resume_job_id=self.services.resume_job_id)
        self.worker=None
        try:
            self.worker=Worker(self.path.text(),profile,order,self.title_check.isChecked(),selected,
                not self.title_check.isChecked() and self.rename_check.isChecked(),self.start_number.value(),self.lang,**extra)
            self.files_saved=getattr(self.worker,'resume_saved_count',0)
            self.files_value.setText(str(self.files_saved))
            self.worker.progress.connect(self.update_progress); self.worker.finished_ok.connect(self.done)
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
            self.progress.setValue(100); self.log.append(name); return
        if name.startswith(failure_prefixes):
            self.log.append(name); return
        if total:
            self.progress.setValue(int(i*100/total))
        else:
            self.progress.setIndeterminate()
        match=re.search(r'\((\d+/\d+)\)',name); count=f' {match.group(0)}' if match else ''
        message='file_progress' if total else 'unknown_progress'
        self.status.setToolTip(name)
        self.status.setText(self.ui_text()['received' if total else 'unknown_size'].format(current=i/1048576,total=total/1048576))
    def done(self,n):
        worker=getattr(self,'worker',None)
        if worker and worker.isRunning(): worker.wait()
        self.set_busy_state(False); self.status.setText(f"{TEXT[self.lang]['done']} · {n}"); folder=Path(self.path.text()) if self.path.text().strip() else None; self.completed_path=str(folder) if n>0 and folder and folder.is_dir() else None; self.open_folder_button.setVisible(bool(self.completed_path))
        self.set_workflow('complete'); self.stop_requested=False; self.files_saved=n; self.files_value.setText(str(n)); self.speed_value.setText('—')
        self.progress.setValue(100 if n else 0); self.percent_label.setText('100' if n else '0'); self.percent_unit.show()
        self.file_name.set_full_text(self.ui_text()['settled'])
        if self.services:
            try:
                self.services.store.record_download(n)
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
        if self.busy:
            event.ignore(); self._close_when_worker_stops=True; self.status.setText(AUX_TEXT[self.lang]['close_wait'])
            if self.services:
                from feature_text import tr
                self.status.setText(tr(self.lang,'saving'))
            worker=getattr(self,'worker',None)
            if worker and worker.isRunning(): worker.requestInterruption()
            if hasattr(self,'scanner') and self.scanner.isRunning():
                self.scanner.requestInterruption()
                if hasattr(self,'splash'): self.splash._allow_close=True; self.splash.hide()
            return
        QApplication.instance().removeEventFilter(self)
        self.credit.animation_timer.stop(); self.fx.stop()
        shutil.rmtree(self.cache_dir,ignore_errors=True); super().closeEvent(event)
        if self.services:self.services.finish_exit()
    def eventFilter(self,obj,event):
        if event.type()==event.Type.MouseMove:
            gp=event.globalPosition().toPoint(); local=self.mapFromGlobal(gp)
            dx=local.x()-self._last_particle.x(); dy=local.y()-self._last_particle.y()
            if self.rect().contains(local) and random.random()<.24 and abs(dx)+abs(dy)>8 and len(self.particles)<100:
                self.particles.append(Particle(local.x(),local.y())); self._last_particle=QPointF(local)
                if not self.fx.isActive(): self.fx.start(16)
        elif event.type()==event.Type.MouseButtonPress:
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
        if hasattr(self,'overlay'): self.overlay.sync(); self.overlay.raise_()
    def showEvent(self,e):
        super().showEvent(e)
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








