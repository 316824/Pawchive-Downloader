"""Shared frameless window chrome for the Windows Pawchive UI.

The native Windows caption keeps a bright border around custom backgrounds and
cannot be themed consistently across dialogs.  This module provides a small
rounded caption with familiar minimize, maximize/restore, and close controls.
It intentionally stays self-contained so every modal window can opt in
without changing its content layout.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QEvent, QRect
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QRegion
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QWidget, QSizePolicy


class ChromeBorder(QWidget):
    """A transparent overlay that paints the soft rounded outer edge."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.rect())
            self.raise_()
            chrome = getattr(obj, '_pawchive_window_chrome', None)
            if chrome is not None:
                chrome.setGeometry(0, 0, obj.width(), chrome.height())
            _apply_round_mask(obj)
        return super().eventFilter(obj, event)

    def paintEvent(self, event):  # noqa: ARG002 - Qt signature
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = self.rect().adjusted(1, 1, -2, -2)
            path = QPainterPath()
            path.addRoundedRect(rect, 13, 13)
            gradient = QColor(255, 255, 255, 62)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(gradient, 1.0))
            painter.drawPath(path)
        finally:
            painter.end()


class WindowChrome(QFrame):
    """Rounded caption bar with compact, recognizable window actions."""

    def __init__(self, window: QWidget, title: str = ''):
        super().__init__(window)
        self.window = window
        self.setObjectName('windowChrome')
        self.setFixedHeight(42)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFont(QFont('Microsoft YaHei UI', 9))
        self.setMouseTracking(True)
        self._drag_origin = None
        self._window_origin = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 5, 8, 5)
        layout.setSpacing(6)
        self.icon = QLabel('✦')
        self.icon.setObjectName('chromeIcon')
        self.icon.setFixedWidth(24)
        self.icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.icon)
        self.title = QLabel(title or window.windowTitle() or 'Pawchive')
        self.title.setObjectName('chromeTitle')
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored,
                                 QSizePolicy.Policy.Preferred)
        layout.addWidget(self.title, 1)
        self.minimize = self._button('—', '最小化窗口', 'chromeMinimize')
        self.maximize = self._button('□', '最大化或还原窗口', 'chromeMaximize')
        self.close = self._button('×', '关闭窗口', 'chromeClose')
        layout.addWidget(self.minimize)
        layout.addWidget(self.maximize)
        layout.addWidget(self.close)
        self.minimize.clicked.connect(window.showMinimized)
        self.maximize.clicked.connect(self.toggle_maximized)
        self.close.clicked.connect(window.close)
        self._apply_style()

    def _button(self, text, tooltip, name):
        button = QToolButton(self)
        button.setObjectName(name)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setFixedSize(30, 30)
        button.setAutoRaise(True)
        return button

    def _apply_style(self):
        self.setStyleSheet("""
        QFrame#windowChrome {
            background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                stop:0 rgba(33, 42, 68, 230), stop:.5 rgba(56, 49, 91, 205),
                stop:1 rgba(27, 39, 66, 225));
            border-bottom: 1px solid rgba(225, 224, 255, 110);
            border-top-left-radius: 13px;
            border-top-right-radius: 13px;
        }
        QLabel#chromeIcon { color: #c9b7ff; font-size: 18px; background: transparent; }
        QLabel#chromeTitle { color: #f2f3ff; font-size: 13px; font-weight: 600; background: transparent; }
        QToolButton { color: #dfe5ff; border: 1px solid transparent; border-radius: 11px;
            background: rgba(255,255,255,22); font-size: 16px; font-weight: 600; }
        QToolButton:hover { background: rgba(193,181,255,90); border-color: rgba(240,237,255,150); }
        QToolButton#chromeClose:hover { background: rgba(242,141,157,220); color: #321923; }
        """)

    def set_title(self, title):
        self.title.setText(str(title or 'Pawchive'))

    def toggle_maximized(self):
        if self.window.isMaximized():
            self.window.showNormal()
            self.maximize.setText('□')
            self.maximize.setToolTip('最大化窗口')
        else:
            self.window.showMaximized()
            self.maximize.setText('❐')
            self.maximize.setToolTip('还原窗口')

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.globalPosition().toPoint()
            self._window_origin = self.window.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self._drag_origin
            self.window.move(self._window_origin + delta)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_origin = None
        self._window_origin = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def install_window_chrome(window: QWidget, *, title: str | None = None) -> WindowChrome:
    """Apply shared rounded chrome once and return its caption widget."""
    existing = getattr(window, '_pawchive_window_chrome', None)
    if existing is not None:
        if title is not None:
            existing.set_title(title)
        return existing
    # Frameless windows remove the bright OS border while retaining normal
    # maximize/minimize semantics through the custom controls.
    window.setWindowFlags(window.windowFlags() | Qt.WindowType.FramelessWindowHint)
    # Let the top-level stylesheet paint the themed surface.  A translucent
    # top-level window leaves the area below the cards black on light themes;
    # the rounded native mask below still clips all four corners without
    # requiring a transparent window surface.
    window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
    window.setProperty('pawchiveChromeSurface', True)
    bar = WindowChrome(window, title)
    layout = window.layout()
    if layout is not None:
        # The content layouts intentionally keep their own horizontal margins,
        # but the caption must span the complete window width.  Keep it as a
        # top-level child and reserve exactly its height in the content area.
        margins = layout.contentsMargins()
        layout.setContentsMargins(margins.left(), margins.top() + bar.height(),
                                   margins.right(), margins.bottom())
    else:
        bar.setGeometry(0, 0, window.width(), bar.height())
    border = ChromeBorder(window)
    border.setGeometry(window.rect())
    border.raise_()
    bar.setGeometry(0, 0, window.width(), bar.height())
    window.installEventFilter(border)
    _apply_round_mask(window)
    window._pawchive_window_chrome = bar
    window._pawchive_chrome_border = border
    return bar


def _apply_round_mask(window: QWidget, radius=13):
    """Clip the frameless surface so all four corners are actually rounded."""
    if window.width() <= 0 or window.height() <= 0:
        return
    rect = QRect(0, 0, window.width(), window.height())
    path = QPainterPath()
    path.addRoundedRect(rect, radius, radius)
    window.setMask(QRegion(path.toFillPolygon().toPolygon()))


__all__ = ['ChromeBorder', 'WindowChrome', 'install_window_chrome']
