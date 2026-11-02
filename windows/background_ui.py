"""Qt background preview, crop editor and per-window background settings.

Only bounded thumbnails produced by :class:`BackgroundEditorDialog` are used
by runtime windows.  The source image is decoded in the editor, cropped, and
then released; page switches never reopen the source file.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (QBuffer, QEvent, QObject, QPoint, QPointF, QRectF, Qt,
                            Signal)
from PySide6.QtGui import (QBrush, QColor, QImage, QImageReader, QPainter, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout,
                               QGraphicsPixmapItem, QGraphicsRectItem,
                               QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel,
                               QMessageBox, QPushButton, QSlider, QVBoxLayout,
                               QWidget, QFrame, QApplication)

from background_store import (ALLOWED_IMAGE_SUFFIXES, DEFAULT_OVERLAY,
                               MAX_PREVIEW_BYTES, MAX_PREVIEW_DIMENSION,
                               MAX_SOURCE_DIMENSION, MAX_SOURCE_PIXELS,
                               BackgroundError, BackgroundStore,
                               normalize_overlay, validate_source_path)


def _read_source_image(path):
    """Decode a user image only after checking dimensions with QImageReader."""
    resolved, _ = validate_source_path(path)
    reader = QImageReader(str(resolved))
    reader.setAutoTransform(True)
    size = reader.size()
    if not size.isValid() or size.width() <= 0 or size.height() <= 0:
        raise BackgroundError('The selected file is not a readable image.')
    if (size.width() > MAX_SOURCE_DIMENSION or size.height() > MAX_SOURCE_DIMENSION or
            size.width() * size.height() > MAX_SOURCE_PIXELS):
        raise BackgroundError('The image dimensions exceed the safe limit.')
    image = reader.read()
    if image.isNull():
        raise BackgroundError(reader.errorString() or 'The selected image could not be decoded.')
    return resolved, image


def _rgb_image(image):
    """Flatten transparency onto a dark neutral canvas before JPEG encoding."""
    if image.format() in (QImage.Format.Format_RGB32, QImage.Format.Format_RGB888):
        return image.convertToFormat(QImage.Format.Format_RGB888)
    canvas = QImage(image.size(), QImage.Format.Format_RGB32)
    canvas.fill(QColor('#11151e'))
    painter = QPainter(canvas)
    painter.drawImage(0, 0, image)
    painter.end()
    return canvas.convertToFormat(QImage.Format.Format_RGB888)


def _encode_preview(image):
    """Return bounded JPEG bytes and dimensions for a cropped image."""
    if image.isNull():
        raise BackgroundError('The crop is empty.')
    scale = min(1.0, MAX_PREVIEW_DIMENSION / max(image.width(), image.height()))
    if scale < 1.0:
        image = image.scaled(max(1, int(image.width() * scale)),
                             max(1, int(image.height() * scale)),
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    image = _rgb_image(image)
    for quality in (88, 76, 64, 52):
        buffer = QBuffer()
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        if not image.save(buffer, 'JPG', quality):
            buffer.close()
            continue
        data = bytes(buffer.data())
        buffer.close()
        if data and len(data) <= MAX_PREVIEW_BYTES:
            return data, (image.width(), image.height())
    raise BackgroundError('The generated preview is too large; choose a smaller crop.')


def _clamp_rect(rect, bounds, minimum=24.0):
    """Keep a crop rectangle inside the image scene."""
    left = max(bounds.left(), min(rect.left(), bounds.right() - minimum))
    top = max(bounds.top(), min(rect.top(), bounds.bottom() - minimum))
    right = min(bounds.right(), max(rect.right(), left + minimum))
    bottom = min(bounds.bottom(), max(rect.bottom(), top + minimum))
    return QRectF(left, top, max(minimum, right - left), max(minimum, bottom - top))


class CropView(QGraphicsView):
    """Image scene with movable/resizable crop rectangle and wheel zoom."""

    cropChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene_obj = QGraphicsScene(self)
        self.setScene(self.scene_obj)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setBackgroundBrush(QBrush(QColor('#080b11')))
        self.setMouseTracking(True)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.image_item = None
        self.crop_item = None
        self.handle_item = None
        self.image_size = None
        self._fit_pending = False
        self._interaction = None
        self._last_scene = QPointF()
        self._last_view = QPoint()

    def set_image(self, image, target_aspect=1.25):
        self.scene_obj.clear()
        self.image_item = QGraphicsPixmapItem(QPixmap.fromImage(image))
        self.scene_obj.addItem(self.image_item)
        self.image_size = image.size()
        bounds = QRectF(0, 0, image.width(), image.height())
        aspect = max(0.2, min(5.0, float(target_aspect or 1.25)))
        if image.width() / max(1, image.height()) > aspect:
            height = image.height() * .78
            width = height * aspect
        else:
            width = image.width() * .78
            height = width / aspect
        crop = QRectF((image.width() - width) / 2, (image.height() - height) / 2,
                      width, height)
        self.crop_item = QGraphicsRectItem(crop)
        self.crop_item.setPen(QPen(QColor('#b89af4'), 3))
        self.crop_item.setBrush(QBrush(QColor(137, 99, 207, 36)))
        self.crop_item.setZValue(2)
        self.scene_obj.addItem(self.crop_item)
        self.handle_item = QGraphicsRectItem(0, 0, 14, 14)
        self.handle_item.setPen(QPen(QColor('#ffffff'), 1))
        self.handle_item.setBrush(QBrush(QColor('#b89af4')))
        self.handle_item.setZValue(3)
        self.scene_obj.addItem(self.handle_item)
        self.scene_obj.setSceneRect(bounds)
        self._sync_handle()
        self._fit_pending = True
        self.fitInView(bounds, Qt.AspectRatioMode.KeepAspectRatio)
        self._fit_pending = False
        self.cropChanged.emit()

    def _sync_handle(self):
        if self.crop_item is None or self.handle_item is None:
            return
        rect = self.crop_item.rect()
        self.handle_item.setRect(rect.right() - 7, rect.bottom() - 7, 14, 14)

    def normalized_crop(self):
        if self.crop_item is None or self.image_size is None:
            return {'x': 0.0, 'y': 0.0, 'width': 1.0, 'height': 1.0}
        rect = self.crop_item.rect()
        return {
            'x': max(0.0, min(1.0, rect.left() / self.image_size.width())),
            'y': max(0.0, min(1.0, rect.top() / self.image_size.height())),
            'width': max(0.0001, min(1.0, rect.width() / self.image_size.width())),
            'height': max(0.0001, min(1.0, rect.height() / self.image_size.height())),
        }

    def crop_image(self):
        if self.image_item is None or self.image_size is None:
            return QImage()
        rect = self.crop_item.rect().toAlignedRect().intersected(
            QRectF(self.scene_obj.sceneRect()).toRect())
        return self.image_item.pixmap().toImage().copy(rect)

    def wheelEvent(self, event):
        if self.image_item is None:
            return super().wheelEvent(event)
        delta = event.angleDelta().y()
        if not delta:
            return
        factor = 1.15 if delta > 0 else 1 / 1.15
        current = self.transform().m11()
        if .08 <= current * factor <= 12:
            self.scale(factor, factor)
        event.accept()

    def mousePressEvent(self, event):
        if self.crop_item is None:
            return super().mousePressEvent(event)
        scene_pos = self.mapToScene(event.position().toPoint())
        self._last_scene = scene_pos
        self._last_view = event.position().toPoint()
        if event.button() == Qt.MouseButton.LeftButton:
            if self.handle_item and self.handle_item.contains(scene_pos):
                self._interaction = 'resize'
            elif self.crop_item.contains(scene_pos):
                self._interaction = 'move'
            else:
                self._interaction = 'pan'
            event.accept()
            return
        if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self._interaction = 'pan'
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self._interaction or self.crop_item is None:
            return super().mouseMoveEvent(event)
        scene_pos = self.mapToScene(event.position().toPoint())
        bounds = self.scene_obj.sceneRect()
        if self._interaction == 'move':
            delta = scene_pos - self._last_scene
            self.crop_item.setRect(_clamp_rect(self.crop_item.rect().translated(delta), bounds))
            self._sync_handle(); self.cropChanged.emit()
        elif self._interaction == 'resize':
            rect = self.crop_item.rect()
            rect.setRight(scene_pos.x()); rect.setBottom(scene_pos.y())
            self.crop_item.setRect(_clamp_rect(rect, bounds)); self._sync_handle(); self.cropChanged.emit()
        elif self._interaction == 'pan':
            delta = event.position().toPoint() - self._last_view
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
        self._last_scene = scene_pos
        self._last_view = event.position().toPoint()
        event.accept()

    def mouseReleaseEvent(self, event):
        self._interaction = None
        super().mouseReleaseEvent(event)


class BackgroundLayer(QWidget):
    """A transparent child that paints one cached preview and its mask."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.pixmap = QPixmap()
        self.overlay = DEFAULT_OVERLAY
        self.aspect = 1.0

    def set_background(self, pixmap, overlay=DEFAULT_OVERLAY, aspect=1.0):
        self.pixmap = pixmap if isinstance(pixmap, QPixmap) else QPixmap()
        self.overlay = normalize_overlay(overlay)
        try:
            self.aspect = max(.0001, float(aspect))
        except (TypeError, ValueError):
            self.aspect = 1.0
        self.update()

    def clear_background(self):
        self.pixmap = QPixmap()
        self.update()

    def paintEvent(self, event):
        if self.pixmap.isNull():
            return
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.fillRect(self.rect(), QColor('#070a10'))
            target = QRectF(self.rect())
            scaled = self.pixmap.scaled(target.size().toSize(),
                                        Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation)
            x = target.left() + (target.width() - scaled.width()) / 2
            y = target.top() + (target.height() - scaled.height()) / 2
            painter.drawPixmap(QPointF(x, y), scaled)
            alpha = int(255 * self.overlay / 100)
            painter.fillRect(self.rect(), QColor(5, 8, 14, alpha))
        finally:
            painter.end()


def load_preview(path):
    """Load only a previously bounded preview; never decode the source path."""
    try:
        if Path(path).stat().st_size > MAX_PREVIEW_BYTES:
            return None
    except OSError:
        return None
    pixmap = QPixmap(str(path))
    if pixmap.isNull() or pixmap.width() > MAX_PREVIEW_DIMENSION or pixmap.height() > MAX_PREVIEW_DIMENSION:
        return None
    return pixmap


class _BackgroundHostFilter(QObject):
    def __init__(self, host, layer, refresh):
        super().__init__(host)
        self.host = host
        self.layer = layer
        self.refresh = refresh

    def eventFilter(self, watched, event):
        if watched is self.host:
            if event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.WindowStateChange):
                self.layer.setGeometry(self.host.rect())
                self.layer.lower()
                self.refresh()
            elif event.type() in (QEvent.Type.Hide, QEvent.Type.Close):
                self.layer.clear_background()
        return super().eventFilter(watched, event)


def install_background_host(host, store, page, *, mode_getter=None):
    """Attach a cache-backed background layer to a dialog-like widget."""
    if store is None:
        return None
    page = str(page)
    layer = BackgroundLayer(host)
    layer.setGeometry(host.rect())
    layer.show()
    layer.lower()
    store.register_layer(page, layer)
    host._background_auto_portrait = False
    host._background_previous_size = host.size()

    def adjust_portrait(config):
        tall = bool(config and float(config.get('aspect', 1.0)) < .82)
        if tall and not host.isVisible() and not host._background_auto_portrait:
            # Dialog constructors often call install_background_host before
            # their final resize().  Wait for the first Show event so the
            # normal size is what gets restored when the image is removed.
            return
        needs_portrait = tall and (
            not host._background_auto_portrait or host.height() <= host.width() * 1.25)
        if needs_portrait:
            if not host._background_auto_portrait:
                host._background_previous_size = host.size()
            host._background_auto_portrait = True
            screen = host.screen() or QApplication.primaryScreen()
            available = screen.availableGeometry() if screen else host.geometry()
            width = min(max(520, int(max(1, host.width()) * .88)),
                         max(520, available.width() - 48))
            height = min(max(host.height() + 120, int(width * 1.28)),
                         max(600, available.height() - 32))
            host.resize(width, height)
        elif not tall and host._background_auto_portrait:
            host._background_auto_portrait = False
            previous = host._background_previous_size
            if previous.isValid() and previous.width() > 0 and previous.height() > 0:
                host.resize(previous)

    def refresh():
        mode = mode_getter() if callable(mode_getter) else store.mode()
        config = store.page_config(page) if mode == 'custom' else None
        adjust_portrait(config)
        store.activate_page(page)
        if not config:
            layer.clear_background()
            return
        pixmap = store.cached_preview(page, load_preview)
        if pixmap is None:
            layer.clear_background()
            return
        layer.set_background(pixmap, config.get('overlay', DEFAULT_OVERLAY), config.get('aspect', 1.0))

    filter_obj = _BackgroundHostFilter(host, layer, refresh)
    host.installEventFilter(filter_obj)
    host._background_layer = layer
    host._background_filter = filter_obj
    host.refresh_background = refresh
    refresh()
    return layer


class BackgroundEditorDialog(QDialog):
    """Choose a source image and save a bounded crop preview."""

    def __init__(self, parent, store, page='workspace', *, target_aspect=1.25,
                 initial_source=''):
        super().__init__(parent)
        self.store = store
        self.page = str(page)
        self.target_aspect = target_aspect
        self.source_path = None
        self.source_image = QImage()
        self.result_config = None
        self.setWindowTitle('编辑背景图')
        self.resize(900, 720)
        self.setMinimumSize(640, 520)
        layout = QVBoxLayout(self)
        self.path_label = QLabel('请选择 JPG、PNG、JPEG 或 WEBP 图片（最大 20 MB）')
        self.path_label.setWordWrap(True)
        layout.addWidget(self.path_label)
        self.view = CropView(self)
        self.view.setMinimumSize(560, 390)
        layout.addWidget(self.view, 1)
        self.coordinates = QLabel('拖动裁剪框，右下角手柄可调整大小；滚轮缩放，空白处拖动平移。')
        self.coordinates.setWordWrap(True)
        layout.addWidget(self.coordinates)
        buttons = QHBoxLayout()
        self.open_button = QPushButton('选择图片…')
        self.reset_button = QPushButton('重置视图')
        self.cancel_button = QPushButton('取消')
        self.save_button = QPushButton('保存此裁剪')
        buttons.addWidget(self.open_button)
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)
        buttons.addWidget(self.cancel_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.open_button.clicked.connect(self.choose_source)
        self.reset_button.clicked.connect(self.reset_view)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self.save_selection)
        self.view.cropChanged.connect(self.update_coordinates)
        self.save_button.setEnabled(False)
        if initial_source:
            try:
                self.load_source(initial_source)
            except BackgroundError:
                pass

    def choose_source(self):
        path, _ = QFileDialog.getOpenFileName(
            self, '选择背景图片', str(Path.home()),
            'Images (*.jpg *.jpeg *.png *.webp)')
        if path:
            try:
                self.load_source(path)
            except BackgroundError as error:
                QMessageBox.warning(self, '无法使用这张图片', str(error))

    def load_source(self, path):
        resolved, image = _read_source_image(path)
        self.source_path = resolved
        self.source_image = image
        self.path_label.setText(f'{resolved.name} · {image.width()} × {image.height()}')
        self.view.set_image(image, self.target_aspect)
        self.save_button.setEnabled(True)
        self.update_coordinates()

    def reset_view(self):
        if not self.source_image.isNull():
            self.view.set_image(self.source_image, self.target_aspect)

    def update_coordinates(self):
        crop = self.view.normalized_crop()
        self.coordinates.setText(
            f"裁剪起点 {crop['x']:.3f}, {crop['y']:.3f} · "
            f"范围 {crop['width']:.3f} × {crop['height']:.3f}")

    def save_selection(self):
        if self.source_path is None or self.source_image.isNull():
            return
        try:
            crop = self.view.normalized_crop()
            rect = self.view.crop_item.rect().toAlignedRect().intersected(
                self.source_image.rect())
            cropped = self.source_image.copy(rect)
            preview, preview_size = _encode_preview(cropped)
            config = self.store.save_page(
                self.page, source=self.source_path, crop=crop,
                preview_bytes=preview,
                source_size=(self.source_image.width(), self.source_image.height()),
                preview_size=preview_size,
                aspect=preview_size[0] / max(1, preview_size[1]))
        except (BackgroundError, OSError, ValueError) as error:
            QMessageBox.warning(self, '无法保存背景', str(error))
            return
        self.result_config = config
        self.accept()


class BackgroundSettingsDialog(QDialog):
    """Manage one background per interface without loading source images."""

    changed = Signal()

    def __init__(self, parent, store, *, language='简体中文', reward_available=False,
                 current_page='workspace'):
        super().__init__(parent)
        self.store = store
        self.language = language
        self.reward_available = bool(reward_available)
        self.page_names = {
            'workspace': '下载工作台', 'calendar': '签到日历',
            'schedules': '下载日程', 'activation': '邀请码窗口',
            'picker': '图集选择窗口', 'close_choice': '退出提示',
            'resume_choice': '续传提示', 'scan': '扫描窗口',
        }
        self.setWindowTitle('背景与界面主题')
        self.resize(620, 560)
        self.setMinimumSize(500, 460)
        layout = QVBoxLayout(self)
        intro = QLabel('每个界面可以使用不同的背景。原图只在裁剪时读取，运行时使用受限尺寸的缩略图。')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.page_combo = QComboBox()
        for page in BACKGROUND_PAGES_FOR_SETTINGS:
            self.page_combo.addItem(self.page_names.get(page, page), page)
        index = self.page_combo.findData(current_page)
        if index >= 0:
            self.page_combo.setCurrentIndex(index)
        form.addRow('编辑界面', self.page_combo)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem('默认主题', 'default')
        self.mode_combo.addItem('自定义背景', 'custom')
        self.mode_combo.addItem('签到奖励主题', 'reward')
        if not self.reward_available:
            reward_index = self.mode_combo.findData('reward')
            self.mode_combo.model().item(reward_index).setEnabled(False)
        form.addRow('当前来源', self.mode_combo)
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(15, 85)
        self.opacity.setSingleStep(1)
        self.opacity_label = QLabel()
        opacity_row = QHBoxLayout()
        opacity_row.addWidget(self.opacity, 1)
        opacity_row.addWidget(self.opacity_label)
        form.addRow('遮罩深度', opacity_row)
        layout.addLayout(form)
        self.preview = QLabel('当前界面尚未设置自定义背景')
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(170)
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview, 1)
        buttons = QHBoxLayout()
        self.edit_button = QPushButton('选择或编辑背景…')
        self.remove_button = QPushButton('清除当前背景')
        close = QPushButton('完成')
        buttons.addWidget(self.edit_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.page_combo.currentIndexChanged.connect(self.refresh)
        self.mode_combo.currentIndexChanged.connect(self.change_mode)
        self.opacity.valueChanged.connect(self.change_opacity)
        self.edit_button.clicked.connect(self.edit_current)
        self.remove_button.clicked.connect(self.remove_current)
        close.clicked.connect(self.accept)
        self.refresh()

    @property
    def current_page(self):
        return self.page_combo.currentData()

    def refresh(self, *_):
        config = self.store.page_config(self.current_page)
        mode = self.store.mode()
        self.mode_combo.blockSignals(True)
        index = self.mode_combo.findData(mode)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)
        self.mode_combo.blockSignals(False)
        self.opacity.blockSignals(True)
        self.opacity.setValue(normalize_overlay(config.get('overlay', DEFAULT_OVERLAY) if config else DEFAULT_OVERLAY))
        self.opacity.blockSignals(False)
        self.opacity_label.setText(f'{self.opacity.value()}%')
        self.remove_button.setEnabled(config is not None)
        if config:
            path = self.store.preview_path(self.current_page)
            pixmap = load_preview(path) if path else None
            if pixmap:
                self.preview.setPixmap(pixmap.scaled(360, 180, Qt.AspectRatioMode.KeepAspectRatio,
                                                     Qt.TransformationMode.SmoothTransformation))
            else:
                self.preview.setText('当前缩略图不可用，将在下次编辑时重建。')
        else:
            self.preview.setPixmap(QPixmap())
            self.preview.setText('当前界面尚未设置自定义背景')

    def change_mode(self, *_):
        mode = self.mode_combo.currentData()
        if mode == 'reward' and not self.reward_available:
            return
        try:
            self.store.set_mode(mode)
        except BackgroundError as error:
            QMessageBox.warning(self, '无法切换主题', str(error))
            self.refresh()
            return
        self.changed.emit()
        self.refresh()

    def change_opacity(self, value):
        self.opacity_label.setText(f'{int(value)}%')
        try:
            self.store.set_overlay(self.current_page, value)
        except BackgroundError:
            return
        self.changed.emit()

    def edit_current(self):
        config = self.store.page_config(self.current_page)
        initial = config.get('source', '') if config else ''
        parent = self.parentWidget()
        width = max(1, parent.width()) if parent else 1180
        height = max(1, parent.height()) if parent else 941
        editor = BackgroundEditorDialog(
            self, self.store, self.current_page,
            target_aspect=width / height, initial_source=initial)
        try:
            if editor.exec() == QDialog.DialogCode.Accepted:
                self.store.set_mode('custom')
                self.changed.emit()
                self.refresh()
        finally:
            editor.deleteLater()

    def remove_current(self):
        if not self.store.remove_page(self.current_page):
            return
        if not self.store.has_custom():
            self.store.set_mode('default')
        self.changed.emit()
        self.refresh()


# The crop editor itself is intentionally not a background host.  These are
# the interfaces exposed by the settings dialog; transient modal prompts can
# still be configured without making the editor recursively preview itself.
BACKGROUND_PAGES_FOR_SETTINGS = (
    'workspace', 'calendar', 'schedules', 'activation', 'picker',
    'close_choice', 'resume_choice', 'scan')


