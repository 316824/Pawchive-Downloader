"""Actual App integration for local-only and partly local immutable plans."""
from pathlib import Path
from types import SimpleNamespace
import unittest

import test_queue_ui_regressions as fixture
from download_journal import DownloadJournal
from static_download_plan import assign_plans


class IncrementalQueueUiTests(unittest.TestCase):
    setUpClass = classmethod(fixture.QueueUiRegressionTests.setUpClass.__func__)
    setUp = fixture.QueueUiRegressionTests.setUp
    tearDown = fixture.QueueUiRegressionTests.tearDown
    make_worker = fixture.QueueUiRegressionTests.make_worker
    wait_until = fixture.QueueUiRegressionTests.wait_until

    def prepare(self):
        window = self.window
        window.artist_folder_check.setChecked(False)
        window.title_check.setChecked(False)
        window.rename_check.setChecked(True)
        window.content_combo.setCurrentIndex(window.content_combo.findData('images'))
        journal = DownloadJournal(self.root / 'journal')
        self.resources.append(journal)
        window.services = SimpleNamespace(journal=journal)
        profile = 'https://pawchive.pw/fanbox/user/123'
        entries = [dict(title='New', url=profile+'/post/new', date='2026-11-20',
                        assets=[dict(url='https://file.pawchive.pw/data/new.png',
                                     name='new.png', index=0)]),
                   dict(title='Old', url=profile+'/post/old', date='2026-10-9',
                        assets=[dict(url='https://file.pawchive.pw/data/old.jpg',
                                     name='old.jpg', index=0)])]
        plans = assign_plans(entries, dict(folder=str(self.root), content_mode='images',
                                          by_title=False, queue_rename_order=True))
        return window, journal, profile, entries, plans

    def test_all_existing_skips_live_queue_and_reaches_completed_without_worker(self):
        window, journal, profile, entries, plans = self.prepare()
        for entry in plans:
            asset = entry['static_plan'][0]
            (self.root / asset['save_name']).write_bytes(b'user image')
        self.assertTrue(window._start_queue(entries, profile, _destination=str(self.root)))
        self.wait_until(lambda: window.queue_scheduler is not None and not window.busy, timeout=6000)
        self.assertFalse(window.queue_scheduler.tasks)
        self.assertEqual({}, self.workers)
        self.assertEqual(2, window._queue_completed_files)
        self.assertEqual(100, window.progress.value)
        self.assertIn('下载完成', window.status.text())
        self.assertTrue(window.open_folder_button.isVisible())
        self.assertEqual([], journal.list_recoverable())

    def test_existing_album_removed_but_missing_album_keeps_global_number(self):
        window, journal, profile, entries, plans = self.prepare()
        old = plans[1]['static_plan'][0]
        old_path = self.root / old['save_name']
        self.assertEqual('1.jpg', old_path.name)
        old_path.write_bytes(b'keep existing content')
        self.assertTrue(window._start_queue(entries, profile, _destination=str(self.root)))
        self.wait_until(lambda: bool(self.workers), timeout=6000)
        tasks = window.queue_scheduler.tasks
        self.assertEqual(1, len(tasks))
        task = tasks[0]
        self.assertEqual(profile+'/post/new', task.url)
        saved = journal.load(task.metadata['job_id'])
        self.assertEqual('2.png', saved['config']['static_plan'][0]['save_name'])
        self.assertEqual(1, window._queue_skipped_files)
        self.assertEqual(1, window._queue_completed_files)
        self.workers[task.id][-1].succeed(1)
        self.wait_until(lambda: not window.busy)
        self.assertEqual(2, window._queue_completed_files)
        self.assertEqual(b'keep existing content', old_path.read_bytes())


if __name__ == '__main__':
    unittest.main()
