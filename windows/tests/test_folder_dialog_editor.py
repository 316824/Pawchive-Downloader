"""Keep QFileDialog's inline rename editor readable inside a compact row."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication,QEvent,QSettings,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication,QFileDialog,QLineEdit,QTreeView,QListView,
                               QStyle,QStyleOptionFrame)
from app_state import AppState
from desktop_runtime import DesktopRuntime
from download_journal import DownloadJournal
import download_pawchive_images as desktop


class NoTasks:
    def install(self,*_):raise AssertionError('No scheduled tasks in folder tests')
    def remove(self,*_):raise AssertionError('No scheduled tasks in folder tests')


class FolderDialogEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='pawchive_folder_editor_')
        self.root=Path(self.temporary.name)
        self.state=AppState(self.root/'state')
        self.runtime=DesktopRuntime(self.qt,self.state,DownloadJournal(self.state.data_dir),NoTasks(),tray_enabled=False)
        self.no_feed=patch.object(desktop.App,'start_subscription_check',return_value=None)
        self.no_feed.start()
        self.no_network=patch('requests.sessions.Session.request',side_effect=AssertionError('No network in folder tests'))
        self.network=self.no_network.start()
        self.window=desktop.App(QSettings(str(self.root/'settings.ini'),QSettings.Format.IniFormat),services=self.runtime)
        self.runtime.attach(self.window);self.runtime.timer.stop();self.dialogs=[]

    def tearDown(self):
        for dialog in self.dialogs:dialog.reject();dialog.deleteLater()
        self.runtime.prepare_exit();self.window.close();self.window.deleteLater();self.runtime.close()
        self.qt.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.no_feed.stop();self.network.assert_not_called();self.no_network.stop();self.temporary.cleanup()

    def wait_for(self,condition,timeout=3):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            self.qt.processEvents()
            if condition():return True
            time.sleep(.005)
        return False

    def _edit_folder(self,mode,dark):
        self.window.apply_theme(dark)
        directory=self.root/f'folders_{mode.name}_{dark}';directory.mkdir()
        folder=directory/'原来的文件夹';folder.mkdir()
        dialog=QFileDialog(self.window);self.dialogs.append(dialog)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog,True)
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setViewMode(mode);dialog.setDirectory(str(directory));dialog.show()
        view=dialog.findChild(QTreeView,'treeView') if mode==QFileDialog.ViewMode.Detail else dialog.findChild(QListView,'listView')
        self.assertIsNotNone(view)
        model=view.model()
        self.assertTrue(self.wait_for(lambda:model.index(str(folder)).isValid()))
        index=model.index(str(folder));view.setCurrentIndex(index);view.scrollTo(index)
        self.assertTrue(self.wait_for(lambda:view.visualRect(index).height()>0))
        view.edit(index)
        self.assertTrue(self.wait_for(lambda:bool(view.findChildren(QLineEdit))))
        editor=next(child for child in view.findChildren(QLineEdit) if child.isVisible())
        editor.setText('中文 Folder 重命名')
        option=QStyleOptionFrame();editor.initStyleOption(option)
        content=editor.style().subElementRect(QStyle.SubElement.SE_LineEditContents,option,editor)
        self.assertGreaterEqual(content.height(),editor.fontMetrics().height(),
            f'Inline text is clipped: content {content}, editor {editor.geometry()}')
        self.assertGreater(content.width(),30)
        # The ordinary location input keeps the spacious application style.
        location=dialog.findChild(QLineEdit,'fileNameEdit')
        self.assertIsNotNone(location)
        self.assertGreater(location.sizeHint().height(),editor.sizeHint().height())
        QTest.keyClick(editor,Qt.Key.Key_Return)
        self.assertTrue(self.wait_for(lambda:(directory/'中文 Folder 重命名').is_dir()))
        self.assertFalse(folder.exists())
        dialog.reject()

    def test_folder_rename_editor_fits_text_in_detail_and_list_dark_and_light(self):
        for mode in (QFileDialog.ViewMode.Detail,QFileDialog.ViewMode.List):
            for dark in (True,False):
                with self.subTest(mode=mode.name,dark=dark):self._edit_folder(mode,dark)


if __name__=='__main__':unittest.main()
