"""Small, transparent desktop components; no workers, timers or external assets."""
from __future__ import annotations

import math
import re
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

def _palette(dark):
    return dict(text='#edf1fa' if dark else '#20283b',
                muted='#8e9bb0' if dark else '#6c778d',
                border='#293244' if dark else '#dde1e9',
                accent='#94afff' if dark else '#556ace',
                success='#6bd9cf' if dark else '#258c83',
                error='#f18d9d' if dark else '#c34962',
                ink='#10141c' if dark else '#ffffff')

def icon(kind, color='#94afff', size=20):
    """Return a sharp, monochrome 24-unit line icon at the requested logical size."""
    size = max(8, int(size))
    image = QPixmap(size * 2, size * 2)
    image.setDevicePixelRatio(2)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(size / 24, size / 24)
        pen = QPen(QColor(color))
        pen.setWidthF(1.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        line = lambda a, b, c, d: painter.drawLine(QPointF(a, b), QPointF(c, d))
        polyline = lambda *points: painter.drawPolyline(QPolygonF([QPointF(*point) for point in points]))
        path = QPainterPath()
        if kind == 'link':
            path.moveTo(10, 13)
            path.cubicTo(12, 15, 14, 15, 16, 13)
            path.lineTo(19, 10)
            path.cubicTo(23, 6, 17, 1, 14, 5)
            path.lineTo(12, 7)
            path.moveTo(14, 11)
            path.cubicTo(12, 9, 10, 9, 8, 11)
            path.lineTo(5, 14)
            path.cubicTo(1, 18, 7, 23, 10, 19)
            path.lineTo(12, 17)
        elif kind == 'folder':
            path.moveTo(3, 7)
            path.lineTo(3, 5)
            path.quadTo(3, 4, 5, 4)
            path.lineTo(9, 4)
            path.lineTo(12, 7)
            path.lineTo(19, 7)
            path.quadTo(21, 7, 21, 9)
            path.lineTo(21, 18)
            path.quadTo(21, 20, 19, 20)
            path.lineTo(5, 20)
            path.quadTo(3, 20, 3, 18)
            path.closeSubpath()
        elif kind == 'paste':
            painter.drawRoundedRect(QRectF(6, 5, 13, 16), 2, 2)
            painter.drawRoundedRect(QRectF(9, 3, 7, 4), 1, 1)
            line(9, 12, 16, 12)
            line(9, 16, 14, 16)
        elif kind == 'broom':
            line(16, 3, 11, 12)
            path.moveTo(8, 11)
            path.lineTo(15, 14)
            path.lineTo(12, 21)
            path.lineTo(3, 17)
            path.closeSubpath()
            line(8, 15, 6, 18)
            line(11, 16, 9, 19)
        elif kind == 'copy':
            painter.drawRoundedRect(QRectF(8, 8, 12, 13), 2, 2)
            path.moveTo(5, 16)
            path.quadTo(3, 16, 3, 14)
            path.lineTo(3, 5)
            path.quadTo(3, 3, 5, 3)
            path.lineTo(14, 3)
            path.quadTo(16, 3, 16, 5)
        elif kind == 'arrow':
            line(4, 12, 20, 12)
            polyline((14, 6), (20, 12), (14, 18))
        elif kind == 'chevron':
            polyline((6, 9), (12, 15), (18, 9))
        elif kind == 'left':
            polyline((15, 6), (9, 12), (15, 18))
        elif kind == 'right':
            polyline((9, 6), (15, 12), (9, 18))
        elif kind == 'check':
            polyline((5, 12), (10, 17), (19, 7))
        elif kind == 'activity':
            polyline((3, 12), (7, 12), (10, 5), (14, 19), (17, 12), (21, 12))
        elif kind == 'eye':
            path.moveTo(2, 12)
            path.cubicTo(7, 4, 17, 4, 22, 12)
            path.cubicTo(17, 20, 7, 20, 2, 12)
            painter.drawEllipse(QPointF(12, 12), 3, 3)
        elif kind == 'close':
            line(6, 6, 18, 18)
            line(18, 6, 6, 18)
        elif kind == 'settings':
            painter.drawEllipse(QPointF(12, 12), 6.5, 6.5)
            painter.drawEllipse(QPointF(12, 12), 2.2, 2.2)
            for step in range(8):
                angle = step * math.pi / 4
                line(12 + 7 * math.cos(angle), 12 + 7 * math.sin(angle),
                     12 + 10 * math.cos(angle), 12 + 10 * math.sin(angle))
        elif kind == 'phone':
            # A compact phone outline communicates the action available in
            # the default wide layout: switch to the tall, stacked view.
            painter.drawRoundedRect(QRectF(7, 2.5, 10, 19), 2.3, 2.3)
            line(10, 5, 14, 5)
            painter.drawEllipse(QPointF(12, 18.5), 0.9, 0.9)
        elif kind == 'desktop':
            # The inverse action shown while the stacked view is active.
            painter.drawRoundedRect(QRectF(3, 4, 18, 12), 1.6, 1.6)
            line(12, 16, 12, 20)
            line(8, 20, 16, 20)
        else:
            raise ValueError(f'Unknown desktop icon: {kind}')
        painter.drawPath(path)
    finally:
        painter.end()
    return QIcon(image)

class StageRail(QWidget):
    """A wrapping workflow indicator. Call set_stage(index, state) as work advances."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet('background:transparent;')
        self.dark, self.stage, self.state = True, 0, 'idle'
        self.labels, self._nodes = [], []
        self.setMinimumWidth(300)
        self.setMinimumHeight(80)
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(8, 8, 8, 8)
        self._row.setSpacing(0)
        self.set_labels(['设置', '预选', '下载', '完成'])

    def heightForWidth(self, width):
        cell = max(1, (int(width) - 16) // max(1, len(self._nodes)) - 8)
        return max(80, 47 + max((title.heightForWidth(cell) for _, title in self._nodes), default=16))

    def set_labels(self, labels):
        if not 2 <= len(labels) <= 6:
            raise ValueError('A workflow indicator needs two to six labels.')
        while self._row.count():
            item = self._row.takeAt(0)
            item.widget().hide()
            item.widget().deleteLater()
        self.labels, self._nodes = [str(text) for text in labels], []
        for text in self.labels:
            node = QWidget(self)
            column = QVBoxLayout(node)
            column.setContentsMargins(4, 0, 4, 0)
            column.setSpacing(7)
            wrapped = re.sub(r'[A-Za-z]{8,}', lambda match: '\u200b'.join(match[0]), text)
            mark, title = QLabel(node), QLabel(wrapped, node)
            title.setAccessibleName(text)
            mark.setFixedSize(24, 24)
            mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
            title.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
            title.setWordWrap(True)
            title.setTextFormat(Qt.TextFormat.PlainText)
            title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            column.addWidget(mark, 0, Qt.AlignmentFlag.AlignHCenter)
            column.addWidget(title, 1)
            self._row.addWidget(node, 1)
            self._nodes.append((mark, title))
        self.stage = min(self.stage, len(self.labels) - 1)
        self.setAccessibleName(' · '.join(self.labels))
        self.set_theme(self.dark)
        self.updateGeometry()

    def set_stage(self, index, state='active'):
        if state not in ('idle', 'active', 'done', 'error'):
            raise ValueError('Unknown workflow state.')
        self.stage = max(0, min(int(index), len(self.labels) - 1))
        self.state = state
        self.set_theme(self.dark)

    def set_theme(self, dark):
        self.dark = bool(dark)
        palette = _palette(self.dark)
        for index, (mark, title) in enumerate(self._nodes):
            completed = index < self.stage or (index == self.stage and self.state == 'done')
            current = index == self.stage and self.state in ('active', 'error')
            color = palette['success'] if completed else palette[
                'error' if self.state == 'error' else 'accent'] if current else palette['border']
            fill = color if completed or current else 'transparent'
            ink = palette['ink'] if completed or current else palette['muted']
            mark.setStyleSheet(f'background:{fill};color:{ink};border:1px solid {color};'
                              'border-radius:12px;font-size:11px;font-weight:600;')
            if completed:
                mark.setPixmap(icon('check', ink, 14).pixmap(14, 14))
            else:
                mark.setText('!' if current and self.state == 'error' else str(index + 1))
            title.setStyleSheet('background:transparent;border:0;font-size:12px;'
                               f'color:{palette["text"] if current else palette["muted"]};')
        self.setAccessibleDescription(f'{self.labels[self.stage]} · {self.state}')
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            palette = _palette(self.dark)
            for index in range(len(self._nodes) - 1):
                first = self._nodes[index][0].mapTo(self, QPoint(12, 12))
                second = self._nodes[index + 1][0].mapTo(self, QPoint(12, 12))
                color = QColor(palette['success'] if index < self.stage else palette['border'])
                color.setAlpha(145 if index < self.stage else 200)
                painter.setPen(QPen(color, 1))
                painter.drawLine(QPointF(first.x() + 20, first.y()),
                                 QPointF(second.x() - 20, second.y()))
        finally:
            painter.end()

class CardHeading(QWidget):
    """Transparent section heading; labels stay public for the app's translations."""

    def __init__(self, title, subtitle=None, index=None, parent=None):
        super().__init__(parent)
        self.setStyleSheet('background:transparent;')
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self.index_label = QLabel(str(index).zfill(2) if index is not None else '', self)
        self.index_label.setFixedSize(35, 35)
        self.index_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.index_label.setVisible(index is not None)
        row.addWidget(self.index_label, 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        column.setSpacing(3)
        self.title_label, self.subtitle_label = QLabel(str(title)), QLabel(subtitle or '')
        for label in (self.title_label, self.subtitle_label):
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            column.addWidget(label)
        self.subtitle_label.setVisible(bool(subtitle))
        row.addLayout(column, 1)
        self.set_theme(True)

    def set_theme(self, dark):
        palette = _palette(dark)
        self.index_label.setStyleSheet(f'background:transparent;color:{palette["accent"]};'
                                      f'border:1px solid {palette["border"]};border-radius:11px;'
                                      'font-size:12px;font-weight:600;')
        self.title_label.setStyleSheet(f'background:transparent;border:0;color:{palette["text"]};'
                                      'font-size:17px;font-weight:600;')
        self.subtitle_label.setStyleSheet(f'background:transparent;border:0;color:{palette["muted"]};'
                                         'font-size:12px;')
