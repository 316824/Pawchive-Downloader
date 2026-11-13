import os
from pathlib import Path
import sys
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QObject, Signal, QThread

from download_scheduler import DownloadScheduler, has_direct_attachment


class FakeWorker(QObject):
    progress = Signal(str, int, int)
    finished_ok = Signal(int)
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.started = False
        self.interrupted = False
        self.preserve_on_interrupt = False

    def start(self):
        self.started = True

    def requestInterruption(self):
        self.interrupted = True


class ThreadLikeWorker(FakeWorker):
    finished = Signal()


class DownloadSchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.workers = {}

        def factory(task):
            worker = FakeWorker()
            self.workers[task.id] = worker
            return worker

        self.scheduler = DownloadScheduler(factory)

    def test_image_pool_is_limited_to_five_and_direct_tasks_are_serial(self):
        entries = [dict(title=f'image {n}', url=f'post-{n}', formats=['.jpg']) for n in range(6)]
        entries.append(dict(title='archive', url='post-direct', formats=['.zip']))
        tasks = self.scheduler.add_tasks(entries)
        self.scheduler.start()
        self.assertEqual(sum(task.state == 'running' for task in self.scheduler.tasks), 5)
        self.assertEqual(self.scheduler.task(tasks[5].id).state, 'queued')
        self.assertEqual(self.scheduler.task(tasks[6].id).state, 'queued')

        self.workers[tasks[0].id].finished_ok.emit(2)
        self.assertEqual(self.scheduler.task(tasks[5].id).state, 'running')
        self.assertEqual(self.scheduler.task(tasks[6].id).state, 'queued')

        for task in tasks[1:6]:
            self.workers[task.id].finished_ok.emit(1)
        self.assertEqual(self.scheduler.task(tasks[6].id).state, 'running')
        self.assertEqual(sum(task.direct and task.is_active for task in self.scheduler.tasks), 1)

    def test_pause_advances_queue_and_retry_returns_to_top(self):
        self.scheduler.MAX_IMAGE_WORKERS = 1
        tasks = self.scheduler.add_tasks([
            dict(title='first', url='post-1', formats=['.png']),
            dict(title='second', url='post-2', formats=['.png']),
            dict(title='third', url='post-3', formats=['.png']),
        ])
        self.scheduler.start()
        first = tasks[0]
        self.assertTrue(self.scheduler.pause(first.id))
        self.assertEqual(self.scheduler.task(first.id).state, 'pausing')
        self.assertTrue(self.workers[first.id].interrupted)
        self.workers[first.id].failed.emit('network error')
        self.assertEqual(self.scheduler.task(first.id).state, 'paused')
        self.assertEqual(self.scheduler.task(tasks[1].id).state, 'running')

        self.assertTrue(self.scheduler.retry(first.id))
        self.assertEqual(self.scheduler.ordered_tasks()[0].id, first.id)
        self.workers[tasks[1].id].finished_ok.emit(1)
        self.assertEqual(self.scheduler.task(first.id).state, 'running')

    def test_qthread_wrapper_is_kept_until_finished_signal(self):
        self.scheduler.MAX_IMAGE_WORKERS = 1
        self.workers = {}

        def factory(task):
            worker = ThreadLikeWorker()
            self.workers[task.id] = worker
            return worker

        self.scheduler.worker_factory = factory
        tasks = self.scheduler.add_tasks([
            dict(title='first', url='post-1', formats=['.png']),
            dict(title='second', url='post-2', formats=['.png']),
        ])
        self.scheduler.start()
        self.workers[tasks[0].id].finished_ok.emit(1)
        self.assertEqual(self.scheduler.task(tasks[0].id).state, 'completed')
        self.assertEqual(self.scheduler.task(tasks[1].id).state, 'queued')
        self.assertIn(tasks[0].id, self.scheduler._workers)
        self.workers[tasks[0].id].finished.emit()
        self.assertNotIn(tasks[0].id, self.scheduler._workers)
        self.assertEqual(self.scheduler.task(tasks[1].id).state, 'running')

    def test_cancelled_queued_task_is_recycled_and_selection_order_survives_reorder(self):
        self.scheduler.MAX_IMAGE_WORKERS = 1
        tasks = self.scheduler.add_tasks([
            dict(title='first', url='post-1', formats=['.png']),
            dict(title='second', url='post-2', formats=['.png']),
            dict(title='third', url='post-3', formats=['.png']),
        ])
        recycled = []
        self.scheduler.task_recycled.connect(recycled.append)
        self.scheduler.start()
        self.assertTrue(self.scheduler.cancel(tasks[2].id))
        self.assertEqual(self.scheduler.task(tasks[2].id).state, 'cancelled')
        self.assertEqual([item.id for item in recycled], [tasks[2].id])
        self.assertTrue(self.scheduler.reorder([tasks[1].id], tasks[0].id))
        self.assertEqual([item.id for item in self.scheduler.selection_order()],
                         [tasks[0].id, tasks[1].id, tasks[2].id])

    def test_resume_clicked_before_pause_thread_finishes_restarts_exactly_once(self):
        self.scheduler.worker_factory = lambda task: self.workers.setdefault(task.id, ThreadLikeWorker())
        task = self.scheduler.add_task(dict(title='first', url='post-1', formats=['.png']))
        self.scheduler.start()
        old = self.workers[task.id]
        self.assertTrue(self.scheduler.pause(task.id))
        self.assertTrue(self.scheduler.resume(task.id))
        old.failed.emit('interrupted')
        self.assertEqual(self.scheduler.task(task.id).state, 'paused')
        # A fresh worker is required because QThread interruption is sticky.
        fresh = ThreadLikeWorker()
        self.scheduler.worker_factory = lambda task: fresh
        old.finished.emit()
        self.assertTrue(fresh.started)
        self.assertEqual(self.scheduler.task(task.id).state, 'running')
        old.failed.emit('stale signal')
        self.assertEqual(self.scheduler.task(task.id).state, 'running')

    def test_retry_before_old_thread_finished_cannot_replace_its_worker(self):
        self.scheduler.worker_factory = lambda task: ThreadLikeWorker()
        task = self.scheduler.add_task(dict(title='first', url='post-1', formats=['.png']))
        self.scheduler.start()
        old = self.scheduler._workers[task.id]
        old.failed.emit('network error')
        self.assertTrue(self.scheduler.retry(task.id))
        self.assertIs(self.scheduler._workers[task.id], old)
        old.finished.emit()
        self.assertIsNot(self.scheduler._workers[task.id], old)
        self.assertEqual(task.state, 'running')
        self.assertTrue(task.metadata['failure_pinned'])
        self.scheduler._workers[task.id].finished_ok.emit(2)
        self.assertNotIn('failure_pinned', task.metadata)

    def test_failure_pin_stays_above_other_priority_changes_and_retry(self):
        self.scheduler.MAX_IMAGE_WORKERS = 1
        tasks = self.scheduler.add_tasks([
            dict(title=str(n), url=f'post-{n}', formats=['.png']) for n in range(3)])
        self.scheduler.start()
        self.workers[tasks[0].id].failed.emit('network error')
        self.scheduler.move_to_top([tasks[2].id])
        self.assertEqual(self.scheduler.ordered_tasks()[0].id, tasks[0].id)
        self.scheduler.retry(tasks[0].id)
        self.scheduler.move_to_top([tasks[2].id])
        self.assertEqual(self.scheduler.ordered_tasks()[0].id, tasks[0].id)

    def test_pause_all_does_not_start_waiting_tasks_and_resume_all_restarts_them(self):
        self.scheduler.MAX_IMAGE_WORKERS = 1
        tasks = self.scheduler.add_tasks([
            dict(title=str(n), url=f'post-{n}', formats=['.png']) for n in range(4)])
        self.scheduler.start()
        self.assertEqual(self.scheduler.pause_all(), 4)
        self.assertTrue(self.scheduler.has_paused_tasks)
        self.assertEqual(len(self.workers), 1)
        self.workers[tasks[0].id].failed.emit('interrupted')
        self.assertTrue(all(task.state == 'paused' for task in tasks))
        self.assertEqual(self.scheduler.resume_all(), 4)
        self.assertEqual(tasks[0].state, 'running')
        self.assertTrue(all(task.state == 'queued' for task in tasks[1:]))

    def test_auto_retry_backs_off_and_manual_retry_cancels_pending_timer(self):
        task = self.scheduler.add_task(dict(title='first', url='post-1', formats=['.png']))
        self.scheduler.start()
        first = self.workers[task.id]
        first.failed.emit('network error')
        self.assertEqual(task.state, 'failed')
        self.assertTrue(self.scheduler._retry_timer.isActive())
        self.assertGreaterEqual(self.scheduler._retry_timer.interval(), 5000)
        self.assertTrue(task.metadata['auto_retry_pending'])
        self.scheduler.retry(task.id)
        self.assertFalse(self.scheduler._retry_timer.isActive())
        self.assertIsNot(self.workers[task.id], first)
        self.assertEqual(task.state, 'running')
        self.workers[task.id].failed.emit('again')
        self.scheduler._retry_timer.stop()
        self.scheduler._run_auto_retry()
        self.assertEqual(task.metadata['auto_retry_count'], 1)
        self.assertEqual(task.state, 'running')

    def test_real_worker_signals_are_marshaled_to_scheduler_thread(self):
        class NativeWorker(QThread):
            progress = Signal(str, object, object)
            finished_ok = Signal(int)
            failed = Signal(str)
            def run(self):
                self.progress.emit('file.jpg', 1, 2)
                self.finished_ok.emit(1)
        self.scheduler.worker_factory = lambda task: NativeWorker()
        calls = []
        self.scheduler.task_updated.connect(lambda task: calls.append(QThread.currentThread()))
        task = self.scheduler.add_task(dict(title='native', url='post-1', formats=['.jpg']))
        self.scheduler.start()
        deadline = time.monotonic() + 3
        while self.scheduler._workers and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.001)
        self.assertEqual(task.state, 'completed')
        self.assertFalse(self.scheduler._workers)
        self.assertTrue(all(thread == self.scheduler.thread() for thread in calls))

    def test_bulk_add_emits_only_one_queue_change(self):
        changes = []
        self.scheduler.queue_changed.connect(lambda: changes.append(True))
        self.scheduler.add_tasks(dict(title=str(n), url=f'post-{n}', formats=['.png']) for n in range(300))
        self.assertEqual(len(changes), 1)

    def test_unknown_extensions_are_serial(self):
        self.assertTrue(has_direct_attachment(['.zip']))
        self.assertTrue(has_direct_attachment(['.mystery']))
        self.assertFalse(has_direct_attachment(['.jpg', '.webp']))


if __name__ == '__main__':
    unittest.main()
