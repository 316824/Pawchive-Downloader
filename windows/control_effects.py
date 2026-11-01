"""Control effects drawn above the layout, without clipping at checkbox edges."""
from weakref import WeakSet

from PySide6.QtCore import QEvent, QPointF, QRect, Qt
from PySide6.QtGui import QColor, QPainter, QRadialGradient
from PySide6.QtWidgets import QAbstractScrollArea, QStyle, QStyleOptionButton, QWidget


DARK_GLOW = '#b89af4'
LIGHT_GLOW = '#8963cf'


class ControlGlowOverlay(QWidget):
    """One passive, timer-free layer for each main window or cover dialog."""

    def __init__(self, window):
        super().__init__(window)
        self.controls = WeakSet()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        window.installEventFilter(self)
        for area in window.findChildren(QAbstractScrollArea):
            area.verticalScrollBar().valueChanged.connect(lambda *_: self.update())
            area.horizontalScrollBar().valueChanged.connect(lambda *_: self.update())
        self.sync()

    def sync(self):
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        particles = getattr(self.parentWidget(), 'overlay', None)
        if particles is not None and particles is not self:
            self.stackUnder(particles)

    def refresh(self, control):
        if control._glow > 0 or control._ripple > 0:
            self.controls.add(control)
        else:
            self.controls.discard(control)
        self.sync()
        self.update()

    def eventFilter(self, watched, event):
        if watched is self.parentWidget() and event.type() in (
                QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest):
            self.sync()
            self.update()
        return super().eventFilter(watched, event)

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            for control in list(self.controls):
                saved = False
                try:
                    if not control.isVisible() or control.window() is not self.parentWidget():
                        continue
                    hover, ripple = control._glow, control._ripple
                    if hover <= 0 and ripple <= 0:
                        continue
                    option = QStyleOptionButton()
                    control.initStyleOption(option)
                    indicator = control.style().subElementRect(
                        QStyle.SubElement.SE_CheckBoxIndicator, option, control)
                    if not control.visibleRegion().contains(indicator.center()):
                        continue
                    center = QPointF(control.mapTo(self.parentWidget(), indicator.center()))
                    light = bool(control.property('lightTheme'))
                    color = QColor(LIGHT_GLOW if light else DARK_GLOW)
                    size = max(indicator.width(), indicator.height())
                    painter.save()
                    saved = True
                    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver
                        if light else QPainter.CompositionMode.CompositionMode_Screen)
                    ancestor=control.parentWidget()
                    while ancestor is not None and ancestor is not self.parentWidget():
                        if isinstance(ancestor,QAbstractScrollArea):
                            viewport=ancestor.viewport()
                            origin=viewport.mapTo(self.parentWidget(),viewport.rect().topLeft())
                            painter.setClipRect(QRect(origin,viewport.size()))
                            break
                        ancestor=ancestor.parentWidget()
                    if hover > 0:
                        radius = size * .95
                        gradient = QRadialGradient(center, radius)
                        inner, outer = QColor(color), QColor(color)
                        inner.setAlpha(int(64 * hover))
                        outer.setAlpha(int(24 * hover))
                        gradient.setColorAt(0, inner)
                        gradient.setColorAt(.55, outer)
                        clear = QColor(color); clear.setAlpha(0)
                        gradient.setColorAt(1, clear)
                        painter.setBrush(gradient)
                        painter.drawEllipse(center, radius, radius)
                    if ripple > 0:
                        radius = max(1., size * 1.45 * ripple)
                        fade = (1-ripple)**2 * (1+2*ripple)
                        edge = QRadialGradient(center, radius)
                        clear = QColor(color); clear.setAlpha(0)
                        dim, bright = QColor(color), QColor(color)
                        dim.setAlpha(int(12 * fade)); bright.setAlpha(int(135 * fade))
                        edge.setColorAt(0, clear); edge.setColorAt(.65, dim)
                        edge.setColorAt(.84, bright); edge.setColorAt(1, clear)
                        painter.setBrush(edge)
                        painter.drawEllipse(center, radius, radius)
                except RuntimeError:
                    # A dialog can delete its children before the next paint.
                    self.controls.discard(control)
                finally:
                    if saved:
                        painter.restore()
        finally:
            painter.end()


def refresh_checkbox_effect(control):
    window = control.window()
    if window is control:
        return  # A new checkbox has not been inserted into the layout yet.
    overlay = getattr(window, '_control_glow_overlay', None)
    if overlay is None and control._glow <= 0 and control._ripple <= 0:
        return
    if overlay is None:
        overlay = ControlGlowOverlay(window)
        window._control_glow_overlay = overlay
    overlay.refresh(control)
