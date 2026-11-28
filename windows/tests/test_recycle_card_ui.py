"""Recycle cards reuse the live picker, bounded thumbnails and safe batch actions."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import sys,tempfile,time,unittest,sqlite3
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication,QEvent
from PySide6.QtGui import QColor,QImage
from PySide6.QtWidgets import QApplication,QCheckBox,QWidget
from post_picker import CoverCard
from recycle_bin import RecycleBin
from recycle_ui import RecycleBinDialog

class RecycleCardUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_recycle_ui_')
        self.root=Path(self.temp.name);self.windows=[]
        self.bin=RecycleBin(self.root/'state')
        self.host=QWidget();self.host.lang='简体中文';self.host._dark=True
        self.host.palette_tokens=dict(card='#171e29',panel='#141b25',text='#e9edf6',muted='#99a6bb',
            line='#2a3445',accent='#9cb3ff',button='#a7bcff',button_text='#14203c',hover='#222f43',glow='#9cb3ff')
        self.host.title_check=QCheckBox(self.host)
        self.host.recycle_bin=self.bin;self.host.busy=False
        self.host.resize(1180,860);self.host.show()
        self.restored=[];self.removed=[]
        self.host.restore_recycle_entries=lambda ids:self.restored.append(list(ids)) or True
        self.host.remove_recycle_entry=self.remove
    def tearDown(self):
        for dialog in self.windows:
            dialog.close();dialog.deleteLater()
        self.host.close();self.host.deleteLater()
        self.qt.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.bin.close();self.temp.cleanup()
    def remove(self,identifier):
        self.removed.append(identifier)
        return bool(self.bin.remove(identifier))
    def entry(self,index=0,cover=None):
        return self.bin.add(title=f'图集 {index}',profile_url='https://pawchive.pw/fanbox/user/1',
            post_url=f'https://pawchive.pw/fanbox/user/1/post/{index}',
            download_url=f'https://pawchive.pw/fanbox/user/1/post/{index}',cover_path=cover,
            metadata={'formats':['.zip'], 'huge_ignored':'x'*20000})
    def dialog(self):
        dialog=RecycleBinDialog(self.host);self.windows.append(dialog)
        dialog.show();self.qt.processEvents();return dialog
    def wait_until(self,condition,timeout=3):
        deadline=time.monotonic()+timeout
        while not condition() and time.monotonic()<deadline:
            self.qt.processEvents();time.sleep(.002)
        self.assertTrue(condition())
    def test_cards_are_shared_large_picker_type_with_status_and_no_download_mode(self):
        image=QImage(2400,1800,QImage.Format.Format_RGB32);image.fill(QColor('#a752c8'))
        cover=self.root/'big.png';self.assertTrue(image.save(str(cover)))
        entry=self.entry(1,str(cover));dialog=self.dialog()
        card=dialog.cards[0]
        self.assertIsInstance(card,CoverCard)
        self.assertEqual(card.cover.height(),152)
        self.assertEqual(card.formats.text(),'已取消')
        self.assertTrue(card.content_choice.isHidden())
        self.assertFalse(card.check.isChecked());self.assertFalse(dialog.confirm_button.isEnabled())
        self.assertNotIn('huge_ignored',dialog.titles[0]);self.assertNotIn('metadata',dialog.titles[0])
        self.assertFalse(dialog.grab().isNull())
        self.assertTrue(dialog._cover_cache)
        self.assertTrue(all(pix.width()<=420 and pix.height()<=300 for pix in dialog._cover_cache.values()))
        dialog.close();self.qt.processEvents();self.assertFalse(dialog._cover_cache)
        self.assertIsNotNone(self.bin.get(entry.id))
    def test_select_all_includes_pending_and_filtered_cards_and_batch_restore_is_single_call(self):
        entries=[self.entry(index) for index in range(70)]
        dialog=RecycleBinDialog(self.host);self.windows.append(dialog)
        self.assertTrue(any(card is None for card in dialog.cards))
        dialog.search.setText('图集 69');dialog.all_button.click()
        self.assertEqual(len(dialog.selected_ids()),70)
        self.wait_until(lambda:all(card is not None for card in dialog.cards))
        self.assertTrue(all(card.check.isChecked() for card in dialog.cards))
        dialog.confirm_button.click()
        self.assertEqual(len(self.restored),1)
        self.assertEqual(set(self.restored[0]),{entry.id for entry in entries})
        self.assertEqual(len(self.bin.list_entries()),70)
    def test_select_none_and_successful_delete_refresh_cards_and_cache(self):
        entries=[self.entry(index) for index in range(3)]
        dialog=self.dialog();dialog.all_button.click();dialog.none_button.click()
        self.assertFalse(dialog.selected);self.assertFalse(dialog.delete_button.isEnabled())
        dialog.all_button.click();dialog.delete_button.click();self.qt.processEvents()
        self.assertEqual(set(self.removed),{entry.id for entry in entries})
        self.assertFalse(self.bin.list_entries());self.assertFalse(dialog.cards)
        self.assertFalse(dialog.selected);self.assertTrue(dialog.empty.isVisible())
        self.assertFalse(dialog.confirm_button.isEnabled());self.assertFalse(dialog.delete_button.isEnabled())
    def test_failed_restore_and_failed_removal_retain_authoritative_records(self):
        entry=self.entry(1);dialog=self.dialog();dialog.all_button.click()
        self.host.restore_recycle_entries=lambda _:False
        dialog.confirm_button.click();self.assertTrue(dialog.isVisible())
        self.assertIsNotNone(self.bin.get(entry.id));self.assertIn('仍保留',dialog.notice.text())
        self.host.remove_recycle_entry=lambda _:False
        dialog.delete_button.click();self.qt.processEvents()
        self.assertEqual(len(dialog.cards),1);self.assertIsNotNone(self.bin.get(entry.id))
        self.assertIn('未能删除',dialog.notice.text());self.assertFalse(dialog.selected)
    def test_restore_reentry_guard_and_active_download_do_not_dispatch(self):
        self.entry(1);dialog=self.dialog();dialog.all_button.click()
        self.host.busy=True;dialog.restore_selected();self.assertFalse(self.restored)
        self.host.busy=False
        calls=[]
        def reentrant(ids):
            calls.append(ids);dialog.restore_selected();return False
        self.host.restore_recycle_entries=reentrant;dialog.restore_selected()
        self.assertEqual(len(calls),1);self.assertTrue(dialog.confirm_button.isEnabled())
    def test_all_languages_have_native_action_copy(self):
        self.entry(1)
        for lang,restore,cancelled in [('简体中文','恢复选中','已取消'),('繁體中文','恢復選取','已取消'),
            ('English','Restore selected','Cancelled'),('日本語','選択を復元','キャンセル済み')]:
            self.host.lang=lang;dialog=self.dialog()
            self.assertEqual(dialog.confirm_button.text(),restore)
            self.assertEqual(dialog.cards[0].formats.text(),cancelled)
            dialog.close()
    def test_database_read_failure_shows_a_safe_empty_state(self):
        class UnavailableBin:
            def list_entries(self):raise sqlite3.OperationalError('database locked')
        self.host.recycle_bin=UnavailableBin()
        dialog=self.dialog()
        self.assertFalse(dialog.cards);self.assertIn('无法读取',dialog.notice.text())
        self.assertFalse(dialog.confirm_button.isEnabled())

if __name__=='__main__':unittest.main()
