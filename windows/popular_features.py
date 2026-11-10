"""Today's popular recommendations and lightweight post previews.

The module keeps network work off the Qt thread and deliberately treats the
popular page as an optional enhancement.  A malformed page or a timeout
produces a user-visible error instead of taking down the downloader.
"""
from __future__ import annotations

from datetime import date
import hashlib
import random
import re
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from PySide6.QtCore import QThread, Signal, Qt, QSize, QUrl
from PySide6.QtGui import QPixmap, QDesktopServices, QIcon
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QVBoxLayout,
    QFileDialog,
    QWidget,
)


BASE_URL = "https://pawchive.pw"
POPULAR_URL = BASE_URL + "/posts/popular?date={}&period=day"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) PawchiveDownloader/1.3"
USER_RE = re.compile(r"/user/(\d+)", re.I)
MAX_RECOMMENDATIONS = 10
MAX_HTML_BYTES = 8 * 1024 * 1024


def popular_url(day: date | str | None = None) -> str:
    value = day.isoformat() if hasattr(day, "isoformat") else str(day or date.today())
    return POPULAR_URL.format(value)


def _safe_url(value: str, *, host="pawchive.pw") -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    parsed = urlparse(urljoin(BASE_URL + "/", value))
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() != host:
        return ""
    return parsed._replace(scheme="https").geturl()


def _creator_id(*values: str) -> str:
    for value in values:
        match = USER_RE.search(str(value or ""))
        if match:
            return match.group(1)
    return ""


def parse_popular_html(html: str, *, limit=MAX_RECOMMENDATIONS, day=None):
    """Parse the first popular page and choose deterministic weighted cards.

    The parser accepts both the current ``article.post-card`` markup and the
    older generic anchors used by Pawchive.  A local ``Random`` instance,
    seeded by the date, keeps today's recommendation set stable.
    """
    if not isinstance(html, str) or len(html.encode("utf-8", "ignore")) > MAX_HTML_BYTES:
        raise ValueError("popular_page_too_large")
    soup = BeautifulSoup(html, "html.parser")
    nodes = soup.select("article.post-card")
    if not nodes:
        nodes = soup.select("a[href*='/post/']")
    items = []
    seen = set()
    for rank, node in enumerate(nodes, 1):
        anchor = node if node.name == "a" else node.select_one("a[href*='/post/']")
        if anchor is None:
            continue
        post_url = _safe_url(anchor.get("href", ""))
        if not post_url or post_url in seen:
            continue
        seen.add(post_url)
        title_node = (node.select_one(".post-card__header, h2, h3, [itemprop='name']")
                      if hasattr(node, "select_one") else None)
        title = title_node.get_text(" ", strip=True) if title_node else ""
        image = node.select_one("img") if hasattr(node, "select_one") else None
        image_url = _safe_url(image.get("src", ""), host=(urlparse(image.get("src", "")).hostname or "")) if image else ""
        # _safe_url intentionally only permits Pawchive.  Cover images may be
        # hosted on file.pawchive or a CDN, so preserve a strict HTTPS image URL
        # while rejecting data/javascript schemes.
        if image and image.get("src"):
            raw = urljoin(BASE_URL + "/", image.get("src"))
            parsed = urlparse(raw)
            if parsed.scheme == "https" and parsed.hostname and parsed.hostname.endswith("pawchive.pw"):
                image_url = raw
        artist = _creator_id(post_url)
        for link in (node.select("a[href*='/user/']") if hasattr(node, "select") else []):
            artist = artist or _creator_id(link.get("href", ""))
        items.append({"rank": rank, "url": post_url, "title": title or "Untitled post",
                      "image_url": image_url, "artist_id": artist})
    if not items:
        return []
    count = min(max(1, int(limit)), len(items))
    # Weighted sampling without replacement.  The rank-ordered weights decay
    # smoothly while still allowing lower ranked posts to appear.
    rng = random.Random((day.isoformat() if hasattr(day, "isoformat") else str(day or date.today())))
    pool = list(items)
    chosen = []
    while pool and len(chosen) < count:
        weights = [max(1.0, (len(items) + 1 - item["rank"]) ** 1.35) for item in pool]
        total = sum(weights)
        pick = rng.random() * total
        selected_index = 0
        for index, weight in enumerate(weights):
            pick -= weight
            if pick <= 0:
                selected_index = index
                break
        chosen.append(pool.pop(selected_index))
    return chosen


def extract_post_assets(html: str, base_url: str):
    """Return thumbnails, original image links, and other direct attachments."""
    soup = BeautifulSoup(html or "", "html.parser")
    images, attachments, seen = [], [], set()
    # Restrict the broad fallback to known attachment containers.  Crawling
    # every anchor would accidentally expose navigation/profile links as
    # downloadable files on pages with custom layouts.
    nodes = soup.select(".post__files a.fileThumb[href], a.post__attachment-link[href]")
    if not nodes:
        nodes = soup.select("a[href^='https://file.pawchive.pw/'], a[href^='https://n2.pawchive.pw/']")
    for node in nodes:
        href = str(node.get("href") or "").strip()
        if not href:
            continue
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        is_pawchive = bool(parsed.scheme == "https" and parsed.hostname and parsed.hostname.endswith("pawchive.pw"))
        is_declared_attachment = "post__attachment-link" in (node.get("class") or [])
        # Declared external attachment links are retained for the explicit
        # browser-open action; image downloads remain limited to Pawchive's
        # own file hosts.
        if not is_pawchive and not (is_declared_attachment and parsed.scheme == "https"):
            continue
        query_name = (parse_qs(parsed.query).get("f") or [""])[0]
        name = str(node.get("download") or "").strip() or query_name or Path(parsed.path).name or "attachment"
        img = node.select_one("img")
        thumb = str(img.get("src") or "").strip() if img else ""
        ext = Path(str(name)).suffix.lower() or Path(parsed.path).suffix.lower()
        if is_pawchive and ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".tif", ".tiff"}:
            item = {"url": url, "thumb_url": urljoin(base_url, thumb) if thumb else url, "name": name}
            if url not in seen:
                images.append(item); seen.add(url)
        elif url not in seen:
            attachments.append({"url": url, "name": name})
            seen.add(url)
    # Some versions expose image links as img-original URLs without an anchor.
    for img in soup.select("img[src]"):
        src = urljoin(base_url, str(img.get("src") or ""))
        if "/img-original/" in src and src not in seen:
            images.append({"url": src, "thumb_url": src, "name": Path(urlparse(src).path).name}); seen.add(src)
    return images, attachments


class PopularFetchWorker(QThread):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, day=None, parent=None):
        super().__init__(parent)
        self.day = day or date.today()

    def run(self):
        session = requests.Session(); session.headers["User-Agent"] = USER_AGENT
        try:
            response = session.get(popular_url(self.day), timeout=(8, 20), stream=True)
            response.raise_for_status()
            chunks, total = [], 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_HTML_BYTES:
                    raise ValueError("popular_page_too_large")
                chunks.append(chunk)
            encoding = response.encoding or "utf-8"
            html = b"".join(chunks).decode(encoding, "replace")
            self.finished.emit(parse_popular_html(html, day=self.day))
        except Exception as error:
            self.failed.emit(str(error)[:240])
        finally:
            session.close()


class PostAssetsWorker(QThread):
    finished = Signal(object, object, str)

    def __init__(self, post_url, parent=None):
        super().__init__(parent); self.post_url = _safe_url(post_url)

    def run(self):
        if not self.post_url:
            self.finished.emit([], [], "invalid_post_url"); return
        session = requests.Session(); session.headers["User-Agent"] = USER_AGENT
        try:
            response = session.get(self.post_url, timeout=(8, 20)); response.raise_for_status()
            images, attachments = extract_post_assets(response.text, self.post_url)
            self.finished.emit(images, attachments, "")
        except Exception as error:
            self.finished.emit([], [], str(error)[:240])
        finally:
            session.close()


class ImageBytesWorker(QThread):
    finished = Signal(str, object, str)

    def __init__(self, url, parent=None):
        super().__init__(parent); self.url = str(url or "")

    def run(self):
        try:
            parsed = urlparse(self.url)
            if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith("pawchive.pw"):
                raise ValueError("invalid_image_url")
            response = requests.get(self.url, headers={"User-Agent": USER_AGENT}, timeout=(8, 30))
            response.raise_for_status()
            # Covers and on-demand previews are bounded so a page with ten
            # very large originals cannot consume hundreds of megabytes.
            if len(response.content) > 8 * 1024 * 1024:
                raise ValueError("image_too_large")
            self.finished.emit(self.url, bytes(response.content), "")
        except Exception as error:
            self.finished.emit(self.url, b"", str(error)[:240])


class AttachmentDownloadWorker(QThread):
    progress = Signal(int, int, str)
    finished = Signal(int, int, str)

    def __init__(self, attachments, folder, parent=None):
        super().__init__(parent); self.attachments = list(attachments or []); self.folder = Path(folder)

    def run(self):
        ok = failed = 0
        try: self.folder.mkdir(parents=True, exist_ok=True)
        except OSError as error: self.finished.emit(0, len(self.attachments), str(error)); return
        for index, item in enumerate(self.attachments, 1):
            url = str(item.get('url') or '')
            try:
                parsed = urlparse(url)
                if parsed.scheme != 'https': raise ValueError('invalid_attachment_url')
                name = Path(str(item.get('name') or Path(parsed.path).name or f'attachment_{index}')).name
                if not name: name = f'attachment_{index}'
                target = self.folder / name; temporary = target.with_name(target.name + '.part')
                response = requests.get(url, headers={'User-Agent': USER_AGENT}, timeout=(8, 60), stream=True); response.raise_for_status()
                with temporary.open('wb') as stream:
                    for chunk in response.iter_content(128 * 1024):
                        if chunk: stream.write(chunk)
                temporary.replace(target); ok += 1
            except Exception:
                failed += 1
            self.progress.emit(index, len(self.attachments), name if 'name' in locals() else url)
        self.finished.emit(ok, failed, '')


class RecommendationCard(QPushButton):
    """Card with a small artist-id chip that slides in on hover."""
    def __init__(self, artist_id='', parent=None):
        super().__init__(parent); self.setMouseTracking(True)
        self.artist_tag=QLabel(f'画师 ID {artist_id or "Unknown"}', self)
        self.artist_tag.setStyleSheet('QLabel{background:rgba(20,24,36,190);color:#fff;padding:4px 7px;border-radius:7px;}')
        self.artist_tag.adjustSize(); self.artist_tag.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.artist_tag.move(self.width()-self.artist_tag.width()-7, max(7, self.height()-self.artist_tag.height()-7))

    def enterEvent(self, event):
        self.artist_tag.show(); self.artist_tag.raise_(); super().enterEvent(event)

    def leaveEvent(self, event):
        self.artist_tag.hide(); super().leaveEvent(event)


class PopularRecommendationsDialog(QDialog):
    """Scrollable daily recommendations with safe timeout handling."""
    def __init__(self, parent=None, *, language="简体中文", on_post=None, on_profile=None):
        super().__init__(parent); self.lang = language; self.on_post = on_post; self.on_profile = on_profile
        self.setWindowTitle("今日画师推荐" if language == "简体中文" else "Today's recommendations")
        self.resize(860, 640); self._worker = None
        layout = QVBoxLayout(self); layout.setContentsMargins(18, 18, 18, 18)
        self.status = QLabel("正在加载今日热搜…" if language == "简体中文" else "Loading today's popular posts…")
        self.status.setWordWrap(True); layout.addWidget(self.status)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.body = QWidget(); self.grid = QGridLayout(self.body); self.grid.setSpacing(12)
        self.scroll.setWidget(self.body); layout.addWidget(self.scroll, 1)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close); close.rejected.connect(self.reject); layout.addWidget(close)
        self.load()

    def load(self):
        self._worker = PopularFetchWorker(parent=self); self._worker.finished.connect(self._populate); self._worker.failed.connect(self._failed); self._worker.start()

    def _failed(self, error):
        self.status.setText("今日热搜数据流中断，请稍后再试" if self.lang == "简体中文" else f"Today's popular data is unavailable: {error}")

    def _populate(self, items):
        while self.grid.count():
            item = self.grid.takeAt(0); widget = item.widget()
            if widget: widget.deleteLater()
        if not items:
            self.status.setText("今日暂无可用推荐" if self.lang == "简体中文" else "No recommendations are available today."); return
        self.status.setText(f"今日推荐 · {len(items)} 个图集" if self.lang == "简体中文" else f"{len(items)} recommendations")
        for index, data in enumerate(items):
            card = RecommendationCard(data.get('artist_id', '')); card.setCursor(Qt.CursorShape.PointingHandCursor); card.setToolTip(f"{data['title']} · 画师 {data.get('artist_id') or 'Unknown'}")
            card.setMinimumSize(245, 180); card.setStyleSheet("QPushButton{text-align:left;padding:8px;border-radius:12px;}QPushButton:hover{border:1px solid #b89af4;}")
            box = QVBoxLayout(card); image = QLabel("加载封面…"); image.setAlignment(Qt.AlignmentFlag.AlignCenter); image.setMinimumHeight(110); image.setObjectName("recommendCover")
            box.addWidget(image); title = QLabel(str(data.get("title") or "Untitled")); title.setWordWrap(True); box.addWidget(title)
            artist = QLabel(f"ID {data.get('artist_id') or '—'}"); artist.setObjectName("muted"); box.addWidget(artist)
            card.clicked.connect(lambda checked=False, item=dict(data): self.open_post(item))
            self.grid.addWidget(card, index // 3, index % 3)
            if data.get("image_url"):
                worker = ImageBytesWorker(data["image_url"], self); worker.finished.connect(lambda url, payload, err, image=image: self._cover_ready(image, payload)); worker.finished.connect(worker.deleteLater); worker.start()

    @staticmethod
    def _cover_ready(label, payload):
        if payload:
            pix = QPixmap(); pix.loadFromData(payload); label.setPixmap(pix.scaled(220, 110, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            label.setText("封面暂不可用")

    def open_post(self, data):
        dialog = PostPreviewDialog(self, data, language=self.lang, on_post=self.on_post, on_profile=self.on_profile)
        dialog.exec(); dialog.deleteLater()


class PostPreviewDialog(QDialog):
    """Lazy thumbnail preview.  Files downloaded for full-size viewing are
    held under a private temporary directory and removed when the dialog closes."""
    def __init__(self, parent, data, *, language="简体中文", on_post=None, on_profile=None):
        super().__init__(parent); self.data = dict(data); self.lang = language; self.on_post = on_post; self.on_profile = on_profile
        self.setWindowTitle(str(data.get("title") or "图集预览")); self.resize(900, 680); self._temp = Path(tempfile.mkdtemp(prefix="pawchive_preview_")); self._worker = None; self._image_workers = []
        layout = QVBoxLayout(self); layout.addWidget(QLabel(str(data.get("title") or "图集预览")))
        self.status = QLabel("正在读取图集内容…"); layout.addWidget(self.status)
        self.list = QListWidget(); self.list.setIconSize(QSize(150, 120)); self.list.itemClicked.connect(self._download_full); layout.addWidget(self.list, 1)
        buttons = QHBoxLayout(); self.download = QPushButton("下载此图集"); self.creator = QPushButton("搜寻画师全部图集"); self.attachments = QPushButton("附件下载"); self.attachments.setVisible(False); close = QPushButton("关闭")
        buttons.addWidget(self.download); buttons.addWidget(self.creator); buttons.addWidget(self.attachments); buttons.addStretch(1); buttons.addWidget(close); layout.addLayout(buttons)
        self.download.clicked.connect(lambda: self._dispatch("post")); self.creator.clicked.connect(lambda: self._dispatch("profile")); self.attachments.clicked.connect(self._open_attachment); close.clicked.connect(self.accept)
        self._worker = PostAssetsWorker(data.get("url", ""), self); self._worker.finished.connect(self._assets_ready); self._worker.start()

    def _assets_ready(self, images, attachments, error):
        self._attachments = list(attachments or [])
        if error:
            self.status.setText("图集读取失败，请稍后重试"); return
        self.status.setText(f"共 {len(images)} 张图片" + (f" · {len(attachments)} 个附件" if attachments else ""))
        self.attachments.setVisible(bool(attachments))
        for index, image in enumerate(images):
            item = QListWidgetItem(image.get("name", f"image_{index+1}")); item.setData(Qt.ItemDataRole.UserRole, image); self.list.addItem(item)
            if image.get("thumb_url"):
                worker = ImageBytesWorker(image["thumb_url"], self); worker.finished.connect(lambda url, payload, err, item=item: self._thumb_ready(item, payload)); worker.finished.connect(worker.deleteLater); worker.start(); self._image_workers.append(worker)

    @staticmethod
    def _thumb_ready(item, payload):
        if payload:
            pix = QPixmap(); pix.loadFromData(payload); item.setIcon(QIcon(pix.scaled(150, 120, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))

    def _download_full(self, item):
        data = item.data(Qt.ItemDataRole.UserRole) or {}; url = data.get("url", "")
        worker = ImageBytesWorker(url, self); worker.finished.connect(lambda _, payload, err: self._full_ready(item, payload, err)); worker.finished.connect(worker.deleteLater); worker.start(); self._image_workers.append(worker)

    def _full_ready(self, item, payload, error):
        if not payload:
            QMessageBox.warning(self, "预览失败", "高清图片读取失败，请稍后重试"); return
        target = self._temp / hashlib.sha256(str(item.data(Qt.ItemDataRole.UserRole).get("url", "")).encode()).hexdigest()
        target.write_bytes(payload)
        pix = QPixmap(); pix.loadFromData(payload); item.setIcon(QIcon(pix.scaled(240, 180, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))); item.setToolTip(str(target))
        viewer=QDialog(self); viewer.setWindowTitle(item.text()); viewer.resize(900,700)
        body=QVBoxLayout(viewer); area=QScrollArea(); area.setWidgetResizable(True)
        image=QLabel(); image.setAlignment(Qt.AlignmentFlag.AlignCenter); image.setPixmap(pix); area.setWidget(image); body.addWidget(area)
        close=QPushButton('关闭'); close.clicked.connect(viewer.accept); body.addWidget(close)
        viewer.exec(); viewer.deleteLater()

    def _dispatch(self, kind):
        callback = self.on_post if kind == "post" else self.on_profile
        if callable(callback): callback(self.data); self.accept()

    def _open_attachment(self):
        folder=QFileDialog.getExistingDirectory(self,'选择附件保存位置','')
        if not folder:return
        downloadable=[item for item in self._attachments if urlparse(str(item.get('url') or '')).hostname in {'file.pawchive.pw','n2.pawchive.pw'}]
        unknown=[item for item in self._attachments if item not in downloadable]
        for item in unknown:
            QDesktopServices.openUrl(QUrl(item.get("url", "")))
        if not downloadable:
            self.status.setText('未知直链已交给浏览器打开'); return
        self.attachments.setEnabled(False); self.status.setText('正在下载附件…')
        worker=AttachmentDownloadWorker(downloadable,folder,self); self._attachment_worker=worker
        worker.progress.connect(lambda i,total,name:self.status.setText(f'正在下载附件 {i}/{total}：{name}'))
        worker.finished.connect(lambda ok,failed,error:self._attachment_done(ok,failed,error)); worker.finished.connect(worker.deleteLater); worker.start()

    def _attachment_done(self, ok, failed, error):
        self.attachments.setEnabled(True)
        self.status.setText(f'附件下载完成：成功 {ok}，失败 {failed}' if not error else f'附件下载失败：{error}')

    def _cleanup_temp(self):
        import shutil
        for worker in list(getattr(self, '_image_workers', [])) + ([getattr(self, '_attachment_worker', None)] if getattr(self, '_attachment_worker', None) else []):
            if worker is not None and worker.isRunning(): worker.requestInterruption(); worker.wait(500)
        shutil.rmtree(self._temp, ignore_errors=True)

    def done(self, result):
        self._cleanup_temp(); super().done(result)

    def closeEvent(self, event):
        self._cleanup_temp(); super().closeEvent(event)


__all__ = ["popular_url", "parse_popular_html", "extract_post_assets", "PopularFetchWorker", "PostAssetsWorker", "PopularRecommendationsDialog", "PostPreviewDialog"]
