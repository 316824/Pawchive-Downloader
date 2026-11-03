"""Desktop presentation: shared controls stay connected to the existing workers."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (QByteArray, QEvent, QMimeData, QPoint, QRect, QRectF, QSize,
    Qt, QSignalBlocker, Signal)
from PySide6.QtGui import (QColor, QDrag, QIcon, QKeySequence, QPainter, QPainterPath,
    QPixmap, QShortcut)
from PySide6.QtWidgets import (QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout,
    QLineEdit, QMenu, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
    QTextEdit, QVBoxLayout, QWidget, QBoxLayout, QApplication, QAbstractButton,
    QRubberBand)

from desktop_visuals import CardHeading, StageRail, icon
from control_effects import DARK_GLOW, LIGHT_GLOW


COPY = {
    '简体中文': dict(workspace='下载工作台',
        source='画师主页', destination='保存位置', options='下载方式', activity='任务概览',
        source_hint='请输入完整的画师主页链接', folder_hint='选择用于保存图片与附件的文件夹',
        paste='粘贴链接', log='运行日志', log_hint='任务记录会在这里出现', ready='准备就绪',
        setup='等待设置', scan='扫描中', choose='选择图集', download='下载中', complete='已完成',
        error='需要检查', stopped='已停止', stopping='正在停止', albums='当前图集', files='已保存文件',
        speed='实时速度', current='当前文件', waiting='等待开始', focus='收起日志 · Ctrl+L',
        expand='显示日志 · Ctrl+L', copy='复制日志', plan='本次计划',
        newest='新到旧', oldest='旧到新', folders='按图集整理', flat='平铺保存',
        selection='先预选', all_posts='下载全部', steps=['设置','预选','下载','完成'],
        received='{current:.1f} / {total:.1f} MB', unknown_size='已接收 {current:.1f} MB',
        paused='扫描已暂停', scanned='已扫描 {pages} 页 · 收集 {posts} 个图集',
        paste_empty='剪贴板中没有可用的链接。', stop='停止下载', safe_stop='正在停止并清理未完成的图集…',
        settled='本次下载已结束', copy_empty='暂无可复制的日志',
        link_choice_title='检测到帖子链接', link_choice_detail='要下载该画师的全部图集，还是只下载当前帖子？',
        link_choice_batch='扫描并选择图集', link_choice_single='只下载当前帖子', link_choice_cancel='取消',
        portrait_mode='切换竖屏模式', window_mode='切换窗口模式',
        queue='下载队列', queue_empty='暂无排队图集', queue_count='{count} 个图集', recycle='回收站', queue_top='移到最前', queue_cancel_selected='取消选中',
        queue_pause='暂停', queue_resume='继续', queue_cancel='取消', queue_retry='重试',
        queue_waiting='等待中', queue_running='下载中', queue_paused='已暂停',
        queue_failed='下载失败', queue_cancelled='已取消', queue_complete='已完成'),
    '繁體中文': dict(workspace='下載工作台',
        source='畫師首頁', destination='儲存位置', options='下載方式', activity='任務概覽',
        source_hint='請輸入完整的畫師首頁連結', folder_hint='選擇儲存圖片與附件的資料夾',
        paste='貼上連結', log='執行日誌', log_hint='任務紀錄將顯示於此', ready='準備就緒',
        setup='等待設定', scan='掃描中', choose='選擇圖集', download='下載中', complete='已完成',
        error='需要檢查', stopped='已停止', stopping='正在停止', albums='目前圖集', files='已儲存檔案',
        speed='即時速度', current='目前檔案', waiting='等待開始', focus='收起日誌 · Ctrl+L',
        expand='顯示日誌 · Ctrl+L', copy='複製日誌', plan='本次計畫',
        newest='新到舊', oldest='舊到新', folders='按圖集整理', flat='平鋪儲存',
        selection='先預選', all_posts='下載全部', steps=['設定','預選','下載','完成'],
        received='{current:.1f} / {total:.1f} MB', unknown_size='已接收 {current:.1f} MB',
        paused='掃描已暫停', scanned='已掃描 {pages} 頁 · 收集 {posts} 個圖集',
        paste_empty='剪貼簿中沒有可用的連結。', stop='停止下載', safe_stop='正在停止並清理未完成的圖集…',
        settled='本次下載已結束', copy_empty='暫無可複製的日誌',
        link_choice_title='偵測到貼文連結', link_choice_detail='要下載這位畫師的全部圖集，還是只下載目前貼文？',
        link_choice_batch='掃描並選擇圖集', link_choice_single='只下載目前貼文', link_choice_cancel='取消',
        portrait_mode='切換直向模式', window_mode='切換視窗模式',
        queue='下載佇列', queue_empty='目前沒有排隊圖集', queue_count='{count} 個圖集', recycle='回收站', queue_top='移到最前', queue_cancel_selected='取消選取',
        queue_pause='暫停', queue_resume='繼續', queue_cancel='取消', queue_retry='重試',
        queue_waiting='等待中', queue_running='下載中', queue_paused='已暫停',
        queue_failed='下載失敗', queue_cancelled='已取消', queue_complete='已完成'),
    'English': dict(workspace='Download workspace',
        source='Creator profile', destination='Save location', options='Download preferences', activity='Activity',
        source_hint='Enter a full creator profile URL', folder_hint='Choose a folder for images and attachments',
        paste='Paste link', log='Activity log', log_hint='Your task history will appear here', ready='Ready',
        setup='Set up a task', scan='Scanning', choose='Choose posts', download='Downloading', complete='Complete',
        error='Needs attention', stopped='Stopped', stopping='Stopping', albums='Current post', files='Files saved',
        speed='Transfer speed', current='CURRENT FILE', waiting='Waiting to begin', focus='Hide log · Ctrl+L',
        expand='Show log · Ctrl+L', copy='Copy log', plan='THIS SESSION',
        newest='Newest first', oldest='Oldest first', folders='Post folders', flat='Single folder',
        selection='Choose posts', all_posts='All posts', steps=['Set up','Select','Download','Done'],
        received='{current:.1f} / {total:.1f} MB', unknown_size='{current:.1f} MB received',
        paused='Scan paused', scanned='{pages} pages scanned · {posts} posts found',
        paste_empty='No usable link on the clipboard.', stop='Stop download', safe_stop='Stopping and cleaning up the unfinished post…',
        settled='This download has ended', copy_empty='There is no log to copy yet',
        link_choice_title='Post link detected', link_choice_detail='Download every post from this creator, or only this post?',
        link_choice_batch='Scan and choose posts', link_choice_single='Only this post', link_choice_cancel='Cancel',
        portrait_mode='Switch to portrait mode', window_mode='Switch to window mode',
        queue='Download queue', queue_empty='No queued collections', queue_count='{count} collections', recycle='Recycle bin', queue_top='Move to top', queue_cancel_selected='Cancel selected',
        queue_pause='Pause', queue_resume='Resume', queue_cancel='Cancel', queue_retry='Retry',
        queue_waiting='Waiting', queue_running='Downloading', queue_paused='Paused',
        queue_failed='Download failed', queue_cancelled='Cancelled', queue_complete='Complete'),
    '日本語': dict(workspace='ダウンロードワークスペース',
        source='クリエイターページ', destination='保存先', options='ダウンロード設定', activity='タスクの状況',
        source_hint='クリエイターページの完全なURLを入力', folder_hint='画像と添付ファイルの保存先を選択',
        paste='リンクを貼付', log='実行ログ', log_hint='タスクの履歴がここに表示されます', ready='準備完了',
        setup='設定待ち', scan='スキャン中', choose='投稿を選択', download='ダウンロード中', complete='完了',
        error='確認が必要', stopped='停止済み', stopping='停止中', albums='現在の投稿', files='保存済み',
        speed='転送速度', current='現在のファイル', waiting='開始待ち', focus='ログを隠す · Ctrl+L',
        expand='ログを表示 · Ctrl+L', copy='ログをコピー', plan='今回の設定',
        newest='新しい順', oldest='古い順', folders='投稿別フォルダー', flat='同じフォルダー',
        selection='投稿を選択', all_posts='すべての投稿', steps=['設定','選択','転送','完了'],
        received='{current:.1f} / {total:.1f} MB', unknown_size='{current:.1f} MB 受信済み',
        paused='スキャン一時停止', scanned='{pages} ページ確認 · {posts} 件の投稿',
        paste_empty='クリップボードに有効なリンクがありません。', stop='ダウンロード停止', safe_stop='停止して未完了の投稿を削除しています…',
        settled='今回のダウンロードは終了しました', copy_empty='コピーできるログがありません',
        link_choice_title='投稿リンクを検出しました', link_choice_detail='このクリエイターの全投稿を取得しますか、それともこの投稿だけにしますか？',
        link_choice_batch='スキャンして投稿を選択', link_choice_single='この投稿だけ', link_choice_cancel='キャンセル',
        portrait_mode='縦長表示に切り替え', window_mode='通常表示に切り替え',
        queue='ダウンロードキュー', queue_empty='待機中のコレクションはありません', queue_count='{count} 件', recycle='ごみ箱', queue_top='先頭へ移動', queue_cancel_selected='選択をキャンセル',
        queue_pause='一時停止', queue_resume='再開', queue_cancel='キャンセル', queue_retry='再試行',
        queue_waiting='待機中', queue_running='ダウンロード中', queue_paused='一時停止',
        queue_failed='ダウンロード失敗', queue_cancelled='キャンセル済み', queue_complete='完了'),
}


class ElidedLabel(QLabel):
    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self.full_text = text
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumHeight(28)

    def set_full_text(self, text):
        self.full_text = text
        self.setToolTip(text)
        self._refresh()

    def _refresh(self):
        self.setText(self.fontMetrics().elidedText(self.full_text, Qt.TextElideMode.ElideMiddle,
                                                 max(20, self.contentsRect().width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()


class RoundedLogo(QWidget):
    def __init__(self,path,parent=None):
        super().__init__(parent)
        self.pixmap=QIcon(str(path)).pixmap(128,128)
        self.setFixedSize(44,44)
        self.setAccessibleName('Pawchive')

    def paintEvent(self,event):
        painter=QPainter(self)
        try:
            painter.setRenderHints(QPainter.RenderHint.Antialiasing|QPainter.RenderHint.SmoothPixmapTransform)
            area=QRectF(self.rect()).adjusted(1,1,-1,-1)
            mask=QPainterPath(); mask.addRoundedRect(area,11,11)
            painter.setClipPath(mask)
            painter.drawPixmap(area,self.pixmap,QRectF(self.pixmap.rect()))
        finally: painter.end()


class LogEditor(QTextEdit):
    def sizeHint(self):
        hint=super().sizeHint()
        hint.setHeight(110)
        return hint

    def append(self,text):
        # Artist titles and filenames are untrusted text, never HTML markup.
        from PySide6.QtGui import QTextCursor
        scrollbar=self.verticalScrollBar(); follow=scrollbar.value()>=scrollbar.maximum()-4
        cursor=QTextCursor(self.document()); cursor.movePosition(QTextCursor.MoveOperation.End)
        if not self.document().isEmpty():cursor.insertBlock()
        cursor.insertText(str(text))
        if follow:scrollbar.setValue(scrollbar.maximum())


class QueueThumbnail(QLabel):
    """Small cover preview used by one download queue row."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.failed = False
        self.setFixedSize(56, 56)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setText('◇')
        self.setObjectName('queueThumbnail')
        self.setScaledContents(False)

    def set_source(self, source):
        pixmap = None
        if isinstance(source, QPixmap):
            pixmap = source
        elif source:
            try:
                candidate = QPixmap(str(source))
                if not candidate.isNull():
                    pixmap = candidate
            except (OSError, TypeError, ValueError):
                pixmap = None
        if pixmap is None or pixmap.isNull():
            self.clear()
            self.setText('◇')
        else:
            self.setText('')
            self.setPixmap(pixmap.scaled(
                self.size(), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation))
        self.update()

    def set_failed(self, failed):
        self.failed = bool(failed)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.failed:
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            center = self.rect().topRight() + QPoint(-9, 9)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor('#e86478'))
            painter.drawEllipse(center, 10, 10)
            painter.setPen(QColor('#ffffff'))
            painter.drawText(self.rect().adjusted(self.width() - 20, 0, 0, -self.height() + 20),
                             Qt.AlignmentFlag.AlignCenter, '!')
        finally:
            painter.end()


class QueueItemWidget(QFrame):
    """One reorderable download entry driven by a loose item mapping."""

    actionRequested = Signal(str, str)
    selectionChanged = Signal(str, bool)
    TERMINAL = frozenset({'completed', 'complete', 'cancelled', 'canceled'})

    def __init__(self, item=None, progress_class=None, parent=None):
        super().__init__(parent)
        self.setObjectName('queueItem')
        self.setMinimumHeight(92)
        self.setMouseTracking(True)
        self._press_pos = None
        self._selected = False
        self.item = {}
        self.item_id = ''
        self.progress_class = progress_class
        self.language = '简体中文'
        self._status_key = 'queue_waiting'

        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(9)
        self.thumbnail = QueueThumbnail(self)
        outer.addWidget(self.thumbnail, 0, Qt.AlignmentFlag.AlignTop)

        middle = QVBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(5)
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        self.title = QLabel('')
        self.title.setObjectName('queueTitle')
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(False)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        title_row.addWidget(self.title, 1)
        self.status = QLabel('')
        self.status.setObjectName('queueStatus')
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        title_row.addWidget(self.status, 0)
        middle.addLayout(title_row)
        progress_row = QHBoxLayout()
        progress_row.setSpacing(6)
        if progress_class is not None:
            self.progress = progress_class()
            self.progress.setFixedHeight(13)
            self.progress.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            progress_row.addWidget(self.progress, 1)
        else:
            self.progress = None
        self.percent = QLabel('0%')
        self.percent.setObjectName('queuePercent')
        self.percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.percent.setMinimumWidth(40)
        progress_row.addWidget(self.percent, 0)
        middle.addLayout(progress_row)
        self.detail = QLabel('')
        self.detail.setObjectName('queueDetail')
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        middle.addWidget(self.detail)
        outer.addLayout(middle, 1)

        controls = QVBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(3)
        self.pause_button = self._button('Ⅱ', 'pause')
        self.cancel_button = self._button('×', 'cancel')
        self.retry_button = self._button('↻', 'retry')
        controls.addWidget(self.pause_button)
        controls.addWidget(self.cancel_button)
        controls.addWidget(self.retry_button)
        outer.addLayout(controls)
        self.set_item(item or {})

    def _button(self, text, action):
        button = QPushButton(text)
        button.setObjectName('queueAction')
        button.setFixedSize(25, 25)
        button.setToolTip(action)
        button.setAccessibleName(action)
        button.clicked.connect(lambda: self.actionRequested.emit(self.item_id, action))
        return button

    @staticmethod
    def _item_value(item, *names, default=None):
        for name in names:
            if isinstance(item, dict) and name in item:
                return item[name]
            if hasattr(item, name):
                return getattr(item, name)
        return default

    @staticmethod
    def _percent_value(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 0
        if 0 <= value <= 1:
            value *= 100
        return max(0, min(100, int(round(value))))

    def set_item(self, item):
        self.item = dict(item) if isinstance(item, dict) else item
        self.item_id = str(self._item_value(item, 'id', 'key', 'post_url', 'url', default=''))
        if not self.item_id:
            self.item_id = str(id(item))
        title = self._item_value(item, 'title', 'name', 'label', default='') or 'Untitled collection'
        self.title.setText(str(title))
        self.title.setToolTip(str(title))
        thumbnail = self._item_value(item, 'thumbnail', 'cover', 'image', 'thumb', default=None)
        self.thumbnail.set_source(thumbnail)
        progress = self._percent_value(self._item_value(item, 'progress', 'percent', 'value', default=0))
        if self.progress is not None:
            self.progress.setValue(progress)
        self.percent.setText(f'{progress}%')
        status = str(self._item_value(item, 'status', 'state', default='queued') or 'queued').lower()
        if status == 'running':
            self._status_key = 'queue_running'
        elif status in {'paused', 'pause', 'pausing'}:
            self._status_key = 'queue_paused'
            status = 'paused'
        elif status in {'cancelling', 'canceling'}:
            self._status_key = 'queue_cancelled'
            status = 'cancelled'
        elif status in {'failed', 'error'}:
            self._status_key = 'queue_failed'
            status = 'failed'
        elif status in self.TERMINAL:
            self._status_key = 'queue_complete' if status in {'completed', 'complete'} else 'queue_cancelled'
        else:
            self._status_key = 'queue_waiting'
            status = 'queued'
        self.status.setText(COPY.get(self.language, COPY['简体中文']).get(self._status_key, self._status_key))
        detail = self._item_value(item, 'detail', 'error', 'message', default='') or ''
        self.detail.setText(str(detail))
        self.thumbnail.set_failed(status == 'failed')
        self.pause_button.setVisible(status in {'running', 'paused'})
        labels = COPY.get(self.language, COPY['简体中文'])
        self.pause_button.setToolTip(labels['queue_pause'] if status == 'running' else labels['queue_resume'])
        self.pause_button.setAccessibleName(self.pause_button.toolTip())
        self.pause_button.setText('Ⅱ' if status == 'running' else '▶')
        self.cancel_button.setVisible(status not in self.TERMINAL and status not in {'failed', 'paused'})
        self.retry_button.setVisible(status in {'failed'})
        self.setProperty('queueState', status)
        self._polish()

    def set_language(self, language):
        self.language = language if language in COPY else '简体中文'
        labels = COPY[self.language]
        self.status.setText(labels.get(self._status_key, self._status_key))
        self.pause_button.setToolTip(labels['queue_pause'] if self.pause_button.text() == 'Ⅱ' else labels['queue_resume'])
        self.cancel_button.setToolTip(labels['queue_cancel'])
        self.retry_button.setToolTip(labels['queue_retry'])
        self.pause_button.setAccessibleName(self.pause_button.toolTip())
        self.cancel_button.setAccessibleName(labels['queue_cancel'])
        self.retry_button.setAccessibleName(labels['queue_retry'])

    def set_progress_style(self, track, colors):
        if self.progress is None:
            return
        self.progress.track = track
        self.progress.colors = [QColor(color) if not isinstance(color, QColor) else color
                                for color in colors]
        self.progress.update()

    def set_selected(self, selected):
        selected = bool(selected)
        if selected == self._selected:
            return
        self._selected = selected
        self.setProperty('selected', selected)
        self._polish()

    def is_selected(self):
        return self._selected

    def _polish(self):
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
            modifiers = event.modifiers()
            self.selectionChanged.emit(self.item_id,
                                       bool(modifiers & Qt.KeyboardModifier.ControlModifier))
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if (self._press_pos is not None and event.buttons() & Qt.MouseButton.LeftButton
                and (event.position().toPoint() - self._press_pos).manhattanLength()
                >= QApplication.startDragDistance()):
            drag = QDrag(self)
            mime = QMimeData()
            mime.setData('application/x-pawchive-queue-id', QByteArray(self.item_id.encode('utf-8')))
            drag.setMimeData(mime)
            drag.exec(Qt.DropAction.MoveAction)
            self._press_pos = None
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._press_pos = None
        super().mouseReleaseEvent(event)


class DownloadQueueWidget(QFrame):
    """Scrollable, reorderable queue panel for batch downloads."""

    actionRequested = Signal(str, str)
    orderChanged = Signal(list)
    selectionChanged = Signal(list)

    def __init__(self, progress_class=None, parent=None):
        super().__init__(parent)
        self.setObjectName('downloadQueue')
        self.progress_class = progress_class
        self.language = '简体中文'
        self._rows = {}
        self._order = []
        self._selected = set()
        self._rubber_origin = None
        self._rubber = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.scroll = QScrollArea(self)
        self.scroll.setObjectName('queueScroll')
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setAcceptDrops(True)
        self.scroll.viewport().setAcceptDrops(True)
        self.scroll.viewport().installEventFilter(self)
        self.body = QWidget()
        self.body.setObjectName('queueBody')
        self.body.setAcceptDrops(True)
        self.body.installEventFilter(self)
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(7)
        self.body_layout.addStretch(1)
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll)
        self.setAcceptDrops(True)

    @property
    def item_ids(self):
        return list(self._order)

    def set_language(self, language):
        self.language = language if language in COPY else '简体中文'
        for row in self._rows.values():
            row.set_language(self.language)

    def set_progress_style(self, track, colors):
        for row in self._rows.values():
            row.set_progress_style(track, colors)

    def _insert_row(self, row, index=None):
        if index is None or index >= len(self._order):
            index = len(self._order)
        index = max(0, index)
        self._order.insert(index, row.item_id)
        self._rows[row.item_id] = row
        row.set_language(getattr(self, 'language', '简体中文'))
        self.body_layout.insertWidget(index, row)
        row.actionRequested.connect(self.actionRequested)
        row.selectionChanged.connect(self._row_selection_changed)

    def set_items(self, items):
        normalized = list(items or [])
        wanted = []
        for item in normalized:
            row_id = QueueItemWidget._item_value(item, 'id', 'key', 'post_url', 'url', default='')
            row_id = str(row_id or id(item))
            wanted.append((row_id, item))
        wanted_ids = {row_id for row_id, _ in wanted}
        for row_id in list(self._order):
            if row_id not in wanted_ids:
                self.remove_item(row_id, emit=False)
        for index, (row_id, item) in enumerate(wanted):
            row = self._rows.get(row_id)
            if row is None:
                row = QueueItemWidget(item, self.progress_class, self.body)
                self._insert_row(row, index)
            else:
                row.set_item(item)
                self._move_row_local(row_id, index)
        self._emit_selection()

    def upsert_item(self, item, index=None):
        row_id = str(QueueItemWidget._item_value(item, 'id', 'key', 'post_url', 'url', default='') or id(item))
        row = self._rows.get(row_id)
        if row is None:
            row = QueueItemWidget(item, self.progress_class, self.body)
            self._insert_row(row, len(self._order) if index is None else index)
        else:
            row.set_item(item)
            if index is not None:
                self._move_row_local(row_id, index)
        return row

    def update_item(self, item_id, **updates):
        row = self._rows.get(str(item_id))
        if row is None:
            return False
        data = dict(row.item) if isinstance(row.item, dict) else {}
        data.update(updates)
        row.set_item(data)
        return True

    def remove_item(self, item_id, *, emit=True):
        item_id = str(item_id)
        row = self._rows.pop(item_id, None)
        if row is None:
            return False
        if item_id in self._order:
            self._order.remove(item_id)
        self._selected.discard(item_id)
        row.deleteLater()
        if emit:
            self._emit_order()
            self._emit_selection()
        return True

    def clear_items(self):
        for item_id in list(self._order):
            self.remove_item(item_id, emit=False)
        self._order.clear()
        self._selected.clear()
        self._emit_selection()

    def _move_row_local(self, item_id, index):
        item_id = str(item_id)
        if item_id not in self._rows:
            return False
        old = self._order.index(item_id)
        index = max(0, min(int(index), len(self._order) - 1))
        if old == index:
            return False
        self._order.pop(old)
        self._order.insert(index, item_id)
        row = self._rows[item_id]
        self.body_layout.removeWidget(row)
        self.body_layout.insertWidget(index, row)
        return True

    def move_item(self, item_id, index, *, emit=True):
        moved = self._move_row_local(item_id, index)
        if moved and emit:
            self._emit_order()
        return moved

    def move_selected_to_top(self):
        if not self._selected:
            return False
        selected = [item_id for item_id in self._order if item_id in self._selected]
        remaining = [item_id for item_id in self._order if item_id not in self._selected]
        new_order = selected + remaining
        if new_order == self._order:
            return False
        for index, item_id in enumerate(new_order):
            self._move_row_local(item_id, index)
        self._emit_order()
        return True

    def _row_selection_changed(self, item_id, additive):
        item_id = str(item_id)
        if additive:
            if item_id in self._selected:
                self._selected.remove(item_id)
            else:
                self._selected.add(item_id)
        else:
            self._selected = {item_id}
        for key, row in self._rows.items():
            row.set_selected(key in self._selected)
        self._emit_selection()

    def _emit_selection(self):
        self.selectionChanged.emit([item_id for item_id in self._order if item_id in self._selected])

    def _emit_order(self):
        self.orderChanged.emit(list(self._order))

    def _rubber_local(self, watched, position):
        try:
            return self.mapFromGlobal(watched.mapToGlobal(position))
        except (AttributeError, RuntimeError):
            return QPoint(0, 0)

    def _begin_rubber(self, watched, position):
        self._rubber_origin=self._rubber_local(watched,position)
        if self._rubber is None:
            self._rubber=QRubberBand(QRubberBand.Shape.Rectangle,self)
        self._rubber.setGeometry(QRectF(self._rubber_origin,self._rubber_origin).toRect())
        self._rubber.show()

    def _update_rubber(self, watched, position):
        if self._rubber_origin is None:
            return
        current=self._rubber_local(watched,position)
        rect=QRectF(self._rubber_origin,current).toRect().normalized()
        self._rubber.setGeometry(rect)
        for item_id,row in self._rows.items():
            row_rect=QRect(row.mapTo(self,QPoint(0,0)),row.size())
            selected=rect.intersects(row_rect)
            if selected:
                self._selected.add(item_id)
            row.set_selected(item_id in self._selected)
        self._emit_selection()

    def _end_rubber(self):
        self._rubber_origin=None
        if self._rubber is not None:
            self._rubber.hide()

    def _drag_id(self, event):
        mime = event.mimeData() if hasattr(event, 'mimeData') else None
        if mime is None or not mime.hasFormat('application/x-pawchive-queue-id'):
            return None
        try:
            return bytes(mime.data('application/x-pawchive-queue-id')).decode('utf-8')
        except (UnicodeDecodeError, TypeError, ValueError):
            return None

    def _drop_index(self, position):
        local = self.body.mapFrom(self, position) if position is not None else QPoint(0, 0)
        for index, item_id in enumerate(self._order):
            row = self._rows.get(item_id)
            if row is None:
                continue
            if local.y() < row.geometry().center().y():
                return index
        return max(0, len(self._order) - 1)

    def _accept_drag(self, event):
        if self._drag_id(event):
            event.acceptProposedAction()
            return True
        return False

    def dragEnterEvent(self, event):
        if not self._accept_drag(event):
            event.ignore()

    def dragMoveEvent(self, event):
        if not self._accept_drag(event):
            event.ignore()

    def dropEvent(self, event):
        item_id = self._drag_id(event)
        if item_id in self._rows:
            index = self._drop_index(event.position().toPoint())
            self.move_item(item_id, index, emit=True)
            event.acceptProposedAction()
        else:
            event.ignore()

    def eventFilter(self, obj, event):
        if obj in {self.body, self.scroll.viewport()}:
            if event.type()==QEvent.Type.MouseButtonPress and event.button()==Qt.MouseButton.LeftButton:
                self._begin_rubber(obj,event.position().toPoint())
                return True
            if event.type()==QEvent.Type.MouseMove and event.buttons() & Qt.MouseButton.LeftButton:
                self._update_rubber(obj,event.position().toPoint())
                return True
            if event.type()==QEvent.Type.MouseButtonRelease and event.button()==Qt.MouseButton.LeftButton:
                self._update_rubber(obj,event.position().toPoint())
                self._end_rubber()
                return True
            if event.type() == QEvent.Type.DragEnter:
                return self._accept_drag(event)
            if event.type() == QEvent.Type.DragMove:
                return self._accept_drag(event)
            if event.type() == QEvent.Type.Drop:
                self.dropEvent(event)
                return True
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._rubber_origin = event.position().toPoint()
            if self._rubber is None:
                self._rubber = QRubberBand(QRubberBand.Shape.Rectangle, self)
            self._rubber.setGeometry(self._rubber_origin.x(), self._rubber_origin.y(), 1, 1)
            self._rubber.show()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._rubber_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            current = event.position().toPoint()
            rect = QRectF(self._rubber_origin, current).toRect().normalized()
            self._rubber.setGeometry(rect)
            for item_id, row in self._rows.items():
                row_rect=QRect(row.mapTo(self,QPoint(0,0)),row.size())
                selected = rect.intersects(row_rect)
                if selected:
                    self._selected.add(item_id)
                row.set_selected(item_id in self._selected)
            self._emit_selection()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._rubber_origin = None
            if self._rubber is not None:
                self._rubber.hide()
        super().mouseReleaseEvent(event)


def text_label(text='', name=''):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setObjectName(name)
    return widget


def build_interface(w, *, ThemeSwitch, SoftComboBox, GlowButton, GlowCheckBox,
                    RainbowProgress, RainbowCredit, languages, icon_path):
    # Keep the two primary panels in one box layout so they can be switched
    # between the wide and stacked presentations without rebuilding widgets.
    w.portrait_mode = bool(getattr(w, 'portrait_mode', False))
    root = QVBoxLayout(w)
    root.setContentsMargins(30, 24, 30, 18)
    root.setSpacing(20)
    header = QHBoxLayout()
    header.setSpacing(14)
    w.logo=RoundedLogo(icon_path)
    header.addWidget(w.logo)
    w.heading = text_label('Pawchive', 'brand')
    w.heading.setText('Pawchive')
    header.addWidget(w.heading,1)
    w.edition = text_label('DESKTOP  /  1.1', 'edition')
    header.addWidget(w.edition)
    w.reward_badge = text_label('', 'rewardBadge')
    w.reward_badge.setMinimumWidth(120)
    w.reward_badge.setMaximumWidth(180)
    w.reward_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
    w.reward_badge.setToolTip('')
    header.addWidget(w.reward_badge)
    w.tools_button=QPushButton()
    w.tools_button.setObjectName('toolButton'); w.tools_button.setFixedSize(40,40)
    w.tools_menu=QMenu(w)
    w.schedule_action=w.tools_menu.addAction('')
    w.calendar_action=w.tools_menu.addAction('')
    w.resume_action=w.tools_menu.addAction('')
    w.background_action=w.tools_menu.addAction('')
    w.background_settings_action=w.tools_menu.addAction('')
    w.schedule_action.triggered.connect(lambda: w.services.show_schedules() if w.services else None)
    w.calendar_action.triggered.connect(lambda: w.services.show_calendar() if w.services else None)
    w.resume_action.triggered.connect(lambda: w.services.offer_resume() if w.services else None)
    w.background_action.triggered.connect(lambda: w.services.keep_background() if w.services else None)
    w.background_settings_action.triggered.connect(lambda: w.show_background_settings() if w.services else None)
    w.tools_button.setMenu(w.tools_menu); w.tools_button.setEnabled(w.services is not None)
    header.addWidget(w.tools_button)
    w.focus_button = QPushButton()
    w.focus_button.setObjectName('toolButton')
    w.focus_button.setFixedSize(40, 40)
    w.focus_button.clicked.connect(w.toggle_logs)
    header.addWidget(w.focus_button)
    w.layout_button = QPushButton()
    w.layout_button.setObjectName('toolButton')
    w.layout_button.setFixedSize(40, 40)
    w.layout_button.clicked.connect(w.toggle_layout_mode)
    header.addWidget(w.layout_button)
    w.background_controls=QWidget()
    w.background_controls.setObjectName('backgroundControls')
    background_row=QHBoxLayout(w.background_controls)
    background_row.setContentsMargins(0,0,0,0)
    background_row.setSpacing(6)
    w.background_mode_switch=QCheckBox()
    w.background_mode_switch.setObjectName('backgroundModeSwitch')
    w.background_mode_switch.setFixedSize(36,24)
    w.background_mode_switch.setToolTip('切换自定义背景 / 签到主题')
    w.background_mode_switch.toggled.connect(w.toggle_background_source)
    background_row.addWidget(w.background_mode_switch)
    w.background_opacity=QSlider(Qt.Orientation.Horizontal)
    w.background_opacity.setObjectName('backgroundOpacity')
    w.background_opacity.setRange(15,85)
    w.background_opacity.setValue(56)
    w.background_opacity.setFixedWidth(82)
    w.background_opacity.setToolTip('背景遮罩深度')
    w.background_opacity.valueChanged.connect(w.set_background_opacity)
    background_row.addWidget(w.background_opacity)
    w.background_opacity_value=text_label('56%','small')
    w.background_opacity_value.setMinimumWidth(30)
    background_row.addWidget(w.background_opacity_value)
    w.background_controls.setVisible(False)
    header.addWidget(w.background_controls)
    w.theme = ThemeSwitch(w.apply_theme)
    header.addWidget(w.theme)
    w.clear_cache = QPushButton()
    w.clear_cache.setObjectName('toolButton')
    w.clear_cache.setFixedSize(40, 40)
    w.clear_cache.clicked.connect(w.clear_cache_files)
    header.addWidget(w.clear_cache)
    w.language = SoftComboBox()
    w.language.addItems(languages)
    w.language.setFixedWidth(142)
    w.language.setMinimumHeight(40)
    w.language.currentTextChanged.connect(w.set_lang)
    header.addWidget(w.language)
    root.addLayout(header)

    w.stage_rail = StageRail()
    root.addWidget(w.stage_rail)
    content = QBoxLayout(QBoxLayout.Direction.LeftToRight)
    content.setSpacing(20)
    w.content_layout = content
    w.main_scroll = QScrollArea()
    w.main_scroll.setObjectName('configurationScroll')
    w.main_scroll.setWidgetResizable(True)
    w.main_scroll.setFrameShape(QFrame.Shape.NoFrame)
    w.main_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    w.main_scroll.setMinimumWidth(460)
    w.main_card = QFrame()
    w.main_card.setObjectName('mainCard')
    box = QVBoxLayout(w.main_card)
    box.setContentsMargins(26, 26, 26, 24)
    box.setSpacing(12)
    w.workspace_heading = CardHeading('', index='01')
    box.addWidget(w.workspace_heading)

    w.profile_label = text_label('', 'sectionLabel')
    w.profile_label.setWordWrap(True)
    box.addWidget(w.profile_label)
    profile_row = QHBoxLayout()
    profile_row.setSpacing(8)
    w.profile = QLineEdit()
    w.profile.setMinimumHeight(54)
    w.profile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    w.profile.textChanged.connect(w.validate_profile)
    profile_row.addWidget(w.profile, 1)
    w.paste_button = GlowButton()
    w.paste_button.setFixedSize(46, 54)
    w.paste_button.clicked.connect(w.paste_profile)
    profile_row.addWidget(w.paste_button)
    box.addLayout(profile_row)
    w.profile_hint = text_label('', 'muted')
    w.profile_hint.setWordWrap(True)
    box.addWidget(w.profile_hint)

    separator = QFrame()
    separator.setObjectName('separator')
    separator.setFixedHeight(1)
    box.addWidget(separator)
    folder_row = QHBoxLayout()
    w.folder_label = text_label('', 'sectionLabel')
    w.folder_label.setWordWrap(True)
    folder_row.addWidget(w.folder_label, 1)
    w.browse = GlowButton()
    w.browse.setMinimumHeight(38)
    w.browse.clicked.connect(w.pick)
    folder_row.addWidget(w.browse)
    box.addLayout(folder_row)
    w.path = QLineEdit()
    w.path.setMinimumHeight(52)
    w.path.setReadOnly(True)
    w.path.setText(w.settings.value('default_save_path', '', type=str))
    box.addWidget(w.path)
    w.default_path_check = GlowCheckBox()
    w.default_path_check.toggled.connect(w.save_default_path_setting)
    box.addWidget(w.default_path_check)

    w.options_panel = QFrame()
    w.options_panel.setObjectName('optionsPanel')
    options = QVBoxLayout(w.options_panel)
    options.setContentsMargins(0, 5, 0, 0)
    options.setSpacing(14)
    ordering = QHBoxLayout()
    w.order_label = text_label('', 'optionLabel')
    ordering.addWidget(w.order_label)
    ordering.addStretch(1)
    w.order_combo = SoftComboBox()
    w.order_combo.addItems(['', ''])
    w.order_combo.setMinimumWidth(176)
    ordering.addWidget(w.order_combo)
    options.addLayout(ordering)
    w.title_check = GlowCheckBox()
    w.title_check.setChecked(True)
    w.select_posts_check = GlowCheckBox()
    w.select_posts_check.setChecked(True)
    options.addWidget(w.title_check)
    options.addWidget(w.select_posts_check)
    box.addWidget(w.options_panel)

    w.rename_container = QWidget()
    w.rename_row = QVBoxLayout(w.rename_container)
    w.rename_row.setContentsMargins(0, 0, 0, 0)
    w.rename_row.setSpacing(12)
    w.rename_check = GlowCheckBox()
    w.rename_check.setChecked(True)
    w.rename_row.addWidget(w.rename_check)
    number_row = QHBoxLayout()
    w.start_number_label = text_label('', 'muted')
    number_row.addWidget(w.start_number_label)
    w.start_number = QSpinBox()
    w.start_number.setRange(0, 999999999)
    w.start_number.setValue(1)
    w.start_number.setMinimumWidth(120)
    number_row.addWidget(w.start_number)
    number_row.addStretch(1)
    w.rename_row.addLayout(number_row)
    box.addWidget(w.rename_container)
    w.rename_container.hide()
    w.title_check.toggled.connect(lambda checked: w.rename_container.setVisible(not checked))
    w.title_check.toggled.connect(w.update_note)
    w.title_check.toggled.connect(w.update_plan)
    w.select_posts_check.toggled.connect(w.update_plan)
    w.order_combo.currentIndexChanged.connect(w.update_plan)
    box.addStretch(1)
    w.start = GlowButton()
    w.start.setObjectName('primaryButton')
    w.start.setMinimumHeight(56)
    w.start.clicked.connect(w.download)
    w.start.setEnabled(False)
    box.addWidget(w.start)
    w.note = text_label('', 'small')
    w.note.setWordWrap(True)
    box.addWidget(w.note)
    w.main_scroll.setWidget(w.main_card)
    content.addWidget(w.main_scroll, 3)

    w.activity_body=QWidget()
    sidebar = QVBoxLayout(w.activity_body)
    sidebar.setContentsMargins(0,0,0,0)
    sidebar.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
    sidebar.setSpacing(16)
    w.task_card = QFrame()
    w.task_card.setObjectName('taskCard')
    task = QVBoxLayout(w.task_card)
    task.setContentsMargins(24, 24, 24, 22)
    task.setSpacing(12)
    task_head = QHBoxLayout()
    w.activity_heading = text_label('', 'sectionLabel')
    task_head.addWidget(w.activity_heading, 1)
    w.state_badge = text_label('', 'stateBadge')
    w.state_badge.setMinimumHeight(24)
    task_head.addWidget(w.state_badge)
    task.addLayout(task_head)
    w.plan_label = text_label('', 'eyebrow')
    task.addWidget(w.plan_label)
    w.plan = text_label('', 'small')
    w.plan.setWordWrap(True)
    task.addWidget(w.plan)
    task.addStretch(1)
    progress_row = QHBoxLayout()
    w.percent_label = text_label('0', 'progressNumber')
    w.percent_label.setMinimumHeight(70)
    w.percent_unit = text_label('%', 'progressUnit')
    progress_row.addWidget(w.percent_label)
    progress_row.addWidget(w.percent_unit, 0, Qt.AlignmentFlag.AlignBottom)
    progress_row.addStretch(1)
    w.progress_caption=text_label('', 'small')
    progress_row.addWidget(w.progress_caption,0,Qt.AlignmentFlag.AlignBottom)
    task.addLayout(progress_row)
    w.progress = RainbowProgress()
    w.progress.setMinimumWidth(0)
    w.progress.setFixedHeight(24)
    task.addWidget(w.progress)
    metrics = QGridLayout()
    metrics.setHorizontalSpacing(18)
    metrics.setVerticalSpacing(8)
    w.album_label = text_label('', 'small')
    w.speed_label = text_label('', 'small')
    w.files_label = text_label('', 'small')
    w.album_value = text_label('— / —', 'metric')
    w.speed_value = text_label('—', 'metric')
    w.files_value = text_label('0', 'metric')
    for column, (caption, value) in enumerate(((w.album_label, w.album_value),
                    (w.speed_label, w.speed_value), (w.files_label, w.files_value))):
        metrics.addWidget(caption, 0, column)
        metrics.addWidget(value, 1, column)
        metrics.setColumnStretch(column, 1)
    task.addLayout(metrics)
    line = QFrame()
    line.setObjectName('separator')
    line.setFixedHeight(1)
    task.addWidget(line)
    w.current_label = text_label('', 'eyebrow')
    task.addWidget(w.current_label)
    w.file_name = ElidedLabel()
    w.file_name.setObjectName('fileName')
    task.addWidget(w.file_name)
    w.status_container = QWidget()
    state = QVBoxLayout(w.status_container)
    state.setContentsMargins(0, 0, 0, 0)
    state.setSpacing(12)
    w.status = text_label('', 'muted')
    w.status.setWordWrap(True)
    w.status.setMinimumHeight(28)
    w.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    state.addWidget(w.status)
    controls = QHBoxLayout()
    w.open_folder_button = GlowButton()
    w.open_folder_button.setMinimumHeight(36)
    w.open_folder_button.clicked.connect(w.open_download_folder)
    w.open_folder_button.hide()
    controls.addWidget(w.open_folder_button)
    w.stop_button = QPushButton()
    w.stop_button.setMinimumHeight(36)
    w.stop_button.setObjectName('stopButton')
    w.stop_button.clicked.connect(w.stop_download)
    w.stop_button.hide()
    controls.addWidget(w.stop_button)
    controls.addStretch(1)
    state.addLayout(controls)
    task.addWidget(w.status_container)
    task.addStretch(1)
    sidebar.addWidget(w.task_card, 3)

    # Per-collection queue.  The scheduler can feed DownloadTask snapshots via
    # ``set_items``/``upsert_item`` without making the existing aggregate card
    # or log panel depend on the scheduler implementation.
    w.queue_card = QFrame()
    w.queue_card.setObjectName('queueCard')
    queue_box = QVBoxLayout(w.queue_card)
    queue_box.setContentsMargins(18, 16, 18, 16)
    queue_box.setSpacing(9)
    queue_header = QHBoxLayout()
    w.queue_heading = text_label('', 'sectionLabel')
    queue_header.addWidget(w.queue_heading, 1)
    w.queue_count = text_label('0', 'queueCount')
    queue_header.addWidget(w.queue_count, 0, Qt.AlignmentFlag.AlignRight)
    w.recycle_button=QPushButton('♻')
    w.recycle_button.setObjectName('toolButton')
    w.recycle_button.setFixedSize(32,32)
    w.recycle_button.clicked.connect(lambda: getattr(w,'show_recycle_bin',lambda: None)())
    queue_header.addWidget(w.recycle_button)
    w.queue_top_button=QPushButton('↑')
    w.queue_top_button.setObjectName('toolButton')
    w.queue_top_button.setFixedSize(32,32)
    w.queue_top_button.clicked.connect(lambda: getattr(w,'handle_queue_selection_action',lambda *_: None)('top'))
    queue_header.addWidget(w.queue_top_button)
    w.queue_cancel_selected_button=QPushButton('×')
    w.queue_cancel_selected_button.setObjectName('toolButton')
    w.queue_cancel_selected_button.setFixedSize(32,32)
    w.queue_cancel_selected_button.clicked.connect(lambda: getattr(w,'handle_queue_selection_action',lambda *_: None)('cancel'))
    queue_header.addWidget(w.queue_cancel_selected_button)
    queue_box.addLayout(queue_header)
    w.download_queue = DownloadQueueWidget(RainbowProgress, w.queue_card)
    w.download_queue.setMinimumHeight(150)
    w.download_queue.actionRequested.connect(
        lambda item_id, action: getattr(w, 'handle_queue_action', lambda *_: None)(item_id, action))
    w.download_queue.orderChanged.connect(
        lambda order: getattr(w, 'handle_queue_reorder', lambda *_: None)(order))
    w.download_queue.selectionChanged.connect(
        lambda ids: getattr(w, 'handle_queue_selection', lambda *_: None)(ids))
    queue_box.addWidget(w.download_queue, 1)
    sidebar.addWidget(w.queue_card, 4)

    w.logs_card = QFrame()
    w.logs_card.setObjectName('logCard')
    logs = QVBoxLayout(w.logs_card)
    logs.setContentsMargins(20, 18, 20, 18)
    logs.setSpacing(12)
    log_header = QHBoxLayout()
    w.log_heading = text_label('', 'sectionLabel')
    log_header.addWidget(w.log_heading, 1)
    w.copy_log = QPushButton()
    w.copy_log.setObjectName('toolButton')
    w.copy_log.setFixedSize(34, 34)
    w.copy_log.clicked.connect(w.copy_log_text)
    log_header.addWidget(w.copy_log)
    logs.addLayout(log_header)
    w.log = LogEditor()
    w.log.setReadOnly(True)
    w.log.setMinimumHeight(110)
    w.log.textChanged.connect(lambda: w.copy_log.setEnabled(not w.log.document().isEmpty()))
    w.copy_log.setEnabled(False)
    logs.addWidget(w.log, 1)
    sidebar.addWidget(w.logs_card, 2)
    w.activity_scroll=QScrollArea()
    w.activity_scroll.setWidgetResizable(True)
    w.activity_scroll.setFrameShape(QFrame.Shape.NoFrame)
    w.activity_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    w.activity_scroll.setWidget(w.activity_body)
    content.addWidget(w.activity_scroll,2)
    root.addLayout(content, 1)

    footer = QHBoxLayout()
    w.footer_hint = text_label('', 'footerHint')
    footer.addWidget(w.footer_hint)
    footer.addStretch(1)
    w.credit = RainbowCredit('千泽音奈  ·  QQ：3412892533')
    w.credit.setMinimumWidth(260)
    w.credit.setMaximumWidth(340)
    footer.addWidget(w.credit)
    root.addLayout(footer)
    w.shortcuts = []
    for sequence, callback in [('Ctrl+L', w.toggle_logs), ('Ctrl+Shift+V', w.paste_profile)]:
        shortcut = QShortcut(QKeySequence(sequence), w)
        shortcut.activated.connect(callback)
        w.shortcuts.append(shortcut)
    for button in w.findChildren(QPushButton):
        button.setCursor(Qt.CursorShape.PointingHandCursor)


def set_layout_mode(w, portrait):
    """Switch the main content between wide and stacked presentations.

    The scroll areas remain the same widgets, so active downloads, logs and
    selections survive the transition. Only the box direction and minimum
    size hints change.
    """
    portrait = bool(portrait)
    w.portrait_mode = portrait
    layout = getattr(w, 'content_layout', None)
    if layout is not None:
        layout.setDirection(QBoxLayout.Direction.TopToBottom if portrait
                            else QBoxLayout.Direction.LeftToRight)
        layout.setSpacing(16 if portrait else 20)
        layout.setStretch(0, 3)
        layout.setStretch(1, 2)
    main_scroll = getattr(w, 'main_scroll', None)
    activity_scroll = getattr(w, 'activity_scroll', None)
    if main_scroll is not None and activity_scroll is not None:
        if portrait:
            main_scroll.setMinimumWidth(0)
            activity_scroll.setMinimumWidth(0)
            # Let the window size and the two scroll areas negotiate their
            # height.  Hard minimum heights here would make the *window*
            # minimum height exceed a short laptop screen; each panel already
            # has its own vertical scroll bar for compact portrait layouts.
            main_scroll.setMinimumHeight(0)
            activity_scroll.setMinimumHeight(0)
            main_scroll.setSizePolicy(QSizePolicy.Policy.Expanding,
                                      QSizePolicy.Policy.Expanding)
            activity_scroll.setSizePolicy(QSizePolicy.Policy.Expanding,
                                          QSizePolicy.Policy.Expanding)
        else:
            main_scroll.setMinimumWidth(460)
            activity_scroll.setMinimumWidth(0)
            main_scroll.setMinimumHeight(0)
            activity_scroll.setMinimumHeight(0)
            main_scroll.setSizePolicy(QSizePolicy.Policy.Expanding,
                                      QSizePolicy.Policy.Preferred)
            activity_scroll.setSizePolicy(QSizePolicy.Policy.Expanding,
                                          QSizePolicy.Policy.Preferred)
    update_layout_button(w)
    if hasattr(w, 'layout_button'):
        labels = COPY.get(getattr(w, 'lang', '简体中文'), COPY['简体中文'])
        key = 'window_mode' if portrait else 'portrait_mode'
        w.layout_button.setToolTip(labels[key])
        w.layout_button.setAccessibleName(labels[key])


def update_layout_button(w):
    button = getattr(w, 'layout_button', None)
    if button is None:
        return
    palette = getattr(w, 'palette_tokens', {})
    color = palette.get('muted', '#94a3bb')
    kind = 'desktop' if getattr(w, 'portrait_mode', False) else 'phone'
    button.setIcon(icon(kind, color))
    button.setIconSize(QSize(18, 18))


def apply_style(w, dark, skin='default'):
    p = dict(bg='#10141c', card='#171e29', panel='#141b25', text='#e9edf6',
             muted='#99a6bb', small='#748399', line='#2a3445', accent='#9cb3ff',
             field='#111822', hover='#222f43', button='#a7bcff', button_text='#14203c') if dark else dict(
             bg='#f4f5f2', card='#ffffff', panel='#fafbf8', text='#252e42',
             muted='#68758a', small='#8993a3', line='#dee4e9', accent='#516aca',
             field='#f7f9fb', hover='#edf1fc', button='#536fd1', button_text='#ffffff')
    # Reward skins only tune the accent palette; the light/dark switch still
    # controls contrast and remains available on every unlocked skin.
    skin_values = {
        'ocean': ({
            'bg':'#081923','card':'#0d2635','panel':'#0b202e','text':'#e9fbff',
            'muted':'#9fc8d6','small':'#6e9eae','line':'#1c4c60','accent':'#63d4ef',
            'field':'#0a1d2a','hover':'#143b4d','button':'#74ddf3','button_text':'#06202b',
            'glow':'#55ccea'}, {
            'bg':'#effcff','card':'#ffffff','panel':'#f5fdff','text':'#163544',
            'muted':'#527b89','small':'#7398a4','line':'#c8e6ee','accent':'#198eaf',
            'field':'#f2fbfd','hover':'#e0f5fa','button':'#168eae','button_text':'#ffffff',
            'glow':'#3caec7'}),
        'cyberpunk': ({
            'bg':'#120d1e','card':'#1b1129','panel':'#171025','text':'#f8efff',
            'muted':'#bda7d2','small':'#8c72a6','line':'#3e2c54','accent':'#ff68c7',
            'field':'#160e24','hover':'#322044','button':'#ff78d0','button_text':'#2b0d2a',
            'glow':'#ff5cb8'}, {
            'bg':'#fff5fd','card':'#ffffff','panel':'#fff8fd','text':'#321d3e',
            'muted':'#805b8c','small':'#9a78a4','line':'#ecd7ed','accent':'#d445a4',
            'field':'#fff7fd','hover':'#fce6f6','button':'#d445a4','button_text':'#ffffff',
            'glow':'#df69b7'}),
    }
    values = skin_values.get(str(skin))
    if values:
        p.update(values[0 if dark else 1])
    else:
        p['hover']='#2b243b' if dark else '#f1ecfa'
    p.setdefault('glow', DARK_GLOW if dark else LIGHT_GLOW)
    w.setStyleSheet(f'''
        QWidget {{background:transparent;color:{p['text']};font-family:"Segoe UI","Microsoft YaHei UI";font-size:14px;}}
        QWidget#appRoot,QDialog {{background:{p['bg']};}}
        QLabel {{background:transparent;border:0;padding:0;}}
        QLabel#brand {{font-family:Georgia,"Palatino Linotype";font-size:27px;font-weight:600;letter-spacing:.3px;}}
        QLabel#muted {{color:{p['muted']};font-size:12px;}}
        QLabel#small {{color:{p['muted']};font-size:12px;}}
        QLabel#eyebrow {{color:{p['small']};font-size:10px;font-weight:600;letter-spacing:1px;}}
        QLabel#edition,QLabel#footerHint {{color:{p['small']};font-size:10px;letter-spacing:1px;}}
        QLabel#rewardBadge {{color:{p['accent']};background:{p['hover']};border:1px solid {p['line']};border-radius:10px;padding:6px 10px;font-size:11px;font-weight:600;}}
        QLabel#sectionLabel {{font-size:14px;font-weight:600;}}
        QLabel#optionLabel {{color:{p['muted']};font-size:13px;}}
        QLabel#progressNumber {{font-size:56px;font-weight:600;letter-spacing:-2px;}}
        QLabel#progressUnit {{color:{p['muted']};font-size:19px;padding-bottom:12px;}}
        QLabel#metric {{font-size:17px;font-weight:600;}}
        QLabel#fileName {{font-size:13px;}}
        QLabel#stateBadge {{color:{p['accent']};background:{p['hover']};border-radius:9px;padding:5px 9px;font-size:11px;}}
        QFrame#mainCard,QFrame#taskCard,QFrame#logCard,QFrame#queueCard {{background:{p['card']};border:1px solid {p['line']};border-radius:19px;}}
        QFrame#logCard {{background:{p['panel']};}}
        QFrame#queueItem {{background:{p['field']};border:1px solid {p['line']};border-radius:12px;}}
        QFrame#queueItem[selected="true"] {{background:{p['hover']};border-color:{p['glow']};}}
        QLabel#queueThumbnail {{background:{p['hover']};color:{p['muted']};border:1px solid {p['line']};border-radius:9px;font-size:22px;}}
        QLabel#queueTitle {{font-size:13px;font-weight:600;}}
        QLabel#queueStatus {{color:{p['accent']};font-size:11px;}}
        QLabel#queuePercent {{color:{p['text']};font-size:12px;font-weight:600;}}
        QLabel#queueDetail {{color:{p['small']};font-size:10px;}}
        QLabel#queueCount {{color:{p['muted']};font-size:11px;}}
        QPushButton#queueAction {{padding:0;border-radius:7px;font-size:13px;color:{p['muted']};}}
        QPushButton#queueAction:hover {{color:{p['text']};border-color:{p['glow']};background:{p['hover']};}}
        QFrame#separator {{background:{p['line']};border:0;}}
        QScrollArea,QScrollArea>QWidget>QWidget {{background:transparent;border:0;}}
        QLineEdit {{background:{p['field']};border:1px solid {p['line']};border-radius:11px;padding:10px 13px;selection-background-color:{p['accent']};}}
        QLineEdit:focus {{border-color:{p['glow']};}}
        QPushButton {{border:1px solid {p['line']};border-radius:11px;background:transparent;padding:9px 13px;font-size:13px;}}
        QPushButton:hover {{background:{p['hover']};border-color:{p['glow']};}}
        QPushButton:pressed {{background:{p['field']};}}
        QPushButton:disabled {{color:{p['small']};border-color:{p['line']};}}
        QPushButton#primaryButton {{background:{p['button']};color:{p['button_text']};border:0;font-size:15px;font-weight:600;border-radius:13px;}}
        QPushButton#primaryButton:hover {{background:{'#bbcbff' if dark else '#637ddd'};}}
        QPushButton#primaryButton:disabled {{background:{p['hover']};color:{p['small']};}}
        QPushButton#toolButton {{padding:6px;border-color:transparent;border-radius:10px;}}
        QPushButton#toolButton:hover {{border-color:{p['glow']};background:{p['hover']};}}
        QPushButton#toolButton[selected="true"] {{border-color:{p['glow']};background:{p['hover']};}}
        QPushButton[animatedGlow="true"],QPushButton[animatedGlow="true"]:hover,QPushButton[animatedGlow="true"]:disabled {{border-color:transparent;}}
        QPushButton#stopButton {{color:{'#ffadb4' if dark else '#b44457'};}}
        QComboBox {{background:{p['field']};border:1px solid {p['line']};border-radius:10px;padding:8px 26px 8px 12px;min-height:22px;}}
        QComboBox:hover,QComboBox:focus {{border-color:{p['glow']};}}
        QComboBox QAbstractItemView {{background:{p['card']};border:1px solid {p['line']};selection-background-color:{p['hover']};selection-color:{p['text']};outline:0;padding:6px;}}
        QComboBox::drop-down {{background:transparent;border:0;width:26px;}}
        QComboBox::down-arrow {{image:none;width:0;height:0;border:0;}}
        QCheckBox {{spacing:11px;padding:5px 0;background:transparent;}}
        QCheckBox::indicator {{width:19px;height:19px;border:1px solid {p['small']};border-radius:5px;background:{p['field']};}}
        QCheckBox::indicator:hover {{border:1px solid {p['glow']};}}
        QCheckBox::indicator:checked {{background:{p['accent']};border:1px solid {p['accent']};}}
        QCheckBox:disabled {{color:{p['small']};}}
        QCheckBox#backgroundModeSwitch {{padding:0;background:transparent;}}
        QCheckBox#backgroundModeSwitch::indicator {{width:30px;height:16px;border:1px solid {p['line']};border-radius:8px;background:{p['field']};}}
        QCheckBox#backgroundModeSwitch::indicator:hover {{border-color:{p['glow']};}}
        QCheckBox#backgroundModeSwitch::indicator:checked {{background:{p['accent']};border:1px solid {p['glow']};}}
        QSlider#backgroundOpacity {{background:transparent;min-height:18px;}}
        QSlider#backgroundOpacity::groove:horizontal {{height:4px;background:{p['line']};border-radius:2px;}}
        QSlider#backgroundOpacity::handle:horizontal {{width:12px;height:12px;margin:-4px 0;border-radius:6px;background:{p['accent']};}}
        QSpinBox {{background:{p['field']};border:1px solid {p['line']};border-radius:8px;padding:6px 10px;}}
        QTextEdit {{background:transparent;border:0;padding:0;font-size:12px;selection-background-color:{p['hover']};}}
        QScrollBar:vertical {{background:transparent;width:9px;margin:4px 0;}}
        QScrollBar::handle:vertical {{background:{p['line']};border-radius:4px;min-height:40px;}}
        QScrollBar::handle:vertical:hover {{background:{p['small']};}}
        QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{height:0;}}
        QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {{background:transparent;}}
        QToolTip {{background:{p['card']};color:{p['text']};border:1px solid {p['line']};padding:7px;}}
        QMenu {{background:{p['card']};color:{p['text']};border:1px solid {p['line']};padding:6px;}}
        QMenu::item {{padding:10px 18px;border-radius:7px;}}
        QMenu::item:selected {{background:{p['hover']};}}
        QPushButton::menu-indicator {{image:none;width:0;height:0;}}
        QListWidget {{background:{p['field']};border:1px solid {p['line']};border-radius:10px;padding:7px;}}
        QListWidget::item {{padding:9px;border-radius:6px;}}
        QListWidget::item:selected {{background:{p['hover']};}}
        QDateTimeEdit {{background:{p['field']};border:1px solid {p['line']};border-radius:9px;padding:8px;}}
        QCalendarWidget QWidget#qt_calendar_navigationbar {{background:{p['card']};}}
        QCalendarWidget QToolButton {{background:transparent;color:{p['text']};border:0;padding:7px;}}
        QCalendarWidget QAbstractItemView {{background:{p['field']};color:{p['text']};selection-background-color:{p['glow']};selection-color:white;}}
    ''')
    w.stage_rail.set_theme(dark)
    w.workspace_heading.set_theme(dark)
    w.palette_tokens = p
    for button in w.findChildren(QPushButton):
        if button.property('animatedGlow'):
            button.setProperty('glowColor',p['glow'])
            button.setProperty('borderColor',p['line'])
            button.update()
    for combo in (w.language,w.order_combo):
        combo.arrow_color=p['muted']; combo.update()
    for button, kind in ((w.paste_button,'paste'), (w.clear_cache,'broom'),
                         (w.copy_log,'copy'), (w.focus_button,'eye'),
                         (w.browse,'folder'), (w.open_folder_button,'folder'), (w.start,'arrow'),(w.tools_button,'settings')):
        button.setIcon(icon(kind, p['muted'] if button is not w.start else p['button_text']))
        button.setIconSize(QSize(17, 17))
    update_layout_button(w)
    w.progress.track = p['line']
    w.progress.colors = ['#f07e99','#f5b864','#e5ce72','#72cba3','#67c9d8','#91a7ef','#c094dd'] if dark else [
        '#d05d80','#d9983d','#c4a736','#3d9e78','#369aad','#647bd0','#9a66bc']
    w.progress.colors = [QColor(c) for c in w.progress.colors]
    w.progress.update()
    if hasattr(w, 'download_queue'):
        w.download_queue.set_progress_style(w.progress.track, w.progress.colors)
        w.download_queue.set_language(w.lang)
    w.set_workflow(w.workflow_state)


def translate_interface(w, text, auxiliary):
    from feature_text import tr
    t = COPY[w.lang]
    w.setWindowTitle(text['title'])
    w.workspace_heading.title_label.setText(t['workspace'])
    w.profile_label.setText(t['source'])
    w.profile.setPlaceholderText(t['source_hint'])
    w.folder_label.setText(t['destination'])
    w.path.setPlaceholderText(t['folder_hint'])
    w.activity_heading.setText(t['activity'])
    if hasattr(w, 'queue_heading'):
        w.queue_heading.setText(t['queue'])
        w.queue_count.setText(t['queue_count'].format(count=len(getattr(w.download_queue, 'item_ids', []))))
        w.recycle_button.setToolTip(t['recycle'])
        w.recycle_button.setAccessibleName(t['recycle'])
        w.queue_top_button.setToolTip(t['queue_top'])
        w.queue_top_button.setAccessibleName(t['queue_top'])
        w.queue_cancel_selected_button.setToolTip(t['queue_cancel_selected'])
        w.queue_cancel_selected_button.setAccessibleName(t['queue_cancel_selected'])
        w.download_queue.set_language(w.lang)
    w.log_heading.setText(t['log'])
    w.log.setPlaceholderText(t['log_hint'])
    w.plan_label.setText(t['plan'])
    w.current_label.setText(t['current'])
    w.progress_caption.setText({'简体中文':'当前文件进度','繁體中文':'目前檔案進度','English':'FILE PROGRESS','日本語':'現在のファイル進捗'}[w.lang])
    w.album_label.setText(t['albums'])
    w.speed_label.setText(t['speed'])
    w.files_label.setText(t['files'])
    w.paste_button.setToolTip(t['paste'] + ' · Ctrl+Shift+V')
    w.paste_button.setAccessibleName(t['paste'])
    w.focus_button.setToolTip(t['expand'] if w.logs_hidden else t['focus'])
    w.focus_button.setAccessibleName(w.focus_button.toolTip())
    w.copy_log.setToolTip(t['copy'])
    w.copy_log.setAccessibleName(t['copy'])
    w.clear_cache.setToolTip(auxiliary['cache_tooltip'])
    w.clear_cache.setAccessibleName(auxiliary['cache_tooltip'])
    theme_tip={'简体中文':'切换明暗主题','繁體中文':'切換明暗主題','English':'Toggle appearance','日本語':'明暗テーマを切替'}[w.lang]
    w.theme.setToolTip(theme_tip)
    w.theme.setAccessibleName(theme_tip)
    mode_key = 'window_mode' if getattr(w, 'portrait_mode', False) else 'portrait_mode'
    w.layout_button.setToolTip(t[mode_key])
    w.layout_button.setAccessibleName(t[mode_key])
    w.start.setToolTip(text['start'])
    w.footer_hint.setText(t['expand'] if w.logs_hidden else t['focus'])
    w.stop_button.setText(t['stop'])
    w.stop_button.setToolTip(auxiliary['close_wait'])
    w.browse.setText(text['browse'])
    w.start.setText(text['start'])
    w.open_folder_button.setText(text['open_folder'])
    w.stage_rail.set_labels(t['steps'])
    w.tools_button.setToolTip(tr(w.lang,'tools'))
    w.tools_button.setAccessibleName(tr(w.lang,'tools'))
    w.schedule_action.setText(tr(w.lang,'schedules'))
    w.calendar_action.setText(tr(w.lang,'calendar'))
    w.resume_action.setText(tr(w.lang,'resume'))
    w.background_action.setText(tr(w.lang,'background'))
    w.background_settings_action.setText(tr(w.lang,'background_settings'))
    w.background_mode_switch.setToolTip(tr(w.lang,'background_mode'))
    w.background_opacity.setToolTip(tr(w.lang,'background_opacity'))
    w.background_opacity_value.setToolTip(tr(w.lang,'background_opacity'))
