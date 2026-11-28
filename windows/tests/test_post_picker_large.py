"""Large cover selections paint incrementally and survive first-show relayouts."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QColor, QImage, QResizeEvent
from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QWidget
from post_picker import PostPicker


class LargePostPickerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pawchive_large_picker_')
        self.parent = QWidget()
        self.parent.lang = '简体中文'; self.parent._dark = True
        self.parent.background_store = None
        self.parent.palette_tokens = dict(panel='#121722', card='#171d29', line='#303849',
            text='#edf3ff', muted='#8995b0', accent='#a5b3fa', button='#a5b3fa',
            button_text='#11151c', hover='#272d41')
        self.parent.content_combo = QComboBox(self.parent)
        self.parent.content_combo.addItem('images', 'images')
        self.image_path = str(Path(self.temp.name) / 'cached-cover.jpg')
        image = QImage(64, 64, QImage.Format.Format_RGB32)
        image.fill(QColor('#27c391')); self.assertTrue(image.save(self.image_path))
        self.dialogs = []
        self.no_network = patch('requests.sessions.Session.request',
            side_effect=AssertionError('Cover UI must use already cached images'))
        self.request = self.no_network.start()

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.reject(); dialog.deleteLater()
        self.parent.deleteLater(); self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.request.assert_not_called(); self.no_network.stop(); self.temp.cleanup()

    def picker(self, count=701):
        entries = [dict(title=f'Collection {index:03}',
            url=f'https://pawchive.pw/fanbox/user/1/post/{index}',
            image=self.image_path, formats=['.jpg']) for index in range(count)]
        text = dict(cover_missing='Cover unavailable', no_direct='No attachments',
            choose_title='Choose collections', select_all='Select all',
            select_none='Select none', cancel='Cancel', back_top='Back to top')
        dialog = PostPicker(self.parent, entries, QCheckBox, text)
        self.dialogs.append(dialog)
        return dialog

    def pump(self, condition=lambda: False, timeout=5):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            self.qt.processEvents()
            if condition(): return True
            time.sleep(.002)
        return condition()

    def test_701_posts_show_first_cards_before_remaining_cards_are_built(self):
        dialog = self.picker()
        self.assertEqual(sum(card is not None for card in dialog.cards), dialog.CARD_BATCH_SIZE)
        self.assertEqual(len(dialog.selected), 701)
        dialog.show()
        self.assertTrue(self.pump(lambda: dialog.cards[0].isVisible(), timeout=1))
        card = dialog.cards[0]
        position = card.mapTo(dialog.scroll.viewport(), QPoint())
        self.assertTrue(dialog.scroll.viewport().rect().intersects(card.rect().translated(position)))
        image = card.cover.grab().toImage()
        pixel = image.pixelColor(image.width() // 2, image.height() // 2)
        self.assertGreater(pixel.green(), pixel.red() + 50)
        self.assertTrue(self.pump(lambda: all(card is not None for card in dialog.cards)))
        self.assertEqual(dialog.grid.count(), 701)
        self.assertGreater(dialog.scroll.verticalScrollBar().maximum(), 0)
        self.assertEqual(len(dialog._cover_cache), 1)
        dialog.scroll.ensureWidgetVisible(dialog.cards[-1])
        self.qt.processEvents()
        self.assertTrue(dialog.cards[-1].isVisible())
        self.assertEqual(dialog.scroll.horizontalScrollBar().maximum(), 0)

    def test_search_prioritizes_unbuilt_last_card_and_bulk_state_survives(self):
        dialog = self.picker()
        self.assertIsNone(dialog.cards[700])
        dialog._select_all(False)
        dialog.search.setText('Collection 700'); dialog.show()
        self.assertTrue(self.pump(lambda: dialog.cards[700] is not None and dialog.cards[700].isVisible()))
        self.assertFalse(dialog.cards[700].check.isChecked())
        self.assertEqual(dialog.cards[700].content_choice.currentData(), 'images')
        self.assertFalse(dialog.selected)
        dialog.cards[700].check.setChecked(True)
        self.assertEqual(dialog.selected, {700})
        dialog.search.setText('missing title')
        self.qt.processEvents()
        self.assertTrue(dialog.empty.isVisible())
        self.assertEqual(dialog.selected, {700})
        dialog.search.clear()
        self.assertTrue(self.pump(lambda: all(card is not None for card in dialog.cards)))
        self.assertEqual(dialog.selected, {700})
        self.assertTrue(dialog.cards[700].check.isChecked())

    def test_nested_resize_reflow_is_coalesced_and_grid_has_unique_positions(self):
        dialog = self.picker(48); dialog.show()
        self.assertTrue(self.pump(lambda: all(card is not None for card in dialog.cards)))
        original_add = dialog.grid.addWidget
        nested = []
        def reentrant_add(*args):
            result = original_add(*args)
            if not nested:
                nested.append(True)
                dialog._layout_cards(force=True)
            return result
        with patch.object(dialog.grid, 'addWidget', side_effect=reentrant_add):
            dialog.search.setText('Collection 00')
            self.qt.processEvents()
            dialog.search.clear()
            dialog.resize(540, 640)
            self.assertTrue(self.pump(lambda: not dialog._layout_timer.isActive()))
        self.assertTrue(nested)
        self.assertFalse(dialog._in_layout)
        self.assertTrue(dialog.body.updatesEnabled())
        self.assertEqual(dialog.grid.count(), 48)
        positions = [dialog.grid.getItemPosition(index)[:2] for index in range(dialog.grid.count())]
        self.assertEqual(len(positions), len(set(positions)))
        self.assertTrue(all(card.isVisible() for card in dialog.cards))

    def test_close_stops_pending_batches_and_missing_cover_keeps_a_visible_card(self):
        dialog = self.picker()
        self.assertTrue(dialog._build_timer.isActive())
        dialog.cards[0].cover.filename = str(Path(self.temp.name) / 'not-downloaded.jpg')
        dialog.show(); self.qt.processEvents()
        self.assertFalse(dialog.cards[0].cover.grab().isNull())
        self.assertTrue(dialog.cards[0].cover.failed)
        self.assertTrue(dialog.cards[0].title_label.isVisible())
        dialog.reject()
        count = sum(card is not None for card in dialog.cards)
        self.qt.processEvents()
        self.assertEqual(count, sum(card is not None for card in dialog.cards))
        self.assertFalse(dialog._build_timer.isActive())
        self.assertFalse(dialog._layout_timer.isActive())

    def test_unchanged_viewport_resize_does_not_reactivate_the_native_grid(self):
        dialog = self.picker(48); dialog.show()
        self.assertTrue(self.pump(lambda: all(card is not None for card in dialog.cards)))
        self.assertTrue(self.pump(lambda: not dialog._layout_timer.isActive()))
        viewport = dialog.scroll.viewport()
        with patch.object(dialog.grid, 'invalidate', wraps=dialog.grid.invalidate) as invalidate:
            for _ in range(60):
                QCoreApplication.sendEvent(viewport, QResizeEvent(viewport.size(), viewport.size()))
            self.qt.processEvents()
            self.assertEqual(invalidate.call_count, 0)
        self.assertFalse(dialog._layout_timer.isActive())
        self.assertEqual(dialog.grid.count(), 48)

    def test_closed_picker_does_not_restart_layout_from_late_resize_or_show(self):
        dialog = self.picker(80); dialog.show(); self.qt.processEvents()
        viewport = dialog.scroll.viewport()
        dialog.reject()
        QCoreApplication.sendEvent(viewport, QResizeEvent(viewport.size(), viewport.size()))
        dialog.show(); self.qt.processEvents()
        self.assertTrue(dialog._closed)
        self.assertFalse(dialog._layout_timer.isActive())
        self.assertFalse(dialog._build_timer.isActive())


if __name__ == '__main__':
    unittest.main()
