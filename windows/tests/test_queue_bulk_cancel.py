"""Cancellation ownership and queue bulk controls, without live HTTP or user data."""
import os,sys,tempfile,time,weakref
from pathlib import Path
import unittest
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QCoreApplication,QEvent,QObject,QSettings,QThread,Signal,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid
from download_scheduler import DownloadScheduler
from desktop_design import DownloadQueueWidget,QueueItemWidget
import download_pawchive_images as desktop

class CancelSchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])
    def setUp(self):
        self.scheduler=DownloadScheduler(lambda _:None)
        self.scheduler.AUTO_RETRY_LIMIT=0
    def tearDown(self):
        self.scheduler.stop()
        for worker in list(self.scheduler._workers.values()):
            if isinstance(worker,QThread):self.assertTrue(worker.wait(3000))
        self.qt.processEvents();self.scheduler.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
    def add(self,**extra):
        return self.scheduler.add_task(dict(id='same',url='post-1',title='Album',formats=['.png'],metadata={'manifest':[b'x'*4096]},**extra))
    def test_recycle_ack_releases_heavy_manifest_but_failure_keeps_retryable_task(self):
        task=self.add();recycled=[]
        self.scheduler.task_recycled.connect(recycled.append)
        self.assertTrue(self.scheduler.cancel(task.id))
        self.assertIs(self.scheduler.task(task.id),task)
        self.assertIn('manifest',task.metadata)
        self.assertTrue(self.scheduler.cancel(task.id));self.assertEqual(len(recycled),2)
        self.assertTrue(self.scheduler.remove_cancelled(task.id))
        self.assertIsNone(self.scheduler.task(task.id));self.assertEqual(task.metadata,{})
        self.assertEqual(self.scheduler.tasks,())
    def test_cancelled_native_task_is_recycled_after_native_finished_only(self):
        class Native(QThread):
            failed=Signal(str)
            def run(self):
                while not self.isInterruptionRequested():self.msleep(1)
                self.failed.emit('interrupted');self.msleep(40)
        self.scheduler.worker_factory=lambda _:Native()
        task=self.add();recycled=[]
        def ack(snapshot):
            self.assertNotIn(snapshot.id,self.scheduler._workers)
            self.assertFalse(task.worker)
            recycled.append(snapshot.id)
            self.assertTrue(self.scheduler.remove_cancelled(snapshot.id))
        self.scheduler.task_recycled.connect(ack)
        self.scheduler.start();worker=self.scheduler._workers[task.id]
        self.assertFalse(self.scheduler.remove_cancelled(task.id))
        self.scheduler.cancel(task.id)
        deadline=time.monotonic()+3
        saw_terminal_while_native=False
        while self.scheduler.task(task.id) is not None and time.monotonic()<deadline:
            self.qt.processEvents()
            if task.state=='cancelled' and task.id in self.scheduler._workers:
                saw_terminal_while_native=True;self.assertFalse(recycled)
            time.sleep(.001)
        self.assertTrue(saw_terminal_while_native);self.assertEqual(recycled,[task.id])
        self.assertEqual(self.scheduler.tasks,())
    def test_completed_terminal_signal_before_finished_can_still_be_cancelled(self):
        class Worker(QObject):
            finished_ok=Signal(int);finished=Signal()
            def start(self):pass
        self.scheduler.worker_factory=lambda _:Worker()
        task=self.add();self.scheduler.start();worker=self.scheduler._workers[task.id]
        worker.finished_ok.emit(2)
        events=[];self.scheduler.task_recycled.connect(lambda snapshot:events.append(snapshot.id))
        self.assertTrue(self.scheduler.cancel(task.id));self.assertFalse(events)
        worker.finished.emit();self.assertEqual(events,[task.id])
        self.assertTrue(self.scheduler.remove_cancelled(task.id))
    def test_bulk_cancel_does_not_start_deleted_waiters_between_cancel_calls(self):
        started=[]
        class Worker(QObject):
            failed=Signal(str)
            def __init__(self,id):super().__init__();self.id=id
            def start(self):started.append(self.id)
            def requestInterruption(self):self.failed.emit('stop')
        self.scheduler.MAX_IMAGE_WORKERS=1
        self.scheduler.worker_factory=lambda task:Worker(task.id)
        tasks=self.scheduler.add_tasks(dict(id=str(i),url=str(i),formats=['.png']) for i in range(5))
        self.scheduler.task_recycled.connect(lambda task:self.scheduler.remove_cancelled(task.id))
        self.scheduler.start();self.scheduler.cancel_many([task.id for task in tasks])
        self.assertEqual(started,['0']);self.assertFalse(self.scheduler.tasks)
    def test_removed_id_reuse_cannot_receive_old_worker_signals(self):
        class Worker(QObject):
            failed=Signal(str);finished=Signal()
            def start(self):pass
            def requestInterruption(self):pass
        self.scheduler.worker_factory=lambda _:Worker()
        first=self.add();self.scheduler.start();old=self.scheduler._workers[first.id]
        self.scheduler.cancel(first.id);old.failed.emit('interrupted');old.finished.emit()
        self.assertTrue(self.scheduler.remove_cancelled(first.id))
        newer=self.add();self.assertEqual(newer.id,first.id)
        old.failed.emit('late');self.assertEqual(newer.state,'running')

class QueueBulkUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.qt=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pawchive_bulk_queue_test_')
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('Offline UI test'));self.network.start()
        self.window=desktop.App(QSettings(str(Path(self.temp.name)/'settings.ini'),QSettings.Format.IniFormat))
        self.window.resize(1180,940);self.window.show();self.qt.processEvents()
        self.queue=self.window.download_queue
    def tearDown(self):
        self.window.close();self.window.deleteLater();self.qt.processEvents()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        self.network.stop();self.temp.cleanup()
    def fill(self,count=3):
        self.queue.set_items([dict(id=str(i),title=f'Album {i}',state='queued') for i in range(count)])
        deadline=time.monotonic()+4
        while len(self.queue._rows)<count and time.monotonic()<deadline:self.qt.processEvents();time.sleep(.002)
        self.assertEqual(len(self.queue._rows),count)
    def test_bulk_checkbox_actions_are_hidden_by_default_and_cancel_resets(self):
        self.fill()
        self.assertEqual(self.window.queue_cancel_selected_button.text(),'批量选择')
        self.assertTrue(self.window.queue_delete_button.isHidden())
        self.assertTrue(all(row.selection_check.isHidden() for row in self.queue._rows.values()))
        self.window.queue_cancel_selected_button.click();self.qt.processEvents()
        self.assertEqual(self.window.queue_cancel_selected_button.text(),'取消选中')
        self.assertFalse(self.window.queue_delete_button.isHidden())
        self.assertTrue(all(isinstance(row.selection_check,desktop.GlowCheckBox) for row in self.queue._rows.values()))
        self.queue._rows['0'].selection_check.click();self.queue._rows['2'].selection_check.click()
        self.assertEqual(self.window._queue_selected_ids,['0','2'])
        self.window.queue_cancel_selected_button.click();self.qt.processEvents()
        self.assertFalse(self.queue.bulk_mode);self.assertFalse(self.window._queue_selected_ids)
        self.assertTrue(all(row.selection_check.isHidden() for row in self.queue._rows.values()))
    def test_remove_click_sends_selection_then_exits_mode_and_restores_toolbar(self):
        self.fill();self.window.queue_cancel_selected_button.click()
        self.queue._rows['1'].selection_check.click();calls=[]
        with patch.object(self.window,'handle_queue_selection_action',side_effect=lambda action:calls.append((action,list(self.window._queue_selected_ids)))):
            self.window.queue_delete_button.click()
        self.qt.processEvents();self.assertEqual(calls,[('cancel',['1'])])
        self.assertFalse(self.queue.bulk_mode);self.assertTrue(self.window.queue_delete_button.isHidden())
        self.assertEqual(self.window.queue_cancel_selected_button.text(),'批量选择')
    def test_deferred_rows_inherit_bulk_mode_and_cannot_reappear_after_removal(self):
        self.queue.set_bulk_mode(True)
        self.queue.set_items([dict(id=str(i),title=str(i)) for i in range(100)])
        self.assertTrue(self.queue._pending_items)
        self.assertTrue(self.queue.remove_item('99'))
        deadline=time.monotonic()+4
        while self.queue._row_timer.isActive() and time.monotonic()<deadline:self.qt.processEvents();time.sleep(.002)
        self.assertNotIn('99',self.queue._rows)
        self.assertTrue(all(not row.selection_check.isHidden() for row in self.queue._rows.values()))
        self.queue.clear_items();self.assertFalse(self.queue.bulk_mode)
        self.assertFalse(self.queue._pending_items)
    def test_checkbox_and_ctrl_rubber_selection_keep_priority_controls_working(self):
        self.fill();self.queue._row_selection_changed('1',False);self.queue._row_selection_changed('2',True)
        self.assertEqual(self.window._queue_selected_ids,['1','2'])
        self.assertTrue(self.queue.move_selected_to_top());self.assertEqual(self.queue.item_ids,['1','2','0'])
        self.window.queue_cancel_selected_button.click();self.assertFalse(self.window._queue_selected_ids)
    def test_toolbar_buttons_do_not_overlap_in_all_languages(self):
        self.fill();self.window.queue_cancel_selected_button.click()
        for language in ('简体中文','繁體中文','English','日本語'):
            self.window.set_lang(language);self.qt.processEvents()
            buttons=[self.window.recycle_button,self.window.queue_top_button,self.window.queue_delete_button,self.window.queue_cancel_selected_button]
            rects=[button.geometry() for button in buttons]
            for i,rect in enumerate(rects):
                self.assertTrue(rect.width()>=buttons[i].minimumSizeHint().width())
                self.assertTrue(all(not rect.intersects(other) for other in rects[i+1:]))
            self.assertNotEqual(self.window.queue_cancel_selected_button.text(),'')

if __name__=='__main__':unittest.main()
