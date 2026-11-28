"""Window wakeups and modal entry points preserve the workspace's mode."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication,QDate,QEvent,QSettings,QTimer,Qt
from PySide6.QtWidgets import QApplication,QCalendarWidget,QDialog,QToolButton
from app_state import AppState
from desktop_runtime import DesktopRuntime
from download_journal import DownloadJournal
from feature_dialogs import CheckInDialog,ScheduleDialog
import download_pawchive_images as desktop


class _NoSystemTasks:
    def install(self,*args):raise AssertionError('No system tasks in window tests')
    remove=install


class RuntimeWindowModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt=QApplication.instance() or QApplication([])
        cls.qt.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive-runtime-mode-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.network=patch('requests.sessions.Session.request',
                           side_effect=AssertionError('Window regression must be offline'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.state=AppState(self.root/'state')
        self.runtime=DesktopRuntime(self.qt,self.state,DownloadJournal(self.state.data_dir),
                                    _NoSystemTasks(),tray_enabled=False)
        self.addCleanup(self.runtime.close)
        with patch.object(desktop.App,'start_subscription_check',return_value=None):
            self.window=desktop.App(QSettings(str(self.root/'settings.ini'),QSettings.Format.IniFormat),
                                    services=self.runtime)
        self.runtime.attach(self.window);self.runtime.timer.stop()
        self.window.show();self.qt.processEvents()

    def tearDown(self):
        self.runtime.prepare_exit();self.window.busy=False
        self.window.close();self.window.deleteLater();self.runtime.close()
        self.qt.processEvents();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.network.stop();self.temp.cleanup()

    def _expanded(self,mode):
        self.window.showNormal();self.qt.processEvents()
        getattr(self.window,'show'+mode)();self.qt.processEvents()
        expected=Qt.WindowState.WindowMaximized if mode=='Maximized' else Qt.WindowState.WindowFullScreen
        self.assertTrue(self.window.windowState() & expected)
        return expected

    def test_wake_visible_hidden_and_minimized_windows_keep_expanded_mode(self):
        for mode in ('Maximized','FullScreen'):
            expected=self._expanded(mode)
            for state in ('visible','hidden','minimized'):
                with self.subTest(mode=mode,state=state):
                    if state=='hidden':self.window.hide()
                    elif state=='minimized':self.window.showMinimized()
                    self.qt.processEvents()
                    self.runtime.show_window();self.qt.processEvents()
                    self.assertTrue(self.window.isVisible())
                    self.assertFalse(self.window.isMinimized())
                    self.assertTrue(self.window.windowState() & expected)
                    self.assertFalse(self.window.portrait_mode)

    def test_actual_schedule_and_calendar_modal_entry_preserve_expanded_workspace(self):
        for mode in ('Maximized','FullScreen'):
            expected=self._expanded(mode)
            for entry,dialog_type in ((self.runtime.show_schedules,ScheduleDialog),
                                      (self.runtime.show_calendar,CheckInDialog)):
                with self.subTest(mode=mode,entry=entry.__name__):
                    seen=[];errors=[]
                    def close_modal():
                        modal=self.qt.activeModalWidget()
                        try:
                            self.assertIsInstance(modal,dialog_type)
                            self.assertTrue(self.window.windowState() & expected)
                            seen.append(type(modal))
                        except BaseException as error:errors.append(error)
                        finally:
                            if isinstance(modal,QDialog):modal.reject()
                    QTimer.singleShot(25,close_modal)
                    entry();self.qt.processEvents()
                    self.assertFalse(errors,repr(errors));self.assertEqual(seen,[dialog_type])
                    self.assertTrue(self.window.windowState() & expected)
                    self.assertFalse(self.window.portrait_mode)

    def test_plain_minimized_window_restores_without_expansion(self):
        self.window.showNormal();self.window.showMinimized();self.qt.processEvents()
        self.runtime.show_window();self.qt.processEvents()
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window.isMinimized() or self.window.isMaximized() or self.window.isFullScreen())

    def test_calendar_buttons_and_programmatic_navigation_stay_in_supported_years(self):
        dialog=CheckInDialog(self.window,self.state)
        try:
            calendar=dialog.calendar;calendar.show()
            self.assertEqual(calendar.minimumDate(),QDate(2026,1,1))
            self.assertEqual(calendar.maximumDate(),QDate(2099,12,31))
            for year,month,direction in ((2026,1,-1),(2099,12,1)):
                calendar.setCurrentPage(year,month);self.qt.processEvents()
                name='qt_calendar_prevmonth' if direction<0 else 'qt_calendar_nextmonth'
                arrow=calendar.findChild(QToolButton,name)
                self.assertFalse(arrow.isEnabled());arrow.click()
                for action in ((calendar.showPreviousMonth,calendar.showPreviousYear,
                                lambda:calendar.setCurrentPage(2025,12),
                                lambda:QCalendarWidget.showPreviousMonth(calendar)) if direction<0 else
                               (calendar.showNextMonth,calendar.showNextYear,
                                lambda:calendar.setCurrentPage(2100,1),
                                lambda:QCalendarWidget.showNextMonth(calendar))):
                    action();self.qt.processEvents()
                    self.assertEqual((calendar.yearShown(),calendar.monthShown()),(year,month))
                    self.assertFalse(arrow.isEnabled())
            calendar.setCurrentPage(2026,2);calendar.showPreviousMonth()
            self.assertEqual((calendar.yearShown(),calendar.monthShown()),(2026,1))
            calendar.setCurrentPage(2099,11);calendar.showNextMonth()
            self.assertEqual((calendar.yearShown(),calendar.monthShown()),(2099,12))
        finally:dialog.close();dialog.deleteLater()

    def test_calendar_range_does_not_limit_scheduled_download_dates(self):
        dialog=ScheduleDialog(self.window,self.runtime)
        try:
            dialog.when.setDate(QDate(2100,1,2))
            self.assertEqual(dialog.when.date(),QDate(2100,1,2))
        finally:dialog.close();dialog.deleteLater()

    def test_mandatory_update_and_installation_block_background_schedule_and_resume(self):
        config=dict(profile='https://pawchive.pw/fanbox/user/1',folder=str(self.root/'saved'),
                    order='newest',by_title=True,rename_order=False,start_number=1)
        before=(self.window.profile.text(),self.window.path.text())
        for flag in ('_mandatory_update','_update_installing'):
            with self.subTest(flag=flag):
                setattr(self.window,flag,True)
                try:
                    with patch.object(self.state,'claim_next_schedule',
                         side_effect=AssertionError('Do not consume a scheduled plan during update')):
                        self.runtime.poll()
                    with patch.object(self.runtime.journal,'list_recoverable',
                         side_effect=AssertionError('Do not offer a resume behind update dialog')):
                        self.runtime.offer_resume()
                    self.assertFalse(self.runtime.run_snapshot(config))
                    self.assertEqual((self.window.profile.text(),self.window.path.text()),before)
                    self.assertIsNone(self.runtime.active_schedule)
                finally:setattr(self.window,flag,False)


if __name__=='__main__':unittest.main()
