"""Queue/lifecycle UI regressions with real widgets and signal-driven fake I/O."""
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QSettings, QTimer, Signal
from PySide6.QtGui import QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from app_state import AppState
from desktop_runtime import DesktopRuntime
from download_journal import DownloadJournal
from download_scheduler import DownloadScheduler
from queue_failure_ui import FailedQueueDialog
import download_pawchive_images as desktop


class FakeWorker(QObject):
    progress = Signal(str, object, object)
    file_progress = Signal(int, int)
    finished_ok = Signal(int)
    failed = Signal(str)
    finished = Signal()

    def __init__(self):
        super().__init__()
        self.running = False
        self.interrupted = False
        self.completed_records = []

    def start(self):
        self.running = True

    def isRunning(self):
        return self.running

    def requestInterruption(self):
        self.interrupted = True

    def fail(self, message='offline interrupted transfer'):
        self.failed.emit(message)
        self.running = False
        self.finished.emit()

    def succeed(self, count=1):
        self.finished_ok.emit(count)
        self.running = False
        self.finished.emit()


class QueueUiRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pawchive_queue_ui_test_')
        self.root = Path(self.temp.name)
        self.settings = QSettings(str(self.root / 'settings.ini'), QSettings.Format.IniFormat)
        self.network = patch('requests.sessions.Session.request',
                             side_effect=AssertionError('UI regression tests must not use the network'))
        self.request = self.network.start()
        self.window = desktop.App(settings=self.settings)
        self.window.resize(1180, 740)
        self.window.path.setText(str(self.root))
        self.window.show()
        self.dialogs = []
        self.workers = {}
        self.resources = []
        self.factory_patch = patch.object(self.window, '_queue_worker_factory', side_effect=self.make_worker)
        self.factory_patch.start()
        self.app.processEvents()

    def make_worker(self, task):
        worker = FakeWorker()
        self.workers.setdefault(task.id, []).append(worker)
        return worker

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        preparation = self.window._queue_preparation
        if preparation is not None and preparation.isRunning():
            self.assertTrue(preparation.wait(30000), 'queue preparation did not stop')
        # Drain pending batch callbacks before destroying their real QObject
        # owners, including when an assertion interrupted queue construction.
        self.window._queue_pending_entries = []
        QTest.qWait(30)
        scheduler = self.window.queue_scheduler
        if scheduler is not None:
            scheduler._started = False
            scheduler._closing = True
            scheduler._retry_timer.stop()
            for task_id, worker in list(scheduler._workers.items()):
                if worker is not None and worker.isRunning():
                    worker.fail('test teardown')
        self.window.services = None
        self.window.busy = False
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        for resource in self.resources:
            resource.close()
        self.factory_patch.stop()
        self.request.assert_not_called()
        self.network.stop()
        self.temp.cleanup()

    def wait_until(self, condition, timeout=2000):
        deadline = time.monotonic() + timeout / 1000
        while not condition() and time.monotonic() < deadline:
            # PySide's QTest wait can retain the GIL while servicing frequent
            # Qt callbacks. Explicitly yield Python too, as app.exec() does in
            # normal use, so the real preparation QThread can execute its run.
            time.sleep(.001)
            QTest.qWait(10)
        self.assertTrue(condition(), 'UI condition did not settle')

    def attach_queue(self, count=3):
        window = self.window
        scheduler = DownloadScheduler(window._queue_worker_factory, window)
        scheduler.AUTO_RETRY_LIMIT = 0
        window.queue_scheduler = scheduler
        scheduler.task_updated.connect(window._queue_task_updated)
        scheduler.task_finished.connect(window._queue_task_finished)
        scheduler.queue_changed.connect(window._queue_view)
        scheduler.became_idle.connect(window._queue_worker_idle)
        for number in range(count):
            scheduler.add_task(dict(id=f'item-{number}', title=f'Album {number}',
                url=f'https://pawchive.pw/fanbox/user/offline/post/{number}',
                formats=['.jpg'], file_count=10))
        window.set_busy_state(True)
        scheduler.start()
        self.wait_until(lambda: len(window.download_queue._rows) == count)
        return scheduler

    def failure_dialog(self):
        dialog = FailedQueueDialog(self.window)
        self.dialogs.append(dialog)
        dialog.show()
        self.app.processEvents()
        return dialog

    def test_failure_dialog_and_main_queue_share_retry_progress_and_completion(self):
        scheduler = self.attach_queue(3)
        self.workers['item-1'][-1].fail()
        dialog = self.failure_dialog()
        self.wait_until(lambda: 'item-1' in dialog.queue._rows)
        self.wait_until(lambda: self.window.download_queue._rows['item-1'].status.text() == '下载失败')
        main = self.window.download_queue._rows['item-1']
        failed = dialog.queue._rows['item-1']
        self.assertEqual('failed', main.property('queueState'))
        self.assertEqual(main.status.text(), failed.status.text())
        self.assertTrue(dialog.retry.isEnabled())
        QTest.mouseClick(failed.retry_button, desktop.Qt.MouseButton.LeftButton)
        retry = self.workers['item-1'][-1]
        self.assertEqual(2, len(self.workers['item-1']))
        retry.file_progress.emit(2, 10)
        retry.progress.emit('receiving image.jpg', 5, 10)
        self.wait_until(lambda: main.percent.text() == '25%' and failed.percent.text() == '25%')
        self.assertEqual(scheduler.tasks[0].id, 'item-1')
        retry.succeed(10)
        self.wait_until(lambda: 'item-1' not in dialog.queue._rows)
        self.wait_until(lambda: main.status.text() == '已完成')
        self.assertFalse(dialog.retry.isEnabled())

    def test_failed_status_uses_red_and_completion_uses_the_normal_accent(self):
        self.attach_queue(1)
        self.workers['item-0'][-1].fail()
        row = self.window.download_queue._rows['item-0']
        self.wait_until(lambda: row.property('queueState') == 'failed')
        self.assertEqual('#ef6378', row.status.palette().color(QPalette.ColorRole.WindowText).name())
        self.window.handle_queue_action('item-0', 'retry')
        self.workers['item-0'][-1].succeed(10)
        self.wait_until(lambda: row.status.text() == '已完成')
        self.assertNotEqual('#ef6378', row.status.palette().color(QPalette.ColorRole.WindowText).name())

    def test_failure_dialog_all_retry_uses_the_existing_tasks(self):
        scheduler = self.attach_queue(3)
        self.workers['item-0'][-1].fail()
        self.workers['item-1'][-1].fail()
        dialog = self.failure_dialog()
        self.wait_until(lambda: len(dialog.queue._rows) == 2)
        QTest.mouseClick(dialog.retry, desktop.Qt.MouseButton.LeftButton)
        self.assertEqual(3, len(scheduler.tasks))
        self.assertEqual(2, len(self.workers['item-0']))
        self.assertEqual(2, len(self.workers['item-1']))
        self.assertEqual(1, len(self.workers['item-2']))
        self.assertTrue(all(scheduler.task(key).state == 'running' for key in ('item-0', 'item-1')))

    def test_pause_button_switches_to_resume_and_restarts_the_task(self):
        scheduler = self.attach_queue(1)
        row = self.window.download_queue._rows['item-0']
        worker = self.workers['item-0'][-1]
        QTest.mouseClick(row.pause_button, desktop.Qt.MouseButton.LeftButton)
        self.assertTrue(worker.interrupted)
        self.assertEqual('pausing', scheduler.task('item-0').state)
        worker.fail('saved pause checkpoint')
        self.assertEqual('paused', scheduler.task('item-0').state)
        self.wait_until(lambda: row.pause_button.text() == '▶')
        QTest.mouseClick(row.pause_button, desktop.Qt.MouseButton.LeftButton)
        self.wait_until(lambda: scheduler.task('item-0').state == 'running')
        self.assertEqual(2, len(self.workers['item-0']))
        self.workers['item-0'][-1].file_progress.emit(4, 10)
        self.wait_until(lambda: row.percent.text() == '40%')

    def test_retry_preserves_both_nested_scroll_positions(self):
        scheduler = self.attach_queue(28)
        self.workers['item-2'][-1].fail()
        self.wait_until(lambda: self.window.download_queue.item_ids[0] == 'item-2')
        # Exercise the same overflow as a compact display with the logs open.
        self.window.activity_body.setMinimumHeight(1400)
        self.app.processEvents()
        outer = self.window.activity_scroll.verticalScrollBar()
        inner = self.window.download_queue.scroll.verticalScrollBar()
        self.assertGreater(outer.maximum(), 0)
        self.assertGreater(inner.maximum(), 0)
        outer.setValue(outer.maximum() // 2)
        inner.setValue(inner.maximum() // 2)
        old = (outer.value(), inner.value())
        self.window.handle_queue_action('item-2', 'retry')
        self.assertEqual('queued', scheduler.task('item-2').state)
        QTest.qWait(160)
        self.assertEqual(old, (outer.value(), inner.value()))
        self.workers['item-0'][-1].succeed(10)
        self.wait_until(lambda: scheduler.task('item-2').state == 'running')
        self.workers['item-2'][-1].fail('second failure')
        QTest.qWait(160)
        self.assertEqual('item-2', self.window.download_queue.item_ids[0])
        self.assertEqual(old, (outer.value(), inner.value()))

    def test_completed_queue_exposes_the_matching_folder_button(self):
        self.attach_queue(1)
        self.assertTrue(self.window.open_folder_button.isHidden())
        self.workers['item-0'][-1].succeed(10)
        self.wait_until(lambda: not self.window.open_folder_button.isHidden())
        self.assertIn('下载完成', self.window.status.text())
        with patch.object(desktop.QDesktopServices, 'openUrl', return_value=True) as opened:
            QTest.mouseClick(self.window.open_folder_button, desktop.Qt.MouseButton.LeftButton)
        self.assertEqual(self.root.resolve(), Path(opened.call_args.args[0].toLocalFile()).resolve())

    def test_tray_exit_requires_yes_while_downloading_and_no_keeps_running(self):
        store = AppState(self.root / 'state')
        journal = DownloadJournal(self.root / 'state')
        runtime = DesktopRuntime(self.app, store=store, journal=journal, tray_enabled=False)
        self.resources.append(runtime)
        runtime.window = self.window
        self.window.busy = True
        with patch('desktop_runtime.QMessageBox.question', return_value=QMessageBox.StandardButton.No) as question, \
                patch.object(runtime, 'prepare_exit') as prepare, patch.object(self.window, 'close') as close:
            runtime.request_exit()
            self.assertIn('检测到当前下载任务正在进行，是否继续退出？', question.call_args.args)
            self.assertEqual(QMessageBox.StandardButton.No, question.call_args.args[-1])
            prepare.assert_not_called()
            close.assert_not_called()
        with patch('desktop_runtime.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes), \
                patch.object(runtime, 'prepare_exit') as prepare, patch.object(self.window, 'close') as close:
            runtime.request_exit()
            prepare.assert_called_once()
            close.assert_called_once()

    def test_async_preparation_defers_start_and_preserves_saved_choices(self):
        journal = DownloadJournal(self.root / 'journal')
        self.resources.append(journal)
        self.window.services = SimpleNamespace(journal=journal)
        saved = dict(folder=str(self.root), profile='https://pawchive.pw/fanbox/user/offline',
            order='oldest', by_title=True, rename_order=False, queue_rename_order=True,
            start_number=1, queue_task=True, layout_version=2, content_mode='images')
        job_id = journal.create(saved)
        job = journal.load(job_id)
        job['status'] = 'paused'
        journal.save(job, dirty_records=[])
        self.window.title_check.setChecked(False)
        self.window.rename_check.setChecked(False)
        self.window.content_combo.setCurrentIndex(self.window.content_combo.findData('attachments'))
        entries = [dict(id='restored', title='restored album',
            url=saved['profile'] + '/post/1', formats=['.jpg', '.zip'],
            image_count=3, attachment_count=1, metadata={'job_id': job_id})]
        self.assertTrue(self.window._start_queue(entries, saved['profile']))
        preparation = self.window._queue_preparation
        self.assertIsNotNone(preparation)
        self.assertIsNone(self.window.queue_scheduler)
        self.assertEqual({}, self.workers)
        self.wait_until(lambda: self.window.queue_scheduler is not None and bool(self.workers))
        self.assertIsNone(self.window._queue_preparation)
        restored = self.window.queue_scheduler.task('restored')
        self.assertEqual(3, restored.files_total)
        self.assertFalse(restored.direct)
        self.assertTrue(restored.metadata['by_title'])
        self.assertTrue(restored.metadata['queue_rename_order'])
        self.assertEqual('images', restored.metadata['content_mode'])
        config = journal.load(job_id)['config']
        for key in ('folder', 'order', 'by_title', 'queue_rename_order', 'content_mode'):
            self.assertEqual(saved[key], config[key])

    def test_large_real_queue_yields_to_heartbeat_and_renders_every_row(self):
        journal = DownloadJournal(self.root / 'large-journal')
        self.resources.append(journal)
        self.window.services = SimpleNamespace(journal=journal)
        total = 300
        profile = 'https://pawchive.pw/fanbox/user/offline-batch'
        entries = [dict(id=f'large-{index}', title=f'Album {index}',
            url=f'{profile}/post/{index}', formats=['.jpg'],
            image_count=1, attachment_count=0, file_count=1,assets=[dict(
                url='https://file.pawchive.pw/data/aa/bb/'+str(index).zfill(64)+'.jpg',
                name=f'{index}.jpg',direct=False,index=0)]) for index in range(total)]
        observed = set()
        heartbeat = QTimer(self.window)
        heartbeat.setInterval(5)

        def sample_event_loop():
            preparation = self.window._queue_preparation
            if preparation is not None and preparation.isRunning():
                observed.add('preparation')
            scheduler = self.window.queue_scheduler
            if scheduler is not None and 0 < len(scheduler._tasks) < total:
                observed.add('insertion')
            if 0 < len(self.window.download_queue._rows) < total:
                observed.add('row-population')

        heartbeat.timeout.connect(sample_event_loop)
        heartbeat.start()
        insert_batch = self.window._queue_insert_batch

        def observed_batch():
            # This queued heartbeat observes a batch boundary without assuming
            # insertion must take longer than the periodic timer's interval.
            # A synchronous recursive implementation would run it only after
            # all 300 inserts and therefore fail the insertion-phase check.
            QTimer.singleShot(0, sample_event_loop)
            insert_batch()

        try:
            with patch.object(self.window, '_queue_insert_batch', new=observed_batch):
                self.assertTrue(self.window._start_queue(entries, profile,_destination=str(self.root)))
                self.assertEqual({}, self.workers, 'download started before preparation yielded')
                try:
                    self.wait_until(lambda: self.window.queue_scheduler is not None and
                        len(self.window.queue_scheduler.tasks) == total and
                        len(self.window.download_queue._rows) == total, timeout=15000)
                except AssertionError:
                    scheduler = self.window.queue_scheduler
                    self.fail(f'large queue did not settle: tasks={len(scheduler._tasks) if scheduler else 0}, '
                        f'rows={len(self.window.download_queue._rows)}, '
                        f'pending={len(getattr(self.window,"_queue_pending_entries",[]))}, '
                        f'preparing={self.window._queue_preparation is not None}, observed={observed}')
        finally:
            heartbeat.stop()
            heartbeat.deleteLater()
        self.assertTrue({'preparation', 'insertion', 'row-population'} <= observed,
                        f'event loop did not run during every queue phase: {observed}')
        self.assertIsNone(self.window._queue_preparation)
        self.assertEqual([entry['id'] for entry in entries], self.window.download_queue.item_ids)
        self.assertEqual(total, sum(task.files_total for task in self.window.queue_scheduler.tasks))
        self.assertEqual(5, sum(len(runs) for runs in self.workers.values()))
        self.assertTrue(all(runs[-1].isRunning() for runs in self.workers.values()))


if __name__ == '__main__':
    unittest.main()
