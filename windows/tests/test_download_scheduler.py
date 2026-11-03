import os
from pathlib import Path
import sys
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QObject, Signal

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

    def test_unknown_extensions_are_serial(self):
        self.assertTrue(has_direct_attachment(['.zip']))
        self.assertTrue(has_direct_attachment(['.mystery']))
        self.assertFalse(has_direct_attachment(['.jpg', '.webp']))


if __name__ == '__main__':
    unittest.main()
