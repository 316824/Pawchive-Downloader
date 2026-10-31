"""Local cover selection; searches never fetch data or change hidden selections."""
from __future__ import annotations

from collections import OrderedDict
from html import escape
import random
import re

from PySide6.QtCore import QEvent, QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImageReader, QPainter, QPainterPath, QPixmap, QTransform
from PySide6.QtWidgets import (QApplication, QDialog, QFrame, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from desktop_visuals import icon


COPY = {
    '简体中文': dict(search='搜索图集标题…', selected='已选 {selected} / {total}',
        shown='显示 {visible} / {total} 个图集', confirm='下载所选图集',
        empty='没有匹配的图集', count='扫描完成 · {total} 个图集'),
    '繁體中文': dict(search='搜尋圖集標題…', selected='已選 {selected} / {total}',
        shown='顯示 {visible} / {total} 個圖集', confirm='下載所選圖集',
        empty='沒有符合的圖集', count='掃描完成 · {total} 個圖集'),
    'English': dict(search='Search post titles…', selected='Selected {selected} / {total}',
        shown='Showing {visible} / {total} posts', confirm='Download selected',
        empty='No matching posts', count='Scan complete · {total} posts'),
    '日本語': dict(search='投稿タイトルを検索…', selected='選択 {selected} / {total} 件',
        shown='{total} 件中 {visible} 件を表示', confirm='選択した投稿をダウンロード',
        empty='一致する投稿がありません', count='スキャン完了 · {total} 件の投稿'),
}
COLORS = ('#ef809d', '#edab65', '#ddc970', '#73cba2', '#64cbd4', '#8fa7f1', '#c594e3')


class _Cover(QWidget):
    """Paint only visible, bounded thumbnails, without resizing every hidden card."""

    def __init__(self, filename, missing, palette, cache, parent=None):
        super().__init__(parent)
        self.missing, self.palette = missing, palette
        self.filename, self.cache = str(filename or ''), cache
        self.failed = not bool(filename)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setMinimumWidth(0)
        self.setFixedHeight(152)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    def _pixmap(self):
        if self.failed:
            return QPixmap()
        if self.filename in self.cache:
            self.cache.move_to_end(self.filename)
            return self.cache[self.filename]
        reader = QImageReader(self.filename)
        reader.setAutoTransform(True)
        size = reader.size()
        if size.isValid() and (size.width() > 420 or size.height() > 300):
            size.scale(420, 300, Qt.AspectRatioMode.KeepAspectRatio)
            reader.setScaledSize(size)
        pixmap = QPixmap.fromImage(reader.read())
        if pixmap.isNull():
            self.failed = True
        else:
            if pixmap.width() > 420 or pixmap.height() > 300:
                pixmap = pixmap.scaled(420, 300, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation)
            self.cache[self.filename] = pixmap
            if len(self.cache) > 64:
                self.cache.popitem(last=False)
        return pixmap

    def paintEvent(self, event):
        pixmap = self._pixmap()
        painter = QPainter(self)
        try:
            painter.setRenderHints(QPainter.RenderHint.Antialiasing |
                                   QPainter.RenderHint.SmoothPixmapTransform)
            area = QRectF(self.rect())
            clip = QPainterPath()
            clip.addRoundedRect(area, 10, 10)
            painter.setClipPath(clip)
            painter.fillRect(area, QColor(self.palette['panel']))
            if pixmap.isNull():
                painter.setPen(QColor(self.palette['muted']))
                painter.drawText(area.adjusted(12, 12, -12, -12),
                                 Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.missing)
            else:
                size = pixmap.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
                destination = QRectF((self.width() - size.width()) / 2,
                                     (self.height() - size.height()) / 2,
                                     size.width(), size.height())
                painter.drawPixmap(destination, pixmap, QRectF(pixmap.rect()))
        finally:
            painter.end()


class CoverCard(QFrame):
    toggled = Signal(bool)

    def __init__(self, entry, checkbox_class, text, palette, dark, parent=None, *, cover_cache=None):
        super().__init__(parent)
        self.entry, self.palette = entry, palette
        self.color = random.choice(COLORS)
        self.setObjectName('postCoverCard')
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        title = str(entry.get('title') or '')
        self.setAccessibleName(title)
        self.setToolTip(escape(title))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 9, 9, 11)
        layout.setSpacing(10)
        area = QGridLayout()
        area.setContentsMargins(0, 0, 0, 0)
        self.cover = _Cover(entry.get('image'), text['cover_missing'], palette,
                            cover_cache if cover_cache is not None else OrderedDict())
        area.addWidget(self.cover, 0, 0)
        self.check = checkbox_class(self)
        self.check.setAccessibleName(title)
        self.check.setProperty('lightTheme', not dark)
        self.check.setFixedSize(32, 32)
        self.check.setStyleSheet('QCheckBox{background:rgba(16,20,28,105);border:0;'
                                'border-radius:10px;padding:6px;}')
        self.check.blockSignals(True)
        self.check.setChecked(True)
        self.check.blockSignals(False)
        area.addWidget(self.check, 0, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.formats = QLabel()
        self.formats.setWordWrap(True)
        self.formats.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.formats.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.formats.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.formats.setStyleSheet('background:rgba(12,16,24,205);border:0;border-radius:6px;'
                                  'padding:4px 6px;font-size:11px;font-weight:600;')
        extensions = entry.get('formats') or []
        self.formats.setText(' '.join(f'<span style="color:{COLORS[n % len(COLORS)]}">'
                                     f'{escape(str(ext).lstrip(".").upper())}</span>'
                                     for n, ext in enumerate(extensions)) if extensions else
                             f'<span style="color:#d1d8e4">{escape(text["no_direct"])}</span>')
        area.addWidget(self.formats, 0, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(area)
        display = title[:160] + ('…' if len(title) > 160 else '')
        display = re.sub(r'[A-Za-z0-9_-]{12,}', lambda match: '\u200b'.join(match[0]), display)
        self.title_label = QLabel(display)
        self.title_label.setTextFormat(Qt.TextFormat.PlainText)
        self.title_label.setWordWrap(True)
        self.title_label.setMinimumHeight(36)
        self.title_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.title_label.setStyleSheet(f'background:transparent;border:0;color:{palette["text"]};'
                                      'font-size:12px;')
        layout.addWidget(self.title_label)
        self.check.toggled.connect(self._changed)
        self._style(True)
        self.check.raise_()
        self.formats.raise_()

    def _style(self, checked):
        border = self.color if checked else self.palette['line']
        self.setStyleSheet(f'QFrame#postCoverCard{{background:{self.palette["card"]};'
                          f'border:2px solid {border};border-radius:15px;}}'
                          'QFrame#postCoverCard:focus{border-style:dashed;}')

    def _changed(self, checked):
        if checked:
            self.color = random.choice(COLORS)
        self._style(checked)
        self.toggled.emit(checked)

    def set_selected(self, checked):
        was_blocked = self.check.blockSignals(True)
        try:
            changed = self.check.isChecked() != checked
            self.check.setChecked(checked)
            if checked and changed:
                self.color = random.choice(COLORS)
            self._style(checked)
        finally:
            self.check.blockSignals(was_blocked)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.check.setChecked(not self.check.isChecked())
            event.accept()
        else:
            super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.check.setChecked(not self.check.isChecked())
            event.accept()
        else:
            super().keyPressEvent(event)


class PostPicker(QDialog):
    def __init__(self, parent, titles, checkbox_class, text):
        super().__init__(parent)
        self.titles, self.text = list(titles), text
        self.copy = COPY.get(getattr(parent, 'lang', '简体中文'), COPY['简体中文'])
        self.dark = bool(getattr(parent, '_dark', True))
        self.palette = parent.palette_tokens
        self.selected = set(range(len(self.titles)))
        self._cover_cache = OrderedDict()
        self.cards, self.checks = [], []
        self._columns, self._visible = 0, []
        self.setObjectName('postPicker')
        self.setWindowTitle(text['choose_title'])
        self.setWindowIcon(parent.windowIcon())
        geometry = (parent.screen() or QApplication.primaryScreen()).availableGeometry()
        width, height = max(280, geometry.width() - 40), max(300, geometry.height() - 48)
        self.setMinimumSize(min(520, width), min(380, height))
        self.resize(min(980, width), min(740, height))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 24, 26, 22)
        layout.setSpacing(16)
        header = QHBoxLayout()
        self.heading = QLabel(text['choose_title'])
        self.heading.setWordWrap(True)
        self.heading.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.heading.setStyleSheet('font-size:21px;font-weight:600;')
        self.selection_count = QLabel()
        self.selection_count.setStyleSheet(f'color:{self.palette["accent"]};font-size:12px;')
        header.addWidget(self.heading, 1)
        header.addWidget(self.selection_count)
        layout.addLayout(header)
        self.summary = QLabel(self.copy['count'].format(total=len(self.titles)))
        self.summary.setStyleSheet(f'color:{self.palette["muted"]};font-size:12px;')
        layout.addWidget(self.summary)
        self.search = QLineEdit()
        self.search.setObjectName('postSearch')
        self.search.setPlaceholderText(self.copy['search'])
        self.search.setAccessibleName(self.copy['search'])
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(42)
        layout.addWidget(self.search)
        tools = QHBoxLayout()
        tools.setSpacing(10)
        self.all_button, self.none_button = QPushButton(text['select_all']), QPushButton(text['select_none'])
        tools.addWidget(self.all_button)
        tools.addWidget(self.none_button)
        self.visible_count = QLabel()
        self.visible_count.setWordWrap(True)
        self.visible_count.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.visible_count.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.visible_count.setStyleSheet(f'color:{self.palette["muted"]};font-size:11px;')
        tools.addWidget(self.visible_count, 1)
        layout.addLayout(tools)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setMinimumSize(0, 0)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body = QWidget()
        self.body.setMinimumWidth(0)
        self.grid = QGridLayout(self.body)
        self.grid.setContentsMargins(0, 0, 4, 8)
        self.grid.setSpacing(14)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.body)
        layout.addWidget(self.scroll, 1)
        self.empty = QLabel(self.copy['empty'], self.body)
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setMinimumHeight(150)
        self.empty.setStyleSheet(f'color:{self.palette["muted"]};font-size:14px;')
        self.empty.hide()
        for index, entry in enumerate(self.titles):
            card = CoverCard(entry, checkbox_class, text, self.palette, self.dark, self.body,
                             cover_cache=self._cover_cache)
            card.toggled.connect(lambda checked, n=index: self._set_selected(n, checked))
            self.cards.append(card)
            self.checks.append(card.check)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.cancel_button = QPushButton(text['cancel'])
        self.confirm_button = QPushButton(self.copy['confirm'])
        self.confirm_button.setObjectName('pickerPrimary')
        self.confirm_button.setStyleSheet(f'QPushButton#pickerPrimary{{background:{self.palette["button"]};'
                                         f'color:{self.palette["button_text"]};border:0;padding:11px 19px;'
                                         'border-radius:11px;font-weight:600;}'
                                         f'QPushButton#pickerPrimary:disabled{{background:{self.palette["hover"]};'
                                         f'color:{self.palette["muted"]};}}')
        self.confirm_button.setAutoDefault(False)
        self.cancel_button.setAutoDefault(False)
        footer.addWidget(self.cancel_button)
        footer.addWidget(self.confirm_button)
        layout.addLayout(footer)
        self.top_button = QPushButton(self.scroll.viewport())
        self.top_button.setObjectName('pickerTop')
        self.top_button.setToolTip(text['back_top'])
        self.top_button.setAccessibleName(text['back_top'])
        self.top_button.setFixedSize(40, 40)
        self.top_button.setIcon(QIcon(icon('arrow', self.palette['text'], 18).pixmap(18, 18)
                                     .transformed(QTransform().rotate(-90))))
        self.top_button.setIconSize(QSize(18, 18))
        self.top_button.setStyleSheet(f'QPushButton#pickerTop{{background:{self.palette["card"]};'
                                     f'border:1px solid {self.palette["line"]};border-radius:20px;padding:0;}}'
                                     f'QPushButton#pickerTop:hover{{background:{self.palette["hover"]};'
                                     f'border-color:{self.palette["accent"]};}}')
        self.top_button.hide()
        scrollbar = self.scroll.verticalScrollBar()
        self.scroll_animation = QPropertyAnimation(scrollbar, b'value', self)
        self.scroll_animation.setDuration(320)
        self.scroll_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.top_button.clicked.connect(self.back_to_top)
        scrollbar.valueChanged.connect(self._update_top_button)
        scrollbar.rangeChanged.connect(lambda *_: self._update_top_button())
        scrollbar.sliderPressed.connect(self.scroll_animation.stop)
        self.finished.connect(self.scroll_animation.stop)
        self.finished.connect(lambda *_: self._cover_cache.clear())
        self.scroll.viewport().installEventFilter(self)
        self.search.textChanged.connect(lambda *_: self._layout_cards(force=True))
        self.all_button.clicked.connect(lambda: self._select_all(True))
        self.none_button.clicked.connect(lambda: self._select_all(False))
        self.cancel_button.clicked.connect(self.reject)
        self.confirm_button.clicked.connect(self.accept)
        for button in self.findChildren(QPushButton):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._layout_cards(force=True)
        self._update_count()

    def _set_selected(self, index, checked):
        self.selected.add(index) if checked else self.selected.discard(index)
        self._update_count()

    def _select_all(self, checked):
        self.selected = set(range(len(self.titles))) if checked else set()
        for card in self.cards:
            card.set_selected(checked)
        self._update_count()

    def _update_count(self):
        self.selection_count.setText(self.copy['selected'].format(
            selected=len(self.selected), total=len(self.titles)))
        self.confirm_button.setEnabled(bool(self.selected))

    def _layout_cards(self, force=False):
        query = self.search.text().strip().casefold()
        visible = [index for index, entry in enumerate(self.titles)
                   if query in str(entry.get('title') or '').casefold()]
        columns = max(2, min(4, (self.scroll.viewport().width() + 14) // 219))
        if not force and visible == self._visible and columns == self._columns:
            return
        self._visible, self._columns = visible, columns
        self.body.setUpdatesEnabled(False)
        try:
            while self.grid.count():
                self.grid.takeAt(0).widget().hide()
            for column in range(4):
                self.grid.setColumnStretch(column, 1 if column < columns else 0)
            for position, index in enumerate(visible):
                self.grid.addWidget(self.cards[index], position // columns, position % columns)
                self.cards[index].show()
            if not visible:
                self.grid.addWidget(self.empty, 0, 0, 1, columns)
                self.empty.show()
        finally:
            self.body.setUpdatesEnabled(True)
        self.visible_count.setText(self.copy['shown'].format(visible=len(visible), total=len(self.titles)))

    def _update_top_button(self, *_):
        viewport = self.scroll.viewport()
        self.top_button.move(max(0, viewport.width() - 54), max(0, viewport.height() - 54))
        self.top_button.setVisible(self.scroll.verticalScrollBar().value() > 40)
        self.top_button.raise_()

    def back_to_top(self):
        self.scroll_animation.stop()
        self.scroll_animation.setStartValue(self.scroll.verticalScrollBar().value())
        self.scroll_animation.setEndValue(0)
        self.scroll_animation.start()

    def eventFilter(self, watched, event):
        if watched is self.scroll.viewport():
            if event.type() == QEvent.Type.Resize:
                self._layout_cards()
                self._update_top_button()
            elif event.type() == QEvent.Type.Wheel:
                self.scroll_animation.stop()
        return super().eventFilter(watched, event)


def choose_posts(parent, titles, checkbox_class, text):
    dialog = PostPicker(parent, titles, checkbox_class, text)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return {str(dialog.titles[index]['url']) for index in dialog.selected
                if dialog.titles[index].get('url')}
    finally:
        dialog.deleteLater()
