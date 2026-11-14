"""Pixel checks for live crop previews, without requesting remote content."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QWheelEvent
from PySide6.QtWidgets import QApplication, QWidget

from background_store import BackgroundStore
from background_ui import BackgroundEditorDialog, CropWindowPreview, _capture_window_controls


class BackgroundPreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dragging_crop_changes_visible_image_instead_of_black_fill(self):
        image = QImage(600, 300, QImage.Format.Format_RGB32)
        image.fill(QColor('#ff0000'))
        painter = QPainter(image)
        painter.fillRect(300, 0, 300, 300, QColor('#0000ff'))
        painter.end()
        preview = CropWindowPreview(1.0, overlay=15)
        preview.resize(320, 380)
        preview.set_source(image)
        for rect, expected in ((QRectF(0, 0, 300, 300), 'red'),
                               (QRectF(300, 0, 300, 300), 'blue')):
            preview.set_crop(rect)
            frame = preview.grab().toImage()
            point = preview.window_rect().center().toPoint()
            color = frame.pixelColor(point)
            if expected == 'red':
                self.assertGreater(color.red(), color.blue() + 150)
            else:
                self.assertGreater(color.blue(), color.red() + 150)
        preview.close()

    def test_crop_keeps_its_aspect_and_uses_runtime_letterbox(self):
        preview = CropWindowPreview(1.0, overlay=15)
        preview.resize(320, 380)
        image = QImage(600, 300, QImage.Format.Format_RGB32)
        image.fill(QColor('#ff0000'))
        preview.set_source(image)
        preview.set_crop(QRectF(0, 0, 600, 300))
        frame = preview.grab().toImage()
        rect = preview.window_rect()
        center = rect.center().toPoint()
        self.assertGreater(frame.pixelColor(center).red(), 150)
        # A 2:1 crop fitted in the square window leaves dark bars at the top.
        upper = frame.pixelColor(round(rect.center().x()), round(rect.top()+rect.height()*.1))
        self.assertLess(upper.red(), 25)
        preview.close()

    def test_control_capture_contains_actual_widgets_and_restores_old_background(self):
        host = QWidget()
        host.resize(500, 400)
        background = QWidget(host)
        background.setGeometry(host.rect())
        background.setStyleSheet('background:#ff0000')
        host._background_layer = background
        control = QWidget(host)
        control.setGeometry(20, 20, 80, 50)
        control.setStyleSheet('background:#00ff00')
        host.show()
        self.app.processEvents()
        snapshot = _capture_window_controls(host)
        self.assertGreater(snapshot.pixelColor(40, 40).green(), 240)
        self.assertEqual(snapshot.pixelColor(300, 300).alpha(), 0,
                         'The current background must not cover the new crop')
        self.assertTrue(background.isVisible(), 'Capturing must restore the live window')
        host.close()

    def test_large_source_is_bounded_without_changing_crop_coordinates(self):
        preview = CropWindowPreview(1.0, overlay=15)
        image = QImage(4000, 2000, QImage.Format.Format_RGB32)
        image.fill(QColor('#ff0000'))
        painter = QPainter(image)
        painter.fillRect(2000, 0, 2000, 2000, QColor('#0000ff'))
        painter.end()
        preview.set_source(image)
        self.assertLessEqual(max(preview.source_image.width(), preview.source_image.height()), 2048)
        preview.set_crop(QRectF(2000, 0, 2000, 2000))
        color = preview.grab().toImage().pixelColor(preview.window_rect().center().toPoint())
        self.assertGreater(color.blue(), color.red() + 150)
        preview.close()

    def _open_existing_background(self, root):
        image = QImage(3000, 2000, QImage.Format.Format_RGB32)
        image.fill(QColor('#ff9090'))
        path = Path(root) / 'source.png'
        self.assertTrue(image.save(str(path)))
        editor = BackgroundEditorDialog(None, BackgroundStore(Path(root) / 'state'),
                                        initial_source=str(path))
        self.addCleanup(editor.deleteLater)
        self.addCleanup(editor.close)
        editor.show()
        self._settle_layout()
        return editor

    def _settle_layout(self):
        for _ in range(6):
            self.app.processEvents()

    def _assert_image_fits_viewport(self, view):
        rect = view.mapFromScene(QRectF(view.scene_obj.sceneRect())).boundingRect()
        viewport = view.viewport().rect()
        self.assertGreater(max(rect.width() / viewport.width(),
                               rect.height() / viewport.height()), .9,
                           'The initial hidden viewport must not leave a tiny image')
        self.assertGreaterEqual(rect.left(), -3)
        self.assertGreaterEqual(rect.top(), -3)
        self.assertLessEqual(rect.right(), viewport.right() + 3)
        self.assertLessEqual(rect.bottom(), viewport.bottom() + 3)

    def test_existing_background_fits_after_first_native_show(self):
        with tempfile.TemporaryDirectory(prefix='pawchive-crop-fit-test-') as root:
            editor = self._open_existing_background(root)
            self._assert_image_fits_viewport(editor.view)
            editor.close()

    def test_editor_resize_refits_without_changing_crop(self):
        with tempfile.TemporaryDirectory(prefix='pawchive-crop-fit-test-') as root:
            editor = self._open_existing_background(root)
            initial_scale = editor.view.transform().m11()
            initial_crop = editor.view.normalized_crop()
            editor.resize(editor.width() + 240, editor.height() + 100)
            self._settle_layout()
            self._assert_image_fits_viewport(editor.view)
            self.assertGreater(editor.view.transform().m11(), initial_scale)
            self.assertEqual(editor.view.normalized_crop(), initial_crop)
            editor.close()

    def test_manual_wheel_zoom_is_not_reset_by_resize_or_show(self):
        with tempfile.TemporaryDirectory(prefix='pawchive-crop-fit-test-') as root:
            editor = self._open_existing_background(root)
            view = editor.view
            initial_scale = view.transform().m11()
            point = view.viewport().rect().center()
            event = QWheelEvent(QPointF(point), QPointF(view.viewport().mapToGlobal(point)),
                                QPoint(), QPoint(0, 120), Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase,
                                False)
            self.app.sendEvent(view.viewport(), event)
            zoomed_scale = view.transform().m11()
            self.assertGreater(zoomed_scale, initial_scale)
            editor.resize(editor.width() + 200, editor.height() + 80)
            self._settle_layout()
            editor.hide()
            editor.show()
            self._settle_layout()
            self.assertAlmostEqual(view.transform().m11(), zoomed_scale)
            editor.close()


if __name__ == '__main__':
    unittest.main()
